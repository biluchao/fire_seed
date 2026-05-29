"""
火种系统 · 协商总线预测缓存 (PredictiveCache)

核心职责：
1. 在信号预热阶段缓存风控、执行等模块的约束预估值，信号正式确认时直接读取，将协商耗时压缩至亚微秒级
2. 管理缓存的写入、命中、失效与惰性淘汰，通过分段锁实现多信号并发访问，避免全局串行瓶颈
3. 采用写入感知的淘汰策略：优先淘汰已被访问过的条目，保护尚未被读取的新写入条目，匹配预协商语义
4. 提供延迟分位统计、命中率、桶级负载分布等可观测性指标，并支持波动率自适应的缓存有效期

外部依赖（真实模块接口）：
- core.utils.config_loader.ConfigLoader : 读取缓存有效期、最大条目数、分桶数等配置参数（可选注入）

接口契约：
- put(key: str, response: Dict[str, Any], ttl_seconds: Optional[float] = None) -> Dict[str, Any] : 存入缓存
- get(key: str) -> Dict[str, Any] : 读取缓存，命中时返回预存响应，未命中或过期返回空数据
- invalidate(key: str) -> Dict[str, Any] : 主动失效指定缓存条目
- cleanup_expired() -> int : 清理所有过期条目并返回清理数量，可由外部调度器定时调用
- update_ttl_by_volatility(volatility_percentile: float) -> None : 根据当前波动率分位动态调整缓存有效期
- health_check() -> Dict[str, Any] : 模块自检，返回命中率、延迟分位、桶级负载等关键性能指标
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 ConfigLoader 不可用时，使用类常量中定义的安全默认值作为缓存参数
- 当内存中缓存条目数达到上限时，自动淘汰过期条目或低价值条目（已访问的LRU条目），确保写入不被阻塞
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护内存缓存字典，提供 cleanup_expired 接口供外部调度器定期调用
- 写入操作在达到容量上限时自动触发惰性淘汰，读取操作对过期条目即查即删
- 不持有任何外部资源句柄，分段锁在模块销毁时自动释放
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional
from collections import deque
import numpy as np

logger = logging.getLogger(__name__)


class PredictiveCache:
    """协商总线预测缓存管理器（分段锁 + 写入感知LRU淘汰 + 波动率自适应TTL + 可观测性）"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_TTL_SECONDS = 0.0005          # 默认缓存有效期，秒，取值范围 [0.0001, 0.01]（500微秒）
    DEFAULT_MAX_ENTRIES_PER_BUCKET = 16  # 每桶最大条目数，无量纲，取值范围 [8, 64]
    DEFAULT_BUCKET_COUNT = 8              # 分段锁桶数，无量纲，取值范围 [4, 16]
    HIGH_VOL_TTL = 0.0002                 # 高波动时TTL，秒，200μs
    NORMAL_VOL_TTL = 0.0005               # 正常波动时TTL，秒，500μs
    LOW_VOL_TTL = 0.0010                  # 低波动时TTL，秒，1ms
    LATENCY_HISTORY_SIZE = 1000           # 延迟统计窗口大小

    def __init__(self):
        # 分段缓存：每个桶独立维护自己的缓存字典、锁、统计计数器和延迟记录
        self._buckets = [
            {
                "cache": {},
                "lock": threading.Lock(),
                "hit_count": 0,
                "miss_count": 0,
                "put_latency": deque(maxlen=self.LATENCY_HISTORY_SIZE),
                "get_latency": deque(maxlen=self.LATENCY_HISTORY_SIZE),
            }
            for _ in range(self.DEFAULT_BUCKET_COUNT)
        ]

        # 全局统计锁（仅保护TTL等全局配置的读写）
        self._config_lock = threading.Lock()

        # 外部依赖注入
        self._config_loader = None

        # 当前实际使用的缓存参数（可由配置覆盖）
        self._active_ttl = self.DEFAULT_TTL_SECONDS
        self._active_max_entries_per_bucket = self.DEFAULT_MAX_ENTRIES_PER_BUCKET
        self._active_bucket_count = self.DEFAULT_BUCKET_COUNT

        logger.info(
            "PredictiveCache 初始化完成：%d个桶, TTL=%.0fμs, 每桶最大条目=%d",
            self._active_bucket_count,
            self._active_ttl * 1_000_000,
            self._active_max_entries_per_bucket,
        )

    # ========== 依赖注入 ==========
    def inject_dependencies(self, config_loader: Optional[Any] = None) -> None:
        """
        注入外部依赖（可选注入，未注入时使用类常量默认值）

        Args:
            config_loader: 配置加载器实例，用于读取缓存参数
        """
        if config_loader is not None:
            self._config_loader = config_loader
            self._load_config()
            logger.info("ConfigLoader 注入成功")
        else:
            logger.info("ConfigLoader 未注入，使用默认缓存参数")

    def _load_config(self) -> None:
        """从配置加载器读取缓存参数，若读取失败则静默使用默认值"""
        if self._config_loader is None:
            return
        try:
            # TTL
            ttl = self._config_loader.get("negotiation_layer.predictive_cache.ttl_seconds")
            if isinstance(ttl, (int, float)):
                with self._config_lock:
                    self._active_ttl = float(ttl)
            elif isinstance(ttl, str):
                try:
                    with self._config_lock:
                        self._active_ttl = float(ttl.strip())
                except ValueError:
                    logger.error(
                        "TTL 配置值无法解析: %s #RECOVERY: 检查配置中 negotiation_layer.predictive_cache.ttl_seconds 是否为合法数字",
                        ttl,
                    )

            # 每桶最大条目数
            max_entries = self._config_loader.get("negotiation_layer.predictive_cache.max_entries_per_bucket")
            if isinstance(max_entries, int) and max_entries > 0:
                self._active_max_entries_per_bucket = max_entries
            elif isinstance(max_entries, str):
                try:
                    self._active_max_entries_per_bucket = int(max_entries.strip())
                except ValueError:
                    logger.error("max_entries_per_bucket 配置值无法解析: %s", max_entries)

            # 桶数量（仅允许在初始化时设定）
            bucket_count = self._config_loader.get("negotiation_layer.predictive_cache.bucket_count")
            if isinstance(bucket_count, int) and bucket_count > 0:
                if bucket_count != self._active_bucket_count:
                    logger.warning("bucket_count 仅可在初始化阶段设置，当前值=%d 保持不变", self._active_bucket_count)
            elif isinstance(bucket_count, str):
                try:
                    bc = int(bucket_count.strip())
                    if bc != self._active_bucket_count:
                        logger.warning("bucket_count 仅可在初始化阶段设置，忽略配置")
                except ValueError:
                    pass
        except Exception as e:
            logger.warning(f"读取配置失败: {e}，使用默认缓存参数")

    # ========== 分段锁桶定位 ==========
    def _get_bucket_index(self, key: str) -> int:
        """基于完整键的哈希计算桶索引（确保负载均衡）"""
        return hash(key) % self._active_bucket_count

    # ========== 公共接口 ==========
    def put(
        self,
        key: str,
        response: Dict[str, Any],
        ttl_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        存入预协商缓存

        Args:
            key: 缓存键，建议格式为 "{signal_type}:{symbol}:{direction}"
            response: 预协商响应字典，必须包含 NeuroConstraint 标准字段
            ttl_seconds: 自定义有效期（秒），为 None 时使用当前默认 TTL

        Returns:
            标准响应字典
        """
        if not key or not isinstance(key, str):
            logger.warning(f"无效缓存键: {key}")
            return {
                "status": "error",
                "reason": "缓存键必须为非空字符串",
                "data": {},
                "warnings": ["invalid_key"],
            }

        if not isinstance(response, dict):
            logger.warning(f"缓存值非字典类型: {type(response).__name__}")
            return {
                "status": "error",
                "reason": "缓存值必须为字典类型",
                "data": {},
                "warnings": ["invalid_response_type"],
            }

        # 安全获取当前TTL（加锁读取）
        with self._config_lock:
            current_ttl = self._active_ttl

        effective_ttl = (
            ttl_seconds
            if isinstance(ttl_seconds, (int, float)) and ttl_seconds > 0
            else current_ttl
        )
        expires_at = time.time() + effective_ttl

        bucket_idx = self._get_bucket_index(key)
        bucket = self._buckets[bucket_idx]

        t0 = time.perf_counter()
        with bucket["lock"]:
            cache = bucket["cache"]
            # 容量控制
            if key not in cache and len(cache) >= self._active_max_entries_per_bucket:
                self._evict_one(bucket)

            cache[key] = {
                "response": response,
                "expires_at": expires_at,
                "_created_at": time.time(),
                "_last_access": time.time(),
                "_access_count": 0,
            }

        elapsed = time.perf_counter() - t0
        bucket["put_latency"].append(elapsed)

        logger.debug(
            "缓存写入: key=%s, TTL=%.0fμs, bucket=%d",
            key,
            effective_ttl * 1_000_000,
            bucket_idx,
        )
        return {
            "status": "ok",
            "reason": f"缓存写入成功，有效期至 {expires_at:.6f}",
            "data": {"key": key, "expires_at": expires_at},
            "warnings": [],
        }

    def get(self, key: str) -> Dict[str, Any]:
        """
        读取预协商缓存

        Args:
            key: 缓存键

        Returns:
            标准响应字典，命中时 data 中包含缓存的响应，未命中时 data 为空
        """
        if not key or not isinstance(key, str):
            return {
                "status": "error",
                "reason": "缓存键必须为非空字符串",
                "data": {},
                "warnings": ["invalid_key"],
            }

        bucket_idx = self._get_bucket_index(key)
        bucket = self._buckets[bucket_idx]
        hit = False
        response = None

        t0 = time.perf_counter()
        with bucket["lock"]:
            cache = bucket["cache"]
            entry = cache.get(key)
            now = time.time()

            if entry is None:
                bucket["miss_count"] += 1
            elif now > entry["expires_at"]:
                # 过期即删（惰性删除）
                del cache[key]
                bucket["miss_count"] += 1
                logger.debug("缓存过期已删除: key=%s, bucket=%d", key, bucket_idx)
            else:
                hit = True
                entry["_last_access"] = now
                entry["_access_count"] = entry.get("_access_count", 0) + 1
                bucket["hit_count"] += 1
                response = entry["response"]
                logger.debug(
                    "缓存命中: key=%s, bucket=%d, 剩余%.0fμs",
                    key,
                    bucket_idx,
                    (entry["expires_at"] - now) * 1_000_000,
                )

        elapsed = time.perf_counter() - t0
        bucket["get_latency"].append(elapsed)

        if hit:
            return {
                "status": "ok",
                "reason": "缓存命中",
                "data": {"hit": True, "response": response},
                "warnings": [],
            }
        else:
            return {
                "status": "ok",
                "reason": "缓存未命中 (不存在或已过期)",
                "data": {"hit": False},
                "warnings": [],
            }

    def invalidate(self, key: str) -> Dict[str, Any]:
        """
        主动失效指定缓存条目

        Args:
            key: 缓存键

        Returns:
            标准响应字典
        """
        if not key or not isinstance(key, str):
            return {
                "status": "error",
                "reason": "缓存键必须为非空字符串",
                "data": {},
                "warnings": ["invalid_key"],
            }

        bucket_idx = self._get_bucket_index(key)
        bucket = self._buckets[bucket_idx]

        with bucket["lock"]:
            removed = bucket["cache"].pop(key, None)

        if removed:
            logger.debug("缓存主动失效: key=%s, bucket=%d", key, bucket_idx)
            return {
                "status": "ok",
                "reason": "缓存已失效",
                "data": {"key": key, "was_present": True},
                "warnings": [],
            }
        return {
            "status": "ok",
            "reason": "缓存条目不存在，无需失效",
            "data": {"key": key, "was_present": False},
            "warnings": [],
        }

    def cleanup_expired(self) -> int:
        """
        清理所有桶中的过期缓存条目

        Returns:
            清理条目总数
        """
        now = time.time()
        total_removed = 0
        for bucket in self._buckets:
            expired_keys = []
            with bucket["lock"]:
                cache = bucket["cache"]
                for key, entry in cache.items():
                    if now > entry["expires_at"]:
                        expired_keys.append(key)
                for key in expired_keys:
                    del cache[key]
                    total_removed += 1

        if total_removed > 0:
            logger.debug("全局过期缓存清理: %d 条", total_removed)
        return total_removed

    def update_ttl_by_volatility(self, volatility_percentile: float) -> None:
        """
        根据当前波动率分位动态调整缓存有效期

        Args:
            volatility_percentile: 波动率分位，取值范围 0.0-1.0，越高代表当前波动越剧烈
        """
        if volatility_percentile > 0.7:
            new_ttl = self.HIGH_VOL_TTL
        elif volatility_percentile > 0.3:
            new_ttl = self.NORMAL_VOL_TTL
        else:
            new_ttl = self.LOW_VOL_TTL

        with self._config_lock:
            if new_ttl != self._active_ttl:
                old_ttl = self._active_ttl
                self._active_ttl = new_ttl
                logger.info(
                    "TTL 波动率自适应调整: %.0fμs → %.0fμs (波动率分位=%.2f)",
                    old_ttl * 1_000_000,
                    new_ttl * 1_000_000,
                    volatility_percentile,
                )

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检，返回命中率、延迟分位、桶级负载等完整性能指标

        Returns:
            标准健康检查响应字典
        """
        try:
            total_entries = 0
            expired_entries = 0
            total_hits = 0
            total_misses = 0
            all_put_latencies = []
            all_get_latencies = []
            bucket_stats = []
            now = time.time()

            for i, bucket in enumerate(self._buckets):
                with bucket["lock"]:
                    cache = bucket["cache"]
                    entries = len(cache)
                    total_entries += entries
                    expired = sum(1 for e in cache.values() if now > e["expires_at"])
                    expired_entries += expired
                    total_hits += bucket["hit_count"]
                    total_misses += bucket["miss_count"]
                    # 收集延迟样本（无锁拷贝）
                    put_lat = list(bucket["put_latency"])
                    get_lat = list(bucket["get_latency"])
                    all_put_latencies.extend(put_lat)
                    all_get_latencies.extend(get_lat)
                    bucket_stats.append({
                        "bucket_index": i,
                        "entries": entries,
                        "expired": expired,
                        "hit_count": bucket["hit_count"],
                        "miss_count": bucket["miss_count"],
                    })

            total_requests = total_hits + total_misses
            hit_rate = total_hits / total_requests if total_requests > 0 else 0.0

            # 延迟分位计算
            def percentile(data, p):
                return np.percentile(data, p) if data else 0.0

            return {
                "status": "ok",
                "reason": f"PredictiveCache 正常，总条目={total_entries}, 命中率={hit_rate:.1%}",
                "data": {
                    "total_entries": total_entries,
                    "expired_entries": expired_entries,
                    "hit_count": total_hits,
                    "miss_count": total_misses,
                    "hit_rate": round(hit_rate, 4),
                    "put_latency_us": {
                        "p50": round(percentile(all_put_latencies, 50) * 1_000_000, 1),
                        "p95": round(percentile(all_put_latencies, 95) * 1_000_000, 1),
                        "p99": round(percentile(all_put_latencies, 99) * 1_000_000, 1),
                    },
                    "get_latency_us": {
                        "p50": round(percentile(all_get_latencies, 50) * 1_000_000, 1),
                        "p95": round(percentile(all_get_latencies, 95) * 1_000_000, 1),
                        "p99": round(percentile(all_get_latencies, 99) * 1_000_000, 1),
                    },
                    "bucket_count": self._active_bucket_count,
                    "max_entries_per_bucket": self._active_max_entries_per_bucket,
                    "active_ttl_seconds": self._active_ttl,
                    "bucket_stats": bucket_stats,
                    "dependencies": {
                        "config_loader": self._config_loader is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查分段桶数据结构完整性")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _evict_one(self, bucket: dict) -> None:
        """
        从指定桶中淘汰一个缓存条目（需在锁内调用）
        策略：优先淘汰已过期条目；其次淘汰已被访问过的LRU条目；
        最后淘汰最早写入的未访问条目（保护尚未被读取的新缓存）
        """
        cache = bucket["cache"]
        now = time.time()

        # 1. 淘汰过期条目
        for key, entry in list(cache.items()):
            if now > entry["expires_at"]:
                del cache[key]
                logger.debug("淘汰过期条目: key=%s", key)
                return

        # 2. 分离“已访问”和“未访问”条目
        accessed = {}
        untouched = {}
        for key, entry in cache.items():
            if entry.get("_access_count", 0) > 0:
                accessed[key] = entry
            else:
                untouched[key] = entry

        # 3. 优先淘汰已访问的LRU条目（因为它们已被利用过，价值较低）
        if accessed:
            lru_key = min(accessed, key=lambda k: accessed[k]["_last_access"])
            del cache[lru_key]
            logger.debug("淘汰已访问LRU条目: key=%s", lru_key)
            return

        # 4. 所有条目都未被访问过，淘汰最早写入的（保护更新的条目）
        if untouched:
            oldest_key = min(untouched, key=lambda k: untouched[k]["_created_at"])
            del cache[oldest_key]
            logger.debug("淘汰未访问最旧条目: key=%s", oldest_key)
