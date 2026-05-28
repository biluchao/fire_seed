"""
火种系统 · 时间戳校验器 (TimestampValidator)

核心职责：
1. 验证每个数据源的时间戳闭合性，确保K线数据在交易所确认周期正式闭合后才释放给下游模块
2. 对齐Tick级因子（OBI、CVD）与订单簿快照的最后更新时间戳，防止使用不完整数据
3. 提供数据完全就绪的综合判定，消除K线闭合与快照更新之间的时间窗错位风险

外部依赖（真实模块接口）：
- core.precision_timer.PrecisionTimer : 获取当前硬件时钟基准时间戳
- core.behavioral_logger.BehavioralLogger : 记录数据完整性校验日志与告警事件

接口契约：
- validate_kline_closure(kline: Dict[str, Any], current_time: float) -> Dict[str, Any]
  验证K线是否已正式闭合，返回闭合状态与等待时间建议。data中包含 is_ready (bool), closure_status (str), wait_seconds, closure_time
- validate_tick_alignment(tick_data: Dict[str, Any], orderbook_snapshot: Dict[str, Any]) -> Dict[str, Any]
  验证Tick数据与订单簿快照的时间戳对齐，返回对齐状态与偏差值
- is_snapshot_stale(orderbook_snapshot: Dict[str, Any]) -> Dict[str, Any]
  基于交易所服务器时间自洽检查订单簿快照是否过期
- is_data_fully_ready(symbol: str, timeframe: str, orderbook_snapshot: Dict[str, Any]) -> Dict[str, Any]
  综合验证K线闭合且订单簿快照在闭合时间之后，返回完全就绪状态（快照时间戳缺失时保守返回未就绪）
- get_data_ready_flag(symbol: str, timeframe: str) -> Dict[str, Any]
  查询指定品种和周期的数据就绪标记
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 PrecisionTimer 不可用时，使用 time.monotonic() 作为降级时间源，并自动放宽对齐阈值，同时记录降级状态
- 当 BehavioralLogger 不可用时，告警降级为标准 logger.warning
- 当交易所时间字段缺失时，快照过期判断降级为基于本地时间的保守估计
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护每个品种/周期的最后闭合时间戳缓存、延迟滑动窗口、数据就绪标记及异常计数，定期清理过期条目
- 使用可重入锁(RLock)确保嵌套调用的线程安全
- 不持有任何外部资源句柄
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple
from collections import deque
import numpy as np

logger = logging.getLogger(__name__)


class TimestampValidator:
    """时间戳校验器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_CACHE_TTL_SEC = 3600          # 闭合时间戳缓存过期时间，秒，取值范围 [600, 86400]
    DEFAULT_CLEANUP_INTERVAL_SEC = 900    # 缓存清理间隔，秒，取值范围 [300, 3600]
    DEFAULT_KLINE_CLOSURE_TOLERANCE_MS = 100  # K线闭合最小容忍时间，毫秒，[0, 2000]
    MAX_KLINE_CLOSURE_TOLERANCE_MS = 5000     # K线闭合最大容忍时间，毫秒，[500, 10000]
    LATENCY_BUFFER_MULTIPLIER = 3.0       # 延迟缓冲倍数，无量纲，[2.0, 5.0]
    LATENCY_WINDOW_SIZE = 10              # 延迟滑动窗口大小，无量纲，[5, 30]
    LATENCY_TREND_THRESHOLD_MS = 500      # 延迟上升趋势检测阈值（毫秒），[200, 2000]
    LATENCY_TREND_ACCEL_FACTOR = 1.5     # 延迟上升时加速放大因子，无量纲，[1.2, 2.0]
    DEFAULT_TICK_ALIGNMENT_MAX_DEVIATION_US = 50  # Tick对齐最大允许偏差（高精度时钟），微秒
    DEGRADED_TICK_ALIGNMENT_THRESHOLD_US = 2000  # 降级时钟下放宽的对齐阈值，微秒
    DEFAULT_SNAPSHOT_STALE_THRESHOLD_MS = 2000  # 订单簿快照过期阈值，毫秒，[500, 5000]
    DEFAULT_READY_FLAG_STALE_SEC = 7200   # 数据就绪标记过期时间，秒，[3600, 86400]
    MAX_CACHE_ENTRIES = 1000              # 缓存最大条目数，无量纲，[500, 5000]
    CONSECUTIVE_ANOMALY_THRESHOLD = 5     # 连续异常告警阈值，无量纲，[3, 10]
    LOCAL_STALE_MULTIPLIER = 3.0          # 本地时钟判断快照过期时的倍数放大，无量纲，[2.0, 5.0]
    DEGRADED_ALERT_DURATION_SEC = 300     # 降级持续告警阈值，秒，[120, 3600]
    MIN_VALID_TIMESTAMP_SEC = 1_000_000_000  # 最小有效时间戳（2001-09-09），秒，用于过滤无效值

    def __init__(self):
        # 缓存：{(symbol, timeframe): last_closure_timestamp}
        self._closure_cache: Dict[tuple, float] = {}
        self._cache_timestamps: Dict[tuple, float] = {}

        # 数据就绪标记：{(symbol, timeframe): bool}
        self._data_ready_flags: Dict[tuple, bool] = {}
        self._data_ready_timestamps: Dict[tuple, float] = {}

        # 延迟滑动窗口：{symbol: deque([latency_ms, ...], maxlen=LATENCY_WINDOW_SIZE)}
        self._latency_windows: Dict[str, deque] = {}
        # 延迟快速回退值
        self._latency_cache: Dict[str, float] = {}

        # 连续异常计数器：{(data_source_key): count}
        self._anomaly_counters: Dict[str, int] = {}

        # 时钟降级状态追踪
        self._degraded_since: Optional[float] = None
        self._degraded_duration_total: float = 0.0

        # 外部依赖注入
        self._precision_timer = None
        self._behavioral_logger = None

        # 线程安全（使用可重入锁避免嵌套调用死锁）
        self._lock = threading.RLock()

        # 清理定时器
        self._last_cleanup = time.time()

        logger.info("TimestampValidator 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        precision_timer: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）
        """
        if precision_timer is not None:
            self._precision_timer = precision_timer
            logger.info("PrecisionTimer 注入成功")
        else:
            logger.warning("PrecisionTimer 未注入，使用 time.monotonic() 降级")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

    # ========== 公共接口 ==========
    def validate_kline_closure(
        self, kline: Dict[str, Any], current_time: float
    ) -> Dict[str, Any]:
        """
        验证K线是否已正式闭合

        Args:
            kline: K线数据字典，必须包含 symbol, timeframe, close_time, is_closed 字段
            current_time: 当前时间戳（秒）

        Returns:
            标准响应字典，data 中包含 is_ready, closure_status, wait_seconds, closure_time
        """
        symbol = kline.get("symbol")
        timeframe = kline.get("timeframe")
        close_time_raw = kline.get("close_time")
        is_closed = kline.get("is_closed", False)

        # 参数校验
        if not symbol or not timeframe:
            logger.warning("K线数据缺少必要字段: symbol 或 timeframe")
            return {
                "status": "error",
                "reason": "K线数据缺少必要字段: symbol 或 timeframe",
                "data": {},
                "warnings": ["missing_required_fields"],
            }

        # 时间戳单位归一化
        close_time_sec = self._normalize_to_seconds(close_time_raw)

        if close_time_sec is None:
            self._set_data_ready(symbol, timeframe, False)
            self._increment_anomaly(symbol)
            return {
                "status": "ok",
                "reason": f"{symbol}/{timeframe} K线闭合时间未知，假定未闭合",
                "data": {
                    "is_ready": False,
                    "closure_status": "unknown_close_time",
                    "wait_seconds": 5.0,
                    "closure_time": None,
                },
                "warnings": ["unknown_close_time"],
            }

        # 检查交易所标记
        if is_closed:
            self._reset_anomaly(symbol)
            self._set_data_ready(symbol, timeframe, True)
            self._update_closure_cache(symbol, timeframe, close_time_sec)
            self._update_latency(symbol, current_time, close_time_sec)
            return {
                "status": "ok",
                "reason": f"{symbol}/{timeframe} K线已确认闭合",
                "data": {
                    "is_ready": True,
                    "closure_status": "confirmed_closed",
                    "wait_seconds": 0.0,
                    "closure_time": close_time_sec,
                },
                "warnings": [],
            }

        # 未标记闭合，检查时间是否已过闭合时间
        time_diff_ms = (current_time - close_time_sec) * 1000.0

        # 使用自适应容忍时间
        adaptive_tolerance = self._get_adaptive_tolerance(symbol, time_diff_ms)

        if time_diff_ms >= adaptive_tolerance:
            logger.debug(
                "%s/%s K线闭合时间已过 %.0fms，但未标记闭合，容忍时间=%.0fms，接受数据",
                symbol, timeframe, time_diff_ms, adaptive_tolerance
            )
            self._increment_anomaly(symbol)
            self._set_data_ready(symbol, timeframe, True)
            self._update_closure_cache(symbol, timeframe, close_time_sec)
            self._update_latency(symbol, current_time, close_time_sec)
            return {
                "status": "ok",
                "reason": f"{symbol}/{timeframe} K线闭合时间已过，接受数据",
                "data": {
                    "is_ready": True,
                    "closure_status": "tolerance_passed",
                    "wait_seconds": 0.0,
                    "closure_time": close_time_sec,
                },
                "warnings": ["closure_flag_delayed"],
            }

        # 还在等待闭合
        wait_seconds = (adaptive_tolerance - time_diff_ms) / 1000.0
        self._set_data_ready(symbol, timeframe, False)
        return {
            "status": "ok",
            "reason": f"{symbol}/{timeframe} K线尚未闭合，需等待 {wait_seconds:.2f}s",
            "data": {
                "is_ready": False,
                "closure_status": "not_closed",
                "wait_seconds": max(0.0, wait_seconds),
                "closure_time": close_time_sec,
            },
            "warnings": [],
        }

    def validate_tick_alignment(
        self,
        tick_data: Dict[str, Any],
        orderbook_snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        验证Tick数据与订单簿快照的时间戳对齐（仅检查两者偏差，不判断快照新鲜度）

        Args:
            tick_data: Tick成交数据，必须包含 timestamp_us 字段
            orderbook_snapshot: 订单簿快照，必须包含 last_update_timestamp_us 字段

        Returns:
            标准响应字典，data 中包含 aligned, deviation_us
        """
        tick_ts = tick_data.get("timestamp_us")
        snapshot_ts = orderbook_snapshot.get("last_update_timestamp_us")

        if tick_ts is None or snapshot_ts is None:
            logger.warning("Tick或订单簿快照缺少时间戳字段")
            return {
                "status": "error",
                "reason": "Tick或订单簿快照缺少时间戳字段",
                "data": {},
                "warnings": ["missing_timestamp_fields"],
            }

        # 归一化到微秒
        tick_ts_us = self._normalize_to_microseconds(tick_ts)
        snapshot_ts_us = self._normalize_to_microseconds(snapshot_ts)

        if tick_ts_us is None or snapshot_ts_us is None:
            return {
                "status": "error",
                "reason": "时间戳归一化失败",
                "data": {},
                "warnings": ["timestamp_normalization_failed"],
            }

        # 获取时钟精度，决定对齐阈值
        _, is_degraded = self._get_current_time_us()
        effective_threshold = (
            self.DEGRADED_TICK_ALIGNMENT_THRESHOLD_US if is_degraded
            else self.DEFAULT_TICK_ALIGNMENT_MAX_DEVIATION_US
        )

        # 计算偏差
        deviation = abs(tick_ts_us - snapshot_ts_us)
        aligned = deviation <= effective_threshold

        warnings = []
        if not aligned:
            warnings.append(
                f"Tick与快照时间偏差过大: {deviation}μs > {effective_threshold}μs"
            )
            if is_degraded:
                warnings.append("当前使用降级时钟，对齐阈值已放宽")

        logger.debug(
            "Tick对齐检查: 偏差=%dμs, 对齐=%s, 降级时钟=%s",
            deviation, aligned, is_degraded
        )

        return {
            "status": "ok",
            "reason": f"Tick与快照{'已对齐' if aligned else '未对齐'}",
            "data": {
                "aligned": aligned,
                "deviation_us": deviation,
            },
            "warnings": warnings,
        }

    def is_snapshot_stale(self, orderbook_snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """
        基于交易所服务器时间自洽检查订单簿快照是否过期。
        当交易所时间不可用时，降级为基于本地时间的保守估计。

        Args:
            orderbook_snapshot: 订单簿快照，必须包含 last_update_timestamp_us 和 exchange_current_time_us（可选）

        Returns:
            标准响应字典，data 中包含 stale, age_us, uncertain 等
        """
        snapshot_ts_raw = orderbook_snapshot.get("last_update_timestamp_us")
        exchange_time_raw = orderbook_snapshot.get("exchange_current_time_us")

        snapshot_ts_us = self._normalize_to_microseconds(snapshot_ts_raw)
        exchange_time_us = self._normalize_to_microseconds(exchange_time_raw)

        if snapshot_ts_us is None:
            return {
                "status": "warning",
                "reason": "快照缺少 last_update_timestamp_us 字段，无法判定新鲜度",
                "data": {"stale": False, "uncertain": True},
                "warnings": ["missing_snapshot_timestamp"],
            }

        # 最佳情况：使用交易所时间
        if exchange_time_us is not None:
            age_us = abs(exchange_time_us - snapshot_ts_us)
            stale = age_us > self.DEFAULT_SNAPSHOT_STALE_THRESHOLD_MS * 1000
            return {
                "status": "ok",
                "reason": f"快照{'已过期' if stale else '新鲜'}（交易所时间）",
                "data": {"stale": stale, "age_us": age_us, "uncertain": False},
                "warnings": ["snapshot_stale"] if stale else [],
            }

        # 降级：使用本地时间戳差值判断
        local_now, is_degraded = self._get_current_time_us()
        local_age = abs(local_now - snapshot_ts_us)
        adjusted_threshold = (
            self.DEFAULT_SNAPSHOT_STALE_THRESHOLD_MS * 1000 * self.LOCAL_STALE_MULTIPLIER
        )
        if local_age > adjusted_threshold:
            logger.warning("交易所时间缺失，本地时间判断快照已过期: age=%dμs", local_age)
            return {
                "status": "warning",
                "reason": "交易所时间不可用，基于本地时间判断快照已过期",
                "data": {"stale": True, "age_us": local_age, "uncertain": True},
                "warnings": ["snapshot_stale_local_fallback", "exchange_time_missing"],
            }

        # 仍无法确定，标记存疑
        return {
            "status": "warning",
            "reason": "无法获取有效时间戳，快照新鲜度存疑",
            "data": {"stale": False, "uncertain": True},
            "warnings": ["snapshot_freshness_uncertain"],
        }

    def is_data_fully_ready(
        self,
        symbol: str,
        timeframe: str,
        orderbook_snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        综合验证：K线已闭合 AND 最近一次订单簿快照的时间戳晚于K线闭合时间
        当快照时间戳缺失时，保守判定为未就绪

        Args:
            symbol: 交易对
            timeframe: K线周期
            orderbook_snapshot: 最新订单簿快照

        Returns:
            标准响应字典，data 中包含 fully_ready, reason, gap_sec 等
        """
        kline_ready_result = self.get_data_ready_flag(symbol, timeframe)
        if not kline_ready_result.get("data", {}).get("is_ready", False):
            return {
                "status": "ok",
                "reason": f"{symbol}/{timeframe} K线尚未闭合，数据未完全就绪",
                "data": {"fully_ready": False, "reason": "kline_not_closed"},
                "warnings": [],
            }

        key = (symbol, timeframe)
        with self._lock:
            closure_time = self._closure_cache.get(key)

        if closure_time is None:
            return {
                "status": "ok",
                "reason": "K线闭合但闭合时间未知，假定数据就绪",
                "data": {"fully_ready": True, "reason": "closure_time_unknown"},
                "warnings": ["closure_time_missing"],
            }

        snapshot_ts_raw = orderbook_snapshot.get("last_update_timestamp_us")
        snapshot_ts_sec = self._normalize_to_seconds(snapshot_ts_raw)

        if snapshot_ts_sec is None:
            # 保守策略：时间戳缺失时判定为未就绪
            return {
                "status": "ok",
                "reason": "快照缺少时间戳，保守判定为未就绪",
                "data": {"fully_ready": False, "reason": "snapshot_timestamp_missing"},
                "warnings": ["snapshot_timestamp_missing"],
            }

        fully_ready = snapshot_ts_sec >= closure_time

        return {
            "status": "ok",
            "reason": f"数据{'完全就绪' if fully_ready else '等待快照更新'}",
            "data": {
                "fully_ready": fully_ready,
                "closure_time": closure_time,
                "snapshot_time": snapshot_ts_sec,
                "gap_sec": (
                    snapshot_ts_sec - closure_time if fully_ready
                    else closure_time - snapshot_ts_sec
                ),
            },
            "warnings": [] if fully_ready else ["snapshot_behind_closure"],
        }

    def get_data_ready_flag(self, symbol: str, timeframe: str) -> Dict[str, Any]:
        """
        查询指定品种和周期的数据就绪标记

        Args:
            symbol: 交易对符号
            timeframe: K线周期

        Returns:
            标准响应字典，data 中包含 is_ready
        """
        if not symbol or not timeframe:
            return {
                "status": "error",
                "reason": "symbol 和 timeframe 不能为空",
                "data": {},
                "warnings": ["invalid_arguments"],
            }

        key = (symbol, timeframe)
        with self._lock:
            is_ready = self._data_ready_flags.get(key, False)
            if is_ready:
                last_set = self._data_ready_timestamps.get(key, 0)
                if time.time() - last_set > self.DEFAULT_READY_FLAG_STALE_SEC:
                    self._data_ready_flags[key] = False
                    is_ready = False
                    logger.debug("就绪标记过期自动重置: %s/%s", symbol, timeframe)
            # 惰性清理过期闭合缓存（线程安全）
            self._lazy_clean_stale_cache(key)

        return {
            "status": "ok",
            "reason": f"{symbol}/{timeframe} 数据{'已就绪' if is_ready else '未就绪'}",
            "data": {"is_ready": is_ready},
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检

        Returns:
            标准健康检查响应字典
        """
        try:
            with self._lock:
                cache_size = len(self._closure_cache)
                ready_count = sum(1 for v in self._data_ready_flags.values() if v)
                test_time, is_degraded = self._get_current_time_us()
                oldest_cache_age = 0.0
                if self._cache_timestamps:
                    oldest_cache_age = time.time() - min(self._cache_timestamps.values())

                anomaly_sources = [
                    {"source": src, "count": cnt}
                    for src, cnt in self._anomaly_counters.items()
                    if cnt >= self.CONSECUTIVE_ANOMALY_THRESHOLD
                ]

            return {
                "status": "ok",
                "reason": f"TimestampValidator 正常，缓存条目 {cache_size}，就绪标记 {ready_count}",
                "data": {
                    "cache_entries": cache_size,
                    "ready_flags": ready_count,
                    "current_time_us": test_time,
                    "clock_degraded": is_degraded,
                    "degraded_since": self._degraded_since,
                    "degraded_duration_total_sec": round(self._degraded_duration_total, 1),
                    "oldest_cache_age_sec": round(oldest_cache_age, 1),
                    "anomaly_sources": anomaly_sources,
                    "dependencies": {
                        "precision_timer": self._precision_timer is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查缓存字典完整性和线程锁状态")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    @staticmethod
    def _normalize_to_seconds(timestamp: Any) -> Optional[float]:
        """将各种可能的时间戳格式统一转换为秒（float）。小于最小有效阈值的视为无效时间戳。"""
        if timestamp is None:
            return None
        if isinstance(timestamp, (int, float)):
            if timestamp > 1_000_000_000_000_000:       # > 10^15，微秒
                return timestamp / 1_000_000.0
            elif timestamp > 1_000_000_000_000:          # > 10^12，毫秒
                return timestamp / 1000.0
            elif timestamp >= TimestampValidator.MIN_VALID_TIMESTAMP_SEC:
                return float(timestamp)
            else:
                logger.warning(f"无效时间戳（值过小）: {timestamp}")
                return None
        return None

    @staticmethod
    def _normalize_to_microseconds(timestamp: Any) -> Optional[int]:
        """将各种可能的时间戳格式统一转换为微秒（int）。"""
        seconds = TimestampValidator._normalize_to_seconds(timestamp)
        if seconds is None:
            return None
        return int(seconds * 1_000_000)

    def _set_data_ready(self, symbol: str, timeframe: str, is_ready: bool) -> None:
        """设置数据就绪标记"""
        key = (symbol, timeframe)
        now = time.time()
        with self._lock:
            self._data_ready_flags[key] = is_ready
            self._data_ready_timestamps[key] = now
            if not is_ready:
                logger.debug("数据未就绪: %s/%s", symbol, timeframe)
            if len(self._data_ready_flags) > self.MAX_CACHE_ENTRIES:
                stale_keys = [
                    k for k, ts in self._data_ready_timestamps.items()
                    if now - ts > self.DEFAULT_READY_FLAG_STALE_SEC
                ]
                for k in stale_keys:
                    del self._data_ready_flags[k]
                    del self._data_ready_timestamps[k]
                logger.debug("清理过期就绪标记: %d 个", len(stale_keys))

    def _update_closure_cache(self, symbol: str, timeframe: str, closure_time: float) -> None:
        """更新闭合时间戳缓存（线程安全，避免死锁）"""
        key = (symbol, timeframe)
        with self._lock:
            self._closure_cache[key] = closure_time
            self._cache_timestamps[key] = time.time()
            if len(self._closure_cache) > self.MAX_CACHE_ENTRIES:
                oldest_key = min(self._cache_timestamps, key=self._cache_timestamps.get)
                del self._closure_cache[oldest_key]
                del self._cache_timestamps[oldest_key]
                logger.debug("闭合缓存超限，清理最旧条目: %s", oldest_key)
        # 清理操作移出锁外，避免锁中锁导致死锁
        self._try_cleanup()

    def _update_latency(self, symbol: str, current_time: float, close_time_sec: float) -> None:
        """更新延迟滑动窗口"""
        latency_ms = max(0.0, (current_time - close_time_sec) * 1000.0)
        with self._lock:
            if symbol not in self._latency_windows:
                self._latency_windows[symbol] = deque(maxlen=self.LATENCY_WINDOW_SIZE)
            self._latency_windows[symbol].append(latency_ms)
            self._latency_cache[symbol] = latency_ms

    def _get_adaptive_tolerance(self, symbol: str, current_delay_ms: float) -> float:
        """根据延迟滑动窗口的P95及趋势计算自适应容忍时间"""
        with self._lock:
            window = self._latency_windows.get(symbol, deque([current_delay_ms]))
            recent = self._latency_cache.get(symbol, self.DEFAULT_KLINE_CLOSURE_TOLERANCE_MS)

        if len(window) >= 3:
            p95_latency = np.percentile(list(window), 95)
        else:
            p95_latency = recent

        # 上升趋势加速
        if len(window) >= 2:
            trend = list(window)[-1] - list(window)[-2]
            if trend > self.LATENCY_TREND_THRESHOLD_MS:
                p95_latency *= self.LATENCY_TREND_ACCEL_FACTOR

        base_delay = max(current_delay_ms, p95_latency)
        adaptive = base_delay * self.LATENCY_BUFFER_MULTIPLIER
        return max(self.DEFAULT_KLINE_CLOSURE_TOLERANCE_MS,
                   min(adaptive, self.MAX_KLINE_CLOSURE_TOLERANCE_MS))

    def _get_current_time_us(self) -> Tuple[int, bool]:
        """获取当前时间戳（微秒），返回 (timestamp, is_degraded)。同时管理降级状态。"""
        if self._precision_timer is not None:
            try:
                ts = self._precision_timer.get_timestamp_us()
                # 检查是否需要从降级恢复
                if self._degraded_since is not None:
                    degraded_sec = time.time() - self._degraded_since
                    self._degraded_duration_total += degraded_sec
                    self._degraded_since = None
                    logger.info("PrecisionTimer 已恢复，降级持续 %.1f 秒", degraded_sec)
                return ts, False
            except Exception as e:
                logger.warning(f"PrecisionTimer 获取时间戳失败: {e}，降级使用 time.monotonic()")

        # 降级模式
        if self._degraded_since is None:
            self._degraded_since = time.time()
            logger.warning("进入降级时钟模式，Tick对齐阈值将放宽")
        elif (time.time() - self._degraded_since) > self.DEGRADED_ALERT_DURATION_SEC:
            logger.error(
                "时钟降级持续超过 %d 秒 #RECOVERY: 检查 PrecisionTimer 模块状态",
                self.DEGRADED_ALERT_DURATION_SEC
            )
        return int(time.monotonic() * 1_000_000), True

    def _increment_anomaly(self, data_source: str) -> str:
        """递增连续异常计数并返回异常等级"""
        with self._lock:
            count = self._anomaly_counters.get(data_source, 0) + 1
            self._anomaly_counters[data_source] = count

        if count >= self.CONSECUTIVE_ANOMALY_THRESHOLD * 3:
            logger.error("%s 连续异常 %d 次，建议隔离该数据源", data_source, count)
            return "critical"
        elif count >= self.CONSECUTIVE_ANOMALY_THRESHOLD:
            logger.error("%s 连续异常 %d 次，建议降权处理", data_source, count)
            return "warning"
        return "normal"

    def _reset_anomaly(self, data_source: str) -> None:
        """重置异常计数"""
        with self._lock:
            self._anomaly_counters.pop(data_source, None)

    def _lazy_clean_stale_cache(self, key: tuple) -> None:
        """惰性清理过期闭合缓存条目（线程安全）"""
        with self._lock:
            ts = self._cache_timestamps.get(key)
            if ts and (time.time() - ts) > self.DEFAULT_CACHE_TTL_SEC:
                self._closure_cache.pop(key, None)
                self._cache_timestamps.pop(key, None)
                logger.debug("惰性清理过期闭合缓存: %s", key)

    def _try_cleanup(self) -> None:
        """定期清理过期的闭合缓存条目（线程安全）"""
        now = time.time()
        if now - self._last_cleanup < self.DEFAULT_CLEANUP_INTERVAL_SEC:
            return

        with self._lock:
            cutoff = now - self.DEFAULT_CACHE_TTL_SEC
            expired_keys = [
                key for key, ts in self._cache_timestamps.items() if ts < cutoff
            ]
            for key in expired_keys:
                self._closure_cache.pop(key, None)
                self._cache_timestamps.pop(key, None)

        self._last_cleanup = now
        if expired_keys:
            logger.info("清理过期闭合缓存条目: %d 个", len(expired_keys))
