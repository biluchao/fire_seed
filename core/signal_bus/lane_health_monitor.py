"""
火种系统 · 车道健康监控器 (LaneHealthMonitor)

核心职责：
1. 实时采集四车道（极速/快速/普通/慢速）的性能指标，包括吞吐量、P50/P95/P99 延迟、队列深度、背压与降级信号次数
2. 基于滑动窗口统计与基线对比，计算车道健康评分，自动判定健康/降级/严重拥堵状态，并触发分级告警

外部依赖（真实模块接口）：
- core.signal_bus.lane_scheduler.LaneScheduler : 获取各车道实时调度状态与性能计数器
- core.negotiation_bus.NegotiationBus : 发送健康状态变更事件与告警通知
- core.behavioral_logger.BehavioralLogger : 记录健康检查日志与告警事件

接口契约：
- update_metrics(lane_name: str, metrics: Dict[str, float]) -> Dict[str, Any] : 更新指定车道的性能指标
- get_health_score(lane_name: str) -> Dict[str, Any] : 返回指定车道的健康评分及详细诊断
- get_all_lanes_health() -> Dict[str, Any] : 返回所有车道的健康状态汇总
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 LaneScheduler 不可用时，使用最近一次有效数据作为安全回退，并标记 "degraded" 状态
- 当 NegotiationBus 不可用时，告警降级为仅本地日志记录
- 当滑动窗口样本不足时，使用目标延迟的 1.5 倍作为保守估计值
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护每个车道的滑动窗口统计数据结构，定期清理过期数据
- 不持有任何外部资源句柄，线程锁在模块销毁时自动释放
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional
from collections import deque
import numpy as np

logger = logging.getLogger(__name__)


class LaneHealthMonitor:
    """四车道健康监控器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_WINDOW_SAMPLES = 100            # 滑动窗口最大样本数，无量纲，取值范围 [50, 500]
    DEFAULT_ALERT_THRESHOLD_RATIO = 0.8    # 黄色预警阈值（排队时长占目标延迟的比例），无量纲，[0.5, 1.0]
    DEFAULT_CRITICAL_THRESHOLD_RATIO = 1.0 # 红色告警阈值，无量纲，[0.8, 2.0]
    DEFAULT_CLEANUP_INTERVAL_SEC = 600     # 过期数据清理间隔，秒，[300, 3600]
    DEFAULT_MAX_DATA_AGE_SEC = 1800        # 数据最大保留时间，秒，[600, 7200]
    DEFAULT_CACHE_TTL_SEC = 0.5            # 健康评分缓存有效期，秒，[0.1, 2.0]
    MIN_SAMPLES_FOR_EVAL = 10              # 最小有效样本数，无量纲，[5, 50]
    DEGRADED_LATENCY_MULTIPLIER = 1.5     # 样本不足时，用目标延迟的倍数作为保守估计
    ALERT_DEDUP_WINDOW_SEC = 30            # 同类型告警去重窗口，秒，[10, 120]
    CONSECUTIVE_DECLINE_THRESHOLD = 5     # 连续下降次数触发趋势告警，无量纲，[3, 10]

    # 四车道目标延迟（微秒）
    LANE_LATENCY_TARGETS = {
        "express": 100,      # 极速车道: 100μs
        "fast": 500,         # 快速车道: 500μs
        "normal": 5000,      # 普通车道: 5ms
        "slow": 50000,       # 慢速车道: 50ms
    }

    LANE_DESCRIPTIONS = {
        "express": "极速车道",
        "fast": "快速车道",
        "normal": "普通车道",
        "slow": "慢速车道",
    }

    def __init__(self):
        # 每个车道的指标滑动窗口
        self._metrics: Dict[str, Dict[str, deque]] = {}
        for lane in self.LANE_LATENCY_TARGETS:
            self._metrics[lane] = {
                "throughput": deque(maxlen=self.DEFAULT_WINDOW_SAMPLES),
                "latency_p50": deque(maxlen=self.DEFAULT_WINDOW_SAMPLES),
                "latency_p95": deque(maxlen=self.DEFAULT_WINDOW_SAMPLES),
                "latency_p99": deque(maxlen=self.DEFAULT_WINDOW_SAMPLES),
                "queue_depth": deque(maxlen=self.DEFAULT_WINDOW_SAMPLES),
                "backpressure_count": deque(maxlen=self.DEFAULT_WINDOW_SAMPLES),
                "degraded_signal_count": deque(maxlen=self.DEFAULT_WINDOW_SAMPLES),
                "timestamps": deque(maxlen=self.DEFAULT_WINDOW_SAMPLES),
            }

        # 健康评分缓存
        self._health_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_timestamp: Dict[str, float] = {}

        # 历史评分（用于趋势分析）
        self._score_history: Dict[str, deque] = {
            lane: deque(maxlen=self.DEFAULT_WINDOW_SAMPLES) for lane in self.LANE_LATENCY_TARGETS
        }

        # 告警去重记录
        self._alert_last_triggered: Dict[str, float] = {}

        # 外部依赖注入
        self._lane_scheduler = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 线程安全（同时保护指标、缓存、告警去重等所有共享状态）
        self._lock = threading.Lock()

        # 清理定时器
        self._last_cleanup = time.time()

        logger.info("LaneHealthMonitor 初始化完成，监控 %d 条车道", len(self.LANE_LATENCY_TARGETS))

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        lane_scheduler: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）
        """
        if lane_scheduler is not None:
            self._lane_scheduler = lane_scheduler
            logger.info("LaneScheduler 注入成功")
        else:
            logger.warning("LaneScheduler 未注入，部分功能降级")

        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, 'publish_alert'):
                logger.warning("NegotiationBus 缺少 publish_alert 方法，告警推送不可用")
                self._negotiation_bus = None
            else:
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

    # ========== 公共接口 ==========
    def update_metrics(self, lane_name: str, metrics: Dict[str, float]) -> Dict[str, Any]:
        """
        更新指定车道的实时性能指标

        Args:
            lane_name: 车道名称 (express/fast/normal/slow)
            metrics: 性能指标字典，可包含 throughput, latency_p50, latency_p95, latency_p99,
                     queue_depth, backpressure_count, degraded_signal_count

        Returns:
            标准响应字典
        """
        if lane_name not in self.LANE_LATENCY_TARGETS:
            logger.warning(f"无效车道名称: {lane_name}")
            return {
                "status": "error",
                "reason": f"无效车道名称: {lane_name}，有效值为 {list(self.LANE_LATENCY_TARGETS.keys())}",
                "data": {},
                "warnings": [f"未知车道: {lane_name}"],
            }

        self._try_cleanup()
        now = time.time()

        # 仅允许有效的数值类型通过，拒绝 None、字符串等
        valid_keys = ["throughput", "latency_p50", "latency_p95", "latency_p99",
                      "queue_depth", "backpressure_count", "degraded_signal_count"]
        filtered_metrics = {}
        for key in valid_keys:
            value = metrics.get(key)
            if isinstance(value, (int, float)):
                filtered_metrics[key] = float(value)
            elif value is not None:
                logger.warning(f"{lane_name}.{key} 非数值类型({type(value).__name__})，已丢弃")

        with self._lock:
            lane_data = self._metrics[lane_name]
            for key, val in filtered_metrics.items():
                lane_data[key].append(val)
            lane_data["timestamps"].append(now)

            # 使缓存失效
            self._cache_timestamp[lane_name] = 0.0

        logger.debug(
            "更新车道指标: %s, queue_depth=%s, backpressure=%s",
            lane_name,
            filtered_metrics.get("queue_depth", "N/A"),
            filtered_metrics.get("backpressure_count", "N/A"),
        )

        return {
            "status": "ok",
            "reason": f"已更新 {self.LANE_DESCRIPTIONS.get(lane_name, lane_name)} 的性能指标",
            "data": {"lane": lane_name, "timestamp": now},
            "warnings": [],
        }

    def get_health_score(self, lane_name: str) -> Dict[str, Any]:
        """
        获取指定车道的健康评分与详细诊断

        Args:
            lane_name: 车道名称

        Returns:
            标准响应字典，data 中包含 score, level, diagnosis 等字段
        """
        if lane_name not in self.LANE_LATENCY_TARGETS:
            logger.warning(f"无效车道名称: {lane_name}")
            return {
                "status": "error",
                "reason": f"无效车道名称: {lane_name}",
                "data": {},
                "warnings": [f"未知车道: {lane_name}"],
            }

        with self._lock:
            now = time.time()
            cache_age = now - self._cache_timestamp.get(lane_name, 0)
            # 缓存有效性检查（在锁内读取，确保与写入一致）
            if cache_age < self.DEFAULT_CACHE_TTL_SEC:
                cached = self._health_cache.get(lane_name, {})
                if cached and cached.get("sample_count", 0) >= self.MIN_SAMPLES_FOR_EVAL:
                    return {
                        "status": "ok",
                        "reason": "返回缓存的健康评分",
                        "data": cached,
                        "warnings": [],
                    }

            lane_data = self._metrics[lane_name]
            timestamps = lane_data["timestamps"]
            sample_count = len(timestamps)

            # 样本不足时的保守估计
            if sample_count < self.MIN_SAMPLES_FOR_EVAL:
                target_latency = self.LANE_LATENCY_TARGETS[lane_name]
                conservative_p95 = target_latency * self.DEGRADED_LATENCY_MULTIPLIER
                result = {
                    "lane": lane_name,
                    "score": 50.0,
                    "level": "unknown",
                    "sample_count": sample_count,
                    "current_p95_us": round(conservative_p95, 1),
                    "target_latency_us": target_latency,
                    "queue_depth": 0,
                    "backpressure_count": 0,
                    "diagnosis": f"样本不足({sample_count}<{self.MIN_SAMPLES_FOR_EVAL})，使用保守估计",
                }
                self._update_cache(lane_name, result, now)
                return {
                    "status": "ok",
                    "reason": "样本不足，返回保守评分",
                    "data": result,
                    "warnings": ["insufficient_data"],
                }

            # 计算当前指标（最近10个样本均值）
            current_p95 = self._safe_mean(lane_data["latency_p95"])
            current_p99 = self._safe_mean(lane_data["latency_p99"])
            current_queue = self._safe_mean(lane_data["queue_depth"])
            current_backpressure = self._safe_mean(lane_data["backpressure_count"])

            # 为 None 的值补上保守默认值
            target_latency = self.LANE_LATENCY_TARGETS[lane_name]
            if current_p95 is None:
                current_p95 = target_latency * self.DEGRADED_LATENCY_MULTIPLIER
                logger.debug("%s 延迟样本缺失，使用降级值 %.1fμs", lane_name, current_p95)
            if current_p99 is None:
                current_p99 = current_p95 * 1.2
            if current_queue is None:
                current_queue = 1.0
            if current_backpressure is None:
                current_backpressure = 0.0

            # 健康评分计算
            latency_ratio = current_p95 / target_latency if target_latency > 0 else 1.0
            queue_penalty = min(30, current_queue * 2)
            backpressure_penalty = min(20, current_backpressure * 4)
            score = max(0.0, 100.0 - (latency_ratio - 0.5) * 40 - queue_penalty - backpressure_penalty)

            # 健康等级判定
            if score >= 85:
                level = "healthy"
                diagnosis = "车道运行正常"
            elif score >= 60:
                level = "degraded"
                diagnosis = f"车道性能下降，P95延迟为目标的{latency_ratio:.1f}倍"
            else:
                level = "critical"
                diagnosis = f"车道严重拥堵，P95延迟为目标的{latency_ratio:.1f}倍，队列深度={current_queue:.1f}"

            # 趋势分析
            self._score_history[lane_name].append(score)
            warnings = []
            if self._detect_consecutive_decline(lane_name):
                warnings.append(f"{self.LANE_DESCRIPTIONS[lane_name]} 评分连续下降，建议排查上游模块")

            result = {
                "lane": lane_name,
                "score": round(score, 1),
                "level": level,
                "sample_count": sample_count,
                "current_p95_us": round(current_p95, 1),
                "current_p99_us": round(current_p99, 1),
                "target_latency_us": target_latency,
                "queue_depth": round(current_queue, 1),
                "backpressure_count": round(current_backpressure, 1),
                "diagnosis": diagnosis,
            }
            self._update_cache(lane_name, result, now)

            # 分级告警（在锁内调用，确保告警去重原子性）
            if level == "critical":
                warnings.append(f"CRITICAL: {self.LANE_DESCRIPTIONS[lane_name]} 严重拥堵")
                self._trigger_alert(lane_name, "critical", diagnosis)
            elif level == "degraded":
                if latency_ratio > self.DEFAULT_ALERT_THRESHOLD_RATIO:
                    warnings.append(f"WARNING: {self.LANE_DESCRIPTIONS[lane_name]} 性能下降")
                    self._trigger_alert(lane_name, "warning", diagnosis)

            return {
                "status": "ok",
                "reason": f"{self.LANE_DESCRIPTIONS[lane_name]} 健康评分: {score:.1f}",
                "data": result,
                "warnings": warnings,
            }

    def get_all_lanes_health(self) -> Dict[str, Any]:
        """
        获取所有车道的健康状态汇总

        Returns:
            标准响应字典，data 中包含所有车道的健康摘要
        """
        all_lanes = {}
        overall_status = "healthy"
        total_warnings = []

        for lane in self.LANE_LATENCY_TARGETS:
            lane_res = self.get_health_score(lane)
            if lane_res["status"] == "ok":
                all_lanes[lane] = lane_res["data"]
                lane_level = lane_res["data"].get("level", "unknown")
                if lane_level == "critical":
                    overall_status = "critical"
                elif lane_level == "degraded" and overall_status == "healthy":
                    overall_status = "degraded"
                total_warnings.extend(lane_res.get("warnings", []))

        # 整体健康状态异常时记录 ERROR 日志
        if overall_status == "critical":
            logger.error("全局车道健康状态: critical #RECOVERY: 检查四车道调度器、降低信号流量、启用降级策略")
        elif overall_status == "degraded":
            logger.warning("全局车道健康状态: degraded")

        return {
            "status": "ok",
            "reason": f"全局车道健康状态: {overall_status}",
            "data": {
                "overall_status": overall_status,
                "lanes": all_lanes,
            },
            "warnings": total_warnings,
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检

        Returns:
            标准健康检查响应字典
        """
        try:
            if not hasattr(self, '_metrics') or not self._metrics:
                return {
                    "status": "degraded",
                    "reason": "metrics 数据结构未初始化",
                    "data": {},
                    "warnings": ["metrics_not_initialized"],
                }

            with self._lock:
                lane_count = len(self._metrics)
                total_samples = sum(len(self._metrics[l]["timestamps"]) for l in self._metrics)
                # 暴露各车道缓冲区使用率
                buffer_usage = {}
                for lane in self._metrics:
                    timestamps = self._metrics[lane]["timestamps"]
                    maxlen = timestamps.maxlen or self.DEFAULT_WINDOW_SAMPLES
                    buffer_usage[lane] = {
                        "used": len(timestamps),
                        "capacity": maxlen,
                        "usage_pct": round(len(timestamps) / maxlen * 100, 1) if maxlen > 0 else 0,
                    }

            return {
                "status": "ok",
                "reason": f"LaneHealthMonitor 正常，监控 {lane_count} 条车道，累计样本 {total_samples}",
                "data": {
                    "lane_count": lane_count,
                    "total_samples": total_samples,
                    "buffer_usage": buffer_usage,
                    "dependencies": {
                        "lane_scheduler": self._lane_scheduler is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和数据字典完整性")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _update_cache(self, lane_name: str, result: Dict[str, Any], timestamp: float) -> None:
        """更新健康评分缓存（需在锁内调用）"""
        self._health_cache[lane_name] = result
        self._cache_timestamp[lane_name] = timestamp

    def _safe_mean(self, deque_obj: deque) -> Optional[float]:
        """安全计算均值，空队列返回 None"""
        raw = list(deque_obj)
        recent = raw[-10:] if raw else []
        if not recent:
            return None
        return float(np.mean(recent))

    def _detect_consecutive_decline(self, lane_name: str) -> bool:
        """检测评分是否连续下降（需在锁内调用）"""
        history = list(self._score_history[lane_name])
        if len(history) < self.CONSECUTIVE_DECLINE_THRESHOLD:
            return False
        recent = history[-self.CONSECUTIVE_DECLINE_THRESHOLD:]
        return all(recent[i] < recent[i - 1] for i in range(1, len(recent)))

    def _try_cleanup(self) -> None:
        """定期清理过期数据"""
        now = time.time()
        if now - self._last_cleanup < self.DEFAULT_CLEANUP_INTERVAL_SEC:
            return

        with self._lock:
            # 记录清理前样本总数
            total_before = sum(len(self._metrics[l]["timestamps"]) for l in self.LANE_LATENCY_TARGETS)
            cutoff = now - self.DEFAULT_MAX_DATA_AGE_SEC
            total_removed = 0
            for lane in self.LANE_LATENCY_TARGETS:
                lane_data = self._metrics[lane]
                timestamps = lane_data["timestamps"]
                before_count = len(timestamps)
                removed = 0
                while timestamps and timestamps[0] < cutoff:
                    for key in ["throughput", "latency_p50", "latency_p95", "latency_p99",
                                "queue_depth", "backpressure_count", "degraded_signal_count"]:
                        queue = lane_data[key]
                        if queue:
                            queue.popleft()
                    timestamps.popleft()
                    removed += 1
                if removed:
                    after_count = len(timestamps)
                    logger.debug("车道 %s 清理 %d 条过期记录 (剩余 %d)", lane, removed, after_count)
                total_removed += removed

            total_after = sum(len(self._metrics[l]["timestamps"]) for l in self.LANE_LATENCY_TARGETS)

        self._last_cleanup = now
        if total_removed > 0:
            logger.info(
                "全局清理过期数据: %d 条 (清理前样本总数: %d, 清理后: %d)",
                total_removed, total_before, total_after
            )

    def _trigger_alert(self, lane_name: str, level: str, message: str) -> None:
        """
        触发告警（含去重机制，需在锁内调用以确保 _alert_last_triggered 原子性）
        """
        alert_key = f"{lane_name}:{level}"
        now = time.time()
        last_time = self._alert_last_triggered.get(alert_key, 0)
        if now - last_time < self.ALERT_DEDUP_WINDOW_SEC:
            logger.debug("告警去重: %s (距上次 %.1f 秒)", alert_key, now - last_time)
            return

        self._alert_last_triggered[alert_key] = now
        alert_msg = f"[{level.upper()}] {self.LANE_DESCRIPTIONS.get(lane_name, lane_name)}: {message}"

        # 尝试通过协商总线推送告警
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="lane_health",
                    lane=lane_name,
                    level=level,
                    message=message,
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning(f"协商总线告警推送失败: {e}")

        # 本地日志
        if level == "critical":
            logger.error(
                "%s #RECOVERY: 检查车道调度器、降低该车道信号流量、考虑动态核心借用",
                alert_msg
            )
        else:
            logger.warning(alert_msg)

        # 行为日志
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="lane_health_alert",
                    details={
                        "lane": lane_name,
                        "level": level,
                        "message": message,
                    },
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")
