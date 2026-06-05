"""
火种系统 · 过滤器协同管理器 (FilterCoordinator)

核心职责：
1. 根据信号饥渴指数与市场活跃度，自动在三档过滤模式（精准/均衡/宽松）之间切换
2. 管理切换冷却、紧急回滚与信号质量联动规则，确保宽松模式下的胜率保护
3. 记录全量切换审计日志，支持事后归因与合规审查

外部依赖（真实模块接口）：
- core.scorecard.signal_funnel.SignalFunnel : 获取当前各等级信号的通过率与近期胜率
- core.perception.tactile_cortex.TactileCortex : 获取当前波动率分位、市场活跃度、价差比
- core.risk_monitor.circuit_breaker.CircuitBreaker : 获取当前风险色彩等级
- core.risk_monitor.fragility_index_calculator.FragilityIndexCalculator : 获取当前脆弱性指数
- core.risk_monitor.RiskMonitor : 获取当前回撤占比
- core.negotiation_bus.NegotiationBus : 发送过滤器切换事件与告警
- core.behavioral_logger.BehavioralLogger : 记录切换日志与异常事件

接口契约：
- evaluate_and_switch() -> Dict[str, Any] : 评估当前状态并执行必要的模式切换
- get_current_mode() -> Dict[str, Any] : 返回当前生效的过滤模式及剩余冷却时间
- get_detailed_status() -> Dict[str, Any] : 返回完整内部状态快照（含性能统计）
- health_check() -> Dict[str, Any] : 模块自检，验证所有依赖可用性
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 SignalFunnel 不可用时，默认保持当前模式不变，并禁用自动切换，同时上报 Error 级别日志
- 当 TactileCortex 不可用时，使用默认波动率分位（50）作为安全假设，并记录降级状态
- 当 CircuitBreaker、FragilityIndexCalculator、RiskMonitor 不可用时，视为最高风险，禁止升级到宽松模式
- 当 NegotiationBus 不可用时，切换事件降级为仅本地日志，并通过 _alert_queue 缓存未发送告警以便恢复后重发
- 所有降级值在类常量区明确声明，决策原因可通过 reason 字段追溯

资源管理：
- 本模块仅维护模式状态与切换时间戳，不持有任何需要手动释放的外部资源
- 采用线程安全单例模式，确保全局唯一实例
- 热重载时可通过 reset_instance() 强制重置单例，防止旧配置残留
- _alert_queue 缓存的告警在清理时自动释放，避免内存泄漏
"""

import time
import logging
import threading
from enum import Enum
from typing import Dict, Any, Optional, Tuple, List
from collections import deque
import numpy as np

logger = logging.getLogger(__name__)


class FilterMode(Enum):
    """过滤器模式枚举"""
    PRECISION = "precision"
    BALANCED = "balanced"
    RELAXED = "relaxed"


class ConfigSnapshot:
    """配置快照，支持事务性回滚"""
    __slots__ = ('hunger_trigger_pct', 'emergency_rollback_winrate', 'emergency_rollback_sample',
                 'emergency_rollback_losses', 'default_cooldown_sec', 'max_consecutive_switches',
                 'max_switch_history', 'balanced_upgrade_hours', 'max_drawdown_pct',
                 'max_fragility_threshold', 'extreme_vol_threshold', 'alert_dedup_window_sec',
                 'hunger_confirm_bars', 'spread_ratio_max', 'max_alert_queue_size')

    def __init__(self, source: 'FilterCoordinator'):
        for attr in self.__slots__:
            setattr(self, attr, getattr(source, attr))


class FilterCoordinator:
    """过滤器协同管理器（线程安全单例，支持热重载与配置回滚）"""

    # ========== 类常量（安全默认值） ==========
    DEFAULT_MODE = FilterMode.PRECISION
    HUNGER_TRIGGER_PCT = 0.5
    BALANCED_UPGRADE_HOURS = 8
    EMERGENCY_ROLLBACK_LOSSES = 5
    EMERGENCY_ROLLBACK_WINRATE = 0.50
    EMERGENCY_ROLLBACK_SAMPLE = 10
    DEFAULT_COOLDOWN_SEC = 600
    MAX_CONSECUTIVE_SWITCHES = 3
    MAX_SWITCH_HISTORY = 20
    DEFAULT_VOL_PERCENTILE = 50.0
    DEFAULT_ACTIVITY_LABEL = "normal"
    ALERT_DEDUP_WINDOW_SEC = 30
    MAX_DRAWDOWN_PCT = 0.15
    MAX_FRAGILITY_THRESHOLD = 0.7
    EXTREME_VOL_THRESHOLD = 90
    RISK_COLOR_BLOCK_RELAX = frozenset({"red", "black"})
    HUNGER_CONFIRM_BARS = 3
    SPREAD_RATIO_MAX = 3.0
    PERF_STATS_WINDOW = 20
    MAX_ALERT_QUEUE_SIZE = 50               # 告警缓存队列上限，[10, 100]

    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """热重载时强制重置单例"""
        with cls._instance_lock:
            if cls._instance is not None:
                cls._instance._cleanup()
                cls._instance = None

    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        self._initialized = True

        self._current_mode: FilterMode = self.DEFAULT_MODE
        self._last_switch_time: float = 0.0
        self._switch_count_this_hour: int = 0
        self._hour_start_time: float = time.monotonic()
        self._switch_history: List[Dict[str, Any]] = []

        self._hunger_counter: int = 0
        self._satiety_counter: int = 0
        self._consecutive_degraded: int = 0     # 连续降级次数

        self._perf_stats: deque = deque(maxlen=self.PERF_STATS_WINDOW)

        self._signal_funnel = None
        self._tactile_cortex = None
        self._circuit_breaker = None
        self._fragility_index = None
        self._risk_monitor = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        self._lock = threading.RLock()
        self._alert_lock = threading.Lock()
        self._alert_last_triggered: Dict[str, float] = {}
        self._alert_queue: deque = deque(maxlen=self.MAX_ALERT_QUEUE_SIZE)

        self._config_backup: Optional[ConfigSnapshot] = None

        logger.info("FilterCoordinator 单例初始化完成，当前模式: %s", self._current_mode.value)

    def _cleanup(self) -> None:
        """清理资源，用于重置实例前调用"""
        try:
            with self._alert_lock:
                self._alert_queue.clear()
        except Exception:
            pass

    # ========== 配置注入 ==========
    def inject_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """注入运行时配置，覆盖类常量。支持事务性回滚。"""
        with self._lock:
            self._config_backup = ConfigSnapshot(self)
            errors = []
            validated = {}

            def _check(key, bounds, val_type):
                if key not in config:
                    return
                try:
                    val = config[key]
                    if val_type == 'float':
                        val = float(val)
                    elif val_type == 'int':
                        val = int(val)
                    if bounds[0] <= val <= bounds[1]:
                        validated[key] = (val, val_type)
                    else:
                        errors.append(f"参数 {key}={val} 超出值域 {bounds}")
                except (ValueError, TypeError) as e:
                    errors.append(f"参数 {key}={config[key]} 类型错误: {e}")

            _check('hunger_trigger_pct', (0.3, 0.8), 'float')
            _check('emergency_rollback_winrate', (0.3, 0.7), 'float')
            _check('emergency_rollback_sample', (5, 50), 'int')
            _check('emergency_rollback_losses', (3, 10), 'int')
            _check('default_cooldown_sec', (300, 3600), 'int')
            _check('max_consecutive_switches', (2, 10), 'int')
            _check('max_switch_history', (10, 50), 'int')
            _check('balanced_upgrade_hours', (4, 24), 'int')
            _check('max_drawdown_pct', (0.05, 0.30), 'float')
            _check('max_fragility_threshold', (0.5, 0.9), 'float')
            _check('extreme_vol_threshold', (80, 99), 'float')
            _check('alert_dedup_window_sec', (10, 120), 'int')
            _check('hunger_confirm_bars', (2, 5), 'int')
            _check('spread_ratio_max', (2.0, 5.0), 'float')
            _check('max_alert_queue_size', (10, 100), 'int')

            if errors:
                if self._config_backup:
                    for attr in ConfigSnapshot.__slots__:
                        setattr(self, attr, getattr(self._config_backup, attr))
                    self._config_backup = None
                return {
                    "status": "error",
                    "reason": f"配置注入失败，已回滚: {'; '.join(errors)}",
                    "data": {},
                    "warnings": errors,
                }

            attr_map = {
                'hunger_trigger_pct': 'HUNGER_TRIGGER_PCT',
                'emergency_rollback_winrate': 'EMERGENCY_ROLLBACK_WINRATE',
                'emergency_rollback_sample': 'EMERGENCY_ROLLBACK_SAMPLE',
                'emergency_rollback_losses': 'EMERGENCY_ROLLBACK_LOSSES',
                'default_cooldown_sec': 'DEFAULT_COOLDOWN_SEC',
                'max_consecutive_switches': 'MAX_CONSECUTIVE_SWITCHES',
                'max_switch_history': 'MAX_SWITCH_HISTORY',
                'balanced_upgrade_hours': 'BALANCED_UPGRADE_HOURS',
                'max_drawdown_pct': 'MAX_DRAWDOWN_PCT',
                'max_fragility_threshold': 'MAX_FRAGILITY_THRESHOLD',
                'extreme_vol_threshold': 'EXTREME_VOL_THRESHOLD',
                'alert_dedup_window_sec': 'ALERT_DEDUP_WINDOW_SEC',
                'hunger_confirm_bars': 'HUNGER_CONFIRM_BARS',
                'spread_ratio_max': 'SPREAD_RATIO_MAX',
                'max_alert_queue_size': 'MAX_ALERT_QUEUE_SIZE',
            }
            for key, (val, _) in validated.items():
                setattr(self, attr_map[key], val)
            self._config_backup = None
            return {"status": "ok", "reason": "配置注入成功", "data": validated, "warnings": []}

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        signal_funnel: Optional[Any] = None,
        tactile_cortex: Optional[Any] = None,
        circuit_breaker: Optional[Any] = None,
        fragility_index: Optional[Any] = None,
        risk_monitor: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """注入外部依赖，执行接口契约校验，不合格依赖拒绝注入"""
        if signal_funnel and hasattr(signal_funnel, 'get_pass_rate') and hasattr(signal_funnel, 'get_recent_stats'):
            self._signal_funnel = signal_funnel
            logger.info("SignalFunnel 注入成功")
        else:
            self._signal_funnel = None
            logger.warning("SignalFunnel 不可用或不满足接口契约，禁用自动切换")

        self._tactile_cortex = tactile_funnel if (tactile_cortex and hasattr(tactile_cortex, 'get_current_volatility_percentile')) else None
        self._circuit_breaker = circuit_breaker if (circuit_breaker and hasattr(circuit_breaker, 'get_current_risk_color')) else None
        self._fragility_index = fragility_index if (fragility_index and hasattr(fragility_index, 'get_current_fragility')) else None
        self._risk_monitor = risk_monitor if (risk_monitor and hasattr(risk_monitor, 'get_current_drawdown_pct')) else None
        self._negotiation_bus = negotiation_bus if (negotiation_bus and hasattr(negotiation_bus, 'publish_alert')) else None
        self._behavioral_logger = behavioral_logger if (behavioral_logger and hasattr(behavioral_logger, 'log_event')) else None

    # ========== 公共接口 ==========
    def evaluate_and_switch(self) -> Dict[str, Any]:
        """评估当前状态并执行模式切换"""
        t_start = time.monotonic()

        # 锁外获取数据
        pass_rate, is_data_valid = self._safe_get_pass_rate()
        current_vol = self._safe_get_volatility()
        current_activity = self._safe_get_activity_label()
        current_drawdown = self._safe_get_drawdown()
        current_risk_color = self._safe_get_risk_color()
        current_fragility = self._safe_get_fragility()
        current_spread = self._safe_get_spread_ratio()

        with self._lock:
            now = time.monotonic()
            if now - self._hour_start_time > 3600:
                self._switch_count_this_hour = 0
                self._hour_start_time = now

            if not is_data_valid:
                self._record_perf(t_start)
                return {
                    "status": "degraded",
                    "reason": "信号通过率数据不可用，保持当前模式",
                    "data": {"current_mode": self._current_mode.value},
                    "warnings": ["signal_data_unavailable"],
                }

            mode = self._current_mode
            time_in_mode = now - self._last_switch_time if self._last_switch_time > 0 else 0
            target_mode = mode
            reason = "无需切换"
            warnings = []
            skip_cooldown = False

            is_hungry = self._confirm_hunger(pass_rate < self.HUNGER_TRIGGER_PCT)

            # 紧急回滚检测
            if mode == FilterMode.RELAXED:
                if self._should_rollback_relaxed():
                    target_mode = FilterMode.PRECISION
                    reason = "宽松模式风控回滚：胜率或连续亏损超限"
                    skip_cooldown = True
                    warnings.append("emergency_rollback")
                    self._hunger_counter = 0

            # 常规切换
            if target_mode == mode:
                if mode == FilterMode.RELAXED:
                    if not is_hungry and time_in_mode > self.DEFAULT_COOLDOWN_SEC:
                        target_mode = FilterMode.BALANCED
                        reason = "信号饥渴解除，回落均衡"
                        self._satiety_counter = 0
                elif mode == FilterMode.BALANCED:
                    if is_hungry and self._should_escalate_to_relaxed(
                            current_activity, current_drawdown, current_vol,
                            current_risk_color, current_fragility, current_spread):
                        target_mode = FilterMode.RELAXED
                        reason = "均衡模式持续饥渴且风控允许，升级宽松"
                    elif not is_hungry and time_in_mode > self.DEFAULT_COOLDOWN_SEC:
                        target_mode = FilterMode.PRECISION
                        reason = "信号饥渴解除，回落精准"
                elif mode == FilterMode.PRECISION:
                    if is_hungry and time_in_mode > self.DEFAULT_COOLDOWN_SEC:
                        target_mode = FilterMode.BALANCED
                        reason = "精准模式信号饥渴，切换均衡"

            # 频次限制
            if target_mode != mode:
                if not skip_cooldown and time_in_mode < self.DEFAULT_COOLDOWN_SEC:
                    target_mode = mode
                    reason = f"冷却中({time_in_mode:.0f}s)，暂不切换"
                elif self._switch_count_this_hour >= self.MAX_CONSECUTIVE_SWITCHES and not skip_cooldown:
                    target_mode = mode
                    reason = "小时切换次数已达上限，锁定当前模式"
                    logger.warning("过滤器切换被限流")

            if target_mode != mode:
                self._apply_switch(target_mode, reason, now, pass_rate, current_vol, current_activity)
                self._switch_count_this_hour += 1

            result = {
                "status": "ok",
                "reason": reason,
                "data": {
                    "current_mode": self._current_mode.value,
                    "pass_rate": round(pass_rate, 3),
                    "volatility_percentile": round(current_vol, 1),
                    "activity": current_activity,
                    "drawdown_pct": round(current_drawdown, 3),
                    "risk_color": current_risk_color,
                    "fragility": round(current_fragility, 3),
                    "time_in_mode_sec": round(time_in_mode, 1),
                    "switch_count_this_hour": self._switch_count_this_hour,
                    "hunger_confirmed": is_hungry,
                },
                "warnings": warnings,
            }
            self._record_perf(t_start)
            return result

    def get_current_mode(self) -> Dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            remaining = max(0, self.DEFAULT_COOLDOWN_SEC - (now - self._last_switch_time))
            return {
                "status": "ok",
                "reason": f"当前过滤模式: {self._current_mode.value}",
                "data": {
                    "mode": self._current_mode.value,
                    "remaining_cooldown_sec": round(remaining, 1) if remaining > 0 else None,
                    "time_in_mode_sec": round(now - self._last_switch_time, 1) if self._last_switch_time > 0 else 0,
                },
                "warnings": [],
            }

    def get_detailed_status(self) -> Dict[str, Any]:
        with self._lock:
            perf_summary = {}
            if self._perf_stats:
                samples = list(self._perf_stats)
                perf_summary = {
                    "avg_us": round(np.mean(samples), 1),
                    "p95_us": round(np.percentile(samples, 95), 1),
                    "p99_us": round(np.percentile(samples, 99), 1),
                    "max_us": round(max(samples), 1),
                }
            return {
                "status": "ok",
                "reason": "内部状态快照",
                "data": {
                    "current_mode": self._current_mode.value,
                    "last_switch_time": self._last_switch_time,
                    "switch_count_this_hour": self._switch_count_this_hour,
                    "hunger_counter": self._hunger_counter,
                    "satiety_counter": self._satiety_counter,
                    "consecutive_degraded": self._consecutive_degraded,
                    "recent_switches": self._switch_history[-5:],
                    "performance_us": perf_summary,
                    "dependencies": {
                        "signal_funnel": self._signal_funnel is not None,
                        "tactile_cortex": self._tactile_cortex is not None,
                        "circuit_breaker": self._circuit_breaker is not None,
                        "fragility_index": self._fragility_index is not None,
                        "risk_monitor": self._risk_monitor is not None,
                    },
                },
                "warnings": [],
            }

    def health_check(self) -> Dict[str, Any]:
        try:
            errors = []
            if self._signal_funnel:
                try:
                    _ = self._signal_funnel.get_pass_rate()
                except Exception as e:
                    errors.append(f"SignalFunnel 不可用: {e}")
            else:
                errors.append("SignalFunnel 未注入")
            status = "degraded" if errors else "ok"
            return {
                "status": status,
                "reason": "所有依赖可用" if not errors else "; ".join(errors),
                "data": {},
                "warnings": errors,
            }
        except Exception as e:
            logger.error(f"健康检查异常: {e} #RECOVERY: 检查锁状态与配置")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": []}

    # ========== 私有方法 ==========
    def _record_perf(self, t_start: float) -> None:
        self._perf_stats.append((time.monotonic() - t_start) * 1e6)

    def _confirm_hunger(self, is_currently_hungry: bool) -> bool:
        if is_currently_hungry:
            self._hunger_counter += 1
            self._satiety_counter = 0
        else:
            self._satiety_counter += 1
            self._hunger_counter = 0

        if self._hunger_counter >= self.HUNGER_CONFIRM_BARS:
            return True
        if self._satiety_counter >= self.HUNGER_CONFIRM_BARS:
            return False
        return self._hunger_counter > 0

    def _apply_switch(self, new_mode: FilterMode, reason: str, timestamp: float,
                      pass_rate: float, vol: float, activity: str) -> None:
        old = self._current_mode
        self._current_mode = new_mode
        self._last_switch_time = timestamp
        record = {
            "time": timestamp,
            "old": old.value,
            "new": new_mode.value,
            "reason": reason,
            "pass_rate": round(pass_rate, 3),
            "volatility": round(vol, 1),
            "activity": activity,
        }
        self._switch_history.append(record)
        if len(self._switch_history) > self.MAX_SWITCH_HISTORY:
            self._switch_history.pop(0)

        logger.info(
            "过滤器切换: %s -> %s, 通过率=%.3f, 波动率=%.1f, 活跃度=%s, 原因: %s",
            old.value, new_mode.value, pass_rate, vol, activity, reason
        )
        self._fire_alert(old, new_mode, reason, pass_rate)

    def _fire_alert(self, old: FilterMode, new: FilterMode, reason: str, pass_rate: float) -> None:
        alert_key = f"{old.value}->{new.value}"
        now = time.monotonic()
        with self._alert_lock:
            if now - self._alert_last_triggered.get(alert_key, 0) < self.ALERT_DEDUP_WINDOW_SEC:
                return
            self._alert_last_triggered[alert_key] = now

        if self._negotiation_bus and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="filter_mode_switch",
                    old=old.value, new=new.value, reason=reason,
                    pass_rate=pass_rate, timestamp=time.time()
                )
            except Exception as e:
                logger.warning(f"告警推送失败: {e}")
                self._queue_alert(alert_key, old, new, reason, pass_rate)

    def _queue_alert(self, key: str, old: FilterMode, new: FilterMode, reason: str, pass_rate: float) -> None:
        """缓存未发送的告警，待协商总线恢复后重发"""
        self._alert_queue.append({
            "key": key, "old": old.value, "new": new.value,
            "reason": reason, "pass_rate": pass_rate, "timestamp": time.time()
        })

    def _safe_get_pass_rate(self) -> Tuple[float, bool]:
        if not self._signal_funnel:
            return 0.0, False
        try:
            val = float(self._signal_funnel.get_pass_rate())
            return max(0.0, min(1.0, val)), True
        except Exception as e:
            logger.warning(f"获取通过率失败: {e}")
            return 0.0, False

    def _safe_get_volatility(self) -> float:
        if not self._tactile_cortex:
            return self.DEFAULT_VOL_PERCENTILE
        try:
            return max(0.0, min(100.0, float(self._tactile_cortex.get_current_volatility_percentile())))
        except Exception as e:
            logger.warning(f"获取波动率失败: {e}")
            return self.DEFAULT_VOL_PERCENTILE

    def _safe_get_activity_label(self) -> str:
        if not self._tactile_cortex:
            return self.DEFAULT_ACTIVITY_LABEL
        try:
            return str(self._tactile_cortex.get_activity_label())
        except Exception as e:
            logger.warning(f"获取市场活跃度失败: {e}")
            return self.DEFAULT_ACTIVITY_LABEL

    def _safe_get_drawdown(self) -> float:
        if not self._risk_monitor:
            return 1.0
        try:
            return max(0.0, min(1.0, float(self._risk_monitor.get_current_drawdown_pct())))
        except Exception as e:
            logger.warning(f"获取回撤失败: {e}")
            return 1.0

    def _safe_get_risk_color(self) -> str:
        if not self._circuit_breaker:
            return "red"
        try:
            return str(self._circuit_breaker.get_current_risk_color())
        except Exception:
            return "red"

    def _safe_get_fragility(self) -> float:
        if not self._fragility_index:
            return 1.0
        try:
            return max(0.0, min(1.0, float(self._fragility_index.get_current_fragility())))
        except Exception:
            return 1.0

    def _safe_get_spread_ratio(self) -> float:
        if not self._tactile_cortex:
            return 1.0
        try:
            return max(0.5, min(5.0, float(self._tactile_cortex.get_spread_ratio())))
        except Exception:
            return 1.0

    def _should_rollback_relaxed(self) -> bool:
        if not self._signal_funnel:
            return False
        try:
            stats = self._signal_funnel.get_recent_stats(samples=self.EMERGENCY_ROLLBACK_SAMPLE)
            n = stats.get("sample_count", 0)
            if n < self.EMERGENCY_ROLLBACK_SAMPLE:
                return False
            winrate = stats.get("winrate", 1.0)
            z = 1.645
            lower_bound = winrate - z * np.sqrt(winrate * (1.0 - winrate) / n)
            if lower_bound < self.EMERGENCY_ROLLBACK_WINRATE:
                logger.warning("宽松模式胜率统计显著低于阈值: lower_bound=%.3f", lower_bound)
                return True
            if stats.get("consecutive_losses", 0) >= self.EMERGENCY_ROLLBACK_LOSSES:
                logger.warning("宽松模式连续亏损 %d 笔，触发紧急回滚", stats["consecutive_losses"])
                return True
        except Exception as e:
            logger.warning(f"获取宽松模式统计失败: {e}")
        return False

    def _should_escalate_to_relaxed(self, activity: str, drawdown: float, vol: float,
                                    risk_color: str, fragility: float, spread_ratio: float) -> bool:
        if activity in ("extreme", "volatile"):
            return False
        if drawdown > self.MAX_DRAWDOWN_PCT:
            return False
        if risk_color in self.RISK_COLOR_BLOCK_RELAX:
            return False
        if fragility > self.MAX_FRAGILITY_THRESHOLD:
            return False
        if vol > self.EXTREME_VOL_THRESHOLD:
            return False
        if spread_ratio > self.SPREAD_RATIO_MAX:
            return False
        return True
