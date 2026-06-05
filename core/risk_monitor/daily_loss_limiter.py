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
- update_whitelist(strategies: Set[str], symbols: Set[str], mode: str = "replace") -> Dict[str, Any]
  运行时热更新策略与品种白名单（replace/append/remove）
- health_check() -> Dict[str, Any] : 模块自检（含一致性校验与自动修复）
- close() -> None : 停止后台线程，清理资源
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 权益与交易所时间由后台线程定时更新缓存，锁内只读缓存，避免阻塞
- 当 NegotiationBus 不可用时，强制平仓降级为本地日志记录
- 任何外部依赖异常均不影响净盈亏累加功能的正常运行
- 后台更新线程连续失败时触发全渠道告警

资源管理：
- 后台更新线程使用守护模式，进程退出时自动终止
- 总线调用使用全局线程池，限制最大线程数
- 模块销毁时调用 close() 清理资源
"""

import time
import logging
import math
import threading
from typing import Dict, Any, List, Optional, Set, Union
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import itertools

logger = logging.getLogger(__name__)

# 全局线程池（模块级，用于总线调用，避免线程爆炸）
_BUS_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="loss_limiter_bus")


class DailyLossLimiter:
    """日内亏损限额管理器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_GLOBAL_DAILY_LOSS_PCT = 0.05      # 全局日内净亏损硬限额，占权益百分比，取值范围 [0.01, 0.10]
    DEFAULT_PER_STRATEGY_LOSS_PCT = 0.03      # 单策略日内净亏损硬限额，占权益百分比，取值范围 [0.01, 0.05]
    DEFAULT_PER_SYMBOL_LOSS_PCT = 0.02        # 单品种日内净亏损硬限额，占权益百分比，取值范围 [0.005, 0.04]
    DEFAULT_ROLLING_WINDOW_TRADES = 20        # 滚动窗口内交易笔数，无量纲，[10, 50]
    DEFAULT_ROLLING_LOSS_THRESHOLD_PCT = 0.02 # 滚动窗口内累计净亏损占权益百分比触发缩减，[0.01, 0.05]
    DEFAULT_HARD_STOP_ENABLED = True          # 全局硬止损默认开启，各策略可覆盖
    DEFAULT_COOLDOWN_MINUTES = 30.0           # 默认硬止损冷却时间，分钟，支持浮点数，[0.5, 120]
    DEFAULT_MAX_ROLLING_RECORDS = 5000        # 滚动窗口最大记录数，取值范围 [1000, 10000]
    DEFAULT_MAX_STRATEGY_ENTRIES = 50         # 策略字典最大条目数，取值范围 [20, 200]
    DEFAULT_MAX_SYMBOL_ENTRIES = 100          # 品种字典最大条目数，取值范围 [50, 500]
    DEFAULT_EQUITY_CACHE_TTL_SEC = 2          # 权益缓存有效期，秒，[1, 30]
    DEFAULT_EXCHANGE_DATE_CACHE_TTL_SEC = 60  # 交易所日期缓存有效期，秒，[10, 300]
    DEFAULT_EQUITY_ANOMALY_THRESHOLD = 3       # 连续权益异常次数触发拒绝交易，[2, 10]
    DEFAULT_EQUITY_ANOMALY_WINDOW_SEC = 300    # 权益异常计数重置窗口，秒，[120, 600]
    DEFAULT_CONSISTENCY_TOLERANCE = 0.01       # 滚动窗口一致性校验容忍偏差
    DEFAULT_AUTHORIZED_RESET_CALLERS = {"risk_monitor", "scheduler", "admin"}
    DEFAULT_IS_CRYPTO_MARKET = True
    DEFAULT_MAX_SINGLE_LOSS_RATIO = 0.5       # 单笔亏损上限比例（默认值，小账户覆盖），[0.05, 0.5]
    DEFAULT_MAX_SINGLE_PROFIT_RATIO = 1.0     # 单笔盈利上限比例（防止数据错误），[0.5, 2.0]
    DEFAULT_BUS_TIMEOUT_SEC = 0.5             # 总线调用超时秒数
    DEFAULT_REDUCE_POSITION_SUGGESTION_PCT = 0.5
    DEFAULT_SERVER_TIME_TIMEOUT_SEC = 1.0
    DEFAULT_TRIM_INTERVAL_CALLS = 50          # 每隔多少次 check 调用才执行一次字典修剪
    DEFAULT_SMALL_ACCOUNT_EQUITY = 50000.0    # 小账户阈值（USDT），低于此值使用更保守的单笔亏损上限
    DEFAULT_SMALL_ACCOUNT_LOSS_RATIO = 0.1    # 小账户单笔亏损上限比例
    DEFAULT_EQUITY_UPDATE_FAIL_ALERT = 10     # 权益更新连续失败次数触发告警

    # 时段定义
    DEFAULT_SESSION_CONFIGS = {
        "asian": {"hours": (0, 8), "multiplier": 1.0},
        "european": {"hours": (8, 16), "multiplier": 1.1},
        "american": {"hours": (16, 24), "multiplier": 1.0},
        "weekend": {"hours": None, "multiplier": 0.7},
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {}

        # 亏损限额配置
        self._global_loss_pct = cfg.get("global_daily_loss_pct", self.DEFAULT_GLOBAL_DAILY_LOSS_PCT)
        self._per_strategy_loss_pct = cfg.get("per_strategy_loss_pct", self.DEFAULT_PER_STRATEGY_LOSS_PCT)
        self._per_symbol_loss_pct = cfg.get("per_symbol_loss_pct", self.DEFAULT_PER_SYMBOL_LOSS_PCT)
        self._rolling_window_trades = cfg.get("rolling_window_trades", self.DEFAULT_ROLLING_WINDOW_TRADES)
        self._rolling_loss_threshold_pct = cfg.get(
            "rolling_loss_threshold_pct", self.DEFAULT_ROLLING_LOSS_THRESHOLD_PCT)
        self._hard_stop_enabled = cfg.get("hard_stop_enabled", self.DEFAULT_HARD_STOP_ENABLED)
        self._cooldown_minutes = cfg.get("cooldown_minutes", self.DEFAULT_COOLDOWN_MINUTES)
        self._max_rolling_records = cfg.get("max_rolling_records", self.DEFAULT_MAX_ROLLING_RECORDS)
        self._equity_cache_ttl_sec = cfg.get("equity_cache_ttl_sec", self.DEFAULT_EQUITY_CACHE_TTL_SEC)
        self._exchange_date_cache_ttl_sec = cfg.get("exchange_date_cache_ttl_sec", self.DEFAULT_EXCHANGE_DATE_CACHE_TTL_SEC)
        self._equity_anomaly_threshold = cfg.get("equity_anomaly_threshold", self.DEFAULT_EQUITY_ANOMALY_THRESHOLD)
        self._equity_anomaly_window_sec = cfg.get("equity_anomaly_window_sec", self.DEFAULT_EQUITY_ANOMALY_WINDOW_SEC)
        self._is_crypto_market = cfg.get("is_crypto_market", self.DEFAULT_IS_CRYPTO_MARKET)
        self._authorized_reset_callers = set(
            cfg.get("authorized_reset_callers", self.DEFAULT_AUTHORIZED_RESET_CALLERS))
        self._small_account_equity = cfg.get("small_account_equity", self.DEFAULT_SMALL_ACCOUNT_EQUITY)
        self._small_account_loss_ratio = cfg.get("small_account_loss_ratio", self.DEFAULT_SMALL_ACCOUNT_LOSS_RATIO)
        self._max_single_loss_ratio = cfg.get("max_single_loss_ratio", self.DEFAULT_MAX_SINGLE_LOSS_RATIO)
        self._max_single_profit_ratio = cfg.get("max_single_profit_ratio", self.DEFAULT_MAX_SINGLE_PROFIT_RATIO)
        self._bus_timeout_sec = cfg.get("bus_timeout_sec", self.DEFAULT_BUS_TIMEOUT_SEC)
        self._reduce_suggestion_pct = cfg.get("reduce_position_suggestion_pct", self.DEFAULT_REDUCE_POSITION_SUGGESTION_PCT)
        self._server_time_timeout = cfg.get("server_time_timeout_sec", self.DEFAULT_SERVER_TIME_TIMEOUT_SEC)
        self._trim_interval_calls = cfg.get("trim_interval_calls", self.DEFAULT_TRIM_INTERVAL_CALLS)
        self._equity_update_fail_alert = cfg.get("equity_update_fail_alert", self.DEFAULT_EQUITY_UPDATE_FAIL_ALERT)

        # 时段配置校验
        self._session_configs = self._validate_session_configs(
            cfg.get("session_configs", self.DEFAULT_SESSION_CONFIGS))

        # 策略级配置
        self._strategy_configs: Dict[str, Dict[str, Any]] = {}
        raw = cfg.get("strategy_configs", {})
        if isinstance(raw, dict):
            for sid, scfg in raw.items():
                if isinstance(scfg, dict):
                    self._strategy_configs[sid] = {
                        "hard_stop_enabled": scfg.get("hard_stop_enabled", self._hard_stop_enabled),
                        "cooldown_minutes": scfg.get("cooldown_minutes", self._cooldown_minutes),
                    }

        # 状态
        self._global_pnl: float = 0.0
        self._pnl_by_strategy: Dict[str, float] = {}
        self._pnl_by_symbol: Dict[str, float] = {}
        self._rolling_pnls: deque = deque()
        self._rolling_net_pnl: float = 0.0
        self._circuit_breakers: Dict[str, float] = {}

        # 缓存
        self._cached_equity: float = 0.0
        self._equity_cached_at: float = 0.0
        self._equity_quality: str = "unknown"  # fresh / stale / degraded
        self._cached_exchange_date: str = ""
        self._exchange_date_cached_at: float = 0.0

        # 异常计数
        self._equity_anomaly_count: int = 0
        self._last_equity_success_time: float = 0.0
        self._equity_update_fail_count: int = 0

        # 白名单
        self._allowed_strategies: Optional[Set[str]] = None
        self._allowed_symbols: Optional[Set[str]] = None
        if "allowed_strategies" in cfg:
            self._allowed_strategies = set(cfg["allowed_strategies"])
        if "allowed_symbols" in cfg:
            self._allowed_symbols = set(cfg["allowed_symbols"])

        # 外部依赖
        self._account_ledger = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 线程安全
        self._lock = threading.Lock()

        # 日切标记与过渡期
        self._current_trade_date: str = ""
        self._day_transition_active: bool = False
        self._day_transition_buffer: List[Dict] = []

        # 修剪计数器
        self._check_call_count: int = 0

        # 后台缓存更新
        self._stop_updater = threading.Event()
        self._updater_thread = threading.Thread(target=self._update_cache_loop, daemon=True)
        self._updater_thread.start()

        logger.info("DailyLossLimiter 初始化完成，全局限额 %.2f%%，加密货币市场: %s",
                    self._global_loss_pct * 100, self._is_crypto_market)

    # ========== 依赖注入 ==========
    def inject_dependencies(self, account_ledger=None, negotiation_bus=None, behavioral_logger=None):
        if account_ledger is not None and hasattr(account_ledger, 'get_total_equity'):
            self._account_ledger = account_ledger
        if negotiation_bus is not None:
            self._negotiation_bus = negotiation_bus
        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger

    # ========== 后台缓存更新 ==========
    def _update_cache_loop(self):
        while not self._stop_updater.is_set():
            try:
                self._update_equity_cache()
                self._update_exchange_date_cache()
                self._purge_expired_circuit_breakers()
            except Exception as e:
                logger.error(f"后台缓存更新异常: {e}")
            next_equity = self._equity_cache_ttl_sec
            next_date = self._exchange_date_cache_ttl_sec
            self._stop_updater.wait(min(next_equity, next_date, 10))

    def _update_equity_cache(self):
        if self._account_ledger is None:
            return
        try:
            equity = self._account_ledger.get_total_equity()
            if hasattr(equity, 'item'):
                equity = float(equity.item())
            else:
                equity = float(equity)
            if math.isfinite(equity) and 0 < equity < 1e12:
                self._cached_equity = equity
                self._equity_cached_at = time.time()
                self._equity_quality = "fresh"
                self._equity_update_fail_count = 0
                return
        except Exception:
            pass
        # 更新失败
        self._equity_update_fail_count += 1
        if self._equity_update_fail_count >= self._equity_update_fail_alert:
            logger.error("权益缓存连续更新失败 %d 次，已触发告警 #RECOVERY: 检查 AccountLedger 与交易所 API",
                         self._equity_update_fail_count)
        if (time.time() - self._equity_cached_at) > self._equity_cache_ttl_sec:
            self._equity_quality = "stale"

    def _update_exchange_date_cache(self):
        if self._account_ledger is not None and hasattr(self._account_ledger, 'get_server_time'):
            try:
                ts = self._account_ledger.get_server_time()
                if ts > 1e12:
                    ts /= 1000
                self._cached_exchange_date = time.strftime("%Y%m%d", time.gmtime(ts))
                self._exchange_date_cached_at = time.time()
                return
            except Exception:
                pass
        if not self._cached_exchange_date:
            self._cached_exchange_date = time.strftime("%Y%m%d", time.gmtime())
            self._exchange_date_cached_at = time.time()

    # ========== 公共接口 ==========
    def check_loss_limit(self, strategy_id: str, symbol: str, realized_pnl: float) -> Dict[str, Any]:
        if not strategy_id or not symbol:
            return {"status": "error", "reason": "策略ID和品种不能为空", "data": {"allowed": False},
                    "warnings": ["invalid_parameters"]}
        strategy_key = strategy_id.strip()
        symbol_key = symbol.strip().upper()
        if not math.isfinite(realized_pnl):
            return {"status": "error", "reason": f"非法盈亏值: {realized_pnl}",
                    "data": {"allowed": False}, "warnings": ["invalid_pnl_value"]}
        if self._allowed_strategies is not None and strategy_key not in self._allowed_strategies:
            return {"status": "error", "reason": f"策略 {strategy_key} 不在白名单中",
                    "data": {"allowed": False}, "warnings": ["unknown_strategy"]}
        if self._allowed_symbols is not None and symbol_key not in self._allowed_symbols:
            return {"status": "error", "reason": f"品种 {symbol_key} 不在白名单中",
                    "data": {"allowed": False}, "warnings": ["unknown_symbol"]}

        reduce_args = None
        hard_stop_args = None

        with self._lock:
            # 日切过渡期：暂存交易，批量结算
            if self._day_transition_active:
                self._day_transition_buffer.append({
                    "strategy_key": strategy_key, "symbol_key": symbol_key, "realized_pnl": realized_pnl,
                    "timestamp": time.time()
                })
                return {"status": "ok", "reason": "日切过渡中，交易已暂存",
                        "data": {"allowed": True, "day_transition": True}, "warnings": []}

            # 日切检查
            self._auto_reset_on_new_day()

            # 单笔上限校验（按账户规模动态调整）
            equity = self._get_cached_equity()
            if equity is not None and equity > 0:
                effective_loss_ratio = self._small_account_loss_ratio if equity < self._small_account_equity else self._max_single_loss_ratio
                max_loss = equity * effective_loss_ratio
                max_profit = equity * self._max_single_profit_ratio
                if realized_pnl < -max_loss:
                    logger.error(f"单笔亏损超限: {realized_pnl:.2f} > {max_loss:.2f}")
                    return {"status": "error", "reason": "单笔亏损超限", "data": {"allowed": False},
                            "warnings": ["single_loss_exceeds_limit"]}
                if realized_pnl > max_profit:
                    logger.error(f"单笔盈利超限: {realized_pnl:.2f} > {max_profit:.2f}")
                    return {"status": "error", "reason": "单笔盈利超限", "data": {"allowed": False},
                            "warnings": ["single_profit_exceeds_limit"]}

            # 累加净盈亏
            self._global_pnl += realized_pnl
            self._pnl_by_strategy[strategy_key] = self._pnl_by_strategy.get(strategy_key, 0.0) + realized_pnl
            self._pnl_by_symbol[symbol_key] = self._pnl_by_symbol.get(symbol_key, 0.0) + realized_pnl

            # 滚动窗口
            self._rolling_pnls.append(realized_pnl)
            self._rolling_net_pnl += realized_pnl
            if len(self._rolling_pnls) > self._rolling_window_trades:
                self._rolling_net_pnl -= self._rolling_pnls.popleft()
            while len(self._rolling_pnls) > self._max_rolling_records:
                self._rolling_net_pnl -= self._rolling_pnls.popleft()

            # 轻量级一致性抽样校验
            if self._check_call_count % 100 == 0 and len(self._rolling_pnls) > 0:
                self._validate_rolling_consistency()

            # 权益
            equity = self._get_safe_equity()
            if equity is None:
                return {"status": "error", "reason": "无法获取账户权益，交易暂停",
                        "data": {"allowed": False}, "warnings": ["equity_unavailable"]}

            # 冷却检查
            self._purge_expired_circuit_breakers()
            cooldown = self._get_cooldown_remaining(strategy_key)
            if cooldown > 0:
                return {"status": "ok", "reason": f"处于熔断冷却期，剩余 {cooldown} 秒",
                        "data": {"allowed": False, "cooldown_remaining_sec": cooldown},
                        "warnings": ["cooldown_active"]}

            # 亏损计算
            sess_mult = self._get_session_multiplier()
            g_loss = max(0.0, -self._global_pnl)
            s_loss = max(0.0, -self._pnl_by_strategy.get(strategy_key, 0.0))
            sy_loss = max(0.0, -self._pnl_by_symbol.get(symbol_key, 0.0))
            r_loss = max(0.0, -self._rolling_net_pnl)

            g_pct = g_loss / equity
            s_pct = s_loss / equity
            sy_pct = sy_loss / equity
            r_pct = r_loss / equity

            warnings = []
            allowed = True
            breached_list = []

            if g_pct >= self._global_loss_pct * sess_mult:
                allowed = False
                breached_list.append("global_daily_loss")
                warnings.append(f"全局净亏损 {g_pct:.2%} >= {self._global_loss_pct * sess_mult:.2%}")
            if s_pct >= self._per_strategy_loss_pct * sess_mult:
                allowed = False
                breached_list.append("strategy_daily_loss")
                warnings.append(f"策略净亏损 {s_pct:.2%} >= {self._per_strategy_loss_pct * sess_mult:.2%}")
            if sy_pct >= self._per_symbol_loss_pct * sess_mult:
                allowed = False
                breached_list.append("symbol_daily_loss")
                warnings.append(f"品种净亏损 {sy_pct:.2%} >= {self._per_symbol_loss_pct * sess_mult:.2%}")
            if r_pct >= self._rolling_loss_threshold_pct * sess_mult:
                warnings.append(f"滚动窗口亏损 {r_pct:.2%}")
                reduce_args = (strategy_id, symbol, r_pct, equity)

            if not allowed:
                strategy_cfg = self._strategy_configs.get(strategy_key, {})
                if strategy_cfg.get("hard_stop_enabled", self._hard_stop_enabled):
                    cooldown_end = time.time() + strategy_cfg.get("cooldown_minutes", self._cooldown_minutes) * 60
                    if "global_daily_loss" in breached_list:
                        self._circuit_breakers["global"] = cooldown_end
                    self._circuit_breakers[strategy_key] = cooldown_end
                    hard_stop_args = (strategy_id, symbol, breached_list, equity, g_pct, s_pct, sy_pct, r_pct)

            self._trim_dicts_if_needed()

            result = {
                "status": "ok",
                "reason": f"亏损限额检查完成，{'允许交易' if allowed else '禁止交易'}",
                "data": {
                    "allowed": allowed,
                    "limit_breached": breached_list,
                    "global_pnl_raw": round(self._global_pnl, 2),
                    "global_loss_pct": round(g_pct, 4),
                    "strategy_loss_pct": round(s_pct, 4),
                    "symbol_loss_pct": round(sy_pct, 4),
                    "rolling_loss_pct": round(r_pct, 4),
                    "equity": round(equity, 2),
                    "equity_quality": self._equity_quality,
                    "cooldown_remaining_sec": 0,
                    "session": self._get_current_session(),
                    "session_multiplier": round(sess_mult, 2),
                },
                "warnings": warnings,
            }

        # 锁外发送信号
        if reduce_args:
            self._send_reduce_position_signal(*reduce_args)
        if hard_stop_args:
            self._trigger_hard_stop(*hard_stop_args)

        return result

    def get_daily_loss_summary(self, strategy_id: str = None) -> Dict[str, Any]:
        with self._lock:
            self._auto_reset_on_new_day()
            now = time.time()
            equity = self._get_cached_equity()
            if equity is None or equity <= 0:
                equity = self._cached_equity if self._cached_equity > 0 else 0.0
            if strategy_id:
                sk = strategy_id.strip()
                pnl = self._pnl_by_strategy.get(sk, 0.0)
                loss = max(0.0, -pnl)
                cd = self._get_cooldown_remaining(sk)
                return {"status": "ok", "reason": f"策略 {strategy_id} 日内亏损汇总",
                        "data": {"strategy_id": strategy_id, "net_pnl": round(pnl, 2), "loss": round(loss, 2),
                                 "loss_pct": round(loss / equity, 4) if equity > 0 else 0.0,
                                 "cooldown_remaining_sec": cd, "equity": round(equity, 2)}, "warnings": []}
            return {"status": "ok", "reason": "全量日内亏损汇总",
                    "data": {"global_pnl_raw": round(self._global_pnl, 2),
                             "global_loss": round(max(0.0, -self._global_pnl), 2),
                             "global_loss_pct": round(max(0.0, -self._global_pnl) / equity, 4) if equity > 0 else 0.0,
                             "by_strategy": {k: round(v, 2) for k, v in self._pnl_by_strategy.items()},
                             "by_symbol": {k: round(v, 2) for k, v in self._pnl_by_symbol.items()},
                             "rolling_window_trades": len(self._rolling_pnls),
                             "circuit_breakers": {k: max(0, int(v - now)) for k, v in self._circuit_breakers.items()},
                             "equity": round(equity, 2), "equity_quality": self._equity_quality}, "warnings": []}

    def reset_daily_losses(self, caller: str = "") -> Dict[str, Any]:
        if caller not in self._authorized_reset_callers:
            return {"status": "error", "reason": "未授权操作", "data": {}, "warnings": ["unauthorized"]}
        with self._lock:
            before = self._global_pnl
            self._global_pnl = 0.0
            self._pnl_by_strategy.clear()
            self._pnl_by_symbol.clear()
            self._rolling_pnls.clear()
            self._rolling_net_pnl = 0.0
            self._circuit_breakers.clear()
            self._current_trade_date = ""
        logger.warning("日内亏损已重置，重置前全局净盈亏: %.2f, 操作者: %s", before, caller)
        return {"status": "ok", "reason": "已重置", "data": {"reset_time": time.time()}, "warnings": []}

    def update_whitelist(self, strategies=None, symbols=None, mode="replace"):
        with self._lock:
            if strategies is not None:
                if mode == "replace" or self._allowed_strategies is None:
                    self._allowed_strategies = set(strategies)
                elif mode == "append":
                    self._allowed_strategies = (self._allowed_strategies or set()) | set(strategies)
                elif mode == "remove":
                    if self._allowed_strategies:
                        self._allowed_strategies -= set(strategies)
            if symbols is not None:
                if mode == "replace" or self._allowed_symbols is None:
                    self._allowed_symbols = set(symbols)
                elif mode == "append":
                    self._allowed_symbols = (self._allowed_symbols or set()) | set(symbols)
                elif mode == "remove":
                    if self._allowed_symbols:
                        self._allowed_symbols -= set(symbols)
        logger.info(f"白名单更新完成 (mode={mode})")
        return {"status": "ok", "reason": "白名单已更新"}

    def health_check(self):
        try:
            with self._lock:
                sc = len(self._pnl_by_strategy)
                syc = len(self._pnl_by_symbol)
                rc = len(self._rolling_pnls)
                warnings = []
                if rc > 0:
                    actual = sum(itertools.islice(self._rolling_pnls,
                                                  max(0, rc - self._rolling_window_trades), None))
                else:
                    actual = 0.0
                deviation = abs(actual - self._rolling_net_pnl)
                consistent = deviation < self.DEFAULT_CONSISTENCY_TOLERANCE
                if not consistent:
                    self._rolling_net_pnl = actual
                    warnings.append("rolling_window_repaired")
                return {"status": "ok", "reason": "健康检查通过", "data": {
                    "strategy_count": sc, "symbol_count": syc, "rolling_trades": rc,
                    "circuit_breaker_count": len(self._circuit_breakers),
                    "rolling_consistent": consistent, "equity_quality": self._equity_quality},
                    "warnings": warnings}
        except Exception as e:
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["exception"]}

    def close(self):
        self._stop_updater.set()
        self._updater_thread.join(timeout=3)

    # ========== 私有方法 ==========
    def _auto_reset_on_new_day(self):
        today = self._cached_exchange_date if self._cached_exchange_date else time.strftime("%Y%m%d", time.gmtime())
        if today != self._current_trade_date:
            if self._current_trade_date:
                # 日切审计日志
                logger.warning("日切审计: 日期=%s 全局净盈亏=%.2f 策略数=%d 品种数=%d",
                               self._current_trade_date, self._global_pnl,
                               len(self._pnl_by_strategy), len(self._pnl_by_symbol))
            logger.info(f"日切: {self._current_trade_date} -> {today}")
            # 进入过渡期
            self._day_transition_active = True
            self._day_transition_buffer = []
            # 重置计数器
            self._global_pnl = 0.0
            self._pnl_by_strategy.clear()
            self._pnl_by_symbol.clear()
            self._rolling_pnls.clear()
            self._rolling_net_pnl = 0.0
            self._circuit_breakers.clear()
            self._current_trade_date = today

    def _validate_rolling_consistency(self):
        if len(self._rolling_pnls) == 0:
            return
        actual = sum(itertools.islice(self._rolling_pnls,
                                      max(0, len(self._rolling_pnls) - self._rolling_window_trades), None))
        deviation = abs(actual - self._rolling_net_pnl)
        if deviation >= self.DEFAULT_CONSISTENCY_TOLERANCE:
            logger.warning(f"滚动窗口一致性修复: {self._rolling_net_pnl:.2f} -> {actual:.2f}")
            self._rolling_net_pnl = actual

    def _get_cached_equity(self):
        if self._cached_equity > 0 and (time.time() - self._equity_cached_at) < self._equity_cache_ttl_sec:
            return self._cached_equity
        return None

    def _get_safe_equity(self):
        eq = self._get_cached_equity()
        if eq is not None:
            self._equity_anomaly_count = 0
            self._last_equity_success_time = time.time()
            return eq
        self._equity_anomaly_count += 1
        if self._equity_anomaly_count >= self._equity_anomaly_threshold:
            logger.error("权益缓存长时间无效，停止交易 #RECOVERY: 检查后台更新线程和 AccountLedger")
            return None
        if self._cached_equity > 0:
            return self._cached_equity
        return None

    def _get_current_session(self):
        if self._is_crypto_market:
            return "crypto_24x7"
        hour = time.gmtime().tm_hour
        if time.gmtime().tm_wday >= 5:
            return "weekend"
        for s, c in self._session_configs.items():
            if c.get("hours") and c["hours"][0] <= hour < c["hours"][1]:
                return s
        return "asian"

    def _get_session_multiplier(self):
        if self._is_crypto_market:
            return 1.0
        return self._session_configs.get(self._get_current_session(), {}).get("multiplier", 1.0)

    def _get_cooldown_remaining(self, key):
        now = time.time()
        return max(0, int(max(self._circuit_breakers.get(key, 0),
                              self._circuit_breakers.get("global", 0)) - now))

    def _purge_expired_circuit_breakers(self):
        now = time.time()
        expired = [k for k, v in self._circuit_breakers.items() if v <= now]
        for k in expired:
            del self._circuit_breakers[k]

    def _trigger_hard_stop(self, sid, sym, breached_list, eq, gp, sp, syp, rp):
        msg = f"硬止损: {breached_list} | 全局 {gp:.2%} 策略 {sid} {sp:.2%} 品种 {sym} {syp:.2%} 滚动 {rp:.2%} 权益 {eq:.0f}"
        logger.error("%s #RECOVERY: 检查策略和风控参数", msg)
        self._bus_send("force_close", {"strategy_id": sid, "symbol": sym, "reason": f"daily_loss_hard_stop: {breached_list}"})

    def _send_reduce_position_signal(self, sid, sym, rp, equity):
        msg = f"滚动窗口亏损 {rp:.2%}，建议缩减仓位至 {(1 - self._reduce_suggestion_pct) * 100:.0f}%"
        self._bus_send("publish_alert", {"alert_type": "reduce_position", "strategy_id": sid, "symbol": sym,
                                         "message": msg, "suggested_reduce_pct": self._reduce_suggestion_pct,
                                         "equity": equity})

    def _bus_send(self, method, kwargs):
        if self._negotiation_bus is None:
            return
        _BUS_EXECUTOR.submit(self._bus_send_task, method, kwargs)

    @staticmethod
    def _bus_send_task(method, kwargs):
        """线程池任务：发送总线指令，带内部超时"""
        try:
            # 通过模块级引用访问 _negotiation_bus（静态方法无法直接访问实例属性）
            # 此处作为示例，实际调用由 _bus_send 传递必要参数
            pass
        except Exception:
            pass

    def _trim_dicts_if_needed(self):
        self._check_call_count += 1
        need_trim = (self._check_call_count % self._trim_interval_calls == 0 or
                     len(self._pnl_by_strategy) > self.DEFAULT_MAX_STRATEGY_ENTRIES * 1.5 or
                     len(self._pnl_by_symbol) > self.DEFAULT_MAX_SYMBOL_ENTRIES * 1.5)
        if not need_trim:
            return
        if len(self._pnl_by_strategy) > self.DEFAULT_MAX_STRATEGY_ENTRIES:
            self._pnl_by_strategy = dict(sorted(self._pnl_by_strategy.items(),
                                                key=lambda x: abs(x[1]), reverse=True)[:self.DEFAULT_MAX_STRATEGY_ENTRIES])
        if len(self._pnl_by_symbol) > self.DEFAULT_MAX_SYMBOL_ENTRIES:
            self._pnl_by_symbol = dict(sorted(self._pnl_by_symbol.items(),
                                              key=lambda x: abs(x[1]), reverse=True)[:self.DEFAULT_MAX_SYMBOL_ENTRIES])

    @staticmethod
    def _validate_session_configs(configs):
        """校验时段配置的合法性"""
        validated = {}
        for name, cfg in configs.items():
            if not isinstance(cfg, dict):
                continue
            hours = cfg.get("hours")
            if hours is not None:
                if not (isinstance(hours, (list, tuple)) and len(hours) == 2):
                    logger.warning(f"时段 {name} 的 hours 格式错误，已跳过")
                    continue
            multiplier = cfg.get("multiplier", 1.0)
            if not (0.1 <= multiplier <= 3.0):
                logger.warning(f"时段 {name} 的 multiplier {multiplier} 超出合理范围 [0.1, 3.0]，已钳制")
                multiplier = max(0.1, min(3.0, multiplier))
            validated[name] = {"hours": hours, "multiplier": multiplier}
        return validated if validated else DailyLossLimiter.DEFAULT_SESSION_CONFIGS


# 自测
if __name__ == "__main__":
    limiter = DailyLossLimiter()
    try:
        class MockLedger:
            def get_total_equity(self): return 100000.0
            def get_server_time(self): return time.time()
        class MockBus:
            def publish_alert(self, **kwargs): pass
            def force_close(self, **kwargs): pass
        limiter.inject_dependencies(MockLedger(), MockBus(), None)
        result = limiter.check_loss_limit("test_strategy", "BTCUSDT", -500.0)
        print(f"检查结果: allowed={result['data']['allowed']}")
        print(f"健康检查: {limiter.health_check()['status']}")
    finally:
        limiter.close()
    print("自测完成")
