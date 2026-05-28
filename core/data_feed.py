"""
火种系统 · 行情数据融合入口 (DataFeed)

核心职责：
1. 作为行情数据模块的统一入口，协调行情接收、数据校验与实时推送
2. 集成时间戳验证与多源加权聚合，确保每次决策使用的都是最新、最可靠的数据

外部依赖（真实模块接口）：
- core.data_feed.market_data_aggregator.MarketDataAggregator : 执行多源行情接收、缓存与合约规格管理
- core.data_feed.timestamp_validator.TimestampValidator : 校验行情的时间戳与序列号连续性
- core.negotiation_bus.NegotiationBus : 向系统广播行情更新事件与数据异常告警
- core.behavioral_logger.BehavioralLogger : 记录行情数据质量日志
- core.symbol_mapper.SymbolMapper : 内部标准名与交易所本地名的双向转换

接口契约：
- subscribe(symbols: List[str]) -> Dict[str, Any] : 订阅指定交易对的实时行情
- get_latest_quote(symbol: str) -> Dict[str, Any] : 获取指定交易对的最新行情快照
- get_historical_klines(symbol: str, interval: str, limit: int) -> Dict[str, Any] : 获取历史K线数据
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 MarketDataAggregator 不可用时，所有行情查询降级为空数据并标记 "degraded"
- 当 TimestampValidator 不可用时，跳过校验但记录告警
- 当 NegotiationBus 不可用时，行情广播降级为本地日志
- 所有降级值在类常量区明确声明

资源管理：
- 行情缓存和连接生命周期由 MarketDataAggregator 管理，本模块不持有额外资源
- 行情更新回调由聚合器触发，本模块负责校验后广播
"""

import logging
import time
import threading
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class DataFeed:
    """行情数据融合入口"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_MAX_QUOTE_AGE_SEC = 5.0        # 行情快照最大有效时间，秒，取值范围 [1.0, 30.0]
    DEFAULT_KLINE_LIMIT = 100              # 单次获取历史K线的默认根数，无量纲，[1, 1000]
    STALE_QUOTE_WARNING_RATIO = 0.5        # 行情延迟预警比例，当 age 超过此比例时发出预警，[0.3, 0.8]
    STREAM_TIMEOUT_SEC = 30.0              # 行情断流超时诊断阈值，秒，[10.0, 120.0]
    SUBSCRIBE_MAX_RETRIES = 3              # 订阅失败最大重试次数，无量纲，[1, 5]
    SUBSCRIBE_RETRY_BACKOFF_BASE = 1.0     # 重试退避基础间隔，秒，[0.5, 5.0]
    MAX_SUBSCRIBE_SYMBOLS = 100            # 单次订阅最大交易对数量，无量纲，[1, 500]
    FREQUENCY_WINDOW_SEC = 10.0            # 行情频率监控滑动窗口，秒，[5.0, 60.0]
    AGGREGATOR_LATENCY_WINDOW = 20         # 聚合器延迟监控样本数，无量纲，[10, 100]

    def __init__(self):
        # 外部依赖注入
        self._aggregator: Optional[Any] = None
        self._timestamp_validator = None
        self._negotiation_bus = None
        self._behavioral_logger = None
        self._symbol_mapper = None

        # 可观测性统计（线程安全由 _stats_lock 保护）
        self._stats_lock = threading.Lock()
        self._discard_count: int = 0
        self._receive_timestamps: deque = deque(maxlen=200)
        self._aggregator_latencies: deque = deque(maxlen=self.AGGREGATOR_LATENCY_WINDOW)

        logger.info("DataFeed 初始化完成，等待依赖注入与行情订阅")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        market_data_aggregator: Optional[Any] = None,
        timestamp_validator: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        symbol_mapper: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选注入，未注入时对应功能降级）"""
        if market_data_aggregator is not None:
            if hasattr(market_data_aggregator, 'set_update_callback'):
                market_data_aggregator.set_update_callback(self._on_market_data)
            self._aggregator = market_data_aggregator
            logger.info("MarketDataAggregator 注入成功并注册行情回调")
        else:
            logger.warning("MarketDataAggregator 未注入，行情功能完全降级")

        self._timestamp_validator = timestamp_validator
        if timestamp_validator is None:
            logger.warning("TimestampValidator 未注入，数据校验降级")

        self._negotiation_bus = negotiation_bus
        if negotiation_bus is None:
            logger.warning("NegotiationBus 未注入，行情广播降级为本地日志")

        self._behavioral_logger = behavioral_logger
        if behavioral_logger is None:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

        self._symbol_mapper = symbol_mapper
        if symbol_mapper is None:
            logger.warning("SymbolMapper 未注入，交易对名称将直接使用内部标准名")

    # ========== 行情更新回调（供 MarketDataAggregator 调用） ==========
    def _on_market_data(self, symbol: str, quote: Dict[str, Any]) -> None:
        """
        收到新行情时的内部回调，负责校验后广播

        ⚠️ 此方法运行在聚合器的回调线程中，非主线程。所有外部调用必须确保线程安全。

        Args:
            symbol: 交易对内部标准名
            quote: 原始行情快照，必须包含 "timestamp" 字段
        """
        # 记录收包时间用于频率监控
        now = time.time()
        with self._stats_lock:
            self._receive_timestamps.append(now)

        # 1. 时间戳校验（若注入）
        if self._timestamp_validator is not None:
            try:
                valid = self._timestamp_validator.validate(symbol, quote)
                if not valid:
                    logger.warning(f"{symbol} 行情数据完整性校验未通过，丢弃此笔数据")
                    with self._stats_lock:
                        self._discard_count += 1
                    # 记录数据丢弃事件，便于运维监控数据丢失率
                    if self._behavioral_logger is not None:
                        try:
                            self._behavioral_logger.log_event(
                                event_type="data_integrity_discard",
                                details={
                                    "symbol": symbol,
                                    "timestamp": quote.get("timestamp"),
                                    "quote_preview": str(quote)[:200]
                                }
                            )
                        except Exception as log_err:
                            logger.warning(f"行为日志记录失败: {log_err}")
                    return
            except Exception as e:
                logger.error(f"TimestampValidator 校验异常: {e} #RECOVERY: 检查校验器逻辑")

        # 2. 通过协商总线广播行情更新
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_market_data'):
            try:
                self._negotiation_bus.publish_market_data(
                    symbol=symbol, quote=quote, timestamp=now
                )
            except Exception as e:
                logger.warning(f"协商总线行情广播失败: {e}")

    # ========== 公共接口 ==========
    def subscribe(self, symbols: List[str]) -> Dict[str, Any]:
        """
        订阅指定交易对的实时行情（含指数退避重试）

        Args:
            symbols: 内部标准交易对名称列表，如 ["BTCUSDT", "ETHUSDT"]

        Returns:
            标准响应字典
        """
        if not symbols:
            return {
                "status": "error",
                "reason": "订阅列表为空",
                "data": {},
                "warnings": ["empty_symbols_list"],
            }

        if len(symbols) > self.MAX_SUBSCRIBE_SYMBOLS:
            logger.error(f"订阅数量超限: {len(symbols)} > {self.MAX_SUBSCRIBE_SYMBOLS}")
            return {
                "status": "error",
                "reason": f"单次订阅最多 {self.MAX_SUBSCRIBE_SYMBOLS} 个交易对，收到 {len(symbols)} 个",
                "data": {},
                "warnings": ["subscribe_limit_exceeded"],
            }

        if self._aggregator is None:
            logger.error("MarketDataAggregator 未注入，无法订阅行情")
            return {
                "status": "error",
                "reason": "MarketDataAggregator 未注入",
                "data": {},
                "warnings": ["aggregator_unavailable"],
            }

        # 去重并转换交易对名称
        local_symbols = {}
        for sym in symbols:
            if not isinstance(sym, str) or not sym.strip():
                continue
            if sym not in local_symbols:
                local_symbols[sym] = self._symbol_mapper.to_local(sym) if self._symbol_mapper else sym

        # 首次批量订阅
        subscribed, failed = self._batch_subscribe(local_symbols)

        # 重试逻辑：指数退避
        remaining = failed
        for attempt in range(self.SUBSCRIBE_MAX_RETRIES):
            if not remaining:
                break
            retry_local = {}
            for sym in remaining:
                if sym in local_symbols:
                    retry_local[sym] = local_symbols[sym]
                else:
                    logger.warning(f"重试订阅跳过未知交易对: {sym}")
            if not retry_local:
                break
            retry_subs, retry_fails = self._batch_subscribe(retry_local)
            subscribed.extend(retry_subs)
            remaining = retry_fails

            if remaining and attempt < self.SUBSCRIBE_MAX_RETRIES - 1:
                wait_sec = self.SUBSCRIBE_RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.info(f"等待 {wait_sec:.1f}s 后进行第 {attempt + 2} 次重试...")
                time.sleep(wait_sec)

        failed = remaining
        if failed:
            logger.error(f"以下交易对最终订阅失败: {failed} #RECOVERY: 检查交易所连接与合约状态")

        return {
            "status": "ok",
            "reason": f"已订阅 {len(subscribed)} 个交易对，失败 {len(failed)} 个",
            "data": {"subscribed": subscribed, "failed": failed},
            "warnings": [f"最终订阅失败: {f}" for f in failed],
        }

    def get_latest_quote(self, symbol: str) -> Dict[str, Any]:
        """
        获取指定交易对的最新行情快照（含价格合理性校验）

        Args:
            symbol: 内部标准交易对名称

        Returns:
            标准响应字典
        """
        if self._aggregator is None:
            return {
                "status": "degraded",
                "reason": "MarketDataAggregator 未注入，无法获取行情",
                "data": {},
                "warnings": ["aggregator_unavailable"],
            }

        _start = time.time()
        try:
            quote = self._aggregator.get_latest(symbol)
        except Exception as e:
            logger.error(f"获取行情异常 {symbol}: {e} #RECOVERY: 检查聚合器缓存")
            return {
                "status": "error",
                "reason": f"获取行情异常: {str(e)}",
                "data": {},
                "warnings": [f"quote_error: {str(e)}"],
            }
        finally:
            _elapsed = time.time() - _start
            with self._stats_lock:
                self._aggregator_latencies.append(_elapsed)
            if _elapsed > 1.0:
                logger.warning(f"get_latest 耗时过长: {_elapsed:.2f}s，建议检查聚合器性能")

        if not quote or not isinstance(quote, dict):
            return {
                "status": "error",
                "reason": f"交易对 {symbol} 无有效行情数据",
                "data": {},
                "warnings": ["empty_quote"],
            }

        # ---------- 价格合理性校验 ----------
        bid = quote.get("bid", 0)
        ask = quote.get("ask", 0)

        if not (isinstance(bid, (int, float)) and isinstance(ask, (int, float))):
            logger.error(f"{symbol} 行情价格字段类型异常 bid={type(bid)} ask={type(ask)}")
            return {
                "status": "error",
                "reason": f"{symbol} 行情价格字段类型异常",
                "data": {},
                "warnings": ["invalid_price_format"],
            }

        if bid <= 0 or ask <= 0:
            logger.error(f"{symbol} 行情价格非正数 bid={bid} ask={ask}")
            return {
                "status": "error",
                "reason": f"{symbol} 行情价格非正数",
                "data": {},
                "warnings": ["non_positive_price"],
            }

        if bid > ask:
            logger.error(f"{symbol} 行情出现倒挂 bid={bid} > ask={ask}")
            return {
                "status": "error",
                "reason": f"{symbol} 行情出现倒挂(bid={bid} > ask={ask})",
                "data": {},
                "warnings": ["crossed_market"],
            }

        # ---------- 行情新鲜度检查 ----------
        now = time.time()
        ts = quote.get("timestamp", 0)
        age = now - ts if ts else 9999.0

        warn_sec = self.DEFAULT_MAX_QUOTE_AGE_SEC * self.STALE_QUOTE_WARNING_RATIO
        if age > warn_sec:
            logger.warning(f"{symbol} 行情延迟预警 (age={age:.2f}s > 预警线={warn_sec:.2f}s)")

        if age > self.DEFAULT_MAX_QUOTE_AGE_SEC:
            return {
                "status": "degraded",
                "reason": f"行情数据过期 (age={age:.1f}s > {self.DEFAULT_MAX_QUOTE_AGE_SEC}s)，已清除价格信息",
                "data": {
                    "symbol": symbol,
                    "timestamp": ts,
                    "age_seconds": round(age, 1),
                    "original_bid": None,
                    "original_ask": None,
                },
                "warnings": ["stale_quote", "price_data_cleared_for_safety"],
            }

        return {
            "status": "ok",
            "reason": f"返回 {symbol} 最新行情",
            "data": quote,
            "warnings": [],
        }

    def get_historical_klines(
        self, symbol: str, interval: str = "1m", limit: int = 100
    ) -> Dict[str, Any]:
        """
        获取历史K线数据

        Args:
            symbol: 内部标准交易对名称
            interval: K线周期
            limit: 获取根数

        Returns:
            标准响应字典
        """
        if limit <= 0 or limit > 1000:
            logger.warning(f"无效 limit: {limit}，使用默认值 {self.DEFAULT_KLINE_LIMIT}")
            limit = self.DEFAULT_KLINE_LIMIT

        if self._aggregator is None:
            return {
                "status": "degraded",
                "reason": "MarketDataAggregator 未注入",
                "data": {"klines": [], "symbol": symbol, "interval": interval},
                "warnings": ["aggregator_unavailable"],
            }

        try:
            return self._aggregator.fetch_klines(symbol, interval, limit)
        except ConnectionError as e:
            logger.error(f"K线网络异常 {symbol}: {e} #RECOVERY: 检查交易所 REST API 连通性")
        except TimeoutError as e:
            logger.error(f"K线超时 {symbol}: {e} #RECOVERY: 检查交易所响应时间")
        except ValueError as e:
            logger.error(f"K线数据格式异常 {symbol}: {e} #RECOVERY: 检查聚合器输出")
        except Exception as e:
            logger.error(f"K线未知异常 {symbol}: {e} #RECOVERY: 查看完整堆栈")

        return {
            "status": "error",
            "reason": f"获取K线失败: {symbol}",
            "data": {},
            "warnings": [f"kline_error: {symbol}"],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            agg_available = self._aggregator is not None
            agg_health = (
                self._aggregator.health_check()
                if agg_available and hasattr(self._aggregator, 'health_check')
                else {}
            )

            if not agg_available:
                return {
                    "status": "degraded",
                    "reason": "MarketDataAggregator 不可用，行情功能完全降级",
                    "data": {"aggregator_available": False, "aggregator_health": {}},
                    "warnings": ["core_dependency_missing"],
                }

            # 收集可观测性统计
            with self._stats_lock:
                discard_count = self._discard_count
                recv_ts = list(self._receive_timestamps)
                latencies = list(self._aggregator_latencies)

            # 行情断流检测
            warnings = []
            now = time.time()
            last_recv = 0.0
            if hasattr(self._aggregator, 'get_last_receive_time'):
                last_recv = self._aggregator.get_last_receive_time(symbol=None) or 0.0
            if last_recv and (now - last_recv) > self.STREAM_TIMEOUT_SEC:
                warnings.append(f"行情断流预警: 距上次收包已超过 {self.STREAM_TIMEOUT_SEC} 秒")

            # 行情接收频率（最近 FREQUENCY_WINDOW_SEC 秒）
            recent_count = sum(1 for ts in recv_ts if now - ts <= self.FREQUENCY_WINDOW_SEC)
            frequency = recent_count / self.FREQUENCY_WINDOW_SEC if self.FREQUENCY_WINDOW_SEC > 0 else 0

            # 聚合器响应延迟统计
            agg_p50 = 0.0
            agg_p95 = 0.0
            if latencies:
                sorted_lat = sorted(latencies)
                agg_p50 = sorted_lat[int(len(sorted_lat) * 0.5)]
                agg_p95 = sorted_lat[int(len(sorted_lat) * 0.95)]

            return {
                "status": "ok",
                "reason": f"DataFeed 正常，聚合器可用",
                "data": {
                    "aggregator_available": True,
                    "aggregator_health": agg_health,
                    "last_receive_time": last_recv if last_recv else None,
                    "discard_count": discard_count,
                    "receive_frequency_hz": round(frequency, 2),
                    "aggregator_latency_p50_s": round(agg_p50, 4),
                    "aggregator_latency_p95_s": round(agg_p95, 4),
                    "dependencies": {
                        "timestamp_validator": self._timestamp_validator is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                        "symbol_mapper": self._symbol_mapper is not None,
                    },
                },
                "warnings": warnings,
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查依赖注入状态")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _batch_subscribe(self, local_map: Dict[str, str]) -> Tuple[List[str], List[str]]:
        """
        执行批量订阅

        Args:
            local_map: {内部标准名: 交易所本地名}

        Returns:
            (subscribed: List[str], failed: List[str])
        """
        if not local_map:
            return [], []
        try:
            result = self._aggregator.subscribe(list(local_map.keys()))
            if not isinstance(result, dict):
                logger.error(f"聚合器返回异常类型: {type(result).__name__}")
                return [], list(local_map.keys())
            data = result.get("data", {})
            if not isinstance(data, dict):
                logger.error(f"聚合器返回的 data 字段类型异常: {type(data).__name__}")
                return [], list(local_map.keys())
            subscribed = data.get("subscribed", list(local_map.keys()))
            failed = data.get("failed", [])
            return subscribed, failed
        except Exception as e:
            logger.error(f"批量订阅异常: {e}")
            return [], list(local_map.keys())
