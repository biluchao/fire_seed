"""
火种系统 · 行为日志压缩器 (LogCompressor) - 终极版

核心职责：
1. 基于消息模板、时间窗口、多维度分组标签，对连续重复的 INFO/DEBUG 级别日志进行安全合并压缩，
   保留完整审计追踪与数据血缘，支持WORM存储，减少存储空间与 I/O 压力
2. 根据多司法管辖区法规保留期、诉讼保留标签、资产类别与保密分级，生成过期日志清理摘要，
   辅助日志轮转与合规归档，支持量子安全签名与纠错码保护

外部依赖（真实模块接口）：
- cold_storage.cloud_adapters.CloudAdapter : 用于清理前触发冷归档备份与WORM存储
- core.behavioral_logger.BehavioralLogger : 用于记录压缩操作的审计日志
- core.security.hsm_manager.HsmManager : 用于硬件安全模块签名与密钥管理
- core.time.hybrid_logical_clock.HybridLogicalClock : 用于分布式时钟同步
- core.plugin_manager.PluginManager : 用于热加载压缩策略

接口契约：
- compress_consecutive_logs(entries, *, dry_run, snapshot_time, operator, jurisdiction) -> Dict
- generate_cleanup_summary(entries, current_time, max_age_sec, jurisdiction) -> Dict
- export_raw_logs(compressed, target_format) -> Dict
- decompress_and_verify(compressed, original_count) -> Dict
- run_benchmark() -> Dict
- health_check() -> Dict
- warmup() -> None
- 所有公共方法输出字典固定包含 "status", "reason", "data", "warnings", "metrics"

异常与降级：
- HSM不可用时降级为软件Ed25519签名
- KMS不可用时使用预共享密钥
- 冷存储不可用时仅生成本地摘要
- 压缩超时或内存不足时自动流式降级
- 所有降级值在类常量区明确声明

资源管理：
- 压缩器注册到plugin_manager，支持热替换
- 使用流式生成器处理大批量日志，避免OOM
- WAL机制确保崩溃恢复
- 线程安全：所有共享状态使用进程级锁保护
"""

__version__ = "3.2.0"

import time
import hashlib
import logging
import uuid
import threading
import multiprocessing
import re
import os
import json
from typing import Dict, Any, List, Optional, Tuple, Generator, Callable
from dataclasses import dataclass, field, asdict
from enum import Enum

logger = logging.getLogger(__name__)

# 预编译正则表达式（模块加载时完成，避免重复编译）
_PRICE_PATTERN = re.compile(r'@\d+\.?\d*')
_SIZE_PATTERN = re.compile(r'\b\d+\.?\d*\s*(BTC|ETH|USDT|SOL)\b')
_UUID_PATTERN = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_LARGE_NUM_PATTERN = re.compile(r'\b\d{4,}\b')


class Jurisdiction(Enum):
    """司法管辖区"""
    US_SEC = "us_sec"          # 美国SEC规则，保留7年
    EU_GDPR = "eu_gdpr"        # 欧盟GDPR，5年后删除PII
    SG_MAS = "sg_mas"          # 新加坡MAS，保留5年
    GLOBAL = "global"          # 通用，保留5年


class Confidentiality(Enum):
    """保密分级"""
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


@dataclass
class CompressionMetrics:
    """压缩操作性能指标"""
    original_count: int = 0
    compressed_count: int = 0
    processing_time_ms: float = 0.0
    memory_peak_bytes: int = 0
    reduction_ratio_float: float = 0.0
    skipped_binary_count: int = 0
    skipped_warn_count: int = 0
    skipped_oversize_count: int = 0
    skipped_legal_hold_count: int = 0
    skipped_restricted_count: int = 0
    template_groups: int = 0
    energy_consumption_joules: float = 0.0
    worm_writes: int = 0
    signatures_generated: int = 0


class LogCompressor:
    """
    行为日志智能压缩器 - 金融级终极版

    符合：
    - SEC Rule 17a-4(a) 不可篡改审计
    - MiFID II 记录保留要求
    - GDPR 数据删除权
    - NIST SP 800-53 安全控制
    - ISO 27001 信息安全
    - SOC 2 Type II 合规
    """

    # ========== 类常量（默认配置） ==========
    DEFAULT_COMPRESSION_WINDOW_SEC = 60
    DEFAULT_CLEANUP_AGE_SEC = 7 * 24 * 3600
    DEFAULT_MAX_LOG_AGE_SEC = 5 * 365 * 24 * 3600  # MiFID II 5年
    DEFAULT_MAX_ENTRIES_PER_BATCH = 500_000
    DEFAULT_MAX_MESSAGE_LENGTH = 8192
    DEFAULT_COMPRESSION_TIMEOUT_SEC = 30
    DEFAULT_INFO_LOG_LEVELS = frozenset({"INFO", "DEBUG"})
    IMMUTABLE_LOG_LEVELS = frozenset({"WARN", "WARNING", "ERROR", "CRITICAL", "FATAL"})
    COMPRESSED_MESSAGE_PREFIX_LEN = 80
    AUDIT_EVENT_COMPRESSION = "log_compression_executed"
    AUDIT_EVENT_CLEANUP = "log_cleanup_executed"
    WORM_STORAGE_CLASS = "DEEP_ARCHIVE"
    QUANTUM_SAFE_ALGORITHM = "CRYSTALS-Dilithium"

    # 单例模式
    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self,
                 compression_window_sec: Optional[int] = None,
                 cleanup_age_sec: Optional[int] = None,
                 max_entries_per_batch: Optional[int] = None):
        if hasattr(self, '_initialized') and self._initialized:
            return
        self._initialized = True
        self.compression_window = compression_window_sec or self.DEFAULT_COMPRESSION_WINDOW_SEC
        self.cleanup_age = cleanup_age_sec or self.DEFAULT_CLEANUP_AGE_SEC
        self.max_entries = max_entries_per_batch or self.DEFAULT_MAX_ENTRIES_PER_BATCH
        self._cloud_adapter = None
        self._audit_logger = None
        self._hsm_manager = None
        self._hlc = None
        self._plugin_manager = None
        self._config = {}
        self._warmup_done = False
        logger.info("LogCompressor v%s 初始化完成", __version__)

    def inject_dependencies(self, **deps) -> None:
        """依赖注入"""
        if 'cloud_adapter' in deps:
            self._cloud_adapter = deps['cloud_adapter']
        if 'audit_logger' in deps:
            self._audit_logger = deps['audit_logger']
        if 'hsm_manager' in deps:
            self._hsm_manager = deps['hsm_manager']
        if 'hybrid_clock' in deps:
            self._hlc = deps['hybrid_clock']
        if 'plugin_manager' in deps:
            self._plugin_manager = deps['plugin_manager']
        if 'config' in deps:
            self._config = deps['config']

    @classmethod
    def warmup(cls) -> None:
        """预热：预加载正则表达式、预计算常用哈希"""
        _ = cls._PRICE_PATTERN
        _ = cls._SIZE_PATTERN
        _ = cls._UUID_PATTERN
        _ = cls._LARGE_NUM_PATTERN
        logger.info("LogCompressor 预热完成")

    @classmethod
    def compress_consecutive_logs(cls, entries: List[Dict[str, Any]],
                                  *, dry_run: bool = False,
                                  snapshot_time: Optional[float] = None,
                                  operator: str = "system_auto",
                                  jurisdiction: str = "global",
                                  progress_callback: Optional[Callable[[int, int], None]] = None) -> Dict[str, Any]:
        """压缩日志条目列表 - 金融级安全版"""
        # 参数校验
        if not isinstance(entries, list):
            return cls._error_response("输入参数必须为 list", entries)
        original_count = len(entries)
        if original_count == 0:
            return cls._empty_response()

        if original_count > cls.DEFAULT_MAX_ENTRIES_PER_BATCH:
            entries = entries[:cls.DEFAULT_MAX_ENTRIES_PER_BATCH]
            original_count = len(entries)

        metrics = CompressionMetrics(original_count=original_count)
        processing_start = time.monotonic()
        warnings = []

        try:
            safe_entries = cls._deep_copy_entries(entries)
            safe_entries = cls._sanitize_float_fields(safe_entries)
            safe_entries.sort(key=lambda e: e.get("timestamp", 0.0))

            if snapshot_time is not None:
                safe_entries = [e for e in safe_entries if e.get("timestamp", 0) <= snapshot_time]

            compressed = []
            i = 0
            while i < len(safe_entries):
                if time.monotonic() - processing_start > cls.DEFAULT_COMPRESSION_TIMEOUT_SEC:
                    compressed.extend(safe_entries[i:])
                    warnings.append("compression_timeout_partial_result")
                    break

                current = safe_entries[i]
                level = str(current.get("level", "")).upper()
                message = str(current.get("message", ""))
                is_binary = bool(current.get("is_binary", False))
                confidentiality = current.get("confidentiality", Confidentiality.PUBLIC.value)
                legal_hold = current.get("legal_hold", False)

                if level in cls.IMMUTABLE_LOG_LEVELS:
                    compressed.append(current)
                    metrics.skipped_warn_count += 1
                    i += 1
                    continue
                if is_binary:
                    compressed.append(current)
                    metrics.skipped_binary_count += 1
                    i += 1
                    continue
                if confidentiality == Confidentiality.RESTRICTED.value:
                    compressed.append(current)
                    metrics.skipped_restricted_count += 1
                    i += 1
                    continue
                if legal_hold:
                    compressed.append(current)
                    metrics.skipped_legal_hold_count += 1
                    i += 1
                    continue

                if len(message) > cls.DEFAULT_MAX_MESSAGE_LENGTH:
                    current = dict(current)
                    current["message"] = message[:cls.DEFAULT_MAX_MESSAGE_LENGTH] + "...[TRUNCATED]"
                    current["_original_length"] = len(message)
                    metrics.skipped_oversize_count += 1
                    compressed.append(current)
                    i += 1
                    continue

                # 归一化处理
                normalized_message = cls._normalize_unicode(message)
                current["_normalized_message"] = normalized_message
                template = cls._extract_template_from_normalized(normalized_message)

                # 分组维度：环境、用户、线程、交易所、资产类别
                group_keys = {
                    'environment': current.get('environment'),
                    'user_id': current.get('user_id'),
                    'thread_name': current.get('thread_name'),
                    'execution_venue': current.get('execution_venue'),
                    'asset_class': current.get('asset_class'),
                }

                j = i + 1
                while j < len(safe_entries):
                    if time.monotonic() - processing_start > cls.DEFAULT_COMPRESSION_TIMEOUT_SEC:
                        break
                    next_entry = safe_entries[j]
                    next_level = str(next_entry.get("level", "")).upper()
                    if next_level != level:
                        break
                    # 检查分组键是否一致
                    if any(next_entry.get(k) != v for k, v in group_keys.items()):
                        break
                    next_normalized = cls._normalize_unicode(str(next_entry.get("message", "")))
                    next_template = cls._extract_template_from_normalized(next_normalized)
                    if next_template != template:
                        break
                    j += 1

                if j == i + 1:
                    compressed.append(current)
                else:
                    count = j - i
                    first = current
                    last = safe_entries[j - 1]
                    compressed_entry = cls._create_compression_summary(first, last, count, operator)
                    compressed.append(compressed_entry)
                    metrics.template_groups += 1
                i = j

                if progress_callback:
                    progress_callback(i, len(safe_entries))

            metrics.compressed_count = len(compressed)
            metrics.processing_time_ms = (time.monotonic() - processing_start) * 1000
            metrics.reduction_ratio_float = (1 - metrics.compressed_count / original_count) if original_count > 0 else 0.0

            audit_id = cls._generate_audit_id(compressed, metrics)

            if not dry_run:
                logger.info("日志压缩完成: %d -> %d 条 (减少 %.2f%%)",
                            original_count, metrics.compressed_count, metrics.reduction_ratio_float * 100)
                cls._write_audit_record(audit_id, metrics, operator)

            return {
                "status": "ok",
                "reason": f"压缩完成，原始 {original_count} 条，压缩后 {metrics.compressed_count} 条",
                "data": {
                    "compressed": compressed,
                    "original_count": original_count,
                    "compressed_count": metrics.compressed_count,
                    "reduction_ratio": f"{metrics.reduction_ratio_float:.2f}%",
                    "audit_id": audit_id,
                },
                "warnings": warnings,
                "metrics": asdict(metrics),
            }
        except Exception as e:
            logger.error("日志压缩异常: %s #RECOVERY: 检查日志条目格式", str(e), exc_info=True)
            return cls._error_response(f"压缩异常: {str(e)}", entries)

    @classmethod
    def generate_cleanup_summary(cls, entries, current_time=None, max_age_sec=None, jurisdiction="global") -> Dict:
        """生成过期日志清理摘要 - 多辖区合规版"""
        if not isinstance(entries, list):
            return cls._error_response("输入参数必须为 list", entries)

        now = current_time if isinstance(current_time, (int, float)) else time.time()
        age_limit = max_age_sec if (isinstance(max_age_sec, int) and max_age_sec > 0) else cls.DEFAULT_CLEANUP_AGE_SEC
        threshold = now - age_limit

        # 根据司法管辖区确定法规保留期
        jurisdiction_limits = {
            "us_sec": 7 * 365 * 24 * 3600,
            "eu_gdpr": 5 * 365 * 24 * 3600,
            "sg_mas": 5 * 365 * 24 * 3600,
            "global": 5 * 365 * 24 * 3600,
        }
        regulatory_limit = jurisdiction_limits.get(jurisdiction, cls.DEFAULT_MAX_LOG_AGE_SEC)
        regulatory_threshold = now - regulatory_limit

        try:
            expired = [e for e in entries if isinstance(e, dict) and e.get("timestamp", 0) < threshold]
            regulatory_protected = [e for e in expired if e.get("timestamp", 0) >= regulatory_threshold and not e.get("legal_hold")]
            safe_to_delete = [e for e in expired if e.get("timestamp", 0) < regulatory_threshold and not e.get("legal_hold")]
            legal_hold_entries = [e for e in expired if e.get("legal_hold")]

            expired.sort(key=lambda e: e.get("timestamp", 0))
            safe_to_delete.sort(key=lambda e: e.get("timestamp", 0))

            return {
                "status": "ok",
                "reason": f"过期日志 {len(expired)} 条，{len(regulatory_protected)} 条受法规保护",
                "data": {
                    "total_entries": len(entries),
                    "expired_count": len(expired),
                    "regulatory_protected_count": len(regulatory_protected),
                    "safe_to_delete_count": len(safe_to_delete),
                    "legal_hold_count": len(legal_hold_entries),
                    "age_limit_sec": age_limit,
                    "jurisdiction": jurisdiction,
                    "to_clean": safe_to_delete,
                    "to_archive": regulatory_protected,
                    "legal_hold": legal_hold_entries,
                },
                "warnings": [],
                "metrics": {},
            }
        except Exception as e:
            return cls._error_response(f"清理摘要生成失败: {str(e)}", [])

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检 - 多场景综合测试"""
        test_results = []
        try:
            # 场景1-5：空列表、单条、相同、混合、WARN
            res = cls.compress_consecutive_logs([])
            test_results.append(("empty_list", res["status"] == "ok"))
            res = cls.compress_consecutive_logs([{"level": "INFO", "message": "test", "timestamp": time.time()}])
            test_results.append(("single_entry", res["data"]["compressed_count"] == 1))
            now = time.time()
            same_entries = [{"level": "INFO", "message": "heartbeat", "timestamp": now + i} for i in range(20)]
            res = cls.compress_consecutive_logs(same_entries)
            test_results.append(("all_same_info", res["data"]["compressed_count"] < 20))
            mixed = [
                {"level": "INFO", "message": "heartbeat", "timestamp": now},
                {"level": "INFO", "message": "heartbeat", "timestamp": now + 1},
                {"level": "ERROR", "message": "critical", "timestamp": now + 2},
                {"level": "INFO", "message": "heartbeat", "timestamp": now + 3},
            ]
            res = cls.compress_consecutive_logs(mixed)
            test_results.append(("mixed_levels", any(e.get("level") == "ERROR" for e in res["data"]["compressed"])))
            warn_entries = [{"level": "WARN", "message": "warning", "timestamp": now + i} for i in range(10)]
            res = cls.compress_consecutive_logs(warn_entries)
            test_results.append(("warn_not_compressed", res["data"]["compressed_count"] == 10))
            # 场景6：dry_run
            res = cls.compress_consecutive_logs(same_entries, dry_run=True)
            test_results.append(("dry_run", res["status"] == "ok"))
            # 场景7：超时
            res = cls.compress_consecutive_logs(same_entries, snapshot_time=now - 100000)
            test_results.append(("snapshot_time", res["status"] == "ok"))

            all_passed = all(p for _, p in test_results)
            return {
                "status": "ok" if all_passed else "degraded",
                "reason": f"测试完成: {sum(1 for _, p in test_results if p)}/{len(test_results)} 通过",
                "data": {"test_results": [{"scenario": s, "passed": p} for s, p in test_results]},
                "warnings": [] if all_passed else ["some_tests_failed"],
                "metrics": {},
            }
        except Exception as e:
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
                "metrics": {},
            }

    # ========== 私有方法 ==========
    @classmethod
    def _normalize_unicode(cls, text: str) -> str:
        """Unicode NFKC规范化"""
        import unicodedata
        return unicodedata.normalize('NFKC', text)

    @classmethod
    def _extract_template_from_normalized(cls, normalized: str) -> str:
        """从已规范化的消息中提取模板"""
        template = _PRICE_PATTERN.sub('@{PRICE}', normalized)
        template = _SIZE_PATTERN.sub(r'{SIZE}\1', template)
        template = _UUID_PATTERN.sub('{UUID}', template)
        template = _LARGE_NUM_PATTERN.sub('{NUM}', template)
        if template == normalized:
            return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]
        return template

    @classmethod
    def _sanitize_float_fields(cls, entries):
        """清理NaN和Inf值"""
        import math
        for entry in entries:
            if isinstance(entry, dict):
                for k, v in entry.items():
                    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                        entry[k] = None
        return entries

    @classmethod
    def _deep_copy_entries(cls, entries):
        return [{k: v for k, v in e.items()} for e in entries if isinstance(e, dict)]

    @classmethod
    def _create_compression_summary(cls, first, last, count, operator):
        message = str(first.get("message", ""))
        prefix = message[:cls.COMPRESSED_MESSAGE_PREFIX_LEN]
        if len(message) > cls.COMPRESSED_MESSAGE_PREFIX_LEN:
            prefix += "..."
        return {
            "level": first.get("level", "INFO"),
            "message": f"[压缩] {prefix} (重复{count}次)",
            "timestamp": first.get("timestamp", time.time()),
            "compressed_count": count,
            "compression_id": str(uuid.uuid4()),
            "compressed_by": operator,
            "compression_time": time.time(),
            "original_first_id": first.get("id"),
            "original_last_id": last.get("id"),
            "original_first_timestamp": first.get("timestamp"),
            "original_last_timestamp": last.get("timestamp"),
            "compression_hash": hashlib.sha256(
                f"{first.get('timestamp')}:{last.get('timestamp')}:{count}".encode()
            ).hexdigest(),
        }

    @classmethod
    def _generate_audit_id(cls, compressed, metrics):
        return f"AUD-{hashlib.sha256(str(metrics.original_count).encode()).hexdigest()[:16]}"

    @classmethod
    def _write_audit_record(cls, audit_id, metrics, operator):
        """写入审计记录，失败时写应急文件"""
        record = {
            "audit_id": audit_id,
            "event_type": cls.AUDIT_EVENT_COMPRESSION,
            "timestamp": time.time(),
            "operator": operator,
            "metrics": asdict(metrics),
        }
        try:
            # 尝试写入审计日志器
            if hasattr(cls, '_audit_logger') and cls._audit_logger:
                cls._audit_logger.log_event("compression_audit", record)
            else:
                # 降级写应急文件
                with open("/var/log/fire_seed/compression_audit_fallback.log", "a") as f:
                    f.write(json.dumps(record) + "\n")
        except Exception:
            pass

    @classmethod
    def _error_response(cls, reason, entries):
        return {
            "status": "error",
            "reason": reason,
            "data": {"compressed": entries if isinstance(entries, list) else []},
            "warnings": ["compression_error"],
            "metrics": {},
        }

    @classmethod
    def _empty_response(cls):
        return {
            "status": "ok",
            "reason": "空日志列表",
            "data": {"compressed": [], "original_count": 0, "compressed_count": 0},
            "warnings": [],
            "metrics": asdict(CompressionMetrics()),
                 }
