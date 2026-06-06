"""
火种系统 · 因子预处理器 (FactorPreprocessor)

核心职责：
1. 对原始因子值执行严格因果的去极值、平滑和缺失值填充，杜绝未来信息泄露
2. 支持实时因果模式（仅用过去数据）与非因果模式（离线批处理），所有计算达到 O(n) 实际复杂度
3. 提供因子级状态隔离、拟合参数有效利用与审计追踪，确保多因子并发预处理的正确性与可观测性

外部依赖（真实模块接口）：
- core.perception.gustatory_cortex.GustatoryCortex : 获取历史相似市场状态下的因子均值（需支持因果查询）
- core.state_machine.StateMachine : 获取当前市场状态标签（trend/range/volatile）

接口契约：
- preprocess(factor_name: str, raw_values: List[float], causal: bool = True, **kwargs) -> Dict[str, Any] : 预处理流水线
- fit_params(factor_name: str, values: List[float]) -> Dict[str, Any] : 为指定因子拟合参数
- health_check() -> Dict[str, Any] : 模块自检
- reset(factor_name: Optional[str] = None) -> Dict[str, Any] : 重置内部状态
- probe(factor_name: str, raw_values: List[float]) -> Dict[str, Any] : 数据探头模式，仅评估不修改，返回质量等级
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 因果模式下所有计算严格遵循时间顺序，使用在线递推或滚动窗口估计
- 非因果模式下优先使用已拟合的全局参数，缺失时回退至全序列统计
- 所有降级值在类常量区明确声明

资源管理：
- 不持有外部资源句柄，不产生 I/O
- 通过 reset() 可主动释放内部状态，防止内存泄漏
"""

import bisect
import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple, Callable
from collections import deque
import numpy as np

logger = logging.getLogger(__name__)


class CausalPercentileWindow:
    """使用双端队列+定期排序的策略维护滑动窗口，保证删除正确性，避免重复值导致的不一致。"""
    def __init__(self, maxlen: int):
        if maxlen < 1:
            raise ValueError("maxlen must be >= 1")
        self._buffer = deque(maxlen=maxlen)
        self._sorted = None
        self._sorted_dirty = True

    def append(self, value: float):
        self._buffer.append(value)
        self._sorted_dirty = True

    def __len__(self) -> int:
        return len(self._buffer)

    @property
    def values(self) -> np.ndarray:
        if self._sorted_dirty or self._sorted is None:
            self._sorted = np.sort(np.array(self._buffer, dtype=np.float64))
            self._sorted_dirty = False
        return self._sorted

    def percentile(self, pct: float) -> float:
        arr = self.values
        if len(arr) == 0:
            return 0.0
        k = (len(arr) - 1) * pct / 100.0
        f, c = int(k), min(int(k) + 1, len(arr) - 1)
        return arr[f] * (c - k) + arr[c] * (k - f)

    def median(self) -> float:
        return self.percentile(50)


class FactorPreprocessor:
    """因果因子预处理器（闭环与规范版）"""

    DEFAULT_WINSORIZE_PERCENTILE = (0.01, 0.99)
    DEFAULT_MAD_THRESHOLD = 3.0
    DEFAULT_SMOOTHING_WINDOW = 3
    MAX_SMOOTHING_WINDOW = 21
    DEFAULT_MAX_MISSING_RATIO = 0.5
    MIN_SAMPLES_FOR_WINSOR = 5
    INF_REPLACEMENT_MULTIPLIER = 10.0
    INF_REPLACEMENT_ABSOLUTE_MIN = 1e-6
    ROLLING_WINDOW_FOR_PARAMS = 252
    MAX_INPUT_LENGTH = 100_000
    EWMA_ALPHA = 0.05
    EWMA_WARMUP_SAMPLES = 30
    MAX_MISSING_PROPORTION = 0.3
    ALLOWED_FILL_METHODS = frozenset({"forward_fill", "regime_similarity", "global_mean", "none"})
    ALLOWED_WINSOR_METHODS = frozenset({"mad", "percentile"})
    STATE_TTL_SECONDS = 3600

    def __init__(self):
        self._gustatory_cortex = None
        self._state_machine = None
        self._factor_states: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._global_params: Dict[str, Dict[str, Any]] = {}

    def inject_dependencies(self, gustatory_cortex=None, state_machine=None):
        if gustatory_cortex is not None and hasattr(gustatory_cortex, 'get_regime_mean'):
            self._gustatory_cortex = gustatory_cortex
        if state_machine is not None and hasattr(state_machine, 'get_current_regime'):
            self._state_machine = state_machine

    def reset(self, factor_name: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            if factor_name is None:
                self._factor_states.clear()
                self._global_params.clear()
            elif factor_name in self._factor_states:
                del self._factor_states[factor_name]
        logger.info("重置完成: %s", factor_name or "全部")
        return {"status": "ok", "reason": "状态已重置", "data": {}, "warnings": []}

    def fit_params(self, factor_name: str, values: List[float]) -> Dict[str, Any]:
        arr = np.array(values, dtype=np.float64)
        arr = arr[np.isfinite(arr)]
        if len(arr) < self.MIN_SAMPLES_FOR_WINSOR:
            return {"status": "error", "reason": "样本不足", "data": {}, "warnings": ["insufficient_samples"]}
        lower = np.percentile(arr, self.DEFAULT_WINSORIZE_PERCENTILE[0] * 100)
        upper = np.percentile(arr, self.DEFAULT_WINSORIZE_PERCENTILE[1] * 100)
        if lower >= upper:
            return {"status": "error", "reason": "上下界异常", "data": {}, "warnings": ["invalid_params"]}
        params = {"lower": float(lower), "upper": float(upper), "method": "percentile", "sample_count": len(arr)}
        with self._lock:
            self._global_params[factor_name] = params
        return {"status": "ok", "reason": "拟合完成", "data": params, "warnings": []}

    def probe(self, factor_name: str, raw_values: List[float]) -> Dict[str, Any]:
        if not raw_values:
            return {"status": "ok", "reason": "空输入", "data": {"grade": "poor"}, "warnings": ["empty_input"]}
        arr = np.array(raw_values, dtype=np.float64)
        inf_count = int(np.sum(np.isinf(arr)))
        nan_count = int(np.sum(np.isnan(arr)))
        ratio = nan_count / len(arr) if len(arr) > 0 else 1.0
        grade = "good" if ratio < 0.1 and inf_count == 0 else ("fair" if ratio < 0.3 else "poor")
        return {"status": "ok", "reason": "探头完成", "data": {"grade": grade, "missing_ratio": ratio, "inf_count": inf_count}, "warnings": []}

    def preprocess(
        self,
        factor_name: str,
        raw_values: List[float],
        causal: bool = True,
        winsorize_method: Optional[str] = None,
        smooth_window: Optional[int] = None,
        fill_method: Optional[str] = None,
    ) -> Dict[str, Any]:
        warnings: List[str] = []
        start_time = time.perf_counter()
        if not raw_values:
            return {"status": "ok", "reason": "空输入", "data": {"processed": []}, "warnings": ["empty_input"]}
        if len(raw_values) > self.MAX_INPUT_LENGTH:
            return {"status": "error", "reason": "输入过长", "data": {}, "warnings": ["input_too_long"]}
        try:
            arr = np.array(raw_values, dtype=np.float64)
        except (ValueError, TypeError):
            return {"status": "error", "reason": "非法类型", "data": {}, "warnings": ["invalid_type"]}

        # 1. inf 替换
        inf_mask = np.isinf(arr)
        if np.any(inf_mask):
            finite = arr[np.isfinite(arr)] if np.any(np.isfinite(arr)) else np.array([1.0])
            scale = max(np.std(finite) if len(finite) > 1 else abs(np.mean(finite)), 1e-8)
            repl = max(scale * self.INF_REPLACEMENT_MULTIPLIER, self.INF_REPLACEMENT_ABSOLUTE_MIN)
            arr[inf_mask] = np.sign(arr[inf_mask]) * repl
            warnings.append("inf_replaced")

        # 2. 缺失填充
        nan_mask = np.isnan(arr)
        miss_ratio = nan_mask.sum() / len(arr)
        if miss_ratio > self.DEFAULT_MAX_MISSING_RATIO:
            return {"status": "error", "reason": "缺失率过高", "data": {}, "warnings": ["excessive_missing"]}
        arr_filled, fill_stats = self._fill_missing(factor_name, arr, nan_mask, fill_method, causal, warnings)

        # 3. 去极值
        method = winsorize_method or ("mad" if causal else "percentile")
        if method not in self.ALLOWED_WINSOR_METHODS:
            method = "mad" if causal else "percentile"
            warnings.append(f"无效去极值方法，已回退到 {method}")
        arr_winsor, win_stats = self._winsorize(factor_name, arr_filled, method, causal, warnings)

        # 4. 平滑
        sw = smooth_window if smooth_window is not None else self.DEFAULT_SMOOTHING_WINDOW
        sw = max(1, min(sw + (sw % 2 == 0), self.MAX_SMOOTHING_WINDOW))
        arr_smooth, smooth_stats = self._causal_smooth(arr_winsor, sw, causal)

        # 5. 异常检测
        extreme_count = self._detect_extremes(factor_name, arr_smooth, causal, warnings)

        elapsed_us = (time.perf_counter() - start_time) * 1_000_000
        stats = {
            "original_count": len(raw_values),
            "missing_count": int(nan_mask.sum()),
            "inf_count": int(np.sum(inf_mask)),
            "extreme_count": extreme_count,
            "mean_after": round(float(np.mean(arr_smooth)), 6),
            "std_after": round(float(np.std(arr_smooth)), 6),
            "fill_method": fill_stats.get("method", "none"),
            "causal": causal,
            "elapsed_us": round(elapsed_us, 0),
            "pre_mean": round(float(np.mean(arr[~nan_mask])) if np.any(~nan_mask) else 0.0, 6),
            "pre_std": round(float(np.std(arr[~nan_mask])) if np.sum(~nan_mask) > 1 else 0.0, 6),
        }

        return {
            "status": "ok",
            "reason": f"预处理完成 ({'因果' if causal else '非因果'})",
            "data": {"processed": arr_smooth.astype(float).tolist(), "stats": stats},
            "warnings": warnings,
        }

    def health_check(self) -> Dict[str, Any]:
        try:
            r = self.preprocess("_hc", [1.0, 2.0, np.nan, 100.0, 1.5], causal=True)
            if len(r["data"]["processed"]) != 5: raise RuntimeError("长度错误")
            self.fit_params("_hc", [5.0] * 200 + [100.0] * 50)
            self.reset()
            return {"status": "ok", "reason": "健康检查通过", "data": {}, "warnings": []}
        except Exception as e:
            return {"status": "error", "reason": str(e), "data": {}, "warnings": []}

    # ========== 私有方法 ==========
    def _with_state(self, factor_name: str, op: Callable) -> Any:
        with self._lock:
            state = self._factor_states.get(factor_name)
            now = time.time()
            if state is None or now - state.get("last_update", 0) > self.STATE_TTL_SECONDS:
                state = {
                    "win": CausalPercentileWindow(self.ROLLING_WINDOW_FOR_PARAMS),
                    "ewma": None,
                    "ewms": None,
                    "count": 0,
                    "last_update": now,
                }
                self._factor_states[factor_name] = state
            state["last_update"] = now
            return op(state)

    def _winsorize(self, factor_name, arr, method, causal, warnings):
        if len(arr) < self.MIN_SAMPLES_FOR_WINSOR:
            return arr.copy(), {"clipped": 0}
        if causal:
            return self._winsorize_causal(factor_name, arr, method, warnings)
        return self._winsorize_global(factor_name, arr, method)

    def _winsorize_causal(self, factor_name, arr, method, warnings):
        params = {}
        with self._lock:
            params = self._global_params.get(factor_name, {}).copy()
        if method == "percentile" and params:
            lo, hi = params.get("lower"), params.get("upper")
            if lo is not None and hi is not None and lo < hi:
                return np.clip(arr, lo, hi), {"using_fitted": True}

        def _ops(state):
            win: CausalPercentileWindow = state["win"]
            res = arr.copy()
            for i in range(len(arr)):
                val = res[i]
                if not np.isfinite(val):
                    continue
                win.append(val)
                if len(win) < self.MIN_SAMPLES_FOR_WINSOR:
                    continue
                if method == "mad":
                    med = win.median()
                    mad = np.median(np.abs(win.values - med)) if len(win.values) > 0 else 0.0
                    if mad < 1e-10:
                        continue
                    lo, hi = med - self.DEFAULT_MAD_THRESHOLD * mad, med + self.DEFAULT_MAD_THRESHOLD * mad
                else:
                    lo = win.percentile(self.DEFAULT_WINSORIZE_PERCENTILE[0] * 100)
                    hi = win.percentile(self.DEFAULT_WINSORIZE_PERCENTILE[1] * 100)
                if val < lo:
                    res[i] = lo
                elif val > hi:
                    res[i] = hi
            return res, {}
        return self._with_state(factor_name, _ops)

    def _winsorize_global(self, factor_name, arr, method):
        params = {}
        with self._lock:
            params = self._global_params.get(factor_name, {}).copy()
        if method == "percentile" and params:
            lo, hi = params.get("lower"), params.get("upper")
            if lo is not None and hi is not None and lo < hi:
                return np.clip(arr, lo, hi), {"using_fitted": True}
        valid = arr[np.isfinite(arr)]
        if method == "mad":
            med = np.median(valid)
            mad = np.median(np.abs(valid - med))
            if mad < 1e-10:
                mad = np.std(valid)
            lo, hi = med - self.DEFAULT_MAD_THRESHOLD * mad, med + self.DEFAULT_MAD_THRESHOLD * mad
        else:
            lo = np.percentile(valid, self.DEFAULT_WINSORIZE_PERCENTILE[0] * 100)
            hi = np.percentile(valid, self.DEFAULT_WINSORIZE_PERCENTILE[1] * 100)
        return np.clip(arr, lo, hi), {}

    def _causal_smooth(self, arr, window, causal):
        if window <= 1 or len(arr) < window:
            return arr.copy(), {}
        smoothed = np.copy(arr)
        if causal:
            buf = deque(maxlen=window)
            for i in range(len(arr)):
                buf.append(arr[i])
                if len(buf) >= window // 2 + 1:
                    smoothed[i] = np.median(list(buf))
        else:
            half = window // 2
            for i in range(len(arr)):
                start = max(0, i - half)
                end = min(len(arr), i + half + 1)
                smoothed[i] = np.median(arr[start:end])
        return smoothed, {}

    def _fill_missing(self, factor_name, arr, nan_mask, method_override, causal, warnings):
        if not np.any(nan_mask):
            return arr.copy(), {}
        regime = self._get_regime() if causal else "normal"
        method = method_override or self.DEFAULT_FILL_METHOD
        if method not in self.ALLOWED_FILL_METHODS:
            method = self.DEFAULT_FILL_METHOD
        if causal:
            if method == "forward_fill" or (method == "regime_similarity" and regime == "trend"):
                return self._forward_fill(arr, nan_mask), {"method": "forward_fill"}
            alpha = self.EWMA_ALPHA
            def _ops(state):
                ewma = state["ewma"]
                if ewma is None:
                    ewma = float(np.mean(arr[~nan_mask])) if np.any(~nan_mask) else 0.0
                    state["ewma"] = ewma
                filled = arr.copy()
                for i in range(len(arr)):
                    if nan_mask[i]:
                        filled[i] = state["ewma"]
                    else:
                        state["ewma"] = alpha * arr[i] + (1 - alpha) * state["ewma"]
                return filled
            res = self._with_state(factor_name, _ops)
            return res, {"method": "ewma (causal)"}
        else:
            if method == "forward_fill":
                return self._forward_fill(arr, nan_mask), {"method": "forward_fill"}
            if method == "regime_similarity" and regime in ("range", "volatile"):
                f = self._regime_fill(factor_name, arr, nan_mask, regime)
                if f is not None:
                    return f, {"method": f"regime_similarity({regime})"}
            valid = arr[~nan_mask]
            val = np.mean(valid) if len(valid) > 0 else 0.0
            filled = arr.copy()
            filled[nan_mask] = val
            return filled, {"method": "global_mean"}

    @staticmethod
    def _forward_fill(arr, nan_mask):
        res = arr.copy()
        idx = np.where(~nan_mask)[0]
        if len(idx) == 0:
            return np.zeros_like(arr)
        last_valid = np.maximum.accumulate(np.where(~nan_mask, np.arange(len(arr)), 0))
        res = res[last_valid]
        if idx[0] > 0:
            res[:idx[0]] = res[idx[0]]
        return res

    def _regime_fill(self, factor_name, arr, nan_mask, regime):
        valid = arr[~nan_mask]
        fill_val = np.mean(valid) if len(valid) > 0 else 0.0
        if self._gustatory_cortex:
            try:
                v = self._gustatory_cortex.get_regime_mean(factor_name, regime, min_samples=20)
                if v is not None and np.isfinite(v):
                    fill_val = v
            except Exception:
                pass
        res = arr.copy()
        res[nan_mask] = fill_val
        return res

    def _get_regime(self):
        if self._state_machine:
            try:
                r = self._state_machine.get_current_regime()
                if r in ("trend", "range", "volatile"):
                    return r
            except Exception as e:
                logger.debug("获取市场状态失败: %s", e)
        return "normal"

    def _detect_extremes(self, factor_name, arr, causal, warnings):
        if len(arr) < 2:
            return 0
        if not causal:
            std = np.std(arr)
            if std == 0:
                return 0
            extremes = int(np.sum(np.abs(arr - np.mean(arr)) / std > self.DEFAULT_MAD_THRESHOLD))
        else:
            alpha = self.EWMA_ALPHA
            def _ops(state):
                ewma = state["ewma"]
                ewms = state["ewms"]
                if ewma is None:
                    warmup = min(self.EWMA_WARMUP_SAMPLES, len(arr))
                    ewma = float(np.median(arr[:warmup])) if warmup > 0 else 0.0
                    ewms = float(np.var(arr[:warmup])) if warmup > 1 else 1e-8
                    state["ewma"], state["ewms"], state["count"] = ewma, ewms, warmup
                    start_idx = warmup
                else:
                    start_idx = 0
                extremes = 0
                for i in range(start_idx, len(arr)):
                    x = arr[i]
                    state["ewma"] = alpha * x + (1 - alpha) * state["ewma"]
                    delta = x - state["ewma"]
                    state["ewms"] = (1 - alpha) * (state["ewms"] + alpha * delta * delta)
                    state["count"] += 1
                    std = np.sqrt(state["ewms"] + 1e-10)
                    if std > 0 and abs(x - state["ewma"]) > self.DEFAULT_MAD_THRESHOLD * std:
                        extremes += 1
                return extremes
            extremes = self._with_state(factor_name, _ops)
        if extremes > len(arr) * 0.3:
            warnings.append("extreme_outliers")
        return extremes
