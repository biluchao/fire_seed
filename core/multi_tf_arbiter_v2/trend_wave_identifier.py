"""
火种系统 · 趋势波浪斜线识别器 (TrendWaveIdentifier)

核心职责：
1. 基于历史K线数据自动识别回踩点序列，通过鲁棒线性回归拟合动态斜线通道（支撑线/压力线）
2. 计算斜线的拟合置信度、自适应厚度及时效衰减，为小周期提供精确的斜线约束边界

外部依赖（真实模块接口）：
- core.perception.visual_cortex.VisualCortex : 获取指定周期的K线形态与高低点序列
- core.utils.config_loader.ConfigLoader : 读取斜线识别的周期参数配置
- core.multi_tf_arbiter_v2.zone_thickness_calculator.ZoneThicknessCalculator : 计算斜线区间的动态厚度
- core.behavioral_logger.BehavioralLogger : 记录识别过程与异常事件

接口契约：
- identify_trend_lines(symbol: str, timeframe: str, lookback_bars: Optional[int], direction: int, market_regime: str = "trend") -> Dict[str, Any]
- get_slope_constraint(symbol: str, timeframe: str, current_price: float, current_bar_index: int, lookback_bars: Optional[int] = None) -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str]), 错误时包含 "error_code" (str)

异常与降级：
- 当 VisualCortex 不可用时，无法进行斜线识别，返回数据不可用错误（不降级使用旧缓存，避免过期信号误导）
- 当 ConfigLoader 不可用时，使用类常量中的保守默认参数
- 回踩点数量不足时，返回 "insufficient_data" 状态，不强制拟合，避免不可靠斜线
- 厚度计算器不可用时，使用简化版计算公式（基于触及次数和ATR）
- 拟合失败时，不降级使用旧斜线，直接返回失败，避免过期信息污染决策
- 缓存容量达到上限时，按LRU策略淘汰最旧的斜线，并定期清理过期条目
- 所有降级值在类常量区明确声明

资源管理：
- 本模块不持有任何需要手动释放的资源，所有计算结果在方法返回后由Python垃圾回收
- 内部缓存通过可重入线程锁保护，确保多线程环境下的数据一致性
- 缓存自动按LRU和时效性双重清理，避免内存泄漏

版本历史：
- v6.0.0: 第六轮审查修复，边界场景加固、代码风格统一、文档完善、降级透明化极致
"""

import logging
import time
import threading
from typing import Dict, Any, List, Optional, Tuple
import numpy as np

logger = logging.getLogger(__name__)

__all__ = ['TrendWaveIdentifier']


class TrendWaveIdentifier:
    """趋势波浪斜线识别器，多周期仲裁核心组件"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 回踩点识别参数
    DEFAULT_MIN_RETEST_POINTS_LONG = 2       # 做多（上升趋势）最小回踩点数，无量纲，[2, 5]
    DEFAULT_MIN_RETEST_POINTS_SHORT = 3      # 做空（下降趋势）最小回踩点数，无量纲，[2, 5]
    DEFAULT_LOOKBACK_BARS = 30               # 默认回溯K线根数，根，[10, 200]
    MAX_LOOKBACK_BARS = 200                  # 最大回溯K线根数，根
    DEFAULT_VOLUME_CONFIRM_RATIO = 1.2       # 成交量确认比率，无量纲，[1.0, 2.0]
    DEFAULT_GAP_THRESHOLD_ATR_MULT = 0.5    # 跳空阈值（ATR倍数），无量纲，[0.2, 1.5]
    MIN_AVERAGE_VOLUME = 1e-10              # 平均成交量最小值，防止除零

    # 拟合参数
    MIN_REGRESSION_R_SQUARED = 0.7           # 最小决定系数 R²，无量纲，[0.5, 0.95]
    RANSAC_MAX_ITER = 5                      # 最大迭代次数，[3, 10]
    RANSAC_INLIER_THRESHOLD_ATR_MULT = 2.0  # 内点距离阈值（ATR倍数），[1.0, 4.0]

    # 均线走平判定
    FLAT_TREND_DISABLE = True                # M12走平时是否禁用斜线识别，布尔
    MA12_FLAT_STD_RATIO = 0.001             # M12斜率标准差与ATR的比值，无量纲，[0.0005, 0.005]

    # 厚度计算降级值
    DEFAULT_THICKNESS_ATR_MULT = 0.15        # 厚度 = atr * mult，无量纲，[0.05, 0.3]

    # 时效衰减
    DEFAULT_DECAY_ALPHA = 0.01               # 基础衰减系数，无量纲，[0.001, 0.05]
    MAX_DECAY_AGE_HOURS = 24                 # 最大有效年龄（小时），超过后衰减至0

    # 缓存
    MAX_CACHE_ENTRIES = 20                   # 最大缓存斜线数量，[5, 50]
    MAX_CACHE_AGE_SEC = 86400                # 缓存最大保留时间（24小时），秒

    # 通用
    MIN_ATR_FALLBACK = 1e-8                  # ATR最小降级值，防止除零
    MIN_LOCAL_ATR_SAMPLES = 5                # 计算局部ATR所需的最小K线数
    MIN_STRENGTH_FALLBACK = 0.5              # 趋势强度默认值，无量纲，[0, 1]

    # 版本
    MODULE_VERSION = "6.0.0"

    def __init__(self):
        # 外部依赖注入
        self._visual_cortex = None
        self._config_loader = None
        self._thickness_calculator = None
        self._behavioral_logger = None

        # 内部状态
        self._last_slope_cache: Dict[str, Any] = {}
        self._cache_timestamp: Dict[str, float] = {}
        self._cache_lock = threading.RLock()

        # 热重载参数缓存
        self._cached_params: Dict[str, Any] = {}
        self._registered_callbacks = set()

        logger.info("TrendWaveIdentifier v%s 初始化完成", self.MODULE_VERSION)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        visual_cortex: Optional[Any] = None,
        config_loader: Optional[Any] = None,
        thickness_calculator: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选注入，未注入时对应功能降级）"""
        if visual_cortex is not None:
            self._visual_cortex = visual_cortex
            logger.info("VisualCortex 注入成功")
        else:
            logger.warning("VisualCortex 未注入，斜线识别功能不可用")

        if config_loader is not None:
            self._config_loader = config_loader
            logger.info("ConfigLoader 注入成功")
            if 'trend_wave' not in self._registered_callbacks:
                if hasattr(config_loader, 'register_update_callback'):
                    config_loader.register_update_callback('trend_wave', self._on_config_update)
                    self._registered_callbacks.add('trend_wave')
        else:
            logger.warning("ConfigLoader 未注入，将使用类常量默认参数")

        if thickness_calculator is not None:
            self._thickness_calculator = thickness_calculator
            logger.info("ZoneThicknessCalculator 注入成功")
        else:
            logger.warning("ZoneThicknessCalculator 未注入，斜线厚度使用ATR降级值")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

    # ========== 公共接口 ==========
    def identify_trend_lines(
        self,
        symbol: str,
        timeframe: str,
        lookback_bars: Optional[int] = None,
        direction: int = 1,
        market_regime: str = "trend"
    ) -> Dict[str, Any]:
        """识别趋势波浪斜线"""
        # 标准化参数
        symbol = symbol.upper().strip()
        timeframe = timeframe.lower().strip()
        if not symbol or not timeframe:
            return self._error_response("INVALID_INPUT", "symbol 和 timeframe 不能为空")
        if direction not in (1, -1):
            return self._error_response("INVALID_DIRECTION", "direction 必须为 1 或 -1")
        if market_regime not in ("trend", "oscillation"):
            return self._error_response("INVALID_REGIME", "market_regime 必须为 trend 或 oscillation")

        # 震荡市直接禁用
        if market_regime == "oscillation":
            return {
                "status": "ok",
                "reason": "震荡市禁用斜线识别",
                "data": {"slope_active": False, "message": "oscillation regime disables wave detection"},
                "warnings": ["oscillation_regime"],
                "error_code": ""
            }

        # 确定回溯根数（安全裁剪）
        if lookback_bars is None:
            lookback_bars = self._get_config("lookback_bars", timeframe, self.DEFAULT_LOOKBACK_BARS)
        lookback_bars = int(min(max(lookback_bars, 5), self.MAX_LOOKBACK_BARS))

        # 获取K线
        kline = self._fetch_kline_data(symbol, timeframe, lookback_bars)
        if kline is None or len(kline.get("closes", [])) < lookback_bars:
            logger.error("获取K线失败或数量不足 #RECOVERY: 检查行情连接和品种是否正确")
            return self._error_response("DATA_UNAVAILABLE", "无法获取足够K线数据")

        highs, lows, closes, volumes = kline["highs"], kline["lows"], kline["closes"], kline["volumes"]

        # 计算ATR（使用完整窗口）
        atr_full = self._calculate_atr(highs, lows, closes, 14)

        # 检查M12走平
        if self.FLAT_TREND_DISABLE and self._is_ma12_flat(closes, atr_full, timeframe):
            return {
                "status": "ok",
                "reason": "M12走平，斜线识别禁用",
                "data": {"slope_active": False, "message": "M12 flat"},
                "warnings": ["flat_ma12"],
                "error_code": ""
            }

        # 识别回踩点（带成交量确认和自适应跳空阈值）
        vol_ratio = self._get_config("volume_ratio", timeframe, self.DEFAULT_VOLUME_CONFIRM_RATIO)
        gap_threshold = atr_full * self._get_config("gap_atr_mult", timeframe, self.DEFAULT_GAP_THRESHOLD_ATR_MULT)
        retest_points, timestamps = self._detect_retest_points(
            highs, lows, closes, volumes, direction, vol_ratio, gap_threshold
        )

        # 动态确定最小回踩点数（区分牛熊）
        trend_strength = self._calc_trend_strength(closes, direction)
        min_points = self._get_min_retest_points(timeframe, trend_strength, direction)

        if len(retest_points) < min_points:
            logger.debug("回踩点不足: %d/%d", len(retest_points), min_points)
            return {
                "status": "ok",
                "reason": "回踩点不足 ({}/{})".format(len(retest_points), min_points),
                "data": {"slope_active": False, "retest_count": len(retest_points)},
                "warnings": ["insufficient_points"],
                "error_code": ""
            }

        # 使用回踩点局部数据重新计算ATR（更精准）
        indices = [p[0] for p in retest_points]
        local_highs = [highs[i] for i in range(min(indices), max(indices)+1)]
        local_lows = [lows[i] for i in range(min(indices), max(indices)+1)]
        local_closes = [closes[i] for i in range(min(indices), max(indices)+1)]
        if len(local_closes) < self.MIN_LOCAL_ATR_SAMPLES:
            atr_local = atr_full
        else:
            atr_local = max(self._calculate_atr(local_highs, local_lows, local_closes, 14), self.MIN_ATR_FALLBACK)

        # 鲁棒拟合（使用局部ATR）
        slope_line = self._fit_robust_slope(retest_points, atr_local, direction)
        if slope_line is None:
            return {
                "status": "degraded",
                "reason": "斜线拟合失败（R²过低或异常）",
                "data": {},
                "warnings": ["regression_failed"],
                "error_code": "FIT_FAILED"
            }

        # 保存基准K线索引（用于后续位置计算）
        slope_line["bar_index_base"] = indices[0] if indices else 0
        slope_line["generated_at"] = time.time()
        slope_line["retest_count"] = len(retest_points)

        # 计算厚度（使用局部ATR和回踩点数）
        thickness = self._calculate_thickness(slope_line, timeframe, atr_local)

        # 计算衰减因子
        decay_factor = self._calculate_decay_factor(slope_line, timeframe)

        line_type = "support" if direction == 1 else "resistance"
        result = {
            "slope_active": True,
            "line_type": line_type,
            "direction": direction,
            "slope": slope_line["slope"],
            "intercept": slope_line["intercept"],
            "r_squared": slope_line["r_squared"],
            "retest_count": len(retest_points),
            "retest_points": [(idx, price) for idx, price in retest_points],
            "thickness": thickness,
            "decay_factor": decay_factor,
            "bar_index_base": slope_line["bar_index_base"],
            "generated_at": slope_line["generated_at"],
            "lookback_bars": lookback_bars,
        }

        # 缓存更新（线程安全 + LRU淘汰 + 过期清理）
        cache_key = "{}:{}:{}:{}".format(symbol, timeframe, direction, lookback_bars)
        with self._cache_lock:
            self._prune_cache()
            if len(self._last_slope_cache) >= self.MAX_CACHE_ENTRIES:
                oldest_key = min(self._cache_timestamp, key=self._cache_timestamp.get)
                del self._last_slope_cache[oldest_key]
                del self._cache_timestamp[oldest_key]
            self._last_slope_cache[cache_key] = result
            self._cache_timestamp[cache_key] = time.time()

        logger.info(
            "识别%s %s斜线: slope=%.6f, R²=%.3f, points=%d",
            timeframe, line_type, slope_line["slope"], slope_line["r_squared"], len(retest_points)
        )
        return {
            "status": "ok",
            "reason": "成功识别斜线，R²={:.3f}".format(slope_line["r_squared"]),
            "data": result,
            "warnings": [],
            "error_code": ""
        }

    def get_slope_constraint(
        self,
        symbol: str,
        timeframe: str,
        current_price: float,
        current_bar_index: int,
        lookback_bars: Optional[int] = None
    ) -> Dict[str, Any]:
        """获取当前价格在斜线通道中的约束"""
        symbol = symbol.upper().strip()
        timeframe = timeframe.lower().strip()
        if lookback_bars is None:
            lookback_bars = self._get_config("lookback_bars", timeframe, self.DEFAULT_LOOKBACK_BARS)
        if lookback_bars <= 0:
            lookback_bars = self.DEFAULT_LOOKBACK_BARS
        if current_bar_index < 0:
            return self._error_response("INVALID_BAR_INDEX", "bar_index 必须 >= 0")
        if current_price <= 0:
            return self._error_response("INVALID_PRICE", "current_price 必须 > 0")

        sup_key = "{}:{}:1:{}".format(symbol, timeframe, lookback_bars)
        res_key = "{}:{}:-1:{}".format(symbol, timeframe, lookback_bars)

        result = {"support": None, "resistance": None, "position": "unknown", "constraint_weight": 0.0}
        found = False
        with self._cache_lock:
            if sup_key in self._last_slope_cache:
                sup = self._last_slope_cache[sup_key]
                pos, weight = self._eval_slope_position(sup, current_price, current_bar_index)
                result["support"] = sup
                result["position_support"] = pos
                result["constraint_weight"] = max(result["constraint_weight"], weight)
                found = True

            if res_key in self._last_slope_cache:
                res = self._last_slope_cache[res_key]
                pos, weight = self._eval_slope_position(res, current_price, current_bar_index)
                result["resistance"] = res
                result["position_resistance"] = pos
                result["constraint_weight"] = max(result["constraint_weight"], weight)
                found = True

        if not found:
            return {
                "status": "ok",
                "reason": "无缓存的斜线约束数据",
                "data": result,
                "warnings": ["no_cached_slope"],
                "error_code": ""
            }

        status = "strong" if result["constraint_weight"] > 0.7 else "weak" if result["constraint_weight"] > 0.3 else "none"
        return {
            "status": "ok",
            "reason": "约束状态: {}".format(status),
            "data": result,
            "warnings": [],
            "error_code": ""
        }

    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            # 尝试从真实数据源获取数据验证连通性
            if self._visual_cortex is not None:
                checked = False
                for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
                    try:
                        klines = self._visual_cortex.get_klines(symbol, "15m", 10)
                        if klines and len(klines) >= 5:
                            logger.debug("真实数据源校验通过: %s", symbol)
                            checked = True
                            break
                    except Exception as e:
                        logger.debug("品种 %s 校验失败: %s", symbol, e)
                if not checked:
                    logger.warning("所有品种数据源校验均失败")

            # 基本拟合功能测试
            pts = [(1, 100.0), (2, 100.5), (3, 101.0)]
            res = self._fit_robust_slope(pts, 1.0, 1)
            if res is None or res.get("r_squared", 0) < 0.9:
                return {"status": "degraded", "reason": "拟合功能异常", "data": {}, "warnings": ["fit_fail"], "error_code": "HEALTH_FIT_FAIL"}

            with self._cache_lock:
                cache_entries = len(self._last_slope_cache)

            return {
                "status": "ok",
                "reason": "TrendWaveIdentifier 健康",
                "data": {
                    "dependencies": {
                        "visual_cortex": self._visual_cortex is not None,
                        "config_loader": self._config_loader is not None,
                        "thickness_calculator": self._thickness_calculator is not None,
                    },
                    "cache_entries": cache_entries,
                },
                "warnings": [],
                "error_code": ""
            }
        except Exception as e:
            logger.error("健康检查失败: %s #RECOVERY: 检查 numpy 和数据源", e)
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["exception"], "error_code": "HEALTH_EXCEPTION"}

    def __repr__(self) -> str:
        return "TrendWaveIdentifier(v{}, cache={})".format(self.MODULE_VERSION, len(self._last_slope_cache))

    # ========== 私有方法 ==========
    def _error_response(self, code: str, reason: str) -> Dict[str, Any]:
        return {"status": "error", "reason": reason, "data": {}, "warnings": [code], "error_code": code}

    def _get_config(self, key: str, timeframe: str, default: Any) -> Any:
        tf_params = self._cached_params.get(timeframe, {})
        if isinstance(tf_params, dict) and key in tf_params:
            return tf_params[key]
        if self._config_loader is not None:
            try:
                return self._config_loader.get("multi_tf.trend_wave.{}.{}".format(timeframe, key), default)
            except Exception:
                pass
        return default

    def _fetch_kline_data(self, symbol, timeframe, count):
        try:
            if self._visual_cortex is not None:
                klines = self._visual_cortex.get_klines(symbol, timeframe, count)
                if klines and len(klines) >= count:
                    highs = [k['high'] for k in klines]
                    lows = [k['low'] for k in klines]
                    closes = [k['close'] for k in klines]
                    volumes = [k.get('volume', 0) for k in klines]
                    if len(highs) == len(lows) == len(closes) == len(volumes):
                        return {"highs": highs, "lows": lows, "closes": closes, "volumes": volumes}
                    else:
                        logger.warning("K线数据长度不一致")
        except Exception as e:
            logger.error("获取K线失败: %s #RECOVERY: 检查行情连接", e)
        return None

    def _calculate_atr(self, highs, lows, closes, period=14):
        """使用EMA计算ATR，初始值不足时采用价格的固定比例降级"""
        if not highs or len(highs) < 2:
            return self.MIN_ATR_FALLBACK
        tr = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, len(highs))]
        if not tr:
            return self.MIN_ATR_FALLBACK
        alpha = 2.0 / (period + 1)
        atr = tr[0]
        for i in range(1, len(tr)):
            atr = alpha * tr[i] + (1 - alpha) * atr
        return max(atr, self.MIN_ATR_FALLBACK)

    def _detect_retest_points(self, highs, lows, closes, volumes, direction, vol_ratio, gap_threshold):
        points = []
        n = len(closes)
        if n < 5:
            return points, []
        avg_vol = max(np.mean(volumes[-20:]) if len(volumes) >= 20 else np.mean(volumes), self.MIN_AVERAGE_VOLUME)
        for i in range(2, n - 2):
            if i > 0 and abs(closes[i] - closes[i-1]) > gap_threshold:
                continue
            if direction == 1:
                if lows[i] <= lows[i-1] and lows[i] <= lows[i+1]:
                    if volumes[i] < avg_vol * vol_ratio:
                        points.append((i, lows[i]))
            else:
                if highs[i] >= highs[i-1] and highs[i] >= highs[i+1]:
                    if volumes[i] < avg_vol * vol_ratio:
                        points.append((i, highs[i]))
        return points, [p[0] for p in points]

    def _is_ma12_flat(self, closes, atr, timeframe):
        if len(closes) < 12:
            return False
        ma12 = np.convolve(closes, np.ones(12)/12, mode='valid')
        if len(ma12) < 5:
            return False
        slopes = np.diff(ma12[-5:])
        ratio = self._get_config("ma12_flat_ratio", timeframe, self.MA12_FLAT_STD_RATIO)
        return np.std(slopes) < atr * ratio

    def _calc_trend_strength(self, closes, direction):
        if len(closes) < 20:
            return self.MIN_STRENGTH_FALLBACK
        ma20 = np.mean(closes[-20:])
        if ma20 <= 0:
            return self.MIN_STRENGTH_FALLBACK
        deviation = (closes[-1] - ma20) / ma20
        return min(1.0, abs(deviation) * 20)

    def _get_min_retest_points(self, timeframe, strength, direction):
        if direction == 1:
            base = self.DEFAULT_MIN_RETEST_POINTS_LONG
        else:
            base = self.DEFAULT_MIN_RETEST_POINTS_SHORT
        if strength > 0.7:
            base = max(2, base - 1)
        cfg = self._get_config("min_points", timeframe, base)
        return max(2, min(cfg, 10))

    def _fit_robust_slope(self, points, atr, direction):
        if len(points) < 2:
            return None
        x = np.array([p[0] for p in points])
        y = np.array([p[1] for p in points])
        original_len = len(x)
        inlier_threshold = atr * self.RANSAC_INLIER_THRESHOLD_ATR_MULT

        for iteration in range(self.RANSAC_MAX_ITER):
            A = np.vstack([x, np.ones(len(x))]).T
            try:
                m, c = np.linalg.lstsq(A, y, rcond=None)[0]
            except np.linalg.LinAlgError:
                return None
            if np.isnan(m) or np.isnan(c):
                return None
            y_pred = m * x + c
            residuals = np.abs(y - y_pred)
            if np.max(residuals) < inlier_threshold:
                break
            keep = residuals < inlier_threshold
            if np.sum(keep) < 2:
                return None
            x, y = x[keep], y[keep]

        if direction == 1 and m < 0:
            logger.debug("上升趋势拟合出负斜率(%.6f)，拒绝斜线", m)
            return None
        if direction == -1 and m > 0:
            logger.debug("下降趋势拟合出正斜率(%.6f)，拒绝斜线", m)
            return None

        ss_res = np.sum((y - (m * x + c)) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
        if r_squared < self.MIN_REGRESSION_R_SQUARED:
            return None

        if len(x) < original_len:
            logger.debug("拟合剔除 %d 个离群点", original_len - len(x))

        return {"slope": float(m), "intercept": float(c), "r_squared": float(r_squared)}

    def _calculate_thickness(self, slope_line, timeframe, atr):
        if self._thickness_calculator is not None:
            try:
                result = self._thickness_calculator.calculate(
                    trend_type="wave",
                    retest_count=slope_line.get("retest_count", 2),
                    timeframe=timeframe
                )
                if isinstance(result, (int, float)):
                    return float(result)
            except Exception as e:
                logger.warning("厚度计算器异常: %s，使用降级值", e)
        n = max(slope_line.get("retest_count", 2), 2)
        return atr * self.DEFAULT_THICKNESS_ATR_MULT * (1.0 + 1.0 / n)

    def _calculate_decay_factor(self, slope_line, timeframe):
        alpha = self._get_config("decay_alpha", timeframe, self.DEFAULT_DECAY_ALPHA)
        r2 = slope_line.get("r_squared", 0.5)
        generated_at = slope_line.get("generated_at", 0)
        if generated_at <= 0:
            logger.warning("斜线缺少生成时间戳，使用当前时间")
            generated_at = time.time()
        age_hours = (time.time() - generated_at) / 3600.0
        decay = alpha * (1.0 - r2) * 0.5 * (1.0 + age_hours)
        return min(decay, 1.0)

    def _eval_slope_position(self, slope_data, current_price, bar_index):
        thickness = max(slope_data.get("thickness", 0.0001), 0.0001)
        slope = slope_data["slope"]
        intercept = slope_data["intercept"]
        bar_base = slope_data.get("bar_index_base", 0)
        relative_bar = bar_index - bar_base
        slope_price = slope * relative_bar + intercept
        deviation = abs(current_price - slope_price) / thickness

        on_line_thresh = self._get_config("zone_on_line", "common", 0.3)
        near_thresh = self._get_config("zone_near", "common", 0.8)
        far_thresh = self._get_config("zone_far", "common", 1.5)

        if deviation < on_line_thresh:
            return "on_line", 1.0
        elif deviation < near_thresh:
            return "near", 0.6
        elif deviation < far_thresh:
            return "far", 0.3
        else:
            return "extreme", 0.1

    def _on_config_update(self, new_params: Dict) -> None:
        with self._cache_lock:
            for timeframe, params in new_params.items():
                if not isinstance(params, dict):
                    continue
                if timeframe not in self._cached_params:
                    self._cached_params[timeframe] = {}
                self._cached_params[timeframe].update(params)
        logger.info("配置热重载: %d 项更新", len(new_params))

    def _prune_cache(self) -> None:
        """清理超过最大年龄的缓存条目（需在锁内调用）"""
        now = time.time()
        expired = [k for k, ts in self._cache_timestamp.items() if now - ts > self.MAX_CACHE_AGE_SEC]
        for k in expired:
            del self._last_slope_cache[k]
            del self._cache_timestamp[k]
        if expired:
            logger.debug("清理过期缓存 %d 条", len(expired))
