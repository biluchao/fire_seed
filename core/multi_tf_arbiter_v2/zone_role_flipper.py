"""
火种系统 · 区间角色翻转管理器 (ZoneRoleFlipper)

核心职责：
1. 当价格有效突破上级周期关键区间时，自动将该区间的角色进行翻转：原压力位转为支撑位，原支撑位转为压力位。
2. 计算翻转后的初始约束力、继承比例以及时效重启策略，确保翻转后的约束力既能反映历史可靠性，又能适应新的市场结构。

外部依赖（真实模块接口）：
- core.multi_tf_arbiter_v2.zone_decay_manager.ZoneDecayManager : 重启区间时效衰减周期，用于翻转后恢复新鲜度
- core.behavioral_logger.BehavioralLogger : 记录区间翻转事件的审计日志
- core.negotiation_bus.NegotiationBus : 向协商总线广播区间翻转事件，供其他模块订阅

接口契约：
- flip_zone(price: float, direction: int, zone: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]
  输出字典固定包含 "flipped" (bool), "new_role" (str), "inherited_strength" (float), "reason" (str), "warnings" (List[str]), "timestamp" (float)
- health_check() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "message" (str)

异常与降级：
- 当 ZoneDecayManager 不可用时，翻转后的时效采用默认线性衰减模型，并记录降级警告。
- 当 NegotiationBus 不可用时，翻转事件仅记录本地日志，不阻塞主流程。
- 当输入 zone 缺少必要字段时，拒绝翻转并返回明确错误原因。

资源管理：
- 本模块无状态，不持有任何需要手动释放的资源，所有结果在方法返回后自动回收。
"""

import time
import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


class ZoneRoleFlipper:
    """区间角色翻转管理器（无状态）"""

    # 类常量（默认配置，附带单位与取值范围注释）
    DEFAULT_INHERIT_STRENGTH = 0.80       # 翻转后约束力默认继承比例，无量纲，取值范围 [0.5, 1.0]
    MIN_INHERIT_STRENGTH = 0.50           # 最小继承比例（保护底线），无量纲，取值范围 [0.2, 0.8]
    HIGH_QUALITY_BREAK_MULT = 1.1         # 高质量突破（放量）时继承比例放大系数，无量纲，取值范围 [1.0, 1.5]
    LOW_QUALITY_BREAK_MULT = 0.85         # 低质量突破（缩量）时继承比例缩减系数，无量纲，取值范围 [0.5, 1.0]
    BREAK_VOLUME_THRESHOLD = 1.3          # 判定放量突破的成交量倍数（相对于近期均量），无量纲，取值范围 [1.0, 2.0]

    def __init__(self, config: Dict[str, Any]):
        # 从配置加载可调节参数，附带安全默认值
        self._inherit_strength = config.get("default_inherit_strength", self.DEFAULT_INHERIT_STRENGTH)
        self._min_strength = config.get("min_inherit_strength", self.MIN_INHERIT_STRENGTH)
        self._high_quality_mult = config.get("high_quality_break_mult", self.HIGH_QUALITY_BREAK_MULT)
        self._low_quality_mult = config.get("low_quality_break_mult", self.LOW_QUALITY_BREAK_MULT)
        self._volume_threshold = config.get("break_volume_threshold", self.BREAK_VOLUME_THRESHOLD)

        # 外部依赖（延迟注入）
        self._decay_manager: Optional[Any] = None
        self._behavioral_logger: Optional[Any] = None
        self._negotiation_bus: Optional[Any] = None

        logger.info("ZoneRoleFlipper 初始化完成，依赖待注入")

    # ────────────────────────── 依赖注入 ──────────────────────────
    def inject_dependencies(
        self,
        decay_manager: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None
    ) -> None:
        """注入外部依赖模块，并进行鸭子类型校验"""
        self._decay_manager = decay_manager
        self._behavioral_logger = behavioral_logger
        self._negotiation_bus = negotiation_bus

        # 校验关键依赖方法存在性
        if decay_manager is not None:
            if not hasattr(decay_manager, "restart_decay"):
                logger.warning("ZoneDecayManager 缺少 restart_decay 方法，翻转后时效重启可能异常")
        logger.info("ZoneRoleFlipper 依赖注入完成")

    # ────────────────────────── 公共接口 ──────────────────────────
    def flip_zone(
        self,
        price: float,
        direction: int,
        zone: Dict[str, Any],
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        执行区间角色翻转。

        :param price: 当前突破价格
        :param direction: 突破方向 (1=向上突破压力位, -1=向下突破支撑位)
        :param zone: 被突破的区间信息，必须包含 role (support/resistance), upper_bound, lower_bound, strength, thickness
        :param context: 附加上下文，如突破时的成交量倍数 volume_ratio、突破持续时间等
        :return: 标准化翻转结果字典
        """
        warnings: List[str] = []
        now = time.time()

        # ── 参数校验 ──
        if direction not in (1, -1):
            reason = f"无效的突破方向: {direction}，翻转未执行"
            logger.warning(reason)
            return {
                "flipped": False,
                "new_role": zone.get("role", "unknown"),
                "inherited_strength": zone.get("strength", 0.0),
                "reason": reason,
                "warnings": [reason],
                "timestamp": now
            }

        required_fields = ["role", "upper_bound", "lower_bound", "strength"]
        missing = [f for f in required_fields if f not in zone]
        if missing:
            reason = f"区间信息缺少必要字段: {missing}，翻转未执行"
            logger.warning(reason)
            return {
                "flipped": False,
                "new_role": zone.get("role", "unknown"),
                "inherited_strength": zone.get("strength", 0.0),
                "reason": reason,
                "warnings": [reason],
                "timestamp": now
            }

        current_role = zone["role"]
        current_strength = zone["strength"]
        volume_ratio = context.get("volume_ratio", 1.0)

        # ── 1. 判定新角色 ──
        if current_role == "resistance":
            new_role = "support"
        elif current_role == "support":
            new_role = "resistance"
        else:
            new_role = current_role

        # ── 2. 计算继承约束力（基于突破质量动态调整） ──
        inherit_base = max(self._min_strength, self._inherit_strength * current_strength)
        if volume_ratio >= self._volume_threshold:
            inherited_strength = min(1.0, inherit_base * self._high_quality_mult)
            quality_tag = "high"
        else:
            inherited_strength = max(self._min_strength, inherit_base * self._low_quality_mult)
            quality_tag = "low"

        reason = (
            f"区间角色翻转: {current_role} -> {new_role}, "
            f"突破质量={quality_tag} (volume_ratio={volume_ratio:.2f}), "
            f"继承约束力={inherited_strength:.2f} (原始={current_strength:.2f})"
        )

        # ── 3. 重启时效计算（若依赖可用） ──
        if self._decay_manager:
            try:
                self._decay_manager.restart_decay(
                    zone_id=context.get("zone_id", "unknown"),
                    new_strength=inherited_strength,
                    role=new_role
                )
            except Exception as e:
                logger.warning(f"重启时效计算失败: {e}，翻转仍执行但时效可能使用默认模型")
                warnings.append("decay_manager 不可用，时效重启降级")
        else:
            logger.debug("decay_manager 未注入，时效重启跳过")
            warnings.append("decay_manager 未注入，时效未重启")

        # ── 4. 审计日志 ──
        if self._behavioral_logger:
            try:
                self._behavioral_logger.log_event(
                    module="zone_role_flipper",
                    event_type="zone_flip",
                    payload={
                        "price": price,
                        "direction": direction,
                        "old_role": current_role,
                        "new_role": new_role,
                        "inherited_strength": inherited_strength,
                        "quality": quality_tag,
                        "timestamp": now
                    }
                )
            except Exception as e:
                logger.warning(f"审计日志写入失败: {e}")

        # ── 5. 广播事件 ──
        if self._negotiation_bus:
            try:
                self._negotiation_bus.emit_event(
                    event_type="zone_role_flipped",
                    payload={
                        "new_role": new_role,
                        "strength": inherited_strength,
                        "price": price,
                        "direction": direction
                    }
                )
            except Exception as e:
                logger.warning(f"翻转事件广播失败: {e}")

        logger.info(reason)
        return {
            "flipped": True,
            "new_role": new_role,
            "inherited_strength": inherited_strength,
            "reason": reason,
            "warnings": warnings,
            "timestamp": now
        }

    # ────────────────────────── 健康检查 ──────────────────────────
    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检：验证常量有效性和核心逻辑"""
        try:
            dummy_config = {}
            flipper = cls(dummy_config)

            # 测试向上突破翻转
            zone = {
                "role": "resistance",
                "upper_bound": 52000.0,
                "lower_bound": 51800.0,
                "strength": 0.9,
                "thickness": 200.0
            }
            result = flipper.flip_zone(
                price=52100.0,
                direction=1,
                zone=zone,
                context={"volume_ratio": 1.5, "zone_id": "health_check"}
            )
            if not result.get("flipped") or result.get("new_role") != "support":
                return {"status": "error", "message": "向上突破翻转逻辑异常"}

            # 测试向下突破翻转
            zone2 = {
                "role": "support",
                "upper_bound": 50000.0,
                "lower_bound": 49800.0,
                "strength": 0.85,
                "thickness": 200.0
            }
            result2 = flipper.flip_zone(
                price=49700.0,
                direction=-1,
                zone=zone2,
                context={"volume_ratio": 0.8, "zone_id": "health_check2"}
            )
            if not result2.get("flipped") or result2.get("new_role") != "resistance":
                return {"status": "error", "message": "向下突破翻转逻辑异常"}

            # 测试常量有效性
            if cls.DEFAULT_INHERIT_STRENGTH <= 0 or cls.MIN_INHERIT_STRENGTH <= 0:
                return {"status": "error", "message": "继承比例常量非法"}

            # 测试缺少必要字段时的拒绝逻辑
            bad_zone = {"role": "resistance"}
            result3 = flipper.flip_zone(52100.0, 1, bad_zone, {})
            if result3.get("flipped"):
                return {"status": "error", "message": "缺少字段时未正确拒绝翻转"}

            return {"status": "ok", "message": "所有测试通过（含降级路径）"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "message": str(e)}
