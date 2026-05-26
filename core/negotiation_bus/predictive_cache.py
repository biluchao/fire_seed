"""
火种系统 · 协商总线预测缓存 (PredictiveCache)

核心职责：
1. 在信号预热阶段缓存风控、执行等模块的约束预估值，信号正式确认时直接读取，将协商耗时压缩至亚微秒级
2. 管理缓存的写入、命中、失效与惰性淘汰，通过容量控制与过期键按需清理防止内存泄漏

外部依赖（真实模块接口）：
- core.utils.config_loader.ConfigLoader : 读取缓存有效期、最大条目数等配置参数（可选注入）

接口契约：
- put(key: str, response: Dict[str, Any], ttl_seconds: Optional[float] = None) -> Dict[str, Any] : 存入缓存
- get(key: str) -> Dict[str, Any] : 读取缓存，命中时返回预存响应，未命中或过期返回空数据
- invalidate(key: str) -> Dict[str, Any] : 主动失效指定缓存条目
- cleanup_expired() -> int : 清理所有过期条目并返回清理数量，可由外部调度器定时调用
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 ConfigLoader 不可用时，使用类常量中定义的安全默认值作为缓存参数，不影响核心缓存功能
- 当内存中缓存条目数达到上限时，自动淘汰过期条目或最早插入的条目，确保写入操作不被阻塞
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护内存缓存字典，提供 cleanup_expired 接口供外部调度器定期调用
- 写入操作在达到容量上限时自动触发惰性淘汰，读取操作对过期条目即查即删
- 不持有任何外部资源句柄，线程锁在模块销毁时自动释放
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class PredictiveCache:
    """协商总线预测缓存管理器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_TTL_SECONDS = 0.0005          # 默认缓存有效期，秒，取值范围 [0.0001, 0.01]（500微秒）
    DEFAULT_MAX_ENTRIES = 64             # 最大缓存条目数，无量纲，取值范围 [16, 256]

    def __init__(self):
        # 缓存存储：key -> {"response": Dict, "expires_at": float}
        self._cache: Dict[str, Dict[str, Any]] = {}

        # 外部依赖注入
        self._config_loader = None

        # 线程安全（保护 _cache 字典的所有读写操作）
        self._lock = threading.Lock()

        # 当前实际使用的缓存参数（可由配置覆盖）
        self._active_ttl = self.DEFAULT_TTL_SECONDS
        self._active_max_entries = self.DEFAULT_MAX_ENTRIES

        logger.info("PredictiveCache 初始化完成，TTL=%.0fμs, 最大条目=%d",
                    self._active_ttl * 1_000_000, self._active_max_entries)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        config_loader: Optional[Any] = None,
    ) -> None:
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
        """从配置加载器读取缓存参数，若读取失败则使用默认值"""
        if self._config_loader is None:
            return
        try:
            ttl = self._config_loader.get("negotiation_layer.predictive_cache.ttl_seconds")
            max_entries = self._config_loader.get("negotiation_layer.predictive_cache.max_entries")
            if isinstance(ttl, (int, float)) and ttl > 0:
                self._active_ttl = float(ttl)
                logger.debug("缓存 TTL 已更新为 %.0fμs", self._active_ttl * 1_000_000)
            if isinstance(max_entries, int) and max_entries > 0:
                self._active_max_entries = max_entries
                logger.debug("最大缓存条目已更新为 %d", self._active_max_entries)
        except Exception as e:
            logger.warning(f"读取配置失败: {e}，使用默认缓存参数")

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
            ttl_seconds: 自定义有效期（秒），为 None 时使用默认 TTL

        Returns:
            标准响应字典
        """
        # 参数校验
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

        effective_ttl = (
            ttl_seconds if isinstance(ttl_seconds, (int, float)) and ttl_seconds > 0
            else self._active_ttl
        )
        expires_at = time.time() + effective_ttl

        with self._lock:
            # 容量控制：若超过最大条目且当前键为新键，则触发一次淘汰
            if key not in self._cache and len(self._cache) >= self._active_max_entries:
                self._evict_one()

            self._cache[key] = {
                "response": response,
                "expires_at": expires_at,
            }

        logger.debug("缓存写入: key=%s, TTL=%.0fμs, 当前条目=%d",
                     key, effective_ttl * 1_000_000, len(self._cache))
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

        with self._lock:
            if key not in self._cache:
                return {
                    "status": "ok",
                    "reason": "缓存未命中 (键不存在)",
                    "data": {"hit": False},
                    "warnings": [],
                }

            entry = self._cache[key]
            now = time.time()
            if now > entry["expires_at"]:
                # 过期即删（惰性删除）
                del self._cache[key]
                logger.debug("缓存过期已删除: key=%s", key)
                return {
                    "status": "ok",
                    "reason": "缓存未命中 (已过期)",
                    "data": {"hit": False},
                    "warnings": [],
                }

            logger.debug("缓存命中: key=%s, 剩余有效 %.0fμs",
                        key, (entry["expires_at"] - now) * 1_000_000)
            return {
                "status": "ok",
                "reason": "缓存命中",
                "data": {"hit": True, "response": entry["response"]},
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

        with self._lock:
            removed = self._cache.pop(key, None)
        if removed:
            logger.debug("缓存主动失效: key=%s", key)
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
        清理所有过期缓存条目（可由外部调度器定时调用）

        Returns:
            清理条目数量
        """
        now = time.time()
        expired_keys = []
        with self._lock:
            for key, entry in self._cache.items():
                if now > entry["expires_at"]:
                    expired_keys.append(key)
            for key in expired_keys:
                del self._cache[key]

        if expired_keys:
            logger.debug("过期缓存清理: %d 条", len(expired_keys))
        return len(expired_keys)

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检

        Returns:
            标准健康检查响应字典
        """
        try:
            if not hasattr(self, '_cache') or not hasattr(self, '_lock'):
                return {
                    "status": "degraded",
                    "reason": "核心数据结构未初始化",
                    "data": {},
                    "warnings": ["core_data_missing"],
                }

            with self._lock:
                total = len(self._cache)
                expired_count = sum(
                    1 for e in self._cache.values()
                    if time.time() > e["expires_at"]
                )

            return {
                "status": "ok",
                "reason": f"PredictiveCache 正常，总条目 {total}，其中过期 {expired_count}",
                "data": {
                    "total_entries": total,
                    "expired_entries": expired_count,
                    "max_entries": self._active_max_entries,
                    "ttl_seconds": self._active_ttl,
                    "dependencies": {
                        "config_loader": self._config_loader is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查缓存字典结构完整性")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _evict_one(self) -> None:
        """
        淘汰一个缓存条目（需在锁内调用）
        优先淘汰已过期的，其次淘汰最早插入的（利用 Python 3.7+ 字典保持插入顺序特性）
        """
        now = time.time()
        # 优先删除已过期的
        for key, entry in list(self._cache.items()):
            if now > entry["expires_at"]:
                del self._cache[key]
                logger.debug("淘汰过期条目: key=%s", key)
                return

        # 若无过期条目，删除最早插入的
        if self._cache:
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]
            logger.debug("淘汰最旧条目: key=%s", oldest_key)
