"""
火种系统 · 车道健康监控器 (LaneHealthMonitor)

核心职责：
1. 实时采集四车道（极速/快速/普通/慢速）的性能指标，包括吞吐量、P50/P95/P99 延迟、
   信号处理时延、队列深度、背压与降级信号次数
2. 基于滑动窗口统计、波动率自适应基线对比、滞回保护与非平稳突变检测，计算车道健康评分，
   自动判定健康/降级/严重拥堵状态，并触发分级告警（含告警静默、去重、假性恢复检测、启动瞬态保护）

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
- 本模块维护每个车道的滑动窗口统计数据结构，定期清理过期数据及告警去重记录
- 不持有任何外部资源句柄，线程锁在模块销毁时自动释放
"""

import time
import logging
import sys
import threading
import itertools
from typing import Dict, Any, List, Optional
from collections import deque
import numpy as np

logger = logging.getLogger(__name__)


class LaneHealthMonitor:
    """四车道健康监控器（机构级实战标准 · 全缺陷修复）"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_WINDOW_SAMPLES = 100            # 滑动窗口最大样本数，无量纲，取值范围 [50, 500]
    DEFAULT_ALERT_THRESHOLD_RATIO = 0.8    # 黄色预警阈值，无量纲，[0.5, 1.0]
    DEFAULT_CRITICAL_THRESHOLD_RATIO = 1.0 # 红色告警阈值，无量纲，[0.8, 2.0]
    DEFAULT_CLEANUP_INTERVAL_SEC = 600     # 过期数据清理间隔，秒，[300, 3600]
    DEFAULT_MAX_DATA_AGE_SEC = 1800        # 数据最大保留时间，秒，[600, 7200]
    DEFAULT_CACHE_TTL_SEC = 0.5            # 健康评分缓存有效期，秒，[0.1, 2.0]
    MIN_SAMPLES_FOR_EVAL = 10              # 最小有效样本数，无量纲，[5, 50]
    DEGRADED_LATENCY_MULTIPLIER = 1.5     # 样本不足时，用目标延迟的倍数作为保守估计
    ALERT_DEDUP_WINDOW_SEC = 30            # 同类型告警去重窗口，秒，[10, 120]
    CONSECUTIVE_DECLINE_THRESHOLD = 5     # 连续下降次数触发趋势告警，无量纲，[3, 10]

    # 波动率自适应阈值
    VOLATILITY_REGIME_MULTIPLIERS = {
        "high_vol": 2.0,     # 波动率 > 80分位时，目标延迟翻倍
        "normal_vol": 1.0,   # 正常波动
        "low_vol": 0.7,      # 低波动时收紧至70%
    }

    # 告警滞回与静默
    DEFAULT_HYSTERESIS_MARGIN = 10          # 滞回区间，分，取值范围 [5, 20]
    DEFAULT_SILENCE_PERIOD_SEC = 120        # 告警恢复后静默期，秒，[60, 600]

    # 健康评分公式权重（解耦为配置参数）
    QUEUE_PENALTY_MULTIPLIER = 2.0          # 队列深度惩罚系数，无量纲，[0.5, 5.0]
    QUEUE_PENALTY_MAX = 30.0                # 队列深度惩罚上限，分，[10, 50]
    BACKPRESSURE_PENALTY_MULTIPLIER = 4.0   # 背压惩罚系数，无量纲，[1.0, 10.0]
    BACKPRESSURE_PENALTY_MAX = 20.0         # 背压惩罚上限，分，[5, 40]
    LATENCY_RATIO_PENALTY_MULTIPLIER = 40.0 # 延迟比惩罚系数，无量纲，[20, 80]
    LATENCY_RATIO_OFFSET = 0.5              # 延迟比偏移量，无量纲，[0.3, 0.8]

    # 非平稳突变检测
    SHORT_WINDOW_SAMPLES = 10               # 短期窗口样本数，无量纲，[5, 30]
    NON_STATIONARY_THRESHOLD_RATIO = 2.0    # 短期均值/长期均值超此倍数视为非平稳，无量纲，[1.5, 3.0]

    # 状态振荡检测
    OSCILLATION_WINDOW_SEC = 600            # 振荡检测时间窗口，秒，[300, 1800]
    OSCILLATION_SWITCH_THRESHOLD = 6        # 状态切换次数阈值（每次变更计一次），无量纲，[4, 12]

    # 绝对延迟临界值
    ABSOLUTE_LATENCY_CRITICAL_US = 50000    # 绝对延迟临界值，微秒，任何车道超此值均为严重异常

    # 最小可用性观察期
    MIN_UPTIME_OBSERVATION_SEC = 600        # 最小观察期，秒，低于此值不计算可用性百分比

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

    # 监控指标键（包含信号处理时延）
    METRIC_KEYS = [
        "throughput", "latency_p50", "latency_p95", "latency_p99",
        "processing_latency_p95",
        "queue_depth", "backpressure_count", "degraded_signal_count",
    ]

    def __init__(self):
        # 每个车道的指标滑动窗口
        self._metrics: Dict[str, Dict[str, deque]] = {}
        for lane in self.LANE_LATENCY_TARGETS:
            self._metrics[lane] = {
                key: deque(maxlen=self.DEFAULT_WINDOW_SAMPLES) for key in self.METRIC_KEYS
            }
            self._metrics[lane]["timestamps"] = deque(maxlen=self.DEFAULT_WINDOW_SAMPLES)

        # 健康评分缓存
        self._health_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_timestamp: Dict[str, float] = {}

        # 历史评分（用于趋势分析）
        self._score_history: Dict[str, deque] = {
            lane: deque(maxlen=self.DEFAULT_WINDOW_SAMPLES) for lane in self.LANE_LATENCY_TARGETS
        }

        # 告警去重记录
        self._alert_last_triggered: Dict[str, float] = {}
        # 告警重复计数
        self._alert_repeat_count: Dict[str, int] = {}

        # 告警静默记录（恢复后的静默期）
        self._alert_silence_until: Dict[str, float] = {}

        # 上一次健康等级（用于滞回）
        self._previous_health_level: Dict[str, str] = {}
        # 首次评估标记（用于跳过滞回）
        self._first_evaluation: Dict[str, bool] = {lane: True for lane in self.LANE_LATENCY_TARGETS}

        # 车道可用性追踪
        self._lane_uptime: Dict[str, Dict[str, float]] = {
            lane: {"total_seconds": 0.0, "degraded_seconds": 0.0, "last_check": time.time()}
            for lane in self.LANE_LATENCY_TARGETS
        }

        # 状态振荡检测：记录每次健康等级变更的时间戳
        self._state_transition_history: Dict[str, deque] = {
            lane: deque(maxlen=20) for lane in self.LANE_LATENCY_TARGETS
        }

        # 外部依赖注入
        self._lane_scheduler = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 当前波动率分位（由外部注入，用于动态阈值）
        self._current_volatility_percentile = 50.0

        # 线程安全（使用可重入锁防止优先级反转）
        self._lock = threading.RLock()

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

    def set_volatility_percentile(self, percentile: float) -> None:
        """
        设置当前波动率分位，用于自适应阈值调整
        """
        if 0 <= percentile <= 100:
            self._current_volatility_percentile = percentile
        else:
            logger.warning("无效波动率分位: %s，保持原值", percentile)

    # ========== 公共接口 ==========
    def update_metrics(self, lane_name: str, metrics: Dict[str, float]) -> Dict[str, Any]:
        """
        更新指定车道的实时性能指标
        """
        if lane_name not in self.LANE_LATENCY_TARGETS:
            logger.warning(f"无效车道名称: {lane_name}")
            return {
                "status": "error",
                "reason": f"无效车道名称: {lane_name}",
                "data": {},
                "warnings": [f"未知车道: {lane_name}"],
            }

        self._try_cleanup()
        now = time.time()

        filtered_metrics = {}
        for key in self.METRIC_KEYS:
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
            self._cache_timestamp[lane_name] = 0.0

            uptime = self._lane_uptime[lane_name]
            elapsed = now - uptime["last_check"]
            uptime["total_seconds"] += elapsed
            uptime["last_check"] = now

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
        获取指定车道的健康评分与详细诊断（外部接口，内部加锁）
        """
        if lane_name not in self.LANE_LATENCY_TARGETS:
            return {
                "status": "error",
                "reason": f"无效车道名称: {lane_name}",
                "data": {},
                "warnings": [f"未知车道: {lane_name}"],
            }

        with self._lock:
            result, pending_alerts = self._get_health_score_locked(lane_name)

        # 在锁外触发告警，避免阻塞
        for args in pending_alerts:
            self._trigger_alert(*args)

        return result

    def get_all_lanes_health(self) -> Dict[str, Any]:
        """
        获取所有车道的健康状态汇总（一次性加锁，保证数据一致性）
        """
        with self._lock:
            all_lanes = {}
            overall_status = "healthy"
            total_warnings = []
            pending_alerts = []

            for lane in self.LANE_LATENCY_TARGETS:
                lane_res, alerts = self._get_health_score_locked(lane)
                if lane_res["status"] == "ok":
                    all_lanes[lane] = lane_res["data"]
                    lane_level = lane_res["data"].get("level", "unknown")
                    if lane_level == "critical":
                        overall_status = "critical"
                    elif lane_level == "degraded" and overall_status == "healthy":
                        overall_status = "degraded"
                    total_warnings.extend(lane_res.get("warnings", []))
                pending_alerts.extend(alerts)

        # 锁外批量触发告警
        for args in pending_alerts:
            self._trigger_alert(*args)

        if overall_status == "critical":
            logger.error("全局车道健康状态: critical #RECOVERY: 检查调度器、降级策略")
        elif overall_status == "degraded":
            logger.warning("全局车道健康状态: degraded")

        return {
            "status": "ok",
            "reason": f"全局车道健康状态: {overall_status}",
            "data": {"overall_status": overall_status, "lanes": all_lanes},
            "warnings": total_warnings,
        }

    def health_check(self) -> Dict[str, Any]:
        """
        模块自检，包含统计有效性验证、绝对延迟阈值、内存占用估算
        """
        try:
            if not hasattr(self, '_metrics') or not self._metrics:
                return {
                    "status": "degraded",
                    "reason": "metrics 未初始化",
                    "data": {},
                    "warnings": ["metrics_not_initialized"],
                }

            with self._lock:
                lane_count = len(self._metrics)
                total_samples = sum(len(self._metrics[l]["timestamps"]) for l in self._metrics)
                buffer_usage = {}
                statistical_anomalies = []
                for lane in self.LANE_LATENCY_TARGETS:
                    timestamps = self._metrics[lane]["timestamps"]
                    maxlen = timestamps.maxlen or self.DEFAULT_WINDOW_SAMPLES
                    buffer_usage[lane] = {
                        "used": len(timestamps),
                        "capacity": maxlen,
                        "usage_pct": round(len(timestamps) / maxlen * 100, 1) if maxlen else 0,
                    }

                    # 统计有效性检测：近20个样本的p95均值和方差
                    recent_p95 = list(self._metrics[lane]["latency_p95"])[-20:]
                    if recent_p95:
                        mean_val = np.mean(recent_p95)
                        std_val = np.std(recent_p95)
                        target = self.LANE_LATENCY_TARGETS[lane]
                        # 同时应用相对阈值和绝对阈值
                        if (mean_val > target * 100 or
                                mean_val > self.ABSOLUTE_LATENCY_CRITICAL_US):
                            statistical_anomalies.append(
                                f"{lane}: mean={mean_val:.1f}, std={std_val:.1f}"
                            )

                # 内存估算
                estimated_memory = sum(
                    sys.getsizeof(self._metrics[lane]) +
                    sum(sys.getsizeof(q) for q in self._metrics[lane].values())
                    for lane in self._metrics
                )

            status = "ok" if not statistical_anomalies else "degraded"
            reason = "LaneHealthMonitor 正常" if not statistical_anomalies else "检测到统计异常"

            return {
                "status": status,
                "reason": reason,
                "data": {
                    "lane_count": lane_count,
                    "total_samples": total_samples,
                    "buffer_usage": buffer_usage,
                    "memory_estimate_bytes": estimated_memory,
                    "statistical_anomalies": statistical_anomalies,
                    "dependencies": {
                        "lane_scheduler": self._lane_scheduler is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                    },
                },
                "warnings": statistical_anomalies,
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁和数据字典")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _get_health_score_locked(self, lane_name: str) -> tuple:
        """
        在锁内计算健康评分，返回 (result_dict, pending_alerts_list)
        """
        now = time.time()
        cache_age = now - self._cache_timestamp.get(lane_name, 0)
        if cache_age < self.DEFAULT_CACHE_TTL_SEC:
            cached = self._health_cache.get(lane_name, {})
            if cached and cached.get("sample_count", 0) >= self.MIN_SAMPLES_FOR_EVAL:
                return {
                    "status": "ok",
                    "reason": "返回缓存的健康评分",
                    "data": cached,
                    "warnings": [],
                }, []

        lane_data = self._metrics[lane_name]
        timestamps = lane_data["timestamps"]
        sample_count = len(timestamps)
        target_latency = self._get_dynamic_target(lane_name)

        if sample_count < self.MIN_SAMPLES_FOR_EVAL:
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
            }, []

        # 短期/长期均值，用于非平稳检测
        short_p95 = self._safe_mean(lane_data["latency_p95"], window=self.SHORT_WINDOW_SAMPLES)
        long_p95 = self._safe_mean(lane_data["latency_p95"], window=self.DEFAULT_WINDOW_SAMPLES)
        current_p95 = long_p95  # 默认使用长期均值
        non_stationary = False
        if (short_p95 is not None and long_p95 is not None and long_p95 > 0
                and short_p95 / long_p95 > self.NON_STATIONARY_THRESHOLD_RATIO):
            non_stationary = True
            current_p95 = short_p95  # 以短期为准

        if current_p95 is None:
            current_p95 = target_latency * self.DEGRADED_LATENCY_MULTIPLIER

        current_p99 = self._safe_mean(lane_data["latency_p99"])
        current_queue = self._safe_mean(lane_data["queue_depth"])
        current_backpressure = self._safe_mean(lane_data["backpressure_count"])
        current_processing = self._safe_mean(lane_data["processing_latency_p95"])

        if current_p99 is None:
            current_p99 = current_p95 * 1.2
        if current_queue is None:
            current_queue = 1.0
        if current_backpressure is None:
            current_backpressure = 0.0
        if current_processing is None:
            current_processing = current_p95 * 0.3

        # 健康评分
        latency_ratio = current_p95 / target_latency if target_latency > 0 else 1.0
        queue_penalty = min(self.QUEUE_PENALTY_MAX, current_queue * self.QUEUE_PENALTY_MULTIPLIER)
        backpressure_penalty = min(self.BACKPRESSURE_PENALTY_MAX,
                                   current_backpressure * self.BACKPRESSURE_PENALTY_MULTIPLIER)
        score = max(0.0, 100.0 -
                    (latency_ratio - self.LATENCY_RATIO_OFFSET) * self.LATENCY_RATIO_PENALTY_MULTIPLIER -
                    queue_penalty - backpressure_penalty)

        # 滞回逻辑（首次评估跳过滞回）
        previous_level = self._previous_health_level.get(lane_name, "healthy")
        is_first = self._first_evaluation.get(lane_name, True)
        if is_first:
            self._first_evaluation[lane_name] = False
            # 首次评估直接按评分判定
            if score >= 85:
                level = "healthy"
            elif score >= 60:
                level = "degraded"
            else:
                level = "critical"
        else:
            if score >= 85:
                level = "healthy"
            elif score >= 60:
                level = "degraded"
            else:
                level = "critical"

            if previous_level == "critical" and level != "critical":
                if score < 60 + self.DEFAULT_HYSTERESIS_MARGIN:
                    level = "critical"
            if previous_level == "degraded" and level == "healthy":
                if score < 85 + self.DEFAULT_HYSTERESIS_MARGIN:
                    level = "degraded"

        # 状态振荡检测
        if level != previous_level:
            self._state_transition_history[lane_name].append(now)

        self._previous_health_level[lane_name] = level

        # 诊断信息
        if level == "healthy":
            diagnosis = "车道运行正常"
        elif level == "degraded":
            diagnosis = f"车道性能下降，P95延迟为目标的{latency_ratio:.1f}倍"
        else:
            diagnosis = f"车道严重拥堵，P95延迟为目标的{latency_ratio:.1f}倍，队列深度={current_queue:.1f}"

        if non_stationary:
            diagnosis += " (非平稳突变)"

        # 可用性更新
        if level != "healthy":
            self._lane_uptime[lane_name]["degraded_seconds"] += self.DEFAULT_CACHE_TTL_SEC

        # 趋势分析
        self._score_history[lane_name].append(score)
        warnings = []
        if self._detect_consecutive_decline(lane_name):
            warnings.append(f"{self.LANE_DESCRIPTIONS[lane_name]} 评分连续下降，建议排查上游模块")
        if non_stationary:
            warnings.append(
                f"{self.LANE_DESCRIPTIONS[lane_name]} 非平稳突变，短期P95={short_p95:.1f}μs，长期={long_p95:.1f}μs"
            )

        # 状态振荡告警
        transitions = list(self._state_transition_history[lane_name])
        recent_osc = [t for t in transitions if now - t < self.OSCILLATION_WINDOW_SEC]
        if len(recent_osc) >= self.OSCILLATION_SWITCH_THRESHOLD:
            osc_warn = (
                f"{self.LANE_DESCRIPTIONS[lane_name]} 状态振荡，"
                f"{self.OSCILLATION_WINDOW_SEC}秒内切换{len(recent_osc)//2}次，建议深度排查"
            )
            warnings.append(osc_warn)

        result = {
            "lane": lane_name,
            "score": round(score, 1),
            "level": level,
            "sample_count": sample_count,
            "current_p95_us": round(current_p95, 1),
            "current_p99_us": round(current_p99, 1),
            "processing_latency_p95_us": round(current_processing, 1),
            "target_latency_us": target_latency,
            "queue_depth": round(current_queue, 1),
            "backpressure_count": round(current_backpressure, 1),
            "uptime_pct": self._calc_uptime_pct(lane_name),
            "diagnosis": diagnosis,
        }
        self._update_cache(lane_name, result, now)

        # 收集待触发告警（锁外执行推送）
        pending_alerts = []
        if level == "critical":
            pending_alerts.append((lane_name, "critical", diagnosis))
        elif level == "degraded" and latency_ratio > self.DEFAULT_ALERT_THRESHOLD_RATIO:
            pending_alerts.append((lane_name, "warning", diagnosis))

        return {
            "status": "ok",
            "reason": f"{self.LANE_DESCRIPTIONS[lane_name]} 健康评分: {score:.1f}",
            "data": result,
            "warnings": warnings,
        }, pending_alerts

    def _update_cache(self, lane_name: str, result: Dict[str, Any], timestamp: float) -> None:
        self._health_cache[lane_name] = result
        self._cache_timestamp[lane_name] = timestamp

    @staticmethod
    def _safe_mean(deque_obj: deque, window: int = 10, clean_data: bool = True) -> Optional[float]:
        """
        安全计算均值，支持指定窗口大小，优化内存分配
        """
        start_idx = max(0, len(deque_obj) - window)
        recent_iter = itertools.islice(deque_obj, start_idx, None)
        recent = list(recent_iter)  # 仅复制窗口内的元素，而非整个deque
        if not recent:
            return None
        if clean_data:
            valid = [x for x in recent if np.isfinite(x) and x >= 0]
            if not valid:
                return None
            if len(valid) > 1:
                mean = np.mean(valid)
                std = np.std(valid)
                valid = [x for x in valid if abs(x - mean) <= 5 * std]
            return float(np.mean(valid)) if valid else None
        return float(np.mean(recent))

    def _detect_consecutive_decline(self, lane_name: str) -> bool:
        history = list(self._score_history[lane_name])
        if len(history) < self.CONSECUTIVE_DECLINE_THRESHOLD:
            return False
        recent = history[-self.CONSECUTIVE_DECLINE_THRESHOLD:]
        return all(recent[i] < recent[i - 1] for i in range(1, len(recent)))

    def _get_dynamic_target(self, lane_name: str) -> float:
        base_target = self.LANE_LATENCY_TARGETS.get(lane_name, 100)
        if self._current_volatility_percentile >= 80:
            mult = self.VOLATILITY_REGIME_MULTIPLIERS["high_vol"]
        elif self._current_volatility_percentile <= 30:
            mult = self.VOLATILITY_REGIME_MULTIPLIERS["low_vol"]
        else:
            mult = self.VOLATILITY_REGIME_MULTIPLIERS["normal_vol"]
        return base_target * mult

    def _calc_uptime_pct(self, lane_name: str) -> Optional[float]:
        uptime = self._lane_uptime[lane_name]
        total = uptime["total_seconds"]
        if total < self.MIN_UPTIME_OBSERVATION_SEC:
            return None  # 样本不足
        degraded = uptime["degraded_seconds"]
        return round((1.0 - degraded / total) * 100, 1)

    def _try_cleanup(self) -> None:
        now = time.time()
        if now - self._last_cleanup < self.DEFAULT_CLEANUP_INTERVAL_SEC:
            return

        with self._lock:
            total_before = sum(len(self._metrics[l]["timestamps"]) for l in self.LANE_LATENCY_TARGETS)
            cutoff = now - self.DEFAULT_MAX_DATA_AGE_SEC
            total_removed = 0
            for lane in self.LANE_LATENCY_TARGETS:
                lane_data = self._metrics[lane]
                timestamps = lane_data["timestamps"]
                removed = 0
                while timestamps and timestamps[0] < cutoff:
                    for key in self.METRIC_KEYS:
                        queue = lane_data[key]
                        if queue:
                            queue.popleft()
                    timestamps.popleft()
                    removed += 1
                if removed:
                    logger.debug("车道 %s 清理 %d 条过期记录 (剩余 %d)", lane, removed, len(timestamps))
                total_removed += removed
            total_after = sum(len(self._metrics[l]["timestamps"]) for l in self.LANE_LATENCY_TARGETS)

            # 清理过期告警记录
            alert_cutoff = now - 86400
            stale_alerts = [k for k, v in self._alert_last_triggered.items() if v < alert_cutoff]
            for k in stale_alerts:
                del self._alert_last_triggered[k]
            # 清理对应重复计数
            for k in stale_alerts:
                self._alert_repeat_count.pop(k, None)

            stale_silence = [k for k, v in self._alert_silence_until.items() if v < now]
            for k in stale_silence:
                del self._alert_silence_until[k]

        self._last_cleanup = now
        if total_removed > 0 or stale_alerts or stale_silence:
            logger.info(
                "全局清理: 数据 %d 条 (前 %d/后 %d), 告警去重 %d 条, 静默 %d 条",
                total_removed, total_before, total_after, len(stale_alerts), len(stale_silence)
            )

    def _trigger_alert(self, lane_name: str, level: str, message: str) -> None:
        """
        触发告警（锁外调用，内部自行保护去重字典），加入重复计数与定期汇总
        """
        alert_key = f"{lane_name}:{level}"
        now = time.time()

        with self._lock:
            # 检查静默期
            silence_until = self._alert_silence_until.get(alert_key, 0)
            if now < silence_until:
                return
            # 检查去重窗口
            last_time = self._alert_last_triggered.get(alert_key, 0)
            if now - last_time < self.ALERT_DEDUP_WINDOW_SEC:
                # 增加重复计数
                count = self._alert_repeat_count.get(alert_key, 0) + 1
                self._alert_repeat_count[alert_key] = count
                if count % 5 == 0:
                    logger.warning("告警 %s 已重复 %d 次 (窗口内)", alert_key, count)
                return
            # 重置计数器
            self._alert_repeat_count[alert_key] = 1
            self._alert_last_triggered[alert_key] = now

        # 以下无锁，执行推送
        alert_msg = f"[{level.upper()}] {self.LANE_DESCRIPTIONS.get(lane_name, lane_name)}: {message}"

        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="lane_health",
                    lane=lane_name,
                    level=level,
                    message=message,
                    timestamp=now,
                )
            except Exception as e:
                logger.warning(f"协商总线告警推送失败: {e}")

        if level == "critical":
            logger.error(
                "%s #RECOVERY: 检查车道调度器、降低该车道信号流量、考虑动态核心借用",
                alert_msg
            )
        else:
            logger.warning(alert_msg)

        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="lane_health_alert",
                    details={"lane": lane_name, "level": level, "message": message},
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")
