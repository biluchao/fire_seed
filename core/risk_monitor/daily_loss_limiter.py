"""
火种系统 · 日内亏损限额管理器 (DailyLossLimiter)

核心职责：
1. 按策略与品种实时监控日内已实现净盈亏，当累计净亏损超过预设硬限额时触发熔断与强制平仓
2. 支持分时段差异化亏损限额、滚动窗口预算检查、按策略配置冷却时长与硬止损开关

外部依赖（真实模块接口）：
- core.account_ledger.AccountLedger : 获取实时账户权益、交易所服务器时间戳
- core.negotiation_bus.NegotiationBus : 发送亏损超限告警、强制平仓指令、仓位缩减指令
- core.behavioral_logger.BehavioralLogger : 记录亏损事件与熔断操作审计日志

接口契约：
- check_loss_limit(strategy_id: str, symbol: str, realized_pnl: float) -> Dict[str, Any]
  检查指定策略与品种的累计净亏损是否超限，返回是否允许继续交易及原因
- get_daily_loss_summary(strategy_id: str = None) -> Dict[str, Any]
  获取日内亏损汇总，可按策略过滤，返回冷却剩余秒数
- reset_daily_losses(caller: str) -> Dict[str, Any]
  重置所有日内亏损计数器（仅限授权调用方）
- update_whitelist(strategies: Set[str], symbols: Set[str]) -> Dict[str, Any]
  运行时热更新策略与品种白名单
- health_check() -> Dict[str, Any] : 模块自检（含一致性校验与自动修复）
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 AccountLedger 不可用时，使用缓存的最近有效权益（最长60秒），超期则拒绝交易并告警
- 当 NegotiationBus 不可用时，强制平仓降级为本地日志记录
- 任何外部依赖异常均不影响净盈亏累加功能的正常运行

资源管理：
- 内部使用线程安全的计数器字典，模块销毁时自动释放
- 不持有任何外部资源句柄
"""

import time
import logging
import math
import threading
from typing import Dict, Any, List, Optional, Set, Union
from collections import deque
import itertools

logger = logging.getLogger(__name__)


class DailyLossLimiter:
    """日内亏损限额管理器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_GLOBAL_DAILY_LOSS_PCT = 0.05      # 全局日内净亏损硬限额，占权益百分比，取值范围 [0.01, 0.10]
    DEFAULT_PER_STRATEGY_LOSS_PCT = 0.03      # 单策略日内净亏损硬限额，占权益百分比，取值范围 [0.01, 0.05]
    DEFAULT_PER_SYMBOL_LOSS_PCT = 0.02        # 单品种日内净亏损硬限额，占权益百分比，取值范围 [0.005, 0.04]
    DEFAULT_ROLLING_WINDOW_TRADES = 20        # 滚动窗口内交易笔数，无量纲，[10, 50]
    DEFAULT_ROLLING_LOSS_THRESHOLD_PCT = 0.02 # 滚动窗口内累计净亏损占权益百分比触发缩减，[0.01, 0.05]
    DEFAULT_HARD_STOP_ENABLED = True          # 全局硬止损默认开启，各策略可覆盖
    DEFAULT_COOLDOWN_MINUTES = 30             # 默认硬止损冷却时间，分钟，[10, 120]
    DEFAULT_MAX_ROLLING_RECORDS = 5000        # 滚动窗口最大记录数，取值范围 [1000, 10000]
    DEFAULT_MAX_STRATEGY_ENTRIES = 50         # 策略字典最大条目数，取值范围 [20, 200]
    DEFAULT_MAX_SYMBOL_ENTRIES = 100          # 品种字典最大条目数，取值范围 [50, 500]
    DEFAULT_EQUITY_CACHE_TTL_SEC = 60         # 权益缓存有效期，秒，[10, 300]
    DEFAULT_EQUITY_ANOMALY_THRESHOLD = 3       # 连续权益异常次数触发拒绝交易，[2, 10]
    DEFAULT_EQUITY_ANOMALY_WINDOW_SEC = 300    # 权益异常计数重置窗口，秒，[120, 600]
    DEFAULT_CONSISTENCY_TOLERANCE = 0.01       # 滚动窗口一致性校验容忍偏差
    DEFAULT_AUTHORIZED_RESET_CALLERS = {"risk_monitor", "scheduler", "admin"}  # 授权重置调用方
    DEFAULT_IS_CRYPTO_MARKET = True           # 加密货币市场默认启用 7x24
    DEFAULT_MAX_SINGLE_PNL_RATIO = 0.5        # 单笔盈亏不得超过权益的此比例，无量纲，[0.1, 1.0]
    DEFAULT_BUS_TIMEOUT_SEC = 2.0             # 总线调用超时秒数
    DEFAULT_REDUCE_POSITION_SUGGESTION_PCT = 0.5  # 缩减仓位建议比例
    DEFAULT_SERVER_TIME_TIMEOUT_SEC = 1.0     # 获取交易所时间超时秒数

    # 时段定义与默认系数（UTC 小时范围）
    DEFAULT_SESSION_CONFIGS = {
        "asian": {"hours": (0, 8), "multiplier": 1.0},
        "european": {"hours": (8, 16), "multiplier": 1.1},
        "american": {"hours": (16, 24), "multiplier": 1.0},
        "weekend": {"hours": None, "multiplier": 0.7},
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        初始化日内亏损限额管理器

        Args:
            config: 可选配置字典，支持以下键：
                - global_daily_loss_pct (float): 全局限额百分比
                - per_strategy_loss_pct (float): 单策略限额百分比
                - per_symbol_loss_pct (float): 单品种限额百分比
                - rolling_window_trades (int): 滚动窗口交易笔数
                - rolling_loss_threshold_pct (float): 滚动窗口预警阈值
                - hard_stop_enabled (bool): 全局硬止损开关
                - cooldown_minutes (int): 默认冷却分钟数
                - session_configs (dict): 时段定义与系数
                - allowed_strategies (List[str]): 合法策略白名单
                - allowed_symbols (List[str]): 合法品种白名单
                - strategy_configs (Dict[str, Dict]): 按策略的硬止损与冷却配置
                - authorized_reset_callers (List[str]): 授权调用重置的模块列表
                - is_crypto_market (bool): 是否加密货币市场（不区分周末）
                - max_rolling_records (int): 滚动窗口最大记录数
                - equity_cache_ttl_sec (int): 权益缓存有效期
                - max_single_pnl_ratio (float): 单笔盈亏上限比例
                - bus_timeout_sec (float): 总线调用超时秒数
                - reduce_position_suggestion_pct (float): 建议减仓比例
        """
        cfg = config or {}

        # 亏损限额配置
        self._global_loss_pct = cfg.get("global_daily_loss_pct", self.DEFAULT_GLOBAL_DAILY_LOSS_PCT)
        self._per_strategy_loss_pct = cfg.get("per_strategy_loss_pct", self.DEFAULT_PER_STRATEGY_LOSS_PCT)
        self._per_symbol_loss_pct = cfg.get("per_symbol_loss_pct", self.DEFAULT_PER_SYMBOL_LOSS_PCT)
        self._rolling_window_trades = cfg.get("rolling_window_trades", self.DEFAULT_ROLLING_WINDOW_TRADES)
        self._rolling_loss_threshold_pct = cfg.get(
            "rolling_loss_threshold_pct", self.DEFAULT_ROLLING_LOSS_THRESHOLD_PCT
        )
        self._hard_stop_enabled = cfg.get("hard_stop_enabled", self.DEFAULT_HARD_STOP_ENABLED)
        self._cooldown_minutes = cfg.get("cooldown_minutes", self.DEFAULT_COOLDOWN_MINUTES)
        self._max_rolling_records = cfg.get("max_rolling_records", self.DEFAULT_MAX_ROLLING_RECORDS)
        self._equity_cache_ttl_sec = cfg.get("equity_cache_ttl_sec", self.DEFAULT_EQUITY_CACHE_TTL_SEC)
        self._equity_anomaly_threshold = cfg.get("equity_anomaly_threshold", self.DEFAULT_EQUITY_ANOMALY_THRESHOLD)
        self._equity_anomaly_window_sec = cfg.get("equity_anomaly_window_sec", self.DEFAULT_EQUITY_ANOMALY_WINDOW_SEC)
        self._is_crypto_market = cfg.get("is_crypto_market", self.DEFAULT_IS_CRYPTO_MARKET)
        self._authorized_reset_callers = set(
            cfg.get("authorized_reset_callers", self.DEFAULT_AUTHORIZED_RESET_CALLERS)
        )
        self._max_single_pnl_ratio = cfg.get("max_single_pnl_ratio", self.DEFAULT_MAX_SINGLE_PNL_RATIO)
        self._bus_timeout_sec = cfg.get("bus_timeout_sec", self.DEFAULT_BUS_TIMEOUT_SEC)
        self._reduce_suggestion_pct = cfg.get("reduce_position_suggestion_pct", self.DEFAULT_REDUCE_POSITION_SUGGESTION_PCT)
        self._server_time_timeout = cfg.get("server_time_timeout_sec", self.DEFAULT_SERVER_TIME_TIMEOUT_SEC)

        # 时段配置
        self._session_configs = cfg.get("session_configs", self.DEFAULT_SESSION_CONFIGS)

        # 策略级硬止损与冷却配置
        self._strategy_configs: Dict[str, Dict[str, Any]] = {}
        raw_strategy_configs = cfg.get("strategy_configs", {})
        if isinstance(raw_strategy_configs, dict):
            for sid, scfg in raw_strategy_configs.items():
                if isinstance(scfg, dict):
                    self._strategy_configs[sid] = {
                        "hard_stop_enabled": scfg.get("hard_stop_enabled", self._hard_stop_enabled),
                        "cooldown_minutes": scfg.get("cooldown_minutes", self._cooldown_minutes),
                    }
        else:
            logger.warning("strategy_configs 格式错误，已忽略")

        # 内部状态
        self._global_pnl: float = 0.0
        self._pnl_by_strategy: Dict[str, float] = {}
        self._pnl_by_symbol: Dict[str, float] = {}
        self._rolling_pnls: deque = deque()
        self._rolling_net_pnl: float = 0.0

        # 熔断与冷却
        self._circuit_breakers: Dict[str, float] = {}

        # 权益缓存与异常状态
        self._cached_equity: float = 0.0
        self._equity_cached_at: float = 0.0
        self._equity_anomaly_count: int = 0
        self._last_equity_success_time: float = 0.0

        # 白名单
        self._allowed_strategies: Optional[Set[str]] = None
        self._allowed_symbols: Optional[Set[str]] = None
        if "allowed_strategies" in cfg:
            self._allowed_strategies = set(cfg["allowed_strategies"])
        if "allowed_symbols" in cfg:
            self._allowed_symbols = set(cfg["allowed_symbols"])

        # 外部依赖注入
        self._account_ledger = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 线程安全
        self._lock = threading.Lock()

        # 日切标记
        self._current_trade_date: str = ""

        logger.info(
            "DailyLossLimiter 初始化完成，全局限额 %.2f%%，加密货币市场: %s",
            self._global_loss_pct * 100, self._is_crypto_market
        )

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        account_ledger: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选注入，未注入时对应功能降级）"""
        if account_ledger is not None:
            if hasattr(account_ledger, 'get_total_equity'):
                self._account_ledger = account_ledger
                logger.info("AccountLedger 注入成功")
            else:
                logger.warning("AccountLedger 缺少 get_total_equity 方法，依赖不可用")

        if negotiation_bus is not None:
            self._negotiation_bus = negotiation_bus
            logger.info("NegotiationBus 注入成功")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")

    # ========== 公共接口 ==========
    def check_loss_limit(
        self, strategy_id: str, symbol: str, realized_pnl: float
    ) -> Dict[str, Any]:
        """
        检查指定策略与品种的累计净亏损是否超限

        Args:
            strategy_id: 策略标识
            symbol: 交易品种
            realized_pnl: 本次交易已实现盈亏（正=盈利，负=亏损）

        Returns:
            标准响应字典
        """
        # 参数校验
        if not strategy_id or not symbol:
            return {
                "status": "error",
                "reason": "策略ID和品种不能为空",
                "data": {"allowed": False},
                "warnings": ["invalid_parameters"],
            }
        strategy_key = strategy_id.strip()
        symbol_key = symbol.strip().upper()

        # 盈亏值合法性校验
        if not math.isfinite(realized_pnl):
            logger.error(f"非法盈亏值: {realized_pnl}，已拒绝累加 #RECOVERY: 检查上游模块计算逻辑")
            return {
                "status": "error",
                "reason": f"非法盈亏值: {realized_pnl}",
                "data": {"allowed": False},
                "warnings": ["invalid_pnl_value"],
            }

        # 白名单校验
        if self._allowed_strategies is not None and strategy_key not in self._allowed_strategies:
            logger.warning(f"未知策略 {strategy_key}，已忽略 #RECOVERY: 添加到配置白名单或调用 update_whitelist")
            return {
                "status": "error",
                "reason": f"策略 {strategy_key} 不在白名单中",
                "data": {"allowed": False},
                "warnings": ["unknown_strategy"],
            }
        if self._allowed_symbols is not None and symbol_key not in self._allowed_symbols:
            logger.warning(f"未知品种 {symbol_key}，已忽略")
            return {
                "status": "error",
                "reason": f"品种 {symbol_key} 不在白名单中",
                "data": {"allowed": False},
                "warnings": ["unknown_symbol"],
            }

        # 锁内操作
        with self._lock:
            # 1. 日切检查（必须在锁内，确保重置与累加原子性）
            self._auto_reset_on_new_day()

            # 2. 单笔盈亏上限校验
            equity_now = self._get_current_equity()
            max_single = equity_now * self._max_single_pnl_ratio
            if abs(realized_pnl) > max_single and max_single > 0:
                logger.error(
                    f"单笔盈亏超限: {realized_pnl:.2f} > {max_single:.2f} (权益 {equity_now:.0f})，已拒绝累加"
                )
                return {
                    "status": "error",
                    "reason": f"单笔盈亏超过权益的 {self._max_single_pnl_ratio*100:.0f}%，疑似异常",
                    "data": {"allowed": False},
                    "warnings": ["single_pnl_exceeds_limit"],
                }

            # 3. 累加净盈亏
            self._global_pnl += realized_pnl
            self._pnl_by_strategy[strategy_key] = self._pnl_by_strategy.get(strategy_key, 0.0) + realized_pnl
            self._pnl_by_symbol[symbol_key] = self._pnl_by_symbol.get(symbol_key, 0.0) + realized_pnl

            # 4. 滚动窗口管理
            self._rolling_pnls.append(realized_pnl)
            self._rolling_net_pnl += realized_pnl
            if len(self._rolling_pnls) > self._rolling_window_trades:
                popped = self._rolling_pnls.popleft()
                self._rolling_net_pnl -= popped
            # 硬截断
            while len(self._rolling_pnls) > self._max_rolling_records:
                self._rolling_net_pnl -= self._rolling_pnls.popleft()

            # 5. 获取权益（带缓存和异常保护）
            equity = self._get_safe_equity()
            if equity is None:
                # 无法获取任何有效权益，直接拒绝交易
                logger.error("无法获取有效权益，暂停所有交易 #RECOVERY: 检查 AccountLedger 和交易所 API")
                return {
                    "status": "error",
                    "reason": "无法获取账户权益，交易暂停",
                    "data": {"allowed": False},
                    "warnings": ["equity_unavailable"],
                }

            # 6. 清理过期冷却条目
            self._purge_expired_circuit_breakers()

            # 7. 检查熔断冷却
            cooldown_remaining = self._get_cooldown_remaining(strategy_key)
            if cooldown_remaining > 0:
                return {
                    "status": "ok",
                    "reason": f"策略 {strategy_id} 处于熔断冷却期，剩余 {cooldown_remaining} 秒",
                    "data": {"allowed": False, "cooldown_remaining_sec": cooldown_remaining},
                    "warnings": ["cooldown_active"],
                }

            # 8. 计算亏损比例
            session_mult = self._get_session_multiplier()
            global_loss = max(0.0, -self._global_pnl)
            strategy_loss = max(0.0, -self._pnl_by_strategy.get(strategy_key, 0.0))
            symbol_loss = max(0.0, -self._pnl_by_symbol.get(symbol_key, 0.0))
            rolling_loss = max(0.0, -self._rolling_net_pnl)

            global_loss_pct = global_loss / equity
            strategy_loss_pct = strategy_loss / equity
            symbol_loss_pct = symbol_loss / equity
            rolling_loss_pct = rolling_loss / equity

            warnings = []
            allowed = True
            limit_breached = ""

            # 9. 分级判定
            adjusted_global = self._global_loss_pct * session_mult
            if global_loss_pct >= adjusted_global:
                allowed = False
                limit_breached = "global_daily_loss"
                warnings.append(f"全局日内净亏损 {global_loss_pct:.2%} >= {adjusted_global:.2%}")

            adjusted_strategy = self._per_strategy_loss_pct * session_mult
            if strategy_loss_pct >= adjusted_strategy:
                allowed = False
                limit_breached = limit_breached or "strategy_daily_loss"
                warnings.append(f"策略 {strategy_id} 日内净亏损 {strategy_loss_pct:.2%} >= {adjusted_strategy:.2%}")

            adjusted_symbol = self._per_symbol_loss_pct * session_mult
            if symbol_loss_pct >= adjusted_symbol:
                allowed = False
                limit_breached = limit_breached or "symbol_daily_loss"
                warnings.append(f"品种 {symbol} 日内净亏损 {symbol_loss_pct:.2%} >= {adjusted_symbol:.2%}")

            adjusted_rolling = self._rolling_loss_threshold_pct * session_mult
            if rolling_loss_pct >= adjusted_rolling:
                warnings.append(f"滚动窗口({self._rolling_window_trades}笔)累计净亏损 {rolling_loss_pct:.2%}")
                # 缩减仓位信号将在锁外发送
                self._pending_reduce_signal = (strategy_id, symbol, rolling_loss_pct)

            # 10. 硬止损处理（只设置状态，不调用外部总线）
            if not allowed:
                strategy_cfg = self._strategy_configs.get(strategy_key, {})
                hard_stop = strategy_cfg.get("hard_stop_enabled", self._hard_stop_enabled)
                cooldown_min = strategy_cfg.get("cooldown_minutes", self._cooldown_minutes)
                if hard_stop:
                    cooldown_until = time.time() + cooldown_min * 60
                    if limit_breached == "global_daily_loss":
                        self._circuit_breakers["global"] = cooldown_until
                    self._circuit_breakers[strategy_key] = cooldown_until
                    # 收集硬止损上下文
                    self._pending_hard_stop = (
                        strategy_id, symbol, limit_breached, equity,
                        global_loss_pct, strategy_loss_pct, symbol_loss_pct, rolling_loss_pct
                    )

            # 11. 限制字典大小
            self._trim_dicts()

            # 12. 准备返回数据
            result = {
                "status": "ok",
                "reason": f"亏损限额检查完成，{'允许交易' if allowed else '禁止交易'}",
                "data": {
                    "allowed": allowed,
                    "limit_breached": limit_breached if not allowed else "",
                    "global_pnl_raw": round(self._global_pnl, 2),
                    "global_loss_pct": round(global_loss_pct, 4),
                    "strategy_loss_pct": round(strategy_loss_pct, 4),
                    "symbol_loss_pct": round(symbol_loss_pct, 4),
                    "rolling_loss_pct": round(rolling_loss_pct, 4),
                    "equity": round(equity, 2),
                    "session": self._get_current_session(),
                    "session_multiplier": round(session_mult, 2),
                },
                "warnings": warnings,
            }

        # ---------- 锁外操作：发送外部总线指令 ----------
        # 缩减仓位信号
        if hasattr(self, '_pending_reduce_signal'):
            sig = self._pending_reduce_signal
            self._send_reduce_position_signal(*sig)
            del self._pending_reduce_signal

        # 硬止损信号
        if hasattr(self, '_pending_hard_stop'):
            ctx = self._pending_hard_stop
            self._trigger_hard_stop(*ctx)
            del self._pending_hard_stop

        return result

    def get_daily_loss_summary(self, strategy_id: str = None) -> Dict[str, Any]:
        """获取日内亏损汇总"""
        with self._lock:
            self._auto_reset_on_new_day()
            now = time.time()
            equity = self._get_current_equity()
            if equity <= 0:
                equity = self._cached_equity if self._cached_equity > 0 else 0.0

            if strategy_id:
                strategy_key = strategy_id.strip()
                pnl = self._pnl_by_strategy.get(strategy_key, 0.0)
                loss = max(0.0, -pnl)
                cd_remaining = self._get_cooldown_remaining(strategy_key)
                return {
                    "status": "ok",
                    "reason": f"策略 {strategy_id} 日内亏损汇总",
                    "data": {
                        "strategy_id": strategy_id,
                        "net_pnl": round(pnl, 2),
                        "loss": round(loss, 2),
                        "loss_pct": round(loss / equity, 4) if equity > 0 else 0.0,
                        "cooldown_remaining_sec": cd_remaining,
                        "equity": round(equity, 2),
                    },
                    "warnings": [],
                }

            # 全量汇总
            return {
                "status": "ok",
                "reason": "全量日内亏损汇总",
                "data": {
                    "global_pnl_raw": round(self._global_pnl, 2),
                    "global_loss": round(max(0.0, -self._global_pnl), 2),
                    "global_loss_pct": round(max(0.0, -self._global_pnl) / equity, 4) if equity > 0 else 0.0,
                    "by_strategy": {k: round(v, 2) for k, v in self._pnl_by_strategy.items()},
                    "by_symbol": {k: round(v, 2) for k, v in self._pnl_by_symbol.items()},
                    "rolling_window_trades": len(self._rolling_pnls),
                    "circuit_breakers": {
                        k: max(0, int(v - now)) for k, v in self._circuit_breakers.items()
                    },
                    "equity": round(equity, 2),
                },
                "warnings": [],
            }

    def reset_daily_losses(self, caller: str = "") -> Dict[str, Any]:
        """重置所有日内亏损计数器（仅限授权调用方）"""
        if caller not in self._authorized_reset_callers:
            logger.warning(f"未授权调用 reset_daily_losses: {caller}")
            return {
                "status": "error",
                "reason": f"调用方 {caller} 未授权执行重置操作",
                "data": {},
                "warnings": ["unauthorized_caller"],
            }
        with self._lock:
            before_global = self._global_pnl
            self._global_pnl = 0.0
            self._pnl_by_strategy.clear()
            self._pnl_by_symbol.clear()
            self._rolling_pnls.clear()
            self._rolling_net_pnl = 0.0
            self._circuit_breakers.clear()
            self._current_trade_date = ""
        logger.warning("日内亏损计数器已重置 (重置前全局净盈亏: %.2f) caller=%s", before_global, caller)
        return {
            "status": "ok",
            "reason": "日内亏损计数器已重置",
            "data": {"reset_time": time.time()},
            "warnings": [],
        }

    def update_whitelist(self, strategies: Optional[Set[str]] = None,
                         symbols: Optional[Set[str]] = None) -> Dict[str, Any]:
        """运行时热更新白名单"""
        with self._lock:
            if strategies is not None:
                self._allowed_strategies = set(strategies)
            if symbols is not None:
                self._allowed_symbols = set(symbols)
        logger.info("白名单已更新: strategies=%s symbols=%s", strategies, symbols)
        return {
            "status": "ok",
            "reason": "白名单已更新",
            "data": {
                "strategies_count": len(self._allowed_strategies) if self._allowed_strategies else 0,
                "symbols_count": len(self._allowed_symbols) if self._allowed_symbols else 0,
            },
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检（含滚动窗口一致性校验与自动修复）"""
        try:
            with self._lock:
                strategy_count = len(self._pnl_by_strategy)
                symbol_count = len(self._pnl_by_symbol)
                rolling_count = len(self._rolling_pnls)
                cb_count = len(self._circuit_breakers)

                # 一致性校验并自动修复
                if rolling_count > 0:
                    actual_sum = sum(itertools.islice(self._rolling_pnls,
                                                      max(0, rolling_count - self._rolling_window_trades),
                                                      None))
                else:
                    actual_sum = 0.0
                deviation = abs(actual_sum - self._rolling_net_pnl)
                consistent = deviation < self.DEFAULT_CONSISTENCY_TOLERANCE
                if not consistent:
                    logger.warning(
                        f"滚动窗口一致性修复: actual_sum={actual_sum:.2f} "
                        f"rolling_net_pnl={self._rolling_net_pnl:.2f}"
                    )
                    self._rolling_net_pnl = actual_sum
                    consistent = True

            return {
                "status": "ok" if consistent else "degraded",
                "reason": f"DailyLossLimiter 正常，追踪 {strategy_count} 策略 {symbol_count} 品种",
                "data": {
                    "strategy_count": strategy_count,
                    "symbol_count": symbol_count,
                    "rolling_trades": rolling_count,
                    "circuit_breaker_count": cb_count,
                    "rolling_consistent": consistent,
                    "dependencies": {
                        "account_ledger": self._account_ledger is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和计数器字典完整性")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _auto_reset_on_new_day(self) -> None:
        """自动检测日切并重置计数器（需在锁内调用）"""
        today = self._get_exchange_date()
        if today and today != self._current_trade_date:
            logger.info(f"检测到日切: {self._current_trade_date} -> {today}，自动重置亏损计数器")
            self._global_pnl = 0.0
            self._pnl_by_strategy.clear()
            self._pnl_by_symbol.clear()
            self._rolling_pnls.clear()
            self._rolling_net_pnl = 0.0
            self._circuit_breakers.clear()
            self._current_trade_date = today

    def _get_exchange_date(self) -> str:
        """获取交易所当前交易日期（UTC），优先使用交易所服务器时间（带超时）"""
        if self._account_ledger is not None and hasattr(self._account_ledger, 'get_server_time'):
            try:
                import threading as _th
                result = [None]
                def _fetch():
                    try:
                        result[0] = self._account_ledger.get_server_time()
                    except Exception:
                        pass
                t = _th.Thread(target=_fetch, daemon=True)
                t.start()
                t.join(self._server_time_timeout)
                if result[0] is not None:
                    ts = result[0]
                    if ts > 1e12:
                        ts /= 1000
                    return time.strftime("%Y%m%d", time.gmtime(ts))
            except Exception:
                pass
        return time.strftime("%Y%m%d", time.gmtime())

    def _get_current_equity(self) -> float:
        """获取当前账户权益，优先使用外部账本（带缓存有效期）"""
        if self._account_ledger is not None and hasattr(self._account_ledger, 'get_total_equity'):
            try:
                equity = self._account_ledger.get_total_equity()
                # 兼容 numpy 类型
                if hasattr(equity, 'item'):
                    equity = float(equity.item())
                else:
                    equity = float(equity)
                if math.isfinite(equity) and 0 < equity < 1e12:
                    self._cached_equity = equity
                    self._equity_cached_at = time.time()
                    self._last_equity_success_time = time.time()
                    self._equity_anomaly_count = 0
                    return equity
            except Exception as e:
                logger.warning(f"获取账户权益失败: {e}")

        # 降级：检查缓存有效期
        if self._cached_equity > 0 and (time.time() - self._equity_cached_at) < self._equity_cache_ttl_sec:
            return self._cached_equity
        return 0.0

    def _get_safe_equity(self) -> Optional[float]:
        """安全获取权益，返回 None 表示完全不可用"""
        equity = self._get_current_equity()
        if equity <= 0:
            # 检查权益异常窗口
            now = time.time()
            if self._last_equity_success_time > 0 and (now - self._last_equity_success_time) > self._equity_anomaly_window_sec:
                self._equity_anomaly_count = 0
            self._equity_anomaly_count += 1
            if self._equity_anomaly_count >= self._equity_anomaly_threshold:
                logger.error(f"权益连续 {self._equity_anomaly_count} 次异常，拒绝交易")
                return None
            # 尝试使用缓存
            if self._cached_equity > 0 and (now - self._equity_cached_at) < self._equity_cache_ttl_sec:
                return self._cached_equity
            return None
        else:
            self._equity_anomaly_count = 0
            self._last_equity_success_time = time.time()
            return equity

    def _get_current_session(self) -> str:
        """获取当前交易时段名称"""
        if self._is_crypto_market:
            return "crypto_24x7"
        hour = time.gmtime().tm_hour
        weekday = time.gmtime().tm_wday
        if weekday >= 5:
            return "weekend"
        for session, config in self._session_configs.items():
            hours = config.get("hours")
            if hours and hours[0] <= hour < hours[1]:
                return session
        return "asian"

    def _get_session_multiplier(self) -> float:
        """获取当前时段的亏损限额系数"""
        if self._is_crypto_market:
            return 1.0
        session = self._get_current_session()
        return self._session_configs.get(session, {}).get("multiplier", 1.0)

    def _get_cooldown_remaining(self, strategy_key: str) -> int:
        """获取指定策略的冷却剩余秒数（全局与策略级取最大值）"""
        now = time.time()
        strategy_cb = self._circuit_breakers.get(strategy_key, 0)
        global_cb = self._circuit_breakers.get("global", 0)
        latest_cb = max(strategy_cb, global_cb)
        return max(0, int(latest_cb - now))

    def _purge_expired_circuit_breakers(self) -> None:
        """清理已过期的熔断冷却条目（需在锁内调用）"""
        now = time.time()
        expired = [k for k, v in self._circuit_breakers.items() if v <= now]
        for k in expired:
            del self._circuit_breakers[k]

    def _trigger_hard_stop(
        self,
        strategy_id: str, symbol: str, limit_type: str,
        equity: float, global_loss_pct: float, strategy_loss_pct: float,
        symbol_loss_pct: float, rolling_loss_pct: float,
    ) -> None:
        """触发硬止损：发送强制平仓指令并记录日志（锁外调用）"""
        message = (
            f"硬止损触发: {limit_type} | 全局 {global_loss_pct:.2%} "
            f"策略 {strategy_id} {strategy_loss_pct:.2%} 品种 {symbol} {symbol_loss_pct:.2%} "
            f"滚动 {rolling_loss_pct:.2%} 权益 {equity:.0f}"
        )
        logger.error("%s #RECOVERY: 检查策略逻辑和风控参数", message)

        # 发送强制平仓指令（带超时）
        if self._negotiation_bus is not None:
            action = getattr(self._negotiation_bus, 'force_close', None) or \
                     getattr(self._negotiation_bus, 'publish_alert', None)
            if action:
                try:
                    import threading as _th
                    def _send():
                        try:
                            if action.__name__ == 'force_close':
                                action(strategy_id=strategy_id, symbol=symbol,
                                       reason=f"daily_loss_hard_stop: {limit_type}")
                            else:
                                action(alert_type="force_close", strategy_id=strategy_id,
                                       symbol=symbol, message=message)
                        except Exception as e:
                            logger.warning(f"总线指令发送失败: {e}")
                    t = _th.Thread(target=_send, daemon=True)
                    t.start()
                    t.join(self._bus_timeout_sec)
                except Exception as e:
                    logger.warning(f"总线调用异常: {e}")

        # 行为日志
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="daily_loss_hard_stop",
                    details={"strategy_id": strategy_id, "symbol": symbol,
                             "limit_type": limit_type, "global_pnl": round(self._global_pnl, 2),
                             "equity": round(equity, 2)}
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

    def _send_reduce_position_signal(self, strategy_id: str, symbol: str, rolling_loss_pct: float) -> None:
        """发送缩减仓位信号（锁外调用）"""
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                import threading as _th
                def _send():
                    try:
                        self._negotiation_bus.publish_alert(
                            alert_type="reduce_position",
                            strategy_id=strategy_id, symbol=symbol,
                            message=f"滚动窗口亏损 {rolling_loss_pct:.2%}，建议缩减仓位至 {(1-self._reduce_suggestion_pct)*100:.0f}%",
                            suggested_reduce_pct=self._reduce_suggestion_pct,
                            timestamp=time.time(),
                        )
                    except Exception as e:
                        logger.warning(f"缩减仓位信号发送失败: {e}")
                t = _th.Thread(target=_send, daemon=True)
                t.start()
                t.join(self._bus_timeout_sec)
            except Exception as e:
                logger.warning(f"缩减仓位总线调用异常: {e}")

    def _trim_dicts(self) -> None:
        """限制内部字典大小（需在锁内调用）"""
        if len(self._pnl_by_strategy) > self.DEFAULT_MAX_STRATEGY_ENTRIES * 1.2:
            sorted_items = sorted(self._pnl_by_strategy.items(), key=lambda x: abs(x[1]), reverse=True)
            self._pnl_by_strategy = dict(sorted_items[:self.DEFAULT_MAX_STRATEGY_ENTRIES])
            logger.debug("策略字典修剪完成，保留 %d 条", len(self._pnl_by_strategy))
        if len(self._pnl_by_symbol) > self.DEFAULT_MAX_SYMBOL_ENTRIES * 1.2:
            sorted_items = sorted(self._pnl_by_symbol.items(), key=lambda x: abs(x[1]), reverse=True)
            self._pnl_by_symbol = dict(sorted_items[:self.DEFAULT_MAX_SYMBOL_ENTRIES])
            logger.debug("品种字典修剪完成，保留 %d 条", len(self._pnl_by_symbol))


# ========== 自测代码 ==========
if __name__ == "__main__":
    print("DailyLossLimiter 模块自测...")
    limiter = DailyLossLimiter()

    # 模拟注入依赖
    class MockLedger:
        def get_total_equity(self): return 100000.0
        def get_server_time(self): return time.time()
    class MockBus:
        def publish_alert(self, **kwargs): pass
        def force_close(self, **kwargs): pass
    class MockLogger:
        def log_event(self, **kwargs): pass

    limiter.inject_dependencies(MockLedger(), MockBus(), MockLogger())

    # 测试正常盈亏
    result = limiter.check_loss_limit("test_strategy", "BTCUSDT", -500.0)
    print(f"亏损后: allowed={result['data']['allowed']}, global_loss={result['data']['global_loss_pct']:.4f}")

    result = limiter.check_loss_limit("test_strategy", "BTCUSDT", 300.0)
    print(f"盈利后: global_pnl_raw={result['data']['global_pnl_raw']}")

    print(f"健康检查: {limiter.health_check()['status']}")
    print("自测完成")
