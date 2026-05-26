"""
火种系统 · 时间戳校验器 (TimestampValidator)

核心职责：
1. 验证每个数据源的时间戳闭合性，确保K线数据在交易所确认周期正式闭合后才释放给下游模块
2. 对齐Tick级因子（OBI、CVD）与订单簿快照的最后更新时间戳，防止使用不完整数据

外部依赖（真实模块接口）：
- core.precision_timer.PrecisionTimer : 获取当前硬件时钟基准时间戳
- core.behavioral_logger.BehavioralLogger : 记录数据完整性校验日志与告警事件

接口契约：
- validate_kline_closure(kline: Dict[str, Any], current_time: float) -> Dict[str, Any]
  验证K线是否已正式闭合，返回闭合状态与等待时间建议
- validate_tick_alignment(tick_data: Dict[str, Any], orderbook_snapshot: Dict[str, Any]) -> Dict[str, Any]
  验证Tick数据与订单簿快照的时间戳对齐，返回对齐状态与偏差值
- is_snapshot_stale(orderbook_snapshot: Dict[str, Any]) -> Dict[str, Any]
  基于交易所服务器时间自洽检查订单簿快照是否过期
- get_data_ready_flag(symbol: str, timeframe: str) -> Dict[str, Any]
  查询指定品种和周期的数据就绪标记
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 PrecisionTimer 不可用时，使用 time.monotonic() 作为降级时间源
- 当 BehavioralLogger 不可用时，告警降级为标准 logger.warning
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护每个品种/周期的最后闭合时间戳缓存和数据就绪标记，定期清理过期条目
- 不持有任何外部资源句柄
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class TimestampValidator:
    """时间戳校验器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_CACHE_TTL_SEC = 3600          # 闭合时间戳缓存过期时间，秒，取值范围 [600, 86400]
    DEFAULT_CLEANUP_INTERVAL_SEC = 900    # 缓存清理间隔，秒，取值范围 [300, 3600]
    DEFAULT_KLINE_CLOSURE_TOLERANCE_MS = 100  # K线闭合确认容忍时间，毫秒，[0, 2000]
    DEFAULT_TICK_ALIGNMENT_MAX_DEVIATION_US = 50  # Tick对齐最大允许偏差，微秒，[10, 500]
    DEFAULT_SNAPSHOT_STALE_THRESHOLD_MS = 2000  # 订单簿快照过期阈值，毫秒，[500, 5000]
    DEFAULT_READY_FLAG_STALE_SEC = 7200   # 数据就绪标记过期时间，秒，[3600, 86400]
    MAX_CACHE_ENTRIES = 1000              # 缓存最大条目数，无量纲，[500, 5000]

    def __init__(self):
        # 缓存：{(symbol, timeframe): last_closure_timestamp}
        self._closure_cache: Dict[tuple, float] = {}
        self._cache_timestamps: Dict[tuple, float] = {}

        # 数据就绪标记：{(symbol, timeframe): bool}
        self._data_ready_flags: Dict[tuple, bool] = {}
        self._data_ready_timestamps: Dict[tuple, float] = {}

        # 外部依赖注入
        self._precision_timer = None
        self._behavioral_logger = None

        # 线程安全
        self._lock = threading.Lock()

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
            标准响应字典，data 中包含 is_ready, wait_seconds, closure_time
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

        # 时间戳单位归一化（毫秒 -> 秒）
        if close_time_raw is not None:
            if close_time_raw > 10**10:  # 毫秒级时间戳
                close_time_sec = close_time_raw / 1000.0
            else:  # 秒级时间戳
                close_time_sec = float(close_time_raw)
        else:
            close_time_sec = None

        if close_time_sec is None:
            # 无法确定闭合时间，假定未闭合
            self._set_data_ready(symbol, timeframe, False)
            return {
                "status": "ok",
                "reason": f"{symbol}/{timeframe} K线闭合时间未知，假定未闭合",
                "data": {"is_ready": False, "wait_seconds": 5.0, "closure_time": None},
                "warnings": ["unknown_close_time"],
            }

        # 检查交易所标记
        if is_closed:
            self._set_data_ready(symbol, timeframe, True)
            self._update_closure_cache(symbol, timeframe, close_time_sec)
            return {
                "status": "ok",
                "reason": f"{symbol}/{timeframe} K线已确认闭合",
                "data": {
                    "is_ready": True,
                    "wait_seconds": 0.0,
                    "closure_time": close_time_sec,
                },
                "warnings": [],
            }

        # 未标记闭合，检查时间是否已过闭合时间
        time_diff_ms = (current_time - close_time_sec) * 1000.0
        if time_diff_ms >= self.DEFAULT_KLINE_CLOSURE_TOLERANCE_MS:
            # 时间已过，但交易所未标记闭合，可能是延迟
            logger.debug(
                "%s/%s K线闭合时间已过 %.0fms，但未标记闭合，等待确认",
                symbol, timeframe, time_diff_ms
            )
            self._set_data_ready(symbol, timeframe, True)
            self._update_closure_cache(symbol, timeframe, close_time_sec)
            return {
                "status": "ok",
                "reason": f"{symbol}/{timeframe} K线闭合时间已过，接受数据",
                "data": {
                    "is_ready": True,
                    "wait_seconds": 0.0,
                    "closure_time": close_time_sec,
                },
                "warnings": ["closure_flag_delayed"],
            }

        # 还在等待闭合
        wait_seconds = (self.DEFAULT_KLINE_CLOSURE_TOLERANCE_MS - time_diff_ms) / 1000.0
        self._set_data_ready(symbol, timeframe, False)
        return {
            "status": "ok",
            "reason": f"{symbol}/{timeframe} K线尚未闭合，需等待 {wait_seconds:.2f}s",
            "data": {
                "is_ready": False,
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

        # 计算偏差
        deviation = abs(tick_ts - snapshot_ts)
        aligned = deviation <= self.DEFAULT_TICK_ALIGNMENT_MAX_DEVIATION_US

        warnings = []
        if not aligned:
            warnings.append(
                f"Tick与快照时间偏差过大: {deviation}μs > {self.DEFAULT_TICK_ALIGNMENT_MAX_DEVIATION_US}μs"
            )

        logger.debug(
            "Tick对齐检查: 偏差=%dμs, 对齐=%s",
            deviation, aligned
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
        基于交易所服务器时间自洽检查订单簿快照是否过期

        Args:
            orderbook_snapshot: 订单簿快照，必须包含 last_update_timestamp_us 和 exchange_current_time_us

        Returns:
            标准响应字典，data 中包含 stale, age_us
        """
        snapshot_ts = orderbook_snapshot.get("last_update_timestamp_us")
        exchange_time = orderbook_snapshot.get("exchange_current_time_us")

        if snapshot_ts is None or exchange_time is None:
            return {
                "status": "warning",
                "reason": "快照缺少时间戳字段，无法判定新鲜度",
                "data": {"stale": False},
                "warnings": ["missing_timestamp_for_staleness"],
            }

        age_us = abs(exchange_time - snapshot_ts)
        stale = age_us > self.DEFAULT_SNAPSHOT_STALE_THRESHOLD_MS * 1000

        return {
            "status": "ok",
            "reason": f"快照{'已过期' if stale else '新鲜'}",
            "data": {"stale": stale, "age_us": age_us},
            "warnings": ["snapshot_stale"] if stale else [],
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
            # 检查就绪标记的时效性：若标记为True但长时间未更新，自动重置为False
            if is_ready:
                last_set = self._data_ready_timestamps.get(key, 0)
                if time.time() - last_set > self.DEFAULT_READY_FLAG_STALE_SEC:
                    self._data_ready_flags[key] = False
                    is_ready = False
                    logger.debug("就绪标记过期自动重置: %s/%s", symbol, timeframe)

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
                test_time = self._get_current_time_us()

            return {
                "status": "ok",
                "reason": f"TimestampValidator 正常，缓存条目 {cache_size}，就绪标记 {ready_count}",
                "data": {
                    "cache_entries": cache_size,
                    "ready_flags": ready_count,
                    "current_time_us": test_time,
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
    def _set_data_ready(self, symbol: str, timeframe: str, is_ready: bool) -> None:
        """设置数据就绪标记（同时记录时间戳）"""
        key = (symbol, timeframe)
        now = time.time()
        with self._lock:
            self._data_ready_flags[key] = is_ready
            self._data_ready_timestamps[key] = now
            if not is_ready:
                logger.debug("数据未就绪: %s/%s", symbol, timeframe)
            # 防止缓存无限膨胀：当条目超过上限时，清理最旧的已失效标记
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
        """更新闭合时间戳缓存"""
        key = (symbol, timeframe)
        with self._lock:
            self._closure_cache[key] = closure_time
            self._cache_timestamps[key] = time.time()

            # 缓存条目超限时清理最旧条目（仅清理闭合缓存）
            if len(self._closure_cache) > self.MAX_CACHE_ENTRIES:
                oldest_key = min(self._cache_timestamps, key=self._cache_timestamps.get)
                del self._closure_cache[oldest_key]
                del self._cache_timestamps[oldest_key]
                logger.debug("闭合缓存超限，清理最旧条目: %s", oldest_key)

        self._try_cleanup()

    def _get_current_time_us(self) -> int:
        """获取当前时间戳（微秒）"""
        if self._precision_timer is not None:
            try:
                return self._precision_timer.get_timestamp_us()
            except Exception as e:
                logger.warning(f"PrecisionTimer 获取时间戳失败: {e}，降级使用 time.monotonic()")
        # 降级
        return int(time.monotonic() * 1_000_000)

    def _try_cleanup(self) -> None:
        """定期清理过期的闭合缓存条目（不触碰数据就绪标记）"""
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
