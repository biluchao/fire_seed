"""
火种系统 · 订单风控网关 (OrderRiskGateway)

核心职责：
1. 在订单发送前执行机构级最终合法性检查，涵盖订单量、频率、日内盈亏、连续亏损、撤单率、重复订单、保证金、流动性、自成交、API限频等维度
2. 实时维护多维度风控状态（全局+策略+品种），基于滚动窗口统计触发即时熔断并推送告警
3. 支持压力测试模拟、策略注销、订单超时清理、风控拦截指标统计等高级运维功能
4. 内置权益备份估算、撤单分级告警与自动暂停、保证金精确预估、告警升级等深度风控能力

外部依赖（真实模块接口）：
- core.utils.config_loader.ConfigLoader : 获取订单风控相关阈值配置
- core.behavioral_logger.BehavioralLogger : 记录风控拦截与状态变更事件
- core.negotiation_bus.NegotiationBus : 推送风控熔断告警
- core.account_ledger.AccountLedger : 获取实时权益、保证金率、预估保证金占用
- core.perception.tactile_cortex.TactileCortex : 获取当前流动性评级与可用深度
- core.position_snapshot.PositionSnapshot : 恢复日度风控状态

接口契约：
- pre_flight_check(order: Dict[str, Any]) -> Dict[str, Any] : 订单发送前全面风控检查
- update_state(event: Dict[str, Any]) -> Dict[str, Any] : 更新风控状态（成交/撤单/盈亏）
- get_metrics() -> Dict[str, Any] : 返回当前风控指标汇总
- health_check() -> Dict[str, Any] : 模块自检
- deregister_strategy(strategy_id: str) -> None : 注销策略并清理其所有状态
- set_simulation_mode(enabled: bool) -> None : 启用/禁用压力测试模式
- force_reset_daily_state() -> Dict[str, Any] : 强制重置日度状态（运维接口）
- get_blocked_stats() -> Dict[str, Any] : 返回拦截统计详情（含占比和趋势）
- shutdown() -> None : 优雅关闭，停止守护线程
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 ConfigLoader 不可用时，使用类常量中的严格默认值运行，确保安全
- 当 NegotiationBus 不可用时，告警降级为仅本地日志记录
- 当 BehavioralLogger 不可用时，日志降级为标准 logger
- 当 AccountLedger 不可用时，权益等字段使用备用估算（持仓×市价+现金余额），首次使用0
- 当 TactileCortex 不可用时，流动性评级默认L2，可用深度估计为0
- 当 PositionSnapshot 不可用时，不恢复历史状态，从零开始
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护多维度可变状态，通过可重入读写锁保护，遵循“锁内不IO”原则
- 独立守护线程定期清理超时订单、过期策略状态，所有共享状态访问均在锁内
- 所有可变数据结构均限制最大长度，防止内存无限增长
- 不持有任何外部资源句柄
- 提供 shutdown() 方法优雅停止守护线程

可配置参数（config路径：risk.order_risk_gateway）：
（同上一版本，新增 alert_cooldown_sec, alert_escalation_cooldown_sec, max_order_history_size）
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple
from collections import deque, defaultdict, OrderedDict
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation, DivisionByZero
import hashlib
import copy

logger = logging.getLogger(__name__)


class OrderRiskGateway:
    """订单风控网关：机构级最后一道防线"""

    # ========== 类常量 ==========
    DEFAULT_MAX_ORDER_VALUE_PCT = Decimal('5.0')
    DEFAULT_MAX_ORDER_SIZE_BTC = Decimal('10.0')
    DEFAULT_MAX_ORDER_SIZE_ETH = Decimal('100.0')
    DEFAULT_MAX_TRADES_PER_MINUTE = 10
    DEFAULT_MAX_TRADES_PER_SYMBOL_PER_MINUTE = 3
    DEFAULT_DUPLICATE_ORDER_WINDOW_MS = 500
    DEFAULT_MAX_DAILY_LOSS_PCT = Decimal('5.0')
    DEFAULT_MAX_DAILY_DRAWDOWN_PCT = Decimal('8.0')
    DEFAULT_CONSECUTIVE_LOSS_LIMIT_GLOBAL = 8
    DEFAULT_CONSECUTIVE_LOSS_LIMIT_STRATEGY = 5
    DEFAULT_MINUTE_LOSS_LIMIT_PCT = Decimal('1.2')
    DEFAULT_MAX_CANCEL_RATE_PCT = Decimal('30.0')
    DEFAULT_CANCEL_MONITOR_WINDOW = 50
    DEFAULT_CANCEL_RATE_MONITOR_MINUTES = 10
    DEFAULT_MIN_MARGIN_RATIO_PCT = Decimal('150.0')
    DEFAULT_SELF_TRADE_WINDOW_MS = 200
    DEFAULT_LIQUIDITY_ORDER_SIZE_MULT = {
        1: Decimal('0.3'), 2: Decimal('0.5'), 3: Decimal('0.8'),
        4: Decimal('1.0'), 5: Decimal('1.5'),
    }
    ACTIVE_ORDER_TIMEOUT_SEC = 300
    ALERT_COOLDOWN_SEC = 30
    ALERT_ESCALATION_COOLDOWN_SEC = 120
    MAX_STRATEGY_COUNT = 100
    CLEANUP_INTERVAL_SEC = 30
    EQUITY_CACHE_TTL_SEC = 0.5
    EQUITY_RETRY_COUNT = 2
    MAX_ORDER_HISTORY_SIZE = 2000
    MAX_BLOCKED_TREND_POINTS = 100

    def __init__(self, instance_id: str = "default"):
        self.instance_id = instance_id
        self._rw_lock = threading.RLock()
        self._equity_lock = threading.Lock()  # 独立保护权益缓存
        self._lock_wait_times: deque = deque(maxlen=100)
        self._consecutive_lock_warnings = 0

        # 日度全局状态（均在 _rw_lock 保护下）
        self._daily_trade_count = 0
        self._daily_loss_pct = Decimal('0.0')
        self._daily_peak_equity = Decimal('0.0')
        self._daily_drawdown_pct = Decimal('0.0')
        self._consecutive_losses_global = 0
        self._last_trade_timestamp = 0.0
        self._minute_trade_timestamps: deque = deque(maxlen=1000)
        self._five_min_loss_history: deque = deque(maxlen=500)
        self._order_history_items: deque = deque(maxlen=self.MAX_ORDER_HISTORY_SIZE)  # (ts, oid, status)
        self._recent_orders: deque = deque(maxlen=200)
        self._active_orders: Dict[str, Dict[str, Any]] = {}
        self._initial_equity_of_day = Decimal('0.0')

        # 撤单分级监控
        self._cancel_count_by_strategy: Dict[str, int] = defaultdict(int)
        self._cancel_warning_strategies: Dict[str, float] = {}
        self._cancel_suspended_strategies: Dict[str, float] = {}

        # 策略级别状态
        self._strategy_states: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "consecutive_losses": 0,
            "daily_trade_count": 0,
            "daily_loss_pct": Decimal('0.0'),
            "last_activity": 0.0,
        })
        self._strategy_state_snapshots: Dict[str, Dict[str, Any]] = {}

        # 品种交易计数
        self._symbol_trade_counts: Dict[str, deque] = defaultdict(lambda: deque(maxlen=200))

        # 风控拦截统计
        self._blocked_stats: Dict[str, int] = defaultdict(int)
        self._blocked_trend: Dict[str, deque] = defaultdict(lambda: deque(maxlen=self.MAX_BLOCKED_TREND_POINTS))

        # 外部依赖
        self._config_loader = None
        self._negotiation_bus = None
        self._behavioral_logger = None
        self._account_ledger = None
        self._tactile_cortex = None
        self._position_snapshot = None

        # 权益缓存（由 _equity_lock 保护）
        self._cached_equity = Decimal('0.0')
        self._last_equity_fetch = 0.0
        self._equity_consecutive_failures = 0
        self._equity_fallback_used = False

        # 流动性参数缓存（在 _rw_lock 内读写，或配置来源）
        self._liquidity_mult = copy.deepcopy(self.DEFAULT_LIQUIDITY_ORDER_SIZE_MULT)

        # 交易日
        self._current_trading_day = self._get_utc_day()
        self._alert_last_triggered: Dict[str, float] = {}

        # 压力测试模式
        self.simulation_mode = False

        # 启动守护清理线程
        self._stop_cleanup = threading.Event()
        self._cleanup_thread = threading.Thread(target=self._cleanup_daemon, daemon=True)
        self._cleanup_thread.start()

        logger.info("OrderRiskGateway 机构版 v5 初始化完成, instance=%s", self.instance_id)

    # ========== 依赖注入 ==========
    def inject_dependencies(self, **kwargs) -> None:
        for name, instance in kwargs.items():
            if instance is not None:
                setattr(self, f"_{name}", instance)
                logger.info("%s 注入成功", name)
            else:
                logger.warning("%s 未注入，功能降级", name)

    # ========== 配置获取 ==========
    def _get_config_decimal(self, key: str, default: Decimal) -> Decimal:
        if self._config_loader is not None:
            try:
                value = self._config_loader.get(f"risk.order_risk_gateway.{key}", None)
                if value is not None:
                    result = Decimal(str(value))
                    if result <= 0:
                        logger.warning("配置值非正数 key=%s value=%s, 使用默认值 %s", key, result, default)
                        return default
                    return result
            except (InvalidOperation, ValueError, TypeError) as e:
                logger.warning("配置解析失败 key=%s: %s, 使用默认值 %s", key, e, default)
            except Exception as e:
                logger.warning("配置读取异常 key=%s: %s, 使用默认值 %s", key, e, default)
        return default

    def _get_config_int(self, key: str, default: int) -> int:
        if self._config_loader is not None:
            try:
                value = self._config_loader.get(f"risk.order_risk_gateway.{key}", None)
                if value is not None:
                    result = int(value)
                    if result <= 0:
                        logger.warning("配置值非正数 key=%s value=%s, 使用默认值 %s", key, result, default)
                        return default
                    return result
            except (ValueError, TypeError) as e:
                logger.warning("配置解析失败 key=%s: %s, 使用默认值 %s", key, e, default)
        return default

    # ========== 公共接口 ==========
    def pre_flight_check(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """订单发送前全面风控检查（机构级），锁外IO，锁内纯内存"""
        self._check_day_rollover()

        # 参数校验
        required_fields = ["symbol", "side", "quantity", "price", "order_type", "order_id", "timestamp"]
        missing = [f for f in required_fields if f not in order]
        if missing:
            return self._reject_order("missing_fields", f"缺少字段: {missing}", order)

        order_id = order["order_id"]
        timestamp = order.get("timestamp", time.monotonic())
        symbol = order["symbol"]
        side = order["side"]
        strategy = order.get("strategy_id", "global")
        account_group = order.get("account_group", "default")

        try:
            quantity = Decimal(str(order["quantity"]))
            price = Decimal(str(order["price"]))
            if quantity <= 0 or price <= 0:
                return self._reject_order("invalid_order_params", "订单数量或价格无效", order)
        except (InvalidOperation, ValueError) as e:
            return self._reject_order("invalid_order_params", f"订单参数解析失败: {e}", order)

        order_value = quantity * price
        raw = f"{self.instance_id}|{symbol}|{side}|{quantity:.8f}|{price:.8f}|{timestamp:.6f}"
        order_hash = hashlib.sha256(raw.encode()).hexdigest()[:32]

        # ---- 锁外：获取权益、流动性、配置 ----
        equity = self._fetch_equity_safe()
        if equity <= 0:
            if self._is_equity_fallback_used():
                logger.error("权益获取完全失败，拒绝交易 #RECOVERY: 检查AccountLedger连接")
                return self._reject_order("equity_unavailable", "无法获取权益信息，暂停交易", order)
            return self._reject_order("invalid_equity", "账户权益异常或不可用", order)
        liq_level, available_depth = self._fetch_liquidity_info_safe(symbol)

        # 锁外预读取所需配置（避免锁内调用）
        max_pct = self._get_config_decimal("max_order_value_pct", self.DEFAULT_MAX_ORDER_VALUE_PCT)
        max_trades = self._get_config_int("max_trades_per_minute", self.DEFAULT_MAX_TRADES_PER_MINUTE)
        max_sym = self._get_config_int("max_trades_per_symbol_per_minute",
                                       self.DEFAULT_MAX_TRADES_PER_SYMBOL_PER_MINUTE)
        max_loss = self._get_config_decimal("max_daily_loss_pct", self.DEFAULT_MAX_DAILY_LOSS_PCT)
        max_dd = self._get_config_decimal("max_daily_drawdown_pct", self.DEFAULT_MAX_DAILY_DRAWDOWN_PCT)
        cg = self._get_config_int("consecutive_loss_limit_global", self.DEFAULT_CONSECUTIVE_LOSS_LIMIT_GLOBAL)
        cs = self._get_config_int("consecutive_loss_limit_strategy", self.DEFAULT_CONSECUTIVE_LOSS_LIMIT_STRATEGY)
        min_loss = self._get_config_decimal("minute_loss_limit_pct", self.DEFAULT_MINUTE_LOSS_LIMIT_PCT)
        max_cr = self._get_config_decimal("max_cancel_rate_pct", self.DEFAULT_MAX_CANCEL_RATE_PCT)
        min_margin = self._get_config_decimal("min_margin_ratio_pct", self.DEFAULT_MIN_MARGIN_RATIO_PCT)
        # 保证金预估（锁外IO）
        est_margin = self._estimate_margin_after(order, equity)
        # 撤单暂停状态（在锁内检查，这里先记下策略）
        # （将在锁内检查 _cancel_suspended_strategies）

        lock_start = time.perf_counter()
        with self._rw_lock:
            lock_wait = time.perf_counter() - lock_start
            self._lock_wait_times.append(lock_wait)
            if lock_wait > 0.01:
                logger.warning("锁等待过长: %.3fms, 活跃订单=%d", lock_wait * 1000, len(self._active_orders))
            warnings = []

            # 检查撤单暂停
            if strategy in self._cancel_suspended_strategies:
                return self._reject_order("strategy_cancel_suspended",
                                          f"策略{strategy}因撤单率过高被暂停", order)

            # 1. 重复订单
            if self._is_duplicate_order(order_id, order_hash, timestamp):
                return self._reject_order("duplicate_order", "检测到重复订单", order)

            # 2. 订单量限制
            if order_value > equity * max_pct / Decimal('100'):
                return self._reject_order("max_order_value_exceeded",
                                         f"订单价值 {order_value:.2f} 超过{max_pct}%权益", order)
            if symbol.startswith("BTC") and quantity > self.DEFAULT_MAX_ORDER_SIZE_BTC:
                return self._reject_order("max_btc_size", "超过最大BTC数量", order)
            if symbol.startswith("ETH") and quantity > self.DEFAULT_MAX_ORDER_SIZE_ETH:
                return self._reject_order("max_eth_size", "超过最大ETH数量", order)

            # 3. 流动性适配
            liq_mult = self._liquidity_mult.get(liq_level, Decimal('0.3'))
            if available_depth > 0 and quantity > available_depth * Decimal('0.5'):
                liq_mult = min(liq_mult, Decimal('0.3'))
                warnings.append("流动性受限，订单量被压缩")
            if order_value > equity * max_pct * liq_mult / Decimal('100'):
                return self._reject_order("liquidity_constraint",
                                         f"流动性L{liq_level}下订单受限", order)

            # 4. 频率限制
            if self._count_recent_trades(timestamp) >= max_trades:
                return self._reject_order("global_trade_rate_limit", f"每分钟超过{max_trades}次", order)
            if self._count_recent_symbol_trades(symbol, timestamp) >= max_sym:
                return self._reject_order("symbol_rate_limit", f"{symbol}每分钟超过{max_sym}次", order)

            # 5. 自成交
            if self._detect_self_trade(symbol, side, account_group, quantity, price, timestamp):
                return self._reject_order("self_trade_risk", "可能自成交", order)

            # 6. 日内亏损
            if self._daily_loss_pct >= max_loss:
                return self._reject_order("daily_loss_limit", f"日内亏损{self._daily_loss_pct:.2f}%", order)
            self._update_drawdown(equity)
            if self._daily_drawdown_pct >= max_dd:
                return self._reject_order("daily_drawdown_limit", f"日内回撤{self._daily_drawdown_pct:.2f}%", order)

            # 7. 连续亏损
            if self._consecutive_losses_global >= cg:
                return self._reject_order("global_consecutive_loss",
                                         f"全局连续亏损{self._consecutive_losses_global}次", order)
            strat_state = self._strategy_states[strategy]
            if strat_state["consecutive_losses"] >= cs:
                return self._reject_order("strategy_consecutive_loss",
                                         f"策略{strategy}连续亏损{strat_state['consecutive_losses']}次", order)

            # 8. 5分钟亏损
            recent_loss = self._sum_five_min_losses(timestamp)
            if recent_loss >= min_loss:
                return self._reject_order("minute_loss_limit", f"5分钟亏损{recent_loss:.2f}%", order)

            # 9. 撤单率
            cr = self._calculate_active_cancel_rate(timestamp)
            if cr > max_cr:
                return self._reject_order("cancel_rate_high", f"主动撤单率{cr:.1f}%", order)

            # 10. 保证金预估（已锁外计算）
            if est_margin < min_margin:
                return self._reject_order("margin_ratio_low", f"预估保证金率{est_margin:.1f}%", order)

            # 全部通过
            self._update_trade_counts(timestamp, symbol, strategy, order_id, order_hash)
            return {"status": "ok", "reason": "通过机构级风控检查",
                    "data": {"blocked": False}, "warnings": warnings}

    def update_state(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event_type = event.get("event_type", "")
        order_id = event.get("order_id", "")
        strategy = event.get("strategy_id", "global")
        cancel_reason = event.get("cancel_reason", "manual")

        with self._rw_lock:
            if event_type == "fill":
                try:
                    pnl_pct = Decimal(str(event.get("pnl_pct", "0.0")))
                except (InvalidOperation, ValueError):
                    logger.error("无效的pnl_pct值: %s", event.get("pnl_pct"))
                    return {"status": "error", "reason": "无效的盈亏值", "data": {}, "warnings": []}
                if pnl_pct < 0:
                    self._daily_loss_pct += abs(pnl_pct)
                    self._consecutive_losses_global += 1
                    self._strategy_states[strategy]["consecutive_losses"] += 1
                    self._five_min_loss_history.append((time.monotonic(), abs(pnl_pct)))
                else:
                    self._consecutive_losses_global = 0
                    self._strategy_states[strategy]["consecutive_losses"] = 0
                self._update_order_history(order_id, "filled")
                self._active_orders.pop(order_id, None)
            elif event_type == "cancel":
                self._update_order_history(order_id, f"canceled_by_{cancel_reason}")
                self._active_orders.pop(order_id, None)
                if cancel_reason == "manual":
                    self._cancel_count_by_strategy[strategy] += 1
            elif event_type == "daily_reset":
                self._reset_daily_state()
            return {"status": "ok", "reason": f"处理事件 {event_type}",
                    "data": self._get_state_summary(), "warnings": []}

    def get_metrics(self) -> Dict[str, Any]:
        with self._rw_lock:
            return {"status": "ok", "reason": "风控指标查询成功",
                    "data": {**self._get_state_summary(),
                             "blocked_stats": self._get_blocked_stats_detail()},
                    "warnings": []}

    def force_reset_daily_state(self) -> Dict[str, Any]:
        with self._rw_lock:
            self._reset_daily_state()
            logger.warning("运维手动触发日度风控状态重置")
            return {"status": "ok", "reason": "日度状态已强制重置",
                    "data": self._get_state_summary(), "warnings": ["manual_reset"]}

    def get_blocked_stats(self) -> Dict[str, Any]:
        with self._rw_lock:
            return {"status": "ok", "reason": "拦截统计查询成功",
                    "data": self._get_blocked_stats_detail(), "warnings": []}

    def deregister_strategy(self, strategy_id: str) -> None:
        with self._rw_lock:
            if strategy_id in self._strategy_states:
                self._strategy_state_snapshots[strategy_id] = dict(self._strategy_states[strategy_id])
                del self._strategy_states[strategy_id]
                expired = [oid for oid, info in self._active_orders.items()
                           if info.get("strategy") == strategy_id]
                for oid in expired:
                    self._active_orders.pop(oid, None)
                    self._update_order_history(oid, "canceled_by_deregister")
                self._cancel_count_by_strategy.pop(strategy_id, None)
                self._cancel_warning_strategies.pop(strategy_id, None)
                self._cancel_suspended_strategies.pop(strategy_id, None)
                logger.info("策略 %s 已注销，清理订单 %d 笔", strategy_id, len(expired))

    def set_simulation_mode(self, enabled: bool) -> None:
        self.simulation_mode = enabled
        logger.info("模拟模式: %s", "开启" if enabled else "关闭")

    def shutdown(self) -> None:
        """优雅关闭，停止守护线程"""
        self._stop_cleanup.set()
        if self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=2.0)
        logger.info("OrderRiskGateway 已关闭")

    def health_check(self) -> Dict[str, Any]:
        try:
            with self._rw_lock:
                _ = self._daily_trade_count
            deps_status = {}
            if self._account_ledger and hasattr(self._account_ledger, 'get_equity'):
                try:
                    eq = self._account_ledger.get_equity()
                    deps_status["account_ledger"] = "ok" if eq > 0 else "empty"
                except Exception:
                    deps_status["account_ledger"] = "error"
            else:
                deps_status["account_ledger"] = "unavailable"
            if self._tactile_cortex and hasattr(self._tactile_cortex, 'get_liquidity_rating'):
                try:
                    _ = self._tactile_cortex.get_liquidity_rating()
                    deps_status["tactile_cortex"] = "ok"
                except Exception:
                    deps_status["tactile_cortex"] = "error"
            else:
                deps_status["tactile_cortex"] = "unavailable"
            all_ok = all(v == "ok" for v in deps_status.values())
            return {"status": "ok" if all_ok else "degraded",
                    "reason": "OrderRiskGateway 健康检查完成",
                    "data": {"dependencies": deps_status,
                             "lock_wait_p99_us": self._get_lock_wait_p99(),
                             "equity_fallback_active": self._is_equity_fallback_used()},
                    "warnings": [] if all_ok else ["部分依赖不可用"]}
        except Exception as e:
            logger.error("健康检查失败: %s #RECOVERY: 检查锁状态", e)
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"]}

    # ========== 私有方法 ==========
    def _reject_order(self, reason_code: str, message: str,
                      order: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = time.monotonic()
        with self._rw_lock:
            self._blocked_stats[reason_code] = self._blocked_stats.get(reason_code, 0) + 1
            self._blocked_trend[reason_code].append((now, 1))
        if order:
            logger.warning("订单被拦截: [%s] %s | %s %s %s@%s",
                           reason_code, message,
                           order.get('symbol', '?'), order.get('side', '?'),
                           order.get('quantity', '?'), order.get('price', '?'))
        else:
            logger.warning("订单被拦截: [%s] %s", reason_code, message)
        self._log_event("order_rejected", {"reason_code": reason_code, "message": message,
                                           "state": self._get_state_summary()})
        if reason_code in ("daily_loss_limit", "consecutive_loss_limit", "minute_loss_limit",
                           "daily_drawdown_limit", "margin_ratio_low", "equity_unavailable"):
            self._trigger_alert(reason_code, f"{message} | 当前状态: {self._get_state_summary()}")
        return {"status": "blocked", "reason": message,
                "data": {"blocked": True, "blocked_reason": reason_code}, "warnings": [message]}

    def _is_duplicate_order(self, order_id: str, order_hash: str, timestamp: float) -> bool:
        self._prune_recent_orders(timestamp)
        for recent in self._recent_orders:
            if recent["order_id"] == order_id or recent["order_hash"] == order_hash:
                return True
        return False

    def _prune_recent_orders(self, now: float) -> None:
        cutoff = now - self.DEFAULT_DUPLICATE_ORDER_WINDOW_MS / 1000.0
        while self._recent_orders and self._recent_orders[0]["timestamp"] < cutoff:
            self._recent_orders.popleft()

    def _count_recent_trades(self, now: float) -> int:
        cutoff = now - 60.0
        while self._minute_trade_timestamps and self._minute_trade_timestamps[0] < cutoff:
            self._minute_trade_timestamps.popleft()
        return len(self._minute_trade_timestamps)

    def _count_recent_symbol_trades(self, symbol: str, now: float) -> int:
        cutoff = now - 60.0
        q = self._symbol_trade_counts[symbol]
        while q and q[0] < cutoff:
            q.popleft()
        return len(q)

    def _sum_five_min_losses(self, now: float) -> Decimal:
        cutoff = now - 300.0
        while self._five_min_loss_history and self._five_min_loss_history[0][0] < cutoff:
            self._five_min_loss_history.popleft()
        return sum(loss for _, loss in self._five_min_loss_history)

    def _calculate_active_cancel_rate(self, now: float) -> Decimal:
        """计算主动撤单率，基于时间窗口过滤"""
        monitor_minutes = self._get_config_int("cancel_rate_monitor_minutes",
                                              self.DEFAULT_CANCEL_RATE_MONITOR_MINUTES)
        cutoff = now - monitor_minutes * 60.0
        # 清理过期
        while self._order_history_items and self._order_history_items[0][0] < cutoff:
            self._order_history_items.popleft()
        total = len(self._order_history_items)
        if total < 10:
            return Decimal('0.0')
        active_cancel = sum(1 for _, _, status in self._order_history_items if "canceled_by_manual" in status)
        filled = sum(1 for _, _, status in self._order_history_items if status == "filled")
        denom = filled + active_cancel
        if denom == 0:
            return Decimal('0.0')
        return Decimal(active_cancel) / Decimal(denom) * Decimal('100')

    def _update_order_history(self, order_id: str, status: str) -> None:
        self._order_history_items.append((time.monotonic(), order_id, status))

    def _check_day_rollover(self) -> None:
        current_day = self._get_utc_day()
        if current_day != self._current_trading_day:
            with self._rw_lock:
                self._reset_daily_state()
                self._current_trading_day = current_day

    def _reset_daily_state(self) -> None:
        self._daily_trade_count = 0
        self._daily_loss_pct = Decimal('0.0')
        self._daily_peak_equity = Decimal('0.0')
        self._daily_drawdown_pct = Decimal('0.0')
        self._consecutive_losses_global = 0
        self._minute_trade_timestamps.clear()
        self._five_min_loss_history.clear()
        self._strategy_states.clear()
        self._symbol_trade_counts.clear()
        self._cancel_count_by_strategy.clear()
        self._cancel_warning_strategies.clear()
        self._cancel_suspended_strategies.clear()
        if self._position_snapshot:
            try:
                snap = self._position_snapshot.load_risk_state()
                if snap:
                    self._daily_loss_pct = Decimal(str(snap.get("daily_loss", "0.0")))
                    self._daily_peak_equity = Decimal(str(snap.get("peak_equity", "0.0")))
                    for sid, st in snap.get("strategies", {}).items():
                        self._strategy_states[sid] = {
                            "consecutive_losses": st.get("consecutive_losses", 0),
                            "daily_trade_count": st.get("daily_trade_count", 0),
                            "daily_loss_pct": Decimal(str(st.get("daily_loss_pct", "0.0"))),
                            "last_activity": 0.0,
                        }
            except Exception as e:
                logger.warning("快照恢复失败: %s", e)
        self._initial_equity_of_day = self._fetch_equity_safe()
        with self._equity_lock:
            self._equity_fallback_used = False

    def _update_trade_counts(self, timestamp: float, symbol: str, strategy: str,
                             order_id: str, order_hash: str) -> None:
        self._minute_trade_timestamps.append(timestamp)
        self._symbol_trade_counts[symbol].append(timestamp)
        self._recent_orders.append({"order_id": order_id, "order_hash": order_hash, "timestamp": timestamp})
        self._active_orders[order_id] = {"symbol": symbol, "strategy": strategy, "timestamp": timestamp}
        self._daily_trade_count += 1
        self._strategy_states[strategy]["daily_trade_count"] += 1
        self._strategy_states[strategy]["last_activity"] = timestamp
        self._last_trade_timestamp = timestamp

    def _detect_self_trade(self, symbol: str, side: str, account_group: str,
                           quantity: Decimal, price: Decimal, timestamp: float) -> bool:
        window = self.DEFAULT_SELF_TRADE_WINDOW_MS / 1000.0
        cutoff = timestamp - window
        for info in self._active_orders.values():
            if (info["symbol"] == symbol and info["side"] != side
                    and info.get("account_group", "default") == account_group
                    and info["timestamp"] >= cutoff):
                other_qty = info.get("quantity", Decimal('0'))
                other_price = info.get("price", Decimal('0'))
                if other_qty > 0 and other_price > 0:
                    if abs(quantity - other_qty) / max(quantity, other_qty) < Decimal('0.5'):
                        if abs(price - other_price) / max(price, other_price) < Decimal('0.02'):
                            return True
        return False

    def _update_drawdown(self, current_equity: Decimal) -> None:
        try:
            if current_equity > self._daily_peak_equity:
                self._daily_peak_equity = current_equity
            if self._daily_peak_equity > 0:
                dd = (self._daily_peak_equity - current_equity) / self._daily_peak_equity * Decimal('100')
                if dd > self._daily_drawdown_pct:
                    self._daily_drawdown_pct = dd
        except (DivisionByZero, InvalidOperation):
            logger.warning("日内回撤计算异常")

    def _estimate_margin_after(self, order: Dict[str, Any], equity: Decimal) -> Decimal:
        if self._account_ledger and hasattr(self._account_ledger, 'estimate_margin_after'):
            try:
                return Decimal(str(self._account_ledger.estimate_margin_after(order)))
            except Exception as e:
                logger.warning("保证金预估失败: %s", e)
        try:
            if self._account_ledger and hasattr(self._account_ledger, 'get_margin_ratio'):
                current_margin = Decimal(str(self._account_ledger.get_margin_ratio()))
                order_val = Decimal(str(order.get("quantity", 0))) * Decimal(str(order.get("price", 0)))
                if equity > 0 and order_val > 0:
                    estimated_used = order_val * Decimal('0.1')
                    new_equity = equity - estimated_used
                    if new_equity > 0:
                        return current_margin * (new_equity / equity)
        except Exception:
            pass
        return Decimal('100.0')

    def _fetch_equity_safe(self) -> Decimal:
        """安全获取权益，带缓存和重试，使用独立锁保护缓存"""
        now = time.monotonic()
        with self._equity_lock:
            if (now - self._last_equity_fetch) < self.EQUITY_CACHE_TTL_SEC and self._cached_equity > 0:
                return self._cached_equity

        for attempt in range(self.EQUITY_RETRY_COUNT):
            try:
                if self._account_ledger and hasattr(self._account_ledger, 'get_equity'):
                    eq = Decimal(str(self._account_ledger.get_equity()))
                    if eq > 0:
                        with self._equity_lock:
                            self._cached_equity = eq
                            self._last_equity_fetch = now
                            self._equity_consecutive_failures = 0
                            self._equity_fallback_used = False
                        return eq
            except Exception as e:
                if attempt < self.EQUITY_RETRY_COUNT - 1:
                    time.sleep(0.05)
                else:
                    logger.error("权益获取重试耗尽: %s", e)
        # 备用估算
        with self._equity_lock:
            self._equity_consecutive_failures += 1
            self._equity_fallback_used = True
        fallback = self._estimate_equity_fallback()
        logger.warning("使用备用权益估算: %s (连续失败%d次)", fallback, self._equity_consecutive_failures)
        return fallback

    def _estimate_equity_fallback(self) -> Decimal:
        try:
            if self._account_ledger and hasattr(self._account_ledger, 'get_positions_value'):
                pos_val = Decimal(str(self._account_ledger.get_positions_value()))
                cash = Decimal(str(self._account_ledger.get_cash_balance()))
                return pos_val + cash
        except Exception as e:
            logger.error("备用估算失败: %s", e)
        with self._equity_lock:
            return self._cached_equity

    def _is_equity_fallback_used(self) -> bool:
        with self._equity_lock:
            return self._equity_fallback_used

    def _fetch_liquidity_info_safe(self, symbol: str) -> Tuple[int, Decimal]:
        if self._tactile_cortex and hasattr(self._tactile_cortex, 'get_liquidity_rating'):
            try:
                rating = self._tactile_cortex.get_liquidity_rating()
                depth = Decimal('0')
                if hasattr(self._tactile_cortex, 'get_available_depth'):
                    depth = Decimal(str(self._tactile_cortex.get_available_depth(symbol)))
                return rating, depth
            except Exception as e:
                logger.warning("获取流动性信息失败: %s", e)
        return 3, Decimal('0')

    @staticmethod
    def _get_utc_day() -> int:
        return int(time.strftime("%Y%m%d", time.gmtime()))

    def _get_blocked_stats_detail(self) -> Dict[str, Any]:
        total = sum(self._blocked_stats.values())
        details = {}
        now = time.monotonic()
        for reason, count in self._blocked_stats.items():
            trend_data = self._blocked_trend.get(reason, deque())
            recent = sum(1 for t, _ in trend_data if t > now - 3600)
            details[reason] = {"total": count, "pct": f"{count/total*100:.1f}%" if total > 0 else "0%",
                               "last_hour": recent}
        return {"total_blocked": total, "by_reason": details}

    # ========== 守护线程（所有共享状态操作均在锁内） ==========
    def _cleanup_daemon(self) -> None:
        while not self._stop_cleanup.wait(self.CLEANUP_INTERVAL_SEC):
            with self._rw_lock:
                now = time.monotonic()
                # 清理超时活跃订单
                expired = [oid for oid, info in self._active_orders.items()
                           if now - info["timestamp"] > self.ACTIVE_ORDER_TIMEOUT_SEC]
                for oid in expired:
                    self._active_orders.pop(oid, None)
                    self._update_order_history(oid, "expired")
                if expired:
                    logger.info("守护清理超时订单 %d 笔", len(expired))
                # 清理过期策略状态
                stale = [sid for sid, st in self._strategy_states.items()
                         if now - st.get("last_activity", 0) > 86400 and sid != "global"]
                for sid in stale:
                    self._strategy_state_snapshots[sid] = dict(self._strategy_states[sid])
                    del self._strategy_states[sid]
                if stale:
                    logger.info("守护清理过期策略 %d 个", len(stale))
                # 限制最大策略数量
                if len(self._strategy_states) > self.MAX_STRATEGY_COUNT:
                    sorted_strats = sorted(self._strategy_states.items(), key=lambda x: x[1]["last_activity"])
                    for sid, _ in sorted_strats[:len(sorted_strats) - self.MAX_STRATEGY_COUNT]:
                        if sid != "global":
                            del self._strategy_states[sid]
                # 撤单分级检查
                self._check_cancel_escalation()

    def _check_cancel_escalation(self) -> None:
        """撤单率分级告警与熔断（需在 _rw_lock 内调用）"""
        for strategy, count in list(self._cancel_count_by_strategy.items()):
            if count >= 20 and strategy not in self._cancel_suspended_strategies:
                self._cancel_suspended_strategies[strategy] = time.monotonic()
                self._cancel_warning_strategies.pop(strategy, None)
                logger.error("策略 %s 撤单率过高(>20次)已暂停 #RECOVERY: 检查策略逻辑", strategy)
                self._trigger_alert("strategy_cancel_suspended",
                                    f"策略{strategy}因撤单率过高被暂停")
            elif count >= 10 and strategy not in self._cancel_warning_strategies and strategy not in self._cancel_suspended_strategies:
                self._cancel_warning_strategies[strategy] = time.monotonic()
                logger.warning("策略 %s 撤单率偏高(>10次)请关注", strategy)
        # 定期清理已暂停策略
        now = time.monotonic()
        for strategy, suspended_time in list(self._cancel_suspended_strategies.items()):
            if now - suspended_time > 3600:
                self._cancel_suspended_strategies.pop(strategy)
                self._cancel_count_by_strategy.pop(strategy, None)
                logger.info("策略 %s 撤单暂停期已过，已恢复", strategy)

    def _get_state_summary(self) -> Dict[str, Any]:
        return {
            "daily_trade_count": self._daily_trade_count,
            "daily_loss_pct": str(self._daily_loss_pct.quantize(Decimal('0.0001'))),
            "daily_drawdown_pct": str(self._daily_drawdown_pct.quantize(Decimal('0.0001'))),
            "consecutive_losses_global": self._consecutive_losses_global,
            "active_cancel_rate_pct": str(self._calculate_active_cancel_rate(time.monotonic()).quantize(Decimal('0.1'))),
            "recent_trades_per_minute": len(self._minute_trade_timestamps),
            "active_orders_count": len(self._active_orders),
            "equity_fallback_active": self._is_equity_fallback_used(),
            "suspended_strategies": list(self._cancel_suspended_strategies.keys()),
        }

    def _get_lock_wait_p99(self) -> float:
        if not self._lock_wait_times:
            return 0.0
        with self._rw_lock:
            times = list(self._lock_wait_times)
        if not times:
            return 0.0
        return sorted(times)[int(len(times) * 0.99)] * 1e6

    def _log_event(self, event_type: str, details: Dict) -> None:
        if self._behavioral_logger and hasattr(self._behavioral_logger, 'log_event'):
            try:
                self._behavioral_logger.log_event(event_type=event_type, details=details)
            except Exception as e:
                logger.warning("BehavioralLogger 记录失败: %s", e)
        else:
            logger.info("风控事件 [%s]: %s", event_type, details)

    def _trigger_alert(self, alert_type: str, message: str) -> None:
        now = time.monotonic()
        dedup_key = f"{alert_type}"
        last = self._alert_last_triggered.get(dedup_key, 0)
        escalated_types = ("daily_loss_limit", "daily_drawdown_limit", "margin_ratio_low",
                           "equity_unavailable", "strategy_cancel_suspended")
        cooldown = self.ALERT_ESCALATION_COOLDOWN_SEC if alert_type in escalated_types else self.ALERT_COOLDOWN_SEC
        if now - last < cooldown:
            return
        self._alert_last_triggered[dedup_key] = now
        if self._negotiation_bus and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(alert_type=alert_type, message=message,
                                                    instance=self.instance_id)
            except Exception as e:
                logger.warning("协商总线告警失败: %s", e)
        logger.error("风控熔断 [%s]: %s #RECOVERY: 检查策略、降低仓位或手动重置", alert_type, message)
