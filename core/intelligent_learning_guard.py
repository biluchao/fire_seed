"""
火种系统 · 智能学习守卫 (IntelligentLearningGuard)

核心职责：
1. 根据市场活跃度动态决定是否进入/退出学习模式，管理策略在低波动期平稳运行与高波动期快速唤醒之间的切换
2. 在学习时段内实时监控波动率、成交量、价格跳空、盘口深度、资金费率等异常指标，触发惊梦唤醒，并自动调整夜间风控参数

外部依赖（真实模块接口）：
- core.state_machine.StateMachine : 获取当前市场状态（趋势/震荡/反转）及波动率分位
- core.perception.tactile_cortex.TactileCortex : 获取实时波动率、成交量等市场活跃度指标
- core.perception.jump_detector.JumpDetector : 提供价格跳空ATR比值（已声明但为预留接口）
- core.risk_monitor.risk_color_manager.RiskColorManager : 获取当前风险色彩等级，用于风控参数联动
- core.behavioral_logger.BehavioralLogger : 记录学习时段切换与惊梦事件日志
- core.negotiation_bus.NegotiationBus : 发送健康状态变更事件与告警通知

接口契约：
- should_enter_learning(market_state: Dict[str, Any]) -> Dict[str, Any] : 判断当前是否应进入学习模式
- check_nightmare(market_state: Dict[str, Any]) -> Dict[str, Any] : 检测是否需要惊梦唤醒
- apply_night_mode_risk(base_params: Optional[Dict[str, Any]], strategy_type: str = "default") -> Dict[str, Any] : 返回学习时段的收紧风控参数
- get_learning_status() -> Dict[str, Any] : 返回当前学习守卫的状态
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 TactileCortex 不可用时，使用保守的默认波动率值，自动触发唤醒以确保安全
- 当 StateMachine 不可用时，默认认为当前不处于学习时段，策略正常运行
- 当 RiskColorManager 不可用时，采用内置的保守风控参数
- 当 NegotiationBus 不可用时，告警降级为仅本地日志记录
- 所有降级值在类常量区明确声明

资源管理：
- 本模块无状态持久化需求，所有中间计算结果在方法返回后自动回收
- 持有线程池资源，在模块销毁时通过 atexit 自动释放
"""

import time
import logging
import threading
import atexit
import statistics
from typing import Dict, Any, List, Optional, Tuple, Sequence
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class IntelligentLearningGuard:
    """智能学习守卫：管理学习时段与惊梦唤醒"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 学习时段判定阈值
    DEFAULT_LOW_ACTIVITY_VOL_RATIO = 0.7
    DEFAULT_LOW_ACTIVITY_VOLUME_RATIO = 0.7
    DEFAULT_LEARNING_COOLDOWN_MINUTES = 30
    MIN_SAMPLES_FOR_DECISION = 20
    DEFAULT_LEARNING_HOURS_UTC = (17, 20)  # UTC 17-20点 = 北京时间凌晨1-4点

    # 惊梦唤醒阈值
    DEFAULT_NIGHTMARE_VOL_RATIO = 2.0
    DEFAULT_NIGHTMARE_VOLUME_DROP_RATIO = 0.3
    DEFAULT_NIGHTMARE_VOLUME_DROP_DURATION_MIN = 5
    DEFAULT_NIGHTMARE_PRICE_JUMP_ATR = 3.0
    DEFAULT_NIGHTMARE_CONFIRM_MINUTES = 5
    DEFAULT_NIGHTMARE_MAX_WAIT_MINUTES = 10
    DEFAULT_NIGHTMARE_DEPTH_DROP_RATIO = 0.3
    DEFAULT_NIGHTMARE_MULTI_TRIGGER_CONFIRM_MIN = 1
    DEFAULT_NIGHTMARE_FUNDING_RATE_THRESHOLD = 0.002  # 0.2%

    # 即时熔断阈值（不经确认窗口，立即唤醒）
    DEFAULT_INSTANT_WAKE_PRICE_JUMP_ATR = 4.0
    DEFAULT_INSTANT_WAKE_VOL_RATIO = 3.0
    INSTANT_WAKE_DEPTH_ZERO_THRESHOLD = 1e-12  # 盘口深度视为零的阈值

    # 学习时段风控收紧参数
    DEFAULT_NIGHT_STOP_ATR_MULT = 0.6
    DEFAULT_NIGHT_STOP_ATR_MULT_MIN = 0.2
    MAX_ATR_MULT = 10.0
    DEFAULT_NIGHT_POSITION_MAX_PCT = 0.5
    DEFAULT_NIGHT_FORCE_DEPTH_CHECK_INTERVAL = 3600
    DEFAULT_HISTORY_MAX_LEN = 1440

    # 成交量恢复确认（脉冲过滤）
    VOLUME_RECOVERY_CONFIRM_SECONDS = 60
    VOLUME_PULSE_FILTER_SECONDS = 10  # 持续时间小于此值的回升视为脉冲

    # 策略系数（未知策略类型默认保守）
    DEFAULT_STRATEGY_COEFF = 0.5

    def __init__(
        self,
        history_max_len: Optional[int] = None,
        learning_hours_utc: Optional[Tuple[int, int]] = None,
    ):
        # 历史数据滑动窗口（Python整数无溢出，版本号无ABA风险）
        window_size = history_max_len or self.DEFAULT_HISTORY_MAX_LEN
        self._volatility_history: deque = deque(maxlen=window_size)
        self._volume_history: deque = deque(maxlen=window_size)

        # 历史数据版本号与快照
        self._history_version = 0
        self._volatility_snapshot: Optional[Tuple[float, ...]] = None
        self._volume_snapshot: Optional[Tuple[float, ...]] = None
        self._snapshot_version = -1

        # 学习时段配置
        self._learning_hours_utc = learning_hours_utc or self.DEFAULT_LEARNING_HOURS_UTC

        # 当前学习状态
        self._learning_active = False
        self._learning_start_time: Optional[float] = None
        self._last_wake_time: Optional[float] = None
        self._last_exit_learning_time: Optional[float] = None

        # 累计统计
        self._total_learning_seconds_today = 0.0
        self._wake_count_today = 0
        self._last_stats_reset_day: Optional[str] = None

        # 惊梦触发计时器（统一字典管理）
        self._nightmare_triggers: Dict[str, Optional[float]] = {
            "vol": None,
            "volume": None,
            "price": None,
            "depth": None,
            "funding": None,
        }

        # 成交量骤降计时器
        self._volume_drop_start_time: Optional[float] = None
        self._volume_recovery_start_time: Optional[float] = None
        self._volume_recovery_pulse_start: Optional[float] = None

        # 唤醒快照记录
        self._last_wake_snapshot: Optional[Dict[str, Any]] = None

        # 唤醒原因日志（用于模式分析）
        self._wake_reasons_log: deque = deque(maxlen=100)

        # 外部依赖注入
        self._state_machine = None
        self._tactile_cortex = None
        self._risk_color_manager = None
        self._behavioral_logger = None
        self._negotiation_bus = None

        # 线程安全
        self._lock = threading.RLock()
        self._stats_lock = threading.Lock()
        self._wake_lock = threading.Lock()

        # 线程池（用于异步通知）
        self._notification_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="learning_guard_notify")
        atexit.register(self._shutdown)

        logger.info(
            "IntelligentLearningGuard 初始化完成 (窗口=%d, 学习时段=UTC %d:00-%d:00)",
            window_size, self._learning_hours_utc[0], self._learning_hours_utc[1]
        )

    def _shutdown(self) -> None:
        """模块销毁时的清理"""
        try:
            self._notification_pool.shutdown(wait=False)
            logger.debug("IntelligentLearningGuard 线程池已关闭")
        except Exception:
            pass

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        state_machine: Optional[Any] = None,
        tactile_cortex: Optional[Any] = None,
        risk_color_manager: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选注入，未注入时对应功能降级）"""
        if state_machine is not None:
            self._state_machine = state_machine
            logger.info("StateMachine 注入成功")
        else:
            logger.warning("StateMachine 未注入，学习时段判定降级为简单时段判断")

        if tactile_cortex is not None:
            if not hasattr(tactile_cortex, 'get_volatility'):
                logger.warning("TactileCortex 缺少 get_volatility 方法，标记为不可用")
                self._tactile_cortex = None
            else:
                try:
                    test_vol = tactile_cortex.get_volatility()
                    if not isinstance(test_vol, (int, float)):
                        logger.warning("TactileCortex.get_volatility 返回非数值类型")
                        self._tactile_cortex = None
                    else:
                        self._tactile_cortex = tactile_cortex
                        logger.info("TactileCortex 注入成功 (dry-run: vol=%.6f)", test_vol)
                except Exception as e:
                    logger.warning(f"TactileCortex dry-run 失败: {e}")
                    self._tactile_cortex = None
        else:
            logger.warning("TactileCortex 未注入，波动率监控降级为保守模式")

        if risk_color_manager is not None:
            self._risk_color_manager = risk_color_manager
            logger.info("RiskColorManager 注入成功")
        else:
            logger.warning("RiskColorManager 未注入，夜间风控采用内置保守参数")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，事件日志降级为标准 logger")

        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, 'publish_alert'):
                logger.warning("NegotiationBus 缺少 publish_alert 方法")
                self._negotiation_bus = None
            else:
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")
        else:
            logger.warning("NegotiationBus 未注入，告警广播降级为本地日志")

    # ========== 公共接口 ==========
    def should_enter_learning(self, market_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        判断当前是否应进入学习模式
        """
        tactile = self._tactile_cortex

        try:
            volatility = market_state.get("volatility", 0.0)
            volume = market_state.get("volume", 0.0)

            with self._lock:
                self._volatility_history.append(volatility)
                self._volume_history.append(volume)
                self._history_version += 1

            vol_snapshot = self._get_volatility_snapshot()
            vol_len = len(vol_snapshot)

            if vol_len < self.MIN_SAMPLES_FOR_DECISION:
                current_hour = datetime.now(timezone.utc).hour
                start_h, end_h = self._learning_hours_utc
                should_learn = start_h <= current_hour < end_h
                return {
                    "status": "ok",
                    "reason": f"历史样本不足({vol_len}<{self.MIN_SAMPLES_FOR_DECISION})，基于UTC时段判断: "
                              f"{'学习时段' if should_learn else '正常时段'}",
                    "data": {"should_learn": should_learn, "learning_active": self._learning_active},
                    "warnings": ["insufficient_data_for_learning_decision"],
                }

            hist_vol_mean = self._trimmed_mean(vol_snapshot, trim_ratio=0.1)
            hist_vol_mean = max(hist_vol_mean, 0.0001)
            if hist_vol_mean <= 0.0002:
                logger.warning("历史波动率均值异常低(%.8f)，可能数据异常", hist_vol_mean)

            vol_hist_snapshot = tuple(self._volume_history)[-vol_len:] if self._volume_history else ()
            if vol_hist_snapshot:
                hist_volume_mean = self._trimmed_mean(vol_hist_snapshot, trim_ratio=0.1)
            else:
                hist_volume_mean = volume
            hist_volume_mean = max(hist_volume_mean, 0.0001)

            low_vol = volatility < hist_vol_mean * self.DEFAULT_LOW_ACTIVITY_VOL_RATIO
            low_volume = volume < hist_volume_mean * self.DEFAULT_LOW_ACTIVITY_VOLUME_RATIO

            # 使用 monotonic 时间进行冷却期计算，防止系统时间调整影响
            now = time.monotonic()
            in_cooldown = self._is_in_cooldown(now)

            should_learn = low_vol and low_volume and not in_cooldown

            reason_parts = [
                f"波动率 {volatility:.6f} {'<' if low_vol else '≥'} "
                f"{hist_vol_mean * self.DEFAULT_LOW_ACTIVITY_VOL_RATIO:.6f}",
                f"成交量 {volume:.2f} {'<' if low_volume else '≥'} "
                f"{hist_volume_mean * self.DEFAULT_LOW_ACTIVITY_VOLUME_RATIO:.2f}",
            ]
            if in_cooldown:
                remaining = self.DEFAULT_LEARNING_COOLDOWN_MINUTES * 60 - (
                    now - self._last_exit_learning_time
                )
                reason_parts.append(f"冷却期剩余 {max(0, remaining):.0f}s")
            reason = "; ".join(reason_parts)

            # 状态切换
            state_changed = False
            with self._lock:
                if should_learn and not self._learning_active:
                    self._learning_active = True
                    self._learning_start_time = time.time()
                    state_changed = True
                elif not should_learn and self._learning_active:
                    self._learning_active = False
                    self._learning_start_time = None
                    self._last_exit_learning_time = now
                    state_changed = True

            if state_changed:
                event_type = "enter" if should_learn else "exit"
                self._log_learning_event(event_type, reason)

            return {
                "status": "ok",
                "reason": reason,
                "data": {
                    "should_learn": should_learn,
                    "learning_active": self._learning_active,
                    "learning_start_time": self._learning_start_time,
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(
                f"学习模式判定失败: {e} #RECOVERY: 检查 market_state 字段完整性，"
                f"当前字段: {list(market_state.keys()) if isinstance(market_state, dict) else 'N/A'}"
            )
            return {
                "status": "error",
                "reason": f"判定异常: {str(e)}",
                "data": {"should_learn": False, "learning_active": False},
                "warnings": [f"LEARNING_DECISION_FAILED: {str(e)}"],
            }

    def check_nightmare(self, market_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        检测是否需要惊梦唤醒（在学习时段内调用）
        """
        if not self._learning_active:
            return {
                "status": "ok",
                "reason": "当前未处于学习时段，无需惊梦检测",
                "data": {"should_wake": False, "trigger_type": "", "wake_reason": ""},
                "warnings": [],
            }

        try:
            volatility = market_state.get("volatility", 0.0)
            volume = market_state.get("volume", 0.0)
            price_jump_atr = market_state.get("price_jump_atr_ratio", 0.0)
            depth_ratio = market_state.get("depth_ratio", None)
            funding_rate = market_state.get("funding_rate", 0.0)
            hist_vol_mean = market_state.get("historical_vol_mean", volatility)
            hist_vol_mean = max(hist_vol_mean, 0.0001)
            volume_ma = market_state.get("volume_ma", volume)
            volume_ma = max(volume_ma, 0.0001)

            now = time.time()

            # ---- 即时熔断检查（不经确认窗口） ----
            if price_jump_atr > self.DEFAULT_INSTANT_WAKE_PRICE_JUMP_ATR:
                return self._execute_wake("price_jump_instant",
                    f"即时熔断: 价格跳空 {price_jump_atr:.1f}×ATR")

            if volatility > hist_vol_mean * self.DEFAULT_INSTANT_WAKE_VOL_RATIO:
                return self._execute_wake("vol_spike_instant",
                    f"即时熔断: 波动率 {volatility/hist_vol_mean:.1f}×")

            # 深度为零（或极小）的即时熔断
            if depth_ratio is not None and depth_ratio < self.INSTANT_WAKE_DEPTH_ZERO_THRESHOLD:
                return self._execute_wake("depth_zero",
                    f"即时熔断: 盘口深度为0或极近于0 (ratio={depth_ratio:.2e})")

            # 资金费率极端值即时熔断
            if abs(funding_rate) > self.DEFAULT_NIGHTMARE_FUNDING_RATE_THRESHOLD:
                return self._execute_wake("funding_rate_extreme",
                    f"即时熔断: 资金费率 {funding_rate:.4f} 超过阈值 ±{self.DEFAULT_NIGHTMARE_FUNDING_RATE_THRESHOLD}")

            # ---- 常规惊梦条件检测 ----
            vol_spike = volatility > hist_vol_mean * self.DEFAULT_NIGHTMARE_VOL_RATIO
            volume_drop = volume < volume_ma * self.DEFAULT_NIGHTMARE_VOLUME_DROP_RATIO
            price_jump = price_jump_atr > self.DEFAULT_NIGHTMARE_PRICE_JUMP_ATR
            depth_drop = (depth_ratio is not None and depth_ratio < self.DEFAULT_NIGHTMARE_DEPTH_DROP_RATIO)

            any_nightmare = vol_spike or volume_drop or price_jump or depth_drop

            # 更新惊梦触发器
            self._update_trigger("vol", vol_spike, now)
            self._update_trigger("price", price_jump, now)
            self._update_trigger("depth", depth_drop, now)

            # 成交量骤降持续计时（带脉冲过滤的恢复确认）
            if volume_drop:
                if self._volume_drop_start_time is None:
                    self._volume_drop_start_time = now
                self._volume_recovery_start_time = None
                self._volume_recovery_pulse_start = None
            else:
                if self._volume_drop_start_time is not None:
                    if self._volume_recovery_start_time is None:
                        self._volume_recovery_start_time = now
                    else:
                        recovery_elapsed = now - self._volume_recovery_start_time
                        if recovery_elapsed >= self.VOLUME_RECOVERY_CONFIRM_SECONDS:
                            self._volume_drop_start_time = None
                            self._volume_recovery_start_time = None
                            self._volume_recovery_pulse_start = None
                else:
                    self._volume_recovery_start_time = None

            # 动态确认窗口：多触发源同时激活时缩短
            active_count = sum(1 for t in self._nightmare_triggers.values() if t is not None)
            confirm_minutes = (
                self.DEFAULT_NIGHTMARE_MULTI_TRIGGER_CONFIRM_MIN
                if active_count >= 2
                else self.DEFAULT_NIGHTMARE_CONFIRM_MINUTES
            )

            # 检查各触发源是否已达确认时间
            for trigger_type, trigger_time in self._nightmare_triggers.items():
                if trigger_time is not None and (now - trigger_time) >= confirm_minutes * 60:
                    return self._execute_wake(trigger_type,
                        f"惊梦确认: {trigger_type} 持续 {now - trigger_time:.0f}s")

            # 检查成交量骤降持续时间
            if self._volume_drop_start_time is not None:
                drop_dur = now - self._volume_drop_start_time
                if drop_dur >= self.DEFAULT_NIGHTMARE_VOLUME_DROP_DURATION_MIN * 60:
                    return self._execute_wake("volume_drop_duration",
                        f"成交量骤降持续 {drop_dur:.0f}s")

            # 超时重置保护
            for trigger_type, trigger_time in list(self._nightmare_triggers.items()):
                if trigger_time is not None and (now - trigger_time) > self.DEFAULT_NIGHTMARE_MAX_WAIT_MINUTES * 60:
                    self._nightmare_triggers[trigger_type] = None
                    logger.info("惊梦确认超时重置: %s", trigger_type)

            nightmare_elapsed = max(
                ((now - t) for t in self._nightmare_triggers.values() if t is not None),
                default=0.0
            )
            active_triggers = [k for k, v in self._nightmare_triggers.items() if v is not None]

            return {
                "status": "ok",
                "reason": "无惊梦触发" if not any_nightmare else f"惊梦条件待确认 ({', '.join(active_triggers)})",
                "data": {
                    "should_wake": False,
                    "trigger_type": "",
                    "wake_reason": "",
                    "nightmare_detected": any_nightmare,
                    "active_triggers": active_triggers,
                    "nightmare_elapsed": round(nightmare_elapsed, 1),
                    "conditions": {
                        "vol_spike": vol_spike,
                        "volume_drop": volume_drop,
                        "price_jump": price_jump,
                        "depth_drop": depth_drop,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"惊梦检测异常: {e} #RECOVERY: 建议手动唤醒")
            return {
                "status": "error",
                "reason": f"惊梦检测异常: {str(e)}",
                "data": {"should_wake": True, "trigger_type": "exception", "wake_reason": f"检测异常: {str(e)}"},
                "warnings": [f"check_nightmare_failed: {str(e)}"],
            }

    def apply_night_mode_risk(
        self, base_params: Optional[Dict[str, Any]] = None, strategy_type: str = "default"
    ) -> Dict[str, Any]:
        """
        返回学习时段的收紧风控参数

        Args:
            base_params: 当前正常风控参数字典，可包含 atr_mult, max_position_pct
            strategy_type: 策略类型白名单值 (trend/oscillation/market_making/event_driven/default)

        Returns:
            标准响应字典
        """
        VALID_STRATEGY_TYPES = {"trend", "oscillation", "market_making", "event_driven", "default"}

        if base_params is None:
            base_params = {}
        if not isinstance(base_params, dict):
            logger.error(f"base_params 类型错误: {type(base_params).__name__}，使用空字典")
            base_params = {}
        if strategy_type not in VALID_STRATEGY_TYPES:
            logger.warning(f"未知策略类型: {strategy_type}，使用保守默认值")
            strategy_type = "default"

        try:
            base_atr = base_params.get("atr_mult", 1.5)
            base_max_pos = base_params.get("max_position_pct", 0.1)

            if not isinstance(base_atr, (int, float)) or base_atr <= 0:
                logger.warning(f"无效 base_atr={base_atr}，使用默认值 1.5")
                base_atr = 1.5
            if base_atr > self.MAX_ATR_MULT:
                logger.warning(f"base_atr={base_atr} 超过上限 {self.MAX_ATR_MULT}，截断处理")
                base_atr = self.MAX_ATR_MULT
            if not isinstance(base_max_pos, (int, float)) or base_max_pos <= 0:
                logger.warning(f"无效 base_max_pos={base_max_pos}，使用默认值 0.1")
                base_max_pos = 0.1

            # 策略系数映射
            strategy_coeff_map = {
                "trend": 0.9,
                "oscillation": 0.7,
                "market_making": 0.5,
                "event_driven": 0.6,
                "default": self.DEFAULT_STRATEGY_COEFF,
            }
            strategy_coeff = strategy_coeff_map.get(strategy_type, self.DEFAULT_STRATEGY_COEFF)

            if strategy_coeff == self.DEFAULT_STRATEGY_COEFF and strategy_type != "default":
                logger.warning(f"策略类型 '{strategy_type}' 使用保守系数 {strategy_coeff}")

            adjusted_atr = base_atr * self.DEFAULT_NIGHT_STOP_ATR_MULT * strategy_coeff
            adjusted_atr = max(adjusted_atr, self.DEFAULT_NIGHT_STOP_ATR_MULT_MIN)
            adjusted_pos = base_max_pos * self.DEFAULT_NIGHT_POSITION_MAX_PCT

            if self._risk_color_manager is not None and hasattr(self._risk_color_manager, 'get_current_level'):
                try:
                    risk_level = self._risk_color_manager.get_current_level()
                    if risk_level in ("orange", "red", "black"):
                        adjusted_atr *= 0.8
                        adjusted_pos *= 0.7
                        logger.info("风险色彩 %s 联动：学习时段风控进一步加强", risk_level)
                except Exception as e:
                    logger.warning(f"风险色彩查询失败: {e}")

            adjusted = {
                "atr_mult": round(adjusted_atr, 4),
                "max_position_pct": round(adjusted_pos, 4),
                "force_depth_check_interval": self.DEFAULT_NIGHT_FORCE_DEPTH_CHECK_INTERVAL,
            }

            logger.info(
                "学习时段风控收紧[strategy=%s]: atr_mult %.2f→%.2f, max_pos %.2f%%→%.2f%%",
                strategy_type, base_atr, adjusted["atr_mult"],
                base_max_pos * 100, adjusted["max_position_pct"] * 100
            )

            return {
                "status": "ok",
                "reason": "学习时段风控参数已收紧",
                "data": {"adjusted_params": adjusted},
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"夜间风控参数调整异常: {e} #RECOVERY: 使用原始参数")
            return {
                "status": "error",
                "reason": f"风控调整异常: {str(e)}，使用原始参数",
                "data": {"adjusted_params": base_params},
                "warnings": [f"apply_night_mode_risk_failed: {str(e)}"],
            }

    def get_learning_status(self) -> Dict[str, Any]:
        """返回当前学习守卫的状态"""
        with self._stats_lock:
            self._reset_daily_stats_if_needed()
            status_data = {
                "learning_active": self._learning_active,
                "learning_start_time": self._learning_start_time,
                "last_wake_time": self._last_wake_time,
                "last_exit_learning_time": self._last_exit_learning_time,
                "history_vol_samples": len(self._volatility_history),
                "history_volume_samples": len(self._volume_history),
                "total_learning_seconds_today": round(self._total_learning_seconds_today, 1),
                "wake_count_today": self._wake_count_today,
                "active_nightmare_triggers": [
                    k for k, v in self._nightmare_triggers.items() if v is not None
                ],
            }
        return {
            "status": "ok",
            "reason": "学习守卫状态查询成功",
            "data": status_data,
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            with self._lock:
                vol_snapshot = self._get_volatility_snapshot()
                vol_len = len(vol_snapshot)
                vol_anomalies = 0

                if vol_len >= 2:
                    try:
                        median_vol = statistics.median(vol_snapshot)
                        mad = statistics.median(abs(v - median_vol) for v in vol_snapshot)
                        mad = max(mad, 1e-12)
                        vol_anomalies = sum(
                            1 for v in vol_snapshot if abs(v - median_vol) > 3 * mad
                        )
                    except statistics.StatisticsError:
                        logger.debug("MAD计算失败，样本量不足")

                # 依赖状态使用三态枚举
                deps = {}
                for name, dep in [
                    ("state_machine", self._state_machine),
                    ("tactile_cortex", self._tactile_cortex),
                    ("risk_color_manager", self._risk_color_manager),
                    ("behavioral_logger", self._behavioral_logger),
                    ("negotiation_bus", self._negotiation_bus),
                ]:
                    if dep is not None:
                        deps[name] = "available"
                    elif name in ("tactile_cortex",):
                        deps[name] = "degraded"
                    else:
                        deps[name] = "unavailable"

                status_info = {
                    "learning_active": self._learning_active,
                    "history_vol_samples": vol_len,
                    "history_volume_samples": len(self._volume_history),
                    "vol_anomalies_detected": vol_anomalies,
                    "nightmare_triggers_keys_ok": set(self._nightmare_triggers.keys()) == {
                        "vol", "volume", "price", "depth", "funding"
                    },
                    "dependencies": deps,
                }
            return {
                "status": "ok",
                "reason": "IntelligentLearningGuard 正常",
                "data": status_info,
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查内部数据结构与依赖注入状态")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _get_volatility_snapshot(self) -> Tuple[float, ...]:
        """
        获取波动率快照（版本号保护，Python整数无溢出，无ABA风险）
        快照首次生成后，仅当版本号变化时才重新生成
        """
        with self._lock:
            if self._snapshot_version != self._history_version or self._volatility_snapshot is None:
                self._volatility_snapshot = tuple(self._volatility_history)
                self._snapshot_version = self._history_version
            return self._volatility_snapshot

    @staticmethod
    def _trimmed_mean(data: Sequence[float], trim_ratio: float = 0.1) -> float:
        """
        截尾均值，去除极端值影响
        sorted() 接受任何可迭代对象，返回排序后的新列表
        """
        if len(data) == 0:
            return 0.0
        if len(data) <= 2:
            return sum(data) / len(data)
        sorted_data = sorted(data)
        trim_count = int(len(sorted_data) * trim_ratio)
        if trim_count > 0 and len(sorted_data) > 2 * trim_count:
            trimmed_data = sorted_data[trim_count:-trim_count]
        else:
            trimmed_data = sorted_data
        if not trimmed_data:
            trimmed_data = sorted_data[max(0, len(sorted_data) // 2 - 1): len(sorted_data) // 2 + 1]
        return sum(trimmed_data) / len(trimmed_data) if trimmed_data else sorted_data[len(sorted_data) // 2]

    def _is_in_cooldown(self, now_monotonic: float) -> bool:
        """检查是否处于学习冷却期（使用 monotonic 时间）"""
        return (
            self._last_exit_learning_time is not None
            and (now_monotonic - self._last_exit_learning_time) < self.DEFAULT_LEARNING_COOLDOWN_MINUTES * 60
        )

    def _reset_daily_stats_if_needed(self) -> None:
        """每日重置统计（需在锁内调用）"""
        today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        if self._last_stats_reset_day is None:
            self._last_stats_reset_day = today_str
        elif self._last_stats_reset_day != today_str:
            logger.info("每日学习统计重置: 昨日学习 %.0fs, 唤醒 %d 次",
                        self._total_learning_seconds_today, self._wake_count_today)
            self._total_learning_seconds_today = 0.0
            self._wake_count_today = 0
            self._last_stats_reset_day = today_str

    def _update_trigger(self, trigger_type: str, condition: bool, now: float) -> None:
        """更新惊梦触发计时器"""
        if condition:
            if self._nightmare_triggers[trigger_type] is None:
                self._nightmare_triggers[trigger_type] = now
        else:
            self._nightmare_triggers[trigger_type] = None

    def _partial_clear_history(self, trigger_type: str) -> None:
        """根据惊梦类型选择性清空历史数据"""
        with self._lock:
            if trigger_type in ("vol_spike", "vol_spike_instant", "vol"):
                self._volatility_history.clear()
            elif trigger_type in ("volume_drop", "volume_drop_duration", "volume"):
                self._volume_history.clear()
            else:
                self._volatility_history.clear()
                self._volume_history.clear()
            self._history_version += 1
            self._volatility_snapshot = None
            self._snapshot_version = -1

    def _capture_pre_wake_snapshot(self) -> None:
        """捕获唤醒前的持仓快照"""
        try:
            if self._state_machine is not None and hasattr(self._state_machine, 'get_current_state'):
                regime = self._state_machine.get_current_state()
            else:
                regime = "unknown"
            self._last_wake_snapshot = {
                "timestamp": time.time(),
                "regime": regime,
                "learning_start_time": self._learning_start_time,
                "trigger_time": time.time(),
            }
        except Exception as e:
            logger.warning(f"唤醒快照捕获失败: {e}")

    def _execute_wake(self, trigger_type: str, reason: str) -> Dict[str, Any]:
        """执行惊梦唤醒"""
        now = time.time()
        self._capture_pre_wake_snapshot()

        with self._wake_lock:
            self._wake_count_today = self._wake_count_today + 1

        with self._lock:
            self._learning_active = False
            self._learning_start_time = None
            self._last_wake_time = now
            self._partial_clear_history(trigger_type)
            for key in self._nightmare_triggers:
                self._nightmare_triggers[key] = None
            self._volume_drop_start_time = None
            self._volume_recovery_start_time = None

        # 记录唤醒原因
        self._wake_reasons_log.append({
            "time": now,
            "trigger_type": trigger_type,
            "reason": reason,
        })

        full_reason = f"[{trigger_type}] {reason}"
        self._log_learning_event("nightmare_wake", full_reason)

        # 异步通知其他模块（使用线程池，不阻塞主流程）
        self._notification_pool.submit(self._notify_modules, trigger_type, full_reason)

        return {
            "status": "ok",
            "reason": full_reason,
            "data": {
                "should_wake": True,
                "trigger_type": trigger_type,
                "wake_reason": full_reason,
            },
            "warnings": ["nightmare_wake_triggered"],
        }

    def _notify_modules(self, trigger_type: str, reason: str) -> None:
        """向协商总线广播唤醒事件（在线程池中异步执行）"""
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="nightmare_wake",
                    lane="all",
                    level="critical",
                    message=f"惊梦唤醒: {trigger_type} - {reason}",
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning(f"协商总线唤醒通知失败: {e}")

    def _log_learning_event(self, event: str, detail: str) -> None:
        """记录学习时段相关事件"""
        msg = f"学习守卫·{event}: {detail}"
        logger.info(msg)
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="learning_guard",
                    details={"event": event, "detail": detail},
                )
            except Exception:
                logger.debug("行为日志记录失败（非关键）")
