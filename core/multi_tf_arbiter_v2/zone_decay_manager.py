"""
火种系统 · 区间时效衰减管理器 (ZoneDecayManager)

核心职责：
1. 管理上级周期关键区间的时效性衰减，实现四阶段（新鲜期、活跃期、记忆期、消退期）的约束力平滑递减。
2. 提供衰减系数的动态计算、最低残余约束力保护以及区间被重新测试时的衰减重启机制。

外部依赖（真实模块接口）：
- core.perception.tactile_cortex.TactileCortex : 获取波动率分位，用于动态调节衰减系数
- core.behavioral_logger.BehavioralLogger : 记录区间时效变化的审计日志
- core.negotiation_bus.NegotiationBus : 广播区间时效耗尽或重启事件

接口契约：
- get_current_strength(zone: Dict[str, Any]) -> Dict[str, Any]
  输出字典固定包含 "strength" (float), "stage" (str), "remaining_seconds" (float), "reason" (str), "warnings" (List[str])
- restart_decay(zone_id: str, new_strength: float, role: str) -> Dict[str, Any]
  输出字典固定包含 "status" (str), "new_strength" (float), "stage" (str), "reason" (str), "warnings" (List[str])
- health_check() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "message" (str)

异常与降级：
- 当 TactileCortex 不可用时，使用固定波动率分位（50%），并记录降级警告。
- 当 NegotiationBus 不可用时，区间时效事件仅记录本地日志，不阻塞主流程。
- 当 zone 缺少必要字段时，返回保守的当前约束力值，确保不因数据缺失而误判。

资源管理：
- 本模块不持有需要手动释放的资源，所有计算结果在方法返回后自动回收。
"""

import time
import math
import logging
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger(__name__)


class ZoneDecayManager:
    """区间时效衰减管理器（无状态）"""

    # 类常量（默认配置，附带单位与取值范围注释）
    # 四阶段时间窗口，秒
    FRESH_MAX_SECONDS = 300.0              # 新鲜期最大时长，秒，取值范围 [60.0, 600.0]
    ACTIVE_MAX_SECONDS = 1800.0            # 活跃期最大时长，秒，取值范围 [300.0, 3600.0]
    MEMORY_MAX_SECONDS = 7200.0            # 记忆期最大时长，秒，取值范围 [1800.0, 14400.0]
    # 各阶段约束力范围
    FRESH_STRENGTH_RANGE = (0.95, 1.0)     # 新鲜期约束力范围，无量纲
    ACTIVE_STRENGTH_RANGE = (0.70, 0.95)   # 活跃期约束力范围，无量纲
    MEMORY_STRENGTH_RANGE = (0.40, 0.70)   # 记忆期约束力范围，无量纲
    FADING_STRENGTH_RANGE = (0.15, 0.40)   # 消退期约束力范围，无量纲
    MIN_RESIDUAL_STRENGTH = 0.15           # 最低残余约束力，无量纲，取值范围 [0.05, 0.30]
    # 衰减系数（不同成交量下的衰减速度）
    DECAY_HIGH_VOL = 0.005                 # 高成交量形成区间的衰减系数，取值范围 [0.001, 0.010]
    DECAY_NORMAL_VOL = 0.012               # 正常成交量形成区间的衰减系数，取值范围 [0.005, 0.020]
    DECAY_LOW_VOL = 0.025                  # 低成交量形成区间的衰减系数，取值范围 [0.010, 0.050]
    # 波动率对衰减的调节系数
    HIGH_VOL_DECAY_MULT = 0.7              # 高波动时衰减减速（保护区间），无量纲，取值范围 [0.5, 1.0]
    LOW_VOL_DECAY_MULT = 1.5               # 低波动时衰减加速（区间易失效），无量纲，取值范围 [1.0, 2.0]
    DEFAULT_VOL_PERCENTILE = 50            # 降级默认波动率分位，%，取值范围 [0, 100]

    def __init__(self, config: Dict[str, Any]):
        # 从配置加载可调节参数，附带安全默认值
        self._fresh_max = config.get("fresh_max_seconds", self.FRESH_MAX_SECONDS)
        self._active_max = config.get("active_max_seconds", self.ACTIVE_MAX_SECONDS)
        self._memory_max = config.get("memory_max_seconds", self.MEMORY_MAX_SECONDS)
        self._fresh_range = tuple(config.get("fresh_strength_range", self.FRESH_STRENGTH_RANGE))
        self._active_range = tuple(config.get("active_strength_range", self.ACTIVE_STRENGTH_RANGE))
        self._memory_range = tuple(config.get("memory_strength_range", self.MEMORY_STRENGTH_RANGE))
        self._fading_range = tuple(config.get("fading_strength_range", self.FADING_STRENGTH_RANGE))
        self._min_residual = config.get("min_residual_strength", self.MIN_RESIDUAL_STRENGTH)
        self._decay_high_vol = config.get("decay_high_vol", self.DECAY_HIGH_VOL)
        self._decay_normal_vol = config.get("decay_normal_vol", self.DECAY_NORMAL_VOL)
        self._decay_low_vol = config.get("decay_low_vol", self.DECAY_LOW_VOL)
        self._high_vol_mult = config.get("high_vol_decay_mult", self.HIGH_VOL_DECAY_MULT)
        self._low_vol_mult = config.get("low_vol_decay_mult", self.LOW_VOL_DECAY_MULT)
        self._default_vol_pct = config.get("default_vol_percentile", self.DEFAULT_VOL_PERCENTILE)

        # 外部依赖（延迟注入）
        self._tactile_cortex: Optional[Any] = None
        self._behavioral_logger: Optional[Any] = None
        self._negotiation_bus: Optional[Any] = None

        logger.info("ZoneDecayManager 初始化完成，依赖待注入")

    # ────────────────────────── 依赖注入 ──────────────────────────
    def inject_dependencies(
        self,
        tactile_cortex: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None
    ) -> None:
        """注入外部依赖模块，并进行鸭子类型校验"""
        self._tactile_cortex = tactile_cortex
        self._behavioral_logger = behavioral_logger
        self._negotiation_bus = negotiation_bus

        if tactile_cortex is not None and not hasattr(tactile_cortex, "get_volatility_percentile"):
            logger.warning("TactileCortex 缺少 get_volatility_percentile 方法，波动率感知将降级")
        logger.info("ZoneDecayManager 依赖注入完成")

    # ────────────────────────── 公共接口 ──────────────────────────
    def get_current_strength(self, zone: Dict[str, Any]) -> Dict[str, Any]:
        """
        获取区间当前的约束力。

        :param zone: 区间信息字典，必须包含 created_at, initial_strength, decay_coefficient, formation_volume_ratio(可选)
        :return: 标准化约束力结果字典
        """
        warnings: List[str] = []
        now = time.time()

        # 参数校验
        required = ["created_at", "initial_strength"]
        missing = [f for f in required if f not in zone]
        if missing:
            reason = f"区间信息缺少必要字段: {missing}，返回保守约束力"
            logger.warning(reason)
            return {
                "strength": self._min_residual,
                "stage": "unknown",
                "remaining_seconds": 0.0,
                "reason": reason,
                "warnings": [reason]
            }

        created_at = zone["created_at"]
        initial_strength = zone["initial_strength"]
        decay_coeff = zone.get("decay_coefficient", None)
        formation_vol_ratio = zone.get("formation_volume_ratio", 1.0)

        # 若未指定衰减系数，根据形成时的成交量选择
        if decay_coeff is None:
            if formation_vol_ratio >= 1.5:
                decay_coeff = self._decay_high_vol
            elif formation_vol_ratio <= 0.7:
                decay_coeff = self._decay_low_vol
            else:
                decay_coeff = self._decay_normal_vol

        # 获取波动率分位，动态调节衰减速度
        vol_percentile = self._default_vol_pct
        if self._tactile_cortex:
            try:
                vol_percentile = self._tactile_cortex.get_volatility_percentile("1m")
                if isinstance(vol_percentile, dict):
                    vol_percentile = vol_percentile.get("percentile", self._default_vol_pct)
            except Exception as e:
                logger.warning(f"获取波动率分位失败: {e}")
                warnings.append(f"波动率感知降级: {e}")
        else:
            warnings.append("TactileCortex 未注入，波动率分位降级")

        # 调节衰减系数
        if vol_percentile >= 70:
            effective_decay = decay_coeff * self._high_vol_mult
        elif vol_percentile <= 30:
            effective_decay = decay_coeff * self._low_vol_mult
        else:
            effective_decay = decay_coeff

        # 计算已流逝时间
        elapsed = max(0.0, now - created_at)
        # 指数衰减: strength = initial * exp(-decay * elapsed_seconds)
        current_strength = initial_strength * math.exp(-effective_decay * elapsed)
        current_strength = max(self._min_residual, current_strength)

        # 判定当前阶段
        stage, remaining_seconds = self._determine_stage(elapsed)

        # 阶段约束力夹紧
        stage_strength = self._clamp_to_stage(current_strength, stage)

        reason = (
            f"区间时效衰减: elapsed={elapsed:.0f}s, stage={stage}, "
            f"strength={stage_strength:.3f} (原始={current_strength:.3f}), "
            f"decay_coeff={effective_decay:.4f}, vol_pct={vol_percentile:.0f}%"
        )
        logger.debug(reason)

        return {
            "strength": stage_strength,
            "stage": stage,
            "remaining_seconds": remaining_seconds,
            "reason": reason,
            "warnings": warnings
        }

    def restart_decay(self, zone_id: str, new_strength: float, role: str) -> Dict[str, Any]:
        """
        重启区间的时效衰减周期（通常在区间被重新测试或角色翻转后调用）。

        :param zone_id: 区间唯一标识
        :param new_strength: 新的初始约束力
        :param role: 新区间角色 (support/resistance)
        :return: 标准化重启结果字典
        """
        warnings: List[str] = []
        now = time.time()

        # 参数校验
        if new_strength <= 0:
            reason = f"无效的新约束力: {new_strength}，拒绝重启"
            logger.warning(reason)
            return {
                "status": "error",
                "new_strength": new_strength,
                "stage": "unknown",
                "reason": reason,
                "warnings": [reason]
            }

        # 约束力夹紧
        clamped_strength = max(self._min_residual, min(1.0, new_strength))

        reason = f"区间 {zone_id} 时效重启: role={role}, strength={clamped_strength:.2f}"
        logger.info(reason)

        # 审计日志
        if self._behavioral_logger:
            try:
                self._behavioral_logger.log_event(
                    module="zone_decay_manager",
                    event_type="decay_restart",
                    payload={
                        "zone_id": zone_id,
                        "new_strength": clamped_strength,
                        "role": role,
                        "timestamp": now
                    }
                )
            except Exception:
                pass

        # 广播事件
        if self._negotiation_bus:
            try:
                self._negotiation_bus.emit_event(
                    event_type="zone_decay_restarted",
                    payload={
                        "zone_id": zone_id,
                        "strength": clamped_strength,
                        "role": role
                    }
                )
            except Exception:
                pass

        return {
            "status": "ok",
            "new_strength": clamped_strength,
            "stage": "fresh",
            "reason": reason,
            "warnings": warnings
        }

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检：验证常量有效性和核心逻辑"""
        try:
            dummy_config = {}
            manager = cls(dummy_config)

            # 测试衰减计算
            test_zone = {
                "zone_id": "test",
                "created_at": time.time() - 600,  # 10 分钟前
                "initial_strength": 0.9,
                "formation_volume_ratio": 1.2
            }
            result = manager.get_current_strength(test_zone)
            if "strength" not in result or result["strength"] <= 0 or result["strength"] > 1.0:
                return {"status": "error", "message": "约束力计算异常"}
            if "stage" not in result:
                return {"status": "error", "message": "阶段判定缺失"}

            # 测试重启
            restart_result = manager.restart_decay("test_zone", 0.85, "support")
            if restart_result.get("status") != "ok":
                return {"status": "error", "message": "重启逻辑异常"}

            # 测试常量有效性
            if cls.FRESH_MAX_SECONDS <= 0 or cls.MIN_RESIDUAL_STRENGTH <= 0:
                return {"status": "error", "message": "关键常量非法"}

            return {"status": "ok", "message": "所有测试通过（含降级路径）"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _determine_stage(self, elapsed: float) -> Tuple[str, float]:
        """
        根据已流逝时间判定当前衰减阶段。

        :param elapsed: 已流逝秒数
        :return: (阶段名, 距离下一阶段剩余秒数)
        """
        if elapsed < self._fresh_max:
            return "fresh", self._fresh_max - elapsed
        elif elapsed < self._fresh_max + self._active_max:
            return "active", (self._fresh_max + self._active_max) - elapsed
        elif elapsed < self._fresh_max + self._active_max + self._memory_max:
            return "memory", (self._fresh_max + self._active_max + self._memory_max) - elapsed
        else:
            return "fading", 0.0

    def _clamp_to_stage(self, strength: float, stage: str) -> float:
        """
        将计算出的约束力夹紧到当前阶段的合法范围内。

        :param strength: 原始计算值
        :param stage: 当前阶段
        :return: 夹紧后的约束力
        """
        if stage == "fresh":
            low, high = self._fresh_range
        elif stage == "active":
            low, high = self._active_range
        elif stage == "memory":
            low, high = self._memory_range
        else:
            low, high = self._fading_range
        return max(low, min(high, strength))
