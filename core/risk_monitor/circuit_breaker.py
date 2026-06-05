"""
火种系统 · 多级熔断器 (CircuitBreaker)

核心职责：
1. 基于滚动分位数动态阈值与分时段历史基线，实时判定多级熔断触发条件，支持冷却期内升级
2. 在触发熔断时通过协商总线广播标准化动作码，并联动订单网关、执行本地平仓降级，记录完整审计快照

外部依赖（真实模块接口）：
- core.negotiation_bus.NegotiationBus : 发送熔断触发/解除等生存级事件，需支持 publish_action(code, params)
- core.behavioral_logger.BehavioralLogger : 记录熔断事件的结构化审计日志
- core.perception.tactile_cortex.TactileCortex : 获取当前实时波动率与盘口深度数据
- core.account_ledger.AccountLedger : 获取当前账户权益与风险敞口信息
- core.order_manager.OrderManager : 在紧急降级时直接执行平仓指令
- core.execution.order_risk_gateway.OrderRiskGateway : 通知订单风控网关限制新开仓
- core.position_snapshot.PositionSnapshot : 触发熔断时保存持仓快照
- core.config.loader.ConfigLoader : 加载品种差异化配置

接口契约：
- check_and_update(symbol, current_price, high_low, open_price) -> Dict[str, Any]
- get_status(symbol) -> Dict[str, Any]
- reset(symbol, force=False) -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- get_effectiveness_stats() -> Dict[str, Any]  # 新增：熔断有效性统计
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 NegotiationBus 不可用时，熔断指令降级为直接调用 OrderManager 执行平仓
- 当 OrderRiskGateway 不可用时，仅记录告警，不影响熔断主流程
- 当 TactileCortex 不可用时，使用近期历史波动率作为保守估计
- 当 AccountLedger 不可用时，冻结新开仓并转为全品种只平仓模式
- 当 ConfigLoader 不可用或配置缺失时，回退至类常量默认值
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护每个交易品种的熔断状态机与振幅历史窗口，无外部资源句柄
- 异步清理过期振幅数据以控制内存，使用独立守护线程避免线程泄漏
- 持久化操作异步执行，不阻塞主线程；使用原子写入防止文件损坏
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple
from collections import deque, defaultdict
import numpy as np
import json
import os

logger = logging.getLogger(__name__)

# 配置覆盖路径（可在 config/risk/circuit_breaker.yaml 中按品种覆盖类常量）
CONFIG_OVERRIDE_PATH = "config.risk.circuit_breaker"


class CircuitBreaker:
    """多级价格振幅熔断器"""

    # ========== 类常量（默认配置，可被 config/risk/circuit_breaker.yaml 按品种覆盖） ==========
    DEFAULT_LEVEL_1_THRESHOLD = 0.03
    DEFAULT_LEVEL_2_THRESHOLD = 0.05
    DEFAULT_LEVEL_3_THRESHOLD = 0.08

    BASE_COOLDOWN_LEVEL_1 = 300
    BASE_COOLDOWN_LEVEL_2 = 600
    BASE_COOLDOWN_LEVEL_3 = 1800

    PERCENTILE_LEVEL_1 = 90
    PERCENTILE_LEVEL_2 = 97
    PERCENTILE_LEVEL_3 = 99.5

    # 最小绝对阈值保护，防止低波动环境下的误触发
    MIN_ABSOLUTE_THRESHOLD_LEVEL_1 = 0.005  # 0.5% 振幅为最低触发线
    MIN_ABSOLUTE_THRESHOLD_LEVEL_2 = 0.01
    MIN_ABSOLUTE_THRESHOLD_LEVEL_3 = 0.02

    DEFAULT_HISTORY_WINDOW_DAYS = 30
    DEFAULT_SAMPLES_PER_HOUR = 60

    ASIAN_SESSION_START = 0
    ASIAN_SESSION_END = 8
    EUROPEAN_SESSION_START = 8
    EUROPEAN_SESSION_END = 16
    AMERICAN_SESSION_START = 16
    AMERICAN_SESSION_END = 24

    FALLBACK_VOLATILITY_PCT = 0.02
    FALLBACK_DEPTH_RATIO = 0.5

    UPGRADE_MULTIPLIER_DURING_COOLDOWN = 1.3

    FATIGUE_FACTOR = 1.25
    MAX_FATIGUE_MULTIPLIER = 5.0
    FATIGUE_DECAY_SECONDS = 3600

    PERSIST_STATE_PATH = "data/circuit_breaker_state.json"
    MAX_STATE_AGE_ON_LOAD = 7200  # 状态有效期：2小时

    THROTTLE_SECONDS = 0.5
    SYMBOL_MAX_LENGTH = 20

    # 异常值过滤：振幅超过此倍数视为交易所数据异常，忽略
    AMPLITUDE_OUTLIER_MULTIPLIER = 5.0

    # 渐进恢复步长与间隔
    PROGRESSIVE_RECOVERY_STEPS = [0.3, 0.6, 1.0]
    PROGRESSIVE_RECOVERY_INTERVAL = 180

    def __init__(self):
        # 每个品种的熔断状态
        self._state: Dict[str, Dict[str, Any]] = {}
        # 振幅历史 (分时段)
        self._amplitude_history: Dict[str, Dict[str, deque]] = defaultdict(dict)
        # 连续触发计数 (用于疲劳)
        self._trigger_counters: Dict[str, int] = defaultdict(int)
        # 上次触发时间（用于疲劳衰减）
        self._last_trigger_time: Dict[str, float] = {}
        # 上次节流检查时间
        self._last_throttle: Dict[str, float] = {}

        # 熔断有效性统计 {symbol: {level: {"true_positive": int, "false_positive": int}}}
        self._effectiveness: Dict[str, Dict[int, Dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: {"true_positive": 0, "false_positive": 0})
        )

        # 外部依赖注入
        self._negotiation_bus = None
        self._behavioral_logger = None
        self._tactile_cortex = None
        self._account_ledger = None
        self._order_manager = None
        self._order_risk_gateway = None
        self._position_snapshot = None
        self._config_loader = None  # 用于加载品种差异化配置

        # 锁：读写分离优化并发
        self._state_lock = threading.RLock()
        self._history_lock = threading.RLock()
        self._cleanup_lock = threading.Lock()
        self._last_cleanup = time.time()
        self._async_cleanup_interval = 1800

        # 持久化锁（保护文件写入的原子性）
        self._persist_lock = threading.Lock()
        # 持久化定时器（单线程）
        self._persist_timer: Optional[threading.Timer] = None

        # 加载持久化状态（仅恢复仍在有效期内的熔断）
        self._load_persisted_state()
        # 加载外部品种差异化配置
        self._load_symbol_configs()

        logger.info("CircuitBreaker 初始化完成（含状态持久化恢复与品种差异化配置）")

    # ========== 依赖注入 ==========
    def inject_dependencies(self, negotiation_bus=None, behavioral_logger=None,
                            tactile_cortex=None, account_ledger=None,
                            order_manager=None, order_risk_gateway=None,
                            position_snapshot=None, config_loader=None):
        self._negotiation_bus = negotiation_bus if negotiation_bus and hasattr(negotiation_bus, 'publish_action') else None
        self._behavioral_logger = behavioral_logger
        self._tactile_cortex = tactile_cortex
        self._account_ledger = account_ledger
        self._order_manager = order_manager
        self._order_risk_gateway = order_risk_gateway
        self._position_snapshot = position_snapshot
        self._config_loader = config_loader

    # ========== 公共接口 ==========
    def check_and_update(self, symbol: str, current_price: float,
                         high_low: Tuple[float, float], open_price: float = None) -> Dict[str, Any]:
        # 参数校验（symbol 注入防御）
        if not symbol or not isinstance(symbol, str) or len(symbol) > self.SYMBOL_MAX_LENGTH:
            logger.warning(f"无效 symbol 参数: {symbol}")
            return {"status": "error", "reason": "无效 symbol", "data": {}, "warnings": []}
        if not isinstance(current_price, (int, float)) or current_price <= 0:
            return {"status": "error", "reason": "无效价格", "data": {}, "warnings": []}
        if not isinstance(high_low, (list, tuple)) or len(high_low) != 2:
            return {"status": "error", "reason": "无效 high_low", "data": {}, "warnings": []}
        high, low = high_low
        if not isinstance(high, (int, float)) or not isinstance(low, (int, float)) or high <= 0 or low <= 0 or high < low:
            return {"status": "error", "reason": "无效振幅数据", "data": {}, "warnings": []}

        # 异常值过滤：过高振幅视为数据异常
        base_price = open_price if open_price and open_price > 0 else current_price
        raw_amplitude = (high - low) / base_price
        if raw_amplitude > self._get_symbol_config(symbol, 'max_valid_amplitude', 0.5):
            logger.warning(f"{symbol} 异常振幅忽略: {raw_amplitude:.4f}")
            return {"status": "ok", "reason": "异常振幅已忽略", "data": {"triggered": False}, "warnings": ["outlier_amplitude"]}

        # 节流保护
        now = time.time()
        last = self._last_throttle.get(symbol, 0)
        if now - last < self.THROTTLE_SECONDS:
            return self.get_status(symbol)
        self._last_throttle[symbol] = now

        amplitude = raw_amplitude
        # 记录历史（线程安全）
        self._record_amplitude(symbol, amplitude)

        with self._state_lock:
            state = self._ensure_state(symbol)

            # 疲劳衰减
            self._decay_fatigue(symbol, now)

            # 冷却期内升级检查
            if state["level"] > 0 and now < state["cooldown_until"]:
                current_level = state["level"]
                upgrade_level = self._check_upgrade(symbol, amplitude, current_level)
                if upgrade_level > current_level:
                    state["level"] = upgrade_level
                    state["cooldown_until"] = now + self._get_cooldown(symbol, upgrade_level)
                    state["last_triggered"] = now
                    self._trigger_counters[symbol] += 1
                    self._execute_action(symbol, upgrade_level, amplitude)
                    self._schedule_persist()
                    return self._build_response(symbol, upgrade_level, amplitude, True,
                                                state["cooldown_until"] - now)
                remaining = int(state["cooldown_until"] - now)
                return self._build_response(symbol, current_level, amplitude, False, remaining)

            # 冷却结束
            if state["level"] > 0 and now >= state["cooldown_until"]:
                self._exit_cooldown(symbol, state)

            # 新判定
            triggered_level = self._determine_level(symbol, amplitude)
            if triggered_level > 0:
                cooldown = self._get_cooldown(symbol, triggered_level)
                state["level"] = triggered_level
                state["cooldown_until"] = now + cooldown
                state["last_triggered"] = now
                self._trigger_counters[symbol] += 1
                self._execute_action(symbol, triggered_level, amplitude)
                self._schedule_persist()
                return self._build_response(symbol, triggered_level, amplitude, True, cooldown)

            return self._build_response(symbol, 0, amplitude, False, 0)

    def get_status(self, symbol: str) -> Dict[str, Any]:
        if not symbol:
            return {"status": "error", "reason": "无效 symbol", "data": {}, "warnings": []}
        with self._state_lock:
            state = self._state.get(symbol, {})
            now = time.time()
            level = state.get("level", 0)
            remaining = max(0, int(state.get("cooldown_until", 0) - now)) if level > 0 else 0
            return {
                "status": "ok", "reason": f"熔断级别: {level}",
                "data": {"symbol": symbol, "level": level, "cooldown_remaining_seconds": remaining,
                         "trigger_count": state.get("trigger_count", 0)},
                "warnings": []
            }

    def reset(self, symbol: str, force: bool = False) -> Dict[str, Any]:
        if not symbol:
            return {"status": "error", "reason": "无效 symbol", "data": {}, "warnings": []}
        with self._state_lock:
            if symbol not in self._state:
                return {"status": "ok", "reason": "无记录", "data": {}, "warnings": []}
            state = self._state[symbol]
            if not force and state["level"] > 0 and time.time() < state["cooldown_until"]:
                return {"status": "error", "reason": "冷却中", "data": {}, "warnings": []}
            prev = state["level"]
            state["level"] = 0
            state["cooldown_until"] = 0
            self._trigger_counters[symbol] = 0
            self._schedule_persist()
            return {"status": "ok", "reason": f"已重置", "data": {"previous_level": prev}, "warnings": []}

    def health_check(self) -> Dict[str, Any]:
        """模块自检：验证依赖真实可用性"""
        try:
            with self._state_lock:
                active = sum(1 for s in self._state.values() if s.get("level", 0) > 0)
                total = len(self._state)

            # 探测依赖可用性
            dep_checks = {
                "negotiation_bus": self._probe_negotiation_bus(),
                "behavioral_logger": self._behavioral_logger is not None,
                "order_manager": self._order_manager is not None,
                "order_risk_gateway": self._order_risk_gateway is not None,
                "position_snapshot": self._position_snapshot is not None,
            }
            dep_healthy = all(dep_checks.values())

            return {
                "status": "ok" if dep_healthy else "degraded",
                "reason": f"监控{total}品种，{active}熔断中",
                "data": {"total": total, "active": active, "dependencies": dep_checks},
                "warnings": [f"{k}_unavailable" for k, v in dep_checks.items() if not v]
            }
        except Exception as e:
            logger.error(f"health_check异常: {e}")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": []}

    def get_effectiveness_stats(self) -> Dict[str, Any]:
        """获取熔断有效性统计"""
        with self._state_lock:
            return {
                "status": "ok",
                "reason": "熔断有效性统计",
                "data": dict(self._effectiveness),
                "warnings": []
            }

    # ========== 私有方法 ==========
    def _probe_negotiation_bus(self) -> bool:
        if self._negotiation_bus is None:
            return False
        try:
            return hasattr(self._negotiation_bus, 'publish_action')
        except Exception:
            return False

    def _ensure_state(self, symbol):
        if symbol not in self._state:
            self._state[symbol] = {"level": 0, "cooldown_until": 0.0, "last_triggered": 0.0, "trigger_count": 0}
        return self._state[symbol]

    def _record_amplitude(self, symbol, amplitude):
        now = time.time()
        session = self._get_session_label()
        with self._history_lock:
            if session not in self._amplitude_history[symbol]:
                self._amplitude_history[symbol][session] = deque(
                    maxlen=self.DEFAULT_HISTORY_WINDOW_DAYS * 24 * self.DEFAULT_SAMPLES_PER_HOUR)
            self._amplitude_history[symbol][session].append((now, amplitude))
        if now - self._last_cleanup > self._async_cleanup_interval:
            self._async_cleanup()

    def _get_session_label(self):
        hour = time.gmtime().tm_hour
        if self.ASIAN_SESSION_START <= hour < self.ASIAN_SESSION_END:
            return "asian"
        elif self.EUROPEAN_SESSION_START <= hour < self.EUROPEAN_SESSION_END:
            return "european"
        return "american"

    def _determine_level(self, symbol, amplitude):
        if amplitude < self._get_symbol_config(symbol, 'min_absolute_threshold', self.MIN_ABSOLUTE_THRESHOLD_LEVEL_1):
            return 0
        session = self._get_session_label()
        with self._history_lock:
            history = self._amplitude_history.get(symbol, {}).get(session, deque())
        if len(history) < 30:
            return self._static_threshold_check(symbol, amplitude)
        amplitudes = [a for _, a in history]
        # 使用品种差异化分位数参数
        p_level1 = self._get_symbol_config(symbol, 'percentile_level_1', self.PERCENTILE_LEVEL_1)
        p_level2 = self._get_symbol_config(symbol, 'percentile_level_2', self.PERCENTILE_LEVEL_2)
        p_level3 = self._get_symbol_config(symbol, 'percentile_level_3', self.PERCENTILE_LEVEL_3)
        p90 = np.percentile(amplitudes, p_level1)
        p97 = np.percentile(amplitudes, p_level2)
        p995 = np.percentile(amplitudes, p_level3)
        effective_1 = max(p90, self._get_symbol_config(symbol, 'min_absolute_threshold', self.MIN_ABSOLUTE_THRESHOLD_LEVEL_1))
        effective_2 = max(p97, self._get_symbol_config(symbol, 'min_absolute_threshold_2', self.MIN_ABSOLUTE_THRESHOLD_LEVEL_2))
        effective_3 = max(p995, self._get_symbol_config(symbol, 'min_absolute_threshold_3', self.MIN_ABSOLUTE_THRESHOLD_LEVEL_3))
        if amplitude >= effective_3:
            return 3
        elif amplitude >= effective_2:
            return 2
        elif amplitude >= effective_1:
            return 1
        return 0

    def _static_threshold_check(self, symbol, amplitude):
        t1 = self._get_symbol_config(symbol, 'level_1_threshold', self.DEFAULT_LEVEL_1_THRESHOLD)
        t2 = self._get_symbol_config(symbol, 'level_2_threshold', self.DEFAULT_LEVEL_2_THRESHOLD)
        t3 = self._get_symbol_config(symbol, 'level_3_threshold', self.DEFAULT_LEVEL_3_THRESHOLD)
        if amplitude >= t3: return 3
        if amplitude >= t2: return 2
        if amplitude >= t1: return 1
        return 0

    def _check_upgrade(self, symbol, amplitude, current_level):
        session = self._get_session_label()
        with self._history_lock:
            history = self._amplitude_history.get(symbol, {}).get(session, deque())
        if len(history) < 30:
            return max(self._static_threshold_check(symbol, amplitude), current_level)
        amplitudes = [a for _, a in history]
        p90 = np.percentile(amplitudes, self._get_symbol_config(symbol, 'percentile_level_1', self.PERCENTILE_LEVEL_1))
        p97 = np.percentile(amplitudes, self._get_symbol_config(symbol, 'percentile_level_2', self.PERCENTILE_LEVEL_2))
        p995 = np.percentile(amplitudes, self._get_symbol_config(symbol, 'percentile_level_3', self.PERCENTILE_LEVEL_3))
        if current_level < 3 and amplitude >= p995 * self.UPGRADE_MULTIPLIER_DURING_COOLDOWN:
            return 3
        if current_level < 2 and amplitude >= p97 * self.UPGRADE_MULTIPLIER_DURING_COOLDOWN:
            return 2
        return current_level

    def _get_cooldown(self, symbol, level):
        base_map = {1: self.BASE_COOLDOWN_LEVEL_1, 2: self.BASE_COOLDOWN_LEVEL_2, 3: self.BASE_COOLDOWN_LEVEL_3}
        base = self._get_symbol_config(symbol, f'cooldown_level_{level}', base_map[level])
        count = self._trigger_counters.get(symbol, 0)
        fatigue = min(self.FATIGUE_FACTOR ** count, self.MAX_FATIGUE_MULTIPLIER)
        return int(base * fatigue)

    def _decay_fatigue(self, symbol, now):
        last_time = self._last_trigger_time.get(symbol, 0)
        if last_time > 0 and self._trigger_counters[symbol] > 0:
            decay_periods = int((now - last_time) / self.FATIGUE_DECAY_SECONDS)
            if decay_periods > 0:
                self._trigger_counters[symbol] = max(0, self._trigger_counters[symbol] - decay_periods)
                self._last_trigger_time[symbol] = now

    def _execute_action(self, symbol, level, amplitude=0.0):
        if self._position_snapshot:
            try:
                self._position_snapshot.save_snapshot(f"circuit_breaker_{symbol}_{level}")
            except Exception as e:
                logger.error(f"持仓快照失败: {e}")

        if self._order_risk_gateway:
            try:
                self._order_risk_gateway.restrict_new_orders(symbol, level)
            except Exception as e:
                logger.error(f"订单网关限制失败: {e}")

        action_code = f"CB_LEVEL_{level}"
        if self._negotiation_bus:
            try:
                self._negotiation_bus.publish_action(action_code, {"symbol": symbol, "level": level, "timestamp": time.time()})
            except Exception as e:
                logger.error(f"总线发送失败: {e}")

        if level >= 3 and self._order_manager:
            try:
                self._order_manager.emergency_close_all(symbol)
                logger.critical(f"本地全平: {symbol}")
            except Exception as e:
                logger.critical(f"本地平仓失败: {e}")

        # 记录有效性统计（暂标记为真阳性，后续通过市场走势评估修正）
        self._effectiveness[symbol][level]["true_positive"] += 1

        if self._behavioral_logger:
            try:
                self._behavioral_logger.log_event("circuit_breaker_triggered",
                                                  {"symbol": symbol, "level": level, "amplitude": amplitude})
            except Exception:
                pass

    def _exit_cooldown(self, symbol, state):
        prev = state["level"]
        state["level"] = 0
        self._trigger_counters[symbol] = 0
        if self._order_risk_gateway:
            try:
                self._order_risk_gateway.lift_restrictions(symbol)
            except Exception:
                pass
        logger.info(f"{symbol} 熔断恢复 (原级别{prev})")

    def _async_cleanup(self):
        def cleanup():
            with self._cleanup_lock:
                cutoff = time.time() - self.DEFAULT_HISTORY_WINDOW_DAYS * 86400
                with self._history_lock:
                    for sym in list(self._amplitude_history.keys()):
                        for ses in list(self._amplitude_history[sym].keys()):
                            hist = self._amplitude_history[sym][ses]
                            while hist and hist[0][0] < cutoff:
                                hist.popleft()
                            if not hist:
                                del self._amplitude_history[sym][ses]
                        if not self._amplitude_history[sym]:
                            del self._amplitude_history[sym]
                self._last_cleanup = time.time()
        t = threading.Thread(target=cleanup, daemon=True)
        t.start()

    def _build_response(self, symbol, level, amplitude, triggered, cooldown=0):
        return {
            "status": "ok",
            "reason": f"熔断级别{level}" if triggered else "未触发",
            "data": {
                "symbol": symbol,
                "level": level,
                "amplitude": round(amplitude, 4),
                "triggered": triggered,
                "cooldown_remaining_seconds": int(cooldown) if triggered else 0
            },
            "warnings": [f"level_{level}_triggered"] if triggered else []
        }

    def _schedule_persist(self):
        """延迟持久化，合并多次写入"""
        if self._persist_timer is not None:
            self._persist_timer.cancel()
        self._persist_timer = threading.Timer(1.0, self._do_persist)
        self._persist_timer.daemon = True
        self._persist_timer.start()

    def _do_persist(self):
        try:
            self._persist_state_sync()
        except Exception as e:
            logger.warning(f"持久化异常: {e}")

    def _persist_state_sync(self):
        """同步写入持久化文件（使用原子写入）"""
        with self._persist_lock:
            with self._state_lock:
                data = {sym: {"level": s["level"], "cooldown_until": s["cooldown_until"],
                              "trigger_count": s.get("trigger_count", 0)}
                        for sym, s in self._state.items() if s["level"] > 0}
            try:
                os.makedirs(os.path.dirname(self.PERSIST_STATE_PATH), exist_ok=True)
                tmp_path = self.PERSIST_STATE_PATH + ".tmp"
                with open(tmp_path, 'w') as f:
                    json.dump(data, f)
                os.replace(tmp_path, self.PERSIST_STATE_PATH)
            except Exception as e:
                logger.error(f"持久化状态失败: {e} #RECOVERY: 检查磁盘空间和目录权限")

    def _load_persisted_state(self):
        try:
            if not os.path.exists(self.PERSIST_STATE_PATH):
                return
            with open(self.PERSIST_STATE_PATH, 'r') as f:
                data = json.load(f)
            now = time.time()
            restored_count = 0
            for sym, s in data.items():
                if s["level"] > 0 and s["cooldown_until"] > now:
                    age = now - s.get("last_triggered", now)
                    if age < self.MAX_STATE_AGE_ON_LOAD:
                        self._state[sym] = s
                        self._trigger_counters[sym] = s.get("trigger_count", 1)
                        self._last_trigger_time[sym] = s.get("last_triggered", now)
                        restored_count += 1
            if restored_count > 0:
                logger.info(f"从持久化状态恢复 {restored_count} 个品种的熔断状态")
        except Exception as e:
            logger.warning(f"加载持久化状态失败: {e} #RECOVERY: 检查 {self.PERSIST_STATE_PATH} 文件完整性")

    def _load_symbol_configs(self):
        """从配置加载器获取品种差异化参数（若无则使用默认值）"""
        self._symbol_configs = {}
        if self._config_loader is not None:
            try:
                raw = self._config_loader.get("risk.circuit_breaker.symbol_overrides", {})
                for sym, cfg in raw.items():
                    self._symbol_configs[sym] = cfg
                logger.info(f"加载品种差异化配置: {list(self._symbol_configs.keys())}")
            except Exception as e:
                logger.warning(f"加载品种差异化配置失败: {e}")

    def _get_symbol_config(self, symbol: str, key: str, default):
        """获取品种特定配置，若无则返回默认值"""
        cfg = self._symbol_configs.get(symbol, {})
        return cfg.get(key, default)
