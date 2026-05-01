#!/usr/bin/env python3
"""
火种系统 (FireSeed) 订单与仓位管理器
======================================
负责：
- 订单生命周期管理（创建、成交、取消、拒绝）
- 实时持仓计算（均价、浮动盈亏、保证金）
- 成交历史记录与统计
- 账户级资金管理（余额、已实现盈亏、资金费率）
- 为其他模块提供统一查询接口
"""

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from config.loader import ConfigLoader
from core.behavioral_logger import BehavioralLogger, EventType

# ---------- 数据结构 ----------
@dataclass
class Order:
    id: str
    symbol: str
    side: str                # buy / sell
    order_type: str          # LIMIT / MARKET / STOP / ...
    price: float
    quantity: float
    filled: float = 0.0
    status: str = "OPEN"
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None
    pnl: Optional[float] = None
    slippage_bps: Optional[float] = None

@dataclass
class Position:
    symbol: str
    side: str                # long / short / empty
    size: float = 0.0
    entry_price: float = 0.0
    mark_price: float = 0.0
    unrealized_pnl: float = 0.0
    liquidation_price: Optional[float] = None

@dataclass
class Account:
    equity: float = 0.0
    available: float = 0.0
    initial_deposit: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl_today: float = 0.0
    total_funding_paid: float = 0.0
    margin_rate: float = 0.0
    net_value_ratio: float = 0.0

# ---------- 管理器 ----------
class OrderManager:
    """统一订单与仓位管理，维护内存状态并提供持久化接口。"""

    def __init__(self, config: ConfigLoader, logger: Optional[BehavioralLogger] = None):
        self.config = config
        self._logger = logger

        # 内存存储
        self._orders: Dict[str, Order] = {}
        self._order_history: List[Order] = []          # 全部历史订单
        self._trades: List[Dict[str, Any]] = []        # 成交记录
        self._position: Position = Position(symbol="", side="empty")
        self._account: Account = Account()

        # 并发锁
        self._lock = asyncio.Lock()

        # 初始化默认账户资金（可根据配置设定）
        init_equity = config.get("account.initial_deposit", 100000.0)
        self._account.equity = init_equity
        self._account.available = init_equity
        self._account.initial_deposit = init_equity

    # ======================== 订单操作 ========================
    async def create_order(self, symbol: str, side: str, order_type: str,
                           price: float, quantity: float) -> Order:
        """创建新订单并返回订单对象。"""
        order_id = self._generate_order_id()
        order = Order(
            id=order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=price,
            quantity=quantity,
            status="OPEN"
        )
        async with self._lock:
            self._orders[order_id] = order

        if self._logger:
            self._logger.log(EventType.ORDER, "OrderManager",
                             f"创建订单: {order_id} {side} {quantity} @ {price}")
        return order

    async def fill_order(self, order_id: str, fill_price: float,
                         fill_qty: Optional[float] = None,
                         slippage_bps: float = 0.0) -> Order:
        """
        完全或部分成交订单。
        返回更新后的 Order 对象，若订单不存在则抛出 KeyError。
        """
        async with self._lock:
            order = self._orders[order_id]
            qty = fill_qty if fill_qty else order.quantity
            order.filled += qty
            if order.filled >= order.quantity:
                order.status = "FILLED"
            else:
                order.status = "PARTIALLY_FILLED"
            order.updated_at = datetime.now()
            order.slippage_bps = slippage_bps

            # 计算盈亏（简单模型：根据方向和成交价）
            if order.status == "FILLED":
                order.pnl = self._calc_order_pnl(order, fill_price)

            # 更新持仓
            self._update_position(order, fill_price)

            # 记录成交
            self._trades.append({
                "trade_id": f"T{len(self._trades)+1}",
                "order_id": order_id,
                "symbol": order.symbol,
                "side": order.side,
                "price": fill_price,
                "quantity": order.quantity,
                "pnl": order.pnl,
                "fee": 0.001 * fill_price * order.quantity,  # 简化手续费
                "timestamp": datetime.now()
            })

            # 成交后移除未活动订单
            if order.status == "FILLED":
                self._order_history.append(order)
                del self._orders[order_id]

            if self._logger:
                self._logger.log(EventType.ORDER, "OrderManager",
                                 f"订单成交: {order_id} @ {fill_price}, 状态={order.status}")
        return order

    async def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        async with self._lock:
            order = self._orders.pop(order_id, None)
            if not order:
                return False
            order.status = "CANCELLED"
            order.updated_at = datetime.now()
            self._order_history.append(order)
            if self._logger:
                self._logger.log(EventType.ORDER, "OrderManager",
                                 f"订单取消: {order_id}")
            return True

    # ======================== 持仓更新 ========================
    def _update_position(self, order: Order, fill_price: float):
        """根据成交订单更新持仓"""
        pos = self._position
        if pos.side == "empty":
            pos.symbol = order.symbol
            pos.side = "long" if order.side == "buy" else "short"
            pos.size = order.quantity
            pos.entry_price = fill_price
        elif pos.side == "long" and order.side == "buy":
            # 加仓
            total_cost = pos.size * pos.entry_price + order.quantity * fill_price
            pos.size += order.quantity
            pos.entry_price = total_cost / pos.size if pos.size else 0.0
        elif pos.side == "short" and order.side == "sell":
            total_cost = pos.size * pos.entry_price + order.quantity * fill_price
            pos.size += order.quantity
            pos.entry_price = total_cost / pos.size if pos.size else 0.0
        else:
            # 平仓或减仓
            pos.size -= order.quantity
            if pos.size <= 0:
                pos.side = "empty"
                pos.size = 0.0
                pos.entry_price = 0.0

    def _calc_order_pnl(self, order: Order, fill_price: float) -> float:
        """简单模型：计算平仓盈亏"""
        # 仅对平仓操作计算盈亏，开仓无盈亏
        pos = self._position
        if pos.side == "empty":
            return 0.0
        if pos.side == "long" and order.side == "sell":
            return (fill_price - pos.entry_price) * order.quantity
        elif pos.side == "short" and order.side == "buy":
            return (pos.entry_price - fill_price) * order.quantity
        return 0.0

    # ======================== 查询接口 ========================
    async def get_position_summary(self) -> Position:
        """获取当前持仓汇总"""
        return self._position

    async def get_account(self) -> Account:
        """获取账户信息（含权益、可用等）"""
        # 实时计算权益 = 初始权益 + 已实现盈亏 + 浮动盈亏
        acc = self._account
        pos = self._position
        if pos.side == "long":
            unrealized = (pos.mark_price - pos.entry_price) * pos.size if pos.mark_price else 0.0
        elif pos.side == "short":
            unrealized = (pos.entry_price - pos.mark_price) * pos.size if pos.mark_price else 0.0
        else:
            unrealized = 0.0
        acc.unrealized_pnl = unrealized
        acc.equity = acc.initial_deposit + acc.realized_pnl_today + unrealized
        acc.available = acc.equity * 0.8  # 简化为80%可用
        return acc

    async def get_recent_orders(self, limit: int = 12, symbol: Optional[str] = None) -> List[Order]:
        """获取最近订单记录"""
        async with self._lock:
            orders = [o for o in self._order_history
                      if (not symbol or o.symbol == symbol)]
            orders.sort(key=lambda x: x.created_at, reverse=True)
            return orders[:limit]

    async def get_active_orders(self) -> List[Order]:
        """获取当前活动订单（未成交的挂单）"""
        return list(self._orders.values())

    async def get_trade_history(self, days: int = 7, symbol: Optional[str] = None) -> List[Dict]:
        """获取成交记录"""
        cutoff = time.time() - days * 86400
        trades = [t for t in self._trades
                  if (not symbol or t['symbol'] == symbol)
                  and t['timestamp'].timestamp() >= cutoff]
        return trades

    async def get_historical_stats(self) -> Dict[str, Any]:
        """获取历史统计数据（用于仪表盘）"""
        async with self._lock:
            total_pnl = sum(o.pnl for o in self._order_history if o.pnl is not None)
            total_volume = sum(o.quantity for o in self._order_history if o.status == "FILLED")
            win_orders = [o for o in self._order_history
                         if o.pnl is not None and o.pnl > 0]
            all_closed = [o for o in self._order_history if o.pnl is not None]
            win_rate = len(win_orders) / len(all_closed) if all_closed else 0.0
            return {
                "total_pnl": total_pnl,
                "total_volume": total_volume,
                "win_rate": win_rate,
            }

    async def get_pnl_for_period(self, days: int = 30) -> float:
        """最近N天的已实现盈亏"""
        cutoff = datetime.now().timestamp() - days * 86400
        pnl = 0.0
        for order in self._order_history:
            if order.pnl is not None and order.updated_at and order.updated_at.timestamp() >= cutoff:
                pnl += order.pnl
        return pnl

    async def get_daily_trading_stats(self) -> Dict[str, Any]:
        """获取今日交易统计（供 risk 等模块使用）"""
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_orders = [o for o in self._order_history
                        if o.created_at >= today_start]
        realized = sum(o.pnl for o in today_orders if o.pnl is not None)
        win_orders = [o for o in today_orders if o.pnl is not None and o.pnl > 0]
        closed = [o for o in today_orders if o.pnl is not None]
        win_rate = len(win_orders) / len(closed) if closed else 0.0
        return {
            "count": len(today_orders),
            "realized_pnl": realized,
            "win_rate": win_rate,
            "best_trade": max((o.pnl for o in closed if o.pnl), default=0.0),
            "worst_trade": min((o.pnl for o in closed if o.pnl), default=0.0),
        }

    # ======================== 辅助 ========================
    @staticmethod
    def _generate_order_id() -> str:
        import uuid
        return str(uuid.uuid4())[:8].upper()
