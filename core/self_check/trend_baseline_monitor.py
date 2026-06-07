"""
火种系统 · 趋势基线监控器 (TrendBaselineMonitor)

核心职责：
1. 为系统关键指标维护滚动窗口，基于 Welford 在线统计算法（含 Kahan 补偿与定期缩放）动态计算基线
2. 实时检测指标偏离基线的异常，结合多维交叉验证、趋势分析、异常值降权与告警聚合，区分孤立异常与系统性风险

外部依赖（真实模块接口）：
- core.negotiation_bus.NegotiationBus : 异步发送健康状态变更事件与告警通知（需 publish_alert 方法）
- core.behavioral_logger.BehavioralLogger : 记录基线异常与交叉验证日志（需 log_event 方法）

接口契约：
- register_metric(name, max_samples, alert_sigma, cross_sigma, direction, outlier_damping) -> Dict[str, Any]
- unregister_metric(name) -> Dict[str, Any]
- update_metric(name, value, context) -> Dict[str, Any]
- batch_update_metrics(updates) -> Dict[str, Any]
- get_metric_health(name) -> Dict[str, Any]
- get_all_metrics_health() -> Dict[str, Any]
- reset_metric(name) -> Dict[str, Any]
- update_direction(name, direction) -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- numpy 不可用时自动降级为纯 Python Welford 算法
- NegotiationBus 不可用时告警降级为本地日志
- 样本不足时使用短期临时基线 + 静态阈值双重保障
- Welford 累加器定期缩放，防止浮点溢出；Kahan 补偿防止精度丢失

资源管理：
- 每个指标通过独立锁保护，支持高并发更新
- 全局锁仅保护指标注册/注销操作，持有时间极短
- 内存占用由 max_samples 控制（上限 50000）
- 异步告警推送线程池由信号量限制，防止线程爆炸
"""

import time
import logging
import threading
import math
from typing import Dict, Any, List, Optional, Tuple, Union
from collections import deque

logger = logging.getLogger(__name__)

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    logger.warning("numpy 未安装，使用纯 Python Welford 算法")

__all__ = ["TrendBaselineMonitor"]

# 统一警告码
WARN_INSUFFICIENT_BASELINE = "insufficient_baseline"
WARN_INVALID_VALUE = "invalid_value"
WARN_NOT_REGISTERED = "not_registered"
WARN_NOT_FOUND = "not_found"
WARN_EMPTY_NAME = "empty_name"
WARN_INVALID_DIRECTION = "invalid_direction"
WARN_INVALID_SIGMA = "invalid_sigma"
WARN_INVALID_DAMPING = "invalid_damping"
WARN_BATCH_TOO_LARGE = "batch_too_large"


class TrendBaselineMonitor:
    """趋势基线监控器，具备稳健统计、交叉验证、趋势分析与告警聚合能力"""

    # ========== 类常量（可通过构造函数配置覆盖） ==========
    DEFAULT_MAX_SAMPLES = 8640
    MAX_SAMPLES_LIMIT = 50000
    DEFAULT_ALERT_SIGMA = 2.5
    MIN_SAMPLES_FOR_BASELINE = 30
    ALERT_COOLDOWN_SEC = 60
    CRITICAL_ALERT_COOLDOWN_SEC = 10
    TREND_DETECTION_WINDOW = 5
    TREND_RATIO_THRESHOLD = 0.8
    OUTLIER_REJECTION_SIGMA = 4.0
    OUTLIER_DAMPING_WEIGHT = 0.2
    WELFORD_SCALE_INTERVAL = 100000
    ALERT_AGGREGATION_WINDOW_SEC = 5
    DEFAULT_CROSS_SIGMA = 3.5
    BATCH_MAX_SIZE = 50
    MAX_ASYNC_ALERT_THREADS = 4
    MIN_VARIANCE_FOR_STD = 1e-15
    AGGREGATION_LOG_SUPPRESS_SEC = 30

    # 可配置键列表（用于 _apply_config 自动发现）
    _CONFIGURABLE_KEYS = [
        "DEFAULT_MAX_SAMPLES", "DEFAULT_ALERT_SIGMA", "MIN_SAMPLES_FOR_BASELINE",
        "ALERT_COOLDOWN_SEC", "CRITICAL_ALERT_COOLDOWN_SEC", "TREND_DETECTION_WINDOW",
        "TREND_RATIO_THRESHOLD", "OUTLIER_REJECTION_SIGMA", "OUTLIER_DAMPING_WEIGHT",
        "DEFAULT_CROSS_SIGMA", "MAX_SAMPLES_LIMIT", "BATCH_MAX_SIZE",
        "MAX_ASYNC_ALERT_THREADS", "ALERT_AGGREGATION_WINDOW_SEC",
        "MIN_VARIANCE_FOR_STD", "AGGREGATION_LOG_SUPPRESS_SEC",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._metrics: Dict[str, Dict[str, Any]] = {}
        self._config: Dict[str, Dict[str, Any]] = {}
        self._alert_aggregator: Dict[str, List[Dict[str, Any]]] = {}
        self._aggregator_lock = threading.Lock()
        self._aggregator_last_flush = time.monotonic()
        self._aggregator_log_last: Dict[str, float] = {}
        self._correlation_rules: Dict[str, List[str]] = {}
        self._static_thresholds: Dict[str, float] = {}
        self._clamp_rules: Dict[str, Tuple[float, float]] = {}
        self._negotiation_bus = None
        self._behavioral_logger = None
        self._inject_lock = threading.Lock()
        self._global_lock = threading.Lock()
        self._alert_semaphore = threading.BoundedSemaphore(self.MAX_ASYNC_ALERT_THREADS)
        self._apply_config(config)
        logger.info("TrendBaselineMonitor 初始化完成 (numpy=%s)", HAS_NUMPY)

    # ========== 依赖注入 ==========
    def inject_dependencies(self, negotiation_bus=None, behavioral_logger=None) -> None:
        with self._inject_lock:
            if negotiation_bus is not None and hasattr(negotiation_bus, 'publish_alert'):
                self._negotiation_bus = negotiation_bus
            if behavioral_logger is not None and hasattr(behavioral_logger, 'log_event'):
                self._behavioral_logger = behavioral_logger

    # ========== 配置热重载 ==========
    def reload_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(config, dict):
            return {"status": "error", "reason": "配置必须是字典", "data": {}, "warnings": []}
        try:
            with self._global_lock:
                self._apply_config(config)
            logger.info("配置热重载完成")
            return {"status": "ok", "reason": "配置已更新", "data": {}, "warnings": []}
        except Exception as e:
            logger.error(f"配置热重载失败: {e}")
            return {"status": "error", "reason": f"配置热重载失败: {str(e)}", "data": {}, "warnings": []}

    # ========== 公共接口 ==========
    def register_metric(self, name: str, max_samples: int = DEFAULT_MAX_SAMPLES,
                       alert_threshold_sigma: float = DEFAULT_ALERT_SIGMA,
                       cross_sigma: Optional[float] = None,
                       direction: str = "above",
                       outlier_damping: Optional[float] = None) -> Dict[str, Any]:
        if not name:
            return {"status": "error", "reason": "指标名称不能为空", "data": {"name": name}, "warnings": [WARN_EMPTY_NAME]}
        direction = direction.lower().strip()
        if direction not in ("above", "below"):
            return {"status": "error", "reason": "direction 必须为 'above' 或 'below'", "data": {}, "warnings": [WARN_INVALID_DIRECTION]}
        if alert_threshold_sigma <= 0:
            return {"status": "error", "reason": "alert_threshold_sigma 必须大于 0", "data": {}, "warnings": [WARN_INVALID_SIGMA]}
        max_samples = max(self.MIN_SAMPLES_FOR_BASELINE, min(max_samples, self.MAX_SAMPLES_LIMIT))
        cross_sigma = cross_sigma or (alert_threshold_sigma * 1.4)
        damping = outlier_damping if outlier_damping is not None else self.OUTLIER_DAMPING_WEIGHT
        if not (0.0 <= damping <= 1.0):
            return {"status": "error", "reason": f"outlier_damping 必须在 [0.0, 1.0] 范围内，当前值: {damping}", "data": {}, "warnings": [WARN_INVALID_DAMPING]}

        with self._global_lock:
            if name in self._metrics:
                # 安全注销旧指标
                old_metric = self._metrics.pop(name, None)
                self._config.pop(name, None)
                with self._aggregator_lock:
                    self._alert_aggregator.pop(name, None)
                if old_metric is not None:
                    try:
                        with old_metric["lock"]:
                            pass  # 等待旧指标释放锁后丢弃
                    except Exception:
                        pass

            self._metrics[name] = {
                "buffer": deque(maxlen=max_samples),
                "lock": threading.Lock(),
                "sum": 0.0, "sum_sq": 0.0, "kahan_c": 0.0, "kahan_c_sq": 0.0, "count": 0,
                "last_value": None, "last_alert_time": 0.0, "last_critical_alert_time": 0.0,
                "outlier_damping": damping,
            }
            self._config[name] = {
                "max_samples": max_samples, "alert_sigma": alert_threshold_sigma,
                "cross_sigma": cross_sigma, "direction": direction,
            }

        logger.info("注册指标 %s (max_samples=%d, sigma=%.1f, direction=%s, damping=%.2f)",
                    name, max_samples, alert_threshold_sigma, direction, damping)
        return {"status": "ok", "reason": f"指标 {name} 注册成功",
                "data": {"name": name, "max_samples": max_samples, "alert_sigma": alert_threshold_sigma,
                         "cross_sigma": cross_sigma, "direction": direction, "outlier_damping": damping},
                "warnings": []}

    def unregister_metric(self, name: str) -> Dict[str, Any]:
        with self._global_lock:
            if name not in self._metrics:
                return {"status": "error", "reason": f"指标 {name} 不存在", "data": {}, "warnings": [WARN_NOT_FOUND]}
            self._metrics.pop(name, None)
            self._config.pop(name, None)
        with self._aggregator_lock:
            self._alert_aggregator.pop(name, None)
            self._aggregator_log_last.pop(name, None)
        logger.info("注销指标 %s", name)
        return {"status": "ok", "reason": f"指标 {name} 已注销", "data": {"name": name}, "warnings": []}

    def reset_metric(self, name: str) -> Dict[str, Any]:
        """重置指定指标的基线数据，保留配置不变"""
        with self._global_lock:
            if name not in self._metrics:
                return {"status": "error", "reason": f"指标 {name} 不存在", "data": {}, "warnings": [WARN_NOT_FOUND]}
            config = self._config[name]
            max_samples = config["max_samples"]
            outlier_damping = self._metrics[name].get("outlier_damping", self.OUTLIER_DAMPING_WEIGHT)
            old_metric = self._metrics.pop(name, None)
            try:
                with old_metric["lock"]:
                    pass
            except Exception:
                pass
            self._metrics[name] = {
                "buffer": deque(maxlen=max_samples),
                "lock": threading.Lock(),
                "sum": 0.0, "sum_sq": 0.0, "kahan_c": 0.0, "kahan_c_sq": 0.0, "count": 0,
                "last_value": None, "last_alert_time": 0.0, "last_critical_alert_time": 0.0,
                "outlier_damping": outlier_damping,
            }
        with self._aggregator_lock:
            self._alert_aggregator.pop(name, None)
            self._aggregator_log_last.pop(name, None)
        logger.info("重置指标 %s 基线", name)
        return {"status": "ok", "reason": f"指标 {name} 基线已重置", "data": {"name": name}, "warnings": []}

    def update_direction(self, name: str, direction: str) -> Dict[str, Any]:
        """动态修改指标的异常检测方向，不丢失历史基线"""
        direction = direction.lower().strip()
        if direction not in ("above", "below"):
            return {"status": "error", "reason": "direction 必须为 'above' 或 'below'", "data": {}, "warnings": [WARN_INVALID_DIRECTION]}
        with self._global_lock:
            if name not in self._config:
                return {"status": "error", "reason": f"指标 {name} 不存在", "data": {}, "warnings": [WARN_NOT_FOUND]}
            old = self._config[name]["direction"]
            self._config[name]["direction"] = direction
        logger.info("更新指标 %s 方向: %s -> %s", name, old, direction)
        return {"status": "ok", "reason": f"指标 {name} 方向已更新", "data": {"name": name, "old_direction": old, "new_direction": direction}, "warnings": []}

    def update_metric(self, name: str, value: float,
                     context: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        if name not in self._metrics:
            return {"status": "error", "reason": f"指标 {name} 未注册", "data": {}, "warnings": [WARN_NOT_REGISTERED]}
        if not math.isfinite(value):
            return {"status": "error", "reason": "非法数值 (NaN/Inf)", "data": {}, "warnings": [WARN_INVALID_VALUE]}
        if context is not None and not isinstance(context, dict):
            logger.debug("指标 %s 的 context 类型无效(%s)，已忽略", name, type(context).__name__)
            context = None

        value = self._clamp_value(name, value)
        metric = self._metrics[name]
        config = self._config[name]

        with metric["lock"]:
            buffer = metric["buffer"]
            buffer.append(value)
            self._update_welford(metric, value)
            metric["last_value"] = value
            sample_count = metric["count"]

        if sample_count < self.MIN_SAMPLES_FOR_BASELINE:
            is_anomaly = self._check_static_threshold(name, value, config["direction"])
            return {"status": "ok", "reason": f"样本不足({sample_count}<{self.MIN_SAMPLES_FOR_BASELINE})",
                    "data": {"name": name, "value": value, "is_anomaly": is_anomaly, "sigma_deviation": 0.0,
                             "baseline_mean": None, "baseline_std": None, "sample_count": sample_count,
                             "cross_validation_hit": False, "alert_level": "warning" if is_anomaly else "normal"},
                    "warnings": [WARN_INSUFFICIENT_BASELINE] if is_anomaly else []}

        with metric["lock"]:
            mean, std = self._compute_baseline_from_welford(metric, sample_count)

        # 标准差为零时的异常检测：若历史值完全相同且新值偏离，视为异常
        if std == 0.0 or std < self.MIN_VARIANCE_FOR_STD:
            if not math.isclose(value, mean, rel_tol=1e-9):
                sigma_dev = 10.0  # 标记为极端偏离
                is_anomaly = True
            else:
                sigma_dev = 0.0
                is_anomaly = False
        else:
            sigma_dev = abs(value - mean) / std
            direction = config["direction"]
            if direction == "above":
                is_anomaly = (value > mean) and (sigma_dev > config["alert_sigma"])
            else:
                is_anomaly = (value < mean) and (sigma_dev > config["alert_sigma"])

        cross_hit = self._check_cross_validation(name, context, config["cross_sigma"]) if (is_anomaly and context) else False
        trend_anomaly = self._detect_trend(name)

        if is_anomaly and (cross_hit or trend_anomaly):
            alert_level = "critical"
        elif is_anomaly:
            alert_level = "warning"
        else:
            alert_level = "normal"

        if is_anomaly:
            self._trigger_alert(name, value, sigma_dev, cross_hit, alert_level, metric)

        diagnosis = (f"指标 {name} 值 {value:.2f}，偏离 {sigma_dev:.1f}σ" +
                     ("，交叉验证异常" if cross_hit else "") +
                     ("，趋势异常" if trend_anomaly else ""))
        return {"status": "ok", "reason": diagnosis,
                "data": {"name": name, "value": value, "is_anomaly": is_anomaly, "sigma_deviation": round(sigma_dev, 2),
                         "baseline_mean": round(mean, 2), "baseline_std": round(std, 2), "sample_count": sample_count,
                         "cross_validation_hit": cross_hit, "trend_anomaly": trend_anomaly, "alert_level": alert_level},
                "warnings": [f"{name}_anomaly"] if is_anomaly else []}

    def batch_update_metrics(self, updates: List[Dict[str, Union[str, float, Dict]]]) -> Dict[str, Any]:
        if not isinstance(updates, list):
            return {"status": "error", "reason": "参数必须是列表", "data": {}, "warnings": ["invalid_type"]}
        if len(updates) > self.BATCH_MAX_SIZE:
            return {"status": "error", "reason": f"批量更新超过最大限制 ({self.BATCH_MAX_SIZE})", "data": {}, "warnings": [WARN_BATCH_TOO_LARGE]}
        results = []
        failed_indices = []
        for idx, item in enumerate(updates):
            if not isinstance(item, dict):
                results.append({"status": "error", "reason": f"第 {idx} 项不是字典"})
                failed_indices.append(idx)
                continue
            try:
                name = str(item["name"])
                value = float(item["value"])
                # 批量模式下使用轻量更新（跳过告警聚合，由调用方统一处理）
                results.append(self._update_metric_light(name, value, item.get("context")))
            except (KeyError, ValueError, TypeError) as e:
                results.append({"status": "error", "reason": str(e)})
                failed_indices.append(idx)
        summary = f"批量更新完成，{len(results)} 个指标，{len(failed_indices)} 个失败"
        return {"status": "ok" if len(failed_indices) == 0 else "partial",
                "reason": summary,
                "data": {"results": results, "failed_count": len(failed_indices), "failed_indices": failed_indices},
                "warnings": []}

    def _update_metric_light(self, name: str, value: float,
                             context: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """轻量更新：仅计算基线，不触发告警（用于批量更新内部）"""
        if name not in self._metrics:
            return {"status": "error", "reason": f"指标 {name} 未注册", "data": {}, "warnings": [WARN_NOT_REGISTERED]}
        if not math.isfinite(value):
            return {"status": "error", "reason": "非法数值", "data": {}, "warnings": [WARN_INVALID_VALUE]}
        value = self._clamp_value(name, value)
        metric = self._metrics[name]
        with metric["lock"]:
            metric["buffer"].append(value)
            self._update_welford(metric, value)
            metric["last_value"] = value
            sample_count = metric["count"]
        return {"status": "ok", "data": {"name": name, "sample_count": sample_count}, "warnings": []}

    def get_metric_health(self, name: str) -> Dict[str, Any]:
        if name not in self._metrics:
            return {"status": "error", "reason": f"指标 {name} 未注册", "data": {}, "warnings": [WARN_NOT_FOUND]}
        metric = self._metrics[name]
        with metric["lock"]:
            sample_count = metric["count"]
            if sample_count == 0:
                return {"status": "ok", "reason": f"指标 {name} 无样本",
                        "data": {"name": name, "sample_count": 0, "baseline_mean": None, "baseline_std": None,
                                 "last_value": None, "baseline_ready": False},
                        "warnings": []}
            mean, std = self._compute_baseline_from_welford(metric, sample_count)
            last_value = metric["last_value"]
        config = self._config.get(name, {})
        safe_config = {"max_samples": config.get("max_samples"),
                       "alert_sigma": config.get("alert_sigma"),
                       "cross_sigma": config.get("cross_sigma"),
                       "direction": config.get("direction")}
        return {"status": "ok", "reason": f"指标 {name} 基线: 均值 {mean:.2f}, 标准差 {std:.2f}",
                "data": {"name": name, "baseline_mean": round(mean, 2), "baseline_std": round(std, 2),
                         "sample_count": sample_count, "last_value": last_value,
                         "baseline_ready": sample_count >= self.MIN_SAMPLES_FOR_BASELINE,
                         "config": safe_config},
                "warnings": []}

    def get_all_metrics_health(self) -> Dict[str, Any]:
        """获取所有已注册指标的健康状态汇总（线程安全）"""
        with self._global_lock:
            names = list(self._metrics.keys())
        result = {}
        for name in names:
            metric = self._metrics.get(name)
            if metric is None:
                continue
            with metric["lock"]:
                sample_count = metric["count"]
                if sample_count == 0:
                    result[name] = {"sample_count": 0, "baseline_ready": False, "last_value": None}
                else:
                    mean, std = self._compute_baseline_from_welford(metric, sample_count)
                    result[name] = {"sample_count": sample_count, "baseline_mean": round(mean, 2),
                                    "baseline_std": round(std, 2), "last_value": metric["last_value"],
                                    "baseline_ready": sample_count >= self.MIN_SAMPLES_FOR_BASELINE}
        return {"status": "ok", "reason": f"返回 {len(result)} 个指标健康状态",
                "data": {"metrics": result}, "warnings": []}

    def health_check(self) -> Dict[str, Any]:
        try:
            with self._global_lock:
                metric_names = list(self._metrics.keys())
                total_samples = 0
                baseline_ready_count = 0
                insufficient_count = 0
                for m in self._metrics.values():
                    with m["lock"]:
                        cnt = m["count"]
                        total_samples += cnt
                        if cnt >= self.MIN_SAMPLES_FOR_BASELINE:
                            baseline_ready_count += 1
                        elif cnt > 0:
                            insufficient_count += 1

            return {"status": "ok",
                    "reason": f"TrendBaselineMonitor 正常，监控 {len(metric_names)} 个指标",
                    "data": {"metric_names": metric_names, "total_samples": total_samples,
                             "baseline_ready_count": baseline_ready_count,
                             "insufficient_count": insufficient_count,
                             "empty_count": len(metric_names) - baseline_ready_count - insufficient_count,
                             "dependencies": {"negotiation_bus": self._negotiation_bus is not None,
                                              "behavioral_logger": self._behavioral_logger is not None,
                                              "numpy_available": HAS_NUMPY}}, "warnings": []}
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和指标字典完整性", exc_info=True)
            return {"status": "error", "reason": f"健康检查异常: {str(e)}", "data": {}, "warnings": []}

    def __repr__(self) -> str:
        with self._global_lock:
            names = list(self._metrics.keys())[:10]
            count = len(self._metrics)
        return f"TrendBaselineMonitor(metrics={count}, sample_names={names})"

    # ========== 私有方法 ==========
    @staticmethod
    def _update_welford(metric: Dict[str, Any], value: float) -> None:
        """
        增量更新 Welford 在线统计算法（含 Kahan 补偿求和、异常值降权、定期缩放）
        """
        count = metric["count"]
        if count == 0:
            metric["sum"] = value
            metric["sum_sq"] = value * value
            metric["kahan_c"] = 0.0
            metric["kahan_c_sq"] = 0.0
            metric["count"] = 1
            return

        current_mean = metric["sum"] / count
        current_var = metric["sum_sq"] / (count - 1) if count > 1 else 0.0
        current_std = math.sqrt(max(0.0, current_var))
        damping = metric.get("outlier_damping", TrendBaselineMonitor.OUTLIER_DAMPING_WEIGHT)

        if current_std > 0:
            deviation = abs(value - current_mean) / current_std
            if deviation > TrendBaselineMonitor.OUTLIER_REJECTION_SIGMA:
                original = value
                value = current_mean + (value - current_mean) * damping
                logger.debug("异常值降权: 原值 %.4f, 偏离 %.1fσ, 降权后 %.4f", original, deviation, value)
        elif not math.isclose(value, current_mean, rel_tol=1e-9):
            # 零方差但新值偏离，使用保守降权
            original = value
            value = current_mean + (value - current_mean) * damping
            logger.debug("零方差偏离降权: 原值 %.4f, 均值 %.4f, 降权后 %.4f", original, current_mean, value)

        count += 1

        # Kahan 补偿求和 (sum)
        y = value - metric["kahan_c"]
        t = metric["sum"] + y
        metric["kahan_c"] = (t - metric["sum"]) - y
        metric["sum"] = t

        # Kahan 补偿求和 (sum_sq)
        sq_value = value * value
        y_sq = sq_value - metric["kahan_c_sq"]
        t_sq = metric["sum_sq"] + y_sq
        metric["kahan_c_sq"] = (t_sq - metric["sum_sq"]) - y_sq
        metric["sum_sq"] = t_sq

        metric["count"] = count

        # 定期缩放，防止累加器溢出
        if count % TrendBaselineMonitor.WELFORD_SCALE_INTERVAL == 0:
            scale = 1.0 / count
            metric["sum"] *= scale
            metric["sum_sq"] *= scale
            metric["kahan_c"] *= scale
            metric["kahan_c_sq"] *= scale
            logger.debug("Welford 累加器缩放完成，count=%d", count)

    @staticmethod
    def _compute_baseline_from_welford(metric: Dict[str, Any], count: int) -> Tuple[float, float]:
        if count == 0:
            return 0.0, 0.0
        mean = metric["sum"] / count
        if count == 1:
            return mean, 0.0
        variance = metric["sum_sq"] / (count - 1)
        if variance < 0.0:
            logger.warning("Welford 方差为负值 %.6e，已置零", variance)
            variance = 0.0
        if variance < TrendBaselineMonitor.MIN_VARIANCE_FOR_STD:
            variance = 0.0
        std = math.sqrt(variance)
        return mean, std

    def _check_static_threshold(self, name: str, value: float, direction: str) -> bool:
        # 优先精确匹配
        threshold = self._static_thresholds.get(name)
        if threshold is None:
            # 尝试最长前缀匹配
            best_match = ""
            best_len = 0
            for key in self._static_thresholds:
                if name.startswith(key) and len(key) > best_len:
                    best_len = len(key)
                    best_match = key
            threshold = self._static_thresholds.get(best_match)
        if threshold is None:
            return False
        if direction == "above":
            return value > threshold
        else:
            return value < threshold

    def _clamp_value(self, name: str, value: float) -> float:
        # 优先使用配置的钳制规则
        if name in self._clamp_rules:
            lo, hi = self._clamp_rules[name]
            return max(lo, min(hi, value))
        # 尝试最长前缀匹配
        for prefix, (lo, hi) in sorted(self._clamp_rules.items(), key=lambda x: -len(x[0])):
            if name.startswith(prefix):
                return max(lo, min(hi, value))

        nl = name.lower()
        if ("cpu" in nl and ("usage" in nl or "util" in nl)) or "cpu_usage" == nl:
            return max(0.0, min(100.0, value))
        if "memory" in nl and ("usage" in nl or "util" in nl):
            return max(0.0, min(100.0, value))
        if "latency" in nl or "delay" in nl:
            return max(0.0, value)
        if ("error" in nl and "rate" in nl) or "percent" in nl:
            return max(0.0, min(100.0, value))
        if "depth" in nl or ("queue" in nl and "length" in nl):
            return max(0.0, value)
        return value  # 未知指标不做钳制，保留原始值

    def _check_cross_validation(self, name: str, context: Dict[str, float], cross_sigma: float) -> bool:
        related = self._correlation_rules.get(name, [])
        for metric_name in related:
            m = self._metrics.get(metric_name)
            if m is None:
                logger.debug("交叉验证: 关联指标 %s 不存在，跳过", metric_name)
                continue
            val = context.get(metric_name)
            if val is None or not math.isfinite(val):
                continue
            with m["lock"]:
                cnt = m["count"]
                if cnt < self.MIN_SAMPLES_FOR_BASELINE:
                    static_threshold = self._static_thresholds.get(metric_name)
                    if static_threshold is not None:
                        assoc_config = self._config.get(metric_name, {})
                        assoc_dir = assoc_config.get("direction", "above")
                        if assoc_dir == "above" and val > static_threshold:
                            return True
                        elif assoc_dir == "below" and val < static_threshold:
                            return True
                    continue
                mean, std = self._compute_baseline_from_welford(m, cnt)
                if std > 0 and abs(val - mean) / std > cross_sigma:
                    return True
        return False

    def _detect_trend(self, name: str) -> bool:
        m = self._metrics[name]
        with m["lock"]:
            buffer = m["buffer"]
            buf_len = len(buffer)
            if buf_len < self.TREND_DETECTION_WINDOW:
                return False
            # 使用 itertools.islice 高效获取最后 N 个元素
            from itertools import islice
            start = buf_len - self.TREND_DETECTION_WINDOW
            recent = list(islice(buffer, start, buf_len))
        ups = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])
        downs = sum(1 for i in range(1, len(recent)) if recent[i] < recent[i - 1])
        total_pairs = len(recent) - 1
        if total_pairs == 0:
            return False
        return (ups / total_pairs) > self.TREND_RATIO_THRESHOLD or (downs / total_pairs) > self.TREND_RATIO_THRESHOLD

    def _trigger_alert(self, name: str, value: float, sigma_dev: float,
                       cross_hit: bool, level: str, metric: Dict[str, Any]) -> None:
        now = time.monotonic()
        cooldown = self.CRITICAL_ALERT_COOLDOWN_SEC if level == "critical" else self.ALERT_COOLDOWN_SEC

        with metric["lock"]:
            if level == "critical":
                if now - metric["last_critical_alert_time"] < cooldown:
                    return
                metric["last_critical_alert_time"] = now
            else:
                if now - metric["last_alert_time"] < cooldown:
                    return
                metric["last_alert_time"] = now

        ts = time.strftime("%H:%M:%S", time.localtime(time.time()))
        message = f"[{ts}] 指标 {name} 异常: 当前 {value:.2f}，偏离 {sigma_dev:.1f}σ"
        if cross_hit:
            message += "，交叉验证异常"

        self._aggregate_alert(name, message, level)

        # 异步推送告警
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            if self._alert_semaphore.acquire(blocking=False):
                t = threading.Thread(
                    target=self._safe_publish_alert,
                    args=(name, value, sigma_dev, level, message),
                    daemon=True,
                    name=f"alert-{name}-{int(now)}"
                )
                t.start()
            else:
                logger.debug("告警推送线程池已满，丢弃当前告警推送")

        if level == "critical":
            logger.error(f"[CRITICAL] {message} #RECOVERY: 检查关联服务、资源扩容")
        else:
            logger.warning(f"[WARNING] {message}")

        if self._behavioral_logger is not None and hasattr(self._behavioral_logger, 'log_event'):
            try:
                self._behavioral_logger.log_event(
                    event_type="baseline_anomaly",
                    details={"metric": name, "value": value, "level": level, "sigma": sigma_dev},
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

    def _safe_publish_alert(self, name: str, value: float, sigma_dev: float, level: str, message: str) -> None:
        """安全发布告警，在线程中执行，finally 确保信号量释放"""
        try:
            if self._negotiation_bus is not None:
                self._negotiation_bus.publish_alert(
                    alert_type="baseline_anomaly", metric=name,
                    value=value, sigma=sigma_dev, level=level, message=message,
                )
        except Exception as e:
            logger.warning(f"协商总线告警推送失败: {e}")
        finally:
            self._alert_semaphore.release()

    def _aggregate_alert(self, name: str, message: str, level: str) -> None:
        now = time.monotonic()
        with self._aggregator_lock:
            # 清理所有过期的告警条目
            cutoff = now - self.ALERT_AGGREGATION_WINDOW_SEC
            all_expired = []
            for key in list(self._alert_aggregator.keys()):
                self._alert_aggregator[key] = [
                    a for a in self._alert_aggregator[key]
                    if a["timestamp"] > cutoff
                ]
                if not self._alert_aggregator[key]:
                    all_expired.append(key)
            for key in all_expired:
                del self._alert_aggregator[key]

            if name not in self._alert_aggregator:
                self._alert_aggregator[name] = []
            self._alert_aggregator[name].append({"message": message, "level": level, "timestamp": now})

            # 抑制聚合日志频率
            last_log = self._aggregator_log_last.get(name, 0)
            count = len(self._alert_aggregator[name])
            if count >= 3 and (now - last_log) > self.AGGREGATION_LOG_SUPPRESS_SEC:
                logger.info("告警聚合: %s 在过去 %ds 触发 %d 次",
                           name, self.ALERT_AGGREGATION_WINDOW_SEC, count)
                self._aggregator_log_last[name] = now

    def _apply_config(self, config: Optional[Dict[str, Any]]) -> None:
        if not isinstance(config, dict):
            config = {}
        # 仅覆盖 _CONFIGURABLE_KEYS 中列出的类常量
        for key in self._CONFIGURABLE_KEYS:
            if key in config:
                val = config[key]
                # 合理性校验
                if key == "TREND_RATIO_THRESHOLD" and not (0.0 < val <= 1.0):
                    logger.warning("TREND_RATIO_THRESHOLD 必须在 (0, 1] 范围内，忽略配置值 %.2f", val)
                    continue
                if key.endswith("_SIGMA") and key != "DEFAULT_CROSS_SIGMA" and val <= 0:
                    logger.warning("%s 必须大于 0，忽略配置值 %.2f", key, val)
                    continue
                setattr(self, key, val)
        self._static_thresholds = config.get("static_thresholds", {
            "cpu_usage": 80.0, "memory_usage": 90.0,
        })
        self._correlation_rules = config.get("correlation_rules", {
            "cpu_usage": ["gc_frequency", "latency_p99", "factor_compute_time"],
            "memory_usage": ["swap_usage", "oom_events"],
            "latency_p99": ["error_rate", "backpressure_count"],
            "error_rate": ["latency_p99", "order_reject_rate"],
        })
        # 钳制规则：key -> (min, max)
        self._clamp_rules = config.get("clamp_rules", {})
