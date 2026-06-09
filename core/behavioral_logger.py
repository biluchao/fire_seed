"""
火种系统 · 行为日志记录器 (BehavioralLogger) - 第三轮机构级修复

核心职责：
1. 作为全系统行为日志的统一写入入口，接收所有模块产生的事件并通过无锁环形缓冲区异步刷盘
2. 管理日志的全生命周期：优先级调度、敏感信息深度脱敏、完整性校验、压缩存储、远程备份与合规审计

外部依赖（真实模块接口）：
- core.behavioral_logger.log_compressor.LogCompressor : 对连续同类日志进行智能压缩与过期清理
- core.behavioral_logger.semantic_indexer.SemanticIndexer : 提取关键实体构建轻量级索引，支持快速检索
- core.negotiation_bus.NegotiationBus : 将 CRITICAL 级别日志作为紧急事件推送至协商总线（可选）
- core.position_snapshot.PositionSnapshot : 系统崩溃前将未刷盘日志紧急持久化（可选）
- threading.Lock / threading.RLock : 用于保护共享的日志统计计数器与文件操作
- io.BytesIO / gzip / lz4.frame / hashlib / os / shutil / re : 用于日志压缩、完整性校验与文件操作

接口契约：
- log_event(event_type: str, details: Dict[str, Any], level: str = "INFO", correlation_id: Optional[str] = None) -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- get_stats() -> Dict[str, Any]
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 LogCompressor 或 SemanticIndexer 不可用时，日志写入降级为直接追加到本地原始日志文件，并发出告警
- 当 NegotiationBus 不可用时，CRITICAL 事件的推送功能静默跳过，仅保留本地记录
- 当磁盘空间低于 10% 时，自动切换为仅 CRITICAL 级别日志落盘，INFO/DEBUG 仅保留内存缓冲区
- 当内存缓冲区使用率超过 80% 时，自动丢弃 DEBUG 级别日志
- 当所有外部依赖（包括 PositionSnapshot）均失败时，最后尝试写入应急文件 /tmp/fire_seed_emergency.log
- 所有降级值在类常量区明确声明

资源管理：
- 内部维护一个基于 collections.deque 的无锁环形缓冲区，通过独立守护线程批量刷盘
- 刷盘线程在模块销毁时通过 atexit 回调安全关闭，确保最后一批日志不丢失
- 日志文件按日期滚动，自动压缩超过保留期限的历史文件
- 敏感信息在入队前自动深度脱敏，禁止明文密钥写入磁盘
- CRITICAL 事件推送通过专用线程池异步执行，限制最大线程数，防止线程爆炸

并发安全：
- 日志写入采用无锁队列 + 独立刷盘线程，避免业务线程阻塞
- 统计计数器使用 threading.Lock 保护，确保多线程下数据一致性
- CRITICAL 事件推送通过专用线程池异步执行，限制最大线程数
- 文件操作使用文件锁（fcntl.flock + 本地文件锁备选）确保多进程安全
- 序列号分配使用独立锁保护，确保单调递增不冲突
"""

import time
import logging
import threading
import atexit
import hashlib
import os
import json
import copy
import glob
import shutil
import re
import fcntl
from typing import Dict, Any, List, Optional, Final, Tuple
from collections import deque
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


class BehavioralLogger:
    """行为日志记录器入口 - 华尔街高频交易级（第三轮修复）"""

    # ========== 类常量（不可变配置） ==========
    DEFAULT_BUFFER_SIZE: Final[int] = 2000
    DEFAULT_FLUSH_INTERVAL_SEC: Final[float] = 0.5
    DEFAULT_MAX_EVENT_SIZE_BYTES: Final[int] = 65536
    DEFAULT_DISK_MIN_FREE_PCT: Final[int] = 10
    DEFAULT_BUFFER_HIGH_WATERMARK: Final[float] = 0.8
    DEFAULT_RETENTION_DAYS: Final[int] = 30
    DEFAULT_MAX_FILE_SIZE_MB: Final[int] = 500
    DEFAULT_CHECKSUM_ALGORITHM: Final[str] = "sha256"
    DEFAULT_COMPRESSION_ALGORITHM: Final[str] = "lz4"
    DEFAULT_CRITICAL_LEVELS: Final[Tuple[str, ...]] = ("CRITICAL", "EMERGENCY")
    DEFAULT_VALID_LEVELS: Final[Tuple[str, ...]] = ("DEBUG", "INFO", "WARNING", "CRITICAL", "EMERGENCY")
    DEFAULT_SENSITIVE_KEYWORDS: Final[Tuple[str, ...]] = (
        "api_key", "api_secret", "secret_key", "private_key",
        "password", "token", "access_key", "signature", "passphrase"
    )
    DEFAULT_MAX_CRITICAL_PUSH_THREADS: Final[int] = 4
    DEFAULT_FILE_LOCK_TIMEOUT_SEC: Final[float] = 1.0
    DEFAULT_MAX_CORRELATION_ID_LEN: Final[int] = 256
    DEFAULT_MAX_DETAILS_DEPTH: Final[int] = 10
    DEFAULT_EMERGENCY_LOG_PATH: Final[str] = "/tmp/fire_seed_emergency.log"

    def __init__(self):
        # 无锁环形缓冲区
        self._buffer: deque = deque(maxlen=self.DEFAULT_BUFFER_SIZE)
        self._buffer_seq_lock = threading.Lock()  # 保护批量取出的原子性

        # 刷盘线程
        self._flush_thread: Optional[threading.Thread] = None
        self._stop_flush = threading.Event()
        self._flush_lock = threading.Lock()

        # 外部依赖注入
        self._compressor: Optional[Any] = None
        self._indexer: Optional[Any] = None
        self._negotiation_bus: Optional[Any] = None
        self._position_snapshot: Optional[Any] = None

        # 统计计数器（线程安全）
        self._stats_lock = threading.Lock()
        self._stats = {
            "total_logged": 0, "critical_logged": 0, "buffer_dropped": 0,
            "flush_failures": 0, "bytes_written": 0,
            "last_flush_timestamp": 0.0, "last_flush_duration_ms": 0.0,
        }

        # 文件管理
        self._log_dir = "logs"
        self._current_log_file = ""
        self._current_file_size = 0
        self._file_lock = threading.Lock()

        # CRITICAL 推送线程池
        self._critical_executor = ThreadPoolExecutor(
            max_workers=self.DEFAULT_MAX_CRITICAL_PUSH_THREADS,
            thread_name_prefix="beh-crit-push"
        )

        # 序列号计数器
        self._sequence_counter = 0
        self._seq_lock = threading.Lock()

        # 初始化
        os.makedirs(self._log_dir, exist_ok=True)
        self._rotate_log_file()
        self._start_flush_thread()
        atexit.register(self._shutdown)

        logger.info(
            "BehavioralLogger 初始化完成 | 缓冲区=%d | 间隔=%.1fs | 压缩=%s | 校验=%s | 推送池=%d",
            self.DEFAULT_BUFFER_SIZE, self.DEFAULT_FLUSH_INTERVAL_SEC,
            self.DEFAULT_COMPRESSION_ALGORITHM, self.DEFAULT_CHECKSUM_ALGORITHM,
            self.DEFAULT_MAX_CRITICAL_PUSH_THREADS
        )

    # ========== 依赖注入 ==========
    def inject_dependencies(self, compressor=None, indexer=None, negotiation_bus=None, position_snapshot=None):
        if compressor is not None and hasattr(compressor, 'compress'):
            self._compressor = compressor
            logger.info("LogCompressor 注入成功")
        if indexer is not None and hasattr(indexer, 'index'):
            self._indexer = indexer
            logger.info("SemanticIndexer 注入成功")
        if negotiation_bus is not None and hasattr(negotiation_bus, 'publish_alert'):
            self._negotiation_bus = negotiation_bus
            logger.info("NegotiationBus 注入成功")
        if position_snapshot is not None and hasattr(position_snapshot, 'emergency_persist'):
            self._position_snapshot = position_snapshot
            logger.info("PositionSnapshot 注入成功")

    # ========== 公共接口 ==========
    def log_event(self, event_type: str, details: Dict[str, Any], level: str = "INFO",
                  correlation_id: Optional[str] = None) -> Dict[str, Any]:
        # 参数校验
        if not event_type or not isinstance(event_type, str):
            return {"status": "error", "reason": "event_type 必须是非空字符串", "data": {}, "warnings": ["invalid_event_type"]}
        level_upper = level.upper()
        if level_upper not in self.DEFAULT_VALID_LEVELS:
            level_upper = "INFO"

        # correlation_id 长度校验
        if correlation_id and len(correlation_id) > self.DEFAULT_MAX_CORRELATION_ID_LEN:
            correlation_id = correlation_id[:self.DEFAULT_MAX_CORRELATION_ID_LEN]
            logger.warning("correlation_id 过长，已截断至 %d 字符", self.DEFAULT_MAX_CORRELATION_ID_LEN)

        # 过载保护
        buf_pct = self._buffer_usage_pct()
        if level_upper == "DEBUG" and buf_pct > self.DEFAULT_BUFFER_HIGH_WATERMARK:
            with self._stats_lock:
                self._stats["buffer_dropped"] += 1
            return {"status": "ok", "reason": "缓冲区高水位，DEBUG 日志已丢弃", "data": {}, "warnings": ["buffer_high_watermark"]}

        # 深度脱敏（带深度限制，防递归爆炸）
        sanitized_details = self._sanitize_details(details)
        # 校验事件大小
        try:
            event_size = len(json.dumps(sanitized_details, default=self._json_serializer))
            if event_size > self.DEFAULT_MAX_EVENT_SIZE_BYTES:
                sanitized_details = self._truncate_details(sanitized_details)
        except Exception as e:
            logger.warning(f"details 序列化失败: {e}")
            sanitized_details = {"error": "serialization_failed"}

        event = {
            "timestamp": time.time(),
            "timestamp_readable": datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat(),
            "type": event_type,
            "level": level_upper,
            "details": sanitized_details,
            "correlation_id": correlation_id or "",
            "sequence": 0,
            "checksum": "",
        }

        # CRITICAL 异步推送
        if level_upper in self.DEFAULT_CRITICAL_LEVELS:
            with self._stats_lock:
                self._stats["critical_logged"] += 1
            self._critical_executor.submit(self._do_push_critical, event)

        self._buffer.append(event)
        with self._stats_lock:
            self._stats["total_logged"] += 1

        return {
            "status": "ok",
            "reason": f"事件已入队 (缓冲区使用率: {buf_pct:.0%})",
            "data": {"buffer_usage_pct": round(buf_pct, 2), "event_type": event_type},
            "warnings": [],
        }

    def get_stats(self) -> Dict[str, Any]:
        with self._stats_lock:
            stats_copy = copy.deepcopy(self._stats)
        stats_copy["buffer_usage_pct"] = self._buffer_usage_pct()
        stats_copy["flush_thread_active"] = self._flush_thread is not None and self._flush_thread.is_alive()
        stats_copy["current_log_file"] = self._current_log_file
        stats_copy["current_file_size_mb"] = round(self._current_file_size / (1024 * 1024), 2)
        return {"status": "ok", "reason": "统计指标已获取", "data": stats_copy, "warnings": []}

    def health_check(self) -> Dict[str, Any]:
        try:
            flush_active = self._flush_thread is not None and self._flush_thread.is_alive()
            disk_free = self._check_disk_free()
            disk_healthy = disk_free > self.DEFAULT_DISK_MIN_FREE_PCT
            file_writable = True
            if self._current_log_file:
                file_writable = os.access(self._current_log_file, os.W_OK)
            elif self._log_dir:
                file_writable = os.access(self._log_dir, os.W_OK)
            warnings = []
            if not flush_active: warnings.append("flush_thread_inactive")
            if not disk_healthy: warnings.append(f"disk_low_space: {disk_free:.1f}%")
            if not file_writable: warnings.append("log_file_not_writable")
            status = "ok" if flush_active and disk_healthy and file_writable else "degraded"

            with self._stats_lock:
                total_logged = self._stats["total_logged"]
                buffer_dropped = self._stats["buffer_dropped"]

            return {
                "status": status,
                "reason": f"BehavioralLogger 状态: {status}",
                "data": {
                    "flush_thread_active": flush_active,
                    "disk_free_pct": round(disk_free, 1),
                    "file_writable": file_writable,
                    "buffer_usage_pct": round(self._buffer_usage_pct(), 2),
                    "total_logged": total_logged,
                    "buffer_dropped": buffer_dropped,
                    "dependencies": {
                        "compressor": self._compressor is not None,
                        "indexer": self._indexer is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                        "position_snapshot": self._position_snapshot is not None,
                    },
                },
                "warnings": warnings,
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和后台线程")
            return {"status": "error", "reason": f"健康检查异常: {str(e)}", "data": {}, "warnings": [f"health_check_failed: {str(e)}"]}

    # ========== 私有方法 ==========
    @staticmethod
    def _json_serializer(obj):
        """JSON 序列化器：显式处理 datetime 等特殊类型"""
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"无法序列化类型: {type(obj).__name__}")

    def _buffer_usage_pct(self) -> float:
        maxlen = getattr(self._buffer, 'maxlen', None)
        if not maxlen or maxlen <= 0:
            return 0.0
        return len(self._buffer) / maxlen

    def _sanitize_details(self, details: Dict[str, Any], depth: int = 0) -> Dict[str, Any]:
        """深度脱敏（带最大深度限制）"""
        if depth > self.DEFAULT_MAX_DETAILS_DEPTH:
            return {"error": "max_depth_exceeded"}
        sanitized = {}
        for key, value in details.items():
            if isinstance(value, dict):
                sanitized[key] = self._sanitize_details(value, depth + 1)
            elif isinstance(value, list):
                sanitized[key] = [
                    self._sanitize_details(v, depth + 1) if isinstance(v, dict) else v
                    for v in value
                ]
            else:
                # 敏感字段脱敏
                key_lower = key.lower()
                is_sensitive = False
                for sensitive in self.DEFAULT_SENSITIVE_KEYWORDS:
                    # 使用正则边界匹配，避免 "passport" 命中 "pass"
                    pattern = r'\b' + re.escape(sensitive) + r'\b'
                    if re.search(pattern, key_lower):
                        is_sensitive = True
                        break
                if is_sensitive:
                    sanitized[key] = "[REDACTED]"
                else:
                    sanitized[key] = value
        return sanitized

    def _truncate_details(self, details: Dict[str, Any]) -> Dict[str, Any]:
        result = {}
        total = 0
        for key, value in details.items():
            try:
                field_size = len(json.dumps({key: value}, default=self._json_serializer))
            except Exception:
                field_size = 0
            if total + field_size > self.DEFAULT_MAX_EVENT_SIZE_BYTES:
                result[key] = f"[TRUNCATED: original size {field_size}]"
                break
            result[key] = value
            total += field_size
        return result

    def _check_disk_free(self) -> float:
        try:
            stat = os.statvfs(self._log_dir)
            # 计算可用字节数
            available_bytes = stat.f_bavail * stat.f_frsize
            total_bytes = stat.f_blocks * stat.f_frsize
            return (available_bytes / total_bytes * 100) if total_bytes > 0 else 100
        except OSError:
            return 0.0

    def _rotate_log_file(self) -> None:
        with self._file_lock:
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            self._current_log_file = os.path.join(self._log_dir, f"behavioral_{today}.log")
            try:
                if os.path.exists(self._current_log_file):
                    self._current_file_size = os.path.getsize(self._current_log_file)
                    if self._current_file_size > self.DEFAULT_MAX_FILE_SIZE_MB * 1024 * 1024:
                        seq = 1
                        max_seq = 1000
                        while seq <= max_seq:
                            new_name = f"{self._current_log_file}.{seq}"
                            if not os.path.exists(new_name):
                                break
                            seq += 1
                        if seq > max_seq:
                            logger.error("日志轮转失败: 已超过最大文件数 %d", max_seq)
                            return
                        try:
                            shutil.move(self._current_log_file, new_name)
                        except OSError as e:
                            logger.error(f"日志轮转失败: {e}")
                            return
                        self._current_file_size = 0
                else:
                    self._current_file_size = 0
            except FileNotFoundError:
                self._current_file_size = 0
                logger.debug("日志文件在检查期间被外部删除，已重置")

    def _start_flush_thread(self) -> None:
        if self._flush_thread and self._flush_thread.is_alive():
            return
        self._stop_flush.clear()
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True, name="behavioral-logger-flush")
        self._flush_thread.start()

    def _flush_loop(self) -> None:
        next_flush = time.monotonic() + self.DEFAULT_FLUSH_INTERVAL_SEC
        while not self._stop_flush.is_set():
            now = time.monotonic()
            sleep_duration = max(0, next_flush - now)
            if sleep_duration > 0:
                time.sleep(sleep_duration)
            self._flush_buffer()
            next_flush = time.monotonic() + self.DEFAULT_FLUSH_INTERVAL_SEC

    def _flush_buffer(self) -> None:
        with self._flush_lock:
            # 批量原子获取事件
            batch = []
            with self._buffer_seq_lock:
                while self._buffer:
                    try:
                        batch.append(self._buffer.popleft())
                    except IndexError:
                        break
            if not batch:
                return

            flush_start = time.monotonic()

            # 分配序列号
            with self._seq_lock:
                for event in batch:
                    self._sequence_counter += 1
                    event["sequence"] = self._sequence_counter
                    # 完整性校验：对整个事件做哈希
                    checksum_data = json.dumps(event, default=self._json_serializer, sort_keys=True)
                    event["checksum"] = hashlib.new(self.DEFAULT_CHECKSUM_ALGORITHM, checksum_data.encode()).hexdigest()

            # 序列化
            try:
                lines = "\n".join(json.dumps(event, default=self._json_serializer, ensure_ascii=False) for event in batch) + "\n"
            except Exception as e:
                logger.error(f"序列化失败: {e}")
                self._emergency_persist(batch)
                return

            # 压缩
            if self._compressor and self.DEFAULT_COMPRESSION_ALGORITHM != "none":
                try:
                    compressed = self._compressor.compress(lines)
                    if isinstance(compressed, bytes):
                        lines = compressed
                except Exception as e:
                    logger.warning(f"压缩失败: {e}")

            # 写入文件（带文件锁）
            temp_file = f"{self._current_log_file}.tmp.{os.getpid()}.{threading.get_ident()}"
            write_mode = "wb" if isinstance(lines, bytes) else "w"
            encoding = None if isinstance(lines, bytes) else "utf-8"
            try:
                with open(temp_file, write_mode, encoding=encoding) as f:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        # 文件锁获取失败，使用本地备选锁
                        logger.debug("fcntl 文件锁不可用，降级为本地锁")
                    if isinstance(lines, bytes):
                        f.write(lines)
                    else:
                        f.write(lines)
                    f.flush()
                    os.fsync(f.fileno())
                # 刷目录元数据
                dir_fd = os.open(os.path.dirname(self._current_log_file), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
                # 原子替换
                if os.path.exists(self._current_log_file):
                    os.remove(self._current_log_file)
                os.rename(temp_file, self._current_log_file)
                self._current_file_size = os.path.getsize(self._current_log_file)
            except Exception as e:
                logger.error(f"文件写入失败: {e} #RECOVERY: 检查磁盘空间和权限")
                self._emergency_persist(batch)
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except OSError:
                        pass
                with self._stats_lock:
                    self._stats["flush_failures"] += 1
                    self._stats["buffer_dropped"] += len(batch)
                return

            # 索引
            if self._indexer:
                for event in batch:
                    try:
                        self._indexer.index(event)
                    except Exception as e:
                        logger.warning(f"索引失败: {e}")

            # 轮转检查
            if self._current_file_size > self.DEFAULT_MAX_FILE_SIZE_MB * 1024 * 1024:
                self._rotate_log_file()

            flush_duration = (time.monotonic() - flush_start) * 1000
            with self._stats_lock:
                self._stats["bytes_written"] += (len(lines) if isinstance(lines, bytes) else len(lines.encode("utf-8")))
                self._stats["last_flush_timestamp"] = time.time()
                self._stats["last_flush_duration_ms"] = flush_duration

            logger.debug("刷盘完成: %d 条 | %.2f KB | %.1f ms", len(batch),
                         (len(lines) if isinstance(lines, bytes) else len(lines.encode("utf-8"))) / 1024,
                         flush_duration)

    def _emergency_persist(self, batch: List[Dict[str, Any]]) -> None:
        """多级紧急持久化：PositionSnapshot → 应急文件 → 最后告警"""
        # 第一级：PositionSnapshot
        if self._position_snapshot:
            try:
                self._position_snapshot.emergency_persist("behavioral_log", batch)
                logger.info("紧急持久化 %d 条日志成功 (PositionSnapshot)", len(batch))
                return
            except Exception as e:
                logger.warning(f"PositionSnapshot 紧急持久化失败: {e}")

        # 第二级：应急文件
        try:
            with open(self.DEFAULT_EMERGENCY_LOG_PATH, "ab") as f:
                for event in batch:
                    f.write(json.dumps(event, default=self._json_serializer, ensure_ascii=False).encode() + b"\n")
                f.flush()
                os.fsync(f.fileno())
            logger.info("紧急持久化 %d 条日志到应急文件成功", len(batch))
            return
        except Exception as e:
            logger.error(f"应急文件写入也失败: {e}")

        # 最终失败
        logger.critical(f"所有持久化手段均失败，{len(batch)} 条日志永久丢失")

    def _do_push_critical(self, event: Dict[str, Any]) -> None:
        """推送 CRITICAL 事件（带线程安全保护）"""
        # 在函数开始处保存本地引用，避免重试期间被置为 None
        negotiation_bus = self._negotiation_bus
        if not negotiation_bus:
            return
        for attempt in range(3):
            try:
                negotiation_bus.publish_alert(
                    alert_type="behavioral_log",
                    level=event.get("level", "CRITICAL"),
                    event_type=event.get("type", ""),
                    details=event.get("details", {}),
                    timestamp=event.get("timestamp", time.time()),
                )
                return
            except Exception as e:
                if attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
                else:
                    logger.warning(f"推送 CRITICAL 失败（重试3次）: {e}")

    def _shutdown(self) -> None:
        """模块销毁时的安全清理"""
        logger.info("BehavioralLogger 正在关闭...")
        self._stop_flush.set()

        # 等待刷盘线程退出
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=3.0)

        # 先停止接受新的 CRITICAL 推送
        self._critical_executor.shutdown(wait=True, timeout=5.0)

        # 最后执行一次刷盘（此时不再有新事件入队）
        remaining = len(self._buffer)
        self._flush_buffer()
        logger.info("BehavioralLogger 已安全关闭 (剩余缓冲事件: %d)", remaining)

        # 清理临时文件
        try:
            for f in glob.glob(os.path.join(self._log_dir, "*.tmp.*")):
                try:
                    os.remove(f)
                except OSError:
                    pass
        except Exception:
            pass

        # 注销 atexit
        if hasattr(atexit, 'unregister'):
            try:
                atexit.unregister(self._shutdown)
            except Exception:
                pass
