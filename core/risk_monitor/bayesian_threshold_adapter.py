"""
火种系统 · 贝叶斯自适应阈值适配器 (BayesianThresholdAdapter)

核心职责：
1. 根据历史熔断事件的真实结果，使用指数衰减贝叶斯方法动态调整风控阈值，结合先验分布与近期事件加权
2. 为不同市场状态维护独立的阈值参数集与事件窗口，支持阈值调整振荡保护、安全回滚及宏观噪音过滤

外部依赖（真实模块接口）：
- core.behavioral_logger.BehavioralLogger : 记录阈值变更审计日志与事件处理详情
- core.negotiation_bus.NegotiationBus : 发布阈值变更通知与异常事件告警
- numpy (>=1.21.0) : 用于滑动窗口内的统计与指数衰减权重计算
- config/risk/bayesian_threshold.yaml : 可选的配置文件，用于覆盖类常量默认值

接口契约：
- process_event(event_data: Dict[str, Any]) -> Dict[str, Any] : 处理一次熔断事件，返回调整结果与诊断统计
- get_threshold(market_regime: str) -> Dict[str, Any] : 获取指定市场状态的当前阈值及窗口统计概要
- reset_to_defaults(market_regime: Optional[str] = None) -> Dict[str, Any] : 将阈值重置为安全默认值
- health_check() -> Dict[str, Any] : 模块自检，返回事件统计与依赖状态
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 BehavioralLogger 或 NegotiationBus 不可用时，相关审计与告警功能静默降级为本地日志
- 当事件样本不足时，保持当前阈值不变，并在诊断中标记 insufficient_data
- 当传入数据非法时，使用安全默认值并记录 WARNING 日志
- 所有降级值在类常量区明确声明，降级频率通过内部计数器暴露

资源管理：
- 每个市场状态的事件窗口有容量与时间双重限制，定期清理过期事件，控制内存使用
- 线程锁保护所有共享状态，不持有任何外部资源句柄
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple
from collections import deque
import numpy as np

logger = logging.getLogger(__name__)


class BayesianThresholdAdapter:
    """贝叶斯自适应阈值适配器"""

    # ========== 类常量（默认配置，单位与取值范围注释） ==========
    # ---- 事件窗口 ----
    DEFAULT_EVENT_WINDOW_SIZE = 20          # 最大事件数，无量纲，[10, 100]
    DEFAULT_MIN_EVENTS_FOR_ADJUST = 10     # 最小触发调整事件数，无量纲，[5, 30]
    DEFAULT_MAX_DATA_AGE_DAYS = 60          # 最大保留天数，[30, 90]

    # ---- 贝叶斯指数衰减参数 ----
    DECAY_HALFLIFE_EVENTS = 10             # 指数衰减半衰期（事件数），[5, 30]
    PRIOR_STRENGTH_EQUIVALENT_SAMPLES = 5  # 先验强度（等效样本数），[2, 10]

    # ---- 阈值调整控制 ----
    DEFAULT_ADJUSTMENT_STEP_PCT = 5         # 基础调整幅度（百分比），[1, 10]
    MIN_ADJUSTMENT_STEP_PCT = 2             # 最小调整幅度（百分比），[1, 5]
    MAX_ADJUSTMENT_STEP_PCT = 10            # 最大调整幅度（百分比），[5, 15]
    DEFAULT_MIN_INTERVAL_DAYS = 7           # 调整最小间隔天数，[1, 30]
    OVER_SENSITIVE_RATIO = 0.60             # “过度敏感”后验概率阈值，[0.3, 0.8]
    UNDER_SENSITIVE_RATIO = 0.60            # “反应不足”后验概率阈值，[0.3, 0.8]

    # ---- 振荡保护 ----
    OSCILLATION_DETECT_WINDOW = 3           # 振荡检测窗口（调整次数），[2, 5]
    OSCILLATION_LOCK_HOURS = 24             # 振荡锁定小时数，[12, 72]

    # ---- 市场状态 ----
    VALID_MARKET_REGIMES = [
        "trend_up", "trend_down", "oscillation",
        "high_volatility", "low_volatility", "default",
    ]

    # ---- 安全默认阈值（百分比振幅） ----
    DEFAULT_THRESHOLDS = {
        "trend_up": 5.0, "trend_down": 5.0,
        "oscillation": 3.5, "high_volatility": 8.0,
        "low_volatility": 3.0, "default": 5.0,
    }

    # ---- 硬边界 ----
    MIN_THRESHOLD_AMPLITUDE_PCT = 1.5
    MAX_THRESHOLD_AMPLITUDE_PCT = 12.0

    # ---- 宏观噪音过滤 ----
    EXTERNAL_EVENT_WINDOW_MINUTES = 10      # 宏观事件前后窗口（分钟），[5, 30]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        初始化适配器，可选传入配置字典覆盖类常量。
        """
        self._load_config(config)

        # 校验关键参数合法性
        self._validate_critical_params()

        # 当前阈值表
        self._thresholds: Dict[str, float] = dict(self.DEFAULT_THRESHOLDS)

        # 事件窗口
        self._event_windows: Dict[str, deque] = {
            r: deque(maxlen=self.DEFAULT_EVENT_WINDOW_SIZE)
            for r in self.VALID_MARKET_REGIMES
        }

        # 调整历史
        self._adjust_history: Dict[str, deque] = {
            r: deque(maxlen=10) for r in self.VALID_MARKET_REGIMES
        }
        self._last_adjustment_time: Dict[str, float] = {
            r: 0.0 for r in self.VALID_MARKET_REGIMES
        }
        self._oscillation_lock_until: Dict[str, float] = {
            r: 0.0 for r in self.VALID_MARKET_REGIMES
        }

        # 外部依赖
        self._behavioral_logger = None
        self._negotiation_bus = None

        # 降级计数器（用于健康监控）
        self._degradation_counters: Dict[str, int] = {
            "behavioral_logger_failures": 0,
            "negotiation_bus_failures": 0,
            "invalid_data_discarded": 0,
        }

        # 线程锁
        self._lock = threading.Lock()

        # 缓存
        self._event_stats_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_timestamp: float = 0.0

        # 启动校验阈值
        self._validate_initial_thresholds()

        logger.info("BayesianThresholdAdapter 初始化完成，管理 %d 种状态", len(self.VALID_MARKET_REGIMES))

    # ========== 配置加载 ==========
    def _load_config(self, config: Optional[Dict[str, Any]]) -> None:
        """从配置字典覆盖类常量，若无则保持默认值"""
        if config is None:
            return
        for key in ["DEFAULT_EVENT_WINDOW_SIZE", "DEFAULT_MIN_EVENTS_FOR_ADJUST",
                     "DECAY_HALFLIFE_EVENTS", "PRIOR_STRENGTH_EQUIVALENT_SAMPLES",
                     "DEFAULT_ADJUSTMENT_STEP_PCT", "MIN_ADJUSTMENT_STEP_PCT",
                     "MAX_ADJUSTMENT_STEP_PCT", "DEFAULT_MIN_INTERVAL_DAYS",
                     "OVER_SENSITIVE_RATIO", "UNDER_SENSITIVE_RATIO",
                     "OSCILLATION_DETECT_WINDOW", "OSCILLATION_LOCK_HOURS",
                     "DEFAULT_MAX_DATA_AGE_DAYS", "EXTERNAL_EVENT_WINDOW_MINUTES"]:
            if key in config:
                setattr(self, key, config[key])
                logger.debug(f"配置覆盖: {key}={config[key]}")

    def _validate_critical_params(self) -> None:
        """校验关键参数合法性"""
        if self.DECAY_HALFLIFE_EVENTS <= 0:
            logger.warning(f"DECAY_HALFLIFE_EVENTS 非法 ({self.DECAY_HALFLIFE_EVENTS})，重置为 10")
            self.DECAY_HALFLIFE_EVENTS = 10
        if self.PRIOR_STRENGTH_EQUIVALENT_SAMPLES <= 0:
            logger.warning(f"PRIOR_STRENGTH_EQUIVALENT_SAMPLES 非法，重置为 5")
            self.PRIOR_STRENGTH_EQUIVALENT_SAMPLES = 5

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        behavioral_logger: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
    ) -> None:
        if behavioral_logger is not None:
            if hasattr(behavioral_logger, 'log_event'):
                self._behavioral_logger = behavioral_logger
                logger.info("BehavioralLogger 注入成功")
            else:
                logger.warning("BehavioralLogger 缺少 log_event 方法")
        else:
            logger.warning("BehavioralLogger 未注入，审计功能降级")

        if negotiation_bus is not None:
            if hasattr(negotiation_bus, 'publish_alert'):
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")
            else:
                logger.warning("NegotiationBus 缺少 publish_alert 方法")
        else:
            logger.warning("NegotiationBus 未注入，告警降级")

    # ========== 公共接口 ==========
    def process_event(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        required = ["market_regime", "event_outcome", "triggered_threshold", "timestamp"]
        for field in required:
            if field not in event_data:
                return {"status": "error", "reason": f"缺少必需字段: {field}", "data": {}, "warnings": []}

        regime = str(event_data["market_regime"]).strip().lower()
        outcome = str(event_data["event_outcome"]).strip().lower()
        triggered_threshold = event_data["triggered_threshold"]
        timestamp = event_data["timestamp"]

        if regime not in self.VALID_MARKET_REGIMES:
            logger.warning(f"未知市场状态: {regime}，映射至 default")
            regime = "default"

        valid_outcomes = {"recovered", "worsened", "neutral"}
        if outcome not in valid_outcomes:
            logger.warning(f"无效事件结果: {outcome}，设为 neutral")
            outcome = "neutral"

        now = time.time()
        if not isinstance(timestamp, (int, float)) or timestamp > now + 60 or timestamp < now - self.DEFAULT_MAX_DATA_AGE_DAYS * 86400:
            logger.warning(f"事件时间戳异常: {timestamp}，使用当前时间并标记")
            timestamp = now
            event_data["_timestamp_corrected"] = True

        if not isinstance(triggered_threshold, (int, float)) or triggered_threshold <= 0:
            logger.warning(f"无效触发阈值: {triggered_threshold}，使用当前阈值")
            with self._lock:
                triggered_threshold = self._thresholds.get(regime, self.DEFAULT_THRESHOLDS["default"])

        # 宏观噪音标记
        is_contaminated = self._is_external_event_contamination(event_data)

        with self._lock:
            self._event_windows[regime].append({
                "outcome": outcome,
                "threshold": triggered_threshold,
                "timestamp": timestamp,
                "contaminated": is_contaminated,
            })
            self._cleanup_expired_events(regime)
            # 缓存失效
            self._cache_timestamp = 0.0

            adjust_needed, direction, stats = self._evaluate_adjustment(regime)
            warnings: List[str] = []

            result: Dict[str, Any] = {
                "regime": regime,
                "adjust_needed": adjust_needed,
                "adjust_direction": direction,
                "current_threshold": self._thresholds[regime],
                "event_count": stats["event_count"],
                "effective_samples": stats["effective_samples"],
                "posterior_prob_recovered": stats["p_recovered"],
                "posterior_prob_worsened": stats["p_worsened"],
                "last_adjustment_days_ago": (now - self._last_adjustment_time[regime]) / 86400,
                "adjust_applied": False,
            }

            if adjust_needed and direction != "hold":
                if self._is_oscillation_locked(regime):
                    logger.info(f"[{regime}] 振荡锁定中，跳过调整")
                    warnings.append("oscillation_locked")
                else:
                    old = self._thresholds[regime]
                    new = self._apply_adjustment(regime, direction, stats["effective_samples"])
                    result["old_threshold"] = old
                    result["new_threshold"] = new
                    result["adjust_applied"] = True
                    self._adjust_history[regime].append((now, direction, old, new))
                    if self._detect_oscillation(regime):
                        self._oscillation_lock_until[regime] = now + self.OSCILLATION_LOCK_HOURS * 3600
                        logger.warning(f"[{regime}] 检测到阈值振荡，锁定 {self.OSCILLATION_LOCK_HOURS}h")
                        warnings.append("oscillation_locked")

        self._log_event(regime, outcome, triggered_threshold, result)
        if result.get("adjust_applied"):
            self._notify_threshold_change(regime, result["old_threshold"], result["new_threshold"],
                                         stats, self._is_oscillation_locked(regime))

        return {"status": "ok", "reason": f"[{regime}] 事件处理完毕，方向: {direction}",
                "data": result, "warnings": warnings}

    def get_threshold(self, market_regime: str) -> Dict[str, Any]:
        regime = market_regime.strip().lower()
        if regime not in self.VALID_MARKET_REGIMES:
            regime = "default"

        with self._lock:
            threshold = self._thresholds[regime]
            stats = self._get_cached_event_stats(regime)
            last_adjust = self._last_adjustment_time[regime]
            locked = time.time() < self._oscillation_lock_until[regime]

        return {
            "status": "ok",
            "reason": f"返回 {regime} 阈值: {threshold:.2f}%",
            "data": {
                "market_regime": regime,
                "threshold_amplitude_pct": threshold,
                "min_hard_limit": self.MIN_THRESHOLD_AMPLITUDE_PCT,
                "max_hard_limit": self.MAX_THRESHOLD_AMPLITUDE_PCT,
                "event_summary": stats,
                "oscillation_locked": locked,
                "last_adjustment_time": last_adjust,
            },
            "warnings": ["oscillation_locked"] if locked else [],
        }

    def reset_to_defaults(self, market_regime: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            if market_regime is None:
                old_thresholds = dict(self._thresholds)
                self._thresholds = dict(self.DEFAULT_THRESHOLDS)
                for r in self.VALID_MARKET_REGIMES:
                    self._oscillation_lock_until[r] = 0.0
                    self._adjust_history[r].clear()
                    self._last_adjustment_time[r] = 0.0
                self._cache_timestamp = 0.0
                logger.info("已重置所有市场状态的阈值至默认值")
                return {"status": "ok", "reason": "所有阈值已重置",
                        "data": {"old_thresholds": old_thresholds}, "warnings": []}
            regime = market_regime.strip().lower()
            if regime not in self.VALID_MARKET_REGIMES:
                return {"status": "error", "reason": f"未知市场状态: {regime}", "data": {}, "warnings": []}
            old = self._thresholds[regime]
            self._thresholds[regime] = self.DEFAULT_THRESHOLDS[regime]
            self._oscillation_lock_until[regime] = 0.0
            self._adjust_history[regime].clear()
            self._last_adjustment_time[regime] = 0.0
            self._cache_timestamp = 0.0
            logger.info(f"[{regime}] 阈值已重置: {old:.2f}% -> {self._thresholds[regime]:.2f}%")
            return {"status": "ok", "reason": f"[{regime}] 阈值已重置",
                    "data": {"old_threshold": old, "new_threshold": self._thresholds[regime]}, "warnings": []}

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        try:
            with self._lock:
                regime_count = len(self._event_windows)
                total_events = sum(len(w) for w in self._event_windows.values())
                warnings: List[str] = []
                for regime, val in self._thresholds.items():
                    if val < self.MIN_THRESHOLD_AMPLITUDE_PCT or val > self.MAX_THRESHOLD_AMPLITUDE_PCT:
                        old = val
                        self._thresholds[regime] = max(self.MIN_THRESHOLD_AMPLITUDE_PCT,
                                                       min(self.MAX_THRESHOLD_AMPLITUDE_PCT, val))
                        msg = f"{regime} 阈值 {old:.2f}% 越界，已自动修复"
                        warnings.append(msg)
                        logger.warning(msg)
                        # 记录行为日志和通知（锁外调用可能死锁，故仅记录日志）
                stats = self._build_event_stats()
            # 锁外发送通知（简化处理）
            return {
                "status": "ok",
                "reason": f"BayesianThresholdAdapter 正常，管理 {regime_count} 状态，事件 {total_events}",
                "data": {
                    "regime_count": regime_count,
                    "total_events": total_events,
                    "thresholds": dict(self._thresholds),
                    "event_stats": stats,
                    "degradation_counters": dict(self._degradation_counters),
                    "dependencies": {
                        "behavioral_logger": self._behavioral_logger is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                    },
                },
                "warnings": warnings,
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和事件窗口结构")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": [f"health_check_failed:{e}"]}

    # ========== 私有方法 ==========
    def _validate_initial_thresholds(self) -> None:
        for regime, val in self._thresholds.items():
            if val < self.MIN_THRESHOLD_AMPLITUDE_PCT or val > self.MAX_THRESHOLD_AMPLITUDE_PCT:
                self._thresholds[regime] = max(self.MIN_THRESHOLD_AMPLITUDE_PCT,
                                               min(self.MAX_THRESHOLD_AMPLITUDE_PCT, val))
                logger.warning(f"初始阈值越界: {regime} {val} -> {self._thresholds[regime]}")

    def _is_external_event_contamination(self, event_data: Dict[str, Any]) -> bool:
        external_events = event_data.get("external_events")
        if not isinstance(external_events, list):
            return False
        event_ts = event_data.get("timestamp", 0)
        for ext in external_events:
            if isinstance(ext, dict) and "timestamp" in ext:
                if abs(event_ts - ext["timestamp"]) <= self.EXTERNAL_EVENT_WINDOW_MINUTES * 60:
                    return True
        return False

    def _cleanup_expired_events(self, regime: str) -> None:
        cutoff = time.time() - self.DEFAULT_MAX_DATA_AGE_DAYS * 86400
        window = self._event_windows[regime]
        count = 0
        while window and window[0]["timestamp"] < cutoff:
            window.popleft()
            count += 1
        if count:
            logger.debug(f"[{regime}] 清理 {count} 条过期事件")

    def _evaluate_adjustment(self, regime: str) -> Tuple[bool, str, Dict[str, Any]]:
        events = list(self._event_windows[regime])
        total = len(events)
        if total < self.DEFAULT_MIN_EVENTS_FOR_ADJUST:
            return False, "hold", {"event_count": total, "effective_samples": 0,
                                   "p_recovered": 0.5, "p_worsened": 0.5}
        clean_events = [e for e in events if not e.get("contaminated", False)]
        use_events = clean_events if len(clean_events) >= 5 else events
        if len(clean_events) < 5:
            logger.debug(f"[{regime}] 清洁事件不足，使用全部 {total} 个事件")

        n = len(use_events)
        if n == 0:
            return False, "hold", {"event_count": total, "effective_samples": 0,
                                   "p_recovered": 0.5, "p_worsened": 0.5}
        weights = np.exp(-np.arange(n)[::-1] / (self.DECAY_HALFLIFE_EVENTS / np.log(2)))
        total_weight = np.sum(weights)
        if total_weight < 1e-12:
            total_weight = 1.0
            weights = np.ones(n)
        recovered_weight = float(np.sum(weights * np.array([1 if e["outcome"] == "recovered" else 0 for e in use_events])))
        worsened_weight = float(np.sum(weights * np.array([1 if e["outcome"] == "worsened" else 0 for e in use_events])))
        prior = self.PRIOR_STRENGTH_EQUIVALENT_SAMPLES
        alpha = 1 + recovered_weight + prior / 2
        beta = 1 + worsened_weight + prior / 2
        p_recovered = alpha / (alpha + beta)
        p_worsened = beta / (alpha + beta)
        effective_samples = round(total_weight, 1)

        if p_recovered >= self.OVER_SENSITIVE_RATIO:
            return True, "loosen", {"event_count": total, "effective_samples": effective_samples,
                                    "p_recovered": round(p_recovered, 3), "p_worsened": round(p_worsened, 3)}
        if p_worsened >= self.UNDER_SENSITIVE_RATIO:
            return True, "tighten", {"event_count": total, "effective_samples": effective_samples,
                                     "p_recovered": round(p_recovered, 3), "p_worsened": round(p_worsened, 3)}
        return False, "hold", {"event_count": total, "effective_samples": effective_samples,
                               "p_recovered": round(p_recovered, 3), "p_worsened": round(p_worsened, 3)}

    def _apply_adjustment(self, regime: str, direction: str, effective_samples: float) -> float:
        current = self._thresholds[regime]
        min_samples = self.DEFAULT_MIN_EVENTS_FOR_ADJUST
        confidence = min(1.0, effective_samples / (min_samples * 2)) if min_samples > 0 else 0.5
        # 步长在 MIN 和 DEFAULT 之间线性插值，低置信度时步长接近 MIN
        step_pct = self.MIN_ADJUSTMENT_STEP_PCT + (self.DEFAULT_ADJUSTMENT_STEP_PCT - self.MIN_ADJUSTMENT_STEP_PCT) * confidence
        step_pct = min(self.MAX_ADJUSTMENT_STEP_PCT, max(self.MIN_ADJUSTMENT_STEP_PCT, step_pct))
        step = step_pct / 100.0
        if direction == "tighten":
            new = current * (1.0 - step)
        else:
            new = current * (1.0 + step)
        new = max(self.MIN_THRESHOLD_AMPLITUDE_PCT, min(self.MAX_THRESHOLD_AMPLITUDE_PCT, new))
        new = round(new, 2)
        self._thresholds[regime] = new
        self._last_adjustment_time[regime] = time.time()
        logger.info(f"[{regime}] 阈值调整: {current:.2f}% -> {new:.2f}% (方向={direction}, 置信度={confidence:.2f})")
        return new

    def _is_oscillation_locked(self, regime: str) -> bool:
        return time.time() < self._oscillation_lock_until[regime]

    def _detect_oscillation(self, regime: str) -> bool:
        history = list(self._adjust_history[regime])
        window = self.OSCILLATION_DETECT_WINDOW
        if len(history) < window + 1:
            return False
        recent = history[-window - 1:]
        dirs = [d for _, d, _, _ in recent]
        for i in range(len(dirs) - 1):
            if dirs[i] == dirs[i + 1]:
                return False
        return True

    def _build_event_stats(self) -> Dict[str, Dict[str, int]]:
        stats = {}
        for regime, win in self._event_windows.items():
            events = list(win)
            recovered = sum(1 for e in events if e["outcome"] == "recovered")
            worsened = sum(1 for e in events if e["outcome"] == "worsened")
            stats[regime] = {"total": len(events), "recovered": recovered, "worsened": worsened}
        return stats

    def _get_cached_event_stats(self, regime: str) -> Dict[str, int]:
        now = time.time()
        if self._cache_timestamp == 0 or now - self._cache_timestamp > 5:
            self._event_stats_cache = self._build_event_stats()
            self._cache_timestamp = now
        return self._event_stats_cache.get(regime, {"total": 0, "recovered": 0, "worsened": 0})

    def _log_event(self, regime: str, outcome: str, threshold: float, result: Dict[str, Any]) -> None:
        if self._behavioral_logger and hasattr(self._behavioral_logger, 'log_event'):
            try:
                self._behavioral_logger.log_event(
                    event_type="bayesian_threshold_event",
                    details={"regime": regime, "outcome": outcome, "threshold": threshold,
                             "adjust_applied": result.get("adjust_applied", False),
                             "new_threshold": result.get("new_threshold"), "direction": result.get("adjust_direction")},
                )
            except Exception as e:
                self._degradation_counters["behavioral_logger_failures"] += 1
                logger.warning(f"行为日志失败: {e}")

    def _notify_threshold_change(self, regime: str, old: float, new: float,
                                stats: Dict[str, Any], locked: bool) -> None:
        if self._negotiation_bus and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="threshold_adaptation",
                    regime=regime,
                    old_value=old, new_value=new,
                    reason=f"贝叶斯自适应: {old:.2f}% -> {new:.2f}%, "
                           f"p_rec={stats.get('p_recovered',0):.2f}, events={stats.get('event_count',0)}, "
                           f"locked={locked}",
                    timestamp=time.time(),
                )
            except Exception as e:
                self._degradation_counters["negotiation_bus_failures"] += 1
                logger.warning(f"告警推送失败: {e}")
