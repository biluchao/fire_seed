"""
火种系统 · 信号质量预评估器 (SignalQualityPrecheck)

核心职责：
1. 对即将进入协商总线的交易信号进行四维质量评估（纯度、稀有性、时效性、共振度）
2. 根据综合评分输出质量等级（A/B/C）以及对应的仓位系数与执行优先级

外部依赖（真实模块接口）：
- core.perception.gustatory_cortex.GustatoryCortex : 获取历史市场状态相似度与经验检索（用于稀有性评估）
- core.multi_tf_arbiter_v2.MultiTfArbiter : 获取跨周期与跨品种共振信息（用于共振度评估）
- core.behavioral_logger.BehavioralLogger : 记录质量评估日志与异常事件

接口契约：
- evaluate(signal_context: Dict[str, Any]) -> Dict[str, Any] : 评估信号质量，返回质量等级、仓位系数、执行优先级及评估明细
- health_check() -> Dict[str, Any] : 模块自检，验证所有维度的计算逻辑与降级路径
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])
- 错误返回固定包含 "error_code" (str)

异常与降级：
- 当 GustatoryCortex 不可用时，稀有性维度使用中性默认值 0.5，并标记 "degraded" 状态
- 当 MultiTfArbiter 不可用时，共振度维度使用默认值 0.0，并标记 "degraded" 状态
- 当子维度数据不可用时，纯度维度使用历史经验默认值 0.6，并标记 "degraded" 状态
- 输入参数校验失败时返回错误状态，附带 error_code，不抛异常
- 所有降级值在类常量区明确声明

资源管理：
- 本模块为无状态计算模块，不持有任何外部资源句柄，所有计算结果即时返回
"""

import time
import logging
import math
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class SignalQualityPrecheck:
    """信号质量预评估器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 四维评估权重，运行时强制校验总和为 1.0
    PURITY_WEIGHT = 0.30                # 纯度权重，无量纲，[0.1, 0.5]
    RARITY_WEIGHT = 0.25                # 稀有性权重，无量纲，[0.1, 0.4]
    TIMELINESS_WEIGHT = 0.25            # 时效性权重，无量纲，[0.1, 0.4]
    RESONANCE_WEIGHT = 0.20             # 共振度权重，无量纲，[0.1, 0.4]
    WEIGHTS_EXPECTED_SUM = 1.0          # 权重期望总和

    # 质量等级阈值
    A_GRADE_MIN_SCORE = 0.80            # A 级最低综合评分，无量纲，[0.7, 0.9]
    B_GRADE_MIN_SCORE = 0.60            # B 级最低综合评分，无量纲，[0.5, 0.8]

    # 时效性参数
    OPTIMAL_WINDOW_SECONDS = 3          # 最佳信号窗口（秒），[1.0, 10.0]
    DECAY_HALFLIFE_SECONDS = 15         # 时效性衰减半衰期（秒），[5.0, 60.0]
    MIN_TIMELINESS = 0.1                # 最低时效性得分，无量纲，[0.0, 0.3]

    # 纯度计算参数
    DIRECTION_DELTA = 0.05              # 方向判定缓冲区，无量纲，[0.01, 0.10]

    # 降级默认值（当外部依赖不可用时使用）
    DEGRADED_RARITY_SCORE = 0.5         # 默认稀有性评分，无量纲，[0.0, 1.0]
    DEGRADED_RESONANCE_SCORE = 0.0      # 默认共振度评分，无量纲，[0.0, 1.0]
    DEGRADED_PURITY_SCORE = 0.6         # 默认纯度评分，无量纲，[0.0, 1.0]

    # 仓位系数映射（相对基准仓位比例）
    A_GRADE_POSITION_MULT = 1.0         # A 级仓位系数，无量纲，[0.8, 1.2]
    B_GRADE_POSITION_MULT = 0.8         # B 级仓位系数，无量纲，[0.5, 1.0]
    C_GRADE_POSITION_MULT = 0.5         # C 级仓位系数，无量纲，[0.2, 0.7]

    # 执行优先级映射
    A_GRADE_EXEC_PRIORITY = "highest"
    B_GRADE_EXEC_PRIORITY = "normal"
    C_GRADE_EXEC_PRIORITY = "conservative"

    def __init__(self):
        # 外部依赖（注入后只读，无需锁保护）
        self._gustatory_cortex = None
        self._multi_tf_arbiter = None
        self._signal_funnel = None
        self._behavioral_logger = None

        # 运行时权重校验
        total_weight = (self.PURITY_WEIGHT + self.RARITY_WEIGHT +
                        self.TIMELINESS_WEIGHT + self.RESONANCE_WEIGHT)
        if abs(total_weight - self.WEIGHTS_EXPECTED_SUM) > 0.001:
            logger.error(f"四维权重总和 {total_weight} 不等于 1.0，请检查配置 #RECOVERY: 检查类常量定义")
        else:
            logger.info("四维权重总和校验通过 (%.4f)", total_weight)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        gustatory_cortex: Optional[Any] = None,
        multi_tf_arbiter: Optional[Any] = None,
        signal_funnel: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选注入，未注入时对应功能降级）"""
        if gustatory_cortex is not None:
            self._gustatory_cortex = gustatory_cortex
            logger.info("GustatoryCortex 注入成功")
        else:
            logger.warning("GustatoryCortex 未注入，稀有性评估降级")

        if multi_tf_arbiter is not None:
            if not hasattr(multi_tf_arbiter, 'get_resonance_info'):
                logger.warning("MultiTfArbiter 缺少 get_resonance_info 方法，共振评估降级")
                self._multi_tf_arbiter = None
            else:
                self._multi_tf_arbiter = multi_tf_arbiter
                logger.info("MultiTfArbiter 注入成功")

        if signal_funnel is not None:
            self._signal_funnel = signal_funnel
            logger.info("SignalFunnel 注入成功")
        else:
            logger.warning("SignalFunnel 未注入，纯度评估降级")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

    # ========== 公共接口 ==========
    def evaluate(self, signal_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        对信号进行四维质量评估。输入参数严格校验，输出标准化字典。
        """
        # 1. 必填字段检查
        required_fields = ["score", "direction", "timestamp", "sub_dimensions", "symbol"]
        missing_fields = [f for f in required_fields if f not in signal_context]
        if missing_fields:
            return {
                "status": "error",
                "reason": f"缺少必要字段: {missing_fields}",
                "error_code": "MISSING_FIELDS",
                "data": {},
                "warnings": missing_fields,
            }

        # 2. 类型与合法性检查
        try:
            score = float(signal_context["score"])
            direction = int(signal_context["direction"])
            symbol = str(signal_context["symbol"])
            trigger_time = float(signal_context["timestamp"])
            sub_dimensions = signal_context.get("sub_dimensions", {})
        except (ValueError, TypeError) as e:
            return {
                "status": "error",
                "reason": f"字段类型错误: {e}",
                "error_code": "TYPE_ERROR",
                "data": {},
                "warnings": [str(e)],
            }

        # 3. NaN检查 (score)
        if math.isnan(score):
            return {
                "status": "error",
                "reason": "score 为 NaN",
                "error_code": "NAN_VALUE",
                "data": {},
                "warnings": ["score_is_nan"],
            }

        # 4. 评分范围钳位
        if not 0.0 <= score <= 100.0:
            logger.warning(f"评分超出范围: {score}，已钳位")
            score = max(0.0, min(100.0, score))

        # 5. 方向有效性校验
        raw_direction = direction
        if direction not in (1, -1):
            logger.warning(f"无效信号方向: {direction}，修正为1")
            direction = 1

        # 6. symbol非空检查
        if not symbol.strip():
            return {
                "status": "error",
                "reason": "symbol 为空字符串",
                "error_code": "INVALID_SYMBOL",
                "data": {},
                "warnings": ["empty_symbol"],
            }

        # 7. sub_dimensions 类型检查
        if not isinstance(sub_dimensions, dict):
            logger.warning("sub_dimensions 非字典类型，已重置为空")
            sub_dimensions = {}

        warnings = []
        dimension_issues = {}

        # 8. 纯度评估
        purity = self._calculate_purity(sub_dimensions, direction)
        if purity == self.DEGRADED_PURITY_SCORE:
            warnings.append("纯度评估降级")
            dimension_issues["purity"] = "degraded"

        # 9. 稀有性评估
        rarity = self._calculate_rarity(score, symbol)
        if rarity == self.DEGRADED_RARITY_SCORE:
            warnings.append("稀有性评估降级")
            dimension_issues["rarity"] = "degraded"

        # 10. 时效性评估
        timeliness = self._calculate_timeliness(trigger_time)

        # 11. 共振度评估
        resonance = self._calculate_resonance(signal_context)
        if resonance == self.DEGRADED_RESONANCE_SCORE:
            warnings.append("共振度评估降级")
            dimension_issues["resonance"] = "degraded"

        # 12. 各维度NaN检查 (防御性编程)
        for dim_name, dim_score in [("purity", purity), ("rarity", rarity),
                                    ("timeliness", timeliness), ("resonance", resonance)]:
            if math.isnan(dim_score):
                logger.error(f"维度 {dim_name} 得分为 NaN，已替换为降级默认值")
                dim_score = 0.5  # 中性值
                warnings.append(f"{dim_name}_nan_replaced")
            dim_score = max(0.0, min(1.0, dim_score))

        # 13. 加权综合评分
        composite_score = (
            self.PURITY_WEIGHT * purity +
            self.RARITY_WEIGHT * rarity +
            self.TIMELINESS_WEIGHT * timeliness +
            self.RESONANCE_WEIGHT * resonance
        )
        composite_score = max(0.0, min(1.0, composite_score))

        # 14. 质量等级判定
        if composite_score >= self.A_GRADE_MIN_SCORE:
            quality_tier = "A"
            position_multiplier = self.A_GRADE_POSITION_MULT
            execution_priority = self.A_GRADE_EXEC_PRIORITY
        elif composite_score >= self.B_GRADE_MIN_SCORE:
            quality_tier = "B"
            position_multiplier = self.B_GRADE_POSITION_MULT
            execution_priority = self.B_GRADE_EXEC_PRIORITY
        else:
            quality_tier = "C"
            position_multiplier = self.C_GRADE_POSITION_MULT
            execution_priority = self.C_GRADE_EXEC_PRIORITY

        # 15. 审计日志记录 (包含原始异常方向)
        audit_context = {
            "symbol": symbol,
            "original_direction": raw_direction,
            "corrected_direction": direction if raw_direction != direction else direction,
            "score": score,
            "tier": quality_tier,
            "composite_score": round(composite_score, 4),
            "dimensions": {
                "purity": round(purity, 4),
                "rarity": round(rarity, 4),
                "timeliness": round(timeliness, 4),
                "resonance": round(resonance, 4),
            },
            "warnings": warnings,
        }
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event("signal_quality_precheck", audit_context)
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

        # 16. 构建返回数据
        result_data = {
            "quality_tier": quality_tier,
            "composite_score": round(composite_score, 4),
            "position_multiplier": position_multiplier,
            "execution_priority": execution_priority,
            "dimensions": {
                "purity": {"score": round(purity, 4), "weight": self.PURITY_WEIGHT},
                "rarity": {"score": round(rarity, 4), "weight": self.RARITY_WEIGHT},
                "timeliness": {"score": round(timeliness, 4), "weight": self.TIMELINESS_WEIGHT},
                "resonance": {"score": round(resonance, 4), "weight": self.RESONANCE_WEIGHT},
            },
            "dimension_issues": dimension_issues if dimension_issues else {},
            "evaluated_at": time.time(),
        }

        return {
            "status": "ok",
            "reason": f"四维质量评估完成，等级: {quality_tier}",
            "data": result_data,
            "warnings": warnings,
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检，使用最小测试信号覆盖所有维度及降级路径"""
        try:
            # 构造最小测试信号
            test_signal = {
                "score": 75.0,
                "direction": 1,
                "timestamp": time.time(),
                "sub_dimensions": {"price_phase": 0.8, "momentum": 0.7, "obi": 0.6, "vwap": 0.9},
                "symbol": "HEALTH_CHECK",
            }
            result = self.evaluate(test_signal)
            if result["status"] != "ok":
                return {
                    "status": "error",
                    "reason": "自检评估失败",
                    "data": {"error_detail": result},
                    "warnings": [],
                }

            # 验证降级路径是否被正确标记
            deg_warnings = [w for w in result.get("warnings", []) if "降级" in w]
            dep_status = {
                "gustatory_cortex": self._gustatory_cortex is not None,
                "multi_tf_arbiter": self._multi_tf_arbiter is not None,
                "signal_funnel": self._signal_funnel is not None,
                "behavioral_logger": self._behavioral_logger is not None,
            }

            return {
                "status": "ok",
                "reason": "SignalQualityPrecheck 自检通过",
                "data": {
                    "dependencies": dep_status,
                    "degraded_dimensions": len(deg_warnings),
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查依赖注入状态与信号上下文结构")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    @staticmethod
    def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
        return max(min_val, min(max_val, value))

    def _calculate_purity(self, sub_dimensions: Dict[str, float], direction: int) -> float:
        """计算纯度：子维度方向一致性"""
        expected_keys = ["price_phase", "momentum", "obi", "vwap"]
        values = []
        for key in expected_keys:
            val = sub_dimensions.get(key)
            if isinstance(val, (int, float)) and not math.isnan(val):
                values.append(self._clamp(float(val), 0.0, 1.0))
            else:
                values.append(0.5)  # 缺失视为中性

        if not values:
            return self.DEGRADED_PURITY_SCORE

        aligned = 0
        for v in values:
            if v > 0.5 + self.DIRECTION_DELTA and direction == 1:
                aligned += 1
            elif v < 0.5 - self.DIRECTION_DELTA and direction == -1:
                aligned += 1

        purity = aligned / len(values)
        return self._clamp(purity)

    def _calculate_rarity(self, score: float, symbol: str) -> float:
        """计算稀有性：历史分位映射"""
        if self._gustatory_cortex and hasattr(self._gustatory_cortex, 'get_rarity_percentile'):
            try:
                percentile = self._gustatory_cortex.get_rarity_percentile(score, symbol)
                if isinstance(percentile, (int, float)) and not math.isnan(percentile):
                    rarity = abs(percentile - 0.5) * 2.0
                    return self._clamp(rarity)
            except Exception as e:
                logger.debug(f"稀有性计算异常: {e}", exc_info=True)
        return self.DEGRADED_RARITY_SCORE

    def _calculate_timeliness(self, trigger_time: float) -> float:
        """计算时效性：指数衰减"""
        now = time.time()
        if math.isnan(trigger_time) or trigger_time > now + 10.0:
            trigger_time = now
        elapsed = max(0.0, now - trigger_time)
        if elapsed <= self.OPTIMAL_WINDOW_SECONDS:
            return 1.0
        decay = 2.0 ** (-(elapsed - self.OPTIMAL_WINDOW_SECONDS) / self.DECAY_HALFLIFE_SECONDS)
        return max(self.MIN_TIMELINESS, decay)

    def _calculate_resonance(self, signal_context: Dict[str, Any]) -> float:
        """计算共振度：跨品种/周期共振"""
        if self._multi_tf_arbiter and hasattr(self._multi_tf_arbiter, 'get_resonance_info'):
            try:
                info = self._multi_tf_arbiter.get_resonance_info(signal_context)
                if isinstance(info, dict):
                    strength = info.get("strength", 0.0)
                    if isinstance(strength, (int, float)) and not math.isnan(strength):
                        return self._clamp(strength)
            except Exception as e:
                logger.debug(f"共振度计算异常: {e}", exc_info=True)
        return self.DEGRADED_RESONANCE_SCORE
