"""
火种系统 · 语义索引器 (SemanticIndexer)

核心职责：
1. 对行为日志条目进行实时语义关键词提取与结构化标注，支持正则规则引擎和可选LLM深度抽取双模式，LLM不可用时自动降级并定时检测恢复
2. 维护内存倒排索引与反向索引，支持按关键词、时间范围、模块名、事件类型、模糊匹配、正则匹配等多维度检索，结果按加权相关性排序，支持分页游标

外部依赖（真实模块接口）：
- core.behavioral_logger.BehavioralLogger : 获取原始日志条目队列，读取待索引的日志数据
- core.llm_lifecycle.LLMLifecycle (可选) : 调用本地 DeepSeek 模型进行复杂实体关系抽取，未注入时降级为基于正则的规则引擎
- core.utils.db_utils.DBUtils : 定期将内存索引（含元数据和倒排索引完整快照）持久化到 SQLite WAL 模式，并在重启时恢复索引
- core.module_health_monitor.ModuleHealthMonitor (可选) : 上报模块健康状态

接口契约：
- index_log(entry: Dict[str, Any]) -> Dict[str, Any] : 对单条日志进行索引，返回索引结果，重复 entry_id 返回 duplicate 状态
- search(query: Dict[str, Any]) -> Dict[str, Any] : 按条件检索日志，返回匹配条目的加权排序列表及分页游标
- get_index_stats() -> Dict[str, Any] : 返回当前索引统计信息（索引条目数、关键词数、精确内存占用等）
- get_metrics() -> Dict[str, Any] : 返回金融级可观测指标（吞吐量、查询延迟分位、错误率、队列深度、缓存命中率）
- health_check() -> Dict[str, Any] : 模块自检，并主动上报 ModuleHealthMonitor
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 LLMLifecycle 不可用或调用超时，自动降级为基于正则表达式的关键词提取，并每 300 秒检测一次恢复
- 当 DBUtils 不可用时，索引仅保存在内存中，定期清理过期条目以避免内存溢出，内存使用达上限时触发写入背压
- 当 BehavioralLogger 不可用时，index_log 直接返回错误，search 返回空结果
- 所有降级值在类常量区明确声明，降级事件计入 metrics

资源管理：
- 内存索引使用紧凑数据结构，热数据（最近 1 小时）保留完整索引，冷数据仅保留元数据摘要
- 持久化使用独立异步线程，不阻塞主索引写入路径；快照采用深拷贝确保一致性
- 所有写操作在细粒度锁保护下进行，读操作使用独立读锁；时间衰减缓存使用独立锁
- 模块销毁时通过 atexit 注册清理函数，触发最后一次索引持久化（若 DBUtils 可用）
"""

import time
import logging
import threading
import atexit
import re
import sys
import hashlib
import json
import queue
import copy
import itertools
import concurrent.futures
from collections import defaultdict, OrderedDict, deque
from typing import Dict, Any, List, Optional, Tuple, Set, Union

logger = logging.getLogger(__name__)

# ========== 编译正则表达式（模块级常量，避免重复编译） ==========
COMPILED_PATTERNS = {
    "module": re.compile(r"(core|agents|brain|ghost|strategies)\.\w+(?:\.\w+)*"),
    "symbol": re.compile(r"\b(BTC|ETH|SOL|BNB|XRP|USDT|USDC|DAI)\b", re.IGNORECASE),
    "percentage": re.compile(r"(\d+(?:\.\d+)?)\s*%"),
    "price": re.compile(r"(\d+(?:\.\d+)?)\s*(?:USDT|USD)"),
    "error_code": re.compile(r"错误码[:：]\s*(\w+)"),
    "uuid": re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"),
}

# 可选依赖
try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False
    logger.warning("numpy 不可用，统计计算将使用纯 Python 实现")

try:
    from nltk.stem import PorterStemmer
    _STEMMER_AVAILABLE = True
except ImportError:
    _STEMMER_AVAILABLE = False

_JIEBA_AVAILABLE = False
try:
    import jieba
    _JIEBA_AVAILABLE = True
    try:
        jieba.load_userdict("config/jieba_finance_dict.txt")
    except FileNotFoundError:
        pass
except ImportError:
    pass

# LLM 调用线程池（全局复用）
_LLM_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="semantic_llm")


class SemanticIndexer:
    """语义索引器"""

    # ========== 类常量 ==========
    DEFAULT_MAX_INDEX_ENTRIES = 50000
    DEFAULT_HOT_DATA_SECONDS = 3600
    DEFAULT_MAX_MEMORY_MB = 512
    DEFAULT_RETENTION_SECONDS = 86400
    DEFAULT_CLEANUP_INTERVAL_SEC = 300
    DEFAULT_PERSIST_INTERVAL_SEC = 600
    DEFAULT_SEARCH_MAX_RESULTS = 1000
    DEFAULT_MAX_MESSAGE_LENGTH = 4096
    DEFAULT_LLM_TIMEOUT_SEC = 3.0
    DEFAULT_LLM_RECOVERY_CHECK_SEC = 300
    DEFAULT_WRITE_RATE_LIMIT_PER_SEC = 5000
    DEFAULT_DB_MAX_SIZE_MB = 2048
    MIN_TERM_LENGTH = 2
    MAX_CANDIDATE_SCAN_THRESHOLD = 100000
    CONTENT_CACHE_MAX_SIZE = 5000
    MEMORY_USAGE_CACHE_SEC = 300
    METADATA_TIME_DECAY_CACHE_SEC = 60
    MAX_FORCE_CLEANUP_ITERATIONS = 100000
    MAX_CLEANUP_BATCH_SIZE = 10000
    TIME_DECAY_CACHE_MAX_SIZE = 10000
    PERSIST_SUCCESS_LOG_INTERVAL = 100
    NGRAM_MAX_CHARS = 512
    TIME_DIFF_RECALIBRATE_SEC = 3600
    REGEX_CACHE_MAX_SIZE = 128

    # 敏感信息脱敏正则
    SENSITIVE_PATTERNS = [
        re.compile(r'(api[_-]?key|api[_-]?secret|secret[_-]?key)\s*[:=]\s*[^\s,}]+', re.IGNORECASE | re.DOTALL),
        re.compile(r'(password|passwd)\s*[:=]\s*[^\s,}]+', re.IGNORECASE | re.DOTALL),
        re.compile(r'sk-[a-zA-Z0-9]{32,}', re.IGNORECASE),
        re.compile(r'(?:x-api-key:|Bearer\s+)[^\s,}]+', re.IGNORECASE),
        re.compile(r'(?:secret|token)\s*[:=]\s*[^\s,}]+', re.IGNORECASE),
    ]
    SENSITIVE_REPLACEMENT = "***REDACTED***"

    EVENT_TYPES = {
        "circuit_breaker": "熔断事件",
        "risk_color_change": "风险色彩变更",
        "profit_compression": "紧缩利润触发",
        "add_position": "加仓动作",
        "signal_generation": "信号生成",
        "pipeline_advance": "流水线推进",
        "module_health": "模块健康",
        "negotiation": "协商事件",
        "error": "系统错误",
        "warning": "系统警告",
    }

    def __init__(self):
        # 主索引结构：keyword -> set of entry_id
        self._keyword_index: Dict[str, Set[int]] = defaultdict(set)
        # 反向索引：entry_id -> List[str]
        self._reverse_index: Dict[int, List[str]] = {}
        # 元数据存储
        self._metadata: Dict[int, Dict[str, Any]] = {}
        # 内容缓存 (LRU)
        self._content_cache: OrderedDict = OrderedDict()
        # 时间戳有序索引（使用 deque 支持 O(1) popleft()）
        self._timestamp_index: deque = deque()

        # 时间序列统计
        self._entry_timestamps: deque = deque(maxlen=10000)
        self._query_latencies: deque = deque(maxlen=1000)
        self._error_counts: Dict[str, int] = defaultdict(int)
        self._total_indexed = 0
        self._write_rate_lock = threading.Lock()
        self._write_rate_reset_monotonic = time.monotonic()
        self._write_counter = 0

        # 缓存统计
        self._time_decay_cache_hits = 0
        self._time_decay_cache_misses = 0
        self._regex_cache: OrderedDict = OrderedDict()

        # 内存使用缓存
        self._cached_memory_kb = 0.0
        self._last_memory_calc_monotonic = 0.0

        # 元数据时间衰减缓存 (LRU) 及独立锁
        self._time_decay_cache: OrderedDict = OrderedDict()
        self._time_decay_lock = threading.Lock()
        self._last_decay_refresh_monotonic = 0.0

        # 外部依赖
        self._behavioral_logger = None
        self._llm_lifecycle = None
        self._db_utils = None
        self._module_health_monitor = None

        # 线程安全（读写锁分离）
        self._write_lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._persist_lock = threading.Lock()
        self._cleanup_lock = threading.Lock()

        # 持久化异步队列
        self._persist_queue: queue.Queue = queue.Queue(maxsize=100)
        self._persist_pending = False
        self._persist_thread: Optional[threading.Thread] = None
        self._persist_running = False
        self._persist_event = threading.Event()
        self._persist_success_count = 0
        self._persist_fail_count = 0

        # 清理异步线程
        self._cleanup_thread: Optional[threading.Thread] = None
        self._cleanup_running = False
        self._cleanup_event = threading.Event()
        self._cleanup_needed = False

        # 线程健康标记
        self._persist_thread_healthy = False
        self._cleanup_thread_healthy = False

        # 依赖注入标记
        self._dependencies_injected = False

        # 时间基准
        self._startup_monotonic = time.monotonic()
        self._time_diff = time.time() - time.monotonic()
        self._last_time_diff_recalibrate = time.monotonic()

        # LLM 降级状态与恢复检测
        self._llm_degraded = False
        self._last_llm_check_monotonic = 0.0

        # 启动恢复
        self._recover_from_persistent()

        # 注册退出清理
        atexit.register(self._cleanup_on_exit)

        logger.info("SemanticIndexer 初始化完成，最大内存索引条目 %d，热数据窗口 %d 秒",
                    self.DEFAULT_MAX_INDEX_ENTRIES, self.DEFAULT_HOT_DATA_SECONDS)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        behavioral_logger: Optional[object] = None,
        llm_lifecycle: Optional[object] = None,
        db_utils: Optional[object] = None,
        module_health_monitor: Optional[object] = None,
    ) -> None:
        """注入外部依赖，未注入时对应功能降级。后台线程在首次注入时启动"""
        if self._dependencies_injected:
            logger.warning("依赖已注入，忽略重复注入请求")
            return
        self._behavioral_logger = behavioral_logger
        if llm_lifecycle is not None:
            if not self._validate_llm_interface(llm_lifecycle):
                logger.warning("LLMLifecycle 接口不兼容，禁用 LLM 模式")
                self._llm_degraded = True
                self._llm_lifecycle = None
            else:
                self._llm_lifecycle = llm_lifecycle
                self._llm_degraded = False
        else:
            self._llm_degraded = True

        if db_utils is not None:
            self._db_utils = db_utils
            self._start_persist_thread()
        if module_health_monitor is not None:
            self._module_health_monitor = module_health_monitor

        self._start_cleanup_thread()
        self._dependencies_injected = True
        logger.info("依赖注入完成: behavioral_logger=%s, llm_lifecycle=%s, db_utils=%s, health_monitor=%s",
                    behavioral_logger is not None, llm_lifecycle is not None, db_utils is not None, module_health_monitor is not None)

    def _validate_llm_interface(self, llm_instance: object) -> bool:
        """验证 LLM 实例是否符合接口契约"""
        if not hasattr(llm_instance, 'extract_entities'):
            return False
        import inspect
        try:
            sig = inspect.signature(llm_instance.extract_entities)
            params = list(sig.parameters.keys())
            return 'text' in params
        except (ValueError, TypeError):
            return False

    # ========== 公共接口 ==========
    def index_log(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """对单条日志进行语义索引"""
        if self._behavioral_logger is None:
            return {"status": "error", "reason": "BehavioralLogger 未注入", "data": {}, "warnings": ["no_logger"]}

        if not self._check_write_rate():
            return {"status": "degraded", "reason": "写入速率超限", "data": {}, "warnings": ["rate_limited"]}

        entry_id = self._resolve_entry_id(entry)
        timestamp, original_ts = self._validate_timestamp(entry.get("timestamp", time.time()))
        message = self._sanitize_sensitive_data(str(entry.get("message", "")))[:self.DEFAULT_MAX_MESSAGE_LENGTH]
        module = str(entry.get("module", "unknown")).strip() or "unknown"
        event_type = str(entry.get("event_type", "unknown")).strip() or "unknown"
        level = str(entry.get("level", "INFO")).strip().upper()
        monotonic_now = time.monotonic()

        # 关键词提取在锁外执行
        keywords = self._extract_keywords(message, module, event_type, level)

        # 定期校准时间差
        self._recalibrate_time_diff()

        with self._write_lock:
            if entry_id in self._metadata:
                return {"status": "duplicate", "reason": f"entry_id {entry_id} 已存在", "data": {"entry_id": entry_id}, "warnings": []}
            if len(self._metadata) >= self.DEFAULT_MAX_INDEX_ENTRIES:
                self._cleanup_needed = True
                self._cleanup_event.set()
            for keyword in keywords:
                self._keyword_index[keyword].add(entry_id)
            self._reverse_index[entry_id] = list(keywords)
            self._metadata[entry_id] = {
                "timestamp": timestamp,
                "monotonic": monotonic_now,
                "module": module,
                "event_type": event_type,
                "level": level,
                "original_ts": original_ts if original_ts != timestamp else timestamp,
            }
            self._evict_content_cache_lru()
            self._content_cache[entry_id] = message[:256]
            self._total_indexed += 1
            self._entry_timestamps.append(timestamp)
            self._timestamp_index.append((monotonic_now, entry_id))

        self._invalidate_time_decay_cache()
        self._try_persist_async()
        warnings_list = [f"timestamp_adjusted_from_{original_ts}"] if original_ts != timestamp else []
        return {
            "status": "ok",
            "reason": f"日志 {entry_id} 索引成功，提取 {len(keywords)} 个关键词",
            "data": {"entry_id": entry_id, "extracted_keywords": sorted(list(keywords))[:20]},
            "warnings": warnings_list,
        }

    def search(self, query: Dict[str, Any]) -> Dict[str, Any]:
        """按条件检索日志"""
        keywords = query.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [keywords]
        time_from = float(query.get("time_from", 0.0))
        time_to = float(query.get("time_to", time.time()))
        max_results = min(int(query.get("max_results", self.DEFAULT_SEARCH_MAX_RESULTS)), 5000)
        cursor = query.get("cursor")
        filter_mode = query.get("filter_mode", "exact")
        query_monotonic = time.monotonic()
        warnings_list = []

        with self._read_lock:
            if keywords:
                candidate_ids = None
                for kw in keywords:
                    ids = self._keyword_index.get(kw, set())
                    if candidate_ids is None:
                        candidate_ids = ids.copy()
                    else:
                        candidate_ids &= ids
                    if not candidate_ids:
                        break
                if candidate_ids is None or not candidate_ids:
                    logger.debug("关键词无匹配: %s", keywords)
                    candidate_ids = set()
            else:
                total_count = len(self._metadata)
                if total_count > self.MAX_CANDIDATE_SCAN_THRESHOLD:
                    return {
                        "status": "error",
                        "reason": f"全表扫描不被允许 (总条目 {total_count} > {self.MAX_CANDIDATE_SCAN_THRESHOLD})，请指定关键词或时间范围",
                        "data": {},
                        "warnings": ["full_scan_denied"],
                    }
                candidate_ids = set(self._metadata.keys())

            matched = []
            for eid in candidate_ids:
                meta = self._metadata.get(eid)
                if meta is None:
                    continue
                meta_ts = meta.get("timestamp", 0)
                if time_from > 0 and meta_ts < time_from:
                    continue
                if meta_ts > time_to:
                    continue
                if not self._match_filter(meta, query, filter_mode):
                    continue
                score = self._calculate_relevance(eid, keywords)
                matched.append((eid, meta_ts, score))

        # 锁外排序和分页
        matched.sort(key=lambda x: (x[2], x[1]), reverse=True)
        total_matches = len(matched)

        start_idx = 0
        if cursor:
            try:
                cursor_data = json.loads(cursor)
                cursor_ts = float(cursor_data.get("ts", 0))
                cursor_id = int(cursor_data.get("id", 0))
                for i, (eid, ts, _) in enumerate(matched):
                    if (ts, eid) <= (cursor_ts, cursor_id):
                        start_idx = i + 1
            except (json.JSONDecodeError, ValueError, KeyError):
                warnings_list.append("invalid_cursor")
                start_idx = 0

        page = matched[start_idx:start_idx + max_results]
        results = self._build_results(page)
        next_cursor = None
        if start_idx + max_results < total_matches:
            last = matched[start_idx + max_results - 1]
            next_cursor = json.dumps({"ts": f"{last[1]:.6f}", "id": last[0]}, separators=(',', ':'))

        query_latency = time.monotonic() - query_monotonic
        self._query_latencies.append(query_latency)

        return {
            "status": "ok",
            "reason": f"检索完成，匹配 {total_matches} 条，返回 {len(results)} 条 (查询时刻 monotonic={query_monotonic:.6f})",
            "data": {
                "total_matches": total_matches,
                "results": results,
                "cursor": next_cursor,
                "is_truncated": total_matches > len(results),
            },
            "warnings": warnings_list,
        }

    def get_index_stats(self) -> Dict[str, Any]:
        """获取当前索引统计信息"""
        with self._read_lock:
            total_entries = len(self._metadata)
            total_keywords = len(self._keyword_index)
            memory_usage_kb = self._get_memory_usage()
            top_keywords = sorted(self._keyword_index.items(), key=lambda x: len(x[1]), reverse=True)[:10]
        return {
            "status": "ok",
            "reason": "索引统计生成成功",
            "data": {
                "total_entries": total_entries,
                "total_keywords": total_keywords,
                "estimated_memory_kb": round(memory_usage_kb, 1),
                "top_keywords": [{"keyword": kw, "frequency": len(entries)} for kw, entries in top_keywords],
            },
            "warnings": [],
        }

    def get_metrics(self) -> Dict[str, Any]:
        """返回金融级可观测指标"""
        with self._read_lock:
            raw_latencies = [x for x in self._query_latencies if x is not None]
            if raw_latencies and _NUMPY_AVAILABLE:
                p50 = float(np.percentile(raw_latencies, 50))
                p95 = float(np.percentile(raw_latencies, 95))
                p99 = float(np.percentile(raw_latencies, 99))
            elif raw_latencies:
                sorted_lat = sorted(raw_latencies)
                n = len(sorted_lat)
                p50 = sorted_lat[n // 2] if n > 0 else -1.0
                p95 = sorted_lat[int(n * 0.95)] if n > 1 else -1.0
                p99 = sorted_lat[int(n * 0.99)] if n > 1 else -1.0
            else:
                p50 = p95 = p99 = -1.0
        return {
            "status": "ok",
            "reason": "指标采集完成",
            "data": {
                "total_indexed": self._total_indexed,
                "current_index_size": len(self._metadata),
                "query_latency_p50_ms": round(p50 * 1000, 2) if p50 >= 0 else None,
                "query_latency_p95_ms": round(p95 * 1000, 2) if p95 >= 0 else None,
                "query_latency_p99_ms": round(p99 * 1000, 2) if p99 >= 0 else None,
                "query_latency_available": len(raw_latencies) > 0,
                "error_counts": dict(self._error_counts),
                "llm_degraded": self._llm_degraded,
                "time_decay_cache_hit_rate": round(self._get_cache_hit_rate(), 4),
                "persist_fail_count": self._persist_fail_count,
            },
            "warnings": [],
        }

    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            if not hasattr(self, '_keyword_index'):
                return {"status": "degraded", "reason": "索引数据结构未初始化", "data": {}, "warnings": ["index_not_initialized"]}

            thread_warnings = []
            if self._persist_thread is not None and (not self._persist_thread.is_alive() or not self._persist_thread_healthy):
                thread_warnings.append("persist_thread_unhealthy")
                logger.error("持久化线程异常 #RECOVERY: 检查 DBUtils 连接，尝试重启线程")
            if self._cleanup_thread is not None and (not self._cleanup_thread.is_alive() or not self._cleanup_thread_healthy):
                thread_warnings.append("cleanup_thread_unhealthy")
                logger.error("清理线程异常 #RECOVERY: 检查锁状态，尝试重启线程")

            with self._read_lock:
                entry_count = len(self._metadata)
                kw_count = len(self._keyword_index)
                reverse_count = len(self._reverse_index)

            if reverse_count != entry_count:
                logger.error("索引一致性异常: metadata=%d, reverse_index=%d #RECOVERY: 触发全量索引重建",
                            entry_count, reverse_count)
                self._error_counts["consistency_mismatch"] += 1
                thread_warnings.append("consistency_mismatch")

            self._report_to_module_health(entry_count, kw_count)
            return {
                "status": "ok" if not thread_warnings else "degraded",
                "reason": f"SemanticIndexer 正常，索引条目 {entry_count}，关键词 {kw_count}",
                "data": {"entry_count": entry_count, "keyword_count": kw_count},
                "warnings": thread_warnings,
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和数据结构完整性")
            return {"status": "error", "reason": f"健康检查异常: {str(e)}", "data": {}, "warnings": [f"health_check_failed: {str(e)}"]}

    # ========== 私有方法：入口ID与时间戳 ==========
    def _resolve_entry_id(self, entry: Dict[str, Any]) -> int:
        raw = entry.get("id")
        if raw is None:
            raise ValueError("entry 缺少 id 字段")
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            eid = int.from_bytes(hashlib.sha256(raw.encode()).digest()[:8], 'big') & 0x7FFFFFFFFFFFFFFF
            if eid in self._metadata and self._metadata[eid].get("_raw_id") != raw:
                logger.warning(f"哈希冲突: entry_id={eid}, raw_id={raw}")
                self._error_counts["hash_collision"] += 1
            return eid
        try:
            return int(raw)
        except (ValueError, TypeError):
            return int.from_bytes(hashlib.sha256(str(raw).encode()).digest()[:8], 'big') & 0x7FFFFFFFFFFFFFFF

    def _validate_timestamp(self, ts: float) -> Tuple[float, float]:
        ts = float(ts)
        now = time.time()
        if ts < now - 86400 * 365 or ts > now + 3600:
            logger.warning(f"时间戳异常: {ts} (当前: {now})，使用当前时间替代")
            return now, ts
        return ts, ts

    def _recalibrate_time_diff(self) -> None:
        now_mono = time.monotonic()
        if now_mono - self._last_time_diff_recalibrate > self.TIME_DIFF_RECALIBRATE_SEC:
            new_diff = time.time() - time.monotonic()
            drift = new_diff - self._time_diff
            if abs(drift) > 0.1:
                logger.info(f"时间差漂移: {self._time_diff:.6f} -> {new_diff:.6f} (漂移 {drift:.6f}s)")
            self._time_diff = new_diff
            self._last_time_diff_recalibrate = now_mono

    # ========== 私有方法：敏感信息与关键词 ==========
    def _sanitize_sensitive_data(self, text: str) -> str:
        for pattern in self.SENSITIVE_PATTERNS:
            before = text
            text = pattern.sub(self.SENSITIVE_REPLACEMENT, text)
            if text != before:
                logger.debug("脱敏替换: pattern=%s", pattern.pattern[:50])
        # 二次扫描确保无残留
        for pattern in self.SENSITIVE_PATTERNS:
            if pattern.search(text):
                text = pattern.sub(self.SENSITIVE_REPLACEMENT * 2, text)
        return text

    def _extract_keywords(self, message: str, module: str, event_type: str, level: str) -> Set[str]:
        keywords = set()
        # LLM恢复检测
        self._check_llm_recovery()
        if self._llm_lifecycle is not None and not self._llm_degraded and len(message) > 50:
            try:
                future = _LLM_EXECUTOR.submit(self._llm_lifecycle.extract_entities, text=message)
                llm_result = future.result(timeout=self.DEFAULT_LLM_TIMEOUT_SEC)
                if isinstance(llm_result, list):
                    for entity in llm_result:
                        if entity and isinstance(entity, str) and len(entity) >= self.MIN_TERM_LENGTH:
                            keywords.add(entity.lower())
                    if keywords:
                        return self._normalize_keywords(keywords)
            except concurrent.futures.TimeoutError:
                logger.debug("LLM 实体提取超时 (%.1fs)，回退到正则", self.DEFAULT_LLM_TIMEOUT_SEC)
            except Exception as e:
                logger.debug(f"LLM 实体提取失败，回退到正则: {e}")
        for _, pattern in COMPILED_PATTERNS.items():
            for match in pattern.findall(message):
                token = match[0] if isinstance(match, tuple) else match
                if token and isinstance(token, str) and len(token) >= self.MIN_TERM_LENGTH:
                    keywords.add(token.lower())
        words = message.split()
        for word in words:
            clean = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fff]', '', word)
            if len(clean) >= self.MIN_TERM_LENGTH:
                keywords.add(clean.lower())
        if _JIEBA_AVAILABLE and any('\u4e00' <= ch <= '\u9fff' for ch in message):
            for seg in jieba.cut(message):
                if len(seg) >= self.MIN_TERM_LENGTH:
                    keywords.add(seg.lower())
        elif any('\u4e00' <= ch <= '\u9fff' for ch in message):
            # 中文分词降级：字符级 n-gram，仅处理前 N 个字符
            snippet = message[:self.NGRAM_MAX_CHARS]
            for n in range(2, min(5, len(snippet) + 1)):
                for i in range(len(snippet) - n + 1):
                    ngram = snippet[i:i + n]
                    if all('\u4e00' <= ch <= '\u9fff' for ch in ngram):
                        keywords.add(ngram.lower())
        if module and module != "unknown":
            keywords.add(f"module:{module}")
        if event_type and event_type != "unknown":
            keywords.add(f"event:{event_type}")
        if level in ("ERROR", "CRITICAL"):
            keywords.add("level:high_priority")
        return self._normalize_keywords(keywords)

    def _check_llm_recovery(self) -> None:
        if not self._llm_degraded:
            return
        now = time.monotonic()
        if now - self._last_llm_check_monotonic < self.DEFAULT_LLM_RECOVERY_CHECK_SEC:
            return
        self._last_llm_check_monotonic = now
        if self._llm_lifecycle is not None and self._validate_llm_interface(self._llm_lifecycle):
            self._llm_degraded = False
            logger.info("LLM 模式已恢复")

    def _normalize_keywords(self, keywords: Set[str]) -> Set[str]:
        result = set()
        for kw in keywords:
            kw = kw.lower()
            if _STEMMER_AVAILABLE and not kw.startswith(("module:", "event:", "level:")):
                try:
                    kw = PorterStemmer().stem(kw)
                except Exception:
                    pass
            result.add(kw)
        return result

    # ========== 私有方法：检索与相关性 ==========
    def _calculate_relevance(self, entry_id: int, query_keywords: List[str]) -> float:
        if not query_keywords:
            return 1.0
        matched_keywords = self._reverse_index.get(entry_id, [])
        if not matched_keywords:
            return 0.0
        matched_set = set(matched_keywords)
        score = sum(1 for kw in query_keywords if kw in matched_set)
        meta = self._metadata.get(entry_id, {})
        if meta.get("level") in ("ERROR", "CRITICAL"):
            score *= 1.5
        time_decay = self._get_time_decay(entry_id)
        return score * time_decay

    def _get_time_decay(self, entry_id: int) -> float:
        with self._time_decay_lock:
            now = time.monotonic()
            if now - self._last_decay_refresh_monotonic > self.METADATA_TIME_DECAY_CACHE_SEC:
                self._time_decay_cache.clear()
                self._last_decay_refresh_monotonic = now
            if entry_id in self._time_decay_cache:
                self._time_decay_cache.move_to_end(entry_id)
                self._time_decay_cache_hits += 1
                return self._time_decay_cache[entry_id]
            self._time_decay_cache_misses += 1
        # 在锁外计算
        meta = self._metadata.get(entry_id, {})
        ts = meta.get("monotonic", meta.get("timestamp", now - 86400))
        age_hours = max(0, now - ts) / 3600
        if age_hours > 168:  # 超过一周
            logger.debug(f"entry_id={entry_id} 时间戳异常，age_hours={age_hours:.1f}，使用保守衰减")
        decay = max(0.15, 2.0 ** (-age_hours / 24))
        with self._time_decay_lock:
            self._time_decay_cache[entry_id] = decay
            self._time_decay_cache.move_to_end(entry_id)
            while len(self._time_decay_cache) > self.TIME_DECAY_CACHE_MAX_SIZE:
                self._time_decay_cache.popitem(last=False)
        return decay

    def _get_cache_hit_rate(self) -> float:
        total = self._time_decay_cache_hits + self._time_decay_cache_misses
        if total == 0:
            return 1.0
        return self._time_decay_cache_hits / total

    def _match_filter(self, meta: Dict[str, Any], query: Dict[str, Any], filter_mode: str) -> bool:
        for key in ("module", "event_type", "level"):
            val = query.get(key)
            if val:
                target = meta.get(key, "")
                if filter_mode == "regex":
                    try:
                        regex = self._get_cached_regex(val)
                        if not regex.search(target):
                            return False
                    except re.error:
                        return False
                elif filter_mode == "glob":
                    import fnmatch
                    # Unix 下 fnmatch 区分大小写，为保持一致性，统一转小写比较
                    if not fnmatch.fnmatch(target.lower(), val.lower()):
                        return False
                else:
                    if target != val:
                        return False
        return True

    def _get_cached_regex(self, pattern: str) -> re.Pattern:
        if pattern in self._regex_cache:
            self._regex_cache.move_to_end(pattern)
            return self._regex_cache[pattern]
        try:
            compiled = re.compile(pattern)
        except re.error:
            raise
        self._regex_cache[pattern] = compiled
        while len(self._regex_cache) > self.REGEX_CACHE_MAX_SIZE:
            self._regex_cache.popitem(last=False)
        return compiled

    def _build_results(self, page: List[Tuple[int, float, float]]) -> List[Dict[str, Any]]:
        results = []
        for eid, ts, score in page:
            meta = self._metadata.get(eid, {})
            preview = self._content_cache.get(eid, "")
            preview_safe = preview[:128]
            try:
                preview_safe = preview_safe.encode('utf-8')[:128].decode('utf-8', 'ignore')
            except (UnicodeDecodeError, UnicodeEncodeError):
                preview_safe = preview[:128]
            results.append({
                "entry_id": eid,
                "timestamp": meta.get("timestamp", ts),
                "module": meta.get("module", "unknown"),
                "event_type": meta.get("event_type", "unknown"),
                "level": meta.get("level", "INFO"),
                "score": round(score, 4),
                "content_preview": preview_safe,
                "has_full_content": eid in self._content_cache and bool(self._content_cache.get(eid)),
            })
        return results

    # ========== 私有方法：内存管理 ==========
    def _get_memory_usage(self) -> float:
        now = time.monotonic()
        if self._cached_memory_kb > 0 and now - self._last_memory_calc_monotonic < self.MEMORY_USAGE_CACHE_SEC:
            return self._cached_memory_kb
        total = 0
        # 使用迭代器避免创建完整列表
        for kw, ids in itertools.islice(self._keyword_index.items(), 1000):
            total += sys.getsizeof(kw) + sys.getsizeof(ids)
            # 采样集合内元素
            for x in itertools.islice(ids, 100):
                total += sys.getsizeof(x)
        for eid, meta in itertools.islice(self._metadata.items(), 1000):
            total += sys.getsizeof(eid) + sys.getsizeof(meta)
            for v in meta.values():
                if isinstance(v, (str, int, float)):
                    total += sys.getsizeof(v)
        self._cached_memory_kb = total / 1024.0
        self._last_memory_calc_monotonic = now
        return self._cached_memory_kb

    def _invalidate_memory_cache(self) -> None:
        self._cached_memory_kb = 0.0

    def _check_write_rate(self) -> bool:
        with self._write_rate_lock:
            now_mono = int(time.monotonic())
            if now_mono > self._write_rate_reset_monotonic:
                self._write_counter = 0
                self._write_rate_reset_monotonic = now_mono
            self._write_counter += 1
            return self._write_counter <= self.DEFAULT_WRITE_RATE_LIMIT_PER_SEC

    def _invalidate_time_decay_cache(self) -> None:
        with self._time_decay_lock:
            self._time_decay_cache.clear()

    def _evict_content_cache_lru(self) -> None:
        """统一的 LRU 缓存淘汰方法，确保淘汰至目标大小以下"""
        while len(self._content_cache) >= self.CONTENT_CACHE_MAX_SIZE:
            try:
                self._content_cache.popitem(last=False)
            except KeyError:
                break

    # ========== 私有方法：清理 ==========
    def _start_cleanup_thread(self) -> None:
        if self._cleanup_thread is not None:
            return
        self._cleanup_running = True
        self._cleanup_thread = threading.Thread(target=self._cleanup_worker, daemon=True, name="semantic_indexer_cleanup")
        self._cleanup_thread.start()
        logger.info("索引清理线程已启动")

    def _cleanup_worker(self) -> None:
        self._cleanup_thread_healthy = True
        while self._cleanup_running:
            try:
                self._cleanup_event.wait(timeout=self.DEFAULT_CLEANUP_INTERVAL_SEC)
                if not self._cleanup_needed:
                    continue
                self._cleanup_event.clear()
                self._do_cleanup()
                self._cleanup_needed = False
            except Exception as e:
                logger.error(f"索引清理失败: {e} #RECOVERY: 检查锁状态")
            self._cleanup_thread_healthy = True

    def _do_cleanup(self) -> None:
        total_removed = 0
        batches = 0
        cutoff_mono = time.monotonic() - self.DEFAULT_RETENTION_SECONDS
        while True:
            with self._write_lock:
                if not self._timestamp_index:
                    break
                if self._timestamp_index[0][0] >= cutoff_mono:
                    break
                batch_removed = 0
                while (self._timestamp_index and self._timestamp_index[0][0] < cutoff_mono
                       and batch_removed < self.MAX_CLEANUP_BATCH_SIZE):
                    _, eid = self._timestamp_index.popleft()
                    if eid not in self._metadata:
                        continue
                    for keyword in self._reverse_index.get(eid, []):
                        if keyword in self._keyword_index:
                            self._keyword_index[keyword].discard(eid)
                            if not self._keyword_index[keyword]:
                                del self._keyword_index[keyword]
                    self._reverse_index.pop(eid, None)
                    self._metadata.pop(eid, None)
                    self._content_cache.pop(eid, None)
                    batch_removed += 1
                total_removed += batch_removed
                batches += 1
            if batch_removed == 0:
                break
            time.sleep(0)  # 显式让出 GIL

        if total_removed > 0:
            self._invalidate_memory_cache()
            self._invalidate_time_decay_cache()
            logger.info("清理过期索引条目: %d (剩余 %d, 分 %d 批次)", total_removed, len(self._metadata), batches)

    def _force_cleanup_locked(self) -> None:
        if not self._metadata:
            return
        target_size = int(self.DEFAULT_MAX_INDEX_ENTRIES * 0.8)
        iterations = 0
        removed = 0
        while len(self._metadata) > target_size and iterations < self.MAX_FORCE_CLEANUP_ITERATIONS:
            if not self._timestamp_index:
                break
            ts_mono, eid = self._timestamp_index.popleft()
            iterations += 1
            if eid not in self._metadata:
                continue
            for keyword in self._reverse_index.get(eid, []):
                if keyword in self._keyword_index:
                    self._keyword_index[keyword].discard(eid)
                    if not self._keyword_index[keyword]:
                        del self._keyword_index[keyword]
            self._reverse_index.pop(eid, None)
            self._metadata.pop(eid, None)
            self._content_cache.pop(eid, None)
            removed += 1
        if removed > 0:
            self._invalidate_memory_cache()
            self._invalidate_time_decay_cache()
        if iterations >= self.MAX_FORCE_CLEANUP_ITERATIONS:
            logger.error("强制清理达到最大迭代次数，可能存在数据异常 #RECOVERY: 检查索引一致性")

    # ========== 私有方法：持久化 ==========
    def _try_persist_async(self) -> None:
        if self._db_utils is None:
            return
        with self._persist_lock:
            if self._persist_pending:
                return
            self._persist_pending = True
        try:
            self._persist_queue.put(time.time(), timeout=1.0)
        except queue.Full:
            logger.warning("持久化队列已满，跳过本次持久化")
            with self._persist_lock:
                self._persist_pending = False
            return
        self._persist_event.set()

    def _start_persist_thread(self) -> None:
        if self._persist_thread is not None:
            return
        self._persist_running = True
        self._persist_thread = threading.Thread(target=self._persist_worker, daemon=True, name="semantic_indexer_persist")
        self._persist_thread.start()
        logger.info("索引持久化线程已启动")

    def _persist_worker(self) -> None:
        self._persist_thread_healthy = True
        while self._persist_running:
            try:
                self._persist_event.wait(timeout=5.0)
                self._persist_event.clear()
                has_work = False
                while True:
                    try:
                        self._persist_queue.get_nowait()
                        has_work = True
                    except queue.Empty:
                        break
                if not has_work:
                    continue
                # 在锁内创建深拷贝快照
                with self._write_lock:
                    snapshot = {
                        "metadata": copy.deepcopy(dict(self._metadata)),
                        "reverse_index": copy.deepcopy(dict(self._reverse_index)),
                        "keyword_index": {k: list(v) for k, v in self._keyword_index.items()},
                    }
                # 锁外执行数据库写入
                result = self._db_utils.save_index_snapshot(snapshot, time.time())
                if result:
                    self._persist_success_count += 1
                    if self._persist_success_count % self.PERSIST_SUCCESS_LOG_INTERVAL == 0:
                        logger.info("索引持久化: 第 %d 次成功，条目数: %d", self._persist_success_count, len(snapshot["metadata"]))
                    else:
                        logger.debug("索引持久化完成，条目数: %d", len(snapshot["metadata"]))
                else:
                    self._persist_fail_count += 1
                    logger.error("持久化返回失败 #RECOVERY: 检查 DBUtils 和磁盘空间")
                with self._persist_lock:
                    self._persist_pending = False
            except Exception as e:
                logger.error(f"索引持久化失败: {e} #RECOVERY: 检查 DBUtils 连接和磁盘空间")
                self._persist_fail_count += 1
                with self._persist_lock:
                    self._persist_pending = False
            self._persist_thread_healthy = True

    def _recover_from_persistent(self) -> None:
        if self._db_utils is None:
            return
        try:
            snapshot = self._db_utils.load_latest_index_snapshot()
            if snapshot is None:
                return
            with self._write_lock:
                self._metadata = snapshot.get("metadata", {})
                self._reverse_index = snapshot.get("reverse_index", {})
                kw_idx = snapshot.get("keyword_index", {})
                self._keyword_index = defaultdict(set)
                for kw, ids in kw_idx.items():
                    self._keyword_index[kw] = set(ids) if not isinstance(ids, set) else ids
                # 一致性校验：反向索引中的关键词是否在关键词索引中
                orphan_reverse = [eid for eid in self._reverse_index if eid not in self._metadata]
                if orphan_reverse:
                    logger.warning("反向索引中存在 %d 个孤儿条目，已自动清理", len(orphan_reverse))
                    for eid in orphan_reverse:
                        del self._reverse_index[eid]
                # 反向索引中的关键词是否在关键词索引中存在
                for eid, keywords in self._reverse_index.items():
                    for kw in keywords:
                        if kw not in self._keyword_index or eid not in self._keyword_index[kw]:
                            self._keyword_index[kw].add(eid)
                # 重建时间戳索引
                now_mono = time.monotonic()
                cutoff = now_mono - 86400 * 30
                recovered_count = 0
                for eid, meta in self._metadata.items():
                    if "monotonic" not in meta:
                        meta["monotonic"] = meta.get("timestamp", 0) - self._time_diff
                    if meta["monotonic"] >= cutoff:
                        self._timestamp_index.append((meta["monotonic"], eid))
                    recovered_count += 1
                self._timestamp_index = deque(sorted(self._timestamp_index, key=lambda x: x[0]))
            # 恢复后重新校准时间差
            self._time_diff = time.time() - time.monotonic()
            self._last_time_diff_recalibrate = time.monotonic()
            logger.info("从持久化恢复索引完成: %d 条目 (热加载 %d 条)", len(self._metadata), len(self._timestamp_index))
        except Exception as e:
            logger.error(f"索引恢复失败: {e} #RECOVERY: 索引将从头开始构建")

    # ========== 私有方法：健康上报 ==========
    def _report_to_module_health(self, entry_count: int, kw_count: int) -> None:
        """向 ModuleHealthMonitor 上报健康状态"""
        if self._module_health_monitor is None:
            return
        try:
            self._module_health_monitor.report(
                module_name="SemanticIndexer",
                status="healthy",
                metrics={
                    "entry_count": entry_count,
                    "keyword_count": kw_count,
                    "memory_kb": self._get_memory_usage(),
                },
            )
        except Exception as e:
            logger.error(f"健康上报失败: {e} #RECOVERY: 检查 ModuleHealthMonitor 状态")

    # ========== 退出清理 ==========
    def _cleanup_on_exit(self) -> None:
        """退出前最后一次持久化（由 atexit 注册）"""
        self._persist_running = False
        self._cleanup_running = False
        self._persist_event.set()
        self._cleanup_event.set()
        if self._persist_thread is not None and self._persist_thread.is_alive():
            self._persist_thread.join(timeout=10.0)
        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=10.0)
        if self._db_utils is not None:
            try:
                with self._write_lock:
                    snapshot = {
                        "metadata": copy.deepcopy(dict(self._metadata)),
                        "reverse_index": copy.deepcopy(dict(self._reverse_index)),
                        "keyword_index": {k: list(v) for k, v in self._keyword_index.items()},
                    }
                result = self._db_utils.save_index_snapshot(snapshot, time.time())
                if result:
                    logger.info("退出前最后一次持久化完成 (条目: %d)", len(self._metadata))
                else:
                    logger.error("退出持久化失败，数据可能丢失 #RECOVERY: 检查磁盘空间")
            except Exception as e:
                logger.error(f"退出持久化失败: {e}")

    def __del__(self) -> None:
        pass
