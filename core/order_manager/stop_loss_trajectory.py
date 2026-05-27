"""
火种系统 · 止损轨迹管理器 (StopLossTrajectory)

核心职责：
1. 管理每笔持仓止损价格的完整生命周期，确保止损只向有利方向移动（不可逆上移/下移）
2. 根据持仓阶段、浮盈幅度、连续时间衰减、加仓合并成本保护等因素，计算并输出最优止损位置

外部依赖（真实模块接口）：
- core.order_manager.lifecycle_stages.LifecycleStages : 获取当前持仓所处的生命周期阶段
- atr 值通过方法参数传入，由上游模块（如 TactileCortex）提供，本模块不直接依赖
- position_health_scorer 预留注入接口，当前版本未直接参与止损计算

接口契约：
- calculate_new_stop(position_id: str, current_price: float, direction: int, entry_price: float,
    existing_stop: float, stage: str, profit_atr: float, add_layer_count: int,
    merged_cost: Optional[float], hold_seconds: float, atr: Optional[float]) -> Dict[str, Any]
- validate_stop_direction(new_stop: float, existing_stop: float, direction: int) -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 LifecycleStages 不可用时，默认按成熟期标准执行止损计算
- 当 atr 参数未传入或异常时，使用类常量 DEFAULT_ATR 作为默认波动率估计，保守计算止损
- 所有降级值在类常量区明确声明

资源管理：
- 本模块为纯计算模块，不持有任何外部资源句柄
- 所有中间计算结果在方法返回后自动回收
"""

import logging
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)


class StopLossTrajectory:
    """止损轨迹管理器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_ATR = 0.005                   # 默认ATR（当atr参数未传入时使用），百分比，取值范围 [0.001, 0.05]
    DEFAULT_ATR_MULT_INITIAL = 0.8         # 初始止损ATR倍数（孵化期），无量纲，取值范围 [0.5, 1.5]
    DEFAULT_ATR_MULT_TRAILING = 1.2        # 追踪止损ATR倍数（成熟期），无量纲，取值范围 [0.8, 2.0]
    DEFAULT_ATR_MULT_AGGRESSIVE = 0.4      # 激进锁定止损ATR倍数（衰减/终止期），无量纲，[0.2, 0.8]
    DEFAULT_ATR_MULT_MERGE_BUFFER = 0.2    # 加仓合并成本后额外缓冲ATR倍数，无量纲，[0.1, 0.5]
    MIN_STOP_DISTANCE_PCT = 0.0005         # 最小止损距离（占价格百分比），无量纲，[0.0001, 0.002]
    TIME_DECAY_START_SEC = 60              # 时间衰减开始生效的持仓秒数，秒，[30, 300]
    TIME_DECAY_FULL_SEC = 360              # 时间衰减完全生效的持仓秒数，秒，[180, 600]
    TIME_DECAY_MIN_RATIO = 0.25            # 时间衰减后止损距离的最小保留比例，无量纲，[0.1, 0.5]
    COMPRESSION_STAGES = {                 # 紧缩利润阶梯（浮盈ATR倍数 -> 止损ATR倍数）
        0.5: 0.8,                          # 浮盈>0.5ATR：止损收紧至ATR×0.8
        1.0: 0.5,                          # 浮盈>1.0ATR：止损收紧至ATR×0.5
        1.5: 0.3,                          # 浮盈>1.5ATR：止损收紧至ATR×0.3
        3.0: 0.15,                         # 浮盈>3.0ATR：止损收紧至ATR×0.15
    }

    def __init__(self):
        self._lifecycle_stages = None
        logger.info("StopLossTrajectory 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        lifecycle_stages: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）

        Args:
            lifecycle_stages: 生命周期阶段管理器
        """
        if lifecycle_stages is not None:
            self._lifecycle_stages = lifecycle_stages
            logger.info("LifecycleStages 注入成功")
        else:
            logger.warning("LifecycleStages 未注入，默认按成熟期标准执行")

    # ========== 公共接口 ==========
    @classmethod
    def calculate_new_stop(
        cls,
        position_id: str,
        current_price: float,
        direction: int,
        entry_price: float,
        existing_stop: float,
        stage: str,
        profit_atr: float,
        add_layer_count: int = 0,
        merged_cost: Optional[float] = None,
        hold_seconds: float = 0.0,
        atr: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        计算最优止损位置

        Args:
            position_id: 持仓唯一标识
            current_price: 当前市场价格
            direction: 持仓方向 (1=多头, -1=空头)
            entry_price: 开仓均价
            existing_stop: 当前已有止损价
            stage: 当前生命周期阶段 (incubation/acceleration/maturity/decline/termination)
            profit_atr: 当前浮盈（ATR倍数）
            add_layer_count: 已加仓次数，默认0
            merged_cost: 加仓后的合并成本价，默认None
            hold_seconds: 持仓已持有秒数，默认0.0
            atr: 当前ATR值，默认None（使用类常量默认值）

        Returns:
            标准响应字典，data 中包含 new_stop_candidate, stop_distance_atr, compression_triggered 等字段
        """
        # 参数校验
        if direction not in (1, -1):
            logger.warning(f"无效方向参数 direction={direction}")
            return {
                "status": "error",
                "reason": f"无效方向参数: {direction}，有效值为 1 (多头) 或 -1 (空头)",
                "data": {},
                "warnings": ["invalid_direction"],
            }

        if current_price <= 0 or entry_price <= 0:
            logger.warning(f"价格参数异常: current={current_price}, entry={entry_price}")
            return {
                "status": "error",
                "reason": f"价格参数必须为正数: current={current_price}, entry={entry_price}",
                "data": {},
                "warnings": ["invalid_price"],
            }

        # 使用传入的 atr 或类常量作为回退
        effective_atr = atr if atr is not None else cls.DEFAULT_ATR
        if effective_atr <= 0:
            effective_atr = cls.DEFAULT_ATR
            logger.debug(f"ATR 值异常，使用默认值: {effective_atr}")

        warnings = []
        new_stop = existing_stop
        compression_triggered = False

        # ---- 阶段一：根据生命周期阶段确定基础止损宽度 ----
        stage_atr_mult = cls._get_stage_atr_mult(stage)

        # ---- 阶段二：根据紧缩利润阶梯调整止损宽度 ----
        compression_mult = 1.0
        if profit_atr <= 0:
            logger.debug(
                f"持仓 {position_id} 处于浮亏状态 (profit_atr={profit_atr:.4f})，跳过紧缩利润阶梯"
            )
        else:
            for threshold, mult in sorted(cls.COMPRESSION_STAGES.items()):
                if profit_atr >= threshold:
                    compression_mult = min(compression_mult, mult)
                    compression_triggered = True

        # ---- 阶段三：连续时间衰减调整 ----
        time_decay_mult = cls._calculate_time_decay(stage, hold_seconds)

        # ---- 阶段四：计算最终止损距离 ----
        final_atr_mult = stage_atr_mult * compression_mult * time_decay_mult
        stop_distance = effective_atr * final_atr_mult

        # 确保最小止损距离
        min_distance = current_price * cls.MIN_STOP_DISTANCE_PCT
        if stop_distance < min_distance:
            stop_distance = min_distance
            logger.debug(f"止损距离过小，已强制设为最小距离: {min_distance:.6f}")

        # 计算新止损候选值（多空双向对称）
        if direction == 1:
            new_stop_candidate = current_price - stop_distance
        else:
            new_stop_candidate = current_price + stop_distance

        # ---- 阶段五：加仓合并成本保护 ----
        if merged_cost is not None and add_layer_count > 0:
            merge_buffer = effective_atr * cls.DEFAULT_ATR_MULT_MERGE_BUFFER
            if direction == 1:
                merged_protect = merged_cost - merge_buffer
                if new_stop_candidate < merged_protect:
                    new_stop_candidate = merged_protect
                    warnings.append("加仓合并成本保护已触发：多头新止损低于合并成本安全线")
                    logger.debug(
                        f"合并成本保护触发: new_stop 从 {current_price - stop_distance:.4f} "
                        f"调整为 {new_stop_candidate:.4f} (merged_cost={merged_cost:.4f})"
                    )
            else:
                merged_protect = merged_cost + merge_buffer
                if new_stop_candidate > merged_protect:
                    new_stop_candidate = merged_protect
                    warnings.append("加仓合并成本保护已触发：空头新止损高于合并成本安全线")
                    logger.debug(
                        f"合并成本保护触发: new_stop 从 {current_price + stop_distance:.4f} "
                        f"调整为 {new_stop_candidate:.4f} (merged_cost={merged_cost:.4f})"
                    )

        # ---- 阶段六：不可逆校验 ----
        is_valid, validation_reason = cls._validate_stop_direction(
            new_stop_candidate, existing_stop, direction
        )
        if not is_valid:
            new_stop_candidate = existing_stop
            warnings.append(validation_reason)

        return {
            "status": "ok",
            "reason": (
                f"止损计算完成，阶段={stage}, 浮盈ATR={profit_atr:.2f}, "
                f"最终ATR倍数={final_atr_mult:.2f}, 持仓时长={hold_seconds:.0f}s"
            ),
            "data": {
                "position_id": position_id,
                "new_stop_candidate": round(new_stop_candidate, 4),
                "stop_distance_atr": round(final_atr_mult, 2),
                "compression_triggered": compression_triggered,
                "stage_atr_mult": round(stage_atr_mult, 2),
                "compression_mult": round(compression_mult, 2),
                "time_decay_mult": round(time_decay_mult, 2),
                "direction": direction,
            },
            "warnings": warnings,
        }

    @classmethod
    def validate_stop_direction(
        cls,
        new_stop: float,
        existing_stop: float,
        direction: int,
    ) -> Dict[str, Any]:
        """
        校验止损移动方向是否合规（独立于 calculate_new_stop 的公开校验接口）

        Args:
            new_stop: 拟设置的新止损价
            existing_stop: 当前已生效的止损价
            direction: 持仓方向 (1=多头, -1=空头)

        Returns:
            标准响应字典
        """
        if direction not in (1, -1):
            return {
                "status": "error",
                "reason": f"无效方向参数: {direction}",
                "data": {"is_valid": False},
                "warnings": ["invalid_direction"],
            }

        is_valid, reason = cls._validate_stop_direction(new_stop, existing_stop, direction)
        return {
            "status": "ok",
            "reason": reason,
            "data": {"is_valid": is_valid},
            "warnings": [] if is_valid else [reason],
        }

    # ========== 健康检查 ==========
    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """
        模块自检

        Returns:
            标准健康检查响应字典
        """
        try:
            # 测试一：核心计算逻辑
            test_result = cls.calculate_new_stop(
                position_id="health_check_test",
                current_price=100.0,
                direction=1,
                entry_price=99.5,
                existing_stop=99.0,
                stage="maturity",
                profit_atr=1.2,
                atr=0.002,
                hold_seconds=120.0,
            )
            if test_result["status"] != "ok":
                return {
                    "status": "error",
                    "reason": f"核心计算逻辑异常: {test_result.get('reason', '未知错误')}",
                    "data": {},
                    "warnings": ["calculation_test_failed"],
                }

            # 测试二：多空对称性
            long_result = cls.calculate_new_stop(
                position_id="symmetry_long",
                current_price=100.0, direction=1, entry_price=99.0,
                existing_stop=98.5, stage="maturity", profit_atr=2.0, atr=0.005,
            )
            short_result = cls.calculate_new_stop(
                position_id="symmetry_short",
                current_price=100.0, direction=-1, entry_price=101.0,
                existing_stop=101.5, stage="maturity", profit_atr=2.0, atr=0.005,
            )
            if long_result["data"]["new_stop_candidate"] <= long_result["data"].get("existing_stop", 0) or \
               short_result["data"]["new_stop_candidate"] >= short_result["data"].get("existing_stop", 0):
                return {
                    "status": "error",
                    "reason": "多空对称性校验失败",
                    "data": {},
                    "warnings": ["symmetry_test_failed"],
                }

            # 测试三：方向校验
            valid_result = cls.validate_stop_direction(99.5, 98.5, 1)
            invalid_result = cls.validate_stop_direction(98.0, 98.5, 1)
            if not valid_result["data"]["is_valid"] or invalid_result["data"]["is_valid"]:
                return {
                    "status": "error",
                    "reason": "方向校验逻辑异常",
                    "data": {},
                    "warnings": ["validation_test_failed"],
                }

            # 测试四：首次设置止损放行
            first_set_result = cls.validate_stop_direction(99.0, 0.0, 1)
            if not first_set_result["data"]["is_valid"]:
                return {
                    "status": "error",
                    "reason": "首次设置止损校验异常",
                    "data": {},
                    "warnings": ["first_set_test_failed"],
                }

            return {
                "status": "ok",
                "reason": "StopLossTrajectory 自检通过：核心逻辑、多空对称、方向校验、首次设置均正常",
                "data": {},
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查类常量定义和计算逻辑完整性")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    @classmethod
    def _get_stage_atr_mult(cls, stage: str) -> float:
        """
        根据生命周期阶段获取基础止损ATR倍数

        Args:
            stage: 生命周期阶段标识

        Returns:
            该阶段对应的ATR倍数
        """
        stage_mult_map = {
            "incubation": cls.DEFAULT_ATR_MULT_INITIAL,
            "acceleration": cls.DEFAULT_ATR_MULT_TRAILING * 0.8,
            "maturity": cls.DEFAULT_ATR_MULT_TRAILING,
            "decline": cls.DEFAULT_ATR_MULT_AGGRESSIVE,
            "termination": cls.DEFAULT_ATR_MULT_AGGRESSIVE * 0.5,
        }
        mult = stage_mult_map.get(stage, cls.DEFAULT_ATR_MULT_TRAILING)
        if stage not in stage_mult_map:
            logger.debug(f"未知生命周期阶段: {stage}，使用默认ATR倍数: {mult}")
        return mult

    @classmethod
    def _calculate_time_decay(cls, stage: str, hold_seconds: float = 0.0) -> float:
        """
        计算连续时间衰减系数

        Args:
            stage: 当前生命周期阶段
            hold_seconds: 持仓已持有秒数

        Returns:
            时间衰减系数 (0.25-1.0)，越低表示止损越紧
        """
        # 终止阶段直接返回最紧衰减
        if stage == "termination":
            return cls.TIME_DECAY_MIN_RATIO

        # 未到衰减起始时间，不衰减
        if hold_seconds <= cls.TIME_DECAY_START_SEC:
            return 1.0

        # 连续衰减区间内
        elapsed = hold_seconds - cls.TIME_DECAY_START_SEC
        total_window = cls.TIME_DECAY_FULL_SEC - cls.TIME_DECAY_START_SEC

        if elapsed >= total_window:
            return cls.TIME_DECAY_MIN_RATIO

        decay_ratio = elapsed / total_window
        return 1.0 - (1.0 - cls.TIME_DECAY_MIN_RATIO) * decay_ratio

    @classmethod
    def _validate_stop_direction(
        cls,
        new_stop: float,
        existing_stop: float,
        direction: int,
    ) -> Tuple[bool, str]:
        """
        校验止损移动方向是否合规

        Args:
            new_stop: 拟设置的新止损价
            existing_stop: 当前已生效的止损价
            direction: 持仓方向 (1=多头, -1=空头)

        Returns:
            (是否有效, 原因说明)
        """
        # 首次设置止损（existing_stop 为初始值），直接放行
        if existing_stop <= 0:
            return True, "首次设置止损"

        if direction == 1:
            if new_stop <= existing_stop:
                return False, f"多头止损不可下移: new={new_stop:.4f} <= existing={existing_stop:.4f}"
            return True, f"多头止损有效上移: {existing_stop:.4f} -> {new_stop:.4f}"
        else:
            if new_stop >= existing_stop:
                return False, f"空头止损不可上移: new={new_stop:.4f} >= existing={existing_stop:.4f}"
            return True, f"空头止损有效下移: {existing_stop:.4f} -> {new_stop:.4f}"
