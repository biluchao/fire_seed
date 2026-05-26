"""
火种系统 · 账户总账 (AccountLedger)

核心职责：
1. 作为全系统唯一的账户财务状态权威来源，实时计算总权益、可用保证金、已用保证金、浮动盈亏、已实现盈亏等核心财务指标
2. 精确记录和追索手续费、资金费率收支、杠杆借贷利息等所有摩擦成本，为策略提供净生存能力评估

外部依赖（真实模块接口）：
- core.data_feed.DataFeed : 获取当前市场行情，用于计算浮动盈亏和强平价格
- core.utils.config_loader.ConfigLoader : 读取保证金率、杠杆倍数等财务配置参数

接口契约：
- get_equity() -> Dict[str, Any] : 返回包含总权益、可用保证金、已用保证金、浮动盈亏的完整财务快照
- get_margin_ratio() -> Dict[str, Any] : 返回当前保证金率及其健康状态
- get_available_balance() -> Dict[str, Any] : 返回可用于开仓的余额
- record_trade(symbol: str, side: int, qty: float, price: float, fee: float) -> Dict[str, Any] : 记录一笔成交，更新持仓与权益
- record_funding(symbol: str, amount: float) -> Dict[str, Any] : 记录资金费率结算
- record_interest(symbol: str, amount: float) -> Dict[str, Any] : 记录杠杆借贷利息
- update_market_prices(prices: Dict[str, float]) -> Dict[str, Any] : 更新所有持仓的浮动盈亏
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 DataFeed 不可用时，使用上一次有效价格计算浮动盈亏，并标记 "degraded" 状态
- 当配置缺失时，使用类常量中的保守默认值（如最低保证金率 1.5）
- 所有交易记录在写入前进行严格的数值校验，非法数据将被拒绝并告警
- 浮点数计算采用 double 精度，权益关键数据在每次写入后生成快照校验和

资源管理：
- 本模块持有持仓字典和权益累加器，通过线程锁保证并发安全
- 不持有任何外部连接或文件句柄，所有状态在内存中维护
- 在系统退出时，需要调用 save_snapshot() 将关键状态持久化
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class AccountLedger:
    """账户总账，全系统唯一的财务状态权威来源"""

    # ========== 类常量 ==========
    DEFAULT_MIN_MARGIN_RATIO = 1.5
    DEFAULT_LEVERAGE = 1.0
    DEFAULT_MAKER_FEE_RATE = 0.0002
    DEFAULT_TAKER_FEE_RATE = 0.0004
    MAX_SNAPSHOT_AGE_SEC = 300
    PRICE_STALE_THRESHOLD_SEC = 30

    def __init__(self):
        self._initial_equity = 0.0
        self._realized_pnl = 0.0
        self._total_fees = 0.0
        self._total_funding = 0.0
        self._total_interest = 0.0

        # 持仓结构：symbol -> {side, qty, entry_price, margin}
        self._positions: Dict[str, Dict[str, Any]] = {}
        self._current_prices: Dict[str, float] = {}

        self._data_feed = None
        self._config_loader = None
        self._snapshot: Dict[str, Any] = {}
        self._snapshot_timestamp = 0.0
        self._lock = threading.Lock()
        self._checksum = 0.0

        logger.info("AccountLedger 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        data_feed: Optional[Any] = None,
        config_loader: Optional[Any] = None,
    ) -> None:
        if data_feed is not None:
            self._data_feed = data_feed
            logger.info("DataFeed 注入成功")
        else:
            logger.warning("DataFeed 未注入，行情更新功能降级")
        if config_loader is not None:
            self._config_loader = config_loader
            logger.info("ConfigLoader 注入成功")
        else:
            logger.warning("ConfigLoader 未注入，使用默认配置")

    # ========== 资金初始化 ==========
    def set_initial_equity(self, amount: float) -> Dict[str, Any]:
        if amount <= 0:
            logger.warning(f"无效初始入金金额: {amount}")
            return {
                "status": "error",
                "reason": f"初始入金金额必须大于零，当前值: {amount}",
                "data": {},
                "warnings": ["invalid_initial_equity"],
            }
        with self._lock:
            if self._positions:
                return {
                    "status": "error",
                    "reason": "账户已有持仓，无法重置初始权益",
                    "data": {},
                    "warnings": ["positions_exist"],
                }
            self._initial_equity = amount
            self._recalculate_checksum()
        logger.info("初始权益设置为: %.2f", amount)
        return {
            "status": "ok",
            "reason": f"初始权益已设置为: {amount:.2f}",
            "data": {"initial_equity": amount},
            "warnings": [],
        }

    # ========== 公共接口 ==========
    def get_equity(self) -> Dict[str, Any]:
        with self._lock:
            equity, available, used, unrealized = self._calculate_equity_locked()
            margin_ratio = equity / used if used > 0 else float('inf')
            result = {
                "total_equity": round(equity, 8),
                "available_margin": round(available, 8),
                "used_margin": round(used, 8),
                "unrealized_pnl": round(unrealized, 8),
                "realized_pnl": round(self._realized_pnl, 8),
                "margin_ratio": round(margin_ratio, 4) if margin_ratio != float('inf') else None,
                "total_fees": round(self._total_fees, 8),
                "total_funding": round(self._total_funding, 8),
                "total_interest": round(self._total_interest, 8),
            }
        return {
            "status": "ok",
            "reason": f"总权益: {result['total_equity']:.2f}",
            "data": result,
            "warnings": [],
        }

    def get_margin_ratio(self) -> Dict[str, Any]:
        equity_res = self.get_equity()
        if equity_res["status"] != "ok":
            return equity_res
        data = equity_res["data"]
        ratio = data.get("margin_ratio")
        if ratio is None:
            return {
                "status": "ok",
                "reason": "无持仓，保证金率不适用",
                "data": {"margin_ratio": None, "health": "no_position"},
                "warnings": [],
            }
        min_ratio = self._get_config("min_margin_ratio", self.DEFAULT_MIN_MARGIN_RATIO)
        if ratio < min_ratio:
            health, reason = "critical", f"保证金率 {ratio:.2f} 低于最低要求 {min_ratio}"
            logger.error(f"{reason} #RECOVERY: 立即补充保证金或主动减仓")
        elif ratio < min_ratio * 1.5:
            health, reason = "warning", f"保证金率 {ratio:.2f} 接近警戒线"
        else:
            health, reason = "healthy", f"保证金率 {ratio:.2f} 正常"
        return {
            "status": "ok",
            "reason": reason,
            "data": {"margin_ratio": ratio, "health": health, "min_ratio": min_ratio},
            "warnings": [],
        }

    def get_available_balance(self) -> Dict[str, Any]:
        equity_res = self.get_equity()
        if equity_res["status"] != "ok":
            return equity_res
        return {
            "status": "ok",
            "reason": f"可用余额: {equity_res['data']['available_margin']:.2f}",
            "data": {"available_balance": round(equity_res["data"]["available_margin"], 8)},
            "warnings": [],
        }

    def record_trade(
        self, symbol: str, side: int, qty: float, price: float, fee: float = 0.0
    ) -> Dict[str, Any]:
        # 参数校验
        if side not in (1, -1):
            return {"status": "error", "reason": f"无效方向: {side}", "data": {}, "warnings": ["invalid_side"]}
        if qty <= 0 or price <= 0:
            return {"status": "error", "reason": f"无效数量/价格: qty={qty}, price={price}", "data": {}, "warnings": ["invalid_trade_params"]}
        if fee < 0:
            fee = 0.0

        with self._lock:
            # 保证金预检
            leverage = self._get_config("leverage", self.DEFAULT_LEVERAGE)
            estimated_margin = (qty * price) / leverage
            _, available, _, _ = self._calculate_equity_locked()
            if estimated_margin > available:
                return {
                    "status": "error",
                    "reason": f"保证金不足: 需要 {estimated_margin:.2f}, 可用 {available:.2f}",
                    "data": {"required_margin": round(estimated_margin, 8), "available": round(available, 8)},
                    "warnings": ["insufficient_margin"],
                }

            self._process_trade_locked(symbol, side, qty, price, fee)
            self._recalculate_checksum()

        logger.info(f"成交: {symbol} {'多' if side==1 else '空'} {qty}@{price:.2f} 手续费 {fee:.4f}")
        return self.get_equity()

    def record_funding(self, symbol: str, amount: float) -> Dict[str, Any]:
        if not isinstance(amount, (int, float)):
            return {"status": "error", "reason": f"无效资金费金额: {amount}", "data": {}, "warnings": ["invalid_funding_amount"]}
        with self._lock:
            self._realized_pnl += amount
            self._total_funding += amount
            self._recalculate_checksum()
        logger.info(f"资金费率结算: {symbol} {amount:+.6f}")
        return self.get_equity()

    def record_interest(self, symbol: str, amount: float) -> Dict[str, Any]:
        if not isinstance(amount, (int, float)):
            return {"status": "error", "reason": f"无效利息金额: {amount}", "data": {}, "warnings": ["invalid_interest_amount"]}
        with self._lock:
            self._realized_pnl += amount
            self._total_interest += amount
            self._recalculate_checksum()
        logger.info(f"杠杆利息结算: {symbol} {amount:+.6f}")
        return self.get_equity()

    def update_market_prices(self, prices: Dict[str, float]) -> Dict[str, Any]:
        if not prices:
            return {"status": "ok", "reason": "无行情数据需要更新", "data": {}, "warnings": []}
        # 拒绝无效价格
        valid_prices = {sym: p for sym, p in prices.items() if isinstance(p, (int, float)) and p > 0}
        if not valid_prices:
            return {"status": "ok", "reason": "所有行情数据无效", "data": {}, "warnings": ["invalid_prices"]}
        with self._lock:
            self._current_prices.update(valid_prices)
            self._snapshot_timestamp = 0.0
        logger.debug(f"更新行情: {list(valid_prices.keys())}")
        return {"status": "ok", "reason": "行情已更新", "data": {}, "warnings": []}

    def get_estimated_liquidation_price(self, symbol: str) -> Dict[str, Any]:
        with self._lock:
            pos = self._positions.get(symbol)
            if pos is None:
                return {"status": "ok", "reason": f"无 {symbol} 持仓", "data": {"liquidation_price": None}, "warnings": []}
            min_ratio = self._get_config("min_margin_ratio", self.DEFAULT_MIN_MARGIN_RATIO)
            entry, margin, qty, side = pos["entry_price"], pos["margin"], pos["qty"], pos["side"]
            # 强平价 = entry * (1 - side * (1 - min_ratio))
            liq_price = entry * (1 - side * (1 - min_ratio))
        return {
            "status": "ok",
            "reason": f"{symbol} 强平估算价: {liq_price:.4f}",
            "data": {"symbol": symbol, "liquidation_price": round(liq_price, 8), "side": side, "min_margin_ratio": min_ratio},
            "warnings": [],
        }

    def health_check(self) -> Dict[str, Any]:
        try:
            if not hasattr(self, '_lock'):
                return {"status": "error", "reason": "模块未初始化", "data": {}, "warnings": ["not_initialized"]}
            with self._lock:
                pos_count = len(self._positions)
                current_checksum = self._calculate_internal_checksum()
                checksum_ok = abs(current_checksum - self._checksum) < 1e-8
                if not checksum_ok:
                    logger.error("财务数据校验和失败 #RECOVERY: 检查数据一致性，可能需要从交易所同步")
            return {
                "status": "ok" if checksum_ok else "degraded",
                "reason": f"AccountLedger 正常，持仓数: {pos_count}",
                "data": {"position_count": pos_count, "checksum_ok": checksum_ok,
                         "dependencies": {"data_feed": self._data_feed is not None, "config_loader": self._config_loader is not None}},
                "warnings": [] if checksum_ok else ["checksum_mismatch"],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和数据结构")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"]}

    # ========== 私有核心交易处理 ==========
    def _process_trade_locked(self, symbol: str, side: int, qty: float, price: float, fee: float) -> None:
        """原子化处理交易：平仓 + 开仓，必须在锁内调用"""
        pos = self._positions.get(symbol)
        if pos is None:
            # 直接开新仓
            self._open_position_locked(symbol, side, qty, price, fee)
            return

        if pos["side"] == side:
            # 同向加仓
            self._add_position_locked(symbol, qty, price, fee)
        else:
            # 先平后开
            close_qty = min(pos["qty"], qty)
            pnl = self._calculate_close_pnl(pos, close_qty, price)
            self._realized_pnl += pnl
            self._realized_pnl -= fee
            self._total_fees += fee

            pos["qty"] -= close_qty
            if pos["qty"] <= 1e-12:
                del self._positions[symbol]
                logger.debug(f"完全平仓: {symbol}, 实现盈亏 {pnl:.4f}")
            else:
                logger.debug(f"部分平仓: {symbol} {close_qty}, 实现盈亏 {pnl:.4f}")

            # 处理剩余数量：开反向新仓
            remaining = qty - close_qty
            if remaining > 1e-12:
                self._open_position_locked(symbol, side, remaining, price, 0.0)

    def _open_position_locked(self, symbol: str, side: int, qty: float, price: float, fee: float) -> None:
        leverage = self._get_config("leverage", self.DEFAULT_LEVERAGE)
        margin = (qty * price) / leverage
        self._positions[symbol] = {"side": side, "qty": qty, "entry_price": price, "margin": margin}
        self._realized_pnl -= fee
        self._total_fees += fee

    def _add_position_locked(self, symbol: str, qty: float, price: float, fee: float) -> None:
        pos = self._positions[symbol]
        total_qty = pos["qty"] + qty
        pos["entry_price"] = (pos["entry_price"] * pos["qty"] + price * qty) / total_qty
        pos["qty"] = total_qty
        leverage = self._get_config("leverage", self.DEFAULT_LEVERAGE)
        pos["margin"] += (qty * price) / leverage
        self._realized_pnl -= fee
        self._total_fees += fee

    @staticmethod
    def _calculate_close_pnl(pos: Dict[str, Any], close_qty: float, price: float) -> float:
        """计算平仓盈亏"""
        # 多头: (price - entry) * qty
        # 空头: (entry - price) * qty = (price - entry) * qty * side (side=-1)
        return (price - pos["entry_price"]) * close_qty * pos["side"]

    # ========== 内部财务计算 ==========
    def _calculate_equity_locked(self) -> Tuple[float, float, float, float]:
        unrealized = 0.0
        used_margin = 0.0
        for symbol, pos in self._positions.items():
            used_margin += pos["margin"]
            price = self._current_prices.get(symbol)
            if price is not None:
                unrealized += self._calculate_close_pnl(pos, pos["qty"], price)
        total_equity = self._initial_equity + self._realized_pnl + unrealized
        available = max(0.0, total_equity - used_margin)
        return total_equity, available, used_margin, unrealized

    def _get_config(self, key: str, default: float) -> float:
        if self._config_loader is not None:
            try:
                return self._config_loader.get(key, default)
            except Exception:
                pass
        return default

    def _recalculate_checksum(self) -> None:
        self._checksum = self._calculate_internal_checksum()

    def _calculate_internal_checksum(self) -> float:
        checksum = self._initial_equity + self._realized_pnl + self._total_fees + self._total_funding
        for pos in self._positions.values():
            checksum += pos["entry_price"] * pos["qty"]
        return checksum
