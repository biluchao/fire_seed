"""
火种系统 · 视觉皮层 (VisualCortex)

核心职责：
1. 基于订单簿快照计算挂单斜率（Theil-Sen 稳健回归，含归一化）、
   挂单集中度（区间占比 + HHI）、挂单墙韧性（价格区间对齐，剔除自身历史订单），
   输出标准化视觉特征
2. 识别单根及双根 K 线形态（优先级：十字星 > 高浪线 > Pin Bar > 吞没 > 普通），
   附带置信度、方向性及趋势上下文修正

外部依赖（真实模块接口）：
- 仅依赖 Python 标准库 (math, logging, statistics, time, threading, operator)
- 无任何第三方或自定义模块依赖

接口契约：
- analyze_orderbook(bids, asks, depth_levels=10, prev_bids=None, prev_asks=None,
                    prev_ts=None, exchange_ts=None, current_ts=None, tick_size=None, *,
                    self_bid_qty=0.0, self_ask_qty=0.0,
                    self_bid_qty_prev=0.0, self_ask_qty_prev=0.0) -> Dict
  输出: data.mid_price, data.weighted_mid_price, data.spread_bps,
        data.bid_slope, data.norm_bid_slope, data.ask_slope, data.norm_ask_slope,
        data.bid_concentration, data.ask_concentration, data.bid_hhi, data.ask_hhi,
        data.wall_resilience, data.analysis_timestamp_utc

- detect_kline_pattern(open_, high, low, close, prev_open=None, prev_close=None,
                       prev_high=None, prev_low=None,
                       trend_direction=0, vol_percentile=50.0,
                       prev_ts=None, current_ts=None) -> Dict
  输出: data.pattern, data.confidence, data.bullish (None for neutral),
        data.body_ratio, data.upper_wick_ratio, data.lower_wick_ratio, data.body_abs

- health_check() -> Dict
- reset_anomaly_counter() -> None

异常与降级：
- 所有浮点运算包含除零保护和范围钳制，确保不产生 NaN 或无穷大
- FALLBACK_HHI = -1.0，调用方必须检查负值
- 自身订单扣除：按档位等比例分配，历史快照与当前快照使用相同扣除策略
- 挂单墙韧性：按价格区间对齐（而非档位），消除价格位移造成的虚假变化率

资源管理：
- 本模块为无状态纯函数集合，所有方法均为 @staticmethod，线程安全
- 模块级计数器使用 threading.Lock 保护
"""

from __future__ import annotations

import logging
import math
import operator
import random
import statistics
import threading
import time
from typing import Any, Dict, Final, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 小型快速对数表（覆盖 1e-12 到 1e8 的常见成交量范围）
_LOG_CACHE: Dict[float, float] = {}


def _fast_log(v: float) -> float:
    """缓存的对数计算，避免高频调用 math.log"""
    key = round(v, 10)
    if key not in _LOG_CACHE:
        _LOG_CACHE[key] = math.log(v) if v > 0 else 0.0
    if len(_LOG_CACHE) > 10000:
        _LOG_CACHE.clear()
    return _LOG_CACHE[key]


class VisualCortex:
    """视觉皮层：订单簿形状分析、K线形态识别（线程安全）"""

    # ========== 类常量 ==========
    DEFAULT_DEPTH_LEVELS: Final[int] = 10
    MIN_DEPTH_FULL: Final[int] = 3
    MIN_DEPTH_PARTIAL: Final[int] = 5
    CONCENTRATION_PRICE_RANGE: Final[float] = 0.001
    MIN_TOTAL_VOLUME_FOR_SLOPE: Final[float] = 1e-8  # 总成交量低于此值时不计算斜率
    MAX_NORM_SLOPE: Final[float] = 100.0
    MIN_TOTAL_RANGE_FOR_PATTERN: Final[float] = 1e-8
    TICK_SIZE_DEFAULT: Final[float] = 0.01  # 默认最小变动单位

    WALL_MIN_SNAPSHOTS: Final[int] = 2
    WALL_WEIGHTS: Final[Tuple[float, ...]] = (0.6, 0.3, 0.1)
    PAPER_WALL_RATIO: Final[float] = 0.4
    WALL_SNAPSHOT_MAX_AGE_SEC: Final[float] = 5.0
    WALL_TIME_DECAY_FACTOR: Final[float] = 1.0 / WALL_SNAPSHOT_MAX_AGE_SEC
    WALL_PRICE_RANGE_RATIO: Final[float] = 0.001  # 墙韧性价格对齐区间（最佳价 ±0.1%）

    PINBAR_BODY_MAX: Final[float] = 0.3
    PINBAR_WICK_MIN: Final[float] = 0.6
    PINBAR_HIGH_VOL_MIN: Final[float] = 0.7
    ENGULFING_RATIO_MIN: Final[float] = 0.9
    ENGULFING_COUNTER_TREND_BOOST: Final[float] = 1.2
    ENGULFING_GAP_MAX: Final[float] = 0.005  # 吞没形态允许的最大跳空比例
    DOJI_BODY_MAX: Final[float] = 0.1
    HIGH_WAVE_BODY_MAX: Final[float] = 0.4  # 高浪线必须是小实体
    KLINE_MAX_INTERVAL_SEC: Final[float] = 3600.0
    ANOMALY_ALERT_THRESHOLD: Final[float] = 0.05

    FALLBACK_SLOPE: Final[float] = 0.0
    FALLBACK_CONCENTRATION: Final[float] = 0.5
    FALLBACK_PATTERN: Final[str] = "unknown"
    FALLBACK_HHI: Final[float] = -1.0  # 负值表示不可用，调用方必须检查

    WALL_THICK: Final[str] = "thick_wall"
    WALL_THIN: Final[str] = "thin_wall"
    WALL_MODERATE: Final[str] = "moderate"
    WALL_UNKNOWN: Final[str] = "unknown"
    WALL_ERROR: Final[str] = "error"

    # 模块级异常计数器（线程安全，进程级别）
    _anomaly_count: int = 0
    _total_count: int = 0
    _counter_lock: threading.Lock = threading.Lock()

    # ========== 公共接口 ==========
    @classmethod
    def analyze_orderbook(
        cls,
        bids: List[List[float]],
        asks: List[List[float]],
        depth_levels: int = DEFAULT_DEPTH_LEVELS,
        prev_bids: Optional[List[List[float]]] = None,
        prev_asks: Optional[List[List[float]]] = None,
        prev_ts: Optional[float] = None,
        exchange_ts: Optional[float] = None,
        current_ts: Optional[float] = None,
        tick_size: Optional[float] = None,
        *,
        self_bid_qty: float = 0.0,
        self_ask_qty: float = 0.0,
        self_bid_qty_prev: float = 0.0,
        self_ask_qty_prev: float = 0.0,
    ) -> Dict[str, Any]:
        """分析订单簿形态。"""
        if depth_levels <= 0:
            return cls._degraded("Invalid depth_levels <= 0")
        if not bids or not asks:
            cls._record_anomaly()
            return cls._degraded("订单簿数据为空")

        bids = cls._ensure_bid_order(bids)
        asks = cls._ensure_ask_order(asks)
        if not bids or not asks:
            return cls._degraded("订单簿数据为空（排序后）")

        effective_depth = min(depth_levels, len(bids), len(asks))
        if effective_depth < cls.MIN_DEPTH_FULL:
            cls._record_anomaly()
            return cls._degraded(f"档位严重不足 ({effective_depth})")
        partial_mode = effective_depth < cls.MIN_DEPTH_PARTIAL

        try:
            bid_prices, bid_volumes = cls._extract_pv(bids, effective_depth, self_bid_qty)
            ask_prices, ask_volumes = cls._extract_pv(asks, effective_depth, self_ask_qty)

            if not bid_prices or not ask_prices:
                return cls._degraded("自身订单扣除后挂单为空")

            best_bid, best_ask = bids[0][0], asks[0][0]
            if best_bid >= best_ask:
                logger.error("Crossed market detected")
                return cls._degraded("Crossed market (bid >= ask)")

            mid_price = (best_bid + best_ask) / 2.0
            if mid_price <= 0:
                return cls._degraded("Invalid mid_price <= 0")

            # 加权中价
            bid_top_vol = max(bids[0][1], 1e-12)
            ask_top_vol = max(asks[0][1], 1e-12)
            weighted_mid = (best_bid * ask_top_vol + best_ask * bid_top_vol) / (bid_top_vol + ask_top_vol)

            spread_bps = (best_ask - best_bid) / best_bid * 10000

            # 斜率计算（仅在总成交量足够时进行）
            total_bid_vol = sum(bid_volumes) if bid_volumes else 0.0
            total_ask_vol = sum(ask_volumes) if ask_volumes else 0.0

            bid_slope = cls._theil_sen_slope(bid_prices, bid_volumes, total_bid_vol)
            ask_slope = cls._theil_sen_slope(ask_prices, ask_volumes, total_ask_vol)

            norm_bid_slope = cls._normalize_slope(bid_slope, mid_price, bid_volumes)
            norm_ask_slope = cls._normalize_slope(ask_slope, mid_price, ask_volumes)

            # 集中度
            effective_tick = tick_size or cls.TICK_SIZE_DEFAULT
            price_range = max(mid_price * cls.CONCENTRATION_PRICE_RANGE, effective_tick * 10)
            bid_conc = cls._concentration(bids, mid_price - price_range, mid_price, effective_depth)
            ask_conc = cls._concentration(asks, mid_price, mid_price + price_range, effective_depth)
            bid_hhi = cls._compute_hhi(bids, effective_depth)
            ask_hhi = cls._compute_hhi(asks, effective_depth)

            # 挂单墙韧性
            wall = cls._wall_resilience(
                bids, asks, prev_bids, prev_asks, prev_ts, current_ts,
                best_bid, best_ask, self_bid_qty, self_ask_qty,
                self_bid_qty_prev, self_ask_qty_prev
            )

            data = {
                "mid_price": round(mid_price, 2),
                "weighted_mid_price": round(weighted_mid, 2),
                "spread_bps": round(max(spread_bps, 0.0), 2),
                "bid_slope": round(bid_slope, 8),
                "norm_bid_slope": round(norm_bid_slope, 8),
                "ask_slope": round(ask_slope, 8),
                "norm_ask_slope": round(norm_ask_slope, 8),
                "bid_concentration": round(bid_conc, 4),
                "ask_concentration": round(ask_conc, 4),
                "bid_hhi": round(bid_hhi, 4),
                "ask_hhi": round(ask_hhi, 4),
                "wall_resilience": wall,
                "analysis_timestamp_utc": exchange_ts if exchange_ts is not None else time.time(),
            }

            warnings = []
            if partial_mode:
                data["bid_concentration"] = cls.FALLBACK_CONCENTRATION
                data["ask_concentration"] = cls.FALLBACK_CONCENTRATION
                data["bid_hhi"] = cls.FALLBACK_HHI
                data["ask_hhi"] = cls.FALLBACK_HHI
                warnings.append({"code": "partial_depth", "message": "Concentration/HHI disabled due to low depth"})

            return {"status": "ok", "reason": f"Orderbook analysis done. spread={spread_bps:.1f}bps", "data": data, "warnings": warnings}

        except Exception as e:
            cls._record_anomaly()
            logger.error(f"Orderbook analysis failed: {e} #RECOVERY: check input format")
            return cls._degraded(f"Analysis exception: {e}")

    @classmethod
    def detect_kline_pattern(
        cls,
        open_: float, high: float, low: float, close: float,
        prev_open: Optional[float] = None,
        prev_close: Optional[float] = None,
        prev_high: Optional[float] = None,
        prev_low: Optional[float] = None,
        trend_direction: int = 0,
        vol_percentile: float = 50.0,
        prev_ts: Optional[float] = None,
        current_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """识别 K 线形态。形态优先级：十字星 > 高浪线 > Pin Bar > 吞没 > 普通"""
        if high < low or open_ <= 0 or close <= 0:
            return {"status": "error", "reason": "Invalid K-line data",
                    "data": {"pattern": cls.FALLBACK_PATTERN, "confidence": 0.0, "bullish": None,
                             "body_ratio": 0.0, "upper_wick_ratio": 0.0, "lower_wick_ratio": 0.0, "body_abs": 0.0},
                    "warnings": [{"code": "invalid_data", "message": "OHLC values invalid"}]}

        if trend_direction not in (-1, 0, 1):
            trend_direction = 0
        if vol_percentile <= 1.0:
            vol_percentile *= 100

        try:
            total_range = high - low
            if total_range < cls.MIN_TOTAL_RANGE_FOR_PATTERN:
                return {"status": "ok", "reason": "Flat line (no trading)",
                        "data": {"pattern": "flat_line", "confidence": 1.0, "bullish": None,
                                 "body_ratio": 0.0, "upper_wick_ratio": 0.0, "lower_wick_ratio": 0.0, "body_abs": 0.0}, "warnings": []}

            body = abs(close - open_)
            upper_wick = high - max(open_, close)
            lower_wick = min(open_, close) - low

            body_ratio = min(max(body / total_range, 0.0), 1.0)
            upper_wick_ratio = upper_wick / total_range
            lower_wick_ratio = lower_wick / total_range

            pinbar_threshold = cls.PINBAR_HIGH_VOL_MIN if vol_percentile > 70 else cls.PINBAR_WICK_MIN

            pattern = cls.FALLBACK_PATTERN
            confidence = 0.0
            bullish: Optional[bool] = None

            # 1. 十字星
            if body_ratio < cls.DOJI_BODY_MAX and upper_wick_ratio > 0.2 and lower_wick_ratio > 0.2:
                pattern, confidence, bullish = "doji", 0.7, None
            # 2. 高浪线（小实体 + 长上下影）
            elif body_ratio < cls.HIGH_WAVE_BODY_MAX and upper_wick_ratio > 0.5 and lower_wick_ratio > 0.5:
                pattern, confidence, bullish = "high_wave", 0.5, None
            # 3. Pin Bar
            elif lower_wick_ratio > pinbar_threshold and body_ratio < cls.PINBAR_BODY_MAX:
                pattern, confidence, bullish = "bullish_pinbar", min(1.0, lower_wick_ratio * 1.2), True
                if trend_direction == -1:
                    confidence *= 0.5; pattern = "bullish_pinbar_counter_trend"
            elif upper_wick_ratio > pinbar_threshold and body_ratio < cls.PINBAR_BODY_MAX:
                pattern, confidence, bullish = "bearish_pinbar", min(1.0, upper_wick_ratio * 1.2), False
                if trend_direction == 1:
                    confidence *= 0.5; pattern = "bearish_pinbar_counter_trend"

            # 4. 吞没形态
            if (prev_open is not None and prev_close is not None and
                cls._kline_continuous(prev_ts, current_ts)):
                gap_ratio = abs(open_ - prev_close) / prev_close if prev_close > 0 else 0.0
                if gap_ratio < cls.ENGULFING_GAP_MAX:  # 无大幅跳空
                    prev_body = abs(prev_close - prev_open)
                    if prev_body > 0:
                        if (prev_close < prev_open and close > open_ and
                            open_ <= prev_close and close >= prev_open and
                            body > prev_body * cls.ENGULFING_RATIO_MIN):
                            if prev_high is not None and prev_low is not None:
                                if high >= prev_high and low <= prev_low:
                                    pattern, confidence, bullish = "bullish_engulfing", 0.9, True
                            else:
                                pattern, confidence, bullish = "bullish_engulfing", 0.85, True
                        elif (prev_close > prev_open and close < open_ and
                              open_ >= prev_close and close <= prev_open and
                              body > prev_body * cls.ENGULFING_RATIO_MIN):
                            if prev_high is not None and prev_low is not None:
                                if high >= prev_high and low <= prev_low:
                                    pattern, confidence, bullish = "bearish_engulfing", 0.9, False
                            else:
                                pattern, confidence, bullish = "bearish_engulfing", 0.85, False

            if pattern in ("bullish_engulfing", "bearish_engulfing"):
                if (pattern == "bullish_engulfing" and trend_direction == -1) or \
                   (pattern == "bearish_engulfing" and trend_direction == 1):
                    confidence = min(1.0, confidence * cls.ENGULFING_COUNTER_TREND_BOOST)

            if pattern == cls.FALLBACK_PATTERN:
                pattern, confidence, bullish = "normal", 0.1, close > open_

            data = {"pattern": pattern, "confidence": round(confidence, 4), "bullish": bullish,
                    "body_ratio": round(body_ratio, 4), "upper_wick_ratio": round(upper_wick_ratio, 4),
                    "lower_wick_ratio": round(lower_wick_ratio, 4), "body_abs": round(body, 8)}

            return {"status": "ok", "reason": f"Pattern: {pattern} (conf={confidence:.2f})", "data": data, "warnings": []}

        except Exception as e:
            logger.error(f"Pattern detection failed: {e} #RECOVERY: check OHLC values")
            return {"status": "error", "reason": f"Detection exception: {e}",
                    "data": {"pattern": cls.FALLBACK_PATTERN, "confidence": 0.0, "bullish": None,
                             "body_ratio": 0.0, "upper_wick_ratio": 0.0, "lower_wick_ratio": 0.0, "body_abs": 0.0},
                    "warnings": [{"code": "exception", "message": str(e)}]}

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检"""
        try:
            bids = [[50000.0, 1.5], [49990.0, 2.0], [49980.0, 3.0]]
            asks = [[50010.0, 1.2], [50020.0, 2.5], [50030.0, 1.8]]
            res = cls.analyze_orderbook(bids, asks, depth_levels=3)
            assert res["status"] == "ok"
            assert 0.0 <= res["data"]["ask_concentration"] <= 1.0
            res_self = cls.analyze_orderbook(bids, asks, depth_levels=3, self_bid_qty=0.5)
            assert res_self["status"] == "ok"
            res_edge = cls.analyze_orderbook(bids, asks, depth_levels=1)
            assert res_edge["status"] == "degraded"
            pat = cls.detect_kline_pattern(100.0, 105.0, 95.0, 104.0)
            assert pat["status"] == "ok"
            flat = cls.detect_kline_pattern(100.0, 100.0, 100.0, 100.0)
            assert flat["data"]["pattern"] == "flat_line"
            return {"status": "ok", "reason": "VisualCortex health check passed", "data": {"tests": "all_passed"}, "warnings": []}
        except AssertionError as e:
            logger.error(f"Health check assertion failed: {e}")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": [{"code": "assertion", "message": str(e)}]}
        except Exception as e:
            logger.error(f"Health check exception: {e}")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": [{"code": "exception", "message": str(e)}]}

    @classmethod
    def reset_anomaly_counter(cls) -> None:
        with cls._counter_lock:
            cls._total_count = 0
            cls._anomaly_count = 0

    # ========== 私有方法 ==========
    @staticmethod
    def _degraded(reason: str) -> Dict[str, Any]:
        return {"status": "degraded", "reason": reason, "data": {
            "mid_price": 0.0, "weighted_mid_price": 0.0, "spread_bps": 0.0,
            "bid_slope": VisualCortex.FALLBACK_SLOPE, "norm_bid_slope": VisualCortex.FALLBACK_SLOPE,
            "ask_slope": VisualCortex.FALLBACK_SLOPE, "norm_ask_slope": VisualCortex.FALLBACK_SLOPE,
            "bid_concentration": VisualCortex.FALLBACK_CONCENTRATION, "ask_concentration": VisualCortex.FALLBACK_CONCENTRATION,
            "bid_hhi": VisualCortex.FALLBACK_HHI, "ask_hhi": VisualCortex.FALLBACK_HHI,
            "wall_resilience": {"type": VisualCortex.WALL_UNKNOWN, "confidence": 0.0,
                                "bid_change_ratio": 1.0, "ask_change_ratio": 1.0, "reason": "degraded"},
            "analysis_timestamp_utc": time.time()}, "warnings": [{"code": "degraded", "message": reason}]}

    @classmethod
    def _record_anomaly(cls) -> None:
        with cls._counter_lock:
            cls._total_count += 1
            cls._anomaly_count += 1
            if cls._total_count > 100 and cls._anomaly_count / cls._total_count > cls.ANOMALY_ALERT_THRESHOLD:
                logger.warning("Orderbook anomaly rate exceeds %.0f%%", cls.ANOMALY_ALERT_THRESHOLD * 100)

    @staticmethod
    def _ensure_bid_order(bids: List[List[float]]) -> List[List[float]]:
        if not bids or len(bids) < 2:
            return bids
        if bids[0][0] < bids[1][0]:
            return sorted(bids, key=operator.itemgetter(0), reverse=True)
        return bids

    @staticmethod
    def _ensure_ask_order(asks: List[List[float]]) -> List[List[float]]:
        if not asks or len(asks) < 2:
            return asks
        if asks[0][0] > asks[1][0]:
            return sorted(asks, key=operator.itemgetter(0))
        return asks

    @staticmethod
    def _extract_pv(entries: List[List[float]], depth: int, self_qty: float) -> Tuple[List[float], List[float]]:
        prices, volumes = [], []
        for i in range(min(depth, len(entries))):
            try:
                p = float(entries[i][0])
                v = max(0.0, float(entries[i][1]))
                prices.append(p)
                volumes.append(v)
            except (IndexError, ValueError):
                continue

        n = len(volumes)
        if self_qty > 0 and n > 0:
            per_level = self_qty / n
            remaining = self_qty
            for i in range(n):
                deduct = min(volumes[i], per_level if remaining >= per_level else remaining)
                volumes[i] -= deduct
                remaining -= deduct
            filtered = [(p, v) for p, v in zip(prices, volumes) if v > 1e-12]
            if not filtered:
                logger.warning("All volumes depleted after self-order deduction")
                return ([prices[0]], [1e-12])  # 保留至少1档伪计数
            prices, volumes = [list(x) for x in zip(*filtered)]

        return prices, volumes

    @classmethod
    def _theil_sen_slope(cls, prices: List[float], volumes: List[float], total_vol: float = None) -> float:
        if total_vol is None:
            total_vol = sum(volumes)
        if total_vol < cls.MIN_TOTAL_VOLUME_FOR_SLOPE:
            return cls.FALLBACK_SLOPE

        n = min(len(prices), 20)
        if n < 2:
            return cls.FALLBACK_SLOPE
        prices, volumes = prices[:n], volumes[:n]

        log_vols = [max(-20.0, min(20.0, _fast_log(v))) if v > 0 else 0.0 for v in volumes]

        # 当样本超过 10 时，随机采样 10 个点进行 Theil-Sen，降低 O(N²) 开销
        if n > 10:
            indices = random.sample(range(n), 10)
            sample_prices = [prices[i] for i in indices]
            sample_logs = [log_vols[i] for i in indices]
            return cls._theil_sen_core(sample_prices, sample_logs)

        return cls._theil_sen_core(prices, log_vols)

    @staticmethod
    def _theil_sen_core(prices: List[float], log_vols: List[float]) -> float:
        n = len(prices)
        if n < 2:
            return VisualCortex.FALLBACK_SLOPE
        slopes = []
        for i in range(n):
            for j in range(i + 1, n):
                if prices[j] != prices[i]:
                    s = (log_vols[j] - log_vols[i]) / (prices[j] - prices[i])
                    if math.isfinite(s):
                        slopes.append(s)
        if not slopes:
            logger.debug("Theil-Sen slopes empty, falling back to least squares")
            return VisualCortex._least_squares_slope(prices, log_vols)
        return statistics.median(slopes)

    @staticmethod
    def _least_squares_slope(prices: List[float], log_vols: List[float]) -> float:
        n = len(prices)
        if n < 2:
            return VisualCortex.FALLBACK_SLOPE
        # 中心化 prices 防止浮点溢出
        min_p = min(prices)
        centered = [p - min_p for p in prices]
        mean_x = sum(centered) / n
        mean_y = sum(log_vols) / n
        num = sum((centered[i] - mean_x) * (log_vols[i] - mean_y) for i in range(n))
        den = sum((centered[i] - mean_x) ** 2 for i in range(n))
        if den == 0:
            logger.debug("Least squares denominator zero")
            return VisualCortex.FALLBACK_SLOPE
        return num / den

    @classmethod
    def _normalize_slope(cls, slope: float, mid_price: float, volumes: List[float]) -> float:
        if not volumes:
            return cls.FALLBACK_SLOPE
        avg_vol = sum(volumes) / len(volumes)
        if avg_vol < 1e-12:
            return cls.FALLBACK_SLOPE
        norm = slope * mid_price / avg_vol
        return max(-cls.MAX_NORM_SLOPE, min(cls.MAX_NORM_SLOPE, norm))

    @staticmethod
    def _concentration(orders: List[List[float]], lower: float, upper: float, depth: int) -> float:
        total, inner = 0.0, 0.0
        for i in range(min(depth, len(orders))):
            try:
                p = float(orders[i][0])
                v = float(orders[i][1])
            except (ValueError, TypeError, IndexError):
                continue
            total += v
            if lower <= p <= upper:
                inner += v
        return inner / total if total > 0 else VisualCortex.FALLBACK_CONCENTRATION

    @staticmethod
    def _compute_hhi(orders: List[List[float]], depth: int) -> float:
        total, squares = 0.0, 0.0
        actual_depth = min(depth, len(orders))
        if actual_depth < 5:
            return VisualCortex.FALLBACK_HHI  # 档位不足，HHI 不可靠
        for i in range(actual_depth):
            try:
                v = float(orders[i][1])
            except (ValueError, TypeError, IndexError):
                continue
            total += v
            squares += v * v
        return squares / (total * total) if total > 0 else VisualCortex.FALLBACK_HHI

    @classmethod
    def _wall_resilience(
        cls, bids, asks, prev_bids, prev_asks, prev_ts, current_ts,
        best_bid, best_ask, self_bid, self_ask, self_bid_prev, self_ask_prev,
    ) -> Dict[str, Any]:
        if prev_bids is None or prev_asks is None:
            return {"type": cls.WALL_UNKNOWN, "confidence": 0.0,
                    "bid_change_ratio": 1.0, "ask_change_ratio": 1.0, "reason": "No historical snapshot"}

        time_decay = 1.0
        if prev_ts is not None and prev_ts > 0 and current_ts is not None and current_ts > prev_ts:
            elapsed = current_ts - prev_ts
            if elapsed > 10 * cls.WALL_SNAPSHOT_MAX_AGE_SEC:
                return {"type": cls.WALL_UNKNOWN, "confidence": 0.0,
                        "bid_change_ratio": 1.0, "ask_change_ratio": 1.0, "reason": f"Snapshot too old ({elapsed:.0f}s)"}
            time_decay = 1.0 / (1.0 + elapsed * cls.WALL_TIME_DECAY_FACTOR)

        try:
            price_range = best_bid * cls.WALL_PRICE_RANGE_RATIO

            def vol_in_range(entries, ref_price, self_q):
                total = 0.0
                remaining = self_q
                for i in range(min(10, len(entries))):
                    p, v = float(entries[i][0]), float(entries[i][1])
                    if abs(p - ref_price) <= price_range:
                        if remaining > 0:
                            deduct = min(v, remaining / max(1, sum(1 for _ in entries if abs(float(_[0]) - ref_price) <= price_range)))
                            v -= deduct
                            remaining -= deduct
                        total += max(0.0, v)
                return total

            curr_bid_w = vol_in_range(bids, best_bid, self_bid)
            curr_ask_w = vol_in_range(asks, best_ask, self_ask)
            prev_bid_w = vol_in_range(prev_bids, best_bid, self_bid_prev)
            prev_ask_w = vol_in_range(prev_asks, best_ask, self_ask_prev)

            bid_ratio = curr_bid_w / prev_bid_w if prev_bid_w > 0 else 1.0
            ask_ratio = curr_ask_w / prev_ask_w if prev_ask_w > 0 else 1.0

            if bid_ratio < cls.PAPER_WALL_RATIO or ask_ratio < cls.PAPER_WALL_RATIO:
                conf = max(0.0, min(1.0, (1.0 - min(bid_ratio, ask_ratio)) * time_decay))
                return {"type": cls.WALL_THIN, "confidence": round(conf, 4),
                        "bid_change_ratio": round(bid_ratio, 6), "ask_change_ratio": round(ask_ratio, 6),
                        "reason": "Volume decaying rapidly, possible paper wall"}
            elif bid_ratio > 1.2 or ask_ratio > 1.2:
                conf = max(0.0, min(1.0, (min(bid_ratio, ask_ratio) - 0.2) * time_decay))
                return {"type": cls.WALL_THICK, "confidence": round(conf, 4),
                        "bid_change_ratio": round(bid_ratio, 6), "ask_change_ratio": round(ask_ratio, 6),
                        "reason": "Volume increasing, genuine wall strengthening"}
            return {"type": cls.WALL_MODERATE, "confidence": round(0.6 * time_decay, 4),
                    "bid_change_ratio": round(bid_ratio, 6), "ask_change_ratio": round(ask_ratio, 6),
                    "reason": "Volume relatively stable"}
        except Exception as e:
            return {"type": cls.WALL_ERROR, "confidence": 0.0,
                    "bid_change_ratio": 1.0, "ask_change_ratio": 1.0, "reason": str(e)}

    @staticmethod
    def _kline_continuous(prev_ts: Optional[float], current_ts: Optional[float]) -> bool:
        if prev_ts is None or current_ts is None:
            return True
        if current_ts < prev_ts:
            return False
        return (current_ts - prev_ts) < VisualCortex.KLINE_MAX_INTERVAL_SEC
