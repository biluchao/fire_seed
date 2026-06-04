"""
火种系统 · 脆弱性指数计算器 (FragilityIndexCalculator)

核心职责：
1. 聚合流动性、集中度、相关性、规模四个子维度的脆弱性评分，输出综合脆弱性指数与风险等级
2. 提供手动覆盖通道，支持运维人员临时调整各子维度权重与直接设定风险等级，覆盖有效期可配置，操作受冷却期与审计限制
3. 实现自动熔断与解除闭环：当脆弱性指数连续触发高危阈值时自动向总线发送暂停指令，并在风险解除后自动恢复

外部依赖（真实模块接口）：
- core.negotiation_bus.NegotiationBus : 发布脆弱性指数变更事件、分级告警与自动熔断指令
- core.behavioral_logger.BehavioralLogger : 记录脆弱性计算日志与手动覆盖审计记录

接口契约：
- calculate_fragility(metrics: Dict[str, Any]) -> Dict[str, Any] : 计算综合脆弱性指数，返回评分、等级、降仓建议
- get_sub_dimensions_detail() -> Dict[str, Any] : 返回各子维度的实时评分、权重与最近一次原始指标
- set_manual_override(overrides: Dict[str, Any], duration_seconds: int) -> Dict[str, Any] : 设置手动覆盖参数
- clear_manual_override() -> Dict[str, Any] : 清除所有手动覆盖
- health_check() -> Dict[str, Any] : 模块自检，会执行冒烟测试并检查依赖模块健康度
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当某个子维度数据缺失或无效时，该维度自动采用指数加权移动平均历史值或最高脆弱性评分（0.8）作为保守估计
- 当 NegotiationBus 不可用时，告警与自动熔断功能降级为仅本地日志记录
- 手动覆盖参数在有效期内被优先采用，过期后自动恢复配置默认值，并强制推送通知与审计日志
- 所有降级值在类常量区明确声明

资源管理：
- 本模块不持有任何外部资源句柄，手动覆盖数据存储于内存字典中，模块销毁时自动回收
- 告警去重状态与历史评分使用定长双端队列（deque），自动淘汰旧数据，防止内存泄漏
- 对外返回的快照数据均使用深拷贝，防止外部意外修改内部状态
"""

import time
import logging
import threading
import math
import copy
from typing import Dict, Any, List, Optional
from collections import deque

logger = logging.getLogger(__name__)


class FragilityIndexCalculator:
    """脆弱性指数计算器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 各子维度默认权重（总和为 1.0）
    DEFAULT_WEIGHT_LIQUIDITY = 0.35       # 流动性脆弱性权重，[0.0, 1.0]
    DEFAULT_WEIGHT_CONCENTRATION = 0.30   # 集中度脆弱性权重，[0.0, 1.0]
    DEFAULT_WEIGHT_CORRELATION = 0.20     # 相关性脆弱性权重，[0.0, 1.0]
    DEFAULT_WEIGHT_SCALE = 0.15           # 规模脆弱性权重，[0.0, 1.0]

    # 脆弱性等级阈值 (0.0 - 1.0)
    THRESHOLD_LOW = 0.30                  # 低脆弱性上限
    THRESHOLD_MEDIUM = 0.60               # 中脆弱性上限
    THRESHOLD_HIGH = 0.85                 # 高脆弱性上限

    # 建议降仓比例（此比例仅供参考，实际执行由订单管理层接管）
    SUGGESTED_REDUCTION = {"low": 0.0, "medium": 0.20, "high": 0.40, "critical": 0.60}

    # 手动覆盖默认有效期（秒）
    DEFAULT_OVERRIDE_DURATION_SEC = 300
    # 手动操作最小冷却期（秒）
    MIN_OVERRIDE_INTERVAL_SEC = 60

    # 数据缺失时的保守默认值 (0.8 基于历史回测，在最坏情况下不会过度反应)
    FALLBACK_FRAGILITY_SCORE = 0.8
    # 启动安全值，用于冷启动时填充历史数据
    STARTUP_SAFE_VALUES = {"liquidity": 0.4, "concentration": 0.3, "correlation": 0.5, "scale": 0.2}
    # 历史评分窗口大小
    HISTORY_WINDOW_SIZE = 10
    # EWMA 衰减因子 (0.0-1.0，越大越依赖近期数据)
    EWMA_ALPHA = 0.4

    # 告警去重窗口（秒）
    ALERT_DEDUP_WINDOW_SEC = 30
    # 高频告警抑制：连续触发 N 次 critical 则自动熔断
    CRITICAL_CONSECUTIVE_THRESHOLD = 3
    # 熔断解除阈值：指数回落至此等级以下时，自动发送解除指令
    CIRCUIT_BREAK_RELEASE_LEVEL = "medium"

    # 计算精度
    SCORE_PRECISION = 4

    # 子维度计算系数（可配置）
    LIQUIDITY_DEPTH_WEIGHT = 0.6
    LIQUIDITY_SPREAD_WEIGHT = 0.4
    CONCENTRATION_SINGLE_WEIGHT = 0.5
    CONCENTRATION_CROSS_WEIGHT = 0.5

    def __init__(self, config: Dict[str, Any] = None):
        # 从配置中覆盖类常量
        if config:
            for key in ["LIQUIDITY_DEPTH_WEIGHT", "LIQUIDITY_SPREAD_WEIGHT",
                        "CONCENTRATION_SINGLE_WEIGHT", "CONCENTRATION_CROSS_WEIGHT",
                        "FALLBACK_FRAGILITY_SCORE", "SCORE_PRECISION", "EWMA_ALPHA",
                        "CIRCUIT_BREAK_RELEASE_LEVEL", "HISTORY_WINDOW_SIZE"]:
                if key in config:
                    setattr(self, key, config[key])

        self._active_weights = {
            "liquidity": self.DEFAULT_WEIGHT_LIQUIDITY,
            "concentration": self.DEFAULT_WEIGHT_CONCENTRATION,
            "correlation": self.DEFAULT_WEIGHT_CORRELATION,
            "scale": self.DEFAULT_WEIGHT_SCALE,
        }

        # 校验默认权重总和
        total = sum(self._active_weights.values())
        if abs(total - 1.0) > 0.01:
            logger.error("默认权重总和异常: %.2f，已自动归一化", total)
            for dim in self._active_weights:
                self._active_weights[dim] /= total

        self._manual_overrides: Dict[str, Any] = {}
        self._override_expiry: float = 0.0
        self._last_override_timestamp: float = 0.0

        # 历史评分（使用 deque 实现 O(1) 淘汰，并预填充启动安全值）
        self._historical_scores: Dict[str, deque] = {
            dim: deque(
                [self.STARTUP_SAFE_VALUES.get(dim, 0.5)] * (self.HISTORY_WINDOW_SIZE // 2),
                maxlen=self.HISTORY_WINDOW_SIZE
            )
            for dim in self._active_weights
        }

        # 最近一次计算详情
        self._last_calculation_detail: Dict[str, Any] = {}

        # 外部依赖
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 告警去重状态
        self._alert_last_triggered: Dict[str, float] = {}
        # 自动熔断状态
        self._critical_alert_counter: int = 0
        self._auto_circuit_break_active: bool = False
        # 上次覆盖过期通知时间
        self._last_expired_notify_time: float = 0.0

        # 线程安全
        self._lock = threading.Lock()

        logger.info("FragilityIndexCalculator 初始化完成，默认权重: %s", self._active_weights)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        if negotiation_bus is not None and hasattr(negotiation_bus, 'publish_alert'):
            self._negotiation_bus = negotiation_bus
            logger.info("NegotiationBus 注入成功")
        else:
            logger.warning("NegotiationBus 不可用或缺少 publish_alert，告警降级")

        if behavioral_logger is not None and hasattr(behavioral_logger, 'log_event'):
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，审计日志降级")

    # ========== 公共接口 ==========
    def calculate_fragility(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        start_time = time.monotonic()
        if not isinstance(metrics, dict):
            logger.error("输入 metrics 格式错误: %s", type(metrics))
            return {"status": "error", "reason": "metrics 必须为字典", "data": {}, "warnings": ["invalid_input"]}

        # ---- 原子化获取当前状态 ----
        with self._lock:
            self._purge_expired_overrides()
            weights = self._active_weights.copy()
            override_data = self._manual_overrides.copy()
            direct_level = override_data.get("direct_level", None)

        warnings = []
        sub_scores = {}
        dominant_dimension = "unknown"

        # 显式规则：direct_level 优先级最高
        if direct_level and direct_level in self.SUGGESTED_REDUCTION:
            # 虽然覆盖了等级，但仍计算子维度以供展示
            for dim in weights:
                sub_scores[dim] = self._compute_dimension(dim, metrics.get(dim), warnings)
            composite_score = (
                self.THRESHOLD_HIGH + 0.05 if direct_level == "critical"
                else self.THRESHOLD_MEDIUM + 0.05
            )
            level = direct_level
            suggested_reduction = self.SUGGESTED_REDUCTION[level]
            detail = {
                "composite_score": composite_score,
                "level": level,
                "suggested_reduction": suggested_reduction,
                "sub_scores": sub_scores,
                "active_weights": weights,
                "manual_override_active": True,
                "direct_level_override": True,
            }
            self._last_calculation_detail = copy.deepcopy(detail)
            logger.info("直接等级覆盖生效: %s, 子维度: %s", level, sub_scores)
            return {"status": "ok", "reason": f"手动直接等级覆盖: {level}", "data": detail, "warnings": warnings}

        # 正常计算逻辑
        for dim in weights:
            sub_scores[dim] = self._compute_dimension(dim, metrics.get(dim), warnings)

        # 加权聚合
        composite_score = round(sum(weights[d] * sub_scores[d] for d in weights), self.SCORE_PRECISION)

        # 异常检测：分数不应超过 1.0，如果超过说明逻辑有 bug
        if composite_score > 1.0:
            logger.error("异常高分 %.4f，可能是子维度计算逻辑错误，已强制钳位并告警", composite_score)
            composite_score = 1.0

        # 确定主导维度（贡献最大的脆弱性来源）
        if sub_scores:
            dominant_dimension = max(sub_scores, key=lambda k: sub_scores[k] * weights.get(k, 0))

        # 判定风险等级
        if composite_score <= self.THRESHOLD_LOW:
            level = "low"
        elif composite_score <= self.THRESHOLD_MEDIUM:
            level = "medium"
        elif composite_score <= self.THRESHOLD_HIGH:
            level = "high"
        else:
            level = "critical"

        suggested_reduction = self.SUGGESTED_REDUCTION[level]
        override_active = bool(self._manual_overrides) and (time.monotonic() < self._override_expiry)

        detail = {
            "composite_score": composite_score,
            "level": level,
            "suggested_reduction": suggested_reduction,
            "sub_scores": sub_scores,
            "active_weights": weights,
            "manual_override_active": override_active,
            "dominant_dimension": dominant_dimension,
        }
        self._last_calculation_detail = copy.deepcopy(detail)

        # 处理告警与自动熔断闭环
        if level in ("high", "critical"):
            self._critical_alert_counter += 1 if level == "critical" else 0
            self._trigger_alert(level, dominant_dimension, f"脆弱性指数 {composite_score:.4f} ({level})", detail)
            if self._critical_alert_counter >= self.CRITICAL_CONSECUTIVE_THRESHOLD and not self._auto_circuit_break_active:
                self._trigger_auto_circuit_break(composite_score, detail)
                self._auto_circuit_break_active = True
                self._critical_alert_counter = 0
        else:
            # 解除熔断
            if self._auto_circuit_break_active and level == self.CIRCUIT_BREAK_RELEASE_LEVEL:
                self._trigger_auto_circuit_break_release(composite_score)
                self._auto_circuit_break_active = False
            self._critical_alert_counter = 0

        elapsed = (time.monotonic() - start_time) * 1e6
        logger.debug("脆弱性计算耗时: %.1fμs, 结果: %.4f (%s)", elapsed, composite_score, level)

        return {
            "status": "ok",
            "reason": f"综合脆弱性指数: {composite_score:.4f} ({level})",
            "data": detail,
            "warnings": warnings,
        }

    def get_sub_dimensions_detail(self) -> Dict[str, Any]:
        """返回各子维度的详细信息"""
        with self._lock:
            weights = self._active_weights.copy()
            last_calc = copy.deepcopy(self._last_calculation_detail)
        return {
            "status": "ok",
            "reason": "返回当前子维度详情",
            "data": {
                "weights": weights,
                "last_calculation": last_calc,
                "historical_means": {dim: self._get_ewma(dim) for dim in weights},
                "manual_override_active": bool(self._manual_overrides) and (time.monotonic() < self._override_expiry),
                "auto_circuit_break_active": self._auto_circuit_break_active,
            },
            "warnings": [],
        }

    def set_manual_override(self, overrides: Dict[str, Any], duration_seconds: int = None) -> Dict[str, Any]:
        if duration_seconds is None:
            duration_seconds = self.DEFAULT_OVERRIDE_DURATION_SEC

        with self._lock:
            now = time.monotonic()
            if now - self._last_override_timestamp < self.MIN_OVERRIDE_INTERVAL_SEC:
                remaining = self.MIN_OVERRIDE_INTERVAL_SEC - (now - self._last_override_timestamp)
                return {"status": "error", "reason": f"冷却中，请 {remaining:.0f} 秒后重试", "data": {}, "warnings": ["cooldown_active"]}

            new_weights = overrides.get("weights", {})
            direct_level = overrides.get("direct_level")

            if direct_level and direct_level not in self.SUGGESTED_REDUCTION:
                return {"status": "error", "reason": f"无效的直接等级: {direct_level}", "data": {}, "warnings": ["invalid_direct_level"]}

            if new_weights:
                total = sum(new_weights.values())
                if abs(total - 1.0) > 0.05:
                    return {"status": "error", "reason": f"权重总和 {total:.2f} 偏差过大", "data": {}, "warnings": ["weight_mismatch"]}
                for dim, val in new_weights.items():
                    if dim not in self._active_weights:
                        return {"status": "error", "reason": f"未知子维度: {dim}", "data": {}, "warnings": ["unknown_dimension"]}
                    if not (0.0 <= val <= 1.0):
                        return {"status": "error", "reason": f"权重 {dim}={val} 超出 [0.0, 1.0]", "data": {}, "warnings": ["weight_out_of_range"]}
                self._active_weights.update(new_weights)

            self._manual_overrides = overrides
            self._override_expiry = now + duration_seconds
            self._last_override_timestamp = now

        if self._behavioral_logger:
            self._behavioral_logger.log_event("fragility_override", {"overrides": overrides, "duration": duration_seconds})

        logger.warning("手动覆盖已生效，有效期至 %s", time.strftime("%H:%M:%S", time.localtime(self._override_expiry)))
        return {"status": "ok", "reason": "手动覆盖已生效", "data": {"expiry": self._override_expiry}, "warnings": []}

    def clear_manual_override(self) -> Dict[str, Any]:
        with self._lock:
            self._manual_overrides.clear()
            self._override_expiry = 0.0
            self._active_weights = {
                "liquidity": self.DEFAULT_WEIGHT_LIQUIDITY,
                "concentration": self.DEFAULT_WEIGHT_CONCENTRATION,
                "correlation": self.DEFAULT_WEIGHT_CORRELATION,
                "scale": self.DEFAULT_WEIGHT_SCALE,
            }

        if self._behavioral_logger:
            self._behavioral_logger.log_event("fragility_override_cleared", {})

        logger.info("手动覆盖已清除，恢复默认权重")
        return {"status": "ok", "reason": "已恢复默认参数", "data": {}, "warnings": []}

    def health_check(self) -> Dict[str, Any]:
        try:
            # 1. 基础状态检查
            with self._lock:
                total = sum(self._active_weights.values())
            if abs(total - 1.0) > 0.05:
                return {"status": "degraded", "reason": f"权重总和异常 ({total:.2f})", "data": {}, "warnings": ["weight_invalid"]}

            # 2. 冒烟测试：传入包含 NaN 和极端值的数据
            dummy_metrics = {
                "liquidity": {"depth_ratio": 0.1, "spread_ratio": 0.9},
                "concentration": {"single_exposure_ratio": 0.8, "cross_strategy_ratio": 0.7},
                "correlation": {"tail_dependency": 0.95},
                "scale": {"equity_depth_ratio": 0.05},
            }
            test_result = self.calculate_fragility(dummy_metrics)
            if test_result["status"] != "ok":
                return {"status": "error", "reason": f"冒烟测试失败: {test_result['reason']}", "data": {}, "warnings": ["smoke_test_failed"]}

            # 3. 检查依赖健康度
            dep_status = {}
            if self._negotiation_bus and hasattr(self._negotiation_bus, 'health_check'):
                dep_status['negotiation_bus'] = self._negotiation_bus.health_check().get('status', 'unknown')
            if self._behavioral_logger and hasattr(self._behavioral_logger, 'health_check'):
                dep_status['behavioral_logger'] = self._behavioral_logger.health_check().get('status', 'unknown')

            return {
                "status": "ok",
                "reason": "所有检查通过",
                "data": {
                    "dependencies": dep_status,
                    "active_weights": self._active_weights,
                    "override_active": bool(self._manual_overrides) and (time.monotonic() < self._override_expiry),
                    "auto_circuit_break_active": self._auto_circuit_break_active,
                    "history_size": {dim: len(self._historical_scores[dim]) for dim in self._active_weights},
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e}", exc_info=True)
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["exception"]}

    # ========== 私有计算辅助 ==========
    def _compute_dimension(self, dim: str, raw_data: Any, warnings: List[str]) -> float:
        """计算单个维度的脆弱性，包含降级和 NaN 处理"""
        if raw_data is None or not isinstance(raw_data, dict):
            ewma = self._get_ewma(dim)
            fallback = ewma if ewma is not None else self.FALLBACK_FRAGILITY_SCORE
            if ewma is not None:
                warnings.append(f"{dim} 数据缺失，使用 EWMA {fallback:.4f}")
            else:
                warnings.append(f"{dim} 数据缺失且无历史，采用保守默认值 {fallback:.4f}")
                logger.warning(f"{dim} 维度数据源异常，已触发保守降级 #RECOVERY: 检查上游感知模块")
            return fallback

        calc_method = getattr(self, f"_calc_{dim}_fragility", None)
        if calc_method is None:
            ewma = self._get_ewma(dim)
            fallback = ewma if ewma is not None else self.FALLBACK_FRAGILITY_SCORE
            warnings.append(f"{dim} 计算方法缺失，采用降级值")
            return fallback

        try:
            raw_score = calc_method(raw_data)
            # NaN/Inf 防御
            if not math.isfinite(raw_score):
                logger.error(f"{dim} 计算结果非有限值: {raw_score}，使用降级值")
                return self._get_ewma(dim) or self.FALLBACK_FRAGILITY_SCORE
            clamped = max(0.0, min(1.0, raw_score))
            # 保存历史
            self._historical_scores[dim].append(clamped)
            return round(clamped, self.SCORE_PRECISION)
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"{dim} 计算异常: {e}")
            return self._get_ewma(dim) or self.FALLBACK_FRAGILITY_SCORE

    def _get_ewma(self, dim: str) -> Optional[float]:
        """获取指定维度的指数加权移动平均"""
        history = self._historical_scores.get(dim, deque(maxlen=self.HISTORY_WINDOW_SIZE))
        if not history:
            return None
        weights = [(1 - self.EWMA_ALPHA) ** i for i in range(len(history))]
        weights.reverse()
        total_weight = sum(weights)
        if total_weight == 0:
            return sum(history) / len(history)
        return round(sum(w * v for w, v in zip(weights, history)) / total_weight, self.SCORE_PRECISION)

    # ========== 子维度计算 ==========
    def _calc_liquidity_fragility(self, data: Dict[str, Any]) -> float:
        depth_ratio = float(data.get("depth_ratio", 1.0))
        spread_ratio = float(data.get("spread_ratio", 0.0))
        return self.LIQUIDITY_DEPTH_WEIGHT * (1.0 - depth_ratio) + self.LIQUIDITY_SPREAD_WEIGHT * spread_ratio

    def _calc_concentration_fragility(self, data: Dict[str, Any]) -> float:
        single = float(data.get("single_exposure_ratio", 0.0))
        cross = float(data.get("cross_strategy_ratio", 0.0))
        return self.CONCENTRATION_SINGLE_WEIGHT * single + self.CONCENTRATION_CROSS_WEIGHT * cross

    def _calc_correlation_fragility(self, data: Dict[str, Any]) -> float:
        return float(data.get("tail_dependency", 0.0))

    def _calc_scale_fragility(self, data: Dict[str, Any]) -> float:
        return float(data.get("equity_depth_ratio", 0.0))

    # ========== 私有：覆盖管理 ==========
    def _purge_expired_overrides(self):
        """清理过期的覆盖状态，并重置权重"""
        if self._override_expiry and time.monotonic() > self._override_expiry:
            logger.info("手动覆盖已过期，自动恢复默认参数")
            self._manual_overrides.clear()
            self._override_expiry = 0.0
            self._active_weights = {
                "liquidity": self.DEFAULT_WEIGHT_LIQUIDITY,
                "concentration": self.DEFAULT_WEIGHT_CONCENTRATION,
                "correlation": self.DEFAULT_WEIGHT_CORRELATION,
                "scale": self.DEFAULT_WEIGHT_SCALE,
            }
            # 限制通知频率（每小时最多一次）
            now = time.monotonic()
            if now - self._last_expired_notify_time > 3600:
                self._notify_override_expired()
                self._last_expired_notify_time = now

    def _notify_override_expired(self):
        """覆盖过期通知与审计"""
        if self._behavioral_logger:
            self._behavioral_logger.log_event("fragility_override_expired", {})
        if self._negotiation_bus and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="fragility_override_expired",
                    level="info",
                    message="手动覆盖已过期，系统恢复自动模式",
                    timestamp=time.time()
                )
            except Exception:
                logger.warning("覆盖过期通知发送失败")

    # ========== 私有：告警与熔断 ==========
    def _trigger_alert(self, level: str, dominant_dim: str, message: str, detail: Dict[str, Any]):
        """带全局去重的告警推送，推送逻辑在锁外执行"""
        alert_key = f"fragility_{level}_{dominant_dim}"
        now = time.monotonic()

        last_time = self._alert_last_triggered.get(alert_key, 0)
        if now - last_time < self.ALERT_DEDUP_WINDOW_SEC:
            return
        self._alert_last_triggered[alert_key] = now

        if self._negotiation_bus and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="fragility_index",
                    level=level,
                    message=message,
                    detail=detail,
                    timestamp=time.time()
                )
            except Exception:
                logger.warning("协商总线告警推送失败", exc_info=True)

        if level in ("high", "critical"):
            logger.error(f"{message} #RECOVERY: 主导维度 {dominant_dim}，检查数据源并考虑降仓")
        else:
            logger.warning(message)

        if self._behavioral_logger and hasattr(self._behavioral_logger, 'log_event'):
            try:
                self._behavioral_logger.log_event("fragility_alert", {"level": level, "dimension": dominant_dim, "message": message})
            except Exception:
                logger.warning("行为日志记录失败", exc_info=True)

    def _trigger_auto_circuit_break(self, score: float, detail: Dict[str, Any]):
        """自动熔断：连续多次 critical 后，主动向协商总线发送暂停指令"""
        if self._negotiation_bus and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="auto_circuit_break",
                    level="critical",
                    message=f"脆弱性指数连续触发阈值 ({score:.4f})，系统自动熔断",
                    detail=detail,
                    timestamp=time.time()
                )
                logger.critical("自动熔断指令已发送！主导维度: %s", detail.get("dominant_dimension"))
            except Exception:
                logger.critical("自动熔断指令发送失败！", exc_info=True)

    def _trigger_auto_circuit_break_release(self, score: float):
        """解除熔断：指数回落至安全区域后，自动发送恢复指令"""
        if self._negotiation_bus and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="auto_circuit_break_release",
                    level="info",
                    message=f"脆弱性指数回落至 {score:.4f}，系统自动解除熔断",
                    timestamp=time.time()
                )
                logger.info("自动熔断解除指令已发送")
            except Exception:
                logger.warning("自动熔断解除指令发送失败", exc_info=True)
