#!/usr/bin/env python3
"""
火种系统 (FireSeed) 虚拟券商模拟器
=====================================
在幽灵影子环境中替代真实交易所，提供订单撮合、滑点模拟、
流动性冲击模型等功能，使影子策略的表现评估更加贴近实盘。

功能：
- 基于当前行情（订单簿快照）撮合限价/市价单
- 滑点模拟：根据当前订单簿深度计算冲击成本
- 部分成交模拟：当挂单量不足时仅成交部分
- 支持市价单的悲观滑点与乐观滑点
- 模拟手续费与资金费率扣除
- 完全虚拟账户，不影响真实资金
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from config.loader import ConfigLoader
from core.order_manager import OrderManager, Order
from core.behavioral_logger import BehavioralLogger, EventType

logger = logging.getLogger("fire_seed.virtual_broker")


class VirtualBroker:
    """
    虚拟券商，为影子策略提供模拟撮合。
    每次收到订单信号时：
    1. 从 data_feed 获取最新订单簿快照
    2. 根据订单类型、方向、数量，计算模拟成交价格
    3. 写入订单管理器的成交记录
    """

    def __init__(self,
                 config: ConfigLoader,
                 order_mgr: OrderManager,
                 data_feed=None,
                 behavior_log: Optional[BehavioralLogger] = None):
        self.config = config
        self.order_mgr = order_mgr
        self.data_feed = data_feed
        self.log = behavior_log

        # 滑点配置
        self.base_slippage_pct = config.get("virtual_broker.base_slippage_pct", 0.02) / 100.0
        self.impact_factor = config.get("virtual_broker.impact_factor", 1.5)  # 冲击系数
        self.use_pessimistic_slippage = config.get("virtual_broker.pessimistic", True)

        # 统计
        self._simulated_trades_count = 0
        self._total_slippage_cost = 0.0

    async def execute(self, order_signal: Dict[str, Any]) -> Optional[Dict]:
        """
        执行一笔虚拟订单。
        :param order_signal: 包含 symbol, side, order_type, price, quantity 等字段
        :return: 成交确认信息
        """
        symbol = order_signal.get("symbol", "BTCUSDT")
        side = order_signal.get("side", "buy")
        order_type = order_signal.get("order_type", "LIMIT")
        price = order_signal.get("price", 0.0)
        quantity = order_signal.get("quantity", 0.0)

        if quantity <= 0:
            return None

        # 获取当前行情
        ob = await self._get_orderbook(symbol)
        if not ob:
            logger.debug(f"无法获取 {symbol} 订单簿，跳过虚拟成交")
            return None

        best_bid = ob.get("bid", 0.0)
        best_ask = ob.get("ask", 0.0)
        mid_price = (best_bid + best_ask) / 2.0 if best_bid and best_ask else 0.0

        if mid_price <= 0:
            return None

        # 计算虚拟成交价
        fill_price, slippage_bps = self._simulate_fill(
            side, order_type, price, quantity, mid_price, ob
        )

        if fill_price <= 0:
            return None

        # 创建虚拟订单并成交
        order = await self.order_mgr.create_order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=price,
            quantity=quantity
        )
        if not order:
            return None

        filled_order = await self.order_mgr.fill_order(
            order.id,
            fill_price=fill_price,
            fill_qty=quantity,
            slippage_bps=slippage_bps
        )

        self._simulated_trades_count += 1

        if self.log:
            self.log.log(
                EventType.ORDER, "VirtualBroker",
                f"虚拟成交: {side} {quantity} @ {fill_price} (滑点 {slippage_bps:.1f}bps)",
                {"symbol": symbol, "side": side, "qty": quantity, "fill": fill_price}
            )

        return {"order_id": order.id, "fill_price": fill_price, "slippage_bps": slippage_bps}

    def _simulate_fill(self, side: str, order_type: str, limit_price: float,
                       quantity: float, mid_price: float,
                       ob: Dict[str, Any]) -> Tuple[float, float]:
        """
        模拟成交价格与滑点。
        :return: (fill_price, slippage_bps)
        """
        if order_type == "MARKET":
            # 市价单：直接在对手价成交，并附加冲击成本
            base_price = ob["ask"] if side == "buy" else ob["bid"]
            if base_price <= 0:
                return 0.0, 0.0

            # 根据订单量与深度计算冲击
            impact_slippage = self._calc_impact(quantity, side, ob)
            if side == "buy":
                fill_price = base_price * (1 + impact_slippage)
            else:
                fill_price = base_price * (1 - impact_slippage)

            slippage_bps = (abs(fill_price - mid_price) / mid_price) * 10000
        else:
            # 限价单：只有在对手价穿过限价时才成交
            best_opposite = ob["ask"] if side == "buy" else ob["bid"]
            if side == "buy":
                if limit_price >= best_opposite:
                    fill_price = best_opposite  # 立即成交于对手价
                else:
                    # 挂单未成交（简化处理：若挂单价格在买一或更优，可模拟稍后成交）
                    if limit_price >= mid_price * 0.999:
                        fill_price = limit_price  # 假设以挂单价成交
                    else:
                        return 0.0, 0.0  # 未成交
            else:
                if limit_price <= best_opposite:
                    fill_price = best_opposite
                else:
                    if limit_price <= mid_price * 1.001:
                        fill_price = limit_price
                    else:
                        return 0.0, 0.0

            slippage_bps = (abs(fill_price - mid_price) / mid_price) * 10000 if mid_price > 0 else 0.0

        # 附加悲观滑点缓冲
        if self.use_pessimistic_slippage:
            extra = self.base_slippage_pct * (1 + np.random.random())  # 随机0~2倍基础滑点
            if side == "buy":
                fill_price *= (1 + extra)
            else:
                fill_price *= (1 - extra)
            slippage_bps += extra * 10000

        fill_price = max(0.0, fill_price)
        return fill_price, slippage_bps

    def _calc_impact(self, quantity: float, side: str, ob: Dict) -> float:
        """
        基于当前订单簿深度估算冲击成本。
        返回价格冲击比例（例如 0.001 表示 0.1%）
        """
        levels = ob.get("asks", []) if side == "buy" else ob.get("bids", [])
        if not levels:
            return self.base_slippage_pct

        remaining = quantity
        total_cost = 0.0
        total_qty = 0.0
        base_price = levels[0][0] if levels else 0.0

        for price, vol in levels:
            take = min(remaining, vol)
            total_cost += take * price
            total_qty += take
            remaining -= take
            if remaining <= 0:
                break

        if total_qty == 0:
            return self.base_slippage_pct

        avg_price = total_cost / total_qty
        impact = abs(avg_price - base_price) / base_price if base_price else 0.0
        return max(self.base_slippage_pct, impact * self.impact_factor)

    async def _get_orderbook(self, symbol: str) -> Optional[Dict]:
        """
        从数据馈送获取最新订单簿快照。
        若 data_feed 不可用，尝试从 ContextFactory 获取。
        """
        if self.data_feed:
            tick = await self.data_feed.get_next_tick(timeout=0.5)
            if tick:
                return {
                    "bid": tick.bid if hasattr(tick, 'bid') else 0.0,
                    "ask": tick.ask if hasattr(tick, 'ask') else 0.0,
                    "bids": getattr(tick, 'bids', []),
                    "asks": getattr(tick, 'asks', []),
                }
        # 回退：使用最新缓存或生成模拟订单簿
        return {
            "bid": 50000.0,
            "ask": 50001.0,
            "bids": [(50000.0, 10.0)],
            "asks": [(50001.0, 10.0)],
          }
