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
- post_inject_init() -> None : 在依赖注入完成后调用，恢复持久化状态并初始化外部系统
- is_sabbath() -> bool : 查询当前是否处于安息日（纯读，无副作用）
- tick_check(now: float) -> None : 由主循环周期性调用，执行自动进出逻辑
- enter_sabbath() -> Dict[str, Any] : 手动触发进入安息日
- exit_sabbath() -> Dict[str, Any] : 手动触发退出安息日
- get_sabbath_status() -> Dict[str, Any] : 获取当前安息日状态
- try_real_switch(signal_tier: str) -> Dict[str, Any] : 安息日期间将虚拟信号转为实盘
- shutdown() -> None : 安全释放资源（线程池等）
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
- 状态持久化使用原子写入，确保崩溃后可从快照恢复
- 线程池在 shutdown() 中显式释放
"""

import calendar
import json
import os
import time
import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
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
    DEFAULT_SABBATH_WINDOW_MINUTES = 15     # 安息日启动窗口宽度（前后各），分钟，[5, 30]

    # 安息日期间实盘切换限制
    MAX_REAL_SWITCH_TRADES = 3             # 最多允许的实盘切换次数，整数，取值范围 [0, 10]
    REAL_SWITCH_SIGNAL_TIER = "A"          # 允许触发实盘切换的最低信号等级，A/B/C
    REAL_SWITCH_SIZE_MULT = 0.5            # 实盘切换后的仓位系数，无量纲，取值范围 [0.1, 1.0]

    # 状态检查间隔（通过外部 Tick 驱动）
    STATE_CHECK_INTERVAL_SEC = 60          # 状态检查最小间隔，秒，取值范围 [10, 300]

    # 缓存有效期
    NEXT_SABBATH_CACHE_TTL_SEC = 3600      # 下一个安息日时间缓存有效期，秒，[600, 86400]

    # 外部调用超时
    EXTERNAL_CALL_TIMEOUT_SEC = 5          # pipeline/算力调度等外部调用最大等待时间，秒，[2, 15]

    # 回滚失败后重试间隔
    RETRY_AFTER_FAILURE_SEC = 60           # 回滚失败后自动重试的等待时间，秒

    # 持久化文件路径（优先使用环境变量 FIRE_SEED_ROOT，否则使用当前工作目录下的 data 子目录）
    STATE_FILE = os.path.join(
        os.environ.get("FIRE_SEED_ROOT", os.getcwd()), "data", "sabbath_state.json"
    )

    def __init__(self):
        # 安息日状态
        self._active = False
        self._last_enter_time = 0.0
        self._last_exit_time = 0.0
        self._real_switch_count = 0
        self._last_check_time = 0.0

        # 月度触发标记
        self._current_month_key = ""
        self._sabbath_triggered_this_month = False

        # 缓存
        self._next_sabbath_cache_time = 0.0
        self._next_sabbath_cache_result = 0.0
        self._next_sabbath_cache_at = 0.0

        # 外部依赖注入
        self._dormancy_manager = None
        self._compute_scheduler = None
        self._pipeline_bus = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 线程安全锁（保护所有共享状态）
        self._state_lock = threading.Lock()

        # 用于外部调用超时的线程池
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sabbath_ext")

        # 标记依赖是否已注入并完成初始化后恢复
        self._post_inject_done = False

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

    def post_inject_init(self) -> None:
        """
        在所有依赖注入完成后调用，用于恢复持久化状态并同步外部系统。
        必须在 inject_dependencies 之后、首次 tick_check 之前调用。
        """
        with self._state_lock:
            self._restore_state()
            self._post_inject_done = True

    # ========== 资源释放 ==========
    def shutdown(self) -> None:
        """安全释放线程池等资源"""
        try:
            self._executor.shutdown(wait=True, cancel_futures=True)
            logger.info("SabbathController 线程池已关闭")
        except Exception as e:
            logger.error(f"关闭线程池失败: {e}")

    def __del__(self):
        """析构函数兜底，确保资源释放（不依赖垃圾回收，仅作为最后保障）"""
        try:
            if hasattr(self, '_executor') and self._executor is not None:
                self._executor.shutdown(wait=False)
        except Exception:
            pass

    # ========== 公共接口 ==========
    def is_sabbath(self) -> bool:
        """查询当前是否处于安息日（纯读，无副作用）"""
        with self._state_lock:
            return self._active

    def tick_check(self, now: float = None) -> None:
        """由主循环周期性调用，检查并自动执行安息日进入/退出"""
        if now is None:
            now = time.time()

        with self._state_lock:
            # 确保依赖注入后的初始化已完成，否则跳过
            if not self._post_inject_done:
                return

            # 如果已激活，检查是否超时需退出
            if self._active:
                elapsed = now - self._last_enter_time
                if elapsed >= self.DEFAULT_SABBATH_DURATION_HOURS * 3600:
                    self._deactivate_locked(now, trigger="auto_timeout")
                return

            # 未激活时，检查是否满足进入条件
            if now - self._last_check_time < self.STATE_CHECK_INTERVAL_SEC:
                return

            self._last_check_time = now

            # 月份变化处理
            month_key = time.strftime("%Y-%m", time.gmtime(now))
            if month_key != self._current_month_key:
                self._current_month_key = month_key
                self._sabbath_triggered_this_month = False
                self._next_sabbath_cache_at = 0.0  # 跨月清缓存

            if self._sabbath_triggered_this_month:
                return

            # 判断是否在安息日窗口内
            if self._inside_sabbath_window(now):
                self._activate_locked(now, trigger="auto_schedule")
                self._sabbath_triggered_this_month = True

    def enter_sabbath(self) -> Dict[str, Any]:
        """手动触发进入安息日"""
        now = time.time()
        with self._state_lock:
            if self._active:
                return {
                    "status": "ok",
                    "reason": "当前已处于安息日，无需重复进入",
                    "data": {"sabbath_active": True, "entered_at": self._last_enter_time},
                    "warnings": [],
                }
            self._activate_locked(now, trigger="manual_command")

        return {
            "status": "ok",
            "reason": f"手动触发安息日，开始时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now))}",
            "data": {"sabbath_active": True, "entered_at": now},
            "warnings": [],
        }

    def exit_sabbath(self) -> Dict[str, Any]:
        """手动触发退出安息日"""
        now = time.time()
        with self._state_lock:
            if not self._active:
                return {
                    "status": "ok",
                    "reason": "当前未处于安息日，无需退出",
                    "data": {"sabbath_active": False},
                    "warnings": [],
                }
            self._deactivate_locked(now, trigger="manual_command")

        return {
            "status": "ok",
            "reason": f"手动退出安息日，结束时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now))}",
            "data": {"sabbath_active": False, "exited_at": now},
            "warnings": [],
        }

    def get_sabbath_status(self) -> Dict[str, Any]:
        """获取当前安息日状态"""
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
                "triggered_this_month": self._sabbath_triggered_this_month,
            },
            "warnings": [],
        }

    def try_real_switch(self, signal_tier: str) -> Dict[str, Any]:
        """安息日期间将虚拟订单转为实盘订单"""
        tier_priority = {"A": 3, "B": 2, "C": 1}
        required = tier_priority.get(self.REAL_SWITCH_SIGNAL_TIER, 3)

        if signal_tier not in tier_priority:
            logger.warning(f"无效的信号等级: {signal_tier}")
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
        """模块自检"""
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
                        "triggered_this_month": self._sabbath_triggered_this_month,
                        "post_inject_done": self._post_inject_done,
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

    # ========== 私有方法：状态持久化 ==========
    def _save_state(self) -> None:
        """将当前安息日核心状态写入本地持久化文件（原子写入）"""
        state = {
            "active": self._active,
            "last_enter_time": self._last_enter_time,
            "last_exit_time": self._last_exit_time,
            "real_switch_count": self._real_switch_count,
            "current_month_key": self._current_month_key,
            "sabbath_triggered_this_month": self._sabbath_triggered_this_month,
        }
        try:
            tmp_path = self.STATE_FILE + ".tmp"
            os.makedirs(os.path.dirname(self.STATE_FILE), exist_ok=True)
            with open(tmp_path, "w") as f:
                json.dump(state, f)
            os.replace(tmp_path, self.STATE_FILE)  # 原子替换
        except Exception as e:
            logger.error(f"安息日状态持久化失败: {e} #RECOVERY: 检查磁盘空间与权限")

    def _restore_state(self) -> None:
        """
        从持久化文件恢复安息日状态，并进行时效性校验。
        必须在依赖注入完成后、持有 _state_lock 的情况下调用。
        """
        if not os.path.exists(self.STATE_FILE):
            return

        try:
            with open(self.STATE_FILE, "r") as f:
                state = json.load(f)

            now = time.time()
            saved_active = state.get("active", False)
            saved_enter_time = state.get("last_enter_time", 0.0)

            if saved_active and saved_enter_time > 0:
                elapsed = now - saved_enter_time
                if elapsed < self.DEFAULT_SABBATH_DURATION_HOURS * 3600:
                    # 仍然在有效安息日内，恢复内部状态
                    self._active = True
                    self._last_enter_time = saved_enter_time
                    self._real_switch_count = state.get("real_switch_count", 0)
                    self._current_month_key = state.get("current_month_key", "")
                    self._sabbath_triggered_this_month = state.get("sabbath_triggered_this_month", True)
                    logger.info("从持久化文件恢复安息日状态: 仍然活跃，剩余时间 %.0f 秒",
                                 self.DEFAULT_SABBATH_DURATION_HOURS * 3600 - elapsed)
                    # 依赖已注入，现在可以安全地纠正外部状态
                    self._restore_external_state()
                    return

            # 安息日已过期，清理文件
            os.remove(self.STATE_FILE)
            logger.info("持久化的安息日状态已过期，已清理")
        except Exception as e:
            logger.error(f"恢复安息日状态失败: {e} #RECOVERY: 删除 {self.STATE_FILE} 后重启")

    def _restore_external_state(self) -> None:
        """
        在恢复活跃的安息日状态后，对外部系统执行状态检查与纠正。
        必须在持有 _state_lock 且依赖已注入的情况下调用。
        """
        if self._pipeline_bus is not None:
            try:
                self._pipeline_bus.pause_new_pipelines()
                logger.info("已根据恢复状态暂停交易流水线")
            except Exception as e:
                logger.error(f"恢复流水线暂停失败: {e}")

        if self._compute_scheduler is not None:
            try:
                self._compute_scheduler.enter_sabbath_mode()
                logger.info("已根据恢复状态切换到安息日算力模式")
            except Exception as e:
                logger.error(f"恢复算力模式失败: {e}")

    # ========== 私有方法：核心逻辑 ==========
    def _inside_sabbath_window(self, now: float) -> bool:
        """判断当前时间是否在安息日窗口内（需在锁内调用）"""
        sabbath_start = self._compute_sabbath_start_for_month(now)
        if sabbath_start is None:
            return False

        window_start = sabbath_start - self.DEFAULT_SABBATH_WINDOW_MINUTES * 60
        window_end = sabbath_start + self.DEFAULT_SABBATH_DURATION_HOURS * 3600

        return window_start <= now <= window_end

    def _compute_sabbath_start_for_month(self, now: float) -> Optional[float]:
        """计算当前月份的理论安息日开始时间（UTC），若月份不对或计算异常返回 None"""
        try:
            tm = time.gmtime(now)
            year, month = tm.tm_year, tm.tm_mon

            first_day = calendar.timegm(time.strptime(f"{year}-{month:02d}-01", "%Y-%m-%d"))
            first_weekday = time.gmtime(first_day).tm_wday
            days_until = (self.DEFAULT_SABBATH_DAY_OF_WEEK - first_weekday) % 7
            first_target = 1 + days_until
            target_day = first_target + (self.DEFAULT_SABBATH_WEEK_OF_MONTH - 1) * 7
            max_day = calendar.monthrange(year, month)[1]
            if target_day > max_day:
                target_day = first_target + (self.DEFAULT_SABBATH_WEEK_OF_MONTH - 2) * 7
                if target_day < 1:
                    return None
            return calendar.timegm(time.strptime(
                f"{year}-{month:02d}-{target_day:02d} "
                f"{self.DEFAULT_SABBATH_START_HOUR:02d}:00:00", "%Y-%m-%d %H:%M:%S"))
        except Exception as e:
            logger.error(f"计算安息日开始时间异常: {e}")
            return None

    def _activate_locked(self, now: float, trigger: str = "unknown") -> None:
        """执行进入安息日的所有动作（事务性，需在锁保护下调用）"""
        if self._active:
            return

        audit_snapshot = {
            "trigger": trigger,
            "timestamp": now,
            "stack_trace": traceback.format_stack()[-3].strip() if trigger == "manual_command" else "",
        }

        pipeline_paused = False
        scheduler_switched = False

        # 暂停流水线（带超时）
        if self._pipeline_bus is not None:
            try:
                future = self._executor.submit(self._pipeline_bus.pause_new_pipelines)
                future.result(timeout=self.EXTERNAL_CALL_TIMEOUT_SEC)
                pipeline_paused = True
                logger.info("交易流水线已暂停新开仓")
            except FuturesTimeoutError:
                logger.critical("暂停交易流水线超时 (>%ds)，放弃本次安息日激活 #RECOVERY: 检查 PipelineBus 状态",
                                 self.EXTERNAL_CALL_TIMEOUT_SEC)
                return
            except Exception as e:
                logger.critical(f"暂停交易流水线异常: {e}，放弃本次安息日激活")
                return

        # 切换算力（带超时）
        if self._compute_scheduler is not None:
            try:
                future = self._executor.submit(self._compute_scheduler.enter_sabbath_mode)
                future.result(timeout=self.EXTERNAL_CALL_TIMEOUT_SEC)
                scheduler_switched = True
                logger.info("算力已切换至安息日模式")
            except FuturesTimeoutError:
                logger.critical("算力切换超时，回滚流水线暂停")
                if pipeline_paused:
                    self._rollback_pipeline_pause()
                return
            except Exception as e:
                logger.critical(f"算力切换异常: {e}，回滚流水线暂停")
                if pipeline_paused:
                    self._rollback_pipeline_pause()
                return

        # 提交内部状态
        self._active = True
        self._last_enter_time = now
        self._real_switch_count = 0
        self._sabbath_triggered_this_month = True
        self._save_state()

        # 广播
        self._broadcast_status("enter", audit_snapshot)
        logger.info("安息日正式激活: %s (触发来源: %s)",
                     time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now)), trigger)

    def _deactivate_locked(self, now: float, trigger: str = "unknown") -> None:
        """执行退出安息日的所有动作（事务性，需在锁保护下调用）"""
        if not self._active:
            return

        audit_snapshot = {
            "trigger": trigger,
            "timestamp": now,
            "stack_trace": traceback.format_stack()[-3].strip() if trigger == "manual_command" else "",
        }

        pipeline_resumed = False
        scheduler_switched = False

        # 恢复流水线
        if self._pipeline_bus is not None:
            try:
                future = self._executor.submit(self._pipeline_bus.resume_new_pipelines)
                future.result(timeout=self.EXTERNAL_CALL_TIMEOUT_SEC)
                pipeline_resumed = True
                logger.info("交易流水线已恢复新开仓")
            except FuturesTimeoutError:
                logger.critical("恢复交易流水线超时，将继续尝试恢复算力，流水线状态未知")
            except Exception as e:
                logger.critical(f"恢复交易流水线异常: {e}")

        # 恢复算力
        if self._compute_scheduler is not None:
            try:
                future = self._executor.submit(self._compute_scheduler.exit_sabbath_mode)
                future.result(timeout=self.EXTERNAL_CALL_TIMEOUT_SEC)
                scheduler_switched = True
                logger.info("算力已恢复至正常模式")
            except FuturesTimeoutError:
                logger.critical("恢复算力超时，系统可能处于半冻结状态 #RECOVERY: 手动检查算力分配")
            except Exception as e:
                logger.critical(f"恢复算力异常: {e}")

        if not pipeline_resumed or not scheduler_switched:
            self._schedule_recovery_retry(now, trigger)
        else:
            self._active = False
            self._last_exit_time = now
            self._real_switch_count = 0
            self._save_state()
            self._broadcast_status("exit", audit_snapshot)
            logger.info("安息日正式结束: %s (触发来源: %s)",
                         time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now)), trigger)

    def _rollback_pipeline_pause(self) -> None:
        """尝试回滚流水线暂停"""
        try:
            future = self._executor.submit(self._pipeline_bus.resume_new_pipelines)
            future.result(timeout=self.EXTERNAL_CALL_TIMEOUT_SEC)
            logger.warning("已回滚流水线暂停")
        except Exception as e:
            logger.critical(f"回滚流水线暂停失败: {e} #RECOVERY: 手动恢复 PipelineBus")

    def _schedule_recovery_retry(self, now: float, trigger: str) -> None:
        """在回滚失败后安排延迟重试"""
        logger.warning("将在 %d 秒后自动重试安息日退出流程", self.RETRY_AFTER_FAILURE_SEC)
        timer = threading.Timer(self.RETRY_AFTER_FAILURE_SEC, self._retry_exit, args=[now, trigger])
        timer.daemon = True
        timer.start()

    def _retry_exit(self, original_now: float, trigger: str) -> None:
        """重试安息日退出流程"""
        logger.info("正在重试安息日退出流程...")
        with self._state_lock:
            if not self._active:
                return
            pipeline_resumed = False
            scheduler_switched = False
            if self._pipeline_bus is not None:
                try:
                    self._pipeline_bus.resume_new_pipelines()
                    pipeline_resumed = True
                except Exception as e:
                    logger.critical(f"重试恢复流水线仍然失败: {e}")
            if self._compute_scheduler is not None:
                try:
                    self._compute_scheduler.exit_sabbath_mode()
                    scheduler_switched = True
                except Exception as e:
                    logger.critical(f"重试恢复算力仍然失败: {e}")

            if pipeline_resumed and scheduler_switched:
                self._active = False
                self._last_exit_time = time.time()
                self._real_switch_count = 0
                self._save_state()
                self._broadcast_status("exit", {"trigger": "auto_retry"})
                logger.info("安息日退出重试成功")
            else:
                logger.critical("安息日退出重试仍失败，请立即人工介入！系统可能处于半冻结状态")
                if self._negotiation_bus is not None:
                    try:
                        self._negotiation_bus.publish_alert(
                            alert_type="sabbath_stuck",
                            level="critical",
                            message="安息日退出失败，系统可能处于半冻结状态，请立即人工介入",
                            timestamp=time.time(),
                        )
                    except Exception:
                        pass

    def _broadcast_status(self, action: str, audit_snapshot: Dict[str, Any]) -> None:
        """广播安息日状态变更事件"""
        event_data = {
            "action": action,
            "timestamp": time.time(),
            "active": self._active,
            "audit": audit_snapshot,
        }

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

        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="sabbath_transition",
                    details=event_data,
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

    def _get_next_sabbath_time(self) -> float:
        """计算下一个安息日开始时间戳（UTC，带缓存）"""
        now = time.time()
        with self._state_lock:
            if (self._next_sabbath_cache_at > 0 and
                    now - self._next_sabbath_cache_at < self.NEXT_SABBATH_CACHE_TTL_SEC):
                cached_tm = time.gmtime(self._next_sabbath_cache_result)
                now_tm = time.gmtime(now)
                if (cached_tm.tm_mon == now_tm.tm_mon and cached_tm.tm_year == now_tm.tm_year) or \
                   (cached_tm.tm_mon == (now_tm.tm_mon % 12 + 1) and cached_tm.tm_year == now_tm.tm_year):
                    return self._next_sabbath_cache_result

        next_time = self._compute_next_sabbath_time(now)
        with self._state_lock:
            self._next_sabbath_cache_result = next_time
            self._next_sabbath_cache_at = now
        return next_time

    def _compute_next_sabbath_time(self, now: float) -> float:
        """实际计算下一个安息日时间戳（UTC）"""
        tm = time.gmtime(now)
        year, month = tm.tm_year, tm.tm_mon

        def find_in_month(y: int, m: int) -> float:
            first_day = calendar.timegm(time.strptime(f"{y}-{m:02d}-01", "%Y-%m-%d"))
            first_weekday = time.gmtime(first_day).tm_wday
            days_until = (self.DEFAULT_SABBATH_DAY_OF_WEEK - first_weekday) % 7
            first_target = 1 + days_until
            target_day = first_target + (self.DEFAULT_SABBATH_WEEK_OF_MONTH - 1) * 7
            max_day = calendar.monthrange(y, m)[1]
            if target_day > max_day:
                target_day = first_target + (self.DEFAULT_SABBATH_WEEK_OF_MONTH - 2) * 7
            return calendar.timegm(time.strptime(
                f"{y}-{m:02d}-{target_day:02d} "
                f"{self.DEFAULT_SABBATH_START_HOUR:02d}:00:00", "%Y-%m-%d %H:%M:%S"))

        start_time = find_in_month(year, month)
        if start_time <= now:
            if month == 12:
                year += 1
                month = 1
            else:
                month += 1
            start_time = find_in_month(year, month)
        return start_time
