"""
火种系统 · 多周期仲裁器入口 (MultiTfArbiterV2)

核心职责：
1. 作为多周期协同体系的统一入口，整合区间厚度计算、时效衰减管理、角色翻转、穿越监控与趋势波浪斜线识别五个子模块。
2. 对外提供标准化的区间约束查询接口与趋势斜线识别接口，供策略引擎在信号生成与仓位计算时调用。

外部依赖（真实模块接口）：
- core.multi_tf_arbiter_v2.zone_thickness_calculator.ZoneThicknessCalculator : 计算上级周期区间的动态厚度
- core.multi_tf_arbiter_v2.zone_decay_manager.ZoneDecayManager : 获取区间时效衰减后的当前约束力
- core.multi_tf_arbiter_v2.zone_role_flipper.ZoneRoleFlipper : 执行区间突破后的角色翻转
- core.multi_tf_arbiter_v2.zone_crossing_monitor.ZoneCrossingMonitor : 监控小周期价格穿越区间的三阶段过程
- core.multi_tf_arbiter_v2.trend_wave_identifier.TrendWaveIdentifier : 识别趋势波浪斜线通道
- core.behavioral_logger.BehavioralLogger : 记录仲裁决策的审计日志
- core.negotiation_bus.NegotiationBus : 广播区间状态变更事件

接口契约：
- query_zone_constraint(symbol: str, period: str, price: float, direction: int, zone: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]
  输出字典固定包含 "constraint_active" (bool), "effective_strength" (float), "thickness" (float), "stage" (str), "position_mult" (float), "stop_anchor" (Optional[float]), "reason" (str), "warnings" (List[str])
- identify_trend_wave(symbol: str, period: str, direction: int, klines: Optional[List[Dict[str, float]]] = None, atr: Optional[float] = None) -> Dict[str, Any]
  输出字典固定包含 "slope" (float), "intercept" (float), "confidence" (float), "thickness" (float), "upper_bound" (float), "lower_bound" (float), "reason" (str), "warnings" (List[str])
- health_check() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "message" (str)

异常与降级：
- 当任一子模块不可用时，对应功能静默降级，返回保守默认值，不阻塞主流程。
- 当 zone 缺少必要字段时，返回 constraint_active=False，确保策略引擎不会基于残缺数据做决策。
- 当 klines 数据不足时，趋势斜线识别返回无效斜线，不影响其他功能。

资源管理：
- 本模块仅作为协调入口，不持有需要手动释放的资源。
- 所有子模块在系统退出时由 system_builder 统一管理生命周期。
"""

import time
import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


class MultiTfArbiterV2:
    """多周期仲裁器入口（协调层）"""

    # 类常量（默认配置，附带单位与取值范围注释）
    DEFAULT_STRENGTH_THRESHOLD = 0.20     # 区间约束力最低生效阈值，无量纲，取值范围 [0.10, 0.50]
    ENABLE_ZONE_MONITORING = True         # 是否启用穿越监控，布尔值
    ENABLE_TREND_WAVE = True              # 是否启用趋势波浪斜线识别，布尔值

    def __init__(self, config: Dict[str, Any]):
        # 从配置加载可调节参数，附带安全默认值
        self._strength_threshold = config.get("strength_threshold", self.DEFAULT_STRENGTH_THRESHOLD)
        self._enable_monitoring = config.get("enable_zone_monitoring", self.ENABLE_ZONE_MONITORING)
        self._enable_trend_wave = config.get("enable_trend_wave", self.ENABLE_TREND_WAVE)

        # 子模块（延迟注入）
        self._thickness_calculator: Optional[Any] = None
        self._decay_manager: Optional[Any] = None
        self._role_flipper: Optional[Any] = None
        self._crossing_monitor: Optional[Any] = None
        self._wave_identifier: Optional[Any] = None

        # 外部服务（延迟注入）
        self._behavioral_logger: Optional[Any] = None
        self._negotiation_bus: Optional[Any] = None

        logger.info("MultiTfArbiterV2 初始化完成，子模块待注入")

    # ────────────────────────── 依赖注入 ──────────────────────────
    def inject_dependencies(
        self,
        thickness_calculator: Optional[Any] = None,
        decay_manager: Optional[Any] = None,
        role_flipper: Optional[Any] = None,
        crossing_monitor: Optional[Any] = None,
        wave_identifier: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None
    ) -> None:
        """注入子模块与外部服务依赖"""
        self._thickness_calculator = thickness_calculator
        self._decay_manager = decay_manager
        self._role_flipper = role_flipper
        self._crossing_monitor = crossing_monitor
        self._wave_identifier = wave_identifier
        self._behavioral_logger = behavioral_logger
        self._negotiation_bus = negotiation_bus

        # 校验子模块可用性并记录降级状态
        if thickness_calculator is None:
            logger.warning("ZoneThicknessCalculator 未注入，厚度计算将使用默认值")
        if decay_manager is None:
            logger.warning("ZoneDecayManager 未注入，时效衰减将使用默认值")
        if role_flipper is None:
            logger.warning("ZoneRoleFlipper 未注入，角色翻转功能不可用")
        if crossing_monitor is None:
            logger.warning("ZoneCrossingMonitor 未注入，穿越监控将跳过")
        if wave_identifier is None:
            logger.warning("TrendWaveIdentifier 未注入，趋势波浪识别将跳过")
        logger.info("MultiTfArbiterV2 依赖注入完成")

    # ────────────────────────── 公共接口 ──────────────────────────
    def query_zone_constraint(
        self,
        symbol: str,
        period: str,
        price: float,
        direction: int,
        zone: Dict[str, Any],
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        查询上级周期区间的完整约束信息。

        :param symbol: 交易对标识
        :param period: 当前小周期标识 (1m, 5m, 15m)
        :param price: 当前价格
        :param direction: 持仓方向 (1=多, -1=空, 0=无仓位)
        :param zone: 上级周期区间信息，需包含 zone_id, role, upper_bound, lower_bound, strength, created_at
        :param context: 附加上下文，如成交量比率、穿越开始时间等
        :return: 标准化区间约束字典
        """
        warnings: List[str] = []
        now = time.time()

        # 参数校验
        if not zone or "zone_id" not in zone:
            reason = "区间信息无效或缺少 zone_id，约束不可用"
            logger.warning(reason)
            return self._build_inactive_result(reason, [reason])

        zone_id = zone["zone_id"]
        current_role = zone.get("role", "unknown")

        # 1. 计算动态厚度
        thickness = zone.get("thickness", 0.0)
        if self._thickness_calculator:
            try:
                thickness_result = self._thickness_calculator.calculate_thickness(zone, context)
                thickness = thickness_result.get("thickness", thickness)
                warnings.extend(thickness_result.get("warnings", []))
            except Exception as e:
                logger.warning(f"厚度计算失败: {e}，使用原始厚度 {thickness}")
                warnings.append(f"厚度计算降级: {e}")
        else:
            warnings.append("厚度计算器未注入，使用原始厚度")

        # 2. 获取时效衰减后的当前约束力
        effective_strength = zone.get("strength", 0.0)
        decay_stage = "unknown"
        if self._decay_manager:
            try:
                decay_result = self._decay_manager.get_current_strength(zone)
                effective_strength = decay_result.get("strength", effective_strength)
                decay_stage = decay_result.get("stage", "unknown")
                warnings.extend(decay_result.get("warnings", []))
            except Exception as e:
                logger.warning(f"时效衰减计算失败: {e}，使用原始约束力 {effective_strength}")
                warnings.append(f"时效衰减降级: {e}")
        else:
            warnings.append("时效管理器未注入，使用原始约束力")

        # 3. 判断约束力是否足够生效
        if effective_strength < self._strength_threshold:
            reason = f"区间约束力不足: {effective_strength:.2f} < {self._strength_threshold}，区间视为失效"
            logger.debug(reason)
            return self._build_inactive_result(reason, warnings)

        # 4. 穿越监控（若启用）
        position_mult = 1.0
        stop_anchor: Optional[float] = None
        crossing_stage = "idle"
        if self._enable_monitoring and self._crossing_monitor:
            try:
                monitor_zone = {
                    "upper_bound": zone.get("upper_bound", 0.0),
                    "lower_bound": zone.get("lower_bound", 0.0),
                    "thickness": thickness,
                    "strength": effective_strength
                }
                crossing_result = self._crossing_monitor.evaluate(price, direction, monitor_zone, context)
                crossing_stage = crossing_result.get("stage", "idle")
                position_mult = crossing_result.get("position_mult", 1.0)
                stop_anchor = crossing_result.get("stop_anchor")
                warnings.extend(crossing_result.get("warnings", []))
            except Exception as e:
                logger.warning(f"穿越监控评估失败: {e}，使用默认仓位系数")
                warnings.append(f"穿越监控降级: {e}")
        elif not self._enable_monitoring:
            crossing_stage = "disabled"

        reason = (
            f"区间约束生效: zone_id={zone_id}, role={current_role}, "
            f"strength={effective_strength:.2f}, thickness={thickness:.2f}, "
            f"decay_stage={decay_stage}, crossing_stage={crossing_stage}, "
            f"position_mult={position_mult:.2f}"
        )
        logger.debug(reason)

        return {
            "constraint_active": True,
            "effective_strength": effective_strength,
            "thickness": thickness,
            "stage": crossing_stage,
            "decay_stage": decay_stage,
            "position_mult": position_mult,
            "stop_anchor": stop_anchor,
            "reason": reason,
            "warnings": warnings
        }

    def identify_trend_wave(
        self,
        symbol: str,
        period: str,
        direction: int,
        klines: Optional[List[Dict[str, float]]] = None,
        atr: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        识别趋势波浪斜线通道。

        :param symbol: 交易对标识
        :param period: 周期标识 (1m, 5m, 15m)
        :param direction: 趋势方向 (1=上升, -1=下降, 0=无趋势)
        :param klines: 可选，外部传入的K线数据
        :param atr: 可选，当前周期的ATR值
        :return: 标准化斜线识别结果
        """
        if not self._enable_trend_wave:
            return {
                "slope": 0.0, "intercept": 0.0, "confidence": 0.0,
                "thickness": 0.0, "upper_bound": 0.0, "lower_bound": 0.0,
                "reason": "趋势波浪识别已禁用",
                "warnings": []
            }

        if self._wave_identifier is None:
            return {
                "slope": 0.0, "intercept": 0.0, "confidence": 0.0,
                "thickness": 0.0, "upper_bound": 0.0, "lower_bound": 0.0,
                "reason": "TrendWaveIdentifier 未注入，功能不可用",
                "warnings": ["wave_identifier 未注入"]
            }

        try:
            return self._wave_identifier.identify_slope(symbol, period, direction, klines, atr)
        except Exception as e:
            logger.warning(f"趋势波浪识别失败: {e}")
            return {
                "slope": 0.0, "intercept": 0.0, "confidence": 0.0,
                "thickness": 0.0, "upper_bound": 0.0, "lower_bound": 0.0,
                "reason": f"趋势波浪识别异常: {e}",
                "warnings": [str(e)]
            }

    def notify_zone_break(
        self,
        symbol: str,
        price: float,
        direction: int,
        zone: Dict[str, Any],
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        通知区间被突破，触发角色翻转与时效重启。

        :param symbol: 交易对标识
        :param price: 突破价格
        :param direction: 突破方向 (1=向上, -1=向下)
        :param zone: 被突破的区间信息
        :param context: 附加上下文，如成交量比率
        :return: 翻转结果字典
        """
        if self._role_flipper is None:
            return {
                "flipped": False,
                "new_role": zone.get("role", "unknown"),
                "inherited_strength": zone.get("strength", 0.0),
                "reason": "ZoneRoleFlipper 未注入，翻转未执行",
                "warnings": ["role_flipper 未注入"]
            }

        try:
            return self._role_flipper.flip_zone(price, direction, zone, context)
        except Exception as e:
            logger.warning(f"区间角色翻转失败: {e}")
            return {
                "flipped": False,
                "new_role": zone.get("role", "unknown"),
                "inherited_strength": zone.get("strength", 0.0),
                "reason": f"翻转异常: {e}",
                "warnings": [str(e)]
            }

    def reset_zone_monitoring(self, symbol: str) -> None:
        """重置指定品种的穿越监控状态"""
        if self._crossing_monitor:
            try:
                self._crossing_monitor.reset(symbol)
                logger.debug(f"[{symbol}] 穿越监控状态已重置")
            except Exception as e:
                logger.warning(f"重置穿越监控失败: {e}")

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检：验证常量有效性和降级路径"""
        try:
            dummy_config = {}
            arbiter = cls(dummy_config)

            # 测试所有子模块未注入时的降级行为
            result = arbiter.query_zone_constraint(
                symbol="test", period="1m", price=50000.0, direction=1,
                zone={"zone_id": "test", "role": "support", "upper_bound": 51000.0, "lower_bound": 49000.0, "strength": 0.8, "created_at": time.time() - 60},
                context={}
            )
            if not isinstance(result, dict) or "constraint_active" not in result:
                return {"status": "error", "message": "query_zone_constraint 返回格式异常"}

            # 测试趋势波浪降级
            wave_result = arbiter.identify_trend_wave("test", "1m", 1)
            if not isinstance(wave_result, dict) or "slope" not in wave_result:
                return {"status": "error", "message": "identify_trend_wave 返回格式异常"}

            # 测试常量有效性
            if cls.DEFAULT_STRENGTH_THRESHOLD <= 0:
                return {"status": "error", "message": "关键常量非法"}

            return {"status": "ok", "message": "所有测试通过（含降级路径）"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _build_inactive_result(self, reason: str, warnings: List[str]) -> Dict[str, Any]:
        """构建区间约束未生效的标准化返回"""
        return {
            "constraint_active": False,
            "effective_strength": 0.0,
            "thickness": 0.0,
            "stage": "inactive",
            "decay_stage": "unknown",
            "position_mult": 1.0,
            "stop_anchor": None,
            "reason": reason,
            "warnings": warnings
        }
