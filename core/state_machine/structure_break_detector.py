"""
火种系统 · 结构突变检测器 (StructureBreakDetector)

核心职责：
1. 基于最大均值差异（MMD）和贝叶斯在线变点检测（BOCD），实时判断市场状态是否发生结构性变化。
2. 向状态机和其他模块推送突变告警，触发全局参数重置、权重回退或表观遗传适应。

外部依赖（真实模块接口）：
- core.perception.factor_preprocessor.FactorPreprocessor : 获取滑动窗口内的市场特征向量序列
- core.negotiation_bus.NegotiationBus : 发送结构突变检测事件和告警
- core.behavioral_logger.BehavioralLogger : 记录突变检测日志和诊断报告

接口契约：
- update_features(features: List[float]) -> Dict[str, Any] : 注入新的市场特征向量，返回突变评分与状态
- is_break_detected() -> bool : 快速查询当前是否处于结构突变状态
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status", "reason", "data", "warnings" 字段

异常与降级：
- 当 FactorPreprocessor 不可用时，使用预设的通用特征作为保守估计，并标记降级状态。
- 当样本不足时，返回"观测中"状态，避免误报。
- 当 NegotiationBus 或 BehavioralLogger 不可用时，告警降级为本地日志。

资源管理：
- 维护滑动窗口特征历史数据，定期清理过期样本。不持有外部资源句柄，线程锁在析构时释放。
"""

import time
import logging
import threading
import os
import json
import sys
from typing import Dict, Any, List, Optional, Tuple
from collections import deque
import numpy as np
from scipy.spatial.distance import cdist

try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

try:
    import portalocker
    _PORTALOCKER_AVAILABLE = True
except ImportError:
    _PORTALOCKER_AVAILABLE = False

logger = logging.getLogger(__name__)


class StructureBreakDetector:
    """结构突变检测器：MMD + BOCD"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # MMD相关
    DEFAULT_WINDOW_SIZE: int = 100
    DEFAULT_BASELINE_SIZE: int = 150
    DEFAULT_MMD_THRESHOLD: float = 0.15
    DEFAULT_MMD_HARD_LIMIT: float = 0.50
    DEFAULT_MMD_SIGMA_MEDIAN: bool = True
    DEFAULT_MMD_RFF_ENABLED: bool = True
    DEFAULT_MMD_RFF_DIM: int = 100
    DEFAULT_MMD_BANDWIDTH_SAMPLE_SIZE: int = 50
    DEFAULT_MMD_ADAPTIVE_DOWNSAMPLE: bool = True
    DEFAULT_MMD_DOWNSAMPLE_CPU_THRESHOLD: float = 0.70
    DEFAULT_MMD_DOWNSAMPLE_RATIO: float = 0.50
    DEFAULT_MMD_BLOCKWISE_THRESHOLD: int = 200
    DEFAULT_MMD_SIGMA_UPPER_BOUND: float = 10.0
    API_VERSION: str = "2.0"

    # BOCD相关
    DEFAULT_HAZARD_RATE: float = 0.005
    DEFAULT_POSTERIOR_THRESHOLD: float = 0.9
    DEFAULT_POSTERIOR_EMA_ALPHA: float = 0.3
    DEFAULT_POSTERIOR_EMA_ALPHA_HIGH_VOL: float = 0.5
    DEFAULT_POSTERIOR_EMA_ALPHA_LOW_VOL: float = 0.2
    DEFAULT_RUNLENGTH_HARD_CAP: int = 1000
    DEFAULT_BOCD_LIKELIHOOD_GROWTH_WEIGHT: float = 2.0
    DEFAULT_BOCD_LIKELIHOOD_CHANGE_WEIGHT: float = 3.0
    DEFAULT_BOCD_ASYMMETRIC_CHANGE_BOOST: float = 1.5
    MIN_LOG_ARG: float = 1e-15
    DEFAULT_BOCD_LOG_SUM_EXP_MIN_RATIO: float = 1e-15
    DEFAULT_RUNLENGTH_STORE_LOG: bool = True  # 游程长度存储为log值，避免硬截断

    # 通用设置
    DEFAULT_MAX_HISTORY: int = 500
    DEFAULT_CLEANUP_INTERVAL_SEC: int = 300
    DEFAULT_COOLDOWN_SEC: int = 60
    DEFAULT_COOLDOWN_RECOVERY_SAMPLES: int = 10
    MIN_SAMPLES_FOR_DETECTION: int = 20
    MIN_SAMPLES_FOR_MMD: int = 3
    MIN_SAMPLES_FOR_PERCENTILE: int = 50
    DEFAULT_CUMULATIVE_THRESHOLD_RATIO: float = 0.8
    DEFAULT_CUMULATIVE_WINDOW_SEC: int = 30
    DEFAULT_CUMULATIVE_MIN_SAMPLES: int = 10
    DEFAULT_CUMULATIVE_DECAY_ALPHA: float = 0.9  # 指数衰减系数
    DEFAULT_ALERT_UPGRADE_COUNT: int = 3
    DEFAULT_ALERT_UPGRADE_WINDOW_SEC: int = 30
    DEFAULT_BASELINE_UPDATE_INTERVAL: int = 20
    DEFAULT_MAX_FEATURE_DIM: int = 50
    DEFAULT_OUTLIER_MAD_MULTIPLIER: float = 3.0
    DEFAULT_PCA_VARIANCE_RATIO: float = 0.95
    DEFAULT_PCA_REGULARIZATION: float = 1e-8
    DEFAULT_FEATURE_DIM_RESET_COOLDOWN_SEC: int = 300
    DEFAULT_BASELINE_CONDITION_NUMBER_THRESHOLD: float = 100.0  # 条件数阈值

    # 特征保护
    DEFAULT_EXTREME_EVENT_PROTECT: bool = True
    DEFAULT_EXTREME_MMD_PERCENTILE: int = 95
    DEFAULT_EXTREME_PROTECT_MAX_RATIO: float = 0.30
    DEFAULT_EXTREME_PROTECT_MAX_AGE_MULT: float = 2.0  # 最大保护年龄为窗口时间的倍数
    DEFAULT_BREAK_SNAPSHOT_PERSIST: bool = True
    DEFAULT_BREAK_SNAPSHOT_MAX_MEMORY: int = 50
    DEFAULT_ORIGINAL_FEATURES_HISTORY_SIZE: int = 200
    DEFAULT_SNAPSHOT_ROTATE_DAYS: int = 30  # 快照轮转天数

    # 基线多样性
    DEFAULT_BASELINE_DIVERSITY_CHECK_INTERVAL: int = 100
    DEFAULT_BASELINE_MIN_VARIANCE: float = 1e-4

    def __init__(self):
        # 特征历史记录
        self._feature_history: deque = deque(maxlen=self.DEFAULT_MAX_HISTORY)
        self._original_feature_history: deque = deque(maxlen=self.DEFAULT_ORIGINAL_FEATURES_HISTORY_SIZE)
        self._extreme_flags: deque = deque(maxlen=self.DEFAULT_MAX_HISTORY)
        # 双缓冲基线
        self._baseline_active: int = 0
        self._baseline_features: List[deque] = [
            deque(maxlen=self.DEFAULT_BASELINE_SIZE),
            deque(maxlen=self.DEFAULT_BASELINE_SIZE)
        ]
        self._baseline_lock: threading.Lock = threading.Lock()

        # MMD历史序列
        self._mmd_history: deque = deque(maxlen=self.DEFAULT_MAX_HISTORY)
        self._mmd_sigma_history: deque = deque(maxlen=self.DEFAULT_MAX_HISTORY)

        # 突变检测状态
        self._break_detected: bool = False
        self._break_score: float = 0.0
        self._last_break_time: float = 0.0
        self._bocd_runlength_log: float = 0.0  # 存储log值
        self._cooldown_normal_count: int = 0
        self._bocd_posterior_history: deque = deque(maxlen=self.DEFAULT_WINDOW_SIZE)

        # 状态快照
        self._break_snapshots: deque = deque(maxlen=self.DEFAULT_BREAK_SNAPSHOT_MAX_MEMORY)

        # 累积异常检测（指数衰减计数）
        self._cumulative_elevated_score: float = 0.0
        self._cumulative_elevated_start: Optional[float] = None
        self._cumulative_elevated_sample_count: int = 0

        # 特征维度记录
        self._feature_dim: Optional[int] = None
        self._feature_dim_change_count: int = 0
        self._feature_dim_candidate: Optional[int] = None
        self._feature_dim_last_reset_time: float = 0.0

        # 检测耗时记录
        self._detection_latency_us: deque = deque(maxlen=100)

        # 基线更新计数
        self._baseline_update_counter: int = 0
        self._baseline_diversity_counter: int = 0

        # PCA模型（在线）
        self._pca_mean: Optional[np.ndarray] = None
        self._pca_components: Optional[np.ndarray] = None
        self._pca_fitted_samples: int = 0
        self._pca_variance_retained: float = 0.0
        # 在线标准化（运行均值和方差，用于PCA未就绪时的替代）
        self._online_mean: Optional[np.ndarray] = None
        self._online_var: Optional[np.ndarray] = None
        self._online_samples: int = 0

        # 日志路径
        self._logs_dir: str = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "logs"))

        # 外部依赖
        self._factor_preprocessor: Optional[Any] = None
        self._negotiation_bus: Optional[Any] = None
        self._behavioral_logger: Optional[Any] = None

        # 线程安全
        self._lock: threading.Lock = threading.Lock()
        self._alert_lock: threading.Lock = threading.Lock()

        self._alert_history: deque = deque(maxlen=20)
        self._last_cleanup: float = time.time()
        self._config_loaded: bool = False

        # 随机状态（多进程安全种子）
        seed = int(time.time() * 1000) % (2**32) ^ os.getpid()
        self._random_generator: np.random.Generator = np.random.default_rng(seed)

        logger.info("StructureBreakDetector 初始化完成，窗口=%d 基线=%d 阈值=%.3f 硬上限=%.3f RFF=%s",
                    self.DEFAULT_WINDOW_SIZE, self.DEFAULT_BASELINE_SIZE,
                    self.DEFAULT_MMD_THRESHOLD, self.DEFAULT_MMD_HARD_LIMIT,
                    self.DEFAULT_MMD_RFF_ENABLED)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        factor_preprocessor: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选注入，未注入时降级）"""
        if factor_preprocessor is not None:
            if not hasattr(factor_preprocessor, 'get_features'):
                logger.warning("FactorPreprocessor 缺少 get_features 方法")
                self._factor_preprocessor = None
            else:
                self._factor_preprocessor = factor_preprocessor
        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, 'publish_alert'):
                logger.warning("NegotiationBus 缺少 publish_alert 方法")
                self._negotiation_bus = None
            else:
                self._negotiation_bus = negotiation_bus
        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger

    def set_logs_dir(self, logs_dir: str) -> None:
        self._logs_dir = os.path.abspath(logs_dir)
        os.makedirs(self._logs_dir, exist_ok=True)

    def load_config(self, config: Dict[str, Any]) -> None:
        """从配置文件加载参数"""
        if "mmd" in config:
            mmd_cfg = config["mmd"]
            self.DEFAULT_WINDOW_SIZE = int(mmd_cfg.get("window_size", self.DEFAULT_WINDOW_SIZE))
            self.DEFAULT_BASELINE_SIZE = int(mmd_cfg.get("baseline_size", self.DEFAULT_BASELINE_SIZE))
            self.DEFAULT_MMD_THRESHOLD = float(mmd_cfg.get("mmd_threshold", self.DEFAULT_MMD_THRESHOLD))
            self.DEFAULT_MMD_HARD_LIMIT = float(mmd_cfg.get("mmd_hard_limit", self.DEFAULT_MMD_HARD_LIMIT))
            self.DEFAULT_BOCD_LIKELIHOOD_GROWTH_WEIGHT = float(mmd_cfg.get("bocd_growth_weight",
                                    self.DEFAULT_BOCD_LIKELIHOOD_GROWTH_WEIGHT))
            self.DEFAULT_BOCD_LIKELIHOOD_CHANGE_WEIGHT = float(mmd_cfg.get("bocd_change_weight",
                                    self.DEFAULT_BOCD_LIKELIHOOD_CHANGE_WEIGHT))
            self.DEFAULT_MMD_RFF_ENABLED = bool(mmd_cfg.get("rff_enabled", self.DEFAULT_MMD_RFF_ENABLED))
            self.DEFAULT_MMD_RFF_DIM = int(mmd_cfg.get("rff_dim", self.DEFAULT_MMD_RFF_DIM))
            self.DEFAULT_MMD_ADAPTIVE_DOWNSAMPLE = bool(mmd_cfg.get("adaptive_downsample",
                                    self.DEFAULT_MMD_ADAPTIVE_DOWNSAMPLE))
            self.DEFAULT_MMD_DOWNSAMPLE_CPU_THRESHOLD = float(mmd_cfg.get("downsample_cpu_threshold",
                                    self.DEFAULT_MMD_DOWNSAMPLE_CPU_THRESHOLD))
            self.DEFAULT_MMD_DOWNSAMPLE_RATIO = float(mmd_cfg.get("downsample_ratio", self.DEFAULT_MMD_DOWNSAMPLE_RATIO))
        if "bocd" in config:
            bocd_cfg = config["bocd"]
            self.DEFAULT_HAZARD_RATE = float(bocd_cfg.get("hazard_rate", self.DEFAULT_HAZARD_RATE))
            self.DEFAULT_POSTERIOR_THRESHOLD = float(bocd_cfg.get("posterior_threshold", self.DEFAULT_POSTERIOR_THRESHOLD))
        self._config_loaded = True
        logger.info("配置加载完成")

    # ========== 公共接口 ==========
    def update_features(self, features: List[float]) -> Dict[str, Any]:
        start_time = time.perf_counter()
        now = time.time()

        if not isinstance(features, list) or not features:
            return {"status": "error", "reason": "特征向量不能为空", "data": {}, "warnings": ["invalid_features"]}
        if len(features) > self.DEFAULT_MAX_FEATURE_DIM:
            return {"status": "error", "reason": f"特征维度超限", "data": {}, "warnings": ["dimension_exceeded"]}

        # 维度校验与自适应
        if self._feature_dim is None:
            self._feature_dim = len(features)
        elif len(features) != self._feature_dim:
            if self._feature_dim_candidate == len(features):
                self._feature_dim_change_count += 1
            else:
                self._feature_dim_candidate = len(features)
                self._feature_dim_change_count = 1
            if (self._feature_dim_last_reset_time > 0 and
                    now - self._feature_dim_last_reset_time < self.DEFAULT_FEATURE_DIM_RESET_COOLDOWN_SEC):
                return {"status": "ok", "reason": "维度变更冷却中",
                        "data": {"break_detected": False, "score": 0.0, "bocd_probability": 0.0},
                        "warnings": ["dimension_reset_cooling"]}
            if self._feature_dim_change_count >= 3:
                with self._lock:
                    self._feature_dim = len(features)
                    self._feature_dim_change_count = 0
                    self._feature_dim_candidate = None
                    self._feature_dim_last_reset_time = now
                    self._baseline_features[self._baseline_active].clear()
                    self._pca_fitted_samples = 0
                    self._online_samples = 0
            else:
                return {"status": "error", "reason": f"维度变更中", "data": {}, "warnings": ["dimension_changing"]}

        try:
            feature_array = np.array(features, dtype=np.float64)
        except (ValueError, TypeError) as e:
            return {"status": "error", "reason": f"非法数值: {e}", "data": {}, "warnings": ["invalid_feature_values"]}
        if not np.isfinite(feature_array).all():
            return {"status": "error", "reason": "含NaN/Inf", "data": {}, "warnings": ["non_finite_features"]}

        self._original_feature_history.append(feature_array.copy())
        feature_array = self._clip_outliers(feature_array)
        self._try_cleanup()

        with self._lock:
            self._feature_history.append(feature_array)
            # 在线标准化更新
            self._update_online_stats(feature_array)

            is_extreme = False
            if len(self._mmd_history) >= 10:
                recent_mmd = np.array(self._mmd_history)[-10:]
                mmd_threshold_95 = np.percentile(recent_mmd, self.DEFAULT_EXTREME_MMD_PERCENTILE)
                is_extreme = (recent_mmd[-1] if len(recent_mmd) > 0 else 0.0) >= mmd_threshold_95
            self._extreme_flags.append(is_extreme)

            active_baseline = self._baseline_features[self._baseline_active]
            if len(active_baseline) < self.DEFAULT_BASELINE_SIZE:
                active_baseline.append(feature_array)

            if len(active_baseline) < self.MIN_SAMPLES_FOR_MMD:
                latency = (time.perf_counter() - start_time) * 1e6
                self._detection_latency_us.append(latency)
                return {"status": "ok", "reason": "基线构建中",
                        "data": {"break_detected": False, "score": 0.0, "bocd_probability": 0.0,
                                 "baseline_ready": False}, "warnings": ["baseline_not_ready"]}
            if len(self._feature_history) < self.MIN_SAMPLES_FOR_DETECTION:
                latency = (time.perf_counter() - start_time) * 1e6
                self._detection_latency_us.append(latency)
                return {"status": "ok", "reason": "样本不足",
                        "data": {"break_detected": False, "score": 0.0, "bocd_probability": 0.0,
                                 "baseline_ready": True}, "warnings": ["insufficient_samples"]}

            if self._break_detected and (now - self._last_break_time < self.DEFAULT_COOLDOWN_SEC):
                mmd_score = self._compute_mmd()
                self._mmd_history.append(mmd_score)
                if mmd_score < self.DEFAULT_MMD_THRESHOLD * 0.5:
                    self._cooldown_normal_count += 1
                else:
                    self._cooldown_normal_count = 0
                if self._cooldown_normal_count >= self.DEFAULT_COOLDOWN_RECOVERY_SAMPLES:
                    self._break_detected = False
                    self._cooldown_normal_count = 0
                latency = (time.perf_counter() - start_time) * 1e6
                self._detection_latency_us.append(latency)
                return {"status": "ok", "reason": "冷却期",
                        "data": {"break_detected": True, "score": round(float(mmd_score), 4),
                                 "bocd_probability": 1.0, "cooling_down": True}, "warnings": []}

            self._baseline_diversity_counter += 1
            if self._baseline_diversity_counter >= self.DEFAULT_BASELINE_DIVERSITY_CHECK_INTERVAL:
                self._baseline_diversity_counter = 0
                if not self._break_detected:
                    self._check_baseline_diversity()

            self._baseline_update_counter += 1
            if self._baseline_update_counter >= self.DEFAULT_BASELINE_UPDATE_INTERVAL and not self._break_detected:
                self._update_baseline()
                self._baseline_update_counter = 0

        # 计算MMD（锁外）
        mmd_score, mmd_sigma = self._compute_mmd(return_sigma=True)

        with self._lock:
            self._mmd_history.append(mmd_score)
            self._mmd_sigma_history.append(mmd_sigma)
            bocd_prob = self._bocd_update(mmd_score)
            ema_alpha = self._get_adaptive_ema_alpha()
            if self._bocd_posterior_history:
                bocd_prob_smoothed = ema_alpha * bocd_prob + (1 - ema_alpha) * self._bocd_posterior_history[-1]
            else:
                bocd_prob_smoothed = bocd_prob
            self._bocd_posterior_history.append(bocd_prob_smoothed)

            mmd_threshold_dynamic = self._get_dynamic_mmd_threshold()
            # 累积异常（指数衰减）
            if mmd_score > mmd_threshold_dynamic * self.DEFAULT_CUMULATIVE_THRESHOLD_RATIO:
                self._cumulative_elevated_score = 1.0 + self.DEFAULT_CUMULATIVE_DECAY_ALPHA * self._cumulative_elevated_score
                if self._cumulative_elevated_start is None:
                    self._cumulative_elevated_start = now
                self._cumulative_elevated_sample_count += 1
            else:
                self._cumulative_elevated_score *= self.DEFAULT_CUMULATIVE_DECAY_ALPHA
                if self._cumulative_elevated_score < 0.1:
                    self._cumulative_elevated_start = None
                    self._cumulative_elevated_sample_count = 0

            cumulative_alert = (
                self._cumulative_elevated_start is not None and
                (now - self._cumulative_elevated_start) >= self.DEFAULT_CUMULATIVE_WINDOW_SEC and
                self._cumulative_elevated_sample_count >= self.DEFAULT_CUMULATIVE_MIN_SAMPLES
            )

            break_detected = (
                (mmd_score > mmd_threshold_dynamic and bocd_prob_smoothed > self.DEFAULT_POSTERIOR_THRESHOLD) or
                cumulative_alert
            )

            if break_detected:
                self._break_detected = True
                self._break_score = mmd_score
                self._last_break_time = time.time()
                self._bocd_runlength_log = 0.0
                self._bocd_posterior_history.clear()
                self._cooldown_normal_count = 0
                self._pca_fitted_samples = 0
                self._pca_mean = None
                self._pca_components = None
                self._save_break_snapshot(mmd_score, bocd_prob_smoothed, cumulative_alert)
                logger.warning("结构突变检测: MMD=%.4f(阈值=%.4f) BOCD=%.3f",
                               mmd_score, mmd_threshold_dynamic, bocd_prob_smoothed)
                self._trigger_alert("critical",
                    f"市场结构突变，MMD={mmd_score:.4f}，BOCD后验概率={bocd_prob_smoothed:.3f}",
                    {"mmd_score": mmd_score, "bocd_prob": bocd_prob_smoothed,
                     "threshold": mmd_threshold_dynamic})
            else:
                self._bocd_runlength_log += np.log(1 - self._get_dynamic_hazard())

            latency = (time.perf_counter() - start_time) * 1e6
            self._detection_latency_us.append(latency)

            return {"status": "ok", "reason": f"检测完成，突变状态: {break_detected}",
                    "data": {
                        "api_version": self.API_VERSION,
                        "break_detected": break_detected,
                        "score": round(float(mmd_score), 4),
                        "bocd_probability": round(float(bocd_prob_smoothed), 3),
                        "run_length_log": round(float(self._bocd_runlength_log), 2),
                        "baseline_ready": True,
                        "cumulative_alert": cumulative_alert,
                        "detection_latency_usec": round(float(latency), 1),
                        "mmd_sigma": round(float(mmd_sigma), 6),
                        "mmd_threshold": round(float(mmd_threshold_dynamic), 4),
                        "ema_alpha": round(float(ema_alpha), 2),
                        "pca_variance_retained": round(float(self._pca_variance_retained), 3)
                    }, "warnings": []}

    def is_break_detected(self) -> bool:
        with self._lock:
            return self._break_detected

    def health_check(self) -> Dict[str, Any]:
        try:
            with self._lock:
                history_count = len(self._feature_history)
                baseline_count = len(self._baseline_features[self._baseline_active])
                test_dim = min(self._feature_dim or 10, 10)

            # 依赖版本校验
            import scipy
            version_ok = True
            try:
                import numpy as _np
                _np.__version__
                scipy.__version__
            except Exception:
                version_ok = False

            rng = np.random.default_rng(42)
            test_X = rng.standard_normal((20, test_dim))
            test_Y = rng.standard_normal((20, test_dim)) + 0.1
            combined = np.vstack([test_X, test_Y])
            sigma = max(np.median(cdist(combined, combined, 'euclidean')), 1e-6)
            XX = np.mean(np.exp(-cdist(test_X, test_X) ** 2 / (2 * sigma ** 2)), dtype=np.float64)
            YY = np.mean(np.exp(-cdist(test_Y, test_Y) ** 2 / (2 * sigma ** 2)), dtype=np.float64)
            XY = np.mean(np.exp(-cdist(test_X, test_Y) ** 2 / (2 * sigma ** 2)), dtype=np.float64)
            mmd_ok = (0.0 <= XX + YY - 2 * XY <= 1.0)

            # 灵敏度基准测试：注入已知突变
            test_X2 = test_X + 1.0
            XX2 = np.mean(np.exp(-cdist(test_X2, test_X2) ** 2 / (2 * sigma ** 2)), dtype=np.float64)
            XY2 = np.mean(np.exp(-cdist(test_X2, test_Y) ** 2 / (2 * sigma ** 2)), dtype=np.float64)
            mmd_sensitivity = max(0.0, XX2 + YY - 2 * XY2)
            sensitivity_ok = mmd_sensitivity > 0.3
            if not sensitivity_ok:
                logger.warning(f"灵敏度测试未通过: MMD={mmd_sensitivity:.4f}")

            bus_ok = True
            if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
                try:
                    self._negotiation_bus.publish_alert(alert_type="health_check", level="debug",
                                                        message="connectivity_test", timestamp=time.time())
                except Exception:
                    bus_ok = False

            return {"status": "ok" if (mmd_ok and version_ok and sensitivity_ok) else "degraded",
                    "reason": f"健康检查完成，测试维度={test_dim}",
                    "data": {"history_count": history_count, "baseline_count": baseline_count,
                             "break_detected": self._break_detected, "feature_dim": self._feature_dim,
                             "mmd_test_passed": mmd_ok, "sensitivity_test_passed": sensitivity_ok,
                             "negotiation_bus_ok": bus_ok, "version_ok": version_ok},
                    "warnings": []}
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查scipy/numpy依赖是否正常")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": [f"health_check_failed: {str(e)}"]}

    # ========== 私有方法 ==========
    def _update_online_stats(self, x: np.ndarray) -> None:
        """在线更新均值和方差（用于标准化）"""
        self._online_samples += 1
        if self._online_mean is None:
            self._online_mean = x.copy()
            self._online_var = np.zeros_like(x)
        else:
            delta = x - self._online_mean
            self._online_mean += delta / self._online_samples
            delta2 = x - self._online_mean
            self._online_var += delta * delta2
        # 防止方差为零
        if self._online_samples > 1:
            var = self._online_var / (self._online_samples - 1)
            var = np.maximum(var, 1e-8)

    def _standardize_online(self, x: np.ndarray) -> np.ndarray:
        """使用在线统计量进行标准化"""
        if self._online_samples < 10 or self._online_mean is None:
            return x
        std = np.sqrt(self._online_var / (self._online_samples - 1)) + 1e-8
        return (x - self._online_mean) / std

    def _clip_outliers(self, feature_array: np.ndarray) -> np.ndarray:
        """向量化异常值钳位（混合MAD和IQR）"""
        if len(self._feature_history) < 20:
            return feature_array
        hist = np.array(list(self._feature_history)[-20:], dtype=np.float64)
        median = np.nanmedian(hist, axis=0)
        mad = np.nanmedian(np.abs(hist - median), axis=0) + 1e-6
        q75, q25 = np.percentile(hist, [75, 25], axis=0)
        iqr = q75 - q25
        scale = np.maximum(mad, iqr * 0.5)
        lower = median - self.DEFAULT_OUTLIER_MAD_MULTIPLIER * scale
        upper = median + self.DEFAULT_OUTLIER_MAD_MULTIPLIER * scale
        return np.clip(feature_array, lower, upper)

    def _compute_mmd(self, return_sigma: bool = False) -> Any:
        """计算MMD，支持RFF近似、自适应降采样、分块计算"""
        try:
            active_baseline = self._baseline_features[self._baseline_active]
            baseline_data = np.array(list(active_baseline), dtype=np.float64)
            window_data = np.array(list(self._feature_history)[-self.DEFAULT_WINDOW_SIZE:], dtype=np.float64)

            if baseline_data.shape[0] < self.MIN_SAMPLES_FOR_MMD or window_data.shape[0] < self.MIN_SAMPLES_FOR_MMD:
                return (0.0, 1.0) if return_sigma else 0.0

            # 自适应降采样
            if self.DEFAULT_MMD_ADAPTIVE_DOWNSAMPLE and _PSUTIL_AVAILABLE:
                cpu_pct = psutil.cpu_percent(interval=None) / 100.0
                if cpu_pct > self.DEFAULT_MMD_DOWNSAMPLE_CPU_THRESHOLD:
                    new_bs = max(self.MIN_SAMPLES_FOR_MMD,
                                 int(baseline_data.shape[0] * self.DEFAULT_MMD_DOWNSAMPLE_RATIO))
                    new_ws = max(self.MIN_SAMPLES_FOR_MMD,
                                 int(window_data.shape[0] * self.DEFAULT_MMD_DOWNSAMPLE_RATIO))
                    if new_bs < baseline_data.shape[0]:
                        indices = self._random_generator.choice(baseline_data.shape[0], new_bs, replace=False)
                        baseline_data = baseline_data[indices]
                    if new_ws < window_data.shape[0]:
                        indices = self._random_generator.choice(window_data.shape[0], new_ws, replace=False)
                        window_data = window_data[indices]

            # 在线标准化（PCA未就绪时）
            if self._pca_fitted_samples < 50 and self._online_samples >= 10:
                window_data = self._standardize_online(window_data)
                baseline_data = self._standardize_online(baseline_data)
            else:
                try:
                    window_data, baseline_data = self._pca_whiten(window_data, baseline_data)
                except Exception:
                    pass

            # 样本量平衡
            if baseline_data.shape[0] > window_data.shape[0] * 5:
                indices = self._random_generator.choice(baseline_data.shape[0], window_data.shape[0], replace=False)
                baseline_data = baseline_data[indices]

            # 核带宽估计（采样）
            sample_size = min(self.DEFAULT_MMD_BANDWIDTH_SAMPLE_SIZE, window_data.shape[0]//2, baseline_data.shape[0]//2)
            sample_size = max(sample_size, 5)
            X_sample = window_data[self._random_generator.choice(window_data.shape[0], sample_size, replace=False)]
            Y_sample = baseline_data[self._random_generator.choice(baseline_data.shape[0], sample_size, replace=False)]
            combined = np.vstack([X_sample, Y_sample])
            dists = cdist(combined, combined)
            sigma = max(np.median(dists), 1e-6) if self.DEFAULT_MMD_SIGMA_MEDIAN else 1.0
            sigma = min(sigma, self.DEFAULT_MMD_SIGMA_UPPER_BOUND)

            # 分块计算（若样本量过大）
            if window_data.shape[0] > self.DEFAULT_MMD_BLOCKWISE_THRESHOLD:
                XX = self._blockwise_rbf_mean(window_data, window_data, sigma)
                YY = self._blockwise_rbf_mean(baseline_data, baseline_data, sigma)
                XY = self._blockwise_rbf_mean(window_data, baseline_data, sigma)
            else:
                XX = np.mean(np.exp(-cdist(window_data, window_data) ** 2 / (2 * sigma ** 2)), dtype=np.float64)
                YY = np.mean(np.exp(-cdist(baseline_data, baseline_data) ** 2 / (2 * sigma ** 2)), dtype=np.float64)
                XY = np.mean(np.exp(-cdist(window_data, baseline_data) ** 2 / (2 * sigma ** 2)), dtype=np.float64)

            mmd = XX + YY - 2 * XY
            if -1e-10 < mmd < 0:
                mmd = 0.0
            elif mmd < -1e-10:
                logger.error(f"MMD负值: {mmd:.15f}")
                mmd = 0.0
            else:
                mmd = max(0.0, float(mmd))
            return (mmd, sigma) if return_sigma else mmd
        except Exception as e:
            logger.warning(f"MMD计算异常: {e}")
            return (0.0, 1.0) if return_sigma else 0.0

    def _blockwise_rbf_mean(self, X: np.ndarray, Y: np.ndarray, sigma: float, block_size: int = 50) -> float:
        """分块计算RBF核矩阵的均值"""
        total = 0.0
        count = 0
        for i in range(0, X.shape[0], block_size):
            X_block = X[i:i+block_size]
            for j in range(0, Y.shape[0], block_size):
                Y_block = Y[j:j+block_size]
                dists = cdist(X_block, Y_block) ** 2
                total += np.sum(np.exp(-dists / (2 * sigma ** 2)))
                count += X_block.shape[0] * Y_block.shape[0]
        return total / count if count > 0 else 0.0

    def _pca_whiten(self, window_data: np.ndarray, baseline_data: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """增量PCA白化，带维度检查"""
        if window_data.shape[1] <= 2 or self._pca_fitted_samples < 50:
            return window_data, baseline_data
        if (self._pca_components is None or self._pca_components.shape[1] != window_data.shape[1]):
            return window_data, baseline_data
        try:
            combined = np.vstack([baseline_data, window_data])
            centered = combined - self._pca_mean
            whitened = centered @ self._pca_components.T
            n_base = baseline_data.shape[0]
            return whitened[n_base:], whitened[:n_base]
        except Exception:
            return window_data, baseline_data

    def _bocd_update(self, mmd_score: float) -> float:
        """对数空间BOCD后验概率更新（含不对称似然）"""
        try:
            hazard = self._get_dynamic_hazard()
            runlength = np.exp(self._bocd_runlength_log) if self.DEFAULT_RUNLENGTH_STORE_LOG else self._bocd_runlength
            runlength = min(runlength, self.DEFAULT_RUNLENGTH_HARD_CAP)
            log_hazard = np.log(max(hazard, self.MIN_LOG_ARG))
            log_1_minus_hazard = np.log(max(1 - hazard, self.MIN_LOG_ARG))

            mmd_std = max(float(np.std(np.array(self._mmd_history)[-30:]))
                          if len(self._mmd_history) >= 30 else 1.0, 1e-6)
            mmd_norm = mmd_score / mmd_std
            log_likelihood_growth = -abs(mmd_norm) * self.DEFAULT_BOCD_LIKELIHOOD_GROWTH_WEIGHT

            dynamic_threshold = self._get_dynamic_mmd_threshold()
            denominator = max(dynamic_threshold, self.MIN_LOG_ARG)
            diff = mmd_score - dynamic_threshold
            # 不对称似然：MMD增大（潜在突变）时给予更高权重
            if diff > 0:
                log_likelihood_change = (-abs(diff) / denominator * self.DEFAULT_BOCD_LIKELIHOOD_CHANGE_WEIGHT *
                                         self.DEFAULT_BOCD_ASYMMETRIC_CHANGE_BOOST)
            else:
                log_likelihood_change = -abs(diff) / denominator * self.DEFAULT_BOCD_LIKELIHOOD_CHANGE_WEIGHT

            log_prior_growth = log_1_minus_hazard * runlength
            log_prior_change = log_hazard
            log_posterior_growth = log_likelihood_growth + log_prior_growth
            log_posterior_change = log_likelihood_change + log_prior_change
            log_max = max(log_posterior_growth, log_posterior_change)

            if np.isneginf(log_max) or np.isnan(log_max):
                logger.debug(f"BOCD全零保护触发: mmd={mmd_score:.4f}, threshold={dynamic_threshold:.4f}")
                return 0.5

            posterior_growth = np.exp(log_posterior_growth - log_max)
            posterior_change = np.exp(log_posterior_change - log_max)

            if posterior_growth < self.DEFAULT_BOCD_LOG_SUM_EXP_MIN_RATIO * posterior_change:
                return 1.0
            if posterior_change < self.DEFAULT_BOCD_LOG_SUM_EXP_MIN_RATIO * posterior_growth:
                return 0.0

            normalization = posterior_growth + posterior_change
            prob_change = posterior_change / normalization if normalization > self.MIN_LOG_ARG else 0.0
            return float(np.clip(prob_change, 0.0, 1.0))
        except Exception as e:
            logger.warning(f"BOCD计算异常: {e}")
            return 0.0

    def _get_dynamic_mmd_threshold(self) -> float:
        """双重阈值：动态分位数 + 硬上限"""
        if len(self._mmd_history) < self.MIN_SAMPLES_FOR_PERCENTILE:
            return self.DEFAULT_MMD_THRESHOLD
        mmd_array = np.array(self._mmd_history)[-100:]
        dynamic = np.percentile(mmd_array, 95)
        dynamic = min(dynamic, self.DEFAULT_MMD_HARD_LIMIT)
        return dynamic

    def _get_dynamic_hazard(self) -> float:
        """动态风险率，基于MMD标准差和游程长度（对数空间）"""
        if len(self._mmd_history) < 20:
            return self.DEFAULT_HAZARD_RATE
        recent_mmd = np.array(self._mmd_history)[-20:]
        mmd_std = float(np.std(recent_mmd))
        adjusted = self.DEFAULT_HAZARD_RATE * (1 + mmd_std / max(self.DEFAULT_MMD_THRESHOLD, self.MIN_LOG_ARG))
        runlength = np.exp(self._bocd_runlength_log) if self.DEFAULT_RUNLENGTH_STORE_LOG else self._bocd_runlength
        runlength_factor = max(0.5, min(2.0, 10.0 / max(runlength, 1)))
        adjusted *= runlength_factor
        return float(np.clip(adjusted, self.DEFAULT_HAZARD_RATE * 0.5, self.DEFAULT_HAZARD_RATE * 5))

    def _get_adaptive_ema_alpha(self) -> float:
        """波动率自适应的EMA平滑系数，使用MAD替代标准差"""
        if len(self._feature_history) < 20:
            return self.DEFAULT_POSTERIOR_EMA_ALPHA
        recent = np.array(list(self._feature_history)[-20:], dtype=np.float64)
        median = np.nanmedian(recent, axis=0)
        mad = np.nanmedian(np.abs(recent - median), axis=0) + 1e-6
        feature_scale = float(np.mean(mad))
        if feature_scale > 2.0:
            return self.DEFAULT_POSTERIOR_EMA_ALPHA_HIGH_VOL
        elif feature_scale < 0.5:
            return self.DEFAULT_POSTERIOR_EMA_ALPHA_LOW_VOL
        return self.DEFAULT_POSTERIOR_EMA_ALPHA

    def _check_baseline_diversity(self) -> None:
        """检查基线的多样性（含条件数检查）"""
        try:
            active_baseline = self._baseline_features[self._baseline_active]
            if len(active_baseline) < self.DEFAULT_BASELINE_SIZE:
                return
            baseline_arr = np.array(list(active_baseline), dtype=np.float64)
            variance = np.mean(np.var(baseline_arr, axis=0))
            if variance < self.DEFAULT_BASELINE_MIN_VARIANCE:
                logger.warning("基线方差过低，触发重新采样")
                self._update_baseline()
                return
            # 条件数检查
            if baseline_arr.shape[1] > 1:
                cov = np.cov(baseline_arr, rowvar=False) + np.eye(baseline_arr.shape[1]) * 1e-8
                eigvals = np.linalg.eigvalsh(cov)
                if eigvals.min() > 0:
                    cond = eigvals.max() / eigvals.min()
                    if cond > self.DEFAULT_BASELINE_CONDITION_NUMBER_THRESHOLD:
                        logger.warning(f"基线条件数过高({cond:.1f})，可能退化")
        except Exception:
            pass

    def _update_baseline(self) -> None:
        """双缓冲基线更新，含锁内快照"""
        with self._lock:
            if len(self._feature_history) < self.DEFAULT_BASELINE_SIZE:
                return
            recent = list(self._feature_history)[-self.DEFAULT_BASELINE_SIZE:]
        inactive = 1 - self._baseline_active
        target = self._baseline_features[inactive]
        target.clear()
        for f in recent:
            target.append(f)
        with self._baseline_lock:
            self._baseline_active = inactive
        # 更新PCA
        try:
            baseline_arr = np.array(list(self._baseline_features[self._baseline_active]), dtype=np.float64)
            self._pca_mean = np.mean(baseline_arr, axis=0)
            centered = baseline_arr - self._pca_mean
            cov = np.cov(centered, rowvar=False) + np.eye(centered.shape[1]) * self.DEFAULT_PCA_REGULARIZATION
            eigvals, eigvecs = np.linalg.eigh(cov)
            eigvals = np.maximum(eigvals, 0)
            cumsum = np.cumsum(eigvals[::-1]) / np.sum(eigvals)
            n_components = max(1, np.searchsorted(cumsum, self.DEFAULT_PCA_VARIANCE_RATIO) + 1)
            self._pca_components = eigvecs[:, -n_components:].T
            self._pca_fitted_samples = baseline_arr.shape[0]
            self._pca_variance_retained = float(cumsum[min(n_components - 1, len(cumsum) - 1)])
        except Exception:
            pass

    def _save_break_snapshot(self, mmd_score: float, bocd_prob: float, cumulative: bool) -> None:
        """保存突变快照，含文件轮转"""
        snapshot = {"timestamp": time.time_ns() / 1e9, "mmd_score": mmd_score, "bocd_probability": bocd_prob,
                    "cumulative_alert": cumulative, "window_sample_count": len(self._feature_history),
                    "baseline_sample_count": len(self._baseline_features[self._baseline_active]),
                    "run_length_log": self._bocd_runlength_log}
        self._break_snapshots.append(snapshot)

        if self.DEFAULT_BREAK_SNAPSHOT_PERSIST:
            try:
                # 按日轮转
                date_str = time.strftime("%Y%m%d")
                persist_path = os.path.join(self._logs_dir, f"break_snapshots_{date_str}.jsonl")
                os.makedirs(self._logs_dir, exist_ok=True)
                json_line = json.dumps(snapshot) + "\n"
                with open(persist_path, 'a') as f:
                    if _PORTALOCKER_AVAILABLE:
                        portalocker.lock(f, portalocker.LOCK_EX)
                        f.write(json_line)
                        f.flush()
                        os.fsync(f.fileno())
                        portalocker.unlock(f)
                    else:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                        f.write(json_line)
                        f.flush()
                        os.fsync(f.fileno())
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                # 清理过期快照
                self._cleanup_old_snapshots()
            except Exception as e:
                logger.warning(f"快照持久化失败: {e}")

    def _cleanup_old_snapshots(self) -> None:
        """清理超过保留天数的快照文件"""
        try:
            import glob
            cutoff = time.time() - self.DEFAULT_SNAPSHOT_ROTATE_DAYS * 86400
            pattern = os.path.join(self._logs_dir, "break_snapshots_*.jsonl")
            for path in glob.glob(pattern):
                try:
                    mtime = os.path.getmtime(path)
                    if mtime < cutoff:
                        os.remove(path)
                except Exception:
                    pass
        except Exception:
            pass

    def _try_cleanup(self) -> None:
        now = time.time()
        if now - self._last_cleanup < self.DEFAULT_CLEANUP_INTERVAL_SEC:
            return
        with self._lock:
            excess = len(self._feature_history) - self.DEFAULT_MAX_HISTORY
            if excess <= 0:
                self._last_cleanup = now
                return

            max_protected = int(self.DEFAULT_MAX_HISTORY * self.DEFAULT_EXTREME_PROTECT_MAX_RATIO)
            protect_age_limit = self.DEFAULT_EXTREME_PROTECT_MAX_AGE_MULT * self.DEFAULT_WINDOW_SIZE
            removed = 0
            protected_skipped = 0
            while removed < excess and len(self._feature_history) > self.MIN_SAMPLES_FOR_DETECTION:
                age = len(self._feature_history) - (removed + 1)  # 粗略年龄估计
                is_protected = (protected_skipped < max_protected and age < protect_age_limit and
                                len(self._extreme_flags) > 0 and self._extreme_flags[0])

                if is_protected:
                    # 重新入队
                    val = self._feature_history[0]
                    self._feature_history.popleft()
                    self._feature_history.append(val)
                    self._extreme_flags.popleft()
                    self._extreme_flags.append(True)
                    if self._mmd_history:
                        mmd_val = self._mmd_history[0]
                        self._mmd_history.popleft()
                        self._mmd_history.append(mmd_val)
                    if self._mmd_sigma_history:
                        sig_val = self._mmd_sigma_history[0]
                        self._mmd_sigma_history.popleft()
                        self._mmd_sigma_history.append(sig_val)
                    protected_skipped += 1
                else:
                    self._feature_history.popleft()
                    if self._extreme_flags:
                        self._extreme_flags.popleft()
                    if self._mmd_history:
                        self._mmd_history.popleft()
                    if self._mmd_sigma_history:
                        self._mmd_sigma_history.popleft()
                    removed += 1

            # 对齐：不足时用NaN填充
            target_len = len(self._feature_history)
            for dq in [self._extreme_flags, self._mmd_history, self._mmd_sigma_history]:
                while len(dq) > target_len:
                    dq.popleft()
                while len(dq) < target_len:
                    if dq is self._mmd_history or dq is self._mmd_sigma_history:
                        dq.append(np.nan)
                    else:
                        dq.append(False)
        self._last_cleanup = now

    def _trigger_alert(self, level: str, message: str, details: Optional[Dict] = None) -> None:
        now = time.time()
        with self._alert_lock:
            self._alert_history.append({"level": level, "timestamp": now})
            cutoff = now - self.DEFAULT_ALERT_UPGRADE_WINDOW_SEC
            while self._alert_history and self._alert_history[0]["timestamp"] < cutoff:
                self._alert_history.popleft()
            critical_count = sum(1 for a in self._alert_history if a["level"] == "critical")
            if critical_count >= self.DEFAULT_ALERT_UPGRADE_COUNT:
                level = "emergency"
                message = f"[UPGRADED] {message}"

        # 紧急防御指令
        if level == "emergency" and self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'send_defense_command'):
            for attempt in range(3):
                try:
                    ack = self._negotiation_bus.send_defense_command(
                        command="freeze_factors_and_degrade_strategies",
                        reason="structure_break_emergency", timestamp=now, timeout=0.1)
                    if ack:
                        break
                except Exception:
                    if attempt == 2:
                        logger.critical("紧急防御指令发送失败，已重试3次")

        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(alert_type="structure_break", level=level,
                                                    message=message, timestamp=now, details=details or {})
            except Exception as e:
                logger.warning(f"协商总线告警推送失败: {e}")

        alert_msg = f"[{level.upper()}] {message}"
        if level == "emergency":
            logger.critical(f"{alert_msg} #RECOVERY: 全系统因子冻结、策略降级")
        elif level == "critical":
            logger.error(f"{alert_msg} #RECOVERY: 检查市场状态、重置参数")

        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(event_type="structure_break_detection",
                                                  details={"level": level, "message": message, "details": details or {}})
            except Exception:
                pass
