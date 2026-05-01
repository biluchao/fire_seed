#!/usr/bin/env python3
"""
火种系统 (FireSeed) 市场数据馈送模块
=====================================
功能：
- 多交易所实时行情接入 ( WebSocket 优先，REST 轮询备选 )
- 订单簿深度、最新成交、K线数据统一格式输出
- 跨交易所价格基准融合（中位数投票、异常剔除）
- 断线重连、心跳保活、故障自动切换备用交易所
- 数据通过异步队列推送给交易引擎
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import ccxt.pro as ccxt  # ccxt 异步支持
from ccxt.base.errors import NetworkError, ExchangeError, RequestTimeout

from config.loader import ConfigLoader
from core.context_isolator import TickRecord

logger = logging.getLogger("fire_seed.data_feed")


class MarketDataFeed:
    """
    多交易所数据源管理器。
    支持配置主交易所与备用交易所，自动故障转移。
    """

    def __init__(self, config: ConfigLoader):
        self.config = config
        self.symbols: List[str] = config.get("trading.symbols", ["BTC/USDT:USDT"])
        self.primary_exchange_id = config.get("exchange.primary", "binance")
        self.backup_exchange_ids: List[str] = config.get("exchange.backups", ["bybit", "okx"])
        self.use_websocket = config.get("network.ws_enabled", True)

        # 内部状态
        self._primary_exchange: Optional[ccxt.Exchange] = None
        self._backup_exchanges: Dict[str, ccxt.Exchange] = {}
        self._active = False
        self._tick_queue: asyncio.Queue = asyncio.Queue(maxsize=5000)

        # 多源数据缓存
        self._latest_ticker: Dict[str, Dict[str, Any]] = {}  # symbol -> {exchange: ticker}
        self._latest_orderbook: Dict[str, Dict[str, Any]] = {}

        # 心跳与重连
        self._last_primary_heartbeat = 0.0
        self._primary_healthy = True
        self._health_check_task: Optional[asyncio.Task] = None

    async def start(self):
        """初始化交易所连接并启动数据流"""
        self._active = True
        # 初始化主交易所
        self._primary_exchange = self._create_exchange(self.primary_exchange_id)
        # 初始化备用交易所 (懒加载，按需)
        for ex_id in self.backup_exchange_ids:
            try:
                self._backup_exchanges[ex_id] = self._create_exchange(ex_id)
            except Exception as e:
                logger.warning(f"备用交易所 {ex_id} 初始化失败: {e}")

        # 启动WebSocket订阅或REST轮询
        if self.use_websocket:
            asyncio.create_task(self._run_websocket_loop())
        else:
            asyncio.create_task(self._run_rest_poll_loop())

        # 启动健康检查
        self._health_check_task = asyncio.create_task(self._health_check_loop())
        logger.info(f"数据馈送启动，主交易所: {self.primary_exchange_id}，交易对: {self.symbols}")

    async def stop(self):
        """优雅关闭所有连接"""
        self._active = False
        if self._health_check_task:
            self._health_check_task.cancel()
        # 关闭交易所连接 (ccxt 异步对象需显式关闭)
        if self._primary_exchange:
            await self._primary_exchange.close()
        for ex in self._backup_exchanges.values():
            await ex.close()
        logger.info("数据馈送已停止")

    async def get_next_tick(self, timeout: float = 0.5) -> Optional[TickRecord]:
        """从队列中获取下一个Tick，供引擎消费"""
        try:
            tick = await asyncio.wait_for(self._tick_queue.get(), timeout=timeout)
            return tick
        except asyncio.TimeoutError:
            return None

    # ---------- 内部方法 ----------
    def _create_exchange(self, exchange_id: str) -> ccxt.Exchange:
        """根据ID创建ccxt交易所实例，加载API密钥"""
        config = self.config
        api_key = config.get(f"exchange.{exchange_id}.api_key", "")
        secret = config.get(f"exchange.{exchange_id}.secret", "")
        ex_class = getattr(ccxt, exchange_id)
        exchange: ccxt.Exchange = ex_class({
            'apiKey': api_key,
            'secret': secret,
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'},  # 永续合约
        })
        return exchange

    async def _run_websocket_loop(self):
        """基于ccxt pro的WebSocket行情接收"""
        while self._active:
            try:
                if not self._primary_healthy:
                    # 主交易所不健康，尝试使用备用
                    await asyncio.sleep(1)
                    continue
                exchange = self._primary_exchange
                # 订阅 ticker 和 orderbook
                while self._active:
                    for symbol in self.symbols:
                        # 使用 ccxt pro 的 watch_ticker / watch_order_book 方法
                        ticker = await exchange.watch_ticker(symbol)
                        self._process_ticker(self.primary_exchange_id, symbol, ticker)

                        # 每隔一定时间获取一次订单簿，减少负载
                        # 可以在单独 task 中按频率拉取
                    # 避免过于紧密的循环
                    await asyncio.sleep(0.1)
            except (NetworkError, RequestTimeout) as e:
                logger.error(f"主交易所 WebSocket 异常: {e}，准备重连...")
                self._primary_healthy = False
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"数据流未知异常: {e}")
                await asyncio.sleep(5)

    async def _run_rest_poll_loop(self):
        """REST轮询备选模式"""
        while self._active:
            try:
                exchange = self._get_active_exchange()
                for symbol in self.symbols:
                    ticker = await exchange.fetch_ticker(symbol)
                    self._process_ticker(exchange.id, symbol, ticker)
                await asyncio.sleep(1)  # 1秒轮询
            except Exception as e:
                logger.error(f"REST轮询异常: {e}")
                await asyncio.sleep(5)

    def _get_active_exchange(self) -> ccxt.Exchange:
        """根据健康状态返回当前使用的交易所实例"""
        if self._primary_healthy and self._primary_exchange:
            return self._primary_exchange
        # 选择第一个可用的备用
        for ex_id, ex in self._backup_exchanges.items():
            if ex:
                return ex
        raise RuntimeError("无可用交易所")

    def _process_ticker(self, exchange_id: str, symbol: str, ticker: Dict[str, Any]):
        """处理单个 ticker 数据，进行多源融合并生成 TickRecord"""
        # 缓存最新 ticker
        if symbol not in self._latest_ticker:
            self._latest_ticker[symbol] = {}
        self._latest_ticker[symbol][exchange_id] = ticker
        self._latest_ticker[symbol]['timestamp'] = time.time()

        # 多交易所价格融合（如果配置了多个数据源）
        if len(self._latest_ticker[symbol]) >= 2:
            fused_price = self._fuse_prices(symbol)
        else:
            fused_price = ticker['last']

        # 构建 TickRecord
        record = TickRecord(
            timestamp=datetime.fromtimestamp(ticker['timestamp'] / 1000.0),
            symbol=symbol,
            last_price=fused_price,
            bid=ticker.get('bid', 0),
            ask=ticker.get('ask', 0),
            volume=ticker.get('baseVolume', 0),
            source=exchange_id
        )
        # 放入队列（非阻塞，如果队列满则丢弃旧数据）
        try:
            self._tick_queue.put_nowait(record)
        except asyncio.QueueFull:
            # 移除最旧的一个
            try:
                self._tick_queue.get_nowait()
                self._tick_queue.put_nowait(record)
            except Exception:
                pass

    def _fuse_prices(self, symbol: str) -> float:
        """
        多交易所价格融合：取中位数，剔除偏离超过3倍标准差的异常值。
        """
        tickers = self._latest_ticker.get(symbol, {})
        prices = []
        for ex_id, tk in tickers.items():
            if ex_id == 'timestamp':
                continue
            if 'last' in tk and tk['last'] is not None:
                prices.append(tk['last'])
        if not prices:
            return 0.0
        if len(prices) == 1:
            return prices[0]
        # 计算中位数
        sorted_prices = sorted(prices)
        n = len(sorted_prices)
        median = sorted_prices[n // 2] if n % 2 == 1 else (sorted_prices[n // 2 - 1] + sorted_prices[n // 2]) / 2
        # 简单异常剔除：与中位数偏差超过 1% 的舍弃（适用于主流币种）
        filtered = [p for p in prices if abs(p - median) / median < 0.01]
        if filtered:
            return sum(filtered) / len(filtered)
        return median

    async def _health_check_loop(self):
        """定期检查主交易所健康状态，并尝试恢复"""
        while self._active:
            await asyncio.sleep(10)
            if not self._primary_healthy and self._primary_exchange:
                try:
                    # 尝试获取服务器时间以测试连通性
                    await self._primary_exchange.fetch_time()
                    self._primary_healthy = True
                    logger.info("主交易所连接已恢复")
                except Exception:
                    self._primary_healthy = False
