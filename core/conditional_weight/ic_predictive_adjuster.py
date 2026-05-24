"""
火种系统 · IC预测性调整器 (ICPredictiveAdjuster)

核心职责：
1. 基于因子 IC 序列的变化趋势（加速度），对因子权重进行前瞻性微调——在 IC 持续恶化前提前降权，在 IC 改善时提前升权。
2. 对因子 IC 的显著性执行 FDR（错误发现率）控制，剔除统计上不稳健的因子。

外部依赖（真实模块接口）：
- 无外部模块依赖，所有计算基于传入的原始 IC 序列和当前权重。

接口契约：
- adjust_weights(ic_series: Dict[str, np.ndarray], current_weights: Dict[str, float]) -> Dict[str, Any]
  输出字典固定包含 "adjusted_weights" (Dict[str, float]), "adjustments" (Dict[str, float]), "reason" (str), "warnings" (List[str])
- health_check() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "message" (str)

异常与降级：
- 若某个因子的 IC 序列长度不足（低于最小样本数），该因子不参与加速度计算，权重保持不变，并记录 WARNING。
- 若 FDR 校正后无因子通过显著性检验，则所有因子权重保持不变，并记录 WARNING，不强制清零。
- 若输入数据包含 NaN/Inf，自动过滤并记录 WARNING，使用有效部分继续计算。
- 若所有权重调整后总和为零，回退为均匀分布。

资源管理：
- 本模块无状态，所有计算为纯函数，不持有任何需要手动释放的资源。
- 内部统计快照（_last_adjustments 等）仅用于监控和审计，使用轻量级锁保护。

并发安全：
- 对内部状态字典的读写使用 threading.Lock 保护，允许多个策略线程安全地查询调整结果。
"""

import logging
import time
import threading
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
from scipy import stats

logger = logging.getLogger(__name__)


class ICPredictiveAdjuster:
    """IC 预测性调整器：基于 IC 加速度提前调整权重，并控制 FDR"""

    # 类常量（默认配置，附带单位与取值范围注释）
    MIN_IC_LENGTH = 20                  # IC 序列最小长度，个，取值范围 [10, 100]
    ACCELERATION_WINDOW = 10            # 加速度计算窗口，个，取值范围 [3, 30]
    ACCELERATION_THRESHOLD = 0.005      # 加速度触发阈值，无量纲，取值范围 [0.001, 0.05]
    NEGATIVE_IC_PENALTY_MULT = 1.5      # 负 IC 惩罚倍数，无量纲，取值范围 [1.0, 3.0]
    POSITIVE_IC_REWARD_MULT = 1.0        # 正 IC 奖励倍数，无量纲，取值范围 [0.5, 1.5]
    MAX_WEIGHT_ADJUSTMENT_PCT = 0.25     # 单次最大权重调整比例，无量纲，取值范围 [0.1, 0.5]
    FDR_ALPHA = 0.1                     # FDR 显著性水平，无量纲，取值范围 [0.01, 0.2]
    FDR_MIN_SAMPLES = 30                # FDR 检验最小样本数，个，取值范围 [20, 100]
    HEALTH_SAMPLE_FACTORS = 3            # 健康检查使用的模拟因子数，个，取值范围 [2, 5]
    HEALTH_SAMPLE_SIZE = 60              # 健康检查使用的模拟样本数，个，取值范围 [30, 200]
    MAX_NAN_RATIO = 0.3                 # 序列中允许的最大缺失值比例，无量纲，取值范围 [0.0, 0.5]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        初始化 IC 预测调整器。
        所有参数优先从配置字典读取，缺失时使用类常量作为安全默认值。
        对传入配置执行严格边界校验，非法值自动回退至默认值。
        """
        cfg = config or {}
        self._min_ic_length = self._validate_int(
            cfg.get("min_ic_length", self.MIN_IC_LENGTH), 10, 100, "min_ic_length"
        )
        self._acceleration_window = self._validate_int(
            cfg.get("acceleration_window", self.ACCELERATION_WINDOW), 3, 30, "acceleration_window"
        )
        self._acceleration_threshold = self._validate_float(
            cfg.get("acceleration_threshold", self.ACCELERATION_THRESHOLD), 0.001, 0.05, "acceleration_threshold"
        )
        self._negative_penalty_mult = self._validate_float(
            cfg.get("negative_ic_penalty_mult", self.NEGATIVE_IC_PENALTY_MULT), 1.0, 3.0, "negative_penalty_mult"
        )
        self._positive_reward_mult = self._validate_float(
            cfg.get("positive_ic_reward_mult", self.POSITIVE_IC_REWARD_MULT), 0.5, 1.5, "positive_reward_mult"
        )
        self._max_adjustment_pct = self._validate_float(
            cfg.get("max_weight_adjustment_pct", self.MAX_WEIGHT_ADJUSTMENT_PCT), 0.1, 0.5, "max_adjustment_pct"
        )
        self._fdr_alpha = self._validate_float(
            cfg.get("fdr_alpha", self.FDR_ALPHA), 0.01, 0.2, "fdr_alpha"
        )
        self._fdr_min_samples = self._validate_int(
            cfg.get("fdr_min_samples", self.FDR_MIN_SAMPLES), 20, 100, "fdr_min_samples"
        )
        self._max_nan_ratio = self._validate_float(
            cfg.get("max_nan_ratio", self.MAX_NAN_RATIO), 0.0, 0.5, "max_nan_ratio"
        )

        # 内部状态与锁
        self._lock = threading.Lock()
        self._last_adjustments: Dict[str, float] = {}
        self._last_update_time: float = 0.0
        # 监控指标
        self._metrics: Dict[str, Any] = {
            "total_adjustments": 0,
            "avg_adjustment_magnitude": 0.0,
            "fdr_dropped_count": 0
        }

        logger.info(
            f"ICPredictiveAdjuster 初始化完成: "
            f"min_len={self._min_ic_length}, "
            f"accel_win={self._acceleration_window}, "
            f"accel_thr={self._acceleration_threshold:.4f}, "
            f"neg_mult={self._negative_penalty_mult}, "
            f"pos_mult={self._positive_reward_mult}, "
            f"max_adj={self._max_adjustment_pct:.0%}, "
            f"fdr_alpha={self._fdr_alpha}"
        )

    # ────────────────────────── 配置校验 ──────────────────────────
    @staticmethod
    def _validate_int(value: Any, low: int, high: int, name: str) -> int:
        """校验整数配置在边界内，否则返回默认值并警告"""
        try:
            v = int(value)
            if low <= v <= high:
                return v
            logger.warning(f"配置 {name}={value} 超出范围 [{low},{high}]，使用默认值")
        except (ValueError, TypeError):
            logger.warning(f"配置 {name}={value} 无效，使用默认值")
        return getattr(ICPredictiveAdjuster, name.upper(), None) or low

    @staticmethod
    def _validate_float(value: Any, low: float, high: float, name: str) -> float:
        """校验浮点配置在边界内，否则返回默认值并警告"""
        try:
            v = float(value)
            if low <= v <= high:
                return v
            logger.warning(f"配置 {name}={value} 超出范围 [{low},{high}]，使用默认值")
        except (ValueError, TypeError):
            logger.warning(f"配置 {name}={value} 无效，使用默认值")
        return getattr(ICPredictiveAdjuster, name.upper(), None) or low

    # ────────────────────────── 公共接口 ──────────────────────────
    def adjust_weights(
        self,
        ic_series: Dict[str, np.ndarray],
        current_weights: Dict[str, float]
    ) -> Dict[str, Any]:
        """
        根据 IC 序列预测性调整因子权重。
        
        参数:
            ic_series: 键为因子名，值为该因子的近期 IC 值序列（按时间升序排列）。
            current_weights: 当前的因子权重映射。
        
        返回:
            标准化字典，包含调整后的权重、调整量、原因和警告。
        """
        warnings: List[str] = []

        # 1. 边界校验：输入为空
        if not ic_series or not current_weights:
            reason = "输入 IC 序列或当前权重为空，返回原始权重"
            logger.debug(reason)
            return {
                "adjusted_weights": {**current_weights},
                "adjustments": {name: 0.0 for name in current_weights},
                "reason": reason,
                "warnings": []
            }

        adjusted_weights = {**current_weights}
        adjustments: Dict[str, float] = {}

        # 2. 数据清洗：处理 NaN/Inf
        clean_ic_series, clean_warnings = self._clean_ic_data(ic_series)
        warnings.extend(clean_warnings)

        # 3. FDR 显著性过滤
        fdr_retained = self._apply_fdr(clean_ic_series)
        fdr_dropped = set(clean_ic_series.keys()) - fdr_retained
        if fdr_dropped:
            drop_count = len(fdr_dropped)
            warn = f"FDR 过滤移除因子 ({drop_count} 个): {list(fdr_dropped)[:5]}..."
            logger.warning(warn)
            warnings.append(warn)
            with self._lock:
                self._metrics["fdr_dropped_count"] += drop_count
            # 对 FDR 未通过的因子，权重置零并标记调整量
            for name in fdr_dropped:
                if name in adjusted_weights:
                    adjustments[name] = -adjusted_weights[name]
                    adjusted_weights[name] = 0.0

        # 4. 逐因子计算 IC 加速度并调整权重
        for name, ic_values in clean_ic_series.items():
            if name in fdr_dropped:
                continue
            if len(ic_values) < self._min_ic_length:
                warn = f"因子 {name} IC 序列长度 ({len(ic_values)}) 不足 {self._min_ic_length}，权重保持不变"
                logger.debug(warn)
                warnings.append(warn)
                adjustments[name] = 0.0
                continue

            accel = self._calculate_ic_acceleration(ic_values)

            if abs(accel) < self._acceleration_threshold:
                adjustments[name] = 0.0
                continue

            current_ic = float(np.mean(ic_values[-5:]))
            # 计算调整幅度
            raw_adj = self._compute_raw_adjustment(current_ic, accel)
            current_w = current_weights.get(name, 0.0)
            max_delta = self._max_adjustment_pct * current_w
            delta = max(-max_delta, min(max_delta, raw_adj * current_w))

            new_w = current_w + delta
            if new_w < 0.0:
                new_w = 0.0
                delta = -current_w

            adjusted_weights[name] = new_w
            adjustments[name] = delta
            logger.debug(f"因子 {name}: ic={current_ic:.4f}, accel={accel:.4f}, delta={delta:.4f}, new_w={new_w:.4f}")

        # 5. 归一化
        total_w = sum(adjusted_weights.values())
        if total_w <= 0.0:
            n = len(adjusted_weights)
            if n > 0:
                uniform = 1.0 / n
                adjusted_weights = {name: uniform for name in adjusted_weights}
                for name in adjusted_weights:
                    adjustments[name] = uniform - current_weights.get(name, 0.0)
            reason = "所有权重为零，回退为均匀分布"
            logger.warning(reason)
            warnings.append(reason)
        else:
            for name in adjusted_weights:
                adjusted_weights[name] /= total_w

        # 6. 更新监控状态
        self._update_metrics(adjustments)

        reason = (
            f"IC 预测调整完成，{len(ic_series)} 个因子参与评估，"
            f"FDR 保留 {len(fdr_retained)} 个，"
            f"调整因子 {sum(1 for d in adjustments.values() if abs(d) > 1e-6)} 个"
        )
        logger.info(reason)

        return {
            "adjusted_weights": adjusted_weights,
            "adjustments": adjustments,
            "reason": reason,
            "warnings": warnings
        }

    def get_last_adjustments(self) -> Dict[str, float]:
        """获取最近一次调整量快照，线程安全"""
        with self._lock:
            return self._last_adjustments.copy()

    def get_metrics(self) -> Dict[str, Any]:
        """获取累积监控指标"""
        with self._lock:
            return dict(self._metrics)

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检：使用模拟数据运行完整调整流程，并测试异常边界。"""
        try:
            rng = np.random.RandomState(42)
            factors = [f"health_f{i}" for i in range(cls.HEALTH_SAMPLE_FACTORS)]
            ic_series = {
                factors[0]: rng.randn(cls.HEALTH_SAMPLE_SIZE) * 0.02 + 0.01,
                factors[1]: rng.randn(cls.HEALTH_SAMPLE_SIZE) * 0.02 - 0.005,
                factors[2]: rng.randn(cls.HEALTH_SAMPLE_SIZE) * 0.01,
            }
            # 加入一个含 NaN 的序列，测试清洗功能
            dirty = rng.randn(cls.HEALTH_SAMPLE_SIZE) * 0.02
            dirty[0] = np.nan
            ic_series["health_nan"] = dirty

            current_weights = {f: 0.25 for f in ic_series.keys()}

            instance = cls()
            result = instance.adjust_weights(ic_series, current_weights)

            # 验证返回字典
            for key in ("adjusted_weights", "adjustments", "reason", "warnings"):
                if key not in result:
                    return {"status": "error", "message": f"缺少键: {key}"}
            # 验证所有权重非负且和为 1
            for w in result["adjusted_weights"].values():
                if w < -1e-9:
                    return {"status": "error", "message": f"权重为负: {w}"}
            total = sum(result["adjusted_weights"].values())
            if abs(total - 1.0) > 0.01:
                return {"status": "error", "message": f"权重和不为 1: {total:.4f}"}

            # 测试空输入
            empty_result = instance.adjust_weights({}, {})
            if empty_result.get("status") == "error":
                return {"status": "error", "message": "空输入处理失败"}

            return {"status": "ok", "message": "健康检查通过"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _clean_ic_data(self, ic_series: Dict[str, np.ndarray]) -> Tuple[Dict[str, np.ndarray], List[str]]:
        """清洗 IC 序列：移除 NaN/Inf，返回有效数据字典和警告列表"""
        clean = {}
        warnings = []
        for name, arr in ic_series.items():
            mask = np.isfinite(arr)
            valid_ratio = mask.sum() / max(len(arr), 1)
            if valid_ratio < 1.0 - self._max_nan_ratio:
                warn = f"因子 {name} 缺失值过多 ({1-valid_ratio:.1%})，跳过该因子"
                logger.warning(warn)
                warnings.append(warn)
                continue
            if not np.all(mask):
                arr = arr[mask]
                warnings.append(f"因子 {name} 存在缺失值，已剔除")
            clean[name] = arr
        return clean, warnings

    def _calculate_ic_acceleration(self, ic_values: np.ndarray) -> float:
        """计算 IC 序列的加速度（二阶趋势），返回归一化后的加速度值"""
        if len(ic_values) < 2 * self._acceleration_window:
            return 0.0
        recent = ic_values[-self._acceleration_window:]
        older = ic_values[-2 * self._acceleration_window:-self._acceleration_window]
        mean_recent = float(np.mean(recent))
        mean_older = float(np.mean(older))
        velocity = mean_recent - mean_older
        if len(ic_values) >= 3 * self._acceleration_window:
            oldest = ic_values[-3 * self._acceleration_window:-2 * self._acceleration_window]
            mean_oldest = float(np.mean(oldest))
            acceleration = velocity - (mean_older - mean_oldest)
        else:
            acceleration = velocity
        std = float(np.std(ic_values))
        if std > 1e-10:
            acceleration /= std
        return float(np.tanh(acceleration))

    @staticmethod
    def _compute_raw_adjustment(current_ic: float, accel: float) -> float:
        """根据当前 IC 方向和加速度方向计算调整量"""
        if current_ic > 0 and accel > 0:
            return abs(accel) * ICPredictiveAdjuster.POSITIVE_IC_REWARD_MULT
        elif current_ic > 0 and accel < 0:
            return -abs(accel) * ICPredictiveAdjuster.NEGATIVE_IC_PENALTY_MULT
        elif current_ic < 0 and accel < 0:
            return -abs(accel) * ICPredictiveAdjuster.NEGATIVE_IC_PENALTY_MULT * 1.2
        elif current_ic < 0 and accel > 0:
            return -abs(accel) * ICPredictiveAdjuster.NEGATIVE_IC_PENALTY_MULT * 0.5
        return 0.0

    def _apply_fdr(self, ic_series: Dict[str, np.ndarray]) -> set:
        """对因子 IC 进行 FDR (Benjamini-Hochberg) 校正，返回通过检验的因子名集合"""
        p_values: List[Tuple[str, float]] = []
        for name, ic_values in ic_series.items():
            if len(ic_values) < self._fdr_min_samples:
                continue
            t_stat, p_val = stats.ttest_1samp(ic_values, 0.0)
            p_values.append((name, float(p_val)))
        if not p_values:
            return set(ic_series.keys())
        p_values.sort(key=lambda x: x[1])
        n = len(p_values)
        rejected = set()
        for rank, (name, p_val) in enumerate(p_values, start=1):
            bh_critical = self._fdr_alpha * rank / n
            if p_val <= bh_critical:
                rejected.add(name)
            else:
                break
        if not rejected:
            logger.warning("FDR 校正后无因子通过显著性检验，保留所有因子")
            return set(ic_series.keys())
        return rejected

    def _update_metrics(self, adjustments: Dict[str, float]) -> None:
        """更新内部监控指标"""
        with self._lock:
            self._last_adjustments = adjustments.copy()
            self._last_update_time = time.time()
            count = sum(1 for d in adjustments.values() if abs(d) > 1e-6)
            magnitude = sum(abs(d) for d in adjustments.values()) / max(len(adjustments), 1)
            n = self._metrics["total_adjustments"]
            old_avg = self._metrics["avg_adjustment_magnitude"]
            self._metrics["total_adjustments"] += 1
            self._metrics["avg_adjustment_magnitude"] = old_avg + (magnitude - old_avg) / (n + 1)
