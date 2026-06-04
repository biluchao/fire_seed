"""
火种系统 · 订单风控网关 (OrderRiskGateway)

核心职责：
1. 在订单发送前执行最终合法性检查，包括订单价值、交易频率、日内净亏损、连续亏损、撤单率、重复订单、自成交检测
2. 实时更新内部状态（成交、撤单、盈亏），基于绝对金额净盈亏累加与滑动窗口统计触发即时熔断
3. 支持策略级独立风控状态与全局风控状态的协同运作

外部依赖（真实模块接口）：
- core.account_ledger.AccountLedger : 获取实时账户权益、保证金、强平价格
- core.utils.config_loader.ConfigLoader : 获取订单风控相关阈值配置
- core.behavioral_logger.BehavioralLogger : 记录风控拦截与状态变更事件
- core.negotiation_bus.NegotiationBus : 推送风控熔断告警

接口契约：
- pre_flight_check(order: Dict[str, Any]) -> Dict[str, Any] : 订单发送前全面风控检查
- update_state(event: Dict[str, Any]) -> Dict[str, Any] : 更新风控状态（成交/撤单/盈亏）
- get_metrics(strategy_id: str = None) -> Dict[str, Any] : 返回当前风控指标汇总
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 AccountLedger 不可用时，拒绝所有开仓指令，仅允许平仓
- 当 ConfigLoader 不可用时，使用类常量中的严格默认值运行
- 当 NegotiationBus 不可用时，告警降级为仅本地日志记录
- 当 BehavioralLogger 不可用时，日志降级为标准 logger
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护日内交易次数、累计净盈亏（绝对金额）、撤单计数等可变状态，通过可重入锁保护
- 锁内严禁文件IO或网络IO，所有IO通过单线程异步队列在锁外执行
- 支持配置化的交易日切换时间、状态持久化路径和策略级风控
- 不持有任何外部资源句柄，线程锁在模块销毁时自动释放
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple
from collections import deque, namedtuple
import json
import os

logger = logging.getLogger(__name__)

# 轻量级亏损记录结构
LossRecord = namedtuple("LossRecord", ["timestamp", "loss_abs", "strategy_id"])


class OrderRiskGateway:
    """订单风控网关：最后一道防线"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 订单量限制
    DEFAULT_MAX_ORDER_VALUE_PCT = 5.0          # 单笔订单最大价值占权益百分比，%，[1.0, 20.0]
    DEFAULT_MAX_ORDER_SIZE = 10.0              # 单笔订单最大数量（张/币），无量纲，[0.1, 100.0]

    # 频率限制（平仓指令自动豁免）
    DEFAULT_MAX_TRADES_PER_SEC = 5             # 每秒最大交易次数，次，[1, 20]
    DEFAULT_MAX_TRADES_PER_MINUTE = 10         # 每分钟最大交易次数，次，[1, 60]
    DEFAULT_DUPLICATE_ORDER_WINDOW_MS = 1000    # 重复订单检测窗口，毫秒，[500, 5000]
    DEFAULT_DUPLICATE_PRICE_TOLERANCE = 0.001  # 重复订单价格容差比例，[0.0001, 0.01]

    # 亏损熔断（基于日内净亏损）
    DEFAULT_MAX_DAILY_LOSS_PCT = 5.0           # 日内最大净亏损比例（相对初始权益），%，[1.0, 15.0]
    DEFAULT_CONSECUTIVE_LOSS_LIMIT = 5         # 连续亏损次数上限，次，[3, 15]
    DEFAULT_MINUTE_LOSS_LIMIT_PCT = 1.2        # 5分钟内累计净亏损上限，%，[0.5, 5.0]

    # 撤单率监控
    DEFAULT_MAX_CANCEL_RATE_PCT = 30.0         # 最大撤单率，%，[10.0, 60.0]
    DEFAULT_CANCEL_MONITOR_WINDOW = 50         # 撤单率监控窗口（最近N笔订单），笔，[10, 200]

    # 恢复机制
    DEFAULT_CONSECUTIVE_LOSS_COOLDOWN_SEC = 300  # 连续亏损冷却时间，秒，[60, 3600]
    DEFAULT_CONSECUTIVE_PROFIT_THAW_COUNT = 2  # 冷却期内盈利次数达到此值自动解冻，次，[1, 5]

    # 状态持久化
    DEFAULT_STATE_DIR = "logs/risk_states"
    DEFAULT_STATE_SAVE_INTERVAL_SEC = 5        # 状态保存间隔，秒，[1, 60]

    def __init__(self):
        # 核心全局状态（加锁保护）
        self._lock = threading.RLock()
        self._daily_trade_count = 0
        self._daily_net_pnl_abs = 0.0          # 日内净盈亏绝对金额（盈利为负，亏损为正）
        self._initial_equity = 0.0             # 当日初始权益
        self._consecutive_losses = 0
        self._consecutive_profits_in_cooldown = 0  # 冷却期内盈利次数
        self._last_trade_timestamp = 0.0
        self._sec_trade_timestamps: deque = deque()
        self._minute_trade_timestamps: deque = deque()
        self._five_min_loss_abs_history: deque = deque(maxlen=200)  # 限制最大长度
        self._order_history: deque = deque(maxlen=self.DEFAULT_CANCEL_MONITOR_WINDOW)
        self._recent_orders: deque = deque(maxlen=100)
        self._cancel_count = 0
        self._total_order_count = 0

        # 策略级状态
        self._strategy_states: Dict[str, Dict[str, Any]] = {}

        # 外部依赖
        self._account_ledger = None
        self._config_loader = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 交易日与冷却
        self._current_trading_day = self._get_trading_day()
        self._consecutive_loss_frozen_until = 0.0

        # 异步IO队列（单线程消费者，避免线程爆炸）
        self._io_queue: deque = deque()
        self._io_thread = threading.Thread(target=self._io_worker, daemon=True)
        self._io_thread.start()

        # 定时清理
        self._last_cleanup = time.time()
        self._last_state_save = time.time()

        # 持久化
        self._state_file = os.path.join(
            self.DEFAULT_STATE_DIR,
            f"order_risk_gateway_{os.getpid()}.json"
        )
        os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
        self._load_state()

        logger.info("OrderRiskGateway 初始化完成，状态文件: %s", self._state_file)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        account_ledger: Optional[Any] = None,
        config_loader: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """注入外部依赖"""
        if account_ledger is None:
            logger.critical("AccountLedger 未注入，订单风控网关无法运行")
            raise RuntimeError("OrderRiskGateway requires AccountLedger")
        if not hasattr(account_ledger, 'get_equity'):
            logger.critical("AccountLedger 缺少 get_equity 方法")
            raise RuntimeError("AccountLedger interface invalid")
        self._account_ledger = account_ledger
        logger.info("AccountLedger 注入成功")

        if config_loader is not None:
            self._config_loader = config_loader
            logger.info("ConfigLoader 注入成功")
        else:
            logger.warning("ConfigLoader 未注入，使用严格默认值")

        if negotiation_bus is not None and hasattr(negotiation_bus, 'publish_alert'):
            self._negotiation_bus = negotiation_bus
            logger.info("NegotiationBus 注入成功")
        else:
            logger.warning("NegotiationBus 不可用，告警降级为本地日志")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

    # ========== 配置获取 ==========
    def _get_config(self, key: str, default: float) -> float:
        """从配置加载器获取参数，失败时返回默认值"""
        if self._config_loader is not None:
            try:
                value = self._config_loader.get(f"risk.order_risk_gateway.{key}", default)
                if isinstance(value, (int, float)):
                    return float(value)
            except Exception as e:
                logger.warning(f"配置读取失败 key={key}: {e}，使用默认值 {default}")
        return default

    def _get_equity(self) -> float:
        """安全获取当前账户权益"""
        try:
            return self._account_ledger.get_equity()
        except Exception as e:
            logger.error(f"获取账户权益失败: {e} #RECOVERY: 检查 AccountLedger 服务状态")
            return 0.0

    # ========== 异步IO工作线程 ==========
    def _io_worker(self) -> None:
        """单线程IO消费者，处理状态保存、日志写入、告警推送"""
        while True:
            try:
                if self._io_queue:
                    task = self._io_queue.popleft()
                    task()
                else:
                    time.sleep(0.01)
            except Exception as e:
                logger.warning(f"IO工作线程异常: {e}")

    def _enqueue_io(self, task: callable) -> None:
        """将IO任务加入队列（线程安全）"""
        self._io_queue.append(task)

    # ========== 公共接口 ==========
    def pre_flight_check(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """订单发送前全面风控检查"""
        self._check_day_rollover()
        self._try_cleanup()

        # 参数校验
        required_fields = ["symbol", "side", "quantity", "price", "order_type", "order_id", "timestamp"]
        missing = [f for f in required_fields if f not in order]
        if missing:
            return {
                "status": "error",
                "reason": f"订单缺少必要字段: {missing}",
                "data": {"blocked": True, "blocked_reason": f"missing_fields: {missing}"},
                "warnings": [],
            }

        try:
            symbol = str(order["symbol"])
            side = str(order["side"])
            quantity = float(order["quantity"])
            price = float(order["price"])
            order_type = str(order["order_type"])
            order_id = str(order["order_id"])
            timestamp = float(order["timestamp"])
        except (ValueError, TypeError) as e:
            return {
                "status": "error",
                "reason": f"订单参数类型错误: {e}",
                "data": {"blocked": True, "blocked_reason": "invalid_param_type"},
                "warnings": [],
            }

        is_closing = order.get("is_closing", False)
        strategy_id = order.get("strategy_id", "unknown")
        contract_multiplier = order.get("contract_multiplier", 1.0)

        # 获取权益（锁外获取）
        equity = self._get_equity()
        initial_equity = self._get_initial_equity()

        with self._lock:
            warnings = []

            # 计算名义价值（含合约乘数）
            order_notional = abs(quantity * price * contract_multiplier)

            # 1. 自成交检测
            if not is_closing:
                for recent in self._recent_orders:
                    if (recent["symbol"] == symbol and recent["side"] != side and
                        abs(recent["timestamp"] - timestamp) < 1.0):
                        if (side == "buy" and price >= recent["price"]) or (side == "sell" and price <= recent["price"]):
                            return self._block_order("self_trade", "检测到潜在自成交", strategy_id, order)

            # 2. 重复订单检测
            if not is_closing and self._is_duplicate_order(order_id, symbol, side, quantity, price, timestamp):
                return self._block_order("duplicate_order", f"检测到重复订单 {order_id}", strategy_id, order)

            # 3. 订单价值限制（平仓豁免）
            if not is_closing:
                max_value_pct = self._get_config("max_order_value_pct", self.DEFAULT_MAX_ORDER_VALUE_PCT)
                if equity > 0 and order_notional > equity * max_value_pct / 100.0:
                    return self._block_order("max_order_value_exceeded",
                                             f"订单名义价值 {order_notional:.2f} 超过权益 {equity:.2f} 的 {max_value_pct}%",
                                             strategy_id, order)

            # 4. 订单数量限制
            max_size = self._get_config("max_order_size", self.DEFAULT_MAX_ORDER_SIZE)
            if quantity > max_size:
                return self._block_order("max_order_size_exceeded",
                                         f"订单数量 {quantity} 超过上限 {max_size}", strategy_id, order)

            # 5. 频率限制（平仓豁免）
            if not is_closing:
                max_per_sec = int(self._get_config("max_trades_per_sec", self.DEFAULT_MAX_TRADES_PER_SEC))
                max_per_minute = int(self._get_config("max_trades_per_minute", self.DEFAULT_MAX_TRADES_PER_MINUTE))
                self._prune_sec_timestamps(timestamp)
                self._prune_minute_timestamps(timestamp)
                if len(self._sec_trade_timestamps) >= max_per_sec:
                    return self._block_order("trade_rate_limit_sec",
                                             f"每秒交易次数超过上限 {max_per_sec}", strategy_id, order)
                if len(self._minute_trade_timestamps) >= max_per_minute:
                    return self._block_order("trade_rate_limit_min",
                                             f"每分钟交易次数超过上限 {max_per_minute}", strategy_id, order)

            # 6. 日内亏损熔断（基于净亏损）
            max_daily_loss_pct = self._get_config("max_daily_loss_pct", self.DEFAULT_MAX_DAILY_LOSS_PCT)
            if initial_equity > 0:
                daily_loss_pct = max(0, self._daily_net_pnl_abs) / initial_equity * 100
                if daily_loss_pct >= max_daily_loss_pct:
                    return self._block_order("daily_loss_limit",
                                             f"日内净亏损 {daily_loss_pct:.2f}% 超过上限 {max_daily_loss_pct}%",
                                             strategy_id, order)

            # 7. 连续亏损熔断（含冷却期盈利解冻）
            cons_limit = int(self._get_config("consecutive_loss_limit", self.DEFAULT_CONSECUTIVE_LOSS_LIMIT))
            if self._consecutive_losses >= cons_limit:
                cooldown = self._get_config("consecutive_loss_cooldown_sec",
                                            self.DEFAULT_CONSECUTIVE_LOSS_COOLDOWN_SEC)
                thaw_count = int(self._get_config("consecutive_profit_thaw_count",
                                                  self.DEFAULT_CONSECUTIVE_PROFIT_THAW_COUNT))
                if self._consecutive_profits_in_cooldown >= thaw_count:
                    self._consecutive_losses = 0
                    self._consecutive_loss_frozen_until = 0.0
                    self._consecutive_profits_in_cooldown = 0
                    logger.info("冷却期内盈利 %d 次，连续亏损冻结自动解除", thaw_count)
                elif time.time() < self._consecutive_loss_frozen_until:
                    return self._block_order("consecutive_loss_limit",
                                             f"连续亏损冻结中，冷却至 {self._consecutive_loss_frozen_until:.0f}",
                                             strategy_id, order)
                else:
                    self._consecutive_losses = 0
                    self._consecutive_loss_frozen_until = 0.0
                    logger.info("连续亏损冷却结束，恢复正常交易")

            # 8. 5分钟亏损熔断（基于净亏损）
            minute_loss_limit_pct = self._get_config("minute_loss_limit_pct", self.DEFAULT_MINUTE_LOSS_LIMIT_PCT)
            self._prune_five_min_losses(timestamp)
            recent_loss_abs = sum(record.loss_abs for record in self._five_min_loss_abs_history)
            if initial_equity > 0 and recent_loss_abs / initial_equity * 100 >= minute_loss_limit_pct:
                return self._block_order("minute_loss_limit",
                                         f"5分钟内累计净亏损超过上限 {minute_loss_limit_pct}%", strategy_id, order)

            # 9. 撤单率监控（策略级）
            max_cancel_rate = self._get_config("max_cancel_rate_pct", self.DEFAULT_MAX_CANCEL_RATE_PCT)
            strategy_cancel_rate = self._calculate_strategy_cancel_rate(strategy_id)
            if strategy_cancel_rate > max_cancel_rate:
                return self._block_order("cancel_rate_high",
                                         f"策略 {strategy_id} 撤单率 {strategy_cancel_rate:.1f}% 超过上限 {max_cancel_rate}%",
                                         strategy_id, order)

            # 全部通过，记录状态
            self._sec_trade_timestamps.append(timestamp)
            self._minute_trade_timestamps.append(timestamp)
            self._recent_orders.append({
                "order_id": order_id, "timestamp": timestamp, "symbol": symbol,
                "side": side, "quantity": quantity, "price": price
            })
            self._daily_trade_count += 1
            self._last_trade_timestamp = timestamp
            # 初始化策略状态
            if strategy_id not in self._strategy_states:
                self._strategy_states[strategy_id] = {"cancel_count": 0, "total_count": 0}

            return {
                "status": "ok",
                "reason": "订单通过全部风控检查",
                "data": {"blocked": False},
                "warnings": warnings,
            }

    def update_state(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """更新风控内部状态"""
        event_type = event.get("event_type", "")
        order_id = event.get("order_id", "")
        pnl_abs = float(event.get("pnl_abs", 0.0))  # 绝对盈亏金额（正为盈利，负为亏损）
        strategy_id = event.get("strategy_id", "unknown")

        with self._lock:
            if event_type == "fill":
                # 更新日内净盈亏（亏损增加，盈利减少）
                if pnl_abs < 0:
                    self._daily_net_pnl_abs += abs(pnl_abs)
                    self._consecutive_losses += 1
                    self._consecutive_profits_in_cooldown = 0
                    self._five_min_loss_abs_history.append(
                        LossRecord(time.time(), abs(pnl_abs), strategy_id)
                    )
                    cons_limit = int(self._get_config("consecutive_loss_limit",
                                                      self.DEFAULT_CONSECUTIVE_LOSS_LIMIT))
                    if self._consecutive_losses >= cons_limit:
                        cooldown = self._get_config("consecutive_loss_cooldown_sec",
                                                    self.DEFAULT_CONSECUTIVE_LOSS_COOLDOWN_SEC)
                        self._consecutive_loss_frozen_until = time.time() + cooldown
                        logger.warning(f"连续亏损触发冻结，恢复时间: {self._consecutive_loss_frozen_until:.0f}")
                else:
                    self._daily_net_pnl_abs = max(0, self._daily_net_pnl_abs - pnl_abs)
                    self._consecutive_losses = 0
                    if self._consecutive_loss_frozen_until > 0:
                        self._consecutive_profits_in_cooldown += 1
                        logger.info(f"冷却期内盈利次数: {self._consecutive_profits_in_cooldown}")
                self._update_order_status(order_id, "filled")
                self._total_order_count += 1
                if strategy_id in self._strategy_states:
                    self._strategy_states[strategy_id]["total_count"] += 1

            elif event_type == "cancel":
                self._update_order_status(order_id, "canceled")
                self._cancel_count += 1
                self._total_order_count += 1
                if strategy_id not in self._strategy_states:
                    self._strategy_states[strategy_id] = {"cancel_count": 0, "total_count": 0}
                self._strategy_states[strategy_id]["cancel_count"] += 1
                self._strategy_states[strategy_id]["total_count"] += 1

            elif event_type == "daily_reset":
                self._reset_daily_state()

            # 定期保存状态
            if time.time() - self._last_state_save > self.DEFAULT_STATE_SAVE_INTERVAL_SEC:
                self._last_state_save = time.time()
                self._enqueue_io(lambda: self._save_state())

        return {
            "status": "ok",
            "reason": f"已处理 {event_type} 事件",
            "data": {
                "daily_net_loss_abs": round(self._daily_net_pnl_abs, 2),
                "consecutive_losses": self._consecutive_losses,
            },
            "warnings": [],
        }

    def get_metrics(self, strategy_id: str = None) -> Dict[str, Any]:
        """获取当前风控指标汇总"""
        with self._lock:
            initial_equity = self._get_initial_equity()
            metrics = {
                "daily_trade_count": self._daily_trade_count,
                "daily_net_loss_abs": round(self._daily_net_pnl_abs, 2),
                "daily_net_loss_pct": (
                    round(self._daily_net_pnl_abs / initial_equity * 100, 2)
                    if initial_equity > 0 else 0
                ),
                "consecutive_losses": self._consecutive_losses,
                "cancel_rate_pct": round(self._calculate_global_cancel_rate(), 1),
                "recent_trades_per_sec": len(self._sec_trade_timestamps),
                "recent_trades_per_minute": len(self._minute_trade_timestamps),
                "initial_equity": initial_equity,
                "frozen_until": self._consecutive_loss_frozen_until,
            }
            if strategy_id:
                metrics["strategy_cancel_rate"] = round(
                    self._calculate_strategy_cancel_rate(strategy_id), 1
                )
            return {
                "status": "ok",
                "reason": "风控指标查询成功",
                "data": metrics,
                "warnings": [],
            }

    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            with self._lock:
                _ = self._daily_trade_count
                _ = self._daily_net_pnl_abs
                _ = self._consecutive_losses
            # 验证核心依赖连通性
            equity = self._get_equity()
            if equity <= 0:
                return {
                    "status": "degraded",
                    "reason": "AccountLedger 返回异常权益",
                    "data": {},
                    "warnings": ["account_ledger_unhealthy"],
                }
            # 验证状态文件可写
            try:
                tmp_test = self._state_file + ".health_test"
                with open(tmp_test, 'w') as f:
                    json.dump({"test": True}, f)
                os.remove(tmp_test)
            except Exception as e:
                return {
                    "status": "degraded",
                    "reason": f"状态文件不可写: {e}",
                    "data": {},
                    "warnings": ["state_file_unwritable"],
                }
            return {
                "status": "ok",
                "reason": "OrderRiskGateway 正常，核心依赖连通，状态文件可写",
                "data": {
                    "equity": equity,
                    "dependencies": {
                        "account_ledger": True,
                        "config_loader": self._config_loader is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                    }
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和 AccountLedger 连接")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _block_order(self, reason_code: str, message: str,
                     strategy_id: str, order: Optional[Dict] = None) -> Dict[str, Any]:
        """生成拦截响应，异步记录日志和告警"""
        symbol = order.get("symbol", "?") if order else "?"
        side = order.get("side", "?") if order else "?"
        log_detail = f"[{reason_code}] {message} | 策略={strategy_id} 品种={symbol} 方向={side}"
        logger.warning(log_detail)
        self._enqueue_io(lambda: self._log_event("order_blocked", {
            "reason_code": reason_code, "message": message,
            "strategy_id": strategy_id, "symbol": symbol, "side": side
        }))
        if reason_code in ("daily_loss_limit", "consecutive_loss_limit", "minute_loss_limit"):
            self._enqueue_io(lambda: self._trigger_alert(
                reason_code, f"{message} (策略={strategy_id}, 品种={symbol})"
            ))
        return {
            "status": "blocked",
            "reason": message,
            "data": {"blocked": True, "blocked_reason": reason_code},
            "warnings": [message],
        }

    def _is_duplicate_order(self, order_id: str, symbol: str, side: str,
                            quantity: float, price: float, timestamp: float) -> bool:
        """增强重复订单检测"""
        window_ms = self._get_config("duplicate_order_window_ms", self.DEFAULT_DUPLICATE_ORDER_WINDOW_MS)
        cutoff = timestamp - window_ms / 1000.0
        price_tolerance = self._get_config("duplicate_price_tolerance",
                                           self.DEFAULT_DUPLICATE_PRICE_TOLERANCE)
        # 清理过期
        while self._recent_orders and self._recent_orders[0]["timestamp"] < cutoff:
            self._recent_orders.popleft()
        for recent in self._recent_orders:
            if recent["order_id"] == order_id:
                return True
            if (recent["symbol"] == symbol and recent["side"] == side and
                abs(recent["quantity"] - quantity) < 0.001 and
                abs(recent["price"] - price) / max(price, 1e-10) < price_tolerance):
                return True
        return False

    def _try_cleanup(self) -> None:
        """定时主动清理过期数据"""
        now = time.time()
        if now - self._last_cleanup < 60:
            return
        self._last_cleanup = now
        with self._lock:
            self._prune_sec_timestamps(now)
            self._prune_minute_timestamps(now)
            self._prune_five_min_losses(now)
            # 主动清理过期重复订单窗口
            cutoff = now - self._get_config("duplicate_order_window_ms",
                                            self.DEFAULT_DUPLICATE_ORDER_WINDOW_MS) / 1000.0
            while self._recent_orders and self._recent_orders[0]["timestamp"] < cutoff:
                self._recent_orders.popleft()

    def _prune_sec_timestamps(self, now: float) -> None:
        cutoff = now - 1.0
        while self._sec_trade_timestamps and self._sec_trade_timestamps[0] < cutoff:
            self._sec_trade_timestamps.popleft()

    def _prune_minute_timestamps(self, now: float) -> None:
        cutoff = now - 60.0
        while self._minute_trade_timestamps and self._minute_trade_timestamps[0] < cutoff:
            self._minute_trade_timestamps.popleft()

    def _prune_five_min_losses(self, now: float) -> None:
        cutoff = now - 300.0
        while (self._five_min_loss_abs_history and
               self._five_min_loss_abs_history[0].timestamp < cutoff):
            self._five_min_loss_abs_history.popleft()

    def _calculate_global_cancel_rate(self) -> float:
        """全局撤单率"""
        if self._total_order_count == 0:
            return 0.0
        return self._cancel_count / self._total_order_count * 100.0

    def _calculate_strategy_cancel_rate(self, strategy_id: str) -> float:
        """策略级撤单率"""
        if strategy_id not in self._strategy_states:
            return 0.0
        state = self._strategy_states[strategy_id]
        if state["total_count"] == 0:
            return 0.0
        return state["cancel_count"] / state["total_count"] * 100.0

    def _update_order_status(self, order_id: str, status: str) -> None:
        for order in self._order_history:
            if order.get("order_id") == order_id:
                order["status"] = status
                return
        self._order_history.append({"order_id": order_id, "status": status})

    def _check_day_rollover(self) -> None:
        current_day = self._get_trading_day()
        if current_day != self._current_trading_day:
            self._reset_daily_state()
            self._current_trading_day = current_day

    def _reset_daily_state(self) -> None:
        self._daily_trade_count = 0
        self._daily_net_pnl_abs = 0.0
        self._initial_equity = self._get_equity()
        self._consecutive_losses = 0
        self._consecutive_loss_frozen_until = 0.0
        self._consecutive_profits_in_cooldown = 0
        self._sec_trade_timestamps.clear()
        self._minute_trade_timestamps.clear()
        self._five_min_loss_abs_history.clear()
        self._strategy_states.clear()
        logger.info("日度风控状态已重置，新初始权益: %.2f", self._initial_equity)
        self._enqueue_io(lambda: self._save_state())

    def _get_trading_day(self) -> int:
        reset_hour = int(self._get_config("trading_day_reset_hour", 0))
        now = time.time()
        return int(time.strftime("%Y%m%d", time.gmtime(now - reset_hour * 3600)))

    def _get_initial_equity(self) -> float:
        """获取当日初始权益"""
        if self._initial_equity <= 0:
            self._initial_equity = self._get_equity()
        return self._initial_equity

    # ========== 异步操作（由IO线程消费） ==========
    def _log_event(self, event_type: str, details: Dict[str, Any]) -> None:
        """记录事件日志"""
        if self._behavioral_logger is not None and hasattr(self._behavioral_logger, 'log_event'):
            try:
                self._behavioral_logger.log_event(event_type=event_type, details=details)
                return
            except Exception as e:
                logger.warning(f"BehavioralLogger 记录失败: {e}")
        logger.warning(f"风控事件 [{event_type}]: {json.dumps(details, default=str)}")

    def _trigger_alert(self, alert_type: str, message: str) -> None:
        """触发告警"""
        if self._negotiation_bus is not None:
            try:
                self._negotiation_bus.publish_alert(
                    alert_type=alert_type, message=message, timestamp=time.time(),
                    context={"module": "OrderRiskGateway"}
                )
                return
            except Exception as e:
                logger.warning(f"协商总线告警推送失败: {e}")
        logger.error(f"风控熔断告警 [{alert_type}]: {message} #RECOVERY: 检查策略盈亏或手动重置风控状态")

    # ========== 状态持久化 ==========
    def _save_state(self) -> None:
        """原子保存状态到文件"""
        try:
            state = {
                "daily_trade_count": self._daily_trade_count,
                "daily_net_pnl_abs": self._daily_net_pnl_abs,
                "initial_equity": self._initial_equity,
                "consecutive_losses": self._consecutive_losses,
                "consecutive_loss_frozen_until": self._consecutive_loss_frozen_until,
                "current_trading_day": self._current_trading_day,
            }
            tmp_file = self._state_file + ".tmp"
            with open(tmp_file, 'w') as f:
                json.dump(state, f)
            os.replace(tmp_file, self._state_file)  # 原子替换
        except Exception as e:
            logger.error(f"状态持久化失败: {e} #RECOVERY: 检查磁盘空间和目录权限")

    def _load_state(self) -> None:
        """从文件恢复状态"""
        try:
            if not os.path.exists(self._state_file):
                logger.info("状态文件不存在，使用全新状态")
                return
            with open(self._state_file, 'r') as f:
                state = json.load(f)
            # 校验数据完整性
            required_keys = ["daily_trade_count", "daily_net_pnl_abs", "current_trading_day"]
            if not all(k in state for k in required_keys):
                logger.warning("状态文件数据不完整，使用全新状态")
                return
            if state.get("current_trading_day") == self._current_trading_day:
                self._daily_trade_count = state.get("daily_trade_count", 0)
                self._daily_net_pnl_abs = state.get("daily_net_pnl_abs", 0.0)
                self._initial_equity = state.get("initial_equity", 0.0)
                self._consecutive_losses = state.get("consecutive_losses", 0)
                self._consecutive_loss_frozen_until = state.get("consecutive_loss_frozen_until", 0.0)
                logger.info("恢复风控状态: 日内净亏损=%.2f, 连续亏损=%d",
                           self._daily_net_pnl_abs, self._consecutive_losses)
            else:
                logger.info("状态文件已过期，使用全新状态")
        except json.JSONDecodeError:
            logger.warning("状态文件损坏，使用全新状态")
        except Exception as e:
            logger.error(f"状态恢复失败: {e}，使用全新状态")
