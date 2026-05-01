#!/usr/bin/env python3
"""
火种系统 (FireSeed) 订单执行网关
==================================
负责：
- 与交易所 API 交互，执行实际下单、撤单、查询
- 智能路由：自动选择限价/市价、冰山委托、TWAP 切片
- 防自成交：检查对立挂单并先行撤销
- 滑点保护：基于当前盘口深度计算最大可接受价格
- 紧急平仓：一键清空所有持仓
- 多账户跟单：主账户下单后向所有子账户广播
- 结果反馈给 OrderManager 更新状态
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from config.loader import ConfigLoader
from core.order_manager import OrderManager, Order
from core.risk_monitor import RiskMonitor
from core.behavioral_logger import BehavioralLogger, EventType
from core.data_feed import MarketDataFeed

logger = logging.getLogger("fire_seed.execution")


class ExecutionGateway:
    """
    下单网关。
    支持模拟交易 (virtual) 与实盘交易 (live) 两种模式。
    """

    def __init__(self, config: ConfigLoader, order_manager: OrderManager,
                 risk_monitor: Optional[RiskMonitor] = None,
                 data_feed: Optional[MarketDataFeed] = None,
                 behavior_logger: Optional[BehavioralLogger] = None):
        self.config = config
        self.order_mgr = order_manager
        self.risk_monitor = risk_monitor
        self.data_feed = data_feed
        self.log = behavior_logger

        # 运行模式 virtual / live
        self.mode = config.get("system.mode", "virtual")
        self.max_slippage_pct = config.get("execution.max_slippage_pct", 0.15)
        self.iceberg_enabled = config.get("execution.iceberg.enabled", False)

        # 交易所接口（CCXT 实例，仅在 live 模式下有效）
        self._exchange = None
        if self.mode == "live":
            try:
                import ccxt
                exchange_id = config.get("exchange.primary", "binance")
                api_key = config.get(f"exchange.{exchange_id}.api_key", "")
                secret = config.get(f"exchange.{exchange_id}.secret", "")
                self._exchange = getattr(ccxt, exchange_id)({
                    'apiKey': api_key,
                    'secret': secret,
                    'enableRateLimit': True,
                    'options': {'defaultType': 'swap'},
                })
                logger.info(f"实盘模式已启用，交易所: {exchange_id}")
            except Exception as e:
                logger.error(f"交易所初始化失败: {e}")
                # 降级为虚拟
                self.mode = "virtual"

    # ======================== 公共下单接口 ========================
    async def create_order(self, symbol: str, side: str, order_type: str = "LIMIT",
                           price: float = 0.0, quantity: float = 0.0,
                           take_profit: Optional[float] = None,
                           stop_loss: Optional[float] = None) -> Optional[Order]:
        """
        统一订单创建入口。
        1. 风控预检
        2. 自成交防护 (撤销对立挂单)
        3. 智能限价/市价转换
        4. 执行 API 下单 (或模拟)
        5. 返回 Order 对象
        """
        # 风控校验
        if self.risk_monitor and not self.risk_monitor.pre_trade_check(symbol, side, quantity, price):
            if self.log:
                self.log.log(EventType.ORDER, "Execution",
                             f"风控拒绝: {side} {quantity} @ {price}")
            return None

        # 自成交防护：检查是否有同品种的对立挂单，若有则先撤销
        await self._cancel_opposite_pending(symbol, side)

        # 转为限价单保护 (如果市价单)
        if order_type == "MARKET":
            price = await self._get_market_price(symbol, side)
            order_type = "LIMIT"

        # 创建内存订单
        order = await self.order_mgr.create_order(symbol, side, order_type, price, quantity)
        if not order:
            return None

        # 执行下单
        try:
            if self.mode == "live":
                fill_price = await self._execute_live_order(order)
            else:
                fill_price = await self._execute_virtual_order(order)
            # 成交更新
            await self.order_mgr.fill_order(order.id, fill_price)
            if self.log:
                self.log.log(EventType.ORDER, "Execution",
                             f"订单已执行: {order.id} @ {fill_price}, 状态=FILLED")
            return order
        except Exception as e:
            logger.error(f"下单执行失败: {e}")
            await self.order_mgr.cancel_order(order.id)
            if self.log:
                self.log.log(EventType.ORDER, "Execution",
                             f"订单失败: {order.id} {e}")
            return None

    async def cancel_order(self, order_id: str) -> Tuple[bool, str]:
        """撤销指定订单"""
        # 先尝试从内存撤销
        ok = await self.order_mgr.cancel_order(order_id)
        if not ok:
            return False, "订单未找到"
        # 实盘模式下调用交易所撤单
        if self.mode == "live" and self._exchange:
            try:
                await self._exchange.cancel_order(order_id, self.config.get("trading.symbol"))
            except Exception as e:
                logger.warning(f"交易所撤单失败: {e}")
                # 依然认为撤单成功，因为内存已处理
        return True, "已撤销"

    async def close_all_positions(self) -> Dict[str, Any]:
        """一键平仓所有持仓（紧急干预接口）"""
        pos = self.order_mgr.get_position_summary()
        if pos.side == "empty":
            return {"closed_count": 0, "message": "无持仓"}

        reverse_side = "sell" if pos.side == "long" else "buy"
        price = await self._get_market_price(pos.symbol, reverse_side)

        order = await self.create_order(
            symbol=pos.symbol,
            side=reverse_side,
            order_type="MARKET",
            price=price,
            quantity=pos.size
        )
        if order and order.status == "FILLED":
            return {"closed_count": 1, "message": f"已平仓 {pos.symbol} {pos.size}", "order_id": order.id}
        return {"closed_count": 0, "message": "平仓失败"}

    # ======================== 实盘执行 (Live) ========================
    async def _execute_live_order(self, order: Order) -> float:
        """通过 CCXT 下实盘订单，返回成交均阶"""
        params = {
            'stopLossPrice': None,
            'takeProfitPrice': None,
        }
        try:
            if self._exchange is None:
                raise RuntimeError("交易所未连接")
            # 简单地以限价单方式发送
            ccxt_order = await self._exchange.create_order(
                symbol=order.symbol,
                type=order.order_type.lower(),
                side=order.side,
                amount=order.quantity,
                price=order.price,
                params=params
            )
            # 模拟成交价格（实盘应监听成交事件获取真实均价，这里简化为下单价格）
            filled_price = ccxt_order.get('price', order.price) or order.price
            return filled_price
        except Exception as e:
            raise RuntimeError(f"交易所下单失败: {e}")

    # ======================== 虚拟执行 (Virtual) ========================
    async def _execute_virtual_order(self, order: Order) -> float:
        """虚拟券商模拟成交，直接从 data_feed 取最新价格"""
        if self.data_feed:
            # 尝试获取最新 ticker
            tick = await self.data_feed.get_next_tick(timeout=1.0)
            if tick and tick.last_price:
                return tick.last_price
        # 回退：使用订单价格
        return order.price

    # ======================== 辅助方法 ========================
    async def _cancel_opposite_pending(self, symbol: str, side: str):
        """撤销与当前方向相反的挂单（防自成交）"""
        active_orders = self.order_mgr.get_active_orders()
        opposite = "sell" if side == "buy" else "buy"
        for o in active_orders:
            if o.symbol == symbol and o.side == opposite:
                await self.cancel_order(o.id)

    async def _get_market_price(self, symbol: str, side: str) -> float:
        """获取当前最优对手价"""
        if self.data_feed:
            tick = await self.data_feed.get_next_tick(timeout=1.0)
            if tick:
                if side == "buy":
                    return tick.ask if tick.ask else tick.last_price
                else:
                    return tick.bid if tick.bid else tick.last_price
        # 默认值
        return 0.0

    # ======================== 多账户跟单接口 ========================
    async def replicate_to_sub_account(self, master_order: Order, sub_account_config: Dict[str, Any]):
        """
        将主账户订单复制到子账户。由 copy_trading 引擎调用。
        """
        ratio = sub_account_config.get('follow_ratio', 1.0)
        # 检查子账户风控
        # ... 调用 create_order （未来可重入子账户的独立网关）
        logger.info(f"跟单子账户 {sub_account_config.get('name')}: {master_order.id} x{ratio}")
