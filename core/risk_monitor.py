#!/usr/bin/env python3
"""
火种系统 (FireSeed) 实时风控监控模块
======================================
负责：
- 风险指标实时计算：最大回撤、日亏损、VaR、CVaR
- 三级熔断器：逐级触发，自动恢复策略
- 流动性监控：订单簿深度衰减、价差异常
- 保证金压力测试：模拟建仓后的保证金率
- 与 C++ 硬实时风控协同 (通过共享内存读取实时风控决策)
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from config.loader import ConfigLoader
from core.order_manager import OrderManager
from core.behavioral_logger import BehavioralLogger, EventType

logger = logging.getLogger("fire_seed.risk")


# ---------- 数据结构 ----------
@dataclass
class CircuitBreaker:
    """三级熔断器状态机"""
    level: int = 0                      # 0=正常, 1=禁止开仓, 2=平非套利, 3=全平休眠
    triggered_at: Optional[datetime] = None
    reason: str = ""
    cooldown_until: Optional[datetime] = None
    daily_loss_accumulated: float = 0.0
    daily_loss_limit: float = 500.0     # 默认值，从配置读取

    def reset(self):
        self.level = 0
        self.triggered_at = None
        self.reason = ""
        self.cooldown_until = None

    def trigger(self, level: int, reason: str, cooldown_minutes: int = 0):
        self.level = level
        self.triggered_at = datetime.now()
        self.reason = reason
        if cooldown_minutes > 0:
            self.cooldown_until = datetime.now().timestamp() + cooldown_minutes * 60


@dataclass
class RiskSnapshot:
    drawdown_pct: float
    drawdown_warn_pct: float
    daily_loss_pct: float
    daily_loss_limit_pct: float
    var_99: float
    cvar: float
    margin_ratio: float
    leverage: float
    timestamp: datetime


# ---------- 风控监控器 ----------
class RiskMonitor:
    """实时风控核心，负责所有软风控逻辑。硬实时止损由 C++ 模块处理。"""

    def __init__(self, config: ConfigLoader, order_manager: OrderManager,
                 behavior_logger: Optional[BehavioralLogger] = None,
                 cpp_risk_module=None):
        self.config = config
        self.order_mgr = order_manager
        self.log = behavior_logger
        self.cpp_risk = cpp_risk_module      # 可选的 C++ 模块引用

        # 从配置加载阈值
        risk_cfg = config.get("risk", {})
        self.drawdown_warn_pct = risk_cfg.get("drawdown_warn", 10.0)
        self.drawdown_halt_pct = risk_cfg.get("drawdown_halt", 15.0)
        cb_cfg = risk_cfg.get("circuit_breakers", {})
        self.daily_loss_limit_pct = cb_cfg.get("level_1", 0.03) * 100  # 转换为百分比

        # 内部状态
        self.circuit_breaker = CircuitBreaker()
        self._equity_history: deque = deque(maxlen=500)    # 用于计算回撤
        self._daily_pnl_history: deque = deque(maxlen=1000)
        self._returns: deque = deque(maxlen=1000)          # 用于 VaR
        self._peak_equity = 0.0
        self._current_drawdown = 0.0

        # 流动性监控
        self._depth_history: deque = deque(maxlen=60)      # 记录最近60分钟的深度

        # 风控参数热更新支持
        self._param_store: Dict[str, float] = {
            "daily_loss_limit": self.daily_loss_limit_pct,
            "drawdown_warn": self.drawdown_warn_pct,
            "max_leverage": config.get("trading.max_leverage", 3)
        }

    # ======================== 预计算接口 (每 Tick 调用) ========================
    async def pre_trade_check(self, symbol: str, side: str, quantity: float, price: float) -> bool:
        """建仓前风控检查，返回 True 表示通过"""
        # 1. 熔断状态检查
        if self.circuit_breaker.level >= 1:
            if self.log:
                self.log.log(EventType.RISK, "PreTrade", f"熔断级别 {self.circuit_breaker.level} 禁止开仓")
            return False

        # 2. 日亏损限额检查
        if self.daily_loss_limit_pct > 0:
            daily_loss = self._get_daily_loss()
            acc = await self.order_mgr.get_account()
            if acc.equity > 0 and daily_loss / acc.equity * 100 >= self.daily_loss_limit_pct:
                if self.log:
                    self.log.log(EventType.RISK, "PreTrade", f"日亏损已达上限 {self.daily_loss_limit_pct}%")
                return False

        # 3. 保证金检查 (简化)
        if price and quantity:
            margin_used = price * quantity / self._param_store["max_leverage"]
            acc = await self.order_mgr.get_account()
            if margin_used > acc.available * 0.95:
                return False

        return True

    async def update_market_data(self, price: float, bid_vol: float, ask_vol: float, spread_pct: float):
        """更新市场微观数据，用于流动性监控"""
        self._depth_history.append((bid_vol, ask_vol, spread_pct))
        # 简单更新收益序列（用于 VaR 计算）
        if len(self._equity_history) > 1:
            prev_price = self._equity_history[-1][1] if self._equity_history[-1][1] else price
            if prev_price:
                ret = (price - prev_price) / prev_price
                self._returns.append(ret)

    async def update_equity(self, equity: float):
        """更新权益曲线，用于回撤计算"""
        self._equity_history.append((datetime.now(), equity))
        if equity > self._peak_equity:
            self._peak_equity = equity
        if self._peak_equity > 0:
            self._current_drawdown = (self._peak_equity - equity) / self._peak_equity * 100

    # ======================== 查询接口 ========================
    async def get_snapshot(self) -> RiskSnapshot:
        """获取当前风险指标快照"""
        acc = await self.order_mgr.get_account()
        var99, cvar = self._calc_var()
        pos = await self.order_mgr.get_position_summary()
        margin_ratio = (acc.equity / (acc.equity + pos.size * pos.mark_price)) * 100 if pos.size else 100.0
        return RiskSnapshot(
            drawdown_pct=round(self._current_drawdown, 2),
            drawdown_warn_pct=self.drawdown_warn_pct,
            daily_loss_pct=round(self._daily_loss_pct(), 2),
            daily_loss_limit_pct=self.daily_loss_limit_pct,
            var_99=round(var99, 2),
            cvar=round(cvar, 2),
            margin_ratio=round(margin_ratio, 1),
            leverage=round(self._param_store["max_leverage"], 1),
            timestamp=datetime.now()
        )

    async def get_liquidity_metrics(self) -> Dict[str, Any]:
        """获取当前流动性指标"""
        if not self._depth_history:
            return {"bid_depth": 0, "ask_depth": 0, "spread_pct": 0, "depth_shrink_from_avg": 0, "risk_level": "normal"}
        latest = self._depth_history[-1]
        bid_depth, ask_depth, spread = latest
        # 计算平均深度
        avg_bid = np.mean([d[0] for d in self._depth_history]) if self._depth_history else bid_depth
        shrink = (1 - bid_depth / avg_bid) * 100 if avg_bid else 0
        risk = "normal"
        if shrink > 50:
            risk = "critical"
        elif shrink > 30:
            risk = "warning"
        return {"bid_depth": bid_depth, "ask_depth": ask_depth, "spread_pct": spread,
                "depth_shrink_from_avg": round(shrink, 1), "risk_level": risk}

    # ======================== 熔断操作 ========================
    async def check_and_trigger(self) -> bool:
        """根据当前风险状态决定是否触发熔断。在主循环中每 Tick 调用"""
        if self.circuit_breaker.level >= 3:
            return False

        acc = await self.order_mgr.get_account()
        equity = acc.equity
        if equity <= 0:
            return False
        daily_loss = self._daily_loss_pct()
        drawdown = self._current_drawdown

        # 三级熔断判断
        if drawdown >= self.drawdown_halt_pct:
            self.circuit_breaker.trigger(3, f"回撤达到{drawdown:.1f}%", 1440)
            if self.log:
                self.log.log(EventType.RISK, "CircuitBreaker", f"三级熔断触发: 回撤{drawdown:.1f}%")
            return True
        elif daily_loss >= 8.0:
            self.circuit_breaker.trigger(3, f"日亏损{daily_loss:.1f}%", 1440)
            return True
        elif daily_loss >= 5.0:
            self.circuit_breaker.trigger(2, f"日亏损{daily_loss:.1f}%", 10)
            return True
        elif daily_loss >= 3.0:
            self.circuit_breaker.trigger(1, f"日亏损{daily_loss:.1f}%", 5)
            return True
        return False

    # ======================== 参数管理 ========================
    def get_param(self, name: str) -> float:
        return self._param_store.get(name, 0.0)

    def set_param(self, name: str, value: float):
        if name in self._param_store:
            self._param_store[name] = value

    @property
    def current_drawdown_pct(self) -> float:
        return self._current_drawdown

    @property
    def daily_loss_pct(self) -> float:
        return self._daily_loss_pct()

    # ======================== 内部计算 ========================
    def _daily_loss_pct(self) -> float:
        """当日已实现亏损 / 初始权益 * 100"""
        acc = self.order_mgr._account  # 简化访问
        if acc.initial_deposit <= 0:
            return 0.0
        return (-acc.realized_pnl_today / acc.initial_deposit) * 100 if acc.realized_pnl_today < 0 else 0.0

    def _calc_var(self) -> Tuple[float, float]:
        """简单历史模拟法计算 VaR 与 CVaR"""
        if len(self._returns) < 50:
            return 0.0, 0.0
        returns = np.array(list(self._returns))
        var99 = np.percentile(returns, 1) * (-1)  # 取负值，正数表示损失金额
        cvar = returns[returns <= -var99].mean() * (-1) if (returns <= -var99).any() else var99
        # 乘以实际权益
        acc = self.order_mgr._account
        equity = acc.equity if acc.equity > 0 else 10000
        return var99 * equity, cvar * equity

    def _get_daily_loss(self) -> float:
        acc = self.order_mgr._account
        return -acc.realized_pnl_today if acc.realized_pnl_today < 0 else 0.0

    # 预留的 C++ 模块更新接口
    def update_trailing_stop(self, current_price: float):
        """通知 C++ 硬实时风控模块更新移动止损价格"""
        if self.cpp_risk and hasattr(self.cpp_risk, 'update_stop_price'):
            self.cpp_risk.update_stop_price(current_price)
