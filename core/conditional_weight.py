"""
火种系统 · 条件权重引擎入口 (ConditionalWeightEngine)

核心职责：
1. 整合因子时效管理、IC 预测性调整与冗余惩罚，对外提供统一的权重更新接口。
2. 按固定周期或事件触发，协调各子模块完成因子权重从“到期判定→前瞻调整→冗余压制”的完整流水线。

外部依赖（真实模块接口）：
- core.conditional_weight.temporal_weight_manager.TemporalWeightManager : 判断因子是否到达更新周期
- core.conditional_weight.ic_predictive_adjuster.ICPredictiveAdjuster : 基于 IC 加速度的预判调整
- core.conditional_weight.redundancy_penalty.RedundancyPenalty : 基于互信息的因子冗余惩罚

接口契约：
- compute_weights(ic_series, current_weights, factor_types, factor_values) -> Dict[str, Any]
  输出字典固定包含 "updated_weights" (Dict[str,float]), "updates_applied" (int), "reason" (str), "warnings" (List[str])
- health_check() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "message" (str), "submodules" (Dict[str,Any])

异常与降级：
- 任一子模块在初始化或运行时发生异常，自动降级为“仅返回原始权重”，并记录 ERROR 日志。
- 当子模块返回的权重总和为 0 时，回退为均匀分布。

资源管理：
- 本模块仅持有子模块实例，不持有任何需要手动释放的外部资源。
"""

import logging
import time
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class ConditionalWeightEngine:
    """条件权重引擎：协调时效、预测和冗余惩罚的权重更新入口"""

    # 类常量（默认配置，附带单位与取值范围注释）
    DEFAULT_UPDATE_INTERVAL = 3600        # 默认全量更新间隔，秒，取值范围 [60, 86400]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        初始化条件权重引擎及其三个子模块。
        所有子模块的配置均从 config 的对应段落中提取，若未提供则使用各自的类常量。
        若任一子模块初始化失败，引擎进入降级模式，并记录具体原因。
        """
        cfg = config or {}
        # 提取子模块配置
        temporal_cfg = cfg.get("temporal", {})
        ic_predictive_cfg = cfg.get("ic_predictive", {})
        redundancy_cfg = cfg.get("redundancy", {})

        # 初始化子模块，若任一子模块初始化失败，引擎进入降级模式
        self._temporal_manager = None
        self._ic_adjuster = None
        self._redundancy_penalty = None
        self._degraded = False
        self._degraded_reason = ""

        try:
            from core.conditional_weight.temporal_weight_manager import TemporalWeightManager
            self._temporal_manager = TemporalWeightManager(config=temporal_cfg)
            logger.info("TemporalWeightManager 加载成功")
        except Exception as e:
            self._degraded = True
            self._degraded_reason = f"TemporalWeightManager 加载失败: {e}"
            logger.error(f"{self._degraded_reason}，引擎进入降级模式")

        try:
            from core.conditional_weight.ic_predictive_adjuster import ICPredictiveAdjuster
            self._ic_adjuster = ICPredictiveAdjuster(config=ic_predictive_cfg)
            logger.info("ICPredictiveAdjuster 加载成功")
        except Exception as e:
            self._degraded = True
            self._degraded_reason = f"ICPredictiveAdjuster 加载失败: {e}"
            logger.error(f"{self._degraded_reason}，引擎进入降级模式")

        try:
            from core.conditional_weight.redundancy_penalty import RedundancyPenalty
            self._redundancy_penalty = RedundancyPenalty(config=redundancy_cfg)
            logger.info("RedundancyPenalty 加载成功")
        except Exception as e:
            self._degraded = True
            self._degraded_reason = f"RedundancyPenalty 加载失败: {e}"
            logger.error(f"{self._degraded_reason}，引擎进入降级模式")

        # 运行时状态
        self._last_full_update: float = 0.0
        self._update_interval = float(cfg.get("update_interval", self.DEFAULT_UPDATE_INTERVAL))
        logger.info(f"ConditionalWeightEngine 初始化完成 (降级: {self._degraded})")

    # ────────────────────────── 公共接口 ──────────────────────────
    def compute_weights(
        self,
        ic_series: Dict[str, Any],
        current_weights: Dict[str, float],
        factor_types: Dict[str, str],
        factor_values: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        执行完整的权重更新流水线：时效判定 → IC 调整 → 冗余惩罚。
        若引擎处于降级模式，直接返回原始权重。

        参数:
            ic_series: 因子名 -> 近期 IC 序列（numpy 数组）。
            current_weights: 当前因子权重映射。
            factor_types: 因子名 -> 类型标签 (fast/medium/slow/dormant)。
            factor_values: 因子名 -> 因子标准化数值序列（numpy 数组），用于冗余惩罚。

        返回:
            标准化字典，包含更新后的权重、已更新因子数、原因和警告。
        """
        warnings: List[str] = []
        if self._degraded:
            reason = f"引擎降级: {self._degraded_reason}，返回原始权重"
            logger.warning(reason)
            return {
                "updated_weights": {**current_weights},
                "updates_applied": 0,
                "reason": reason,
                "warnings": warnings
            }

        if not ic_series or not current_weights:
            reason = "输入 IC 序列或当前权重为空，返回原始权重"
            logger.debug(reason)
            return {
                "updated_weights": {**current_weights},
                "updates_applied": 0,
                "reason": reason,
                "warnings": []
            }

        # 1. 时效过滤：仅对到期的因子执行后续调整
        due_factors = self._filter_due_factors(factor_types, warnings)

        # 2. 若无因子到期，直接返回
        if not due_factors:
            reason = "无因子到达更新周期，权重保持不变"
            logger.debug(reason)
            return {
                "updated_weights": {**current_weights},
                "updates_applied": 0,
                "reason": reason,
                "warnings": warnings
            }

        # 3. 提取到期因子的 IC 序列
        due_ic_series = {name: ic_series[name] for name in due_factors if name in ic_series}
        due_current_weights = {name: current_weights.get(name, 0.0) for name in due_factors}

        # 4. IC 预测性调整
        ic_result = self._ic_adjuster.adjust_weights(due_ic_series, due_current_weights)
        warnings.extend(ic_result.get("warnings", []))
        interim_weights = {**current_weights, **ic_result["adjusted_weights"]}

        # 5. 冗余惩罚（需要因子值）
        if factor_values and self._redundancy_penalty:
            due_factor_values = {name: factor_values[name] for name in due_factors if name in factor_values}
            if due_factor_values:
                penalty_result = self._redundancy_penalty.compute_penalty(due_factor_values)
                warnings.extend(penalty_result.get("warnings", []))
                penalty_map = penalty_result["penalty_map"]
                # 应用惩罚系数
                for name in due_factors:
                    if name in interim_weights and name in penalty_map:
                        interim_weights[name] *= penalty_map[name]

        # 6. 归一化权重
        total = sum(interim_weights.values())
        if total <= 0.0:
            n = len(interim_weights)
            if n > 0:
                uniform = 1.0 / n
                for name in interim_weights:
                    interim_weights[name] = uniform
            reason = "权重总和为零，回退为均匀分布"
            logger.warning(reason)
            warnings.append(reason)
        else:
            for name in interim_weights:
                interim_weights[name] /= total

        # 7. 标记已更新的因子
        for name in due_factors:
            self._temporal_manager.mark_updated(name)

        self._last_full_update = time.time()
        reason = f"权重更新完成: 到期 {len(due_factors)} 个，最终权重已归一化"
        logger.info(reason)

        return {
            "updated_weights": interim_weights,
            "updates_applied": len(due_factors),
            "reason": reason,
            "warnings": warnings
        }

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检：依次检查三个子模块的健康状态，并汇总结果。"""
        try:
            sub_status = {}
            # 尝试动态导入并检查各子模块
            try:
                from core.conditional_weight.temporal_weight_manager import TemporalWeightManager
                sub_status["temporal"] = TemporalWeightManager.health_check()
            except Exception as e:
                sub_status["temporal"] = {"status": "error", "message": str(e)}

            try:
                from core.conditional_weight.ic_predictive_adjuster import ICPredictiveAdjuster
                sub_status["ic_predictive"] = ICPredictiveAdjuster.health_check()
            except Exception as e:
                sub_status["ic_predictive"] = {"status": "error", "message": str(e)}

            try:
                from core.conditional_weight.redundancy_penalty import RedundancyPenalty
                sub_status["redundancy"] = RedundancyPenalty.health_check()
            except Exception as e:
                sub_status["redundancy"] = {"status": "error", "message": str(e)}

            all_ok = all(v.get("status") == "ok" for v in sub_status.values())
            return {
                "status": "ok" if all_ok else "degraded",
                "message": "健康检查完成" if all_ok else "部分子模块异常",
                "submodules": sub_status
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _filter_due_factors(
        self,
        factor_types: Dict[str, str],
        warnings: List[str]
    ) -> List[str]:
        """
        根据时效管理器筛选出当前到期的因子列表。
        """
        due = []
        for name, ftype in factor_types.items():
            result = self._temporal_manager.should_update(name, ftype)
            if result.get("warnings"):
                warnings.extend(result["warnings"])
            if result["should_update"]:
                due.append(name)
        return due
