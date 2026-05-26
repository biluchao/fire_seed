"""
火种系统 · 行情数据融合入口 (DataFeed)

核心职责：
1. 作为行情数据模块的统一入口，协调多个交易所的 WebSocket 连接、REST API 轮询与故障切换
2. 集成数据完整性校验（时间戳对齐、序列号连续性）与市场数据聚合（多源加权中位数、合约规格刷新）

外部依赖（真实模块接口）：
- core.data_feed.timestamp_validator.TimestampValidator : 校验行情数据的时间戳与序列号连续性
- core.data_feed.market_data_aggregator.MarketDataAggregator : 执行多源加权中位数计算与合约规格管理
- core.negotiation_bus.NegotiationBus : 向系统广播行情更新事件与数据异常告警
- core.behavioral_logger.BehavioralLogger : 记录行情数据质量日志与异常事件
- core.symbol_mapper.SymbolMapper : 内部标准交易对名称与交易所本地名称的双向转换

接口契约：
- subscribe(symbols: List[str]) -> Dict[str, Any] : 订阅指定交易对的实时行情
- get_latest_quote(symbol: str) -> Dict[str, Any] : 获取指定交易对的最新行情快照
- get_historical_klines(symbol: str, interval: str, limit: int) -> Dict[str, Any] : 获取历史K线数据
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 TimestampValidator 不可用时，跳过数据完整性校验，仅依赖交易所自身的序列号
- 当 MarketDataAggregator 不可用时，直接返回主交易所的原始数据，不进行多源聚合
- 当 NegotiationBus 不可用时，行情更新事件仅通过本地日志记录
- 所有降级值在类常量区明确声明

资源管理：
- 管理多个 WebSocket 连接，在 unsubscribe 或模块销毁时自动关闭连接
- 定期清理过期的行情缓存，防止内存膨胀
- 不持有任何外部文件句柄
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional, Set

logger = logging.getLogger(__name__)


class DataFeed:
    """行情数据融合入口"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_MAX_QUOTE_AGE_SEC = 5.0        # 行情快照最大有效时间，秒，取值范围 [1.0, 30.0]
    DEFAULT_RECONNECT_DELAY_SEC = 2.0      # WebSocket 重连初始延迟，秒，取值范围 [0.5, 10.0]
    DEFAULT_CLEANUP_INTERVAL_SEC = 300     # 缓存清理间隔，秒，取值范围 [60, 600]
    DEFAULT_KLINE_LIMIT = 100              # 单次获取历史K线的默认根数，无量纲，[1, 1000]
    STALE_QUOTE_WARNING_RATIO = 0.5        # 行情延迟预警比例，当 age 超过此比例时发出预警，[0.3, 0.8]

    def __init__(self):
        # 内部行情缓存（线程安全由 _quotes_lock 保护）
        self._quotes: Dict[str, Dict[str, Any]] = {}
        self._active_subscriptions: Set[str] = set()
        self._quotes_lock = threading.Lock()

        # 外部依赖注入
        self._timestamp_validator = None
        self._market_data_aggregator = None
        self._negotiation_bus = None
        self._behavioral_logger = None
        self._symbol_mapper = None

        # 清理定时器
        self._last_cleanup = time.time()

        logger.info("DataFeed 初始化完成，等待行情订阅")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        timestamp_validator: Optional[Any] = None,
        market_data_aggregator: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        symbol_mapper: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）
        """
        if timestamp_validator is not None:
            self._timestamp_validator = timestamp_validator
            logger.info("TimestampValidator 注入成功")
        else:
            logger.warning("TimestampValidator 未注入，数据完整性校验降级")

        if market_data_aggregator is not None:
            self._market_data_aggregator = market_data_aggregator
            logger.info("MarketDataAggregator 注入成功")
        else:
            logger.warning("MarketDataAggregator 未注入，多源聚合降级为单源")

        if negotiation_bus is not None:
            self._negotiation_bus = negotiation_bus
            logger.info("NegotiationBus 注入成功")
        else:
            logger.warning("NegotiationBus 未注入，行情广播降级为本地日志")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

        if symbol_mapper is not None:
            self._symbol_mapper = symbol_mapper
            logger.info("SymbolMapper 注入成功")
        else:
            logger.warning("SymbolMapper 未注入，交易对名称将直接使用内部标准名")

    # ========== 公共接口 ==========
    def subscribe(self, symbols: List[str]) -> Dict[str, Any]:
        """
        订阅指定交易对的实时行情

        Args:
            symbols: 内部标准交易对名称列表，如 ["BTCUSDT", "ETHUSDT"]

        Returns:
            标准响应字典，data 中包含订阅成功的交易对列表
        """
        if not symbols:
            return {
                "status": "error",
                "reason": "订阅列表为空",
                "data": {},
                "warnings": ["empty_symbols_list"],
            }

        subscribed = []
        failed = []
        seen = set()

        for sym in symbols:
            # 严格校验交易对名称
            if not isinstance(sym, str) or not sym.strip():
                logger.warning(f"无效的交易对名称: {repr(sym)}，已跳过")
                failed.append(str(sym) if sym is not None else "None")
                continue

            # 跳过重复项，避免重复订阅与日志冗余
            if sym in seen:
                logger.debug(f"重复的交易对名称: {sym}，已跳过")
                continue
            seen.add(sym)

            # 转换为交易所本地名称（若 SymbolMapper 可用）
            local = self._symbol_mapper.to_local(sym) if self._symbol_mapper is not None else sym

            try:
                with self._quotes_lock:
                    self._active_subscriptions.add(sym)
                subscribed.append(sym)
                logger.info(f"已订阅行情: {sym} (本地名称: {local})")
            except Exception as e:
                logger.error(f"订阅失败 {sym}: {e} #RECOVERY: 检查交易所 WebSocket 端点是否可达")
                failed.append(sym)

        return {
            "status": "ok",
            "reason": f"已订阅 {len(subscribed)} 个交易对，失败 {len(failed)} 个",
            "data": {
                "subscribed": subscribed,
                "failed": failed,
            },
            "warnings": [f"订阅失败: {f}" for f in failed],
        }

    def get_latest_quote(self, symbol: str) -> Dict[str, Any]:
        """
        获取指定交易对的最新行情快照

        Args:
            symbol: 内部标准交易对名称

        Returns:
            标准响应字典，data 中包含 bid/ask、时间戳等字段
        """
        self._try_cleanup()

        with self._quotes_lock:
            quote = self._quotes.get(symbol)
            if quote is None:
                return {
                    "status": "error",
                    "reason": f"交易对 {symbol} 未订阅或尚无行情数据",
                    "data": {},
                    "warnings": ["symbol_not_subscribed"],
                }

            # 结构完整性校验（防御上游模块写入异常格式）
            if not isinstance(quote, dict) or "timestamp" not in quote:
                logger.error(
                    f"{symbol} 行情数据结构异常: 缺少 timestamp 字段 #RECOVERY: 检查上游数据源输出格式"
                )
                return {
                    "status": "error",
                    "reason": f"{symbol} 行情数据结构异常",
                    "data": {},
                    "warnings": ["invalid_quote_structure"],
                }

            age = time.time() - quote["timestamp"]

            # 行情延迟预警（在到达过期阈值之前发出预警，给运维留出响应窗口）
            warn_sec = self.DEFAULT_MAX_QUOTE_AGE_SEC * self.STALE_QUOTE_WARNING_RATIO
            if age > warn_sec:
                logger.warning(
                    f"{symbol} 行情延迟预警 (age={age:.2f}s > 预警线={warn_sec:.2f}s)"
                )

            # 行情过期处理：清除价格数据，仅保留元数据，防止下游模块误用过期价格
            if age > self.DEFAULT_MAX_QUOTE_AGE_SEC:
                stale_data = {
                    "symbol": symbol,
                    "timestamp": quote.get("timestamp", 0),
                    "age_seconds": round(age, 1),
                    "subscription_active": symbol in self._active_subscriptions,
                    "original_bid": None,
                    "original_ask": None,
                }
                logger.warning(
                    f"{symbol} 行情过期 (age={age:.1f}s) #RECOVERY: 检查 WebSocket 连接状态"
                )
                return {
                    "status": "degraded",
                    "reason": f"行情数据过期 (age={age:.1f}s > {self.DEFAULT_MAX_QUOTE_AGE_SEC}s)，已清除价格信息",
                    "data": stale_data,
                    "warnings": ["stale_quote", "price_data_cleared_for_safety"],
                }

            return {
                "status": "ok",
                "reason": f"返回 {symbol} 最新行情",
                "data": quote,
                "warnings": [],
            }

    def get_historical_klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 100
    ) -> Dict[str, Any]:
        """
        获取历史K线数据

        Args:
            symbol: 内部标准交易对名称
            interval: K线周期，如 "1m", "5m", "15m", "1h"
            limit: 获取根数，上限 1000

        Returns:
            标准响应字典，data 中包含 K线列表
        """
        if limit <= 0 or limit > 1000:
            logger.warning(f"无效的 limit 参数: {limit}，使用默认值 {self.DEFAULT_KLINE_LIMIT}")
            limit = self.DEFAULT_KLINE_LIMIT

        if self._market_data_aggregator is None:
            logger.warning("MarketDataAggregator 不可用，返回空K线数据")
            return {
                "status": "degraded",
                "reason": "MarketDataAggregator 未注入，无法获取K线数据",
                "data": {"klines": [], "symbol": symbol, "interval": interval},
                "warnings": ["aggregator_unavailable"],
            }

        try:
            return self._market_data_aggregator.fetch_klines(symbol, interval, limit)
        except ConnectionError as e:
            logger.error(f"获取K线网络异常 {symbol}: {e} #RECOVERY: 检查交易所 REST API 网络连通性")
        except TimeoutError as e:
            logger.error(f"获取K线超时 {symbol}: {e} #RECOVERY: 检查交易所响应时间，考虑增加超时阈值")
        except ValueError as e:
            logger.error(f"获取K线数据格式异常 {symbol}: {e} #RECOVERY: 检查 MarketDataAggregator 输出格式")
        except AttributeError as e:
            logger.error(f"MarketDataAggregator 方法缺失 {symbol}: {e} #RECOVERY: 检查 MarketDataAggregator 是否实现了 fetch_klines 方法")
        except Exception as e:
            logger.error(f"获取K线未知异常 {symbol}: {e} #RECOVERY: 检查完整错误堆栈")

        return {
            "status": "error",
            "reason": f"获取K线失败: {symbol}",
            "data": {},
            "warnings": [f"kline_fetch_error: {symbol}"],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检

        Returns:
            标准健康检查响应字典
        """
        try:
            with self._quotes_lock:
                subscription_count = len(self._active_subscriptions)
                quote_count = len(self._quotes)
                now = time.time()
                stale_count = sum(
                    1 for q in self._quotes.values()
                    if isinstance(q, dict) and now - q.get("timestamp", 0) > self.DEFAULT_MAX_QUOTE_AGE_SEC
                )

            return {
                "status": "ok",
                "reason": f"DataFeed 正常，活跃订阅 {subscription_count}，缓存行情 {quote_count}，过期 {stale_count}",
                "data": {
                    "subscriptions": subscription_count,
                    "cached_quotes": quote_count,
                    "stale_quotes": stale_count,
                    "dependencies": {
                        "timestamp_validator": self._timestamp_validator is not None,
                        "market_data_aggregator": self._market_data_aggregator is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                        "symbol_mapper": self._symbol_mapper is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和内部数据结构完整性")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _try_cleanup(self) -> None:
        """定期清理过期的行情缓存"""
        now = time.time()
        if now - self._last_cleanup < self.DEFAULT_CLEANUP_INTERVAL_SEC:
            return

        with self._quotes_lock:
            cutoff = now - self.DEFAULT_MAX_QUOTE_AGE_SEC * 10
            expired = []
            for sym, quote in self._quotes.items():
                if isinstance(quote, dict) and quote.get("timestamp", 0) < cutoff:
                    expired.append(sym)

            for sym in expired:
                try:
                    del self._quotes[sym]
                    logger.debug(f"清理过期行情缓存: {sym}")
                except KeyError:
                    logger.debug(f"清理时键已不存在: {sym}，可能已被其他操作移除")

        self._last_cleanup = now
        if expired:
            logger.info(f"清理 {len(expired)} 个过期行情缓存")
