"""
火种系统 · 安息日调度控制器 (SabbathController)

核心职责：
1. 管理系统的安息日周期：判断进入/退出条件，维护安息日状态机
2. 协调安息日期间的算力重分配：触发全量回测、影子验证、梦境推演、经验整理等深度进化任务

外部依赖（真实模块接口）：
- core.engine.dormancy_manager.DormancyManager : 在安息日前后管理分层休眠状态的切换
- core.compute_scheduler.ComputeScheduler : 在安息日期间重分配算力资源
- core.pipeline_bus.PipelineBus : 暂停或恢复交易流水线
- core.negotiation_bus.NegotiationBus : 广播安息日状态变更事件
- core.behavioral_logger.BehavioralLogger : 记录安息日相关事件

接口契约：
- is_sabbath(now: float) -> bool : 判断当前是否处于安息日
- enter_sabbath() -> Dict[str, Any] : 手动触发进入安息日
- exit_sabbath() -> Dict[str, Any] : 手动触发退出安息日
- get_sabbath_status() -> Dict[str, Any] : 获取当前安息日状态
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 DormancyManager 不可用时，安息日进出流程跳过休眠步骤，仅切换交易模式
- 当 ComputeScheduler 不可用时，安息日算力重分配降级为仅暂停非核心任务
- 当 NegotiationBus 不可用时，状态变更广播降级为本地日志
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护安息日状态机，不持有任何需要手动释放的外部资源
- 定时器依赖主事件循环的 Tick 驱动，无独立线程
"""

import calendar
import time
import logging
import threading
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class SabbathController:
    """安息日调度控制器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 安息日周期定义
    DEFAULT_SABBATH_DAY_OF_WEEK = 5        # 安息日触发日，0=周一，6=周日，默认周六
    DEFAULT_SABBATH_WEEK_OF_MONTH = 1      # 每月的第几个触发日，1=第一个，取值范围 [1, 4]
    DEFAULT_SABBATH_START_HOUR = 0          # 安息日开始小时 (UTC)，取值范围 [0, 23]
    DEFAULT_SABBATH_DURATION_HOURS = 6      # 安息日持续时长，小时，取值范围 [2, 12]

    # 安息日期间实盘切换限制
    MAX_REAL_SWITCH_TRADES = 3             # 最多允许的实盘切换次数，整数，取值范围 [0, 10]
    REAL_SWITCH_SIGNAL_TIER = "A"          # 允许触发实盘切换的最低信号等级，A/B/C
    REAL_SWITCH_SIZE_MULT = 0.5            # 实盘切换后的仓位系数，无量纲，取值范围 [0.1, 1.0]

    # 状态检查间隔（通过外部 Tick 驱动）
    STATE_CHECK_INTERVAL_SEC = 60          # 状态检查最小间隔，秒，取值范围 [10, 300]

    def __init__(self):
        # 安息日状态
        self._active = False               # 是否处于安息日
        self._last_enter_time = 0.0        # 最近一次进入安息日的时间戳
        self._last_exit_time = 0.0         # 最近一次退出安息日的时间戳
        self._real_switch_count = 0        # 本次安息日已发生的实盘切换次数
        self._last_check_time = 0.0        # 上次周期性检查的时间

        # 外部依赖注入
        self._dormancy_manager = None
        self._compute_scheduler = None
        self._pipeline_bus = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 线程安全锁（保护所有共享状态）
        self._state_lock = threading.Lock()

        logger.info("SabbathController 初始化完成，默认触发周期: 每月第%d个周%d %02d:00，持续%d小时",
                     self.DEFAULT_SABBATH_WEEK_OF_MONTH,
                     self.DEFAULT_SABBATH_DAY_OF_WEEK + 1,
                     self.DEFAULT_SABBATH_START_HOUR,
                     self.DEFAULT_SABBATH_DURATION_HOURS)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        dormancy_manager: Optional[Any] = None,
        compute_scheduler: Optional[Any] = None,
        pipeline_bus: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选注入，未注入时对应功能降级）"""
        if dormancy_manager is not None:
            self._dormancy_manager = dormancy_manager
            logger.info("DormancyManager 注入成功")
        else:
            logger.warning("DormancyManager 未注入，安息日进出跳过休眠管理")

        if compute_scheduler is not None:
            self._compute_scheduler = compute_scheduler
            logger.info("ComputeScheduler 注入成功")
        else:
            logger.warning("ComputeScheduler 未注入，安息日算力重分配降级为仅暂停非核心任务")

        if pipeline_bus is not None:
            self._pipeline_bus = pipeline_bus
            logger.info("PipelineBus 注入成功")
        else:
            logger.warning("PipelineBus 未注入，交易流水线暂停降级为仅拒绝新开仓")

        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, 'publish_alert'):
                logger.warning("NegotiationBus 缺少 publish_alert 方法，状态广播不可用")
                self._negotiation_bus = None
            else:
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，事件日志降级为标准 logger")

    # ========== 公共接口 ==========
    def is_sabbath(self, now: float = None) -> bool:
        """
        判断当前是否处于安息日

        Args:
            now: 当前时间戳，默认使用 time.time()

        Returns:
            布尔值，True 表示处于安息日
        """
        if now is None:
            now = time.time()

        with self._state_lock:
            # 如果已经处于激活状态，检查是否超时
            if self._active:
                if now - self._last_enter_time >= self.DEFAULT_SABBATH_DURATION_HOURS * 3600:
                    self._deactivate(now)
                    return False
                return True

            # 未激活时，检查是否满足进入条件（在锁外调用耗时计算，锁内只做状态检查）
        return self._should_activate(now)

    def enter_sabbath(self) -> Dict[str, Any]:
        """
        手动触发进入安息日（供前端应急窗口或管理员命令使用）

        Returns:
            标准响应字典
        """
        now = time.time()
        with self._state_lock:
            if self._active:
                return {
                    "status": "ok",
                    "reason": "当前已处于安息日，无需重复进入",
                    "data": {"sabbath_active": True, "entered_at": self._last_enter_time},
                    "warnings": [],
                }

            self._activate(now)

        return {
            "status": "ok",
            "reason": f"手动触发安息日，开始时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now))}",
            "data": {"sabbath_active": True, "entered_at": now},
            "warnings": [],
        }

    def exit_sabbath(self) -> Dict[str, Any]:
        """
        手动触发退出安息日（供前端应急窗口或管理员命令使用）

        Returns:
            标准响应字典
        """
        now = time.time()
        with self._state_lock:
            if not self._active:
                return {
                    "status": "ok",
                    "reason": "当前未处于安息日，无需退出",
                    "data": {"sabbath_active": False},
                    "warnings": [],
                }

            self._deactivate(now, manual=True)

        return {
            "status": "ok",
            "reason": f"手动退出安息日，结束时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now))}",
            "data": {"sabbath_active": False, "exited_at": now},
            "warnings": [],
        }

    def get_sabbath_status(self) -> Dict[str, Any]:
        """
        获取当前安息日状态

        Returns:
            标准响应字典，data 中包含 active, entered_at, remaining_seconds 等字段
        """
        now = time.time()
        with self._state_lock:
            remaining = 0.0
            if self._active:
                elapsed = now - self._last_enter_time
                total_sec = self.DEFAULT_SABBATH_DURATION_HOURS * 3600
                remaining = max(0.0, total_sec - elapsed)

        return {
            "status": "ok",
            "reason": "安息日状态查询",
            "data": {
                "active": self._active,
                "entered_at": self._last_enter_time if self._active else None,
                "remaining_seconds": round(remaining, 1),
                "real_switch_count": self._real_switch_count,
                "max_real_switches": self.MAX_REAL_SWITCH_TRADES,
                "next_scheduled": self._get_next_sabbath_time(),
            },
            "warnings": [],
        }

    def try_real_switch(self, signal_tier: str) -> Dict[str, Any]:
        """
        在安息日期间，策略引擎尝试将虚拟订单转为实盘订单

        Args:
            signal_tier: 当前信号的等级 (A/B/C)

        Returns:
            标准响应字典，data 中包含 allowed, reason, remaining_quota 等字段
        """
        tier_priority = {"A": 3, "B": 2, "C": 1}
        required = tier_priority.get(self.REAL_SWITCH_SIGNAL_TIER, 3)

        if signal_tier not in tier_priority:
            logger.warning(f"无效的信号等级: {signal_tier}，无法进行安息日实盘切换评估")
            return {
                "status": "error",
                "reason": f"无效的信号等级: {signal_tier}，有效值为 {list(tier_priority.keys())}",
                "data": {},
                "warnings": [f"invalid_signal_tier: {signal_tier}"],
            }

        current = tier_priority[signal_tier]
        if current < required:
            return {
                "status": "ok",
                "reason": f"信号等级 {signal_tier} 低于安息日实盘切换最低要求 {self.REAL_SWITCH_SIGNAL_TIER}",
                "data": {"allowed": False},
                "warnings": [],
            }

        with self._state_lock:
            if not self._active:
                return {
                    "status": "ok",
                    "reason": "非安息日，正常执行实盘订单",
                    "data": {"allowed": True},
                    "warnings": [],
                }

            # 检查是否超过最大切换次数
            if self._real_switch_count >= self.MAX_REAL_SWITCH_TRADES:
                return {
                    "status": "ok",
                    "reason": f"本次安息日实盘切换次数已达上限 ({self.MAX_REAL_SWITCH_TRADES})",
                    "data": {"allowed": False, "remaining_quota": 0},
                    "warnings": ["sabbath_real_switch_quota_exhausted"],
                }

            self._real_switch_count += 1
            remaining_quota = self.MAX_REAL_SWITCH_TRADES - self._real_switch_count

        logger.info("安息日实盘切换: 信号等级 %s, 本次安息日累计 %d/%d",
                     signal_tier, self._real_switch_count, self.MAX_REAL_SWITCH_TRADES)

        return {
            "status": "ok",
            "reason": f"安息日实盘切换批准，信号等级 {signal_tier}，剩余配额 {remaining_quota}",
            "data": {
                "allowed": True,
                "size_mult": self.REAL_SWITCH_SIZE_MULT,
                "remaining_quota": remaining_quota,
            },
            "warnings": [],
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
                if self._active and self._last_enter_time <= 0:
                    return {
                        "status": "error",
                        "reason": "安息日状态不一致：active=True 但 last_enter_time 无效",
                        "data": {},
                        "warnings": ["state_inconsistency"],
                    }

                next_sabbath = self._get_next_sabbath_time()

                return {
                    "status": "ok",
                    "reason": "SabbathController 正常运行",
                    "data": {
                        "active": self._active,
                        "next_scheduled": next_sabbath,
                        "dependencies": {
                            "dormancy_manager": self._dormancy_manager is not None,
                            "compute_scheduler": self._compute_scheduler is not None,
                            "pipeline_bus": self._pipeline_bus is not None,
                            "negotiation_bus": self._negotiation_bus is not None,
                            "behavioral_logger": self._behavioral_logger is not None,
                        },
                    },
                    "warnings": [],
                }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查安息日状态变量完整性")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _should_activate(self, now: float) -> bool:
        """判断当前是否满足安息日进入条件（基于可配置的周期规则）"""
        # 解析当前 UTC 时间
        tm = time.gmtime(now)
        current_weekday = tm.tm_wday
        current_hour = tm.tm_hour
        current_minute = tm.tm_min
        current_day = tm.tm_mday

        # 检查星期几
        if current_weekday != self.DEFAULT_SABBATH_DAY_OF_WEEK:
            return False

        # 检查是否是本月的第 N 个该星期几
        week_of_month = (current_day - 1) // 7 + 1
        if week_of_month != self.DEFAULT_SABBATH_WEEK_OF_MONTH:
            return False

        # 检查小时（在开始小时的前 5 分钟内允许触发，避免因 Tick 间隔错过）
        if current_hour != self.DEFAULT_SABBATH_START_HOUR:
            return False

        # 避免在同一分钟内重复检查
        with self._state_lock:
            if now - self._last_check_time < self.STATE_CHECK_INTERVAL_SEC:
                return False
            self._last_check_time = now

        # 所有条件满足，触发进入（_activate 内部有锁，但此处不持有锁，避免死锁）
        logger.info("满足安息日进入条件: 第%d个周%d %02d:%02d",
                     week_of_month, current_weekday + 1, current_hour, current_minute)
        self._activate(now)
        return True

    def _activate(self, now: float) -> None:
        """执行进入安息日的所有动作（需在锁保护下调用）"""
        with self._state_lock:
            if self._active:  # 幂等性保护
                return
            self._active = True
            self._last_enter_time = now
            self._real_switch_count = 0

        logger.info("安息日开始: %s", time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now)))

        # 1. 暂停交易流水线（禁止新开仓）
        if self._pipeline_bus is not None:
            try:
                self._pipeline_bus.pause_new_pipelines()
                logger.info("交易流水线已暂停新开仓")
            except Exception as e:
                logger.warning(f"暂停交易流水线失败: {e}")

        # 2. 触发算力重分配
        if self._compute_scheduler is not None:
            try:
                self._compute_scheduler.enter_sabbath_mode()
                logger.info("算力已切换至安息日模式")
            except Exception as e:
                logger.warning(f"算力重分配失败: {e}")

        # 3. 广播安息日状态变更
        self._broadcast_status("enter")

    def _deactivate(self, now: float, manual: bool = False) -> None:
        """执行退出安息日的所有动作（需在锁保护下调用）"""
        with self._state_lock:
            if not self._active:  # 幂等性保护
                return
            self._active = False
            self._last_exit_time = now

        logger.info("安息日结束 (%s): %s",
                     "手动触发" if manual else "自动到期",
                     time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now)))

        # 1. 恢复交易流水线
        if self._pipeline_bus is not None:
            try:
                self._pipeline_bus.resume_new_pipelines()
                logger.info("交易流水线已恢复新开仓")
            except Exception as e:
                logger.warning(f"恢复交易流水线失败: {e}")

        # 2. 恢复算力分配
        if self._compute_scheduler is not None:
            try:
                self._compute_scheduler.exit_sabbath_mode()
                logger.info("算力已恢复至正常模式")
            except Exception as e:
                logger.warning(f"算力恢复失败: {e}")

        # 3. 广播安息日状态变更
        self._broadcast_status("exit")

    def _broadcast_status(self, action: str) -> None:
        """广播安息日状态变更事件"""
        event_data = {
            "action": action,
            "timestamp": time.time(),
            "active": self._active,
        }

        # 通过协商总线广播
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="sabbath_status_change",
                    level="info",
                    message=f"安息日状态变更: {action}",
                    timestamp=time.time(),
                    details=event_data,
                )
            except Exception as e:
                logger.warning(f"协商总线广播失败: {e}")

        # 行为日志记录
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="sabbath_transition",
                    details=event_data,
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

    def _get_next_sabbath_time(self) -> float:
        """计算下一个安息日开始时间戳（UTC）"""
        now = time.time()
        tm = time.gmtime(now)
        current_year = tm.tm_year
        current_month = tm.tm_mon

        # 计算本月第一个目标星期几的日期
        first_day = calendar.timegm(time.strptime(
            f"{current_year}-{current_month:02d}-01", "%Y-%m-%d"))
        first_weekday = time.gmtime(first_day).tm_wday
        days_until_target = (self.DEFAULT_SABBATH_DAY_OF_WEEK - first_weekday) % 7
        first_target_day = 1 + days_until_target

        # 第 N 个目标日期
        target_day = first_target_day + (self.DEFAULT_SABBATH_WEEK_OF_MONTH - 1) * 7

        # 处理超出当月天数的情况：回退到前一个目标星期几（如第5个周六不存在则取第4个）
        max_day = calendar.monthrange(current_year, current_month)[1]
        if target_day > max_day:
            target_day = first_target_day + (self.DEFAULT_SABBATH_WEEK_OF_MONTH - 2) * 7
            logger.debug("本月第%d个目标日不存在，回退至第%d个", 
                         self.DEFAULT_SABBATH_WEEK_OF_MONTH, 
                         self.DEFAULT_SABBATH_WEEK_OF_MONTH - 1)

        sabbath_start = calendar.timegm(time.strptime(
            f"{current_year}-{current_month:02d}-{target_day:02d} "
            f"{self.DEFAULT_SABBATH_START_HOUR:02d}:00:00", "%Y-%m-%d %H:%M:%S"))

        # 如果本月的安息日已经过去，计算下个月的
        if sabbath_start <= now:
            if current_month == 12:
                next_year = current_year + 1
                next_month = 1
            else:
                next_year = current_year
                next_month = current_month + 1

            first_day_next = calendar.timegm(time.strptime(
                f"{next_year}-{next_month:02d}-01", "%Y-%m-%d"))
            next_first_weekday = time.gmtime(first_day_next).tm_wday
            days_until = (self.DEFAULT_SABBATH_DAY_OF_WEEK - next_first_weekday) % 7
            next_first_target = 1 + days_until
            next_target_day = next_first_target + (self.DEFAULT_SABBATH_WEEK_OF_MONTH - 1) * 7

            max_day_next = calendar.monthrange(next_year, next_month)[1]
            if next_target_day > max_day_next:
                next_target_day = next_first_target + (self.DEFAULT_SABBATH_WEEK_OF_MONTH - 2) * 7

            sabbath_start = calendar.timegm(time.strptime(
                f"{next_year}-{next_month:02d}-{next_target_day:02d} "
                f"{self.DEFAULT_SABBATH_START_HOUR:02d}:00:00", "%Y-%m-%d %H:%M:%S"))

        return sabbath_start
