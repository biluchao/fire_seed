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
- set_weights(weights: Dict[str, float]) -> None : 动态调整评分权重（由条件权重引擎调用）
- get_weights() -> Dict[str, float] : 获取当前生效的权重配置
- health_check() -> Dict[str, Any]
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 obi_direction/pll_frequency/volume_cv 为 None 时，自动使用类常量中的中性默认值，并在 warnings 中标记
- 当权重总和偏差超过 1% 时，拒绝本次调整，保留原有权重
- 任何未预期的内部异常将返回降级评分（50分），并记录完整错误信息，保证调用方安全

资源管理：
- 本模块为纯计算模块，不持有任何外部资源句柄
- 所有中间计算结果在方法返回后自动回收
"""

import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class PositionHealthScorer:
    """持仓健康度评分器（六维加权模型）"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 六维权重（降级默认值，运行时可通过 set_weights 动态覆盖）
    DEFAULT_WEIGHT_PROFIT_STATE = 0.25        # 浮盈状态权重，无量纲，[0.10, 0.40]
    DEFAULT_WEIGHT_DURATION = 0.20           # 持仓时长权重，无量纲，[0.10, 0.30]
    DEFAULT_WEIGHT_OBI_ALIGNMENT = 0.20      # OBI方向匹配权重，无量纲，[0.10, 0.30]
    DEFAULT_WEIGHT_PLL_FREQUENCY = 0.20      # 锁相环频率权重，无量纲，[0.10, 0.30]
    DEFAULT_WEIGHT_VOLUME_CONFIRM = 0.15     # 成交量配合权重，无量纲，[0.05, 0.25]

    # 持仓时长最优窗口（秒），用于评分
    OPTIMAL_HOLD_MIN = 60                    # 最优持仓起始秒数，取值范围 [30, 120]
    OPTIMAL_HOLD_MAX = 180                   # 最优持仓结束秒数，取值范围 [120, 300]

    # 评分函数参数
    PLL_FULL_SCORE_FREQUENCY = 0.02          # PLL 频率满分阈值，无量纲，[0.015, 0.04]
    OBI_REVERSE_TOLERANCE_SCORE = 0.3        # OBI 反向时的过渡分数，无量纲，[0.1, 0.5]

    # 降级默认值（当外部依赖不可用时）
    DEFAULT_OBI_SCORE = 0.5                  # OBI 中性得分，无量纲，[0.0, 1.0]
    DEFAULT_PLL_FREQUENCY = 0.0              # PLL 频率降级值，无量纲，[0.0, 0.05]
    DEFAULT_VOLUME_SCORE = 0.5               # 成交量中性得分，无量纲，[0.0, 1.0]

    # 快速评估与深度评估的维度权重调整
    FAST_MODE_DIMENSIONS = ["profit_state", "duration", "obi_alignment"]
    DEEP_MODE_DIMENSIONS = ["profit_state", "duration", "obi_alignment", "pll_frequency", "volume_confirm"]

    # 降级评分（内部异常时使用）
    DEGRADED_HEALTH_SCORE = 50.0             # 降级评分，取值范围 [0, 100]

    # 权重总和允许的最大偏差
    WEIGHT_TOLERANCE = 0.01                  # 权重偏差容忍度，无量纲，[0.001, 0.05]

    def __init__(self):
        # 动态权重（初始化为默认值，可通过 set_weights 覆盖）
        self._weights: Dict[str, float] = {
            "profit_state": self.DEFAULT_WEIGHT_PROFIT_STATE,
            "duration": self.DEFAULT_WEIGHT_DURATION,
            "obi_alignment": self.DEFAULT_WEIGHT_OBI_ALIGNMENT,
            "pll_frequency": self.DEFAULT_WEIGHT_PLL_FREQUENCY,
            "volume_confirm": self.DEFAULT_WEIGHT_VOLUME_CONFIRM,
        }

        # 外部依赖注入（当前版本预留，不直接调用）
        self._visual_cortex = None
        self._multi_band_pll = None
        self._tactile_cortex = None

        logger.info("PositionHealthScorer 初始化完成，默认权重已加载")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        visual_cortex: Optional[Any] = None,
        multi_band_pll: Optional[Any] = None,
        tactile_cortex: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选，当前版本预留）"""
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
    def set_weights(self, weights: Dict[str, float]) -> Dict[str, Any]:
        """动态调整评分权重（由条件权重引擎调用）"""
        total = sum(weights.values())
        if abs(total - 1.0) > self.WEIGHT_TOLERANCE:
            logger.warning(f"权重总和不等于1.0: {total}，忽略本次调整")
            return {
                "status": "error",
                "reason": f"权重总和不等于1.0: {total}",
                "data": {},
                "warnings": ["weights_not_normalized"],
            }

        updated = {}
        for k in self._weights:
            if k in weights:
                self._weights[k] = weights[k]
                updated[k] = weights[k]
        logger.info(f"持仓健康度权重已更新: {updated}")
        return {
            "status": "ok",
            "reason": f"权重已更新: {updated}",
            "data": {"new_weights": self._weights.copy()},
            "warnings": [],
        }

    def get_weights(self) -> Dict[str, float]:
        """获取当前生效的权重配置"""
        return self._weights.copy()

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
        """计算持仓综合健康度评分"""
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

            active_dimensions = (
                self.DEEP_MODE_DIMENSIONS if mode == "deep" else self.FAST_MODE_DIMENSIONS
            )

            warnings = []
            dimension_scores = {}

            # ---- 维度一：浮盈状态得分 (允许负分以区分深套) ----
            dimension_scores["profit_state"] = self._score_profit_state(profit_atr)

            # ---- 维度二：持仓时长得分 (超时加速衰减) ----
            dimension_scores["duration"] = self._score_duration(hold_seconds)

            # ---- 维度三：OBI 方向匹配得分 (反向容忍度) ----
            obi_score = self._score_obi_alignment(obi_direction, direction)
            dimension_scores["obi_alignment"] = obi_score
            if obi_direction is None:
                warnings.append("OBI 方向数据缺失，使用中性评分")

            # ---- 维度四：锁相环频率得分 (灵敏度提升) ----
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
            if mode == "fast":
                active_weight_total = sum(self._weights[d] for d in active_dimensions)
                if active_weight_total > 0:
                    effective_weights = {
                        d: self._weights[d] / active_weight_total for d in active_dimensions
                    }
                else:
                    effective_weights = {d: 0.0 for d in active_dimensions}
            else:
                effective_weights = {
                    d: self._weights[d] for d in active_dimensions
                }

            health_score = 0.0
            for dim in active_dimensions:
                score = dimension_scores.get(dim, 0.5)
                weight = effective_weights.get(dim, 0.0)
                health_score += score * weight
            # 映射到 0-100 区间，负分自然截断为 0
            health_score = round(max(0.0, min(100.0, health_score * 100.0)), 1)

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
        """模块自检"""
        try:
            scorer = cls()
            # 验证默认权重和
            weight_sum = sum(scorer._weights.values())
            if abs(weight_sum - 1.0) > 0.01:
                return {"status": "error", "reason": f"默认权重总和不等于1.0: {weight_sum}", "data": {}, "warnings": ["weights_not_normalized"]}

            # 默认权重下深度评估
            result = scorer.evaluate(position_id="test", profit_atr=1.2, hold_seconds=90.0, direction=1, obi_direction=1, pll_frequency=0.025, volume_cv=0.5, mode="deep")
            if result["status"] != "ok":
                return {"status": "error", "reason": f"深度评估异常: {result.get('reason')}", "data": {}, "warnings": ["deep_evaluate_failed"]}
            if not (0.0 <= result["data"]["health_score"] <= 100.0):
                return {"status": "error", "reason": "评分超范围", "data": {}, "warnings": ["score_out_of_range"]}

            # 快速评估
            fast_result = scorer.evaluate(position_id="test_fast", profit_atr=0.5, hold_seconds=30.0, direction=-1, obi_direction=-1, mode="fast")
            if fast_result["status"] != "ok":
                return {"status": "error", "reason": f"快速评估异常: {fast_result.get('reason')}", "data": {}, "warnings": ["fast_evaluate_failed"]}

            # 权重更新与拒绝测试
            set_ok = scorer.set_weights({"profit_state": 0.5, "duration": 0.3, "obi_alignment": 0.1, "pll_frequency": 0.05, "volume_confirm": 0.05})
            if set_ok["status"] != "ok":
                return {"status": "error", "reason": "合法权重更新失败", "data": {}, "warnings": ["set_weights_failed"]}
            set_fail = scorer.set_weights({"profit_state": 0.8, "duration": 0.5})
            if set_fail["status"] != "error":
                return {"status": "error", "reason": "非法权重未被拒绝", "data": {}, "warnings": ["weight_validation_failed"]}

            # 动态权重生效测试
            dyn_result = scorer.evaluate(position_id="dyn_test", profit_atr=1.2, hold_seconds=90.0, direction=1, obi_direction=1, pll_frequency=0.025, volume_cv=0.5, mode="deep")
            if dyn_result["status"] != "ok":
                return {"status": "error", "reason": f"动态权重评估异常: {dyn_result.get('reason')}", "data": {}, "warnings": ["dynamic_evaluate_failed"]}

            return {"status": "ok", "reason": f"所有测试通过，深度评分={result['data']['health_score']:.1f}", "data": {}, "warnings": []}
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查权重配置和评分函数完整性")
            return {"status": "error", "reason": f"健康检查异常: {str(e)}", "data": {}, "warnings": [f"health_check_failed: {str(e)}"]}

    # ========== 私有方法 ==========
    @classmethod
    def _score_profit_state(cls, profit_atr: float) -> float:
        """浮盈状态得分（允许负分以区分深套程度）"""
        if profit_atr <= 0:
            return max(-2.0, 1.0 + profit_atr * 0.5)
        return min(1.0, profit_atr / 2.0)

    @classmethod
    def _score_duration(cls, hold_seconds: float) -> float:
        """持仓时长得分（超时加速衰减）"""
        if hold_seconds < cls.OPTIMAL_HOLD_MIN:
            return hold_seconds / cls.OPTIMAL_HOLD_MIN
        if hold_seconds <= cls.OPTIMAL_HOLD_MAX:
            return 1.0
        decay = (hold_seconds - cls.OPTIMAL_HOLD_MAX) / (cls.OPTIMAL_HOLD_MAX * 2.0)
        return max(0.0, 1.0 - decay)

    @classmethod
    def _score_obi_alignment(cls, obi_direction: Optional[int], position_direction: int) -> float:
        """OBI方向匹配得分（反向容忍度）"""
        if obi_direction is None:
            return cls.DEFAULT_OBI_SCORE
        if obi_direction == position_direction:
            return 1.0
        if obi_direction == 0:
            return 0.5
        return cls.OBI_REVERSE_TOLERANCE_SCORE

    @classmethod
    def _score_pll_frequency(cls, frequency: Optional[float]) -> float:
        """锁相环频率得分（灵敏度提升）"""
        if frequency is None:
            return 0.0
        return min(1.0, abs(frequency) / cls.PLL_FULL_SCORE_FREQUENCY)

    @classmethod
    def _score_volume_confirm(cls, volume_cv: Optional[float]) -> float:
        """成交量配合得分"""
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
        """健康等级判定"""
        if score >= 80:
            return "healthy"
        if score >= 60:
            return "moderate"
        if score >= 40:
            return "unhealthy"
        return "critical"
