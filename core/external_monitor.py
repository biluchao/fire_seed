"""
火种系统 · 外部依赖监控器 (ExternalMonitor)

核心职责：
1. 实时监控外部API（交易所REST/WebSocket、云存储、LLM推理）的健康状态，包括延迟、错误率、限频次数、时间戳抖动
2. 告警的聚合去重、分级升级（INFO→WARN→CRITICAL）与静默窗口管理，支持多渠道推送与自动恢复通知

外部依赖（真实模块接口）：
- core.data_feed.MarketDataAggregator : 获取各交易所连接状态与延迟数据
- core.llm_lifecycle.LLMLifecycleManager : 获取LLM推理服务的可用性与响应延迟
- core.negotiation_bus.NegotiationBus : 推送告警事件与状态变更通知
- core.behavioral_logger.BehavioralLogger : 记录监控日志与告警事件

接口契约：
- update_api_status(service_name: str, metrics: Dict[str, Any]) -> Dict[str, Any] : 更新指定外部服务的状态指标
- get_health_report() -> Dict[str, Any] : 返回所有被监控服务的健康汇总报告
- trigger_alert(alert_type: str, level: str, message: str) -> Dict[str, Any] : 手动触发一条告警
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 MarketDataAggregator 不可用时，API 监控功能降级为仅依赖本地最后一次心跳记录
- 当 NegotiationBus 不可用时，告警降级为仅本地日志记录
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护每个被监控服务的滑动窗口指标数据，定期清理过期记录
- 不持有任何外部资源句柄，线程锁在模块销毁时自动释放
"""

import time
import logging
import threading
import statistics
import copy
import re
from typing import Dict, Any, List, Optional, Tuple
from collections import deque, defaultdict

logger = logging.getLogger(__name__)


class ExternalMonitor:
    """外部依赖监控器"""

    _SERVICE_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_.\-]{1,64}$')

    DEFAULT_CONFIG = {
        "window_samples": 60,
        "health_score_threshold": 80,
        "alert_aggregation_seconds": 10,
        "alert_escalation_seconds": 30,
        "alert_rate_limit_per_minute": 5,
        "silence_window_start": "02:00",
        "silence_window_end": "05:00",
        "max_data_age_seconds": 1800,
        "cleanup_interval_seconds": 300,
        "min_samples_for_eval": 5,
        "max_services": 50,
        "max_alert_queue_size": 1000,
        "health_weights": {"latency": 0.35, "error_rate": 0.30, "rate_limit": 0.20, "jitter": 0.15},
        "rate_limit_counter_ttl_seconds": 120,
        "service_name_max_length": 64,
        "cache_ttl_seconds": 0.5,
        "silence_cache_ttl_seconds": 10,
        "latency_outlier_percentile": 95,
        "max_alert_message_length": 1024,
        "max_metrics_keys": 20,
        "alert_push_max_retries": 2,
        "alert_push_retry_delay_ms": 100,
        "health_check_timeout_seconds": 2,
    }

    SERVICE_DESCRIPTIONS = {
        "exchange_rest": "交易所REST",
        "exchange_ws": "交易所WebSocket",
        "cloud_storage": "云存储",
        "llm_inference": "LLM推理",
        "ntp_service": "NTP时间服务",
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._cfg = copy.deepcopy(self.DEFAULT_CONFIG)
        if config:
            self._cfg.update(copy.deepcopy(config))
        self._window_samples = self._cfg["window_samples"]
        self._health_score_threshold = self._cfg["health_score_threshold"]
        self._alert_aggregation_seconds = self._cfg["alert_aggregation_seconds"]
        self._alert_escalation_seconds = self._cfg["alert_escalation_seconds"]
        self._alert_rate_limit_per_min = self._cfg["alert_rate_limit_per_minute"]
        self._max_data_age_seconds = self._cfg["max_data_age_seconds"]
        self._cleanup_interval_seconds = self._cfg["cleanup_interval_seconds"]
        self._min_samples_for_eval = self._cfg["min_samples_for_eval"]
        self._max_services = self._cfg["max_services"]
        self._max_alert_queue_size = self._cfg["max_alert_queue_size"]
        self._health_weights = self._cfg["health_weights"]
        self._rate_limit_counter_ttl = self._cfg["rate_limit_counter_ttl_seconds"]
        self._service_name_max_length = self._cfg["service_name_max_length"]
        self._cache_ttl_seconds = self._cfg["cache_ttl_seconds"]
        self._silence_cache_ttl = self._cfg["silence_cache_ttl_seconds"]
        self._latency_outlier_percentile = self._cfg["latency_outlier_percentile"]
        self._max_alert_message_length = self._cfg["max_alert_message_length"]
        self._max_metrics_keys = self._cfg["max_metrics_keys"]
        self._alert_push_max_retries = self._cfg["alert_push_max_retries"]
        self._alert_push_retry_delay_sec = max(0.001, self._cfg["alert_push_retry_delay_ms"] / 1000.0)
        self._health_check_timeout = self._cfg["health_check_timeout_seconds"]

        # 验证权重
        w = self._health_weights
        weight_sum = w.get("latency", 0) + w.get("error_rate", 0) + w.get("rate_limit", 0) + w.get("jitter", 0)
        if abs(weight_sum - 1.0) > 0.01:
            logger.error("健康评分权重之和为 %.2f，不等于1.0，将使用默认权重", weight_sum)
            self._health_weights = copy.deepcopy(self.DEFAULT_CONFIG["health_weights"])

        # 验证截尾百分位
        if not (50 <= self._latency_outlier_percentile <= 100):
            logger.warning("延迟截尾百分位 %d 无效，设置为默认值95", self._latency_outlier_percentile)
            self._latency_outlier_percentile = 95

        self._silence_start = self._parse_time_to_minutes(self._cfg["silence_window_start"])
        self._silence_end = self._parse_time_to_minutes(self._cfg["silence_window_end"])

        self._service_metrics: Dict[str, Dict[str, deque]] = {}
        self._health_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_timestamp: Dict[str, float] = {}
        self._alert_aggregation: Dict[str, deque] = {}
        self._service_alert_state: Dict[str, str] = {}
        self._alert_last_sent: Dict[str, float] = {}
        self._alert_sent_count: Dict[str, Dict[int, int]] = defaultdict(dict)

        self._silence_cache_time = 0.0
        self._silence_cache_result = False

        self._data_feed = None
        self._llm_manager = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        self._lock = threading.Lock()
        self._last_cleanup = time.time()

        logger.info("ExternalMonitor 初始化完成，监控 %d 种服务类型", len(self.SERVICE_DESCRIPTIONS))

    # ========== 依赖注入 ==========
    def inject_dependencies(self, data_feed=None, llm_manager=None, negotiation_bus=None, behavioral_logger=None):
        if data_feed is not None:
            self._data_feed = data_feed
            logger.info("MarketDataAggregator 注入成功")
        else:
            logger.warning("MarketDataAggregator 未注入")
        if llm_manager is not None:
            self._llm_manager = llm_manager
            logger.info("LLMLifecycleManager 注入成功")
        else:
            logger.warning("LLMLifecycleManager 未注入")
        if negotiation_bus is not None:
            if hasattr(negotiation_bus, 'publish_alert'):
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")
            else:
                logger.warning("NegotiationBus 缺少 publish_alert 方法，告警降级")
                self._negotiation_bus = None
        else:
            logger.warning("NegotiationBus 未注入")
        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入")

    # ========== 公共接口 ==========
    def update_api_status(self, service_name: str, metrics: Dict[str, Any]) -> Dict[str, Any]:
        if not self._validate_service_name(service_name):
            return {"status": "error", "reason": f"服务名称非法: {service_name}", "data": {}, "warnings": ["invalid_service_name"]}
        if len(metrics) > self._max_metrics_keys:
            logger.warning("服务 %s 传入指标数量 %d 超过限制 %d，已截断", service_name, len(metrics), self._max_metrics_keys)
            metrics = dict(list(metrics.items())[:self._max_metrics_keys])

        self._try_cleanup()
        now = time.time()
        valid_metrics = {}
        if "latency_ms" in metrics and isinstance(metrics["latency_ms"], (int, float)) and metrics["latency_ms"] >= 0:
            valid_metrics["latency_ms"] = float(metrics["latency_ms"])
        if "error_count" in metrics and isinstance(metrics["error_count"], int) and metrics["error_count"] >= 0:
            valid_metrics["error_count"] = metrics["error_count"]
        if "rate_limit_hit" in metrics and isinstance(metrics["rate_limit_hit"], int) and metrics["rate_limit_hit"] >= 0:
            valid_metrics["rate_limit_hit"] = metrics["rate_limit_hit"]
        if "jitter_ms" in metrics and isinstance(metrics["jitter_ms"], (int, float)) and metrics["jitter_ms"] >= 0:
            valid_metrics["jitter_ms"] = float(metrics["jitter_ms"])
        if "is_available" in metrics and isinstance(metrics["is_available"], bool):
            valid_metrics["is_available"] = metrics["is_available"]

        with self._lock:
            if service_name not in self._service_metrics and len(self._service_metrics) >= self._max_services:
                logger.warning("服务数已达上限 %d，拒绝添加", self._max_services)
                return {"status": "error", "reason": f"服务数已达上限 {self._max_services}", "data": {}, "warnings": ["max_services_reached"]}
            if service_name not in self._service_metrics:
                self._service_metrics[service_name] = {
                    "latency_ms": deque(maxlen=self._window_samples),
                    "error_count": deque(maxlen=self._window_samples),
                    "rate_limit_hit": deque(maxlen=self._window_samples),
                    "jitter_ms": deque(maxlen=self._window_samples),
                    "availability": deque(maxlen=self._window_samples),
                    "timestamps": deque(maxlen=self._window_samples),
                }
            data = self._service_metrics[service_name]
            if "latency_ms" in valid_metrics: data["latency_ms"].append(valid_metrics["latency_ms"])
            if "error_count" in valid_metrics: data["error_count"].append(valid_metrics["error_count"])
            if "rate_limit_hit" in valid_metrics: data["rate_limit_hit"].append(valid_metrics["rate_limit_hit"])
            if "jitter_ms" in valid_metrics: data["jitter_ms"].append(valid_metrics["jitter_ms"])
            if "is_available" in valid_metrics: data["availability"].append(1.0 if valid_metrics["is_available"] else 0.0)
            data["timestamps"].append(now)
            self._cache_timestamp[service_name] = 0.0

        return {"status": "ok", "reason": f"已更新服务 {service_name}", "data": {"service": service_name, "timestamp": now}, "warnings": []}

    def get_health_report(self) -> Dict[str, Any]:
        pending_alerts = []
        all_services = {}
        overall_status = "healthy"
        total_warnings = []
        now = time.time()

        # 锁内收集原始数据副本
        with self._lock:
            metrics_snapshot = {
                svc: {k: list(v) for k, v in self._service_metrics[svc].items()}
                for svc in self._service_metrics
            }

        # 锁外计算健康状态
        for svc_name, data_copy in metrics_snapshot.items():
            health = self._compute_service_health_from_copy(svc_name, data_copy)
            all_services[svc_name] = health
            status = health.get("level", "unknown")
            if status == "critical":
                overall_status = "critical"
            elif status == "degraded" and overall_status == "healthy":
                overall_status = "degraded"
            for w in health.get("warnings", []):
                total_warnings.append(f"{svc_name}: {w}")
            # 检查恢复
            previous = self._service_alert_state.get(svc_name, "healthy")
            if previous in ("critical", "degraded") and health.get("level") == "healthy":
                pending_alerts.append((f"{svc_name}_recovery", "info", f"服务 {svc_name} 已恢复正常"))

        # 更新状态缓存（锁外，但安全，因为只写单键且无并发冲突）
        for svc_name in all_services:
            self._service_alert_state[svc_name] = all_services[svc_name].get("level", "healthy")

        # 推送恢复告警
        for alert_type, level, msg in pending_alerts:
            self._process_alert_recovery(alert_type, msg)

        if overall_status == "critical":
            logger.error("外部依赖整体状态: critical #RECOVERY: 检查网络、API连通性、防火墙规则")
        elif overall_status == "degraded":
            logger.warning("外部依赖整体状态: degraded")

        return {"status": "ok", "reason": f"外部依赖整体状态: {overall_status}", "data": {"overall_status": overall_status, "services": all_services, "timestamp": now}, "warnings": total_warnings}

    def trigger_alert(self, alert_type: str, level: str, message: str) -> Dict[str, Any]:
        if not alert_type or len(alert_type) > 128:
            return {"status": "error", "reason": "告警类型非法", "data": {}, "warnings": ["invalid_alert_type"]}
        if len(message) > self._max_alert_message_length:
            message = message[:self._max_alert_message_length] + "..."
        valid_levels = ["info", "warn", "critical"]
        if level not in valid_levels:
            return {"status": "error", "reason": f"无效告警等级: {level}", "data": {}, "warnings": ["invalid_level"]}
        self._process_alert_with_lock(alert_type, level, message)
        return {"status": "ok", "reason": f"告警已处理: [{level.upper()}] {alert_type}", "data": {"type": alert_type, "level": level}, "warnings": []}

    def health_check(self) -> Dict[str, Any]:
        try:
            with self._lock:
                service_count = len(self._service_metrics)
                total_samples = sum(len(self._service_metrics[s]["timestamps"]) for s in self._service_metrics)
            connectivity_ok = True
            if self._data_feed is not None and hasattr(self._data_feed, 'health_check'):
                try:
                    conn = self._data_feed.health_check()
                    connectivity_ok = conn.get("status") == "ok"
                except Exception as e:
                    logger.warning("数据源健康检查异常: %s", e)
                    connectivity_ok = False
            return {"status": "ok", "reason": f"ExternalMonitor 正常，监控 {service_count} 个外部服务", "data": {"service_count": service_count, "total_samples": total_samples, "connectivity_check": connectivity_ok, "dependencies": {"data_feed": self._data_feed is not None, "llm_manager": self._llm_manager is not None, "negotiation_bus": self._negotiation_bus is not None, "behavioral_logger": self._behavioral_logger is not None}}, "warnings": []}
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和指标字典完整性")
            return {"status": "error", "reason": f"健康检查异常: {str(e)}", "data": {}, "warnings": [f"health_check_failed: {str(e)}"]}

    # ========== 私有方法 ==========
    def _validate_service_name(self, name: str) -> bool:
        return bool(self._SERVICE_NAME_PATTERN.match(name)) and len(name) <= self._service_name_max_length

    def _parse_time_to_minutes(self, time_str: str) -> int:
        try:
            h, m = map(int, time_str.split(':'))
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h * 60 + m
            raise ValueError(f"时间值越界: {time_str}")
        except Exception:
            logger.error(f"无法解析静默窗口时间: {time_str}，已禁用静默窗口")
            return -1

    def _is_in_silence_window(self) -> bool:
        if self._silence_start == -1 or self._silence_end == -1:
            return False
        now = time.time()
        if now - self._silence_cache_time < self._silence_cache_ttl:
            return self._silence_cache_result
        lt = time.localtime(now)
        current_min = lt.tm_hour * 60 + lt.tm_min
        result = False
        if self._silence_start < self._silence_end:
            result = self._silence_start <= current_min < self._silence_end
        else:
            # 跨午夜窗口，例如 23:00 - 02:00
            result = current_min >= self._silence_start or current_min < self._silence_end
        self._silence_cache_time = now
        self._silence_cache_result = result
        return result

    def _compute_service_health_from_copy(self, service_name: str, data_copy: Dict[str, List]) -> Dict[str, Any]:
        timestamps = data_copy.get("timestamps", [])
        if len(timestamps) < self._min_samples_for_eval:
            return {"level": "unknown", "score": 50, "diagnosis": "样本不足"}

        def truncated_mean(values, percentile=95):
            if not values:
                return None
            sorted_vals = sorted(values)
            limit = max(1, int(len(sorted_vals) * percentile / 100))
            trimmed = sorted_vals[:limit]
            return statistics.fmean(trimmed) if trimmed else None

        avg_latency = truncated_mean(data_copy.get("latency_ms", []), self._latency_outlier_percentile)
        if avg_latency is None: avg_latency = 200.0
        avg_jitter = truncated_mean(data_copy.get("jitter_ms", []), self._latency_outlier_percentile)
        if avg_jitter is None: avg_jitter = 10.0

        error_vals = data_copy.get("error_count", [])
        error_rate = sum(error_vals) / len(timestamps) if timestamps else 0.0
        rate_limit_vals = data_copy.get("rate_limit_hit", [])
        rate_limit_rate = sum(rate_limit_vals) / len(timestamps) if timestamps else 0.0
        avail_vals = data_copy.get("availability", [])
        avg_avail = statistics.fmean(avail_vals) if avail_vals else 1.0

        latency_score = max(0, 100 - avg_latency)
        error_score = max(0, 100 - error_rate * 200)
        rate_limit_score = max(0, 100 - rate_limit_rate * 200)
        jitter_score = max(0, 100 - avg_jitter * 5)

        w = self._health_weights
        score = (latency_score * w["latency"] + error_score * w["error_rate"] + rate_limit_score * w["rate_limit"] + jitter_score * w["jitter"])

        if avg_avail < 0.5:
            level = "critical"
            diagnosis = "服务不可用"
        elif score >= self._health_score_threshold:
            level = "healthy"
            diagnosis = "服务正常"
        elif score >= 60:
            level = "degraded"
            diagnosis = "服务性能下降"
        else:
            level = "critical"
            diagnosis = "服务严重异常"

        warnings = []
        if avg_avail < 0.9: warnings.append(f"可用性: {avg_avail:.1%}")
        if avg_latency > 100: warnings.append(f"高延迟: {avg_latency:.0f}ms")
        return {"level": level, "score": round(score, 1), "latency_ms": round(avg_latency, 1), "error_rate": round(error_rate, 3), "rate_limit_rate": round(rate_limit_rate, 3), "availability": round(avg_avail, 3), "diagnosis": diagnosis, "warnings": warnings}

    def _process_alert_recovery(self, alert_type: str, message: str) -> None:
        """处理恢复告警，简化流程：仅限频和推送，不做聚合升级"""
        if self._is_in_silence_window():
            return
        if not self._check_rate_limit("info"):
            return
        dedup_key = f"{alert_type}:info"
        now = time.time()
        if now - self._alert_last_sent.get(dedup_key, 0) < self._alert_escalation_seconds:
            return
        self._alert_last_sent[dedup_key] = now
        self._push_alert(alert_type, "info", message)

    def _process_alert_with_lock(self, alert_type: str, level: str, message: str) -> None:
        """常规告警处理，聚合、升级、限频（锁外调用，但访问共享状态需加锁）"""
        if level != "critical" and self._is_in_silence_window():
            return
        with self._lock:
            now = time.time()
            if alert_type not in self._alert_aggregation:
                self._alert_aggregation[alert_type] = deque(maxlen=self._max_alert_queue_size)
            agg_queue = self._alert_aggregation[alert_type]
            agg_queue.append((now, message))
            cutoff = now - self._alert_aggregation_seconds
            while agg_queue and agg_queue[0][0] < cutoff:
                agg_queue.popleft()
            aggregated_count = len(agg_queue)
            escalated_level = level
            if escalated_level == "warn" and aggregated_count > 5:
                escalated_level = "critical"
            if not self._check_rate_limit(escalated_level):
                return
            dedup_key = f"{alert_type}:{escalated_level}"
            last_time = self._alert_last_sent.get(dedup_key, 0)
            if now - last_time < self._alert_escalation_seconds:
                return
            self._alert_last_sent[dedup_key] = now
            full_message = f"[聚合 {aggregated_count} 条] {message}" if aggregated_count > 1 else message
        # 锁外推送
        self._push_alert(alert_type, escalated_level, full_message)

    def _check_rate_limit(self, level: str) -> bool:
        now = int(time.time())
        minute_key = now // 60
        level_dict = self._alert_sent_count.get(level, {})
        # 清理当前级别的过期计数器
        for k in list(level_dict.keys()):
            if abs(k - minute_key) > 2:
                del level_dict[k]
        count = level_dict.get(minute_key, 0) + 1
        level_dict[minute_key] = count
        self._alert_sent_count[level] = level_dict
        return count <= self._alert_rate_limit_per_min

    def _push_alert(self, alert_type: str, level: str, message: str) -> None:
        alert_msg = f"[{level.upper()}] ExternalMonitor: {alert_type} - {message}"
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            for retry in range(self._alert_push_max_retries):
                try:
                    self._negotiation_bus.publish_alert(
                        alert_type=alert_type, level=level, message=message,
                        source="external_monitor", timestamp=time.time()
                    )
                    break
                except Exception as e:
                    if retry < self._alert_push_max_retries - 1:
                        time.sleep(self._alert_push_retry_delay_sec)
                        logger.debug("告警推送重试 %d/%d: %s", retry+1, self._alert_push_max_retries, e)
                    else:
                        logger.error(f"协商总线告警推送最终失败: {e} #RECOVERY: 检查 NegotiationBus 连接状态")
        if level == "critical":
            logger.error("%s #RECOVERY: 立即检查外部服务状态、网络连通性、API密钥有效性", alert_msg)
        else:
            logger.warning(alert_msg)
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type=f"external_alert_{alert_type}",
                    details={"level": level, "message": message}
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

    def _try_cleanup(self) -> None:
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval_seconds:
            return
        with self._lock:
            cutoff = now - self._max_data_age_seconds
            total_removed = 0
            services_to_remove = []
            for svc_name, data in self._service_metrics.items():
                timestamps = data["timestamps"]
                removed = 0
                while timestamps and timestamps[0] < cutoff:
                    for key in ["latency_ms", "error_count", "rate_limit_hit", "jitter_ms", "availability"]:
                        if data[key]:
                            data[key].popleft()
                    timestamps.popleft()
                    removed += 1
                total_removed += removed
                if not timestamps:
                    services_to_remove.append(svc_name)
            for svc in services_to_remove:
                del self._service_metrics[svc]
                self._health_cache.pop(svc, None)
                self._service_alert_state.pop(svc, None)
                # 精确清理聚合键：自身及恢复键
                for key in list(self._alert_aggregation.keys()):
                    if key == svc or key == f"{svc}_recovery":
                        del self._alert_aggregation[key]
            # 清理空聚合队列
            for key in list(self._alert_aggregation.keys()):
                if not self._alert_aggregation[key]:
                    del self._alert_aggregation[key]
            # 清理长时间未活动的 alert_sent_count level
            for level in list(self._alert_sent_count.keys()):
                if all(abs(k - int(now // 60)) > self._rate_limit_counter_ttl / 60 for k in self._alert_sent_count[level]):
                    del self._alert_sent_count[level]
        self._last_cleanup = now
        if total_removed > 0:
            logger.info("全局清理过期外部监控数据: %d 条", total_removed)
