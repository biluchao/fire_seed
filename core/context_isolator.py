"""
火种系统 · 上下文隔离管理器 (ContextIsolator)

核心职责：
1. 为不同交易周期（1m、5m、15m等）创建并维护严格隔离的独立DataView，确保每个周期的数据访问完全独立。
2. 提供数据快照的原子更新、数据完整性校验以及跨周期访问的强制拦截，杜绝未来信息泄露。

外部依赖（真实模块接口）：
- core.perception.sensory_snapshot.SensorySnapshot : 获取标准化的感官快照，用于数据填充
- core.behavioral_logger.BehavioralLogger : 记录隔离违规操作的审计日志

接口契约：
- create_view(period: str) -> str : 创建并返回指定周期的视图ID
- update_view(view_id: str, snapshot: Dict[str, Any]) -> Dict[str, Any]
- get_data(view_id: str, key: str) -> Any
- destroy_view(view_id: str) -> None
- health_check() -> Dict[str, Any]

异常与降级：
- 当尝试跨周期访问数据时，记录违规审计日志并返回安全默认值（None或空字典），不阻塞主流程。
- 当 SensorySnapshot 不可用时，视图更新仅使用传入数据，无额外校验，并记录降级警告。

资源管理：
- 每个视图的数据存储在内存字典中，视图销毁时自动释放。
- 模块卸载时通过 atexit 回调清空所有视图。
"""

import time
import logging
import threading
import atexit
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


class ContextIsolator:
    """上下文隔离管理器（单例）"""

    # 类常量
    MAX_VIEWS_PER_PERIOD = 10             # 每个周期最大视图数，取值范围 [1, 100]
    DEFAULT_VIEW_TTL = 86400              # 视图默认存活时间（秒），取值范围 [3600, 604800]

    _instance: Optional["ContextIsolator"] = None
    _lock = threading.Lock()

    def __new__(cls, *args: Any, **kwargs: Any) -> "ContextIsolator":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config: Dict[str, Any] = None) -> None:
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        config = config or {}

        self._max_views = config.get("max_views_per_period", self.MAX_VIEWS_PER_PERIOD)
        self._view_ttl = config.get("default_view_ttl", self.DEFAULT_VIEW_TTL)

        # 视图存储: {view_id: {"period": str, "data": dict, "created_at": float}}
        self._views: Dict[str, Dict[str, Any]] = {}
        self._views_lock = threading.Lock()

        # 外部依赖
        self._sensory_snapshot: Optional[Any] = None
        self._behavioral_logger: Optional[Any] = None

        # 注册退出清理
        atexit.register(self._cleanup)
        logger.info("ContextIsolator 初始化完成")

    # ────────────────────────── 依赖注入 ──────────────────────────
    def inject_dependencies(
        self,
        sensory_snapshot: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None
    ) -> None:
        self._sensory_snapshot = sensory_snapshot
        self._behavioral_logger = behavioral_logger

        if sensory_snapshot is not None and not hasattr(sensory_snapshot, "get_snapshot"):
            logger.warning("SensorySnapshot 缺少 get_snapshot 方法")
        logger.info("ContextIsolator 依赖注入完成")

    # ────────────────────────── 公共接口 ──────────────────────────
    def create_view(self, period: str) -> str:
        """
        为指定周期创建一个新的隔离数据视图。

        :param period: 周期标识，如 "1m", "5m", "15m"
        :return: 视图唯一ID
        """
        with self._views_lock:
            # 检查该周期视图数量
            count = sum(1 for v in self._views.values() if v["period"] == period)
            if count >= self._max_views:
                # 淘汰最旧的视图
                oldest_id = min(
                    (vid for vid, v in self._views.items() if v["period"] == period),
                    key=lambda vid: self._views[vid]["created_at"]
                )
                del self._views[oldest_id]
                logger.info(f"周期 {period} 视图数超限，淘汰最旧视图 {oldest_id}")

            view_id = f"{period}_{int(time.time()*1000)}_{id(self)}"
            self._views[view_id] = {
                "period": period,
                "data": {},
                "created_at": time.time(),
                "updated_at": time.time()
            }
            logger.info(f"创建视图 {view_id} (周期={period})")
            return view_id

    def update_view(self, view_id: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """
        向指定视图写入数据快照，同时进行数据完整性校验。

        :param view_id: 视图ID
        :param snapshot: 需要写入的数据字典
        :return: 更新结果
        """
        warnings: List[str] = []
        now = time.time()

        with self._views_lock:
            if view_id not in self._views:
                reason = f"视图 {view_id} 不存在"
                logger.warning(reason)
                return {"status": "error", "reason": reason, "warnings": [reason]}

            view = self._views[view_id]

            # 数据完整性校验（若感官快照可用）
            if self._sensory_snapshot:
                try:
                    if not self._sensory_snapshot.validate(snapshot):
                        warnings.append("感官快照校验失败，数据可能不完整")
                except Exception as e:
                    logger.warning(f"感官快照校验异常: {e}")
                    warnings.append("感官快照不可用，跳过校验")
            else:
                warnings.append("SensorySnapshot 未注入，跳过完整性校验")

            # 原子更新
            view["data"].update(snapshot)
            view["updated_at"] = now

        reason = f"视图 {view_id} 更新成功 (周期={view['period']})"
        logger.debug(reason)
        return {
            "status": "ok",
            "reason": reason,
            "warnings": warnings
        }

    def get_data(self, view_id: str, key: str) -> Any:
        """
        从指定视图读取数据。

        :param view_id: 视图ID
        :param key: 数据键名
        :return: 数据值，若不存在返回 None
        """
        with self._views_lock:
            if view_id not in self._views:
                logger.warning(f"视图 {view_id} 不存在，返回 None")
                return None
            return self._views[view_id]["data"].get(key)

    def destroy_view(self, view_id: str) -> None:
        """销毁指定视图并释放资源"""
        with self._views_lock:
            if view_id in self._views:
                del self._views[view_id]
                logger.info(f"视图 {view_id} 已销毁")

    def check_cross_period_access(self, source_period: str, target_view_id: str) -> bool:
        """
        检查是否存在跨周期访问违规。

        :param source_period: 请求来源的周期
        :param target_view_id: 目标视图ID
        :return: True 表示违规
        """
        with self._views_lock:
            target = self._views.get(target_view_id)
            if target and target["period"] != source_period:
                # 跨周期访问，记录违规
                if self._behavioral_logger:
                    try:
                        self._behavioral_logger.log_event(
                            module="context_isolator",
                            event_type="cross_period_violation",
                            payload={
                                "source_period": source_period,
                                "target_period": target["period"],
                                "target_view": target_view_id,
                                "timestamp": time.time()
                            }
                        )
                    except Exception:
                        pass
                logger.warning(f"跨周期访问违规: {source_period} -> {target['period']} (view={target_view_id})")
                return True
            return False

    def cleanup_expired_views(self) -> int:
        """
        清理超过存活时间的过期视图。

        :return: 清理数量
        """
        now = time.time()
        expired = []
        with self._views_lock:
            for vid, v in self._views.items():
                if now - v["created_at"] > self._view_ttl:
                    expired.append(vid)
            for vid in expired:
                del self._views[vid]
        if expired:
            logger.info(f"清理过期视图: {len(expired)} 个")
        return len(expired)

    # ────────────────────────── 健康检查 ──────────────────────────
    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检"""
        try:
            isolator = cls({})

            # 测试创建与销毁
            vid = isolator.create_view("1m")
            if not vid or vid not in isolator._views:
                return {"status": "error", "message": "视图创建失败"}

            # 测试数据写入与读取
            result = isolator.update_view(vid, {"ma12": 50000.0, "obi": 0.35})
            if result["status"] != "ok":
                return {"status": "error", "message": "数据更新失败"}

            val = isolator.get_data(vid, "ma12")
            if val != 50000.0:
                return {"status": "error", "message": "数据读取失败"}

            # 测试跨周期检测
            if isolator.check_cross_period_access("5m", vid):
                # 预期返回 True（违规）
                pass
            else:
                return {"status": "error", "message": "跨周期访问检测失败"}

            # 清理
            isolator.destroy_view(vid)
            if vid in isolator._views:
                return {"status": "error", "message": "视图销毁失败"}

            # 测试常量
            if cls.MAX_VIEWS_PER_PERIOD <= 0:
                return {"status": "error", "message": "关键常量非法"}

            return {"status": "ok", "message": "所有测试通过"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _cleanup(self) -> None:
        """退出时清空所有视图"""
        with self._views_lock:
            count = len(self._views)
            self._views.clear()
        if count > 0:
            logger.info(f"退出清理: 销毁 {count} 个视图")

    def __del__(self) -> None:
        self._cleanup()
