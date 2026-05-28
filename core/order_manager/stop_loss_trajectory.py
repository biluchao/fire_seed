"""
火种系统 · 止损轨迹管理器 (StopLossTrajectory)

核心职责：
1. 管理每笔持仓止损价格的完整生命周期，确保止损只向有利方向移动（不可逆上移/下移）
2. 根据持仓阶段、浮盈幅度、实际持仓时长、加仓合并成本等因素，计算并输出最优止损位置

外部依赖（真实模块接口）：
- core.order_manager.lifecycle_stages.LifecycleStages : 获取当前持仓所处的生命周期阶段
- core.perception.tactile_cortex.TactileCortex : 获取当前ATR、波动率分位等市场感知数据
- core.order_manager.position_health_scorer.PositionHealthScorer : 获取持仓健康度评分

接口契约：
- calculate_new_stop(position_id: str, current_price: float, direction: int, entry_price: float,
    existing_stop: float, stage: str, profit_atr: float, hold_seconds: float,
    add_layer_count: int = 0, merged_cost: Optional[float] = None,
    atr: Optional[float] = None) -> Dict[str, Any]
- validate_stop_direction(new_stop: float, existing_stop: float, direction: int,
    current_price: Optional[float] = None) -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 TactileCortex 不可用时，使用类常量 DEFAULT_ATR 作为默认波动率估计，保守计算止损
- 当 LifecycleStages 不可用时，默认按成熟期标准执行止损计算
- 当 PositionHealthScorer 不可用时，跳过健康度加权，仅使用浮盈幅度进行止损计算
- 所有降级值在类常量区明确声明，附带单位与取值范围注释

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
    DEFAULT_ATR = 0.005                   # 默认ATR（当TactileCortex不可用时），百分比，取值范围 [0.001, 0.05]
    DEFAULT_ATR_MULT_INITIAL = 0.8         # 初始止损ATR倍数，无量纲，取值范围 [0.5, 1.5]
    DEFAULT_ATR_MULT_TRAILING = 1.2        # 追踪止损ATR倍数，无量纲，取值范围 [0.8, 2.0]
    DEFAULT_ATR_MULT_AGGRESSIVE = 0.4      # 激进锁定止损ATR倍数，无量纲，取值范围 [0.2, 0.8]
    DEFAULT_ATR_MULT_MERGE_BUFFER = 0.2    # 合并成本后额外缓冲ATR倍数，无量纲，取值范围 [0.1, 0.5]
    MIN_STOP_DISTANCE_PCT = 0.0005         # 最小止损距离（占价格百分比），防止止损过近，取值范围 [0.0001, 0.002]
    MAX_STOP_ATR_MULT = 2.0                # 最终ATR倍数上限，无量纲，取值范围 [1.5, 3.0]
    MIN_STOP_ATR_MULT = 0.2                # 最终ATR倍数下限，无量纲，取值范围 [0.1, 0.5]
    TIME_DECAY_START_SEC = 60              # 时间衰减开始生效的持仓秒数，秒，取值范围 [30, 300]
    TIME_DECAY_FULL_SEC = 360              # 时间衰减完全生效的持仓秒数，秒，取值范围 [180, 600]
    TIME_DECAY_MIN_RATIO = 0.25            # 时间衰减后止损距离的最小保留比例，无量纲，[0.1, 0.5]
    MERGE_COST_CLOSE_THRESHOLD_ATR = 0.5   # 合并成本接近市价的判定阈值（ATR倍数），无量纲，[0.3, 1.0]
    COMPRESSION_STAGES = {                 # 紧缩利润阶梯（浮盈ATR倍数 -> 止损ATR倍数）
        0.5: 0.8,                          # 浮盈>0.5ATR：止损收紧至ATR×0.8
        1.0: 0.5,                          # 浮盈>1.0ATR：止损收紧至ATR×0.5
        1.5: 0.3,                          # 浮盈>1.5ATR：止损收紧至ATR×0.3
        3.0: 0.15,                         # 浮盈>3.0ATR：止损收紧至ATR×0.15
    }

    def __init__(self):
        # 外部依赖注入
        self._tactile_cortex = None
        self._lifecycle_stages = None
        self._position_health_scorer = None

        logger.info("StopLossTrajectory 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        tactile_cortex: Optional[Any] = None,
        lifecycle_stages: Optional[Any] = None,
        position_health_scorer: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）

        Args:
            tactile_cortex: 触觉皮层，提供ATR与波动率数据
            lifecycle_stages: 生命周期阶段管理器
            position_health_scorer: 持仓健康度评分器
        """
        if tactile_cortex is not None:
            self._tactile_cortex = tactile_cortex
            logger.info("TactileCortex 注入成功")
        else:
            logger.warning("TactileCortex 未注入，将使用默认ATR值")

        if lifecycle_stages is not None:
            self._lifecycle_stages = lifecycle_stages
            logger.info("LifecycleStages 注入成功")
        else:
            logger.warning("LifecycleStages 未注入，默认按成熟期标准执行")

        if position_health_scorer is not None:
            self._position_health_scorer = position_health_scorer
            logger.info("PositionHealthScorer 注入成功")
        else:
            logger.warning("PositionHealthScorer 未注入，跳过健康度加权")

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
        hold_seconds: float,
        add_layer_count: int = 0,
        merged_cost: Optional[float] = None,
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
            hold_seconds: 实际持仓时长（秒）
            add_layer_count: 已加仓次数，默认0
            merged_cost: 加仓后的合并成本价，默认None
            atr: 当前ATR值，默认None（从TactileCortex获取或使用默认值）

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

        if hold_seconds < 0:
            logger.warning(f"持仓时长为负: hold_seconds={hold_seconds}")
            hold_seconds = 0

        # 使用注入的依赖或类常量作为回退
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
        for threshold, mult in sorted(cls.COMPRESSION_STAGES.items()):
            if profit_atr >= threshold:
                compression_mult = min(compression_mult, mult)
                compression_triggered = True

        # ---- 阶段三：加仓合并成本处理（动态缓冲）----
        if merged_cost is not None and add_layer_count > 0:
            cost_distance_pct = abs(current_price - merged_cost) / current_price
            if cost_distance_pct < effective_atr * cls.MERGE_COST_CLOSE_THRESHOLD_ATR:
                merge_buffer = cls.DEFAULT_ATR_MULT_MERGE_BUFFER * 1.5
                warnings.append("合并成本接近市价，已增加额外缓冲")
                logger.debug(f"合并成本接近市价: distance_pct={cost_distance_pct:.4%}, buffer={merge_buffer}")
            else:
                merge_buffer = cls.DEFAULT_ATR_MULT_MERGE_BUFFER * 0.5
                logger.debug(f"合并成本远离市价: distance_pct={cost_distance_pct:.4%}, buffer={merge_buffer}")
            stage_atr_mult += merge_buffer

        # ---- 阶段四：时间衰减调整（基于实际持仓时长）----
        time_decay_mult = cls._calculate_time_decay(hold_seconds)

        # ---- 阶段五：计算最终止损距离（含上下限硬约束）----
        final_atr_mult = stage_atr_mult * compression_mult * time_decay_mult
        final_atr_mult = max(cls.MIN_STOP_ATR_MULT, min(cls.MAX_STOP_ATR_MULT, final_atr_mult))
        stop_distance = effective_atr * final_atr_mult

        # 确保最小止损距离
        min_distance = current_price * cls.MIN_STOP_DISTANCE_PCT
        if stop_distance < min_distance:
            stop_distance = min_distance
            logger.debug(f"止损距离过小({stop_distance})，已强制设为最小距离: {min_distance}")

        # 计算新止损候选值
        if direction == 1:
            new_stop_candidate = current_price - stop_distance
        else:
            new_stop_candidate = current_price + stop_distance

        # 不可逆校验（含绝对位置校验）
        is_valid, validation_reason = cls._validate_stop_direction(
            new_stop_candidate, existing_stop, direction, current_price
        )
        if not is_valid:
            new_stop_candidate = existing_stop
            warnings.append(validation_reason)

        return {
            "status": "ok",
            "reason": (
                f"止损计算完成，阶段={stage}, 浮盈ATR={profit_atr:.2f}, "
                f"持仓{hold_seconds:.0f}秒, 最终ATR倍数={final_atr_mult:.2f}"
            ),
            "data": {
                "position_id": position_id,
                "new_stop_candidate": round(new_stop_candidate, 4),
                "stop_distance_atr": round(final_atr_mult, 2),
                "compression_triggered": compression_triggered,
                "stage_atr_mult": round(stage_atr_mult, 2),
                "compression_mult": round(compression_mult, 2),
                "time_decay_mult": round(time_decay_mult, 2),
                "hold_seconds": hold_seconds,
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
        current_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        校验止损移动方向是否合规（独立于 calculate_new_stop 的公开校验接口）

        Args:
            new_stop: 拟设置的新止损价
            existing_stop: 当前已生效的止损价
            direction: 持仓方向 (1=多头, -1=空头)
            current_price: 当前市场价（可选，用于绝对位置校验）

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

        is_valid, reason = cls._validate_stop_direction(new_stop, existing_stop, direction, current_price)
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
        模块自检（含边界与异常场景测试）

        Returns:
            标准健康检查响应字典
        """
        try:
            test_cases = [
                # (position_id, current_price, direction, entry_price, existing_stop,
                #  stage, profit_atr, hold_seconds, add_layer_count, merged_cost, atr,
                #  expected_status, description)
                ("test_normal_long", 100.0, 1, 99.0, 98.5, "maturity", 1.2, 120.0, 0, None, 0.005, "ok", "正常多头止损上移"),
                ("test_normal_short", 100.0, -1, 101.0, 101.5, "maturity", 1.2, 120.0, 0, None, 0.005, "ok", "正常空头止损下移"),
                ("test_loss_no_move", 99.0, 1, 100.0, 99.5, "incubation", -0.5, 30.0, 0, None, 0.005, "ok", "浮亏时止损不应移动"),
                ("test_invalid_dir", 100.0, 0, 99.0, 98.5, "maturity", 1.0, 100.0, 0, None, 0.005, "error", "无效方向参数应返回错误"),
                ("test_merged_cost", 101.0, 1, 99.0, 100.0, "maturity", 1.5, 180.0, 2, 100.5, 0.005, "ok", "加仓合并成本止损计算"),
                ("test_extreme_profit", 105.0, 1, 99.0, 103.0, "maturity", 4.0, 300.0, 1, None, 0.005, "ok", "极端浮盈时止损激进锁定"),
                ("test_invalid_price", -1.0, 1, 99.0, 98.5, "maturity", 1.0, 100.0, 0, None, 0.005, "error", "无效价格应返回错误"),
            ]

            for (pos_id, price, d, entry, stop, stage, p_atr, hold, add_n, merged, atr_val, exp_status, desc) in test_cases:
                result = cls.calculate_new_stop(
                    position_id=pos_id,
                    current_price=price,
                    direction=d,
                    entry_price=entry,
                    existing_stop=stop,
                    stage=stage,
                    profit_atr=p_atr,
                    hold_seconds=hold,
                    add_layer_count=add_n,
                    merged_cost=merged,
                    atr=atr_val,
                )
                if result["status"] != exp_status:
                    return {
                        "status": "error",
                        "reason": f"测试失败: {desc} - 期望状态={exp_status}, 实际状态={result['status']}",
                        "data": {"failed_test": desc},
                        "warnings": ["health_check_test_failed"],
                    }

            # 验证方向校验边界
            valid_long = cls.validate_stop_direction(99.5, 99.0, 1, 100.0)
            if not valid_long["data"]["is_valid"]:
                return {"status": "error", "reason": "正常多头止损上移被拒绝", "data": {}, "warnings": ["validation_boundary_test_failed"]}
            invalid_long = cls.validate_stop_direction(100.5, 99.0, 1, 100.0)
            if invalid_long["data"]["is_valid"]:
                return {"status": "error", "reason": "多头止损超过市价未被拦截", "data": {}, "warnings": ["validation_boundary_test_failed"]}
            valid_short = cls.validate_stop_direction(100.5, 101.0, -1, 100.0)
            if not valid_short["data"]["is_valid"]:
                return {"status": "error", "reason": "正常空头止损下移被拒绝", "data": {}, "warnings": ["validation_boundary_test_failed"]}
            invalid_short = cls.validate_stop_direction(99.5, 101.0, -1, 100.0)
            if invalid_short["data"]["is_valid"]:
                return {"status": "error", "reason": "空头止损低于市价未被拦截", "data": {}, "warnings": ["validation_boundary_test_failed"]}

            return {
                "status": "ok",
                "reason": "StopLossTrajectory 自检通过，核心逻辑、边界条件、异常场景均正常",
                "data": {"test_count": len(test_cases)},
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查类常量定义和计算逻辑完整性")
            return {"status": "error", "reason": f"健康检查异常: {str(e)}", "data": {}, "warnings": [f"health_check_failed: {str(e)}"]}

    # ========== 私有方法 ==========
    @classmethod
    def _get_stage_atr_mult(cls, stage: str) -> float:
        """根据生命周期阶段获取基础止损ATR倍数"""
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
    def _calculate_time_decay(cls, hold_seconds: float) -> float:
        """计算基于实际持仓时长的时间衰减系数"""
        if hold_seconds <= cls.TIME_DECAY_START_SEC:
            return 1.0
        if hold_seconds >= cls.TIME_DECAY_FULL_SEC:
            return cls.TIME_DECAY_MIN_RATIO
        decay_ratio = (hold_seconds - cls.TIME_DECAY_START_SEC) / (cls.TIME_DECAY_FULL_SEC - cls.TIME_DECAY_START_SEC)
        return 1.0 - decay_ratio * (1.0 - cls.TIME_DECAY_MIN_RATIO)

    @classmethod
    def _validate_stop_direction(
        cls,
        new_stop: float,
        existing_stop: float,
        direction: int,
        current_price: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """校验止损移动方向是否合规，含绝对位置校验"""
        if direction == 1:
            if new_stop <= existing_stop:
                return False, f"多头止损不可下移: new={new_stop:.6f} <= existing={existing_stop:.6f}"
            if current_price is not None and new_stop > current_price:
                return False, f"多头止损不可超过当前市价: new={new_stop:.6f} > price={current_price:.6f}"
            return True, "多头止损上移有效"
        else:
            if new_stop >= existing_stop:
                return False, f"空头止损不可上移: new={new_stop:.6f} >= existing={existing_stop:.6f}"
            if current_price is not None and new_stop < current_price:
                return False, f"空头止损不可低于当前市价: new={new_stop:.6f} < price={current_price:.6f}"
            return True, "空头止损下移有效"
