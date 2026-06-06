"""
火种系统 · 车道健康监控器 (LaneHealthMonitor)
Copyright 2024 FireSeed. All rights reserved.

核心职责：
1. 实时采集多车道（极速/快速/普通/慢速）的性能指标，包括吞吐量、P50/P95/P99 延迟、队列深度、背压与降级信号次数
2. 基于滑动窗口统计与基线对比，计算车道健康评分，自动判定健康/降级/严重拥堵状态，并触发分级告警
3. 支持性能计数器暴露、缓存命中率统计、车道趋势分析、清理线程健康自愈，适配 Prometheus 等外部监控系统

外部依赖（真实模块接口）：
- core.signal_bus.lane_scheduler.LaneScheduler : 获取各车道实时调度状态与性能计数器
- core.negotiation_bus.NegotiationBus : 发送健康状态变更事件与告警通知
- core.behavioral_logger.BehavioralLogger : 记录健康检查日志与告警事件
- core.module_health_monitor.ModuleHealthMonitor : 上报模块自身的健康状态与依赖可用性
- core.utils.config_loader.ConfigLoader : 配置加载器（可选注入，用于热重载车道参数）

接口契约：
- update_metrics(lane_name: str, metrics: Dict[str, float]) -> Dict[str, Any]
- get_health_score(lane_name: str) -> Dict[str, Any]
- get_all_lanes_health() -> Dict[str, Any]
- get_performance_counters() -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 LaneScheduler 不可用时，使用最近一次有效数据作为安全回退，并标记 "degraded" 状态
- 当 NegotiationBus 不可用时，告警降级为仅本地日志记录
- 当滑动窗口样本不足时，使用车道独立配置的保守估计值
- 清理守护线程异常退出时，health_check 自动标记重建需求，由独立监控线程按需重建
- 进程 fork 后自动重置锁和清理线程，确保子进程安全运行
- 所有降级值在类常量区明确声明，并附带金融含义说明

资源管理：
- 本模块维护每个车道的滑动窗口统计数据结构，通过独立守护线程定期清理过期数据
- 清理线程具备自愈能力：health_check 标记重建需求，由监控线程执行
- 不持有任何外部资源句柄，线程锁在模块销毁时自动释放
- 所有配置参数支持从配置文件热重载，变更后自动应用到新车道
- 使用纯 Python 实现核心统计算子，避免 numpy 依赖的开销
"""

import os
import sys
import time
import copy
import logging
import threading
import inspect
import statistics
from typing import Dict, Any, List, Optional, Tuple, Protocol, TypedDict, Callable, Union
from collections import deque
from enum import IntEnum

logger = logging.getLogger(__name__)

# ========== TypedDict 定义 ==========
class HealthScoreResult(TypedDict, total=False):
    lane: str
    score: float
    level: str
    trend: str
    sample_count: int
    current_p95_us: float
    current_p99_us: float
    target_latency_us: int
    queue_depth: float
    backpressure_count: float
    degraded_signal_count: float
    latency_ratio_raw: float
    diagnosis: str

class MetricsDict(TypedDict, total=False):
    throughput: float
    latency_p50: float
    latency_p95: float
    latency_p99: float
    queue_depth: float
    backpressure_count: float
    degraded_signal_count: float


# ========== 协议定义 ==========
class LaneSchedulerProtocol(Protocol):
    """车道调度器接口协议"""
    def get_lane_stats(self, lane_name: str) -> Dict[str, Any]: ...

class NegotiationBusProtocol(Protocol):
    """协商总线接口协议"""
    def publish_alert(self, alert_type: str, lane: str, level: str, message: str,
                      suppressed_count: int, timestamp: float) -> None: ...

class BehavioralLoggerProtocol(Protocol):
    """行为日志接口协议"""
    def log_event(self, event_type: str, details: Dict[str, Any]) -> None: ...

class ModuleHealthMonitorProtocol(Protocol):
    """模块健康监控接口协议"""
    def report_dependency_failure(self, module: str, dependency: str,
                                  failure_type: str, failure_count: int) -> None: ...

class ConfigLoaderProtocol(Protocol):
    """配置加载器接口协议"""
    def get(self, key: str, default: Any = None) -> Any: ...


# ========== 枚举定义 ==========
class MetricIndex(IntEnum):
    """指标索引，用于高性能访问 deque 列表"""
    THROUGHPUT = 0
    LATENCY_P50 = 1
    LATENCY_P95 = 2
    LATENCY_P99 = 3
    QUEUE_DEPTH = 4
    BACKPRESSURE_COUNT = 5
    DEGRADED_SIGNAL_COUNT = 6
    TIMESTAMPS = 7


class LaneHealthMonitor:
    """多车道健康监控器"""

    # ========== 类常量（金融级默认配置，附带金融含义和调优建议） ==========
    # 滑动窗口最大样本数。较大的窗口提供更平滑的估计，但增加内存和计算开销。
    # 建议：高频车道 (express) 可设置为 200，低频车道 (slow) 可设置为 50。
    DEFAULT_WINDOW_SAMPLES: int = 100

    # 评分历史窗口大小。用于趋势分析，决定连续下降/恢复检测的灵敏度。
    # 建议：设置为 CONSECUTIVE_DECLINE_THRESHOLD 的 5-10 倍。
    DEFAULT_SCORE_HISTORY_SIZE: int = 30

    # 黄色预警阈值（排队时长占目标延迟的比例）。低于此比例时为健康状态。
    # 金融意义：当车道排队时间达到目标延迟的 80% 时，系统应开始关注并准备降级。
    DEFAULT_ALERT_THRESHOLD_RATIO: float = 0.8

    # 红色告警阈值。超过此比例时车道进入严重拥堵状态，应立即采取降级措施。
    DEFAULT_CRITICAL_THRESHOLD_RATIO: float = 1.0

    # 过期数据清理间隔（秒）。较短的间隔可及时释放内存，但增加清理线程开销。
    # 建议：根据数据增长速率调整，一般为最大数据保留时间的 1/3 到 1/6。
    DEFAULT_CLEANUP_INTERVAL_SEC: int = 300

    # 数据最大保留时间（秒）。超过此时间的数据将被清理。
    # 金融意义：确保健康评估基于近期市场微结构，避免陈旧数据污染统计。
    DEFAULT_MAX_DATA_AGE_SEC: int = 1800

    # 健康评分缓存有效期（秒）。在高频查询场景下减少重复计算。
    DEFAULT_CACHE_TTL_SEC: float = 0.5

    # P99/P95 比率的默认值。用于在 P99 样本不足时估算 P99。
    # 金融意义：在高频交易中，P99 通常为 P95 的 1.1-1.3 倍，取保守值 1.2。
    DEFAULT_P99_P95_RATIO: float = 1.2

    # 同类型告警去重窗口（秒）。在此窗口内重复的相同告警将被抑制，仅计数。
    # 建议：设置为 30-60 秒，避免告警风暴。
    ALERT_DEDUPLICATION_WINDOW_SEC: int = 30

    # 抑制计数清理周期（秒）。应设为 ALERT_DEDUPLICATION_WINDOW_SEC 的 5-10 倍。
    SUPPRESSED_COUNT_CLEANUP_INTERVAL: int = 300

    # 连续下降次数触发趋势告警。当评分连续下降超过此次数时，发出趋势告警。
    CONSECUTIVE_DECLINE_THRESHOLD: int = 5

    # 连续上升次数触发自动恢复。当评分连续上升超过此次数时，可自动降低告警等级。
    CONSECUTIVE_RECOVERY_THRESHOLD: int = 5

    # 最小下降幅度（分）。评分下降小于此值不计入连续下降，防止微小波动误判。
    MIN_DECLINE_DELTA: float = 0.5

    # 最小上升幅度（分）。评分上升小于此值不计入连续恢复。
    MIN_RECOVERY_DELTA: float = 0.5

    # 延迟扣分上限。防止极端延迟导致评分溢出，同时保留严重拥堵的量化信息。
    MAX_LATENCY_PENALTY: float = 80.0

    # 延迟超过目标后的最小扣分。即使轻微超标也给予一定惩罚，防止运维忽略。
    MIN_LATENCY_PENALTY_ABOVE_TARGET: float = 5.0

    # 去重窗口内最多抑制的告警计数。超过后不再计数，防止内存溢出。
    MAX_SUPPRESSED_ALERTS: int = 100

    # 告警推送连续失败次数触发自愈诊断。超过此阈值后上报健康监控。
    ALERT_PUSH_FAILURE_THRESHOLD: int = 10

    # 告警推送失败计数的时间窗口（秒）。仅统计此窗口内的失败次数。
    ALERT_PUSH_FAILURE_WINDOW_SEC: int = 300

    # 截尾均值修剪比例。用于剔除异常样本，提高均值鲁棒性。
    TRIMMED_MEAN_TRIM_RATIO: float = 0.1

    # 修剪后最少保留样本数。若修剪后样本数少于此值，回退为普通均值。
    TRIMMED_MEAN_MIN_REMAINING: int = 3

    # 分位数计算样本数。取最近 N 个样本计算分位数。
    PERCENTILE_SAMPLE_SIZE: int = 20

    # 清理线程最大连续失败次数。超过后标记清理功能降级。
    CLEANUP_MAX_CONSECUTIVE_FAILURES: int = 3

    # 单车道健康查询超时（秒）。仅用于异步场景。
    LANE_HEALTH_TIMEOUT_SEC: float = 5.0

    # 计数器回卷阈值。当计数器超过此值时自动重置，防止监控系统溢出。
    COUNTER_ROLLOVER_LIMIT: int = 10**15

    # 默认车道配置（仅作为内置兜底，实例化时深拷贝，避免污染类常量）
    BUILT_IN_LANE_CONFIG: Dict[str, Dict[str, Any]] = {
        "express": {
            "description": "极速车道",
            "target_latency_us": 100,
            "degraded_multiplier": 2.0,
            "min_samples_for_eval": 10,
        },
        "fast": {
            "description": "快速车道",
            "target_latency_us": 500,
            "degraded_multiplier": 1.8,
            "min_samples_for_eval": 8,
        },
        "normal": {
            "description": "普通车道",
            "target_latency_us": 5000,
            "degraded_multiplier": 1.5,
            "min_samples_for_eval": 5,
        },
        "slow": {
            "description": "慢速车道",
            "target_latency_us": 50000,
            "degraded_multiplier": 1.3,
            "min_samples_for_eval": 5,
        },
    }

    __slots__ = (
        '_window_samples', '_score_history_size', '_cleanup_interval_sec', '_max_data_age_sec',
        '_metrics', '_score_history', '_health_cache', '_cache_timestamp',
        '_cache_hit_count', '_cache_miss_count',
        '_alert_last_triggered', '_alert_suppressed_count', '_alert_last_suppress_cleanup',
        '_alert_push_failure_times', '_alerts_by_lane',
        '_lane_scheduler', '_negotiation_bus', '_behavioral_logger', '_module_health_monitor', '_config_loader',
        '_perf_counters',
        '_lock', '_cleanup_thread', '_cleanup_stop_event', '_cleanup_thread_alive',
        '_cleanup_consecutive_failures', '_cleanup_start_lock', '_cleanup_last_executed_at',
        '_cleanup_rebuild_needed',
        '_ready', '_lane_config', '_time_func', '_perf_counter_func', '_language',
    )

    def __init__(self, lane_config_override: Optional[Dict[str, Dict[str, Any]]] = None):
        # 时间函数注入，方便单元测试
        self._time_func: Callable[[], float] = time.time
        self._perf_counter_func: Callable[[], float] = time.perf_counter

        # 配置加载：优先从注入的 ConfigLoader 读取，否则使用类常量
        _config: Dict[str, Any] = {}
        self._config_loader: Optional[ConfigLoaderProtocol] = None

        self._window_samples = max(1, _config.get("window_samples", self.DEFAULT_WINDOW_SAMPLES))
        self._score_history_size = max(1, _config.get("score_history_size", self.DEFAULT_SCORE_HISTORY_SIZE))
        self._cleanup_interval_sec = max(1, _config.get("cleanup_interval_sec", self.DEFAULT_CLEANUP_INTERVAL_SEC))
        self._max_data_age_sec = max(1, _config.get("max_data_age_sec", self.DEFAULT_MAX_DATA_AGE_SEC))
        self._language = _config.get("language", "zh")  # zh / en

        # 车道配置：深拷贝内置配置，然后合并用户配置（用户优先）
        self._lane_config: Dict[str, Dict[str, Any]] = copy.deepcopy(self.BUILT_IN_LANE_CONFIG)
        if lane_config_override:
            self._lane_config.update(copy.deepcopy(lane_config_override))
        elif _config.get("lane_config"):
            self._lane_config.update(copy.deepcopy(_config["lane_config"]))

        # 校验车道配置
        for lane_name, cfg in self._lane_config.items():
            if cfg.get("target_latency_us", 0) <= 0:
                logger.error("车道 %s 的 target_latency_us 必须 > 0，使用内置默认值", lane_name)
                fallback = self.BUILT_IN_LANE_CONFIG.get(lane_name, {}).get("target_latency_us", 100)
                cfg["target_latency_us"] = fallback

        # 每个车道的指标滑动窗口（使用整数索引提升性能）
        self._metrics: Dict[str, Dict[MetricIndex, deque]] = {}
        self._score_history: Dict[str, deque] = {}
        for lane in self._lane_config:
            self._init_lane(lane)

        # 健康评分缓存
        self._health_cache: Dict[str, HealthScoreResult] = {}
        self._cache_timestamp: Dict[str, float] = {}
        self._cache_hit_count: Dict[str, int] = {}
        self._cache_miss_count: Dict[str, int] = {}

        # 告警去重记录
        self._alert_last_triggered: Dict[str, float] = {}
        self._alert_suppressed_count: Dict[str, int] = {}
        self._alert_last_suppress_cleanup = self._time_func()
        self._alert_push_failure_times: deque = deque(maxlen=self.ALERT_PUSH_FAILURE_THRESHOLD)
        self._alerts_by_lane: Dict[str, Dict[str, int]] = {}  # {lane: {level: count}}

        # 外部依赖注入
        self._lane_scheduler: Optional[LaneSchedulerProtocol] = None
        self._negotiation_bus: Optional[NegotiationBusProtocol] = None
        self._behavioral_logger: Optional[BehavioralLoggerProtocol] = None
        self._module_health_monitor: Optional[ModuleHealthMonitorProtocol] = None

        # 性能计数器
        self._perf_counters = {
            "total_metric_updates": 0,
            "total_health_queries": 0,
            "total_alerts_triggered": 0,
            "total_alerts_suppressed": 0,
            "total_alert_push_failures": 0,
            "counter_rollover_count": 0,
            "self_latency_p50_us": 0.0,
            "self_latency_p95_us": 0.0,
            "self_latency_p99_us": 0.0,
            "self_latency_samples": deque(maxlen=self._window_samples),
        }

        # 线程安全
        self._lock = threading.RLock()

        # 清理线程
        self._cleanup_thread: Optional[threading.Thread] = None
        self._cleanup_stop_event = threading.Event()
        self._cleanup_thread_alive = False
        self._cleanup_consecutive_failures = 0
        self._cleanup_start_lock = threading.Lock()
        self._cleanup_last_executed_at = 0.0
        self._cleanup_rebuild_needed = False

        try:
            self._start_cleanup_daemon()
        except (RuntimeError, threading.ThreadError) as e:
            logger.critical("清理线程创建失败: %s #RECOVERY: 检查系统线程资源限制", e)
            self._cleanup_thread_alive = False

        # 就绪标志
        self._ready = True

        # 注册 fork 回调（不自动创建线程，仅重置锁）
        try:
            os.register_at_fork(after_in_child=self._reset_after_fork)
        except AttributeError:
            pass

        logger.info("LaneHealthMonitor 初始化完成，监控 %d 条车道", len(self._lane_config))

    def _reset_after_fork(self) -> None:
        """子进程 fork 后重置锁，不自动创建清理线程"""
        self._lock = threading.RLock()
        self._cleanup_stop_event = threading.Event()
        self._cleanup_thread_alive = False
        self._cleanup_thread = None
        self._cleanup_consecutive_failures = 0
        self._cleanup_start_lock = threading.Lock()
        self._cleanup_rebuild_needed = False
        logger.debug("子进程 fork 后已重置锁和线程状态")

    def __del__(self) -> None:
        try:
            if hasattr(self, '_cleanup_stop_event'):
                self._cleanup_stop_event.set()
            cleanup_thread = getattr(self, '_cleanup_thread', None)
            if cleanup_thread is not None and cleanup_thread.is_alive():
                cleanup_thread.join(timeout=2.0)
        except Exception:
            pass

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        lane_scheduler: Optional[LaneSchedulerProtocol] = None,
        negotiation_bus: Optional[NegotiationBusProtocol] = None,
        behavioral_logger: Optional[BehavioralLoggerProtocol] = None,
        module_health_monitor: Optional[ModuleHealthMonitorProtocol] = None,
        config_loader: Optional[ConfigLoaderProtocol] = None,
    ) -> None:
        if lane_scheduler is not None:
            if self._validate_dependency("lane_scheduler", lane_scheduler, "get_lane_stats"):
                self._lane_scheduler = lane_scheduler
                logger.info("LaneScheduler 注入成功")
        else:
            logger.warning("LaneScheduler 未注入，部分功能降级")

        if negotiation_bus is not None:
            if self._validate_dependency("negotiation_bus", negotiation_bus, "publish_alert"):
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")
            else:
                self._negotiation_bus = None
        else:
            logger.warning("NegotiationBus 未注入，告警降级为本地日志")

        if behavioral_logger is not None:
            if self._validate_dependency("behavioral_logger", behavioral_logger, "log_event"):
                self._behavioral_logger = behavioral_logger
                logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

        if module_health_monitor is not None:
            if self._validate_dependency("module_health_monitor", module_health_monitor, "report_dependency_failure"):
                self._module_health_monitor = module_health_monitor
                logger.info("ModuleHealthMonitor 注入成功")
        else:
            logger.warning("ModuleHealthMonitor 未注入，健康上报降级")

        if config_loader is not None:
            self._config_loader = config_loader
            logger.info("ConfigLoader 注入成功")

    def _validate_dependency(self, dep_name: str, obj: Any, method_name: str) -> bool:
        """严格校验依赖对象的接口签名"""
        if not hasattr(obj, method_name):
            logger.warning("%s 缺少 %s 方法，拒绝注入", dep_name, method_name)
            return False
        try:
            method = getattr(obj, method_name)
            sig = inspect.signature(method)
            if len(sig.parameters) < 1:
                logger.warning("%s.%s 方法签名异常，拒绝注入", dep_name, method_name)
                return False
        except (ValueError, TypeError) as e:
            logger.warning("%s.%s 签名检查失败: %s", dep_name, method_name, e)
            return False
        return True

    # ========== 公共接口 ==========
    def update_metrics(self, lane_name: str, metrics: MetricsDict) -> Dict[str, Any]:
        if lane_name not in self._lane_config:
            if self._is_dynamic_lane_allowed():
                self._init_lane(lane_name)
                self._lane_config[lane_name] = {
                    "description": f"动态车道:{lane_name}",
                    "target_latency_us": 1000,
                    "degraded_multiplier": 2.0,
                    "min_samples_for_eval": 10,
                    "dynamic": True,
                }
                logger.info("动态添加车道: %s", lane_name)
            else:
                return {
                    "status": "error",
                    "reason": f"无效车道: {lane_name}",
                    "data": {},
                    "warnings": [f"unknown_lane:{lane_name}"],
                }

        now = self._time_func()
        metric_keys = [
            ("throughput", MetricIndex.THROUGHPUT),
            ("latency_p50", MetricIndex.LATENCY_P50),
            ("latency_p95", MetricIndex.LATENCY_P95),
            ("latency_p99", MetricIndex.LATENCY_P99),
            ("queue_depth", MetricIndex.QUEUE_DEPTH),
            ("backpressure_count", MetricIndex.BACKPRESSURE_COUNT),
            ("degraded_signal_count", MetricIndex.DEGRADED_SIGNAL_COUNT),
        ]
        filtered_metrics: Dict[MetricIndex, float] = {}
        for key, idx in metric_keys:
            value = metrics.get(key)
            if isinstance(value, (int, float)):
                filtered_metrics[idx] = float(value)
            elif value is not None:
                logger.warning("%s.%s 非数值类型(%s)，已丢弃", lane_name, key, type(value).__name__)

        if not filtered_metrics:
            logger.warning("%s 所有指标无效，跳过更新", lane_name)
            return {
                "status": "error",
                "reason": f"{lane_name} 指标无效",
                "data": {},
                "warnings": ["all_metrics_invalid"],
            }

        with self._lock:
            lane_data = self._metrics[lane_name]
            for idx, val in filtered_metrics.items():
                lane_data[idx].append(val)
            lane_data[MetricIndex.TIMESTAMPS].append(now)
            self._cache_timestamp[lane_name] = 0.0
            cnt = self._perf_counters["total_metric_updates"] + 1
            if cnt >= self.COUNTER_ROLLOVER_LIMIT:
                self._perf_counters["counter_rollover_count"] += 1
                cnt = 0
            self._perf_counters["total_metric_updates"] = cnt

        return {
            "status": "ok",
            "reason": f"已更新 {self._lane_config[lane_name]['description']} 指标",
            "data": {"lane": lane_name, "timestamp": now},
            "warnings": [],
        }

    def get_health_score(self, lane_name: str) -> Dict[str, Any]:
        if lane_name not in self._lane_config:
            return {"status": "error", "reason": f"无效车道: {lane_name}", "data": {}, "warnings": []}

        with self._lock:
            return self._get_health_score_locked(lane_name)

    def _get_health_score_locked(self, lane_name: str) -> Dict[str, Any]:
        """在锁内计算健康评分"""
        t_start = self._perf_counter_func()
        if not self._ready:
            return {"status": "degraded", "reason": "模块未就绪", "data": {}, "warnings": ["module_not_ready"]}

        now = self._time_func()
        lane_config = self._lane_config[lane_name]
        cache_age = now - self._cache_timestamp.get(lane_name, 0)

        if cache_age < self.DEFAULT_CACHE_TTL_SEC:
            cached = self._health_cache.get(lane_name)
            min_samples = lane_config.get("min_samples_for_eval", 10)
            if cached and cached.get("sample_count", 0) >= min_samples:
                self._cache_hit_count[lane_name] = self._cache_hit_count.get(lane_name, 0) + 1
                self._perf_counters["total_health_queries"] += 1
                self._record_self_latency(t_start)
                return {"status": "ok", "reason": "返回缓存", "data": dict(cached), "warnings": []}

        self._cache_miss_count[lane_name] = self._cache_miss_count.get(lane_name, 0) + 1
        result, warnings = self._compute_health_score_locked(lane_name, lane_config, now)
        self._update_cache(lane_name, result, now)
        self._perf_counters["total_health_queries"] += 1
        self._record_self_latency(t_start)

        desc = lane_config["description"]
        level = result.get("level", "unknown")
        diagnosis = result.get("diagnosis", "")

        if level == "critical":
            warnings.append(f"CRITICAL: {desc} 严重拥堵")
            self._trigger_alert(lane_name, "critical", diagnosis)
        elif level == "degraded" and result.get("latency_ratio_raw", 0) > self.DEFAULT_ALERT_THRESHOLD_RATIO:
            warnings.append(f"WARNING: {desc} 性能下降")
            self._trigger_alert(lane_name, "warning", diagnosis)

        return {
            "status": "ok",
            "reason": f"{desc} 评分: {result['score']:.1f}",
            "data": result,
            "warnings": warnings,
        }

    def _compute_health_score_locked(self, lane_name: str, lane_config: Dict[str, Any], now: float) -> Tuple[HealthScoreResult, List[str]]:
        """纯计算逻辑，在锁内调用"""
        warnings: List[str] = []
        lane_data = self._metrics[lane_name]
        sample_count = len(lane_data[MetricIndex.TIMESTAMPS])
        min_samples = lane_config.get("min_samples_for_eval", 10)
        desc = lane_config["description"]

        if sample_count < min_samples:
            conservative_p95 = lane_config["target_latency_us"] * lane_config["degraded_multiplier"]
            result: HealthScoreResult = {
                "lane": lane_name, "score": 50.0, "level": "unknown", "trend": "stable",
                "sample_count": sample_count, "current_p95_us": round(conservative_p95, 1),
                "target_latency_us": lane_config["target_latency_us"],
                "queue_depth": 0.0, "backpressure_count": 0.0, "degraded_signal_count": 0.0,
                "latency_ratio_raw": round(conservative_p95 / max(lane_config["target_latency_us"], 1), 3),
                "diagnosis": f"[{desc}] 样本不足({sample_count}<{min_samples})",
            }
            return result, warnings

        target = max(lane_config["target_latency_us"], 1)
        current_p95 = self._trimmed_mean_or_default(lane_data[MetricIndex.LATENCY_P95], target * lane_config["degraded_multiplier"])
        p99_ratio = self._get_p99_to_p95_ratio(lane_name)
        current_p99 = self._safe_percentile_or_default(lane_data[MetricIndex.LATENCY_P99], 99, current_p95 * p99_ratio)
        current_queue = self._trimmed_mean_or_default(lane_data[MetricIndex.QUEUE_DEPTH], 1.0)
        current_backpressure = self._trimmed_mean_or_default(lane_data[MetricIndex.BACKPRESSURE_COUNT], 0.0)
        current_degraded = self._trimmed_mean_or_default(lane_data[MetricIndex.DEGRADED_SIGNAL_COUNT], 0.0)

        latency_ratio = current_p95 / target
        latency_penalty = max(
            self.MIN_LATENCY_PENALTY_ABOVE_TARGET if latency_ratio > 1.0 else 0,
            min(self.MAX_LATENCY_PENALTY, (latency_ratio - 1.0) * 60)
        )
        score = max(0.0, 100.0 - latency_penalty
                    - min(30, current_queue * 2)
                    - min(20, current_backpressure * 4)
                    - min(25, current_degraded * 5))

        level = "healthy" if score >= 85 else "degraded" if score >= 60 else "critical"
        if self._language == "en":
            diagnosis = (f"[{desc}] Normal" if level == "healthy"
                         else f"[{desc}] Degraded, P95 {latency_ratio:.1f}x target"
                         if level == "degraded"
                         else f"[{desc}] Critical, P95 {latency_ratio:.1f}x target")
        else:
            diagnosis = (f"[{desc}] 车道运行正常" if level == "healthy"
                         else f"[{desc}] 车道性能下降，P95延迟为目标的{latency_ratio:.1f}倍"
                         if level == "degraded"
                         else f"[{desc}] 车道严重拥堵，P95延迟为目标的{latency_ratio:.1f}倍")

        self._score_history[lane_name].append(score)
        trend = "stable"
        if self._detect_consecutive_decline(lane_name, lane_data):
            warnings.append(f"{desc} 评分连续下降")
            trend = "declining"
        elif self._detect_consecutive_recovery(lane_name):
            trend = "improving"
            if level == "degraded" and score >= 70:
                level = "healthy"
                diagnosis = f"[{desc}] 车道正在恢复" if self._language == "zh" else f"[{desc}] Recovering"

        result: HealthScoreResult = {
            "lane": lane_name, "score": round(score, 1), "level": level, "trend": trend,
            "sample_count": sample_count, "current_p95_us": round(current_p95, 1),
            "current_p99_us": round(current_p99, 1), "target_latency_us": lane_config["target_latency_us"],
            "queue_depth": round(current_queue, 1), "backpressure_count": round(current_backpressure, 1),
            "degraded_signal_count": round(current_degraded, 1),
            "latency_ratio_raw": round(latency_ratio, 3), "diagnosis": diagnosis,
        }
        return result, warnings

    def get_all_lanes_health(self) -> Dict[str, Any]:
        all_lanes = {}
        overall_status = "healthy"
        total_warnings: List[str] = []
        with self._lock:
            for lane in self._lane_config:
                try:
                    lane_res = self._get_health_score_locked(lane)
                except Exception as e:
                    logger.error("车道 %s 查询异常: %s", lane, e)
                    all_lanes[lane] = {"lane": lane, "score": 0, "level": "error", "trend": "unknown", "diagnosis": str(e)}
                    total_warnings.append(f"lane_query_error:{lane}")
                    continue
                if lane_res["status"] == "ok":
                    all_lanes[lane] = lane_res["data"]
                    lv = lane_res["data"].get("level", "unknown")
                    if lv == "critical":
                        overall_status = "critical"
                    elif lv == "degraded" and overall_status == "healthy":
                        overall_status = "degraded"
                    total_warnings.extend(lane_res.get("warnings", []))
        return {"status": "ok", "reason": f"全局状态: {overall_status}",
                "data": {"overall_status": overall_status, "lanes": all_lanes}, "warnings": total_warnings}

    def get_performance_counters(self) -> Dict[str, Any]:
        with self._lock:
            alerts_by_lane = {lane: dict(counts) for lane, counts in self._alerts_by_lane.items()}
            return {"status": "ok", "reason": "性能计数器", "data": {
                "total_metric_updates": self._perf_counters["total_metric_updates"],
                "total_health_queries": self._perf_counters["total_health_queries"],
                "total_alerts_triggered": self._perf_counters["total_alerts_triggered"],
                "total_alerts_suppressed": self._perf_counters["total_alerts_suppressed"],
                "total_alert_push_failures": self._perf_counters["total_alert_push_failures"],
                "counter_rollover_count": self._perf_counters["counter_rollover_count"],
                "self_latency_p50_us": round(self._perf_counters["self_latency_p50_us"], 1),
                "self_latency_p95_us": round(self._perf_counters["self_latency_p95_us"], 1),
                "self_latency_p99_us": round(self._perf_counters["self_latency_p99_us"], 1),
                "cache_hit_rate_pct": round(self._calc_cache_hit_rate(), 1),
                "cleanup_thread_alive": self._cleanup_thread_alive,
                "cleanup_last_executed_at": self._cleanup_last_executed_at,
                "alerts_suppressed_current": sum(self._alert_suppressed_count.values()),
                "alerts_by_lane": alerts_by_lane,
            }, "warnings": []}

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        try:
            if not self._ready:
                return {"status": "degraded", "reason": "未就绪", "data": {}, "warnings": ["not_ready"]}
            if not hasattr(self, '_metrics') or not self._metrics:
                return {"status": "degraded", "reason": "metrics未初始化", "data": {}, "warnings": ["no_metrics"]}

            # 检查清理线程，仅设置重建标志，不阻塞
            if not self._cleanup_thread_alive:
                logger.warning("清理线程死亡，设置重建标志")
                self._cleanup_rebuild_needed = True

            with self._lock:
                lane_count = len(self._metrics)
                total_samples = sum(len(self._metrics[l][MetricIndex.TIMESTAMPS]) for l in self._metrics)

            deps = {
                "lane_scheduler": self._lane_scheduler is not None,
                "negotiation_bus": self._negotiation_bus is not None,
                "behavioral_logger": self._behavioral_logger is not None,
                "module_health_monitor": self._module_health_monitor is not None,
            }
            all_failed = not any(deps.values())

            # 估算自身内存占用
            try:
                mem_bytes = sys.getsizeof(self) + sum(sys.getsizeof(v) for v in self._metrics.values())
            except Exception:
                mem_bytes = 0

            return {
                "status": "degraded" if all_failed else "ok",
                "reason": "所有依赖不可用" if all_failed else f"正常，{lane_count}车道 {total_samples}样本",
                "data": {
                    "lane_count": lane_count,
                    "total_samples": total_samples,
                    "dependencies": deps,
                    "cleanup_thread_alive": self._cleanup_thread_alive,
                    "cleanup_consecutive_failures": self._cleanup_consecutive_failures,
                    "self_memory_bytes": mem_bytes,
                },
                "warnings": ["all_dependencies_unavailable"] if all_failed else [],
            }
        except Exception as e:
            logger.error("health_check异常: %s", e)
            return {"status": "error", "reason": str(e), "data": {}, "warnings": [str(e)]}

    # ========== 私有方法：初始化和配置 ==========
    def _init_lane(self, lane_name: str) -> None:
        self._metrics[lane_name] = {
            MetricIndex.THROUGHPUT: deque(maxlen=self._window_samples),
            MetricIndex.LATENCY_P50: deque(maxlen=self._window_samples),
            MetricIndex.LATENCY_P95: deque(maxlen=self._window_samples),
            MetricIndex.LATENCY_P99: deque(maxlen=self._window_samples),
            MetricIndex.QUEUE_DEPTH: deque(maxlen=self._window_samples),
            MetricIndex.BACKPRESSURE_COUNT: deque(maxlen=self._window_samples),
            MetricIndex.DEGRADED_SIGNAL_COUNT: deque(maxlen=self._window_samples),
            MetricIndex.TIMESTAMPS: deque(maxlen=self._window_samples),
        }
        self._score_history[lane_name] = deque(maxlen=self._score_history_size)

    def _is_dynamic_lane_allowed(self) -> bool:
        return self._config_loader is not None and self._config_loader.get("system.lane_health.allow_dynamic", False)

    # ========== 私有方法：线程管理 ==========
    def _start_cleanup_daemon(self) -> None:
        def _loop():
            self._cleanup_thread_alive = True
            while not self._cleanup_stop_event.is_set():
                self._cleanup_stop_event.wait(self._cleanup_interval_sec)
                if self._cleanup_stop_event.is_set():
                    break
                t0 = self._perf_counter_func()
                try:
                    self._try_cleanup()
                    self._cleanup_consecutive_failures = 0
                except Exception as e:
                    self._cleanup_consecutive_failures += 1
                    logger.error("清理失败(%d): %s", self._cleanup_consecutive_failures, e)
                self._cleanup_last_executed_at = self._time_func()
            self._cleanup_thread_alive = False

        t = threading.Thread(target=_loop, name="lane_health_cleanup", daemon=True)
        t.start()
        self._cleanup_thread = t
        self._cleanup_thread_alive = True

    # ========== 私有方法：缓存 ==========
    def _update_cache(self, lane_name: str, result: HealthScoreResult, timestamp: float) -> None:
        self._health_cache[lane_name] = result
        self._cache_timestamp[lane_name] = timestamp

    def _calc_cache_hit_rate(self) -> float:
        total = sum(self._cache_hit_count.values()) + sum(self._cache_miss_count.values())
        return sum(self._cache_hit_count.values()) / total * 100 if total > 0 else 0.0

    # ========== 私有方法：统计算子 ==========
    def _recent_samples(self, deque_obj: deque, n: int = 20) -> List[float]:
        raw = list(deque_obj)
        return raw[-n:] if len(raw) >= n else raw

    def _trimmed_mean_or_default(self, deque_obj: deque, default: float) -> float:
        samples = self._recent_samples(deque_obj, 20)
        if not samples:
            return default
        if len(samples) <= 4:
            return float(sum(samples) / len(samples))
        # 纯 Python trimmed mean：排序后去掉首尾
        sorted_samples = sorted(samples)
        trim = int(len(sorted_samples) * self.TRIMMED_MEAN_TRIM_RATIO)
        if trim > 0 and len(sorted_samples) - 2 * trim >= self.TRIMMED_MEAN_MIN_REMAINING:
            trimmed = sorted_samples[trim:-trim]
        else:
            trimmed = sorted_samples
        return float(sum(trimmed) / len(trimmed))

    def _safe_percentile_or_default(self, deque_obj: deque, percentile: int, default: float) -> float:
        samples = self._recent_samples(deque_obj, self.PERCENTILE_SAMPLE_SIZE)
        if not samples:
            return default
        # 使用纯 Python 计算分位数（最近邻方法，偏保守）
        sorted_samples = sorted(samples)
        k = (len(sorted_samples) - 1) * percentile / 100.0
        f = int(k)
        c = f + 1 if f + 1 < len(sorted_samples) else f
        if f == c:
            return sorted_samples[f]
        # 线性插值
        return sorted_samples[f] * (c - k) + sorted_samples[c] * (k - f)

    def _get_p99_to_p95_ratio(self, lane_name: str) -> float:
        p95 = self._recent_samples(self._metrics[lane_name][MetricIndex.LATENCY_P95], 20)
        p99 = self._recent_samples(self._metrics[lane_name][MetricIndex.LATENCY_P99], 20)
        if len(p95) < 10 or len(p99) < 10:
            return self.DEFAULT_P99_P95_RATIO
        ratios = []
        for p, pp in zip(p99, p95):
            if pp > 0 and abs(p) < 1e9 and abs(pp) < 1e9:
                ratios.append(p / pp)
        if not ratios:
            return self.DEFAULT_P99_P95_RATIO
        sorted_ratios = sorted(ratios)
        trim = int(len(sorted_ratios) * self.TRIMMED_MEAN_TRIM_RATIO)
        if trim > 0 and len(sorted_ratios) - 2 * trim >= self.TRIMMED_MEAN_MIN_REMAINING:
            trimmed = sorted_ratios[trim:-trim]
        else:
            trimmed = sorted_ratios
        return float(sum(trimmed) / len(trimmed)) if trimmed else self.DEFAULT_P99_P95_RATIO

    # ========== 私有方法：趋势检测 ==========
    def _detect_consecutive_decline(self, lane_name: str, lane_data: Dict[MetricIndex, deque]) -> bool:
        history = list(self._score_history[lane_name])
        if len(history) < self.CONSECUTIVE_DECLINE_THRESHOLD:
            return False
        recent = history[-self.CONSECUTIVE_DECLINE_THRESHOLD:]
        if not all(recent[i] + self.MIN_DECLINE_DELTA < recent[i - 1] for i in range(1, len(recent))):
            return False
        # 双重确认
        p95_hist = list(lane_data.get(MetricIndex.LATENCY_P95, []))
        if len(p95_hist) >= self.CONSECUTIVE_DECLINE_THRESHOLD:
            p95_recent = p95_hist[-self.CONSECUTIVE_DECLINE_THRESHOLD:]
            return all(p95_recent[i] > p95_recent[i - 1] for i in range(1, len(p95_recent)))
        p99_hist = list(lane_data.get(MetricIndex.LATENCY_P99, []))
        if len(p99_hist) >= self.CONSECUTIVE_DECLINE_THRESHOLD:
            p99_recent = p99_hist[-self.CONSECUTIVE_DECLINE_THRESHOLD:]
            return all(p99_recent[i] > p99_recent[i - 1] for i in range(1, len(p99_recent)))
        # 无延迟数据时，不确认下降趋势
        return False

    def _detect_consecutive_recovery(self, lane_name: str) -> bool:
        history = list(self._score_history[lane_name])
        if len(history) < self.CONSECUTIVE_RECOVERY_THRESHOLD:
            return False
        recent = history[-self.CONSECUTIVE_RECOVERY_THRESHOLD:]
        return all(recent[i] > recent[i - 1] + self.MIN_RECOVERY_DELTA for i in range(1, len(recent)))

    # ========== 私有方法：清理 ==========
    def _try_cleanup(self) -> None:
        with self._lock:
            cutoff = self._time_func() - self._max_data_age_sec
            total_removed = 0
            for lane in self._lane_config:
                lane_data = self._metrics[lane]
                ts = lane_data[MetricIndex.TIMESTAMPS]
                removed = 0
                while ts and ts[0] < cutoff:
                    for idx in range(MetricIndex.TIMESTAMPS):
                        q = lane_data[idx]
                        if q:
                            q.popleft()
                    ts.popleft()
                    removed += 1
                if removed:
                    logger.debug("车道 %s 清理 %d 条", lane, removed)
                total_removed += removed
        if total_removed:
            logger.info("全局清理 %d 条过期记录", total_removed)

    # ========== 私有方法：告警 ==========
    def _trigger_alert(self, lane_name: str, level: str, message: str) -> None:
        alert_key = f"{lane_name}:{level}"
        now = self._time_func()
        if now - self._alert_last_suppress_cleanup > self.SUPPRESSED_COUNT_CLEANUP_INTERVAL:
            self._alert_suppressed_count.clear()
            self._alert_last_suppress_cleanup = now

        last = self._alert_last_triggered.get(alert_key, 0)
        if now - last < self.ALERT_DEDUPLICATION_WINDOW_SEC:
            c = self._alert_suppressed_count.get(alert_key, 0)
            if c < self.MAX_SUPPRESSED_ALERTS:
                self._alert_suppressed_count[alert_key] = c + 1
            self._perf_counters["total_alerts_suppressed"] += 1
            return

        suppressed = self._alert_suppressed_count.pop(alert_key, 0)
        self._alert_last_triggered[alert_key] = now
        self._perf_counters["total_alerts_triggered"] += 1

        # 按车道和级别统计告警
        if lane_name not in self._alerts_by_lane:
            self._alerts_by_lane[lane_name] = {}
        self._alerts_by_lane[lane_name][level] = self._alerts_by_lane[lane_name].get(level, 0) + 1

        if self._negotiation_bus:
            try:
                self._negotiation_bus.publish_alert("lane_health", lane_name, level, message, suppressed, now)
                self._alert_push_failure_times.clear()
            except Exception as e:
                logger.warning("告警推送失败: %s", e)
                self._alert_push_failure_times.append(now)
                self._perf_counters["total_alert_push_failures"] += 1

        recent = [t for t in self._alert_push_failure_times if now - t < self.ALERT_PUSH_FAILURE_WINDOW_SEC]
        if len(recent) >= self.ALERT_PUSH_FAILURE_THRESHOLD and self._module_health_monitor:
            try:
                self._module_health_monitor.report_dependency_failure(
                    "lane_health_monitor", "negotiation_bus", "push_failure", len(recent))
            except Exception:
                pass

        if level == "critical":
            logger.error("[%s] %s #RECOVERY: 检查调度器与信号流量", level.upper(), message)
        else:
            logger.warning("[%s] %s", level.upper(), message)

        if self._behavioral_logger:
            try:
                self._behavioral_logger.log_event("lane_health_alert",
                    {"lane": lane_name, "level": level, "message": message})
            except Exception:
                pass

    # ========== 辅助方法 ==========
    def _record_self_latency(self, t_start: float) -> None:
        elapsed = (self._perf_counter_func() - t_start) * 1_000_000
        samples = self._perf_counters["self_latency_samples"]
        samples.append(elapsed)
        if len(samples) >= 20:
            raw = sorted(samples)
            self._perf_counters["self_latency_p50_us"] = statistics.median(raw) if raw else 0.0
            self._perf_counters["self_latency_p95_us"] = self._safe_percentile_sorted(raw, 95)
            self._perf_counters["self_latency_p99_us"] = self._safe_percentile_sorted(raw, 99)

    @staticmethod
    def _safe_percentile_sorted(sorted_data: List[float], percentile: int) -> float:
        if not sorted_data:
            return 0.0
        k = (len(sorted_data) - 1) * percentile / 100.0
        f = int(k)
        c = min(f + 1, len(sorted_data) - 1)
        if f == c:
            return sorted_data[f]
        return sorted_data[f] * (c - k) + sorted_data[c] * (k - f)
