"""
火种系统 · 因子时效分层管理器 (TemporalWeightManager)

核心职责：
1. 根据因子的时效分类（快/中/慢/休眠），记录每个因子上次权重更新时间，判断当前是否需要触发权重更新。
2. 提供标准化的因子到期查询接口，供条件权重引擎调度器调用，确保不同时效的因子以最优频率重新评估。

外部依赖（真实模块接口）：
- 无外部模块依赖。本模块为纯状态管理器，所有配置参数从构造函数注入。

接口契约：
- should_update(factor_name: str, factor_type: str) -> Dict[str, Any]
  输出字典固定包含 "should_update" (bool), "reason" (str), "warnings" (List[str])
- mark_updated(factor_name: str) -> None
- get_all_due_factors() -> Dict[str, Any]
  输出字典固定包含 "due_factors" (List[str]), "reason" (str)
- health_check() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "message" (str)

异常与降级：
- 若传入的 factor_type 不在预定义的分类中，使用默认更新间隔（慢因子），并记录 WARNING 日志。
- 若内部状态字典因未知原因损坏，自动重建空字典，确保方法不抛出异常。
- 若系统时钟出现回拨（ntp 修正等），`should_update` 将基于实际时间戳进行保守处理，标记为需要更新。

资源管理：
- 本模块仅维护一个内存中的字典，不持有任何需要手动释放的外部资源。
- 内部状态使用轻量级锁保护，确保多线程查询安全。
"""

import time
import threading
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class TemporalWeightManager:
    """因子时效分层管理器：按分类控制因子权重更新频率"""

    # 类常量（默认配置，附带单位与取值范围注释）
    # 更新间隔，秒，取值范围 [5, 604800]（5 秒到一周）
    DEFAULT_INTERVAL_FAST = 300          # 快因子默认更新间隔，5 分钟
    DEFAULT_INTERVAL_MEDIUM = 1800       # 中因子默认更新间隔，30 分钟
    DEFAULT_INTERVAL_SLOW = 86400        # 慢因子默认更新间隔，24 小时
    DEFAULT_INTERVAL_DORMANT = 604800    # 休眠因子默认更新间隔，7 天（仅复活检查）
    
    # 上次更新时间的默认初始值（设为 0，表示从未更新，首次必定触发）
    DEFAULT_LAST_UPDATE = 0.0

    # 时钟回拨容忍值，秒，取值范围 [1, 30]
    CLOCK_BACKWARD_TOLERANCE = 5.0

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        初始化时效管理器。
        所有参数优先从配置字典读取，缺失时使用类常量作为安全默认值。
        """
        cfg = config or {}
        # 从配置加载各分类的更新间隔（若提供），并进行边界校验
        self._interval_fast = self._validate_positive_float(
            cfg.get("interval_fast", self.DEFAULT_INTERVAL_FAST),
            5, 604800, "interval_fast"
        )
        self._interval_medium = self._validate_positive_float(
            cfg.get("interval_medium", self.DEFAULT_INTERVAL_MEDIUM),
            5, 604800, "interval_medium"
        )
        self._interval_slow = self._validate_positive_float(
            cfg.get("interval_slow", self.DEFAULT_INTERVAL_SLOW),
            5, 604800, "interval_slow"
        )
        self._interval_dormant = self._validate_positive_float(
            cfg.get("interval_dormant", self.DEFAULT_INTERVAL_DORMANT),
            5, 604800, "interval_dormant"
        )
        self._clock_tolerance = float(cfg.get("clock_backward_tolerance", self.CLOCK_BACKWARD_TOLERANCE))

        # 内部状态及其线程安全锁
        self._lock = threading.Lock()
        self._last_update_map: Dict[str, float] = {}
        self._factor_type_map: Dict[str, str] = {}

        # 监控指标
        self._metrics: Dict[str, Any] = {
            "total_checks": 0,
            "total_updates": 0,
            "last_check_time": 0.0
        }

        logger.info(
            f"TemporalWeightManager 初始化完成: "
            f"fast={self._interval_fast}s, medium={self._interval_medium}s, "
            f"slow={self._interval_slow}s, dormant={self._interval_dormant}s"
        )

    # ────────────────────────── 配置校验 ──────────────────────────
    @staticmethod
    def _validate_positive_float(value: Any, low: float, high: float, name: str) -> float:
        """校验浮点配置在边界内，否则返回默认值并警告"""
        try:
            v = float(value)
            if low <= v <= high:
                return v
            logger.warning(f"配置 {name}={value} 超出范围 [{low},{high}]，使用默认值")
        except (ValueError, TypeError):
            logger.warning(f"配置 {name}={value} 无效，使用默认值")
        return getattr(TemporalWeightManager, name.upper(), None) or low

    # ────────────────────────── 公共接口 ──────────────────────────
    def should_update(self, factor_name: str, factor_type: str = "medium") -> Dict[str, Any]:
        """
        判断指定因子是否达到了更新周期。
        
        参数:
            factor_name: 因子名称。
            factor_type: 因子分类，支持 "fast", "medium", "slow", "dormant"。
                        未知类型将降级为 "slow"，并记录 WARNING。
        
        返回:
            标准化字典，包含更新判定、原因和警告。
        """
        warnings: List[str] = []
        now = time.time()
        
        # 1. 解析因子类型，获取对应的更新间隔
        interval = self._get_interval_for_type(factor_type)
        effective_type = factor_type
        if factor_type not in ("fast", "medium", "slow", "dormant"):
            warn = f"未知的因子类型 '{factor_type}'，降级为 'slow' 处理"
            logger.warning(warn)
            warnings.append(warn)
            effective_type = "slow"
            interval = self._interval_slow

        # 2. 获取上次更新时间（若从未更新，默认为 0）
        with self._lock:
            last_update = self._last_update_map.get(factor_name, self.DEFAULT_LAST_UPDATE)
        elapsed = now - last_update

        # 3. 时钟回拨检测
        if elapsed < -self._clock_tolerance:
            # 系统时钟发生了显著回拨，保守处理：强制标记需要更新
            warn = (
                f"检测到系统时钟回拨 (last={last_update:.0f}, now={now:.0f}, "
                f"diff={elapsed:.0f}s)，因子 '{factor_name}' 强制标记为需要更新"
            )
            logger.warning(warn)
            warnings.append(warn)
            should = True
            with self._lock:
                self._last_update_map[factor_name] = now
            reason = f"因子 '{factor_name}' ({effective_type}): 时钟回拨，强制触发更新"
        else:
            elapsed = max(0.0, elapsed)  # 微小的负值归零
            should = elapsed >= interval
            reason = (
                f"因子 '{factor_name}' ({effective_type}): "
                f"距上次更新 {elapsed:.0f} 秒，{'需要' if should else '无需'}更新 "
                f"(间隔={interval:.0f}秒)"
            )

        # 4. 更新因子类型映射（线程安全）
        with self._lock:
            self._factor_type_map[factor_name] = effective_type
            self._metrics["total_checks"] += 1
            self._metrics["last_check_time"] = now

        logger.debug(reason)
        return {
            "should_update": should,
            "reason": reason,
            "warnings": warnings
        }

    def mark_updated(self, factor_name: str) -> None:
        """
        标记指定因子为“已更新”，将当前时间记录为最近更新时间。
        
        参数:
            factor_name: 因子名称。
        """
        now = time.time()
        with self._lock:
            self._last_update_map[factor_name] = now
            self._metrics["total_updates"] += 1
        logger.debug(f"因子 '{factor_name}' 已标记为更新 (timestamp={now:.0f})")

    def get_all_due_factors(self) -> Dict[str, Any]:
        """
        获取所有当前到期的因子列表。
        遍历内部状态表，对每个已记录的因子调用 should_update 判定，
        返回所有需要更新的因子名称。
        """
        due_factors: List[str] = []
        with self._lock:
            snapshot = dict(self._factor_type_map)
        for name, ftype in snapshot.items():
            result = self.should_update(name, ftype)
            if result["should_update"]:
                due_factors.append(name)

        reason = (
            f"到期因子扫描完成，共 {len(snapshot)} 个因子，{len(due_factors)} 个需要更新"
        )
        return {
            "due_factors": due_factors,
            "reason": reason
        }

    def get_factor_interval(self, factor_type: str) -> float:
        """
        获取指定因子类型的更新间隔（秒）。
        主要用于外部模块查询配置。
        """
        return self._get_interval_for_type(factor_type)

    def get_metrics(self) -> Dict[str, Any]:
        """获取内部监控指标，线程安全"""
        with self._lock:
            return dict(self._metrics)

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检：模拟因子注册、到期判定、时钟回拨和更新标记流程。"""
        try:
            instance = cls()
            test_factor = "health_test_factor"

            # 1. 首次检查应触发更新
            result = instance.should_update(test_factor, "fast")
            if not result["should_update"]:
                return {"status": "error", "message": "首次检查应触发更新，但返回 False"}

            # 2. 标记更新后，短时间内不应再触发
            instance.mark_updated(test_factor)
            result2 = instance.should_update(test_factor, "fast")
            if result2["should_update"]:
                return {"status": "error", "message": "刚更新后不应再次触发"}

            # 3. 模拟时钟回拨
            with instance._lock:
                instance._last_update_map[test_factor] = time.time() + 60  # 未来时间
            result3 = instance.should_update(test_factor, "fast")
            if not result3["should_update"]:
                return {"status": "error", "message": "时钟回拨应触发强制更新"}
            if not any("时钟回拨" in w for w in result3.get("warnings", [])):
                return {"status": "error", "message": "时钟回拨应产生警告"}

            # 4. 测试未知类型降级
            result4 = instance.should_update("unknown_factor", "unknown_type")
            if not result4.get("warnings"):
                return {"status": "error", "message": "未知类型应产生警告"}

            # 5. 到期扫描
            due = instance.get_all_due_factors()
            if not isinstance(due.get("due_factors"), list):
                return {"status": "error", "message": "到期扫描返回值格式错误"}

            # 6. 验证监控指标
            metrics = instance.get_metrics()
            if metrics.get("total_checks", 0) == 0:
                return {"status": "error", "message": "监控指标未正确更新"}

            return {"status": "ok", "message": "健康检查通过"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _get_interval_for_type(self, factor_type: str) -> float:
        """根据因子类型字符串返回对应的更新间隔（秒）。"""
        type_lower = factor_type.lower()
        if type_lower == "fast":
            return self._interval_fast
        elif type_lower == "medium":
            return self._interval_medium
        elif type_lower == "slow":
            return self._interval_slow
        elif type_lower == "dormant":
            return self._interval_dormant
        else:
            return self._interval_slow  # 默认兜底
