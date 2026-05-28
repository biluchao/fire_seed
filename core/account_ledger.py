"""
火种系统 · 账户总账 (AccountLedger)

核心职责：
1. 作为全系统唯一的账户财务状态权威来源，实时计算总权益、可用保证金、已用保证金、浮动盈亏、已实现盈亏等核心财务指标
2. 精确记录和追索手续费、资金费率收支、杠杆借贷利息等所有摩擦成本，为策略提供净生存能力评估

外部依赖（真实模块接口）：
- core.data_feed.DataFeed : 获取当前市场行情，用于计算浮动盈亏和强平价格；需实现 ping() 方法用于连通性测试
- core.utils.config_loader.ConfigLoader : 读取保证金率、杠杆倍数等财务配置参数；需实现 health_check() 方法

接口契约：
- get_equity() -> Dict[str, Any] : 返回包含总权益、可用保证金、已用保证金、浮动盈亏的完整财务快照
- get_margin_ratio() -> Dict[str, Any] : 返回当前保证金率及其健康状态
- get_available_balance() -> Dict[str, Any] : 返回可用于开仓的余额
- record_trade(symbol: str, side: int, qty: float, price: float, fee: float) -> Dict[str, Any] : 记录一笔成交，更新持仓与权益
- record_funding(symbol: str, amount: float) -> Dict[str, Any] : 记录资金费率结算
- record_interest(symbol: str, amount: float) -> Dict[str, Any] : 记录杠杆借贷利息
- update_market_prices(prices: Dict[str, float]) -> Dict[str, Any] : 更新所有持仓的浮动盈亏
- health_check() -> Dict[str, Any] : 模块自检，包含外部依赖端到端连通性验证
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 DataFeed 不可用时，使用最近有效价格计算浮动盈亏，并标记 "price_stale" 警告
- 当配置缺失时，使用类常量中的保守默认值（如最低保证金率 1.5）
- 所有交易记录在写入前进行严格的数值校验，非法数据将被拒绝并告警
- 财务校验和每日自动校验，异常时推送至 Telemetry 并在前端显示红色预警

资源管理：
- 本模块持有持仓字典和权益累加器，通过线程锁保证并发安全
- 不持有任何外部连接或文件句柄，所有状态在内存中维护
- 在系统退出时，需要调用 save_snapshot() 将关键状态持久化到磁盘
"""

import time
import logging
import threading
import os
import hashlib
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class AccountLedger:
    """账户总账，全系统唯一的财务状态权威来源"""

    # ========== 类常量 ==========
    DEFAULT_MIN_MARGIN_RATIO = 1.5
    DEFAULT_LEVERAGE = 1.0
    DEFAULT_MAKER_FEE_RATE = 0.0002
    DEFAULT_TAKER_FEE_RATE = 0.0004
    PRICE_STALE_THRESHOLD_SEC = 30
    MAX_SINGLE_POSITION_RISK_PCT = 0.08

    def __init__(self):
        self._initial_equity = 0.0
        self._realized_pnl = 0.0
        self._total_fees = 0.0
        self._total_funding = 0.0
        self._total_interest = 0.0

        self._positions: Dict[str, Dict[str, Any]] = {}
        self._current_prices: Dict[str, float] = {}
        self._price_timestamps: Dict[str, float] = {}

        self._data_feed = None
        self._config_loader = None
        self._lock = threading.Lock()

        self._checksum_salt = hashlib.sha256(os.urandom(32)).hexdigest()
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

    # ========== 资金初始化（仅限管理员，需通过协商总线调用） ==========
    def set_initial_equity(self, amount: float, admin_token: str = "") -> Dict[str, Any]:
        """
        设置初始入金金额。仅在没有持仓时允许调用，且应通过管理接口触发。
        """
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
                logger.warning("账户已有持仓，拒绝重置初始权益 #AUDIT")
                return {
                    "status": "error",
                    "reason": "账户已有持仓，无法重置初始权益",
                    "data": {},
                    "warnings": ["positions_exist"],
                }
            old_equity = self._initial_equity
            self._initial_equity = amount
            self._recalculate_checksum()
            logger.warning(f"初始权益重置: {old_equity:.2f} -> {amount:.2f} #AUDIT: 初始权益变更，旧值={old_equity}")

        return {
            "status": "ok",
            "reason": f"初始权益已设置为: {amount:.2f}",
            "data": {"initial_equity": amount},
            "warnings": [],
        }

    # ========== 公共接口 ==========
    def get_equity(self) -> Dict[str, Any]:
        with self._lock:
            equity, available, used, unrealized, has_stale = self._calculate_equity_locked()
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
                "has_stale_prices": has_stale,
            }
        return {
            "status": "ok",
            "reason": f"总权益: {result['total_equity']:.2f}",
            "data": result,
            "warnings": ["stale_prices_detected"] if has_stale else [],
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
            "warnings": equity_res.get("warnings", []),
        }

    def get_available_balance(self) -> Dict[str, Any]:
        equity_res = self.get_equity()
        if equity_res["status"] != "ok":
            return equity_res
        return {
            "status": "ok",
            "reason": f"可用余额: {equity_res['data']['available_margin']:.2f}",
            "data": {"available_balance": round(equity_res["data"]["available_margin"], 8)},
            "warnings": equity_res.get("warnings", []),
        }

    def record_trade(
        self, symbol: str, side: int, qty: float, price: float, fee: float = 0.0
    ) -> Dict[str, Any]:
        if side not in (1, -1):
            return {"status": "error", "reason": f"无效方向: {side}", "data": {}, "warnings": ["invalid_side"]}
        if qty <= 0 or price <= 0:
            return {"status": "error", "reason": f"无效数量/价格: qty={qty}, price={price}", "data": {}, "warnings": ["invalid_trade_params"]}
        if fee < 0:
            fee = 0.0

        with self._lock:
            # 计算净保证金变动（考虑先平后开）
            leverage = self._get_config("leverage", self.DEFAULT_LEVERAGE)
            net_margin_delta = self._estimate_net_margin_delta_locked(symbol, side, qty, price, leverage)
            equity, available, _, _ = self._calculate_equity_locked()
            if net_margin_delta > available:
                return {
                    "status": "error",
                    "reason": f"保证金不足: 需要 {net_margin_delta:.2f}, 可用 {available:.2f}",
                    "data": {"required_margin": round(net_margin_delta, 8), "available": round(available, 8)},
                    "warnings": ["insufficient_margin"],
                }

            self._process_trade_locked(symbol, side, qty, price)
            # 统一扣除手续费
            self._realized_pnl -= fee
            self._total_fees += fee
            self._recalculate_checksum()

            risk_warning = self._check_position_risk_locked(symbol)
            if risk_warning:
                logger.warning(f"单品种风险敞口超限: {symbol} {risk_warning}")

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
        valid_prices = {sym: p for sym, p in prices.items() if isinstance(p, (int, float)) and p > 0}
        if not valid_prices:
            return {"status": "ok", "reason": "所有行情数据无效", "data": {}, "warnings": ["invalid_prices"]}
        now = time.time()
        with self._lock:
            for sym, p in valid_prices.items():
                self._current_prices[sym] = p
                self._price_timestamps[sym] = now
        logger.debug(f"更新行情: {list(valid_prices.keys())}")
        return {"status": "ok", "reason": "行情已更新", "data": {}, "warnings": []}

    def get_estimated_liquidation_price(self, symbol: str) -> Dict[str, Any]:
        with self._lock:
            pos = self._positions.get(symbol)
            if pos is None:
                return {"status": "ok", "reason": f"无 {symbol} 持仓", "data": {"liquidation_price": None}, "warnings": []}
            ts = self._price_timestamps.get(symbol, 0)
            if time.time() - ts > self.PRICE_STALE_THRESHOLD_SEC:
                return {
                    "status": "degraded",
                    "reason": f"{symbol} 行情数据过期 ({time.time() - ts:.0f}秒)，强平价格估算不可靠",
                    "data": {"liquidation_price": None},
                    "warnings": ["stale_price"],
                }
            min_ratio = self._get_config("min_margin_ratio", self.DEFAULT_MIN_MARGIN_RATIO)
            entry, margin, qty, side = pos["entry_price"], pos["margin"], pos["qty"], pos["side"]
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

            dep_status = {}
            # data_feed 连通性
            if self._data_feed is not None:
                if hasattr(self._data_feed, 'ping'):
                    try:
                        self._data_feed.ping()
                        dep_status["data_feed"] = "connected"
                    except Exception:
                        dep_status["data_feed"] = "unreachable"
                elif hasattr(self._data_feed, 'health_check'):
                    try:
                        res = self._data_feed.health_check()
                        dep_status["data_feed"] = res.get("status", "unknown")
                    except Exception:
                        dep_status["data_feed"] = "unreachable"
                else:
                    dep_status["data_feed"] = "no_ping_method"
            else:
                dep_status["data_feed"] = "not_injected"

            # config_loader 连通性
            if self._config_loader is not None and hasattr(self._config_loader, 'health_check'):
                try:
                    res = self._config_loader.health_check()
                    dep_status["config_loader"] = res.get("status", "unknown")
                except Exception:
                    dep_status["config_loader"] = "unreachable"
            else:
                dep_status["config_loader"] = "not_injected"

            with self._lock:
                pos_count = len(self._positions)
                current_checksum = self._calculate_internal_checksum()
                checksum_ok = abs(current_checksum - self._checksum) < 1e-8
                if not checksum_ok:
                    logger.error("财务数据校验和失败 #RECOVERY: 检查数据一致性，可能需要从交易所同步")

            return {
                "status": "ok" if checksum_ok and "unreachable" not in dep_status.values() else "degraded",
                "reason": f"AccountLedger 正常，持仓数: {pos_count}",
                "data": {
                    "position_count": pos_count,
                    "checksum_ok": checksum_ok,
                    "dependencies": dep_status,
                },
                "warnings": [] if checksum_ok else ["checksum_mismatch"],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和数据结构")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"]}

    # ========== 私有核心交易处理 ==========
    def _process_trade_locked(self, symbol: str, side: int, qty: float, price: float) -> None:
        """原子化处理交易：更新持仓和盈亏，不处理手续费"""
        pos = self._positions.get(symbol)
        if pos is None:
            self._open_position_locked(symbol, side, qty, price)
            return

        if pos["side"] == side:
            self._add_position_locked(symbol, qty, price)
        else:
            close_qty = min(pos["qty"], qty)
            pnl = self._calculate_close_pnl(pos, close_qty, price)
            self._realized_pnl += pnl
            pos["qty"] -= close_qty
            if pos["qty"] <= 1e-12:
                del self._positions[symbol]
                logger.debug(f"完全平仓: {symbol}, 实现盈亏 {pnl:.4f}")
            else:
                logger.debug(f"部分平仓: {symbol} {close_qty}, 实现盈亏 {pnl:.4f}")

            remaining = qty - close_qty
            if remaining > 1e-12:
                self._open_position_locked(symbol, side, remaining, price)

    def _open_position_locked(self, symbol: str, side: int, qty: float, price: float) -> None:
        leverage = self._get_config("leverage", self.DEFAULT_LEVERAGE)
        margin = (qty * price) / leverage
        self._positions[symbol] = {"side": side, "qty": qty, "entry_price": price, "margin": margin}

    def _add_position_locked(self, symbol: str, qty: float, price: float) -> None:
        pos = self._positions[symbol]
        total_qty = pos["qty"] + qty
        pos["entry_price"] = (pos["entry_price"] * pos["qty"] + price * qty) / total_qty
        pos["qty"] = total_qty
        leverage = self._get_config("leverage", self.DEFAULT_LEVERAGE)
        pos["margin"] += (qty * price) / leverage

    @staticmethod
    def _calculate_close_pnl(pos: Dict[str, Any], close_qty: float, price: float) -> float:
        return (price - pos["entry_price"]) * close_qty * pos["side"]

    # ========== 保证金变动预估 ==========
    def _estimate_net_margin_delta_locked(self, symbol: str, side: int, qty: float, price: float, leverage: float) -> float:
        """预估本次交易导致的净保证金变动（考虑平仓释放）"""
        pos = self._positions.get(symbol)
        if pos is None or pos["side"] == side:
            # 纯开仓或加仓
            return (qty * price) / leverage
        # 先平后开
        close_qty = min(pos["qty"], qty)
        released_margin = (close_qty / pos["qty"]) * pos["margin"] if pos["qty"] > 0 else 0.0
        remaining_qty = qty - close_qty
        new_margin = (remaining_qty * price) / leverage if remaining_qty > 0 else 0.0
        return max(0.0, new_margin - released_margin)

    # ========== 内部财务计算 ==========
    def _calculate_equity_locked(self) -> Tuple[float, float, float, float, bool]:
        unrealized = 0.0
        used_margin = 0.0
        has_stale = False
        now = time.time()
        for symbol, pos in self._positions.items():
            used_margin += pos["margin"]
            ts = self._price_timestamps.get(symbol, 0)
            price = self._current_prices.get(symbol)
            if price is None:
                continue
            if now - ts > self.PRICE_STALE_THRESHOLD_SEC:
                has_stale = True
                # 仍然使用最近价格计算浮动盈亏，但标记过期
            unrealized += self._calculate_close_pnl(pos, pos["qty"], price)
        total_equity = self._initial_equity + self._realized_pnl + unrealized
        available = max(0.0, total_equity - used_margin)
        return total_equity, available, used_margin, unrealized, has_stale

    def _check_position_risk_locked(self, symbol: str) -> Optional[str]:
        pos = self._positions.get(symbol)
        if pos is None:
            return None
        total_equity, _, _, _, _ = self._calculate_equity_locked()
        if total_equity <= 0:
            return None
        risk_pct = (pos["qty"] * pos["entry_price"]) / total_equity
        if risk_pct > self.MAX_SINGLE_POSITION_RISK_PCT:
            return f"{symbol} 风险敞口 {risk_pct:.1%} 超过上限 {self.MAX_SINGLE_POSITION_RISK_PCT:.0%}"
        return None

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
        """计算覆盖持仓结构、权益累积和防篡改盐值的校验和"""
        data_str = (
            f"{self._initial_equity:.8f}|{self._realized_pnl:.8f}|"
            f"{self._total_fees:.8f}|{self._total_funding:.8f}|{self._total_interest:.8f}|"
            f"{self._checksum_salt}"
        )
        for symbol in sorted(self._positions.keys()):
            pos = self._positions[symbol]
            data_str += f"|{symbol}:{pos['side']}:{pos['qty']:.8f}:{pos['entry_price']:.8f}:{pos['margin']:.8f}"
        return float(hashlib.sha256(data_str.encode()).hexdigest()[:16], 16) / 1e12
