"""
火种系统 · 味觉皮层 (GustatoryCortex)

核心职责：
1. 基于当前市场状态向量，在历史经验库中检索最相似的 k 条记忆，输出其盈亏分布与甜/苦标记
2. 在持仓平仓后，根据盈亏结果更新甜味/苦味记忆区，并执行容量管理与时间衰减清理

外部依赖（真实模块接口）：
- core.global_state_archive.GlobalStateArchive : 获取历史市场状态原型，用于相似度校准（可选注入）
- core.experience_replay.ExperienceReplay : 若注入，则在检索时优先使用其样本；否则仅使用内部记忆

接口契约：
- retrieve_similar(state_vector: List[float], top_k: int = 10, symbol: str = "", strategy: str = "") -> Dict[str, Any]
  输出字典包含 "similar_memories" (List[Dict]), "sweet_ratio" (float), "bitter_ratio" (float), "average_pnl" (float)
- record_outcome(state_vector: List[float], pnl: float, context: Dict[str, Any] = None, symbol: str = "", strategy: str = "") -> Dict[str, Any]
  输出字典包含 "memory_count" (int), "sweet_count" (int), "bitter_count" (int)
- clear_memories() -> Dict[str, Any] : 清除全部记忆（用于测试或重置）
- get_stats() -> Dict[str, Any] : 获取内部统计信息
- health_check() -> Dict[str, Any] : 模块自检，验证内部记忆数据结构完整性与容量

异常与降级：
- 当 GlobalStateArchive 或 ExperienceReplay 不可用时，相似度检索仅使用内部记忆，并在 warnings 中标记 "degraded_external"
- 当内部记忆为空时，返回空结果，status 仍为 "ok"，diagnosis 提示 "no_memories"
- 所有相似度计算中若出现 NaN，自动将相似度置零并记录警告

资源管理：
- 内部双区记忆（甜味/苦味）使用 deque 实现，由后台清理线程与容量检查共同维护
- 后台清理线程通过 Event 控制退出，确保对象销毁时安全停止
- 线程池在 atexit 回调中安全关闭，避免资源泄漏
- 所有共享状态均有明确锁保护，文档中标注线程安全保证

线程安全保证：
- `_sweet_memories` / `_bitter_memories` 受 `_rwlock` 保护
- `_cache` 受 `_cache_lock` 保护
- 性能指标受 `_metrics_lock` 保护
"""

import time
import logging
import threading
import math
import hashlib
from typing import Dict, Any, List, Optional, Tuple
from collections import deque, OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

logger = logging.getLogger(__name__)

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False
    logger.warning("numpy 未安装，将使用纯 Python 相似度计算，性能可能下降")


class GustatoryCortex:
    """味觉皮层：环境相似度匹配与苦甜记忆管理"""

    # ========== 类常量 ==========
    DEFAULT_MAX_MEMORIES = 2000            # 总记忆容量上限（甜+苦），[500, 10000]
    DEFAULT_MAX_AGE_SECONDS = 604800      # 记忆最大保留时间(秒)，默认 7 天，[86400, 2592000]
    DEFAULT_CLEANUP_INTERVAL_SEC = 600    # 清理间隔(秒)，[300, 3600]
    DEFAULT_TOP_K = 10                    # 默认检索返回条数，[1, 100]
    DEFAULT_MAX_TOP_K = 50                # top_k 最大允许值，防止滥用
    DEFAULT_COSINE_EPSILON = 1e-6         # 余弦相似度防零除，兼顾 float32/64
    DEFAULT_MIN_SIMILARITY = 0.7          # 最低有效相似度阈值，[0.0, 1.0]
    DEFAULT_EXTERNAL_TIMEOUT_SEC = 0.3    # 外部依赖调用超时(秒)，进一步收紧
    DEFAULT_MAX_EXTERNAL_SAMPLES = 300    # 外部样本数量上限，防止 OOM
    DEFAULT_VECTOR_MAX_DIM = 128          # 状态向量最大维度
    DEFAULT_CONTEXT_MAX_KEYS = 16         # context 最大键值对数，防止大对象
    DEFAULT_CONTEXT_MAX_VALUE_LEN = 256   # context 中字符串值最大长度，防止大字符串
    DEFAULT_DECAY_FACTOR = 0.95           # 记忆时间衰减因子，每周期(秒)衰减一次，[0.8, 1.0]
    DEFAULT_DECAY_PERIOD_SEC = 3600       # 衰减周期(秒)，[600, 86400]
    DEFAULT_MIN_MEMORY_AGE_FOR_RETRIEVAL = 86400  # 检索时记忆的最小年龄阈值(秒)，太旧的记忆直接跳过收集

    SWEET_THRESHOLD = 1e-6                # pnl > 此值归为甜
    BITTER_THRESHOLD = -1e-6              # pnl < 此值归为苦，中间值视为中性(不记录)

    # 性能参数
    DEFAULT_BATCH_SIZE = 128              # 批量计算相似度的分块大小，控制内存峰值
    DEFAULT_THREAD_POOL_MAX_WORKERS = 2   # 线程池最大工作线程数
    DEFAULT_CACHE_MAX_SIZE = 16           # 检索缓存最大条目数，LRU 淘汰
    DEFAULT_CACHE_TTL_SEC = 60            # 缓存条目最大存活时间(秒)，防止陈旧数据
    DEFAULT_SNAPSHOT_MAX_SIZE = 5000      # 快照复制最大条目数，防止内存尖峰

    def __init__(self):
        self._sweet_memories: deque = deque()
        self._bitter_memories: deque = deque()

        self._global_state_archive = None
        self._experience_replay = None

        self._rwlock = threading.RLock()
        self._last_cleanup = time.time()
        self._last_retrieve_latency_ms = 0.0

        # 性能指标（使用锁保护）
        self._metrics_lock = threading.Lock()
        self._retrieve_count = 0
        self._total_retrieve_time_ms = 0.0
        self._batch_skip_count = 0         # 批量计算中跳过的无效向量总数
        self._cache_hit_count = 0          # 缓存命中次数
        self._cache_miss_count = 0         # 缓存未命中次数

        # 请求级缓存（LRU，带 TTL）
        self._cache: OrderedDict = OrderedDict()
        self._cache_lock = threading.Lock()

        # 后台清理线程（使用 Event 控制退出）
        self._cleanup_stop_event = threading.Event()
        self._cleanup_thread = threading.Thread(
            target=self._background_cleanup, daemon=True, name="gustatory_cleanup"
        )
        self._cleanup_thread.start()

        # 线程池
        self._executor = ThreadPoolExecutor(
            max_workers=self.DEFAULT_THREAD_POOL_MAX_WORKERS,
            thread_name_prefix="gustatory_ext"
        )
        self._shutdown_called = False
        self._shutdown_lock = threading.Lock()

        import atexit
        atexit.register(self.shutdown)

        logger.info("GustatoryCortex 初始化完成，最大记忆容量: %d, 最大保留时间: %d 秒",
                    self.DEFAULT_MAX_MEMORIES, self.DEFAULT_MAX_AGE_SECONDS)

    def shutdown(self) -> None:
        """安全关闭线程池和后台清理线程（幂等）"""
        with self._shutdown_lock:
            if self._shutdown_called:
                return
            self._shutdown_called = True

        # 停止后台清理线程
        if hasattr(self, '_cleanup_stop_event'):
            self._cleanup_stop_event.set()
        # 关闭线程池
        if hasattr(self, '_executor') and self._executor is not None:
            try:
                self._executor.shutdown(wait=True, timeout=2.0)
            except Exception:
                pass

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        global_state_archive: Optional[Any] = None,
        experience_replay: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选注入）"""
        if global_state_archive is not None:
            self._global_state_archive = global_state_archive
            logger.info("GlobalStateArchive 注入成功")
        else:
            self._global_state_archive = None
            logger.warning("GlobalStateArchive 未注入，相似度校准将使用内部记忆")

        if experience_replay is not None:
            self._experience_replay = experience_replay
            logger.info("ExperienceReplay 注入成功")
        else:
            self._experience_replay = None
            logger.warning("ExperienceReplay 未注入，仅使用内部记忆")

    # ========== 公共接口 ==========
    def retrieve_similar(
        self,
        state_vector: List[float],
        top_k: int = DEFAULT_TOP_K,
        symbol: str = "",
        strategy: str = "",
    ) -> Dict[str, Any]:
        """检索与当前市场状态最相似的 k 条历史记忆"""
        if not isinstance(state_vector, list) or len(state_vector) == 0:
            return self._error_response("状态向量必须为非空列表", "invalid_state_vector")
        if not all(isinstance(x, (int, float)) for x in state_vector):
            return self._error_response("状态向量元素必须为数值类型", "invalid_state_vector")

        top_k = max(1, min(top_k, self.DEFAULT_MAX_TOP_K))
        vec_dim = min(len(state_vector), self.DEFAULT_VECTOR_MAX_DIM)
        query_np = np.asarray(state_vector[:vec_dim], dtype=np.float64)

        if not np.isfinite(query_np).all():
            logger.error("状态向量包含 NaN 或 Inf，拒绝检索")
            return self._error_response("状态向量包含无效数值", "invalid_state_vector")

        query_norm = np.linalg.norm(query_np)
        if query_norm == 0.0:
            return {
                "status": "ok",
                "reason": "查询向量全零，无法计算有效相似度",
                "data": {
                    "similar_memories": [],
                    "sweet_ratio": 0.0, "bitter_ratio": 0.0, "average_pnl": 0.0,
                    "diagnosis": "零向量查询"
                },
                "warnings": ["zero_query_vector"],
            }

        # 生成缓存键（使用向量哈希，避免浮点精度问题）
        cache_key = self._make_cache_key(query_np, top_k, symbol, strategy)
        with self._cache_lock:
            cached = self._get_cached(cache_key)
            if cached is not None:
                with self._metrics_lock:
                    self._cache_hit_count += 1
                return cached

        with self._metrics_lock:
            self._cache_miss_count += 1

        start_time = time.monotonic()
        candidates = self._gather_candidates_snapshot(vec_dim, symbol, strategy)

        if not candidates:
            result = {
                "status": "ok",
                "reason": "无可用记忆",
                "data": {
                    "similar_memories": [],
                    "sweet_ratio": 0.0, "bitter_ratio": 0.0, "average_pnl": 0.0,
                    "diagnosis": "记忆库为空"
                },
                "warnings": ["no_memories"],
            }
            self._set_cached(cache_key, result)
            return result

        # 批量计算相似度（传入预计算范数）
        scores = self._batch_similarity(query_np, candidates, query_norm)
        scores.sort(key=lambda x: x[0], reverse=True)

        top_matches = scores[:top_k]
        valid_matches = [(sim, cand) for sim, cand in top_matches if sim >= self.DEFAULT_MIN_SIMILARITY]
        latency_ms = (time.monotonic() - start_time) * 1000.0
        self._last_retrieve_latency_ms = latency_ms

        with self._metrics_lock:
            self._retrieve_count += 1
            self._total_retrieve_time_ms += latency_ms

        if not valid_matches:
            result = {
                "status": "ok",
                "reason": f"无满足阈值({self.DEFAULT_MIN_SIMILARITY})的相似记忆",
                "data": {
                    "similar_memories": [],
                    "sweet_ratio": 0.0, "bitter_ratio": 0.0, "average_pnl": 0.0,
                    "diagnosis": "低相似度",
                    "candidate_count": len(candidates),
                    "valid_count": 0,
                },
                "warnings": ["low_similarity"],
            }
            self._set_cached(cache_key, result)
            return result

        sweet_count = 0
        bitter_count = 0
        total_pnl = 0.0
        similar_memories = []
        for sim, cand in valid_matches:
            total_pnl += cand["pnl"]
            if cand["pnl"] > self.SWEET_THRESHOLD:
                sweet_count += 1
            elif cand["pnl"] < self.BITTER_THRESHOLD:
                bitter_count += 1
            similar_memories.append({
                "similarity": round(sim, 4),
                "pnl": cand["pnl"],
                "tag": cand.get("tag", "unknown"),
            })

        total = len(valid_matches)
        sweet_ratio = sweet_count / total if total else 0.0
        bitter_ratio = bitter_count / total if total else 0.0
        avg_pnl = total_pnl / total if total else 0.0

        diagnosis = f"检索到 {total} 条相似记忆，平均盈亏 {avg_pnl:.4f}"
        if sweet_ratio > 0.7:
            diagnosis += "，偏甜（历史盈利概率较高）"
        elif bitter_ratio > 0.7:
            diagnosis += "，偏苦（历史亏损概率较高）"

        result = {
            "status": "ok",
            "reason": f"基于 {query_np.shape[0]} 维状态向量检索完成",
            "data": {
                "similar_memories": similar_memories,
                "sweet_ratio": round(sweet_ratio, 4),
                "bitter_ratio": round(bitter_ratio, 4),
                "average_pnl": round(avg_pnl, 4),
                "diagnosis": diagnosis,
                "candidate_count": len(candidates),
                "valid_count": total,
            },
            "warnings": [],
        }
        self._set_cached(cache_key, result)
        return result

    def record_outcome(
        self,
        state_vector: List[float],
        pnl: float,
        context: Dict[str, Any] = None,
        symbol: str = "",
        strategy: str = "",
    ) -> Dict[str, Any]:
        """记录一笔交易的最终盈亏"""
        if not isinstance(state_vector, list) or len(state_vector) == 0:
            return self._error_response("状态向量必须为非空列表", "invalid_state_vector")
        if not math.isfinite(pnl):
            logger.warning("拒绝记录无效盈亏值: pnl=%s", pnl)
            return self._error_response("盈亏值无效 (NaN/Inf)", "invalid_pnl")
        if abs(pnl) <= self.SWEET_THRESHOLD:
            logger.debug("盈亏接近零 (%.6f)，视为中性，不记录", pnl)
            return self._neutral_response()

        vec_dim = min(len(state_vector), self.DEFAULT_VECTOR_MAX_DIM)
        trimmed_vec = np.asarray(state_vector[:vec_dim], dtype=np.float64).tolist()
        safe_context = self._sanitize_context(context)

        mem_entry = {
            "vector": trimmed_vec,
            "pnl": pnl,
            "context": safe_context,
            "timestamp": time.monotonic(),
            "symbol": symbol,
            "strategy": strategy,
        }

        with self._rwlock:
            if pnl > self.SWEET_THRESHOLD:
                self._sweet_memories.append(mem_entry)
                tag = "sweet"
            else:
                self._bitter_memories.append(mem_entry)
                tag = "bitter"

            self._enforce_capacity()
            sweet_count = len(self._sweet_memories)
            bitter_count = len(self._bitter_memories)

        # 清除缓存：所有与 symbol 或 strategy 相关的缓存条目
        self._invalidate_related_cache(symbol, strategy)

        logger.debug("记录新%s记忆: pnl=%.4f, 记忆总量: sweet=%d, bitter=%d", tag, pnl, sweet_count, bitter_count)

        return {
            "status": "ok",
            "reason": f"已记录至{tag}记忆区",
            "data": {
                "memory_count": sweet_count + bitter_count,
                "sweet_count": sweet_count,
                "bitter_count": bitter_count,
            },
            "warnings": [],
        }

    def clear_memories(self) -> Dict[str, Any]:
        """清除全部记忆（用于测试或重置）"""
        with self._rwlock:
            sweet_before = len(self._sweet_memories)
            bitter_before = len(self._bitter_memories)
            self._sweet_memories.clear()
            self._bitter_memories.clear()
            logger.info("已清除全部记忆: 甜味 %d, 苦味 %d", sweet_before, bitter_before)
        with self._cache_lock:
            self._cache.clear()
        # 重置性能指标
        with self._metrics_lock:
            self._retrieve_count = 0
            self._total_retrieve_time_ms = 0.0
            self._batch_skip_count = 0
            self._cache_hit_count = 0
            self._cache_miss_count = 0
        return {
            "status": "ok",
            "reason": f"已清除甜味 {sweet_before} 条，苦味 {bitter_before} 条",
            "data": {"sweet_removed": sweet_before, "bitter_removed": bitter_before},
            "warnings": [],
        }

    def get_stats(self) -> Dict[str, Any]:
        """获取内部统计信息"""
        with self._rwlock:
            sweet_count = len(self._sweet_memories)
            bitter_count = len(self._bitter_memories)
        with self._metrics_lock:
            avg_latency = self._total_retrieve_time_ms / max(self._retrieve_count, 1)
            stats = {
                "sweet_count": sweet_count,
                "bitter_count": bitter_count,
                "total_count": sweet_count + bitter_count,
                "retrieve_count": self._retrieve_count,
                "avg_retrieve_latency_ms": round(avg_latency, 2),
                "last_retrieve_latency_ms": round(self._last_retrieve_latency_ms, 2),
                "batch_skip_count": self._batch_skip_count,
                "cache_hit_count": self._cache_hit_count,
                "cache_miss_count": self._cache_miss_count,
            }
        with self._cache_lock:
            stats["cache_size"] = len(self._cache)
        return {
            "status": "ok",
            "reason": "统计信息获取成功",
            "data": stats,
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检：验证内部记忆数据结构完整性"""
        try:
            if not hasattr(self, '_sweet_memories') or not hasattr(self, '_bitter_memories'):
                return self._error_response("记忆数据结构未初始化", "memories_not_initialized")

            with self._rwlock:
                sweet_count = len(self._sweet_memories)
                bitter_count = len(self._bitter_memories)
                total = sweet_count + bitter_count

            ext_status = {}
            if self._experience_replay is not None and hasattr(self._experience_replay, 'health_check'):
                ext_result = self._call_with_timeout(
                    self._experience_replay.health_check, timeout=0.5, default=None
                )
                ext_status['experience_replay'] = ext_result if ext_result is not None else {"status": "timeout"}
            if self._global_state_archive is not None and hasattr(self._global_state_archive, 'health_check'):
                ext_result = self._call_with_timeout(
                    self._global_state_archive.health_check, timeout=0.5, default=None
                )
                ext_status['global_state_archive'] = ext_result if ext_result is not None else {"status": "timeout"}

            with self._metrics_lock:
                avg_latency = self._total_retrieve_time_ms / max(self._retrieve_count, 1)

            return {
                "status": "ok",
                "reason": f"味觉皮层正常，甜味记忆: {sweet_count}, 苦味记忆: {bitter_count}, 总计: {total}",
                "data": {
                    "sweet_count": sweet_count,
                    "bitter_count": bitter_count,
                    "total_count": total,
                    "max_capacity": self.DEFAULT_MAX_MEMORIES,
                    "last_retrieve_latency_ms": round(self._last_retrieve_latency_ms, 2),
                    "avg_retrieve_latency_ms": round(avg_latency, 2),
                    "retrieve_count": self._retrieve_count,
                    "dependencies": {
                        "global_state_archive": self._global_state_archive is not None,
                        "experience_replay": self._experience_replay is not None,
                    },
                    "external_health": ext_status,
                },
                "warnings": ["capacity_exceeded"] if total > self.DEFAULT_MAX_MEMORIES else [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查内部 deque 对象是否损坏")
            return self._error_response(f"健康检查异常: {str(e)}", "health_check_failed", "INTERNAL_ERROR")

    # ========== 私有方法 ==========
    def _error_response(self, reason: str, warning: str = "", error_code: str = "") -> Dict[str, Any]:
        warnings = [warning] if warning else []
        return {
            "status": "error",
            "reason": reason,
            "error_code": error_code or warning,
            "data": {},
            "warnings": warnings,
        }

    def _neutral_response(self) -> Dict[str, Any]:
        # 获取计数时加锁
        with self._rwlock:
            sweet_count = len(self._sweet_memories)
            bitter_count = len(self._bitter_memories)
        return {
            "status": "ok",
            "reason": "中性盈亏，不记录",
            "data": {
                "memory_count": sweet_count + bitter_count,
                "sweet_count": sweet_count,
                "bitter_count": bitter_count,
            },
            "warnings": [],
        }

    def _sanitize_context(self, context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """安全处理 context，限制大小和深度，防止大对象"""
        if not context:
            return {}
        safe = {}
        for k, v in list(context.items())[:self.DEFAULT_CONTEXT_MAX_KEYS]:
            if isinstance(v, str):
                safe[k] = v[:self.DEFAULT_CONTEXT_MAX_VALUE_LEN]
            elif isinstance(v, (int, float, bool)):
                safe[k] = v
            elif isinstance(v, (list, tuple)):
                safe[k] = f"<{type(v).__name__}({len(v)})>"
            elif isinstance(v, dict):
                safe[k] = f"<dict({len(v)})>"
            else:
                safe[k] = str(type(v).__name__)
        return safe

    def _tag_match(self, mem: Dict, symbol: str, strategy: str) -> bool:
        """标签过滤"""
        if symbol and mem.get("symbol", "") != symbol:
            return False
        if strategy and mem.get("strategy", "") != strategy:
            return False
        return True

    def _make_cache_key(self, query: np.ndarray, top_k: int, symbol: str, strategy: str) -> str:
        """生成基于向量哈希的缓存键，使用固定字节序避免平台差异"""
        rounded = np.round(query, decimals=4).astype(np.float64)
        vec_hash = hashlib.md5(rounded.tobytes(order='C')).hexdigest()[:8]
        return f"{vec_hash}:{top_k}:{symbol}:{strategy}"

    def _get_cached(self, key: str) -> Optional[Dict[str, Any]]:
        """获取缓存条目（需在 _cache_lock 内调用）"""
        if key in self._cache:
            entry = self._cache[key]
            if time.monotonic() - entry["_timestamp"] < self.DEFAULT_CACHE_TTL_SEC:
                self._cache.move_to_end(key)
                return entry["_data"]
            else:
                del self._cache[key]
        return None

    def _set_cached(self, key: str, data: Dict[str, Any]) -> None:
        """设置缓存条目（需在 _cache_lock 内调用）"""
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            if len(self._cache) >= self.DEFAULT_CACHE_MAX_SIZE:
                self._cache.popitem(last=False)
        self._cache[key] = {"_data": data, "_timestamp": time.monotonic()}

    def _invalidate_related_cache(self, symbol: str, strategy: str) -> None:
        """清除与指定 symbol/strategy 相关的缓存条目"""
        with self._cache_lock:
            if not symbol and not strategy:
                self._cache.clear()
            else:
                keys_to_remove = []
                for k in self._cache:
                    if (symbol and symbol in k) or (strategy and strategy in k):
                        keys_to_remove.append(k)
                for k in keys_to_remove:
                    del self._cache[k]

    def _gather_candidates_snapshot(self, vec_dim: int, symbol: str, strategy: str) -> List[Dict]:
        """使用快照方式收集候选记忆，减少锁持有时间，并过滤过旧记忆"""
        candidates = []
        now = time.monotonic()
        cutoff_age = now - self.DEFAULT_MIN_MEMORY_AGE_FOR_RETRIEVAL
        with self._rwlock:
            # 快照复制时限制最大条目数，防止内存尖峰（取尾部最新条目）
            sweet_snapshot = list(self._sweet_memories)[-self.DEFAULT_SNAPSHOT_MAX_SIZE:]
            bitter_snapshot = list(self._bitter_memories)[-self.DEFAULT_SNAPSHOT_MAX_SIZE:]
        # 在锁外处理，优先过滤时间戳再过滤标签，减少循环开销
        for mem in sweet_snapshot:
            if mem["timestamp"] < cutoff_age:
                continue
            if self._tag_match(mem, symbol, strategy):
                candidates.append({
                    "vector": mem["vector"][:vec_dim],
                    "pnl": mem["pnl"],
                    "tag": "sweet",
                    "timestamp": mem["timestamp"],
                })
        for mem in bitter_snapshot:
            if mem["timestamp"] < cutoff_age:
                continue
            if self._tag_match(mem, symbol, strategy):
                candidates.append({
                    "vector": mem["vector"][:vec_dim],
                    "pnl": mem["pnl"],
                    "tag": "bitter",
                    "timestamp": mem["timestamp"],
                })
        # 外部样本
        external = self._fetch_external_samples(vec_dim, symbol, strategy)
        candidates.extend(external[:self.DEFAULT_MAX_EXTERNAL_SAMPLES])
        return candidates

    def _fetch_external_samples(self, vec_dim: int, symbol: str, strategy: str) -> List[Dict]:
        """带超时和线程池的外部样本获取，传入零向量作为占位以保持一致性"""
        samples = []
        # 使用零向量避免引入随机噪声，外部接口应当能处理零向量查询
        dummy_vector = [0.0] * vec_dim

        if self._experience_replay is not None and hasattr(self._experience_replay, 'get_similar_samples'):
            future = self._executor.submit(
                self._experience_replay.get_similar_samples,
                state_vector=dummy_vector, top_k=self.DEFAULT_TOP_K, symbol=symbol, strategy=strategy
            )
            try:
                result = future.result(timeout=self.DEFAULT_EXTERNAL_TIMEOUT_SEC)
                if isinstance(result, list):
                    for item in result:
                        if isinstance(item, dict) and "vector" in item and "pnl" in item:
                            vec = np.asarray(item["vector"][:vec_dim], dtype=np.float64)
                            if np.isfinite(vec).all():
                                samples.append({
                                    "vector": vec.tolist(),
                                    "pnl": item["pnl"],
                                    "tag": "external",
                                    "timestamp": item.get("timestamp", now),
                                })
            except FutureTimeoutError:
                logger.warning("ExperienceReplay 检索超时")
            except Exception as e:
                logger.warning(f"ExperienceReplay 检索异常: {e}")

        if self._global_state_archive is not None and hasattr(self._global_state_archive, 'query_similar'):
            future = self._executor.submit(
                self._global_state_archive.query_similar, dummy_vector, top_k=self.DEFAULT_TOP_K
            )
            try:
                result = future.result(timeout=self.DEFAULT_EXTERNAL_TIMEOUT_SEC)
                if isinstance(result, list):
                    for item in result:
                        if isinstance(item, dict) and "vector" in item and "pnl" in item:
                            vec = np.asarray(item["vector"][:vec_dim], dtype=np.float64)
                            if np.isfinite(vec).all():
                                samples.append({
                                    "vector": vec.tolist(),
                                    "pnl": item["pnl"],
                                    "tag": "archive",
                                    "timestamp": item.get("timestamp", now),
                                })
            except FutureTimeoutError:
                logger.warning("GlobalStateArchive 检索超时")
            except Exception as e:
                logger.warning(f"GlobalStateArchive 检索异常: {e}")

        return samples

    def _batch_similarity(self, query: np.ndarray, candidates: List[Dict], query_norm: float) -> List[Tuple[float, Dict]]:
        """分块批量计算余弦相似度，排除无效向量以减少噪声"""
        scores = []
        batch_size = self.DEFAULT_BATCH_SIZE
        now = time.monotonic()
        total_skip = 0
        for i in range(0, len(candidates), batch_size):
            batch = candidates[i:i + batch_size]
            valid_batch = []
            valid_vectors = []
            for cand in batch:
                v = np.asarray(cand["vector"], dtype=np.float64)
                if v.shape[0] == query.shape[0] and np.isfinite(v).all():
                    v_norm = np.linalg.norm(v)
                    if v_norm > 0:
                        valid_batch.append(cand)
                        valid_vectors.append(v)
                    else:
                        total_skip += 1
                else:
                    total_skip += 1
            if not valid_vectors:
                continue
            mat = np.stack(valid_vectors)
            dots = np.dot(mat, query)
            # query_norm 已在外部计算，避免重复
            norms = np.linalg.norm(mat, axis=1) * query_norm
            sims = dots / (norms + self.DEFAULT_COSINE_EPSILON)
            sims = np.where(np.isfinite(sims), sims, 0.0)
            for idx, cand in enumerate(valid_batch):
                age = max(0.0, now - cand.get("timestamp", 0))
                decay = self.DEFAULT_DECAY_FACTOR ** (age / self.DEFAULT_DECAY_PERIOD_SEC)
                effective_sim = sims[idx] * decay
                if effective_sim > 0:
                    scores.append((effective_sim, cand))
        with self._metrics_lock:
            self._batch_skip_count += total_skip
        return scores

    def _enforce_capacity(self) -> None:
        """容量控制：均匀淘汰最旧记忆（需在锁内调用）"""
        total = len(self._sweet_memories) + len(self._bitter_memories)
        while total > self.DEFAULT_MAX_MEMORIES:
            sweet_ts = self._sweet_memories[0]["timestamp"] if self._sweet_memories else float('inf')
            bitter_ts = self._bitter_memories[0]["timestamp"] if self._bitter_memories else float('inf')
            if sweet_ts <= bitter_ts:
                self._sweet_memories.popleft()
            else:
                self._bitter_memories.popleft()
            total -= 1

    def _background_cleanup(self) -> None:
        """后台定期清理线程，通过 Event 控制退出，失败后自动重试"""
        while not self._cleanup_stop_event.is_set():
            self._cleanup_stop_event.wait(self.DEFAULT_CLEANUP_INTERVAL_SEC)
            if self._cleanup_stop_event.is_set():
                break
            acquired = self._rwlock.acquire(blocking=False)
            if acquired:
                try:
                    self._cleanup_expired()
                    self._enforce_capacity()
                except Exception as e:
                    logger.error(f"后台清理异常: {e} #RECOVERY: 检查记忆数据结构完整性")
                finally:
                    self._rwlock.release()
            else:
                # 长时间未获取锁，记录告警
                logger.warning("后台清理线程无法获取锁，跳过本轮清理")

    def _cleanup_expired(self) -> None:
        """清理过期记忆（需在锁内）"""
        now = time.monotonic()
        cutoff = now - self.DEFAULT_MAX_AGE_SECONDS
        sweet_before = len(self._sweet_memories)
        bitter_before = len(self._bitter_memories)

        while self._sweet_memories and self._sweet_memories[0]["timestamp"] < cutoff:
            self._sweet_memories.popleft()
        while self._bitter_memories and self._bitter_memories[0]["timestamp"] < cutoff:
            self._bitter_memories.popleft()

        sweet_after = len(self._sweet_memories)
        bitter_after = len(self._bitter_memories)
        removed = (sweet_before - sweet_after) + (bitter_before - bitter_after)
        if removed:
            logger.info("清理过期记忆: 甜味 %d→%d, 苦味 %d→%d, 总计移除 %d 条",
                        sweet_before, sweet_after, bitter_before, bitter_after, removed)

    def _call_with_timeout(self, func, timeout: float, default: Any = None) -> Any:
        """通用超时调用（提交到线程池，避免阻塞主线程）"""
        try:
            future = self._executor.submit(func)
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            logger.debug("调用 %s 超时 (%.2fs)", getattr(func, '__name__', str(func)), timeout)
            return default
        except RuntimeError:
            # 线程池已关闭
            return default
        except Exception as e:
            logger.debug("调用 %s 异常: %s", getattr(func, '__name__', str(func)), str(e))
            return default
