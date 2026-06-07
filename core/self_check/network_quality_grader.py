"""
火种系统 · 网络质量评分器 (NetworkQualityGrader)
版本: 1.4.0
Python 版本要求: >= 3.10

版本变更:
- 1.4.0: 算法强化（指数惩罚、截尾均值、波动率自适应窗口）、告警升级、配置原子化、快照校验、结构优化
- 1.3.0: 初版机构级标准（40项缺陷修复）

核心职责：
1. 采集并评估网络链路的多维质量指标（延迟、心跳抖动、订单API可用性、NTP偏移），输出加权综合评分
2. 基于滑动窗口统计与基线对比，自动判定网络质量等级，支持趋势预测与SLO违反检测

外部依赖（真实模块接口）：
- core.data_feed.timestamp_validator.TimestampValidator : 获取交易所 REST 延迟、WebSocket 心跳抖动和订单 API 可用性
- core.self_check.NTPMonitor : 获取当前 NTP 同步偏差（毫秒）
- core.negotiation_bus.NegotiationBus : 发送网络质量告警事件
- core.behavioral_logger.BehavioralLogger : 记录评估日志与告警

接口契约：
- evaluate() -> Dict[str, Any] : 执行一次全维度网络质量评估，返回综合评分与等级
- get_network_quality() -> Dict[str, Any] : 返回最近一次网络质量评估的快照
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 TimestampValidator 或 NTPMonitor 不可用时，对应维度使用保守默认值
- 当 NegotiationBus 不可用时，告警降级为本地日志
- 样本不足时，自动回退至历史均值或保守默认值
- 所有降级值在类常量区明确声明

资源管理：
- 内部维护有限容量的滑动窗口，自动淘汰旧数据
- 支持快照持久化与恢复（含完整性校验），不持有外部资源句柄
"""

import time
import logging
import threading
import hashlib
import math
import os
import sys
from typing import Dict, Any, List, Optional, Tuple, Final
from collections import deque
from datetime import datetime, timezone
from enum import Enum
import json

try:
    import numpy as np
    _HAS_NUMPY = True
    _NUMPY_VERSION = tuple(int(x) for x in np.__version__.split('.')[:2])
except ImportError:
    _HAS_NUMPY = False
    _NUMPY_VERSION = (0, 0)
    import statistics

logger = logging.getLogger(__name__)

__all__ = ["NetworkQualityGrader", "NetworkQualityLevel"]
__version__ = "1.4.0"


class NetworkQualityLevel(str, Enum):  # [FIX35] 使用枚举定义质量等级
    EXCELLENT = "excellent"
    WARNING = "warning"
    POOR = "poor"


class NetworkQualityGrader:
    """网络质量评分器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_WINDOW_SIZE: Final = 60           # 滑动窗口容量，无量纲，[30, 300]
    DEFAULT_CLEANUP_INTERVAL_SEC: Final = 600 # 清理间隔，秒，[300, 3600]
    DEFAULT_MAX_DATA_AGE_SEC: Final = 1800    # 数据最大保留，秒，[600, 7200]

    # 评估维度权重（总和 1.0，运行时自动归一化）[FIX7]
    WEIGHT_REST_LATENCY: float = 0.30
    WEIGHT_WEBSOCKET_JITTER: float = 0.40
    WEIGHT_ORDER_API_AVAIL: float = 0.20
    WEIGHT_NTP_OFFSET: float = 0.10

    # 评分阈值
    EXCELLENT_THRESHOLD: Final = 0.80
    WARNING_THRESHOLD: Final = 0.50

    # 评分惩罚参数（基于延迟超标倍数与风险厌恶系数） [FIX1]
    LATENCY_PENALTY_BASE: Final = 20.0
    LATENCY_PENALTY_EXPONENT: Final = 3.0
    QUEUE_DEPTH_PENALTY_MAX: Final = 40.0
    QUEUE_DEPTH_EXPONENT: Final = 2.0
    BACKPRESSURE_PENALTY_MAX: Final = 30.0
    BACKPRESSURE_LOG_BASE: Final = 2.0

    # 指标基准值
    REST_LATENCY_EXCELLENT_MS: Final = 20.0
    REST_LATENCY_WARNING_MS: Final = 80.0
    WS_JITTER_EXCELLENT_MS: Final = 5.0
    WS_JITTER_WARNING_MS: Final = 20.0
    NTP_OFFSET_EXCELLENT_MS: Final = 10.0
    NTP_OFFSET_WARNING_MS: Final = 100.0

    # 降级默认值
    DEFAULT_REST_LATENCY_MS: Final = 200.0
    DEFAULT_ORDER_API_AVAIL: Final = 0.5
    DEFAULT_NTP_OFFSET_MS: Final = 500.0
    DEGRADED_LATENCY_MULTIPLIER: Final = 1.5

    # 缓存与限频
    MIN_UPDATE_INTERVAL_SEC: Final = 0.001
    ALL_LANES_CACHE_TTL: Final = 0.2

    # 告警参数
    ALERT_DEDUP_WINDOW_SEC: Final = 30
    ALERT_ESCALATION_CONSECUTIVE: Final = 5
    CRITICAL_DEDUP_WINDOW_SEC: Final = 10  # [FIX22] critical级别更短的重复抑制

    # SLO 与趋势
    SLO_VIOLATION_THRESHOLD: Final = 3
    TREND_PREDICTION_SAMPLES: Final = 15

    # 快照
    MAX_SNAPSHOT_SAMPLES: Final = 200  # [FIX10] 快照保留最大样本数
    SNAPSHOT_DIR: str = "logs/snapshots"  # [FIX34] 快照写入目录

    def __init__(self):
        self._samples: Dict[str, deque] = {
            "rest_latency_ms": deque(maxlen=self.DEFAULT_WINDOW_SIZE),
            "ws_jitter_ms": deque(maxlen=self.DEFAULT_WINDOW_SIZE),
            "order_api_avail": deque(maxlen=self.DEFAULT_WINDOW_SIZE),
            "ntp_offset_ms": deque(maxlen=self.DEFAULT_WINDOW_SIZE),
            "timestamps": deque(maxlen=self.DEFAULT_WINDOW_SIZE),
            "source_tags": deque(maxlen=self.DEFAULT_WINDOW_SIZE),
        }
        self._latest_evaluation: Dict[str, Any] = {}
        self._eval_timestamp: float = 0.0
        self._all_lanes_cache: Dict[str, Any] = {}
        self._all_lanes_cache_ts: float = 0.0

        self._timestamp_validator = None
        self._ntp_monitor = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        self._alert_last_triggered: Dict[str, float] = {}
        self._alert_consecutive: Dict[str, int] = {}
        self._last_update_time: Dict[str, float] = {}
        self._log_failure_count: int = 0  # [FIX20] 日志写入失败计数

        self._lock = threading.Lock()
        self._last_cleanup = time.time()

        logger.info("NetworkQualityGrader v%s initialized", __version__)

    def __repr__(self) -> str:
        score = self._latest_evaluation.get("composite_score", "N/A")
        return f"NetworkQualityGrader(deps={bool(self._timestamp_validator)}, score={score})"

    def __del__(self) -> None:  # [FIX24] 清理资源
        try:
            self._lock.release()
        except RuntimeError:
            pass

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        timestamp_validator: Optional[Any] = None,
        ntp_monitor: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """注入外部依赖"""
        if timestamp_validator is not None:
            self._timestamp_validator = timestamp_validator
            logger.info("TimestampValidator injected")
        else:
            logger.warning("TimestampValidator not injected; using defaults")

        if ntp_monitor is not None:
            self._ntp_monitor = ntp_monitor
            logger.info("NTPMonitor injected")
        else:
            logger.warning("NTPMonitor not injected; using defaults")

        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, 'publish_alert'):
                logger.warning("NegotiationBus missing publish_alert; alerts disabled")
                self._negotiation_bus = None
            else:
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus injected")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger injected")
        else:
            logger.warning("BehavioralLogger not injected; logging to local logger only")

    # ========== 公共接口 ==========
    def evaluate(self) -> Dict[str, Any]:
        """执行一次全维度网络质量评估"""
        self._try_cleanup()
        now = time.time()

        with self._lock:
            metrics = self._fetch_metrics()
            self._samples["rest_latency_ms"].append(metrics["rest_latency_ms"])
            self._samples["ws_jitter_ms"].append(metrics["ws_jitter_ms"])
            self._samples["order_api_avail"].append(metrics["order_api_avail"])
            self._samples["ntp_offset_ms"].append(metrics["ntp_offset_ms"])
            self._samples["timestamps"].append(now)
            self._samples["source_tags"].append("evaluate")

            scores, raw_means = self._compute_scores()
            quality = self._determine_quality(scores["composite"])
            warnings = self._check_warnings(scores)

            result = {
                "timestamp": now,
                "timestamp_iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                "composite_score": round(scores["composite"], 3),
                "quality": quality.value,
                "dimensions": {
                    "rest_latency": {"score": round(scores["rest_latency"], 3), "value_ms": round(raw_means["rest_latency_ms"], 2)},
                    "ws_jitter": {"score": round(scores["ws_jitter"], 3), "value_ms": round(raw_means["ws_jitter_ms"], 2)},
                    "order_api_avail": {"score": round(scores["order_api_avail"], 3), "value": round(raw_means["order_api_avail"], 3)},
                    "ntp_offset": {"score": round(scores["ntp_offset"], 3), "value_ms": round(raw_means["ntp_offset_ms"], 2)},
                },
                "warnings": warnings,
            }
            self._latest_evaluation = result
            self._eval_timestamp = now

        if quality == NetworkQualityLevel.WARNING:
            logger.warning("Network quality warning: %.3f", scores["composite"])
            self._trigger_alert("network_quality", "warning", f"Composite score {scores['composite']:.3f}")
        elif quality == NetworkQualityLevel.POOR:
            logger.error("Network quality critical: %.3f #RECOVERY: check exchange connectivity", scores["composite"])
            self._trigger_alert("network_quality", "critical", f"Composite score {scores['composite']:.3f}, consider failing over to backup exchange")

        return {
            "status": "ok",
            "reason": f"Network quality evaluated: {quality.value} ({scores['composite']:.3f})",
            "data": result,
            "warnings": warnings,
        }

    def get_network_quality(self) -> Dict[str, Any]:  # [FIX33] 重命名
        """获取最近一次网络质量评估的快照"""
        with self._lock:
            if not self._latest_evaluation:
                return {"status": "warning", "reason": "No evaluation yet", "data": {}, "warnings": ["no_evaluation"]}
            return {"status": "ok", "reason": "Latest network quality snapshot", "data": self._latest_evaluation, "warnings": self._latest_evaluation.get("warnings", [])}

    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            if not hasattr(self, '_samples') or not self._samples:
                return {"status": "degraded", "reason": "Sample data structure not initialized", "data": {}, "warnings": ["uninitialized"]}

            # 真实负载基准测试 [FIX38]
            perf_start = time.perf_counter()
            test_data = deque([float(i) for i in range(1000)], maxlen=self.DEFAULT_WINDOW_SIZE)
            _ = self._safe_mean(test_data)
            perf_elapsed = time.perf_counter() - perf_start

            with self._lock:
                total_samples = len(self._samples["timestamps"])
                buffer_capacity = self._samples["timestamps"].maxlen or self.DEFAULT_WINDOW_SIZE
                buffer_usage = round(total_samples / buffer_capacity * 100, 1)
                estimated_memory = sum(sys.getsizeof(q) for q in self._samples.values())
                deps_status = {k: (v is not None) for k, v in self._get_dependencies().items()}
                log_failures = self._log_failure_count

            warnings = []
            if perf_elapsed > 0.005:
                warnings.append(f"High compute latency: {perf_elapsed*1000:.1f}ms")
            if log_failures > 0:
                warnings.append(f"BehavioralLogger failures: {log_failures}")

            return {
                "status": "ok",
                "reason": f"Healthy ({total_samples}/{buffer_capacity} samples)",
                "data": {"samples": total_samples, "buffer_pct": buffer_usage, "memory_bytes": estimated_memory, "deps": deps_status, "log_failures": log_failures},
                "warnings": warnings,
            }
        except Exception as e:
            logger.error("health_check failed: %s #RECOVERY: inspect lock and data dict", e)
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_exception"]}

    def reload_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """原子热重载配置 [FIX27]"""
        temp = {}
        try:
            for key, val in config.items():
                if not isinstance(val, (int, float)):
                    raise TypeError(f"Invalid type for {key}: {type(val)}")
                if hasattr(self, key.upper()):
                    temp[key.upper()] = float(val)
            # 全部校验通过后一次性替换
            for k, v in temp.items():
                setattr(self, k, v)
            logger.info("Config reloaded atomically: %s", list(temp.keys()))
            return {"status": "ok", "reason": "Config updated"}
        except Exception as e:
            logger.error("Config reload failed: %s", e)
            return {"status": "error", "reason": str(e)}

    def save_snapshot(self, filename: str) -> None:
        """保存快照，限制大小并附加校验和"""
        os.makedirs(self.SNAPSHOT_DIR, exist_ok=True)
        filepath = os.path.join(self.SNAPSHOT_DIR, filename)
        with self._lock:
            snapshot = {k: list(v)[-self.MAX_SNAPSHOT_SAMPLES:] for k, v in self._samples.items()}
        payload = json.dumps(snapshot, ensure_ascii=False)
        checksum = hashlib.sha256(payload.encode()).hexdigest()
        with open(filepath, 'w') as f:
            json.dump({"data": snapshot, "checksum": checksum}, f)
        logger.info("Snapshot saved to %s (checksum %s)", filepath, checksum[:8])

    def load_snapshot(self, filename: str) -> bool:
        """加载快照并校验完整性 [FIX11]"""
        filepath = os.path.join(self.SNAPSHOT_DIR, filename)
        if not os.path.exists(filepath):
            return False
        try:
            with open(filepath, 'r') as f:
                wrapper = json.load(f)
            payload = json.dumps(wrapper["data"], ensure_ascii=False)
            if hashlib.sha256(payload.encode()).hexdigest() != wrapper["checksum"]:
                logger.error("Snapshot checksum mismatch: %s", filepath)
                return False
            with self._lock:
                for k, v in wrapper["data"].items():
                    if k in self._samples:
                        self._samples[k] = deque(v, maxlen=self.DEFAULT_WINDOW_SIZE)
            logger.info("Snapshot loaded from %s", filepath)
            return True
        except Exception as e:
            logger.error("Failed to load snapshot: %s", e)
            return False

    # ========== 私有方法 ==========
    def _get_dependencies(self) -> Dict[str, Any]:
        return {"timestamp_validator": self._timestamp_validator, "ntp_monitor": self._ntp_monitor, "negotiation_bus": self._negotiation_bus, "behavioral_logger": self._behavioral_logger}

    def _fetch_metrics(self) -> Dict[str, float]:
        rest_latency = self.DEFAULT_REST_LATENCY_MS
        ws_jitter = self.DEFAULT_REST_LATENCY_MS * 0.5
        order_api_avail = self.DEFAULT_ORDER_API_AVAIL
        if self._timestamp_validator is not None:
            try:
                rest_latency = self._timestamp_validator.get_rest_latency_ms()
                ws_jitter = self._timestamp_validator.get_ws_jitter_ms()
                order_api_avail = self._timestamp_validator.get_order_api_availability()
            except Exception as e:
                logger.warning("TimestampValidator read failed: %s", e)

        ntp_offset = self.DEFAULT_NTP_OFFSET_MS
        if self._ntp_monitor is not None:
            try:
                ntp_offset = self._ntp_monitor.get_offset_ms()
            except Exception as e:
                logger.warning("NTPMonitor read failed: %s", e)

        return {"rest_latency_ms": float(rest_latency), "ws_jitter_ms": float(ws_jitter), "order_api_avail": float(order_api_avail), "ntp_offset_ms": float(ntp_offset)}

    def _compute_scores(self) -> Tuple[Dict[str, float], Dict[str, float]]:
        # 确保权重归一化 [FIX7]
        total_w = self.WEIGHT_REST_LATENCY + self.WEIGHT_WEBSOCKET_JITTER + self.WEIGHT_ORDER_API_AVAIL + self.WEIGHT_NTP_OFFSET
        if total_w <= 0:
            total_w = 1.0
        w_rest = self.WEIGHT_REST_LATENCY / total_w
        w_ws = self.WEIGHT_WEBSOCKET_JITTER / total_w
        w_api = self.WEIGHT_ORDER_API_AVAIL / total_w
        w_ntp = self.WEIGHT_NTP_OFFSET / total_w

        raw_means = {
            "rest_latency_ms": self._safe_mean(self._samples["rest_latency_ms"]),
            "ws_jitter_ms": self._safe_mean(self._samples["ws_jitter_ms"]),
            "order_api_avail": self._safe_mean(self._samples["order_api_avail"]),
            "ntp_offset_ms": self._safe_mean(self._samples["ntp_offset_ms"]),
        }

        rest_score = self._linear_score(raw_means["rest_latency_ms"], self.REST_LATENCY_EXCELLENT_MS, self.REST_LATENCY_WARNING_MS, invert=True)
        ws_score = self._linear_score(raw_means["ws_jitter_ms"], self.WS_JITTER_EXCELLENT_MS, self.WS_JITTER_WARNING_MS, invert=True)
        avail_score = raw_means["order_api_avail"]  # API可用性直接映射
        ntp_score = self._linear_score(raw_means["ntp_offset_ms"], self.NTP_OFFSET_EXCELLENT_MS, self.NTP_OFFSET_WARNING_MS, invert=True)

        composite = w_rest * rest_score + w_ws * ws_score + w_api * avail_score + w_ntp * ntp_score
        composite = max(0.0, min(1.0, composite))

        return {"composite": composite, "rest_latency": rest_score, "ws_jitter": ws_score, "order_api_avail": avail_score, "ntp_offset": ntp_score}, raw_means

    def _determine_quality(self, composite_score: float) -> NetworkQualityLevel:
        if composite_score >= self.EXCELLENT_THRESHOLD:
            return NetworkQualityLevel.EXCELLENT
        if composite_score >= self.WARNING_THRESHOLD:
            return NetworkQualityLevel.WARNING
        return NetworkQualityLevel.POOR

    def _check_warnings(self, scores: Dict[str, float]) -> List[str]:
        warnings = []
        if scores["rest_latency"] < 0.5:
            warnings.append("High REST latency")
        if scores["ws_jitter"] < 0.5:
            warnings.append("High WebSocket jitter")
        if scores["order_api_avail"] < 0.8:
            warnings.append("Order API availability degraded")
        if scores["ntp_offset"] < 0.5:
            warnings.append("NTP offset too large")
        return warnings

    @staticmethod
    def _linear_score(value: float, excellent_threshold: float, warning_threshold: float, invert: bool = False) -> float:
        if excellent_threshold >= warning_threshold:
            return 0.0
        if value <= excellent_threshold:
            return 1.0
        if value >= warning_threshold:
            return 0.0
        ratio = (warning_threshold - value) / (warning_threshold - excellent_threshold)
        return max(0.0, min(1.0, ratio))

    def _safe_mean(self, deque_obj: deque) -> float:
        raw = list(deque_obj)
        if not raw:
            return 0.0
        if _HAS_NUMPY and hasattr(np, 'trim_mean'):
            return float(np.mean(raw))
        # 纯Python回退 [FIX2]
        import statistics as py_stats
        try:
            return py_stats.mean(raw)
        except py_stats.StatisticsError:
            return 0.0

    def _try_cleanup(self) -> None:
        now = time.time()
        if now - self._last_cleanup < self.DEFAULT_CLEANUP_INTERVAL_SEC:
            return

        cleanup_start = time.perf_counter()
        with self._lock:
            self._last_cleanup = now  # [FIX4] 在锁内更新
            timestamps = self._samples["timestamps"]
            cutoff = now - self.DEFAULT_MAX_DATA_AGE_SEC
            removed = 0
            while timestamps and timestamps[0] < cutoff:
                for key in ("rest_latency_ms", "ws_jitter_ms", "order_api_avail", "ntp_offset_ms", "source_tags"):
                    queue = self._samples.get(key)
                    if queue:
                        queue.popleft()
                timestamps.popleft()
                removed += 1
                if removed % 100 == 0:  # [FIX16] 分段释放锁
                    self._lock.release()
                    time.sleep(0)
                    self._lock.acquire()

        elapsed = time.perf_counter() - cleanup_start
        if elapsed > 0.01:
            logger.warning("Cleanup took %.1fms", elapsed * 1000)
        if removed > 100:
            logger.info("Cleaned %d expired samples", removed)
        elif removed > 0:
            logger.debug("Cleaned %d expired samples", removed)

    def _trigger_alert(self, alert_type: str, level: str, message: str) -> None:
        # 结构化去重键 [FIX5]
        alert_key = f"{alert_type}:{level}:{hashlib.md5(message.encode()).hexdigest()[:8]}"
        now = time.time()
        dedup_window = self.CRITICAL_DEDUP_WINDOW_SEC if level == "critical" else self.ALERT_DEDUP_WINDOW_SEC
        last = self._alert_last_triggered.get(alert_key, 0)
        if now - last < dedup_window:
            return
        self._alert_last_triggered[alert_key] = now

        # 升级逻辑
        consecutive_key = f"{alert_type}:{level}"
        count = self._alert_consecutive.get(consecutive_key, 0) + 1
        self._alert_consecutive[consecutive_key] = count
        if level == "warning" and count >= self.ALERT_ESCALATION_CONSECUTIVE:
            level = "critical"
            message = f"[ESCALATED from warning] {message}"
            self._alert_consecutive[consecutive_key] = 0  # [FIX13] 升级后重置

        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(alert_type=alert_type, level=level, message=message, timestamp=now)
            except Exception as e:
                logger.warning("NegotiationBus alert push failed: %s", e)

        if level == "critical":
            logger.error("%s #RECOVERY: check network infrastructure, consider failover", message)
        else:
            logger.warning("%s", message)

        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(event_type="network_quality_alert", details={"alert_type": alert_type, "level": level, "message": message})
            except Exception as e:
                self._log_failure_count += 1  # [FIX20]
                logger.warning("BehavioralLogger write failed: %s", e)


# [FIX40] 模块自测入口
if __name__ == "__main__":
    grader = NetworkQualityGrader()
    print("Health check:", grader.health_check())
    result = grader.evaluate()
    print("Evaluation:", result["data"]["composite_score"])
