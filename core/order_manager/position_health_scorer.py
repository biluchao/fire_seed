"""
火种系统 · 持仓健康度评分器 (PositionHealthScorer)

核心职责：
1. 基于浮盈状态、持仓时长、订单簿方向、趋势强度、成交量配合等多维度，计算持仓的综合健康评分（0-100）
2. 提供分阶段健康评估，支持秒级快速评估（加仓决策）与分钟级深度评估（生命周期结构调整）

外部依赖（真实模块接口）：
- 本模块为纯计算单元，所有市场数据通过 evaluate() 方法的参数传入，不直接依赖感知层模块。
- 依赖注入接口（inject_dependencies）为未来扩展预留，当前版本不直接调用注入对象。

接口契约：
- evaluate(position_id: str, profit_atr: float, hold_seconds: float, direction: int,
    obi_direction: Optional[int], pll_frequency: Optional[float],
    volume_cv: Optional[float]) -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 obi_direction/pll_frequency/volume_cv 为 None 时，自动使用类常量中的中性默认值，并在 warnings 中标记
- 任何未预期的内部异常将返回降级评分（50分），并记录完整错误信息，保证调用方安全

资源管理：
- 本模块为纯计算模块，不持有任何外部资源句柄
- 所有中间计算结果在方法返回后自动回收
"""

import logging
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)


class PositionHealthScorer:
    """持仓健康度评分器（六维加权模型）"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 六维权重（总和 = 1.0）
    WEIGHT_PROFIT_STATE = 0.25           # 浮盈状态权重，无量纲，[0.10, 0.40]
    WEIGHT_DURATION = 0.20              # 持仓时长权重，无量纲，[0.10, 0.30]
    WEIGHT_OBI_ALIGNMENT = 0.20         # OBI方向匹配权重，无量纲，[0.10, 0.30]
    WEIGHT_PLL_FREQUENCY = 0.20         # 锁相环频率权重，无量纲，[0.10, 0.30]
    WEIGHT_VOLUME_CONFIRM = 0.15        # 成交量配合权重，无量纲，[0.05, 0.25]

    # 持仓时长最优窗口（秒），用于评分
    OPTIMAL_HOLD_MIN = 60               # 最优持仓起始秒数，取值范围 [30, 120]
    OPTIMAL_HOLD_MAX = 180              # 最优持仓结束秒数，取值范围 [120, 300]

    # 降级默认值（当外部依赖不可用时）
    DEFAULT_OBI_SCORE = 0.5             # OBI 中性得分，无量纲，[0.0, 1.0]
    DEFAULT_PLL_FREQUENCY = 0.0         # PLL 频率降级值，无量纲，[0.0, 0.05]
    DEFAULT_VOLUME_SCORE = 0.5          # 成交量中性得分，无量纲，[0.0, 1.0]

    # 快速评估与深度评估的维度权重调整
    FAST_MODE_DIMENSIONS = ["profit_state", "duration", "obi_alignment"]
    DEEP_MODE_DIMENSIONS = ["profit_state", "duration", "obi_alignment", "pll_frequency", "volume_confirm"]

    # 降级评分（内部异常时使用）
    DEGRADED_HEALTH_SCORE = 50.0        # 降级评分，取值范围 [0, 100]

    def __init__(self):
        # 外部依赖注入（当前版本预留，不直接调用）
        self._visual_cortex = None
        self._multi_band_pll = None
        self._tactile_cortex = None

        logger.info("PositionHealthScorer 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        visual_cortex: Optional[Any] = None,
        multi_band_pll: Optional[Any] = None,
        tactile_cortex: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，当前版本预留，未注入不影响核心计算）

        Args:
            visual_cortex: 视觉皮层，提供OBI数据
            multi_band_pll: 多频段锁相环，提供趋势强度
            tactile_cortex: 触觉皮层，提供成交量特征
        """
        if visual_cortex is not None:
            self._visual_cortex = visual_cortex
            logger.info("VisualCortex 注入成功")
        else:
            logger.debug("VisualCortex 未注入，OBI 维度将通过参数传入")

        if multi_band_pll is not None:
            self._multi_band_pll = multi_band_pll
            logger.info("MultiBandPLL 注入成功")
        else:
            logger.debug("MultiBandPLL 未注入，PLL 维度将通过参数传入")

        if tactile_cortex is not None:
            self._tactile_cortex = tactile_cortex
            logger.info("TactileCortex 注入成功")
        else:
            logger.debug("TactileCortex 未注入，成交量维度将通过参数传入")

    # ========== 公共接口 ==========
    def evaluate(
        self,
        position_id: str,
        profit_atr: float,
        hold_seconds: float,
        direction: int,
        obi_direction: Optional[int] = None,
        pll_frequency: Optional[float] = None,
        volume_cv: Optional[float] = None,
        mode: str = "deep",
    ) -> Dict[str, Any]:
        """
        计算持仓综合健康度评分

        Args:
            position_id: 持仓唯一标识
            profit_atr: 当前浮盈（ATR倍数），正值盈利，负值亏损
            hold_seconds: 已持仓秒数
            direction: 持仓方向 (1=多头, -1=空头)
            obi_direction: OBI方向 (1=买压, -1=卖压)，None时使用默认值
            pll_frequency: 锁相环瞬时频率，None时使用默认值
            volume_cv: 成交量变异系数，None时使用默认值
            mode: 评估模式 ("fast"=秒级快速评估, "deep"=分钟级深度评估)

        Returns:
            标准响应字典，data 中包含 health_score, tier, dimension_scores 等字段
        """
        try:
            if direction not in (1, -1):
                logger.warning(f"无效方向参数 direction={direction}")
                return {
                    "status": "error",
                    "reason": f"无效方向参数: {direction}，有效值为 1 (多头) 或 -1 (空头)",
                    "data": {},
                    "warnings": ["invalid_direction"],
                }

            if hold_seconds < 0:
                logger.warning(f"无效持仓时长: {hold_seconds}s，已置为0")
                hold_seconds = 0.0

            # 根据模式选择激活的维度
            active_dimensions = (
                self.DEEP_MODE_DIMENSIONS if mode == "deep" else self.FAST_MODE_DIMENSIONS
            )

            warnings = []
            dimension_scores = {}

            # ---- 维度一：浮盈状态得分 ----
            dimension_scores["profit_state"] = self._score_profit_state(profit_atr)

            # ---- 维度二：持仓时长得分 ----
            dimension_scores["duration"] = self._score_duration(hold_seconds)

            # ---- 维度三：OBI 方向匹配得分 ----
            obi_score = self._score_obi_alignment(obi_direction, direction)
            dimension_scores["obi_alignment"] = obi_score
            if obi_direction is None:
                warnings.append("OBI 方向数据缺失，使用中性评分")

            # ---- 维度四：锁相环频率得分 ----
            if "pll_frequency" in active_dimensions:
                pll_score = self._score_pll_frequency(pll_frequency)
                dimension_scores["pll_frequency"] = pll_score
                if pll_frequency is None:
                    warnings.append("PLL 频率数据缺失，使用保守评分")
            else:
                dimension_scores["pll_frequency"] = self.DEFAULT_PLL_FREQUENCY

            # ---- 维度五：成交量配合得分 ----
            if "volume_confirm" in active_dimensions:
                volume_score = self._score_volume_confirm(volume_cv)
                dimension_scores["volume_confirm"] = volume_score
                if volume_cv is None:
                    warnings.append("成交量数据缺失，使用中性评分")
            else:
                dimension_scores["volume_confirm"] = self.DEFAULT_VOLUME_SCORE

            # ---- 综合加权计算 ----
            base_weights = {
                "profit_state": self.WEIGHT_PROFIT_STATE,
                "duration": self.WEIGHT_DURATION,
                "obi_alignment": self.WEIGHT_OBI_ALIGNMENT,
                "pll_frequency": self.WEIGHT_PLL_FREQUENCY,
                "volume_confirm": self.WEIGHT_VOLUME_CONFIRM,
            }

            # 计算加权分数（仅使用激活维度）
            if mode == "fast":
                # 快速模式：只使用激活维度，并重新归一化权重
                active_weight_total = sum(base_weights[d] for d in active_dimensions)
                effective_weights = {
                    d: base_weights[d] / active_weight_total
                    for d in active_dimensions
                }
            else:
                effective_weights = base_weights

            health_score = 0.0
            for dim in active_dimensions:
                score = dimension_scores.get(dim, 0.5)
                weight = effective_weights.get(dim, 0.0)
                health_score += score * weight
            health_score = round(min(100.0, max(0.0, health_score * 100.0)), 1)

            # ---- 健康等级判定 ----
            tier = self._get_health_tier(health_score)

            return {
                "status": "ok",
                "reason": f"持仓健康度评分完成，得分={health_score:.1f}，等级={tier}",
                "data": {
                    "position_id": position_id,
                    "health_score": health_score,
                    "tier": tier,
                    "dimension_scores": {k: round(v, 3) for k, v in dimension_scores.items()},
                    "weights": {k: round(v, 3) for k, v in effective_weights.items()},
                    "mode": mode,
                    "direction": direction,
                },
                "warnings": warnings,
            }

        except Exception as e:
            logger.error(f"持仓健康度评估异常: {e} #RECOVERY: 检查输入参数和权重配置")
            return {
                "status": "degraded",
                "reason": f"内部评估异常，返回降级评分: {str(e)}",
                "data": {
                    "position_id": position_id,
                    "health_score": self.DEGRADED_HEALTH_SCORE,
                    "tier": "unknown",
                    "dimension_scores": {},
                    "weights": {},
                    "mode": mode,
                    "direction": direction,
                },
                "warnings": ["internal_error_degraded"],
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
            scorer = cls()
            # 执行一次完整评估
            result = scorer.evaluate(
                position_id="health_check_test",
                profit_atr=1.2,
                hold_seconds=90.0,
                direction=1,
                obi_direction=1,
                pll_frequency=0.025,
                volume_cv=0.5,
                mode="deep",
            )
            if result["status"] != "ok":
                return {
                    "status": "error",
                    "reason": f"评估逻辑异常: {result.get('reason', '未知错误')}",
                    "data": {},
                    "warnings": ["evaluate_test_failed"],
                }

            # 验证分数在有效范围内
            score = result["data"]["health_score"]
            if not (0.0 <= score <= 100.0):
                return {
                    "status": "error",
                    "reason": f"健康评分超出范围: {score}",
                    "data": {},
                    "warnings": ["score_out_of_range"],
                }

            # 验证权重总和接近 1.0
            weights = [
                cls.WEIGHT_PROFIT_STATE,
                cls.WEIGHT_DURATION,
                cls.WEIGHT_OBI_ALIGNMENT,
                cls.WEIGHT_PLL_FREQUENCY,
                cls.WEIGHT_VOLUME_CONFIRM,
            ]
            weight_sum = sum(weights)
            if abs(weight_sum - 1.0) > 0.01:
                return {
                    "status": "error",
                    "reason": f"权重总和不等于1.0: {weight_sum}",
                    "data": {},
                    "warnings": ["weights_not_normalized"],
                }

            # 验证快速模式也能正常工作
            fast_result = scorer.evaluate(
                position_id="health_check_test_fast",
                profit_atr=0.5,
                hold_seconds=30.0,
                direction=-1,
                obi_direction=-1,
                mode="fast",
            )
            if fast_result["status"] != "ok":
                return {
                    "status": "error",
                    "reason": f"快速模式评估异常: {fast_result.get('reason', '未知错误')}",
                    "data": {},
                    "warnings": ["fast_mode_test_failed"],
                }

            return {
                "status": "ok",
                "reason": f"PositionHealthScorer 自检通过，深度评分={score:.1f}，快速评分={fast_result['data']['health_score']:.1f}",
                "data": {},
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查权重配置和评分函数完整性")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    @classmethod
    def _score_profit_state(cls, profit_atr: float) -> float:
        """
        评分维度：浮盈状态 (0.0-1.0)
        - profit_atr > 0：盈利，得分随浮盈增加而上升，上限 1.0
        - profit_atr <= 0：亏损，得分随亏损加深而下降，下限 0.0
        """
        if profit_atr <= 0:
            return max(0.0, 1.0 + profit_atr * 0.5)  # 亏损越多，得分越低
        return min(1.0, profit_atr / 2.0)             # 盈利超2ATR满分

    @classmethod
    def _score_duration(cls, hold_seconds: float) -> float:
        """
        评分维度：持仓时长 (0.0-1.0)
        - 在最优窗口 [OPTIMAL_HOLD_MIN, OPTIMAL_HOLD_MAX] 内得分最高
        - 过短或过长均衰减
        """
        if hold_seconds < cls.OPTIMAL_HOLD_MIN:
            return hold_seconds / cls.OPTIMAL_HOLD_MIN
        if hold_seconds <= cls.OPTIMAL_HOLD_MAX:
            return 1.0
        decay = (hold_seconds - cls.OPTIMAL_HOLD_MAX) / cls.OPTIMAL_HOLD_MAX
        return max(0.0, 1.0 - decay * 0.5)

    @classmethod
    def _score_obi_alignment(cls, obi_direction: Optional[int], position_direction: int) -> float:
        """
        评分维度：OBI方向匹配 (0.0-1.0)
        - OBI 方向与持仓方向一致：高分
        - OBI 方向与持仓方向相反：低分
        - OBI 数据缺失：使用默认中性值
        """
        if obi_direction is None:
            return cls.DEFAULT_OBI_SCORE
        if obi_direction == position_direction:
            return 1.0
        if obi_direction == 0:
            return 0.5
        return 0.0

    @classmethod
    def _score_pll_frequency(cls, frequency: Optional[float]) -> float:
        """
        评分维度：锁相环频率 (0.0-1.0)
        - 频率越高，趋势越强，得分越高
        - 数据缺失：使用保守估计 0.0
        """
        if frequency is None:
            return 0.0
        # 频率范围通常 0.0 - 0.05，0.03以上视为强趋势
        return min(1.0, abs(frequency) / 0.03)

    @classmethod
    def _score_volume_confirm(cls, volume_cv: Optional[float]) -> float:
        """
        评分维度：成交量配合 (0.0-1.0)
        - 变异系数 CV 适中（0.3-0.7）：视为健康成交节奏
        - CV 过低（疑似算法市商主导）或过高（杂乱）：得分降低
        - 数据缺失：使用默认中性值
        """
        if volume_cv is None:
            return cls.DEFAULT_VOLUME_SCORE
        cv = abs(volume_cv)
        if 0.3 <= cv <= 0.7:
            return 1.0
        if cv < 0.3:
            return cv / 0.3
        return max(0.0, 1.0 - (cv - 0.7) / 0.3)

    @classmethod
    def _get_health_tier(cls, score: float) -> str:
        """
        根据评分返回健康等级

        Args:
            score: 健康评分 (0-100)

        Returns:
            健康等级字符串
        """
        if score >= 80:
            return "healthy"
        if score >= 60:
            return "moderate"
        if score >= 40:
            return "unhealthy"
        return "critical"
