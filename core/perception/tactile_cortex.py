"""
火种系统 · 触觉皮层 (TactileCortex) v2.3.0

核心职责：
1. 感知市场流动性纹理：基于订单簿深度分布、价差宽度、动态基准与品种因子，输出流动性评级与深度衰减速率
2. 感知成交脉搏：分析逐笔成交的时间间隔变异系数，区分算法做市商主导与散户/手动交易状态
3. 感知波动率结构：计算短期与长期波动率比值，输出波动率期限结构的预警信号

外部依赖（真实模块接口）：
- 无：本模块为纯计算层，所有市场数据通过方法参数传入，不直接依赖外部模块
- 若上游传入数据格式不符，将记录警告并返回安全的保守默认值

接口契约：
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])
- "status" 仅允许 "ok", "degraded", "error" 三种值
- "warnings" 中的每一条均为带固定前缀的结构化告警码（如 "LIQ_DEGRADED"）
- 降级返回的 data 字段与正常返回完全一致，确保下游模块无感切换
- 每个 data 字段内包含 "sensor_version" 键，用于多版本数据溯源

异常与降级：
- 输入数据缺失或无效时，返回保守默认值并标记 "degraded" 状态
- 数值异常（NaN/Inf/None）被自动过滤并替换为安全默认值，同时记录 WARNING 日志
- 所有降级默认值在类常量区明确声明
- 降级方法返回的 reason 字段不包含任何内部路径或堆栈信息

资源管理：
- 本模块不持有任何外部资源句柄，所有方法为纯计算、无状态、线程安全
- 使用 __slots__ 减少实例内存开销
- 所有公共方法均为静态方法，可无需实例直接调用
"""

import functools
import logging
import math
import os
import time
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["TactileCortex"]


class TactileCortex:
    """触觉皮层：感知流动性、成交脉搏与波动率结构"""

    __slots__ = ()
    __version__ = "2.3.0"

    # ========== 类常量 ==========
    # 流动性评级
    LIQUIDITY_L1_MAX_SPREAD = 0.0001
    LIQUIDITY_L2_MAX_SPREAD = 0.0005
    LIQUIDITY_L3_MAX_SPREAD = 0.002
    LIQUIDITY_L4_MAX_SPREAD = 0.005
    MIN_DEPTH_FOR_L1 = 100.0
    MIN_DEPTH_FOR_L2 = 10.0
    MIN_DEPTH_FOR_L3 = 0.5
    MIN_DEPTH_FOR_L4 = 0.01   # L4 也需要最低深度，避免无深度盘口被评为 L4

    # 成交脉搏
    PULSE_CV_ALGO_THRESHOLD = 0.3
    PULSE_CV_MANUAL_THRESHOLD = 0.7
    PULSE_LOW_SAMPLE_WARNING = 5
    PULSE_CV_MAX = 10.0

    # 波动率期限结构
    VOL_TERM_HEAT_THRESHOLD = 1.5
    VOL_TERM_COLD_THRESHOLD = 0.6
    VOL_RATIO_MAX = 20.0

    # 降级默认值
    DEFAULT_LIQUIDITY_LEVEL = "L3"
    DEFAULT_RELATIVE_SPREAD = 0.001
    DEFAULT_TOTAL_DEPTH = 0.0
    DEFAULT_DEPTH_DECAY_BPS = 0.0
    DEFAULT_MID_PRICE = 0.0
    DEFAULT_PULSE_CV = 0.5
    DEFAULT_PULSE_REGIME = "balanced"
    DEFAULT_MEAN_INTERVAL_MS = 500.0
    DEFAULT_TRADE_COUNT = 0
    DEFAULT_VOL_RATIO = 1.0
    DEFAULT_VOL_STATUS = "normal"

    # 数值安全
    MAX_VALID_VALUE = 1e12
    MIN_VALID_VALUE = -1e12
    MAX_DEPTH_RATIO = 100.0

    DEPTH_DECAY_WARNING_BPS = 5000
    ZERO_INTERVAL_WARNING_PCT = 10.0
    REASON_MAX_LENGTH = 200

    # 性能埋点（延迟绑定，避免装饰器内反复读取环境变量）
    _perf_monitor_enabled = os.getenv("FIRE_SEED_PERF_MONITOR", "0") == "1"
    _perf_threshold_us = 1000

    def __init__(self):
        logger.info("TactileCortex v%s 初始化完成", self.__version__)

    def __repr__(self):
        return f"TactileCortex(v{self.__version__})"

    @staticmethod
    def inject_dependencies(**kwargs) -> None:
        pass

    # ========== 私有工具方法 ==========
    @staticmethod
    def _sanitize_numeric(value: Any, default: float = 0.0, context: str = "") -> float:
        """净化数值：将 None/NaN/Inf 替换为安全默认值"""
        if value is None:
            if context:
                logger.warning("NUMERIC_NULL (%s)", context)
            return default
        try:
            fval = float(value)
            if math.isnan(fval) or math.isinf(fval):
                if context:
                    logger.warning("NUMERIC_ANOMALY (%s): %s", context, value)
                return default
            return fval
        except (TypeError, ValueError, OverflowError):
            if context:
                logger.warning("NUMERIC_CONVERT_FAIL (%s): %s", context, value)
            return default

    @staticmethod
    def _validate_orderbook_level(level: Any) -> bool:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            return False
        price, qty = level[0], level[1]
        return isinstance(price, (int, float)) and isinstance(qty, (int, float))

    @staticmethod
    def _welford_variance(values: List[float]) -> Tuple[float, float]:
        """Welford 在线算法，返回 (均值, 样本方差)"""
        n = 0
        mean = 0.0
        M2 = 0.0
        for x in values:
            n += 1
            delta = x - mean
            mean += delta / n
            delta2 = x - mean
            M2 += delta * delta2
        if n < 2:
            return mean, 0.0
        variance = M2 / (n - 1)
        return mean, max(0.0, variance)

    @staticmethod
    def _clean_reason(raw: str, max_len: int = 200) -> str:
        # 保留异常类型，移除文件路径和堆栈
        first_line = raw.split('\n')[0].strip()
        # 若第一行包含文件路径（如 File "xxx", line），则提取异常类型
        if first_line.startswith("File ") or "Error" not in first_line:
            # 尝试找到异常类型行
            for line in raw.split('\n'):
                if 'Error' in line or 'Exception' in line:
                    first_line = line.strip()
                    break
        if len(first_line) > max_len:
            first_line = first_line[:max_len - 3] + "..."
        return first_line

    @classmethod
    def _perf_monitor(cls, func):
        """性能埋点装饰器，仅在类变量开启且耗时超阈值时记录"""
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if not cls._perf_monitor_enabled:
                return func(*args, **kwargs)
            start = time.perf_counter()
            result = func(*args, **kwargs)
            elapsed_us = (time.perf_counter() - start) * 1_000_000
            if elapsed_us > cls._perf_threshold_us:
                logger.debug("%s 执行耗时: %.1f μs", func.__name__, elapsed_us)
            return result
        return wrapper

    # ========== 公共接口（全部静态方法） ==========
    @classmethod
    def sense_liquidity(
        cls, orderbook_snapshot: Dict[str, Any], historical_depth_avg: Optional[float] = None
    ) -> Dict[str, Any]:
        warnings = []
        try:
            bids = orderbook_snapshot.get("bids", [])
            asks = orderbook_snapshot.get("asks", [])
            if not isinstance(bids, list) or not isinstance(asks, list):
                return cls._degraded_liquidity("订单簿数据格式错误")

            bids = [b for b in bids if cls._validate_orderbook_level(b)]
            asks = [a for a in asks if cls._validate_orderbook_level(a)]
            if not bids or not asks:
                return cls._degraded_liquidity("订单簿无有效档位")

            best_bid = cls._sanitize_numeric(bids[0][0], context="best_bid")
            best_ask = cls._sanitize_numeric(asks[0][0], context="best_ask")
            if best_bid <= 0 or best_ask <= 0:
                return cls._degraded_liquidity("订单簿价格非正")
            if best_ask <= best_bid:
                return cls._degraded_liquidity("订单簿交叉")

            mid_price = (best_ask + best_bid) / 2.0
            if mid_price <= 0 or mid_price < 1e-6:
                return cls._degraded_liquidity("中间价无效或过低")

            spread = best_ask - best_bid
            relative_spread = spread / mid_price

            bid_depth = sum(
                cls._sanitize_numeric(level[1], context="bid_qty")
                for level in bids[:5]
                if cls._sanitize_numeric(level[1], context="bid_qty") > 0
            )
            ask_depth = sum(
                cls._sanitize_numeric(level[1], context="ask_qty")
                for level in asks[:5]
                if cls._sanitize_numeric(level[1], context="ask_qty") > 0
            )
            total_depth = bid_depth + ask_depth

            # 流动性评级（价差 + 深度双重约束，L4 也增加深度下限）
            if relative_spread <= cls.LIQUIDITY_L1_MAX_SPREAD and total_depth >= cls.MIN_DEPTH_FOR_L1:
                liquidity_level = "L1"
            elif relative_spread <= cls.LIQUIDITY_L2_MAX_SPREAD and total_depth >= cls.MIN_DEPTH_FOR_L2:
                liquidity_level = "L2"
            elif relative_spread <= cls.LIQUIDITY_L3_MAX_SPREAD and total_depth >= cls.MIN_DEPTH_FOR_L3:
                liquidity_level = "L3"
            elif relative_spread <= cls.LIQUIDITY_L4_MAX_SPREAD and total_depth >= cls.MIN_DEPTH_FOR_L4:
                liquidity_level = "L4"
            else:
                liquidity_level = "L5"

            depth_decay_bps = 0.0
            if historical_depth_avg is not None and historical_depth_avg > 0:
                depth_ratio = total_depth / historical_depth_avg
                depth_ratio = min(depth_ratio, cls.MAX_DEPTH_RATIO)
                depth_decay_bps = round((1.0 - depth_ratio) * 10000, 1)
                if abs(depth_decay_bps) > cls.DEPTH_DECAY_WARNING_BPS:
                    logger.warning("深度变化异常: %.1f bps", depth_decay_bps)

            return {
                "status": "ok",
                "reason": f"流动性评估完成，等级 {liquidity_level}",
                "data": {
                    "sensor_version": cls.__version__,
                    "liquidity_level": liquidity_level,
                    "relative_spread": round(relative_spread, 6),
                    "total_depth": round(total_depth, 2),
                    "depth_decay_bps": depth_decay_bps,
                    "mid_price": round(mid_price, 6),
                },
                "warnings": warnings,
            }
        except Exception as e:
            logger.error("流动性感知异常: %s #RECOVERY: 检查订单簿数据格式", e)
            return cls._degraded_liquidity("流动性计算异常")

    @classmethod
    def _degraded_liquidity(cls, reason: str) -> Dict[str, Any]:
        return {
            "status": "degraded",
            "reason": cls._clean_reason(reason),
            "data": {
                "sensor_version": cls.__version__,
                "liquidity_level": cls.DEFAULT_LIQUIDITY_LEVEL,
                "relative_spread": cls.DEFAULT_RELATIVE_SPREAD,
                "total_depth": cls.DEFAULT_TOTAL_DEPTH,
                "depth_decay_bps": cls.DEFAULT_DEPTH_DECAY_BPS,
                "mid_price": cls.DEFAULT_MID_PRICE,
            },
            "warnings": ["LIQ_DEGRADED"],
        }

    @classmethod
    def sense_trade_pulse(cls, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        warnings = []
        try:
            if not isinstance(trades, list) or len(trades) < 2:
                return cls._degraded_pulse("成交数据不足")

            timestamps = []
            for t in trades:
                ts = t.get("timestamp")
                if isinstance(ts, (int, float)):
                    fts = cls._sanitize_numeric(float(ts), default=-1.0, context="trade_ts")
                    if fts > 0:
                        timestamps.append(fts)
            if len(timestamps) < 2:
                return cls._degraded_pulse("有效时间戳不足")

            timestamps.sort()
            now = time.time()
            future_count = sum(1 for ts in timestamps if ts > now + 86400)
            if future_count > 0:
                warnings.append(f"TIMESTAMP_FUTURE: {future_count}")

            intervals = []
            zero_count = 0
            for i in range(1, len(timestamps)):
                delta = timestamps[i] - timestamps[i - 1]
                if delta > 0:
                    intervals.append(delta)
                elif delta == 0:
                    zero_count += 1

            total_pairs = len(timestamps) - 1
            if total_pairs <= 0:
                return cls._degraded_pulse("无有效交易对")

            zero_pct = (zero_count / total_pairs) * 100 if total_pairs > 0 else 0
            if zero_pct > cls.ZERO_INTERVAL_WARNING_PCT:
                warnings.append(f"ZERO_INTERVAL_HIGH: {zero_pct:.1f}%")

            if len(intervals) < 2:
                return cls._degraded_pulse("有效间隔不足")

            mean_interval, variance = cls._welford_variance(intervals)
            if mean_interval <= 0:
                return cls._degraded_pulse("间隔统计无效")

            cv = math.sqrt(variance) / mean_interval if variance > 0 else 0.0
            cv_original = cv
            cv = min(cv, cls.PULSE_CV_MAX)
            if cv_original > cls.PULSE_CV_MAX:
                warnings.append(f"CV_CLAMPED: {cv_original:.2f}")

            if cv < cls.PULSE_CV_ALGO_THRESHOLD:
                regime = "algo_dominant"
                if cv == 0.0:
                    warnings.append("CV_ZERO: 所有间隔相等，可能为模拟数据")
            elif cv > cls.PULSE_CV_MANUAL_THRESHOLD:
                regime = "manual_chaotic"
            else:
                regime = "mixed"

            if len(intervals) < cls.PULSE_LOW_SAMPLE_WARNING:
                warnings.append(f"LOW_SAMPLE: n={len(intervals)}")

            return {
                "status": "ok",
                "reason": f"成交脉搏分析完成，CV={cv:.3f}",
                "data": {
                    "sensor_version": cls.__version__,
                    "pulse_cv": round(cv, 4),
                    "pulse_regime": regime,
                    "mean_interval_ms": round(mean_interval * 1000, 2),
                    "trade_count": len(trades),
                },
                "warnings": warnings,
            }
        except Exception as e:
            logger.error("成交脉搏感知异常: %s #RECOVERY: 检查逐笔成交数据格式", e)
            return cls._degraded_pulse("成交脉搏计算异常")

    @classmethod
    def _degraded_pulse(cls, reason: str) -> Dict[str, Any]:
        return {
            "status": "degraded",
            "reason": cls._clean_reason(reason),
            "data": {
                "sensor_version": cls.__version__,
                "pulse_cv": cls.DEFAULT_PULSE_CV,
                "pulse_regime": cls.DEFAULT_PULSE_REGIME,
                "mean_interval_ms": cls.DEFAULT_MEAN_INTERVAL_MS,
                "trade_count": cls.DEFAULT_TRADE_COUNT,
            },
            "warnings": ["PULSE_DEGRADED"],
        }

    @classmethod
    def sense_volatility_structure(cls, short_atr: float, long_atr: float) -> Dict[str, Any]:
        warnings = []
        try:
            short = cls._sanitize_numeric(short_atr, default=-1.0, context="short_atr")
            long = cls._sanitize_numeric(long_atr, default=-1.0, context="long_atr")
            if short <= 0 or long <= 0:
                return cls._degraded_volatility("ATR参数无效")

            ratio_original = short / long
            ratio = min(ratio_original, cls.VOL_RATIO_MAX)
            if ratio_original > cls.VOL_RATIO_MAX:
                warnings.append(f"VOL_CLAMPED: {ratio_original:.2f}")

            if ratio > cls.VOL_TERM_HEAT_THRESHOLD:
                status = "overheated"
            elif ratio < cls.VOL_TERM_COLD_THRESHOLD:
                status = "frozen"
            else:
                status = "normal"

            return {
                "status": "ok",
                "reason": f"波动率结构分析完成，比值={ratio:.2f}",
                "data": {
                    "sensor_version": cls.__version__,
                    "vol_ratio": round(ratio, 4),
                    "vol_status": status,
                },
                "warnings": warnings,
            }
        except Exception as e:
            logger.error("波动率结构感知异常: %s #RECOVERY: 检查ATR参数", e)
            return cls._degraded_volatility("波动率计算异常")

    @classmethod
    def _degraded_volatility(cls, reason: str) -> Dict[str, Any]:
        return {
            "status": "degraded",
            "reason": cls._clean_reason(reason),
            "data": {
                "sensor_version": cls.__version__,
                "vol_ratio": cls.DEFAULT_VOL_RATIO,
                "vol_status": cls.DEFAULT_VOL_STATUS,
            },
            "warnings": ["VOL_DEGRADED"],
        }

    @classmethod
    def snapshot(
        cls,
        orderbook: Optional[Dict] = None,
        trades: Optional[List[Dict]] = None,
        atr_short: Optional[float] = None,
        atr_long: Optional[float] = None,
        historical_depth_avg: Optional[float] = None,
    ) -> Dict[str, Any]:
        all_warnings = []
        statuses = []

        liquidity = cls.sense_liquidity(orderbook, historical_depth_avg) if orderbook else cls._degraded_liquidity("未提供订单簿")
        statuses.append(liquidity["status"])
        all_warnings.extend(liquidity.get("warnings", []))

        pulse = cls.sense_trade_pulse(trades) if trades else cls._degraded_pulse("未提供成交数据")
        statuses.append(pulse["status"])
        all_warnings.extend(pulse.get("warnings", []))

        if atr_short is not None and atr_long is not None:
            volatility = cls.sense_volatility_structure(atr_short, atr_long)
        else:
            volatility = cls._degraded_volatility("未提供ATR数据")
        statuses.append(volatility["status"])
        all_warnings.extend(volatility.get("warnings", []))

        if "error" in statuses:
            overall = "error"
        elif "degraded" in statuses:
            overall = "degraded"
        else:
            overall = "ok"

        return {
            "status": overall,
            "reason": f"触觉感官快照生成完成 (整体: {overall})",
            "data": {
                "sensor_version": cls.__version__,
                "liquidity": liquidity.get("data", {}),
                "pulse": pulse.get("data", {}),
                "volatility": volatility.get("data", {}),
            },
            "warnings": list(dict.fromkeys(all_warnings)),
        }

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        try:
            test_orderbook = {
                "bids": [[50000.0, 1.5], [49999.0, 3.0]],
                "asks": [[50001.0, 1.2], [50002.0, 2.5]],
            }
            thin_orderbook = {
                "bids": [[50000.0, 0.01]],
                "asks": [[50050.0, 0.01]],   # 大幅价差，深度极低，确保 L5
            }
            test_trades = [
                {"timestamp": 1.0}, {"timestamp": 1.2}, {"timestamp": 1.5}, {"timestamp": 1.9}
            ]
            liq1 = cls.sense_liquidity(test_orderbook)
            liq2 = cls.sense_liquidity(thin_orderbook)
            pulse = cls.sense_trade_pulse(test_trades)
            vol = cls.sense_volatility_structure(0.02, 0.015)

            failed = [name for name, res in [
                ("liquidity_normal", liq1), ("liquidity_thin", liq2),
                ("pulse", pulse), ("volatility", vol)
            ] if res["status"] == "error"]

            if failed:
                return {
                    "status": "degraded",
                    "reason": f"感官功能测试异常: {', '.join(failed)}",
                    "data": {"failed_sensors": failed},
                    "warnings": ["HEALTH_SENSOR_FAIL"],
                }

            thin_level = liq2["data"]["liquidity_level"]
            if thin_level not in ("L4", "L5"):
                return {
                    "status": "degraded",
                    "reason": f"低流动性盘口评级异常: {thin_level}",
                    "data": {"thin_level": thin_level},
                    "warnings": ["HEALTH_RATING_ANOMALY"],
                }

            return {
                "status": "ok",
                "reason": "TactileCortex 自检通过",
                "data": {"tested_sensors": ["liquidity", "pulse", "volatility"]},
                "warnings": [],
            }
        except Exception as e:
            logger.error("健康检查失败: %s #RECOVERY: 检查模块常量与方法完整性", e)
            return {
                "status": "error",
                "reason": f"健康检查异常: {e}",
                "data": {},
                "warnings": ["HEALTH_CHECK_FAIL"],
      }
