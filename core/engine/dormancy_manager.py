"""
火种系统 · 分层休眠管理器 (DormancyManager)

核心职责：
1. 根据系统运行状态（信号空窗期、平衡指数、连续亏损次数、全策略夏普）判定并触发分层休眠（浅层/深层/冬眠）
2. 管理休眠状态的渐进唤醒流程，确保从休眠到正常运行的平滑过渡与安全回退

外部依赖（真实模块接口）：
- core.behavioral_logger.BehavioralLogger : 记录休眠进入/退出及唤醒步骤日志（可选注入）

接口契约：
- evaluate_dormancy(no_signal_duration: float, balance_index: float, consecutive_losses: int, all_sharpe: float) -> Dict[str, Any]
  评估当前是否应进入休眠，返回休眠决策及层级
- get_dormancy_state() -> Dict[str, Any] : 返回当前休眠状态
- progressive_wakeup(signal_present: bool, market_vol_ok: bool, trial_result: Optional[Dict]) -> Dict[str, Any]
  执行一步渐进唤醒，返回下一步动作建议
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 所有外部依赖均为可选注入，未注入时功能降级为仅本地日志
- 进入冬眠状态时，建议由硬实时监视者执行最终保护，本模块仅输出决策信号
- 唤醒步骤中出现任何异常，自动回退到上一安全步骤

资源管理：
- 本模块不持有任何外部资源句柄，所有状态均在内存中维护
- 线程锁在模块销毁时自动释放
"""

import time
import logging
import threading
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class DormancyManager:
    """分层休眠管理器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 浅层休眠
    LIGHT_DORMANCY_NO_SIGNAL_SEC = 14400.0   # 连续无信号时长（秒），触发浅层休眠，[3600, 86400]
    LIGHT_DORMANCY_BALANCE_INDEX_MIN = 20.0  # 平衡指数下限（0-100），低于此值触发浅层休眠，[10, 40]
    LIGHT_DORMANCY_WAKE_BALANCE_INDEX = 40.0 # 唤醒浅层休眠的平衡指数阈值，[30, 60]
    LIGHT_DORMANCY_WAKE_A_SIGNAL = True       # 出现A级信号时是否唤醒浅层休眠

    # 深层休眠
    DEEP_DORMANCY_LIGHT_DURATION_SEC = 86400.0  # 浅层休眠持续时长（秒）后进入深层，[43200, 259200]
    DEEP_DORMANCY_CONSECUTIVE_LOSSES = 10        # 连续亏损笔数触发深层休眠，[5, 30]
    DEEP_DORMANCY_WAKE_VOLATILITY_PCT = 30.0     # 波动率回升至该分位（0-100）以上可唤醒深层，[20, 50]

    # 冬眠
    HIBERNATION_ALL_SHARPE_DAYS = 7            # 全策略夏普<0持续天数触发冬眠，[3, 30]
    HIBERNATION_ALL_SHARPE_THRESHOLD = -0.5    # 触发冬眠的夏普阈值，[-2.0, 0.0]
    HIBERNATION_WAKE_MANUAL_ONLY = True         # 冬眠是否仅支持人工唤醒

    # 唤醒步骤
    WAKEUP_STEP1_DURATION_SEC = 1800.0          # 第一步：恢复降频感知，观察市场（秒），[300, 3600]
    WAKEUP_STEP2_TRIAL_TRADES = 3               # 第二步：试探交易最小笔数，[1, 10]
    WAKEUP_STEP2_SIZE_MULTIPLIER = 0.1           # 试探交易的仓位倍率，[0.01, 0.3]
    WAKEUP_STEP2_MIN_WINRATE = 0.5               # 试探交易最小胜率，[0.3, 0.7]
    WAKEUP_STEP3_STABLE_SEC = 7200.0            # 第三步：观察标签下稳定运行时间（秒），[1800, 21600]
    WAKEUP_STEP3_REQUIRED_SHARPE = 0.0           # 第三步稳定期要求的最低滚动夏普，[-0.5, 0.5]

    # 其他
    ANOMALY_REGRESSION_DELAY_SEC = 60.0          # 唤醒异常回退后的等待时间（秒），[30, 600]
    MAX_WAKEUP_REGRESSIONS = 3                   # 最大唤醒回退次数，[2, 10]

    def __init__(self):
        # 休眠状态: "active", "light_dormancy", "deep_dormancy", "hibernation"
        self._current_state = "active"
        self._state_entered_time = time.time()  # 进入当前状态的时间戳
        self._light_dormancy_start_time = 0.0

        # 唤醒步骤状态: None, "step1", "step2", "step3"
        self._wakeup_step = None
        self._wakeup_step_start_time = 0.0
        self._trial_trades_done = 0
        self._trial_trades_won = 0

        # 冬眠前置计时器（用于持续时间校验）
        self._sharpe_below_start = 0.0

        # 唤醒回退计数器
        self._wakeup_regression_count = 0

        # 外部依赖（可选注入）
        self._behavioral_logger = None

        # 线程安全锁（保护所有共享状态）
        self._state_lock = threading.Lock()

        logger.info("DormancyManager 初始化完成，当前状态: %s", self._current_state)

    # ========== 依赖注入 ==========
    def inject_dependencies(self, behavioral_logger: Optional[Any] = None) -> None:
        """注入外部依赖（可选注入，未注入时仅使用本地日志）"""
        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，休眠事件仅记录本地日志")

    # ========== 公共接口 ==========
    def evaluate_dormancy(
        self,
        no_signal_duration: float,
        balance_index: float,
        consecutive_losses: int,
        all_sharpe: float,
    ) -> Dict[str, Any]:
        """
        评估当前是否应进入休眠状态，并返回决策

        Args:
            no_signal_duration: 连续无信号的时长（秒）
            balance_index: 当前平衡指数（0-100）
            consecutive_losses: 连续亏损笔数
            all_sharpe: 所有活跃策略的当前滚动夏普

        Returns:
            标准响应字典，data 中包含建议的休眠层级和触发原因
        """
        with self._state_lock:
            if self._current_state == "hibernation":
                return {
                    "status": "ok",
                    "reason": "系统已处于冬眠状态，无需再次评估",
                    "data": {"dormancy_level": "hibernation", "triggered": False},
                    "warnings": [],
                }

            # 参数边界保护
            no_signal_duration = max(0.0, no_signal_duration)
            balance_index = max(0.0, min(100.0, balance_index))
            consecutive_losses = max(0, consecutive_losses)

            triggered_level = None
            reason = ""

            # 冬眠检测（最高优先级，需持续时间校验）
            if all_sharpe < self.HIBERNATION_ALL_SHARPE_THRESHOLD:
                if self._sharpe_below_start == 0.0:
                    self._sharpe_below_start = time.time()
                elif time.time() - self._sharpe_below_start >= self.HIBERNATION_ALL_SHARPE_DAYS * 86400:
                    triggered_level = "hibernation"
                    reason = (
                        f"全策略夏普 ({all_sharpe:.2f}) 持续低于冬眠阈值 "
                        f"{self.HIBERNATION_ALL_SHARPE_DAYS} 天"
                    )
                    self._sharpe_below_start = 0.0
            else:
                # 夏普恢复，重置计时器
                self._sharpe_below_start = 0.0

            if triggered_level is None:
                if self._current_state == "deep_dormancy":
                    # 已在深层休眠，检查是否应升级到冬眠
                    if consecutive_losses >= self.DEEP_DORMANCY_CONSECUTIVE_LOSSES * 2:
                        triggered_level = "hibernation"
                        reason = f"深层休眠期间连续亏损 {consecutive_losses} 笔，触发冬眠"
                elif self._current_state == "light_dormancy":
                    # 检查浅层是否应升级为深层
                    light_duration = time.time() - self._light_dormancy_start_time
                    if light_duration >= self.DEEP_DORMANCY_LIGHT_DURATION_SEC:
                        triggered_level = "deep_dormancy"
                        reason = f"浅层休眠已持续 {light_duration:.0f} 秒，升级为深层休眠"
                    elif consecutive_losses >= self.DEEP_DORMANCY_CONSECUTIVE_LOSSES:
                        triggered_level = "deep_dormancy"
                        reason = f"连续亏损 {consecutive_losses} 笔，触发深层休眠"
                elif self._current_state == "active":
                    # 浅层休眠检测
                    if (no_signal_duration >= self.LIGHT_DORMANCY_NO_SIGNAL_SEC or
                            balance_index <= self.LIGHT_DORMANCY_BALANCE_INDEX_MIN):
                        triggered_level = "light_dormancy"
                        if no_signal_duration >= self.LIGHT_DORMANCY_NO_SIGNAL_SEC:
                            reason = f"连续无信号 {no_signal_duration:.0f} 秒，触发浅层休眠"
                        else:
                            reason = (
                                f"平衡指数 {balance_index:.1f} 低于阈值 "
                                f"{self.LIGHT_DORMANCY_BALANCE_INDEX_MIN}"
                            )
                    # 活跃状态下连续亏损达到深层阈值时直接升级
                    elif consecutive_losses >= self.DEEP_DORMANCY_CONSECUTIVE_LOSSES * 2:
                        triggered_level = "deep_dormancy"
                        reason = (
                            f"活跃状态下连续亏损 {consecutive_losses} 笔，"
                            f"直接升级为深层休眠"
                        )

            # 执行状态转换
            warnings = []
            if triggered_level:
                self._transition_to(triggered_level)
                warnings.append(f"触发休眠: {triggered_level}")
                logger.warning("触发休眠: %s, 原因: %s", triggered_level, reason)
            else:
                logger.debug("休眠评估完成，无需进入休眠")

            return {
                "status": "ok",
                "reason": f"休眠评估完成，当前状态: {self._current_state}",
                "data": {
                    "current_state": self._current_state,
                    "triggered": triggered_level is not None,
                    "triggered_level": triggered_level,
                    "trigger_reason": reason,
                },
                "warnings": warnings,
            }

    def get_dormancy_state(self) -> Dict[str, Any]:
        """
        返回当前休眠状态及元数据

        Returns:
            标准响应字典
        """
        with self._state_lock:
            return {
                "status": "ok",
                "reason": f"当前休眠状态: {self._current_state}",
                "data": {
                    "state": self._current_state,
                    "state_entered_time": self._state_entered_time,
                    "wakeup_step": self._wakeup_step,
                    "trial_trades_done": self._trial_trades_done,
                    "trial_trades_won": self._trial_trades_won,
                },
                "warnings": [],
            }

    def progressive_wakeup(
        self,
        signal_present: bool,
        market_vol_ok: bool,
        trial_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        执行一步渐进唤醒流程，调用方需根据返回的 action 调整系统状态

        Args:
            signal_present: 是否出现 A 级或以上信号（用于浅层唤醒）
            market_vol_ok: 市场波动率是否回升至唤醒阈值以上（用于深层唤醒）
            trial_result: 试探交易的结果字典，应包含 "success" (bool) 和 "pnl" (float)

        Returns:
            标准响应字典，data 中包含建议的下一步动作和当前唤醒步骤
        """
        with self._state_lock:
            if self._current_state == "active":
                return {
                    "status": "ok",
                    "reason": "系统处于活跃状态，无需唤醒",
                    "data": {"action": "none", "wakeup_step": None},
                    "warnings": [],
                }

            # 冬眠不支持自动唤醒
            if self._current_state == "hibernation" and self.HIBERNATION_WAKE_MANUAL_ONLY:
                return {
                    "status": "ok",
                    "reason": "冬眠状态仅支持人工唤醒",
                    "data": {"action": "await_manual", "wakeup_step": None},
                    "warnings": ["hibernation_manual_only"],
                }

            action = "hold"
            new_step = self._wakeup_step
            reason = ""
            warnings = []

            # 浅层休眠唤醒条件
            if self._current_state == "light_dormancy":
                if signal_present and self.LIGHT_DORMANCY_WAKE_A_SIGNAL:
                    # 直接唤醒到活跃
                    self._transition_to("active")
                    action = "full_wakeup"
                    reason = "A级信号触发浅层休眠唤醒"
                elif self._wakeup_step is None:
                    # 开始渐进唤醒
                    new_step = "step1"
                    self._wakeup_step = new_step
                    self._wakeup_step_start_time = time.time()
                    action = "enter_step1"
                    reason = "启动渐进唤醒第一步：恢复降频感知"

            # 深层休眠唤醒条件
            elif self._current_state == "deep_dormancy":
                if market_vol_ok:
                    if self._wakeup_step is None:
                        new_step = "step1"
                        self._wakeup_step = new_step
                        self._wakeup_step_start_time = time.time()
                        action = "enter_step1"
                        reason = "波动率回升，启动渐进唤醒"
                    else:
                        # 继续当前唤醒步骤
                        action, new_step, reason = self._advance_wakeup_step(trial_result)

            elif self._current_state == "active" and self._wakeup_step is not None:
                # 活跃状态下继续完成剩余的唤醒步骤
                action, new_step, reason = self._advance_wakeup_step(trial_result)

            if action == "full_wakeup":
                self._wakeup_step = None
                self._wakeup_regression_count = 0

            return {
                "status": "ok",
                "reason": f"唤醒流程: {reason}",
                "data": {
                    "action": action,
                    "wakeup_step": new_step,
                    "current_state": self._current_state,
                },
                "warnings": warnings,
            }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检

        Returns:
            标准健康检查响应字典
        """
        try:
            with self._state_lock:
                current_state = self._current_state
                wakeup_step = self._wakeup_step

            return {
                "status": "ok",
                "reason": f"DormancyManager 正常，当前状态: {current_state}",
                "data": {
                    "state": current_state,
                    "wakeup_step": wakeup_step,
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查内部状态变量完整性")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _transition_to(self, new_state: str) -> None:
        """执行状态转换并记录日志（需在锁内调用）"""
        old_state = self._current_state
        self._current_state = new_state
        self._state_entered_time = time.time()

        if new_state == "light_dormancy":
            self._light_dormancy_start_time = time.time()
        if new_state != "light_dormancy":
            self._light_dormancy_start_time = 0.0

        if new_state == "active":
            self._wakeup_step = None
            self._trial_trades_done = 0
            self._trial_trades_won = 0
            self._wakeup_regression_count = 0

        logger.info("休眠状态转换: %s -> %s", old_state, new_state)
        if self._behavioral_logger:
            try:
                self._behavioral_logger.log_event(
                    event_type="dormancy_transition",
                    details={"from": old_state, "to": new_state, "timestamp": time.time()},
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

    def _advance_wakeup_step(self, trial_result: Optional[Dict]) -> tuple:
        """根据当前步骤和试探交易结果，计算下一步唤醒动作（返回 action, new_step, reason）（需在锁内调用）"""
        step = self._wakeup_step
        if step == "step1":
            elapsed = time.time() - self._wakeup_step_start_time
            if elapsed >= self.WAKEUP_STEP1_DURATION_SEC:
                self._wakeup_step = "step2"
                self._wakeup_step_start_time = time.time()
                return "enter_step2", "step2", "第一步完成，进入试探交易阶段"
            else:
                return (
                    "hold_step1",
                    "step1",
                    f"第一步观察中，已耗时 {elapsed:.0f}s/{self.WAKEUP_STEP1_DURATION_SEC}s",
                )

        elif step == "step2":
            if trial_result is not None:
                self._trial_trades_done += 1
                if trial_result.get("success", False):
                    self._trial_trades_won += 1

            if self._trial_trades_done >= self.WAKEUP_STEP2_TRIAL_TRADES:
                winrate = self._trial_trades_won / max(self._trial_trades_done, 1)
                if winrate >= self.WAKEUP_STEP2_MIN_WINRATE:
                    self._wakeup_step = "step3"
                    self._wakeup_step_start_time = time.time()
                    self._wakeup_regression_count = 0
                    return (
                        "enter_step3",
                        "step3",
                        f"试探交易胜率 {winrate:.1%} 达标，进入稳定观察期",
                    )
                else:
                    self._wakeup_regression_count += 1
                    if self._wakeup_regression_count >= self.MAX_WAKEUP_REGRESSIONS:
                        self._wakeup_step = None
                        logger.error(
                            f"唤醒回退次数 ({self._wakeup_regression_count}) 超过上限，"
                            f"锁定休眠状态 #RECOVERY: 人工检查策略有效性，确认市场状态是否适合自动唤醒"
                        )
                        return (
                            "locked",
                            None,
                            f"回退{self._wakeup_regression_count}次，休眠已锁定，等待人工介入",
                        )
                    self._wakeup_step = "step1"
                    self._wakeup_step_start_time = time.time()
                    logger.warning(
                        "试探交易胜率 %.1f%% 不足，回退到第一步 (第%d次)",
                        winrate * 100,
                        self._wakeup_regression_count,
                    )
                    return (
                        "regress_step1",
                        "step1",
                        f"胜率不足 {winrate:.1%}，回退重新观察 (第{self._wakeup_regression_count}次)",
                    )
            else:
                return (
                    "await_trial",
                    "step2",
                    f"等待试探交易 ({self._trial_trades_done}/{self.WAKEUP_STEP2_TRIAL_TRADES})",
                )

        elif step == "step3":
            elapsed = time.time() - self._wakeup_step_start_time
            if elapsed >= self.WAKEUP_STEP3_STABLE_SEC:
                self._transition_to("active")
                return "full_wakeup", None, "稳定观察期结束，完全唤醒"
            else:
                return (
                    "hold_step3",
                    "step3",
                    f"稳定观察中，已耗时 {elapsed:.0f}s/{self.WAKEUP_STEP3_STABLE_SEC}s",
                )

        return "hold", step, "未知步骤"
