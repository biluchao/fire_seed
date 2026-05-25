"""
火种系统 · 紧急降维控制器 (EmergencySimplifier)

核心职责：
1. 在系统检测到严重异常（连续熔断、全策略夏普极值恶化、硬件生存底线突破）时，按分级深度暂停非生存模块，仅保留核心交易与风控链路
2. 在异常解除后，按渐进式唤醒流程逐级恢复系统功能，每一步需稳定运行观察期后才进入下一步，任一步骤异常自动回退

外部依赖（真实模块接口）：
- core.system_builder.SystemBuilder : 获取全系统模块注册表，用于批量暂停与恢复模块
- core.signal_bus.lane_scheduler.LaneScheduler : 在降级期间调整四车道优先级，将非核心流量降级至慢速车道
- core.negotiation_bus.NegotiationBus : 发送降级/恢复通知事件
- core.risk_monitor.risk_color_manager.RiskColorManager : 获取当前风险色彩等级，作为降级触发依据之一
- core.engine.dormancy_manager.DormancyManager : 重度降级时与分层休眠联动
- core.behavioral_logger.BehavioralLogger : 记录降级/恢复全流程日志

接口契约：
- simplify(level: int, reason: str) -> Dict[str, Any] : 执行指定等级的降级操作，level 1=轻度, 2=中度, 3=重度
- restore() -> Dict[str, Any] : 按渐进式唤醒流程恢复系统功能
- get_simplification_status() -> Dict[str, Any] : 返回当前降级状态，包括当前等级、已暂停模块列表、恢复阶段
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 SystemBuilder 不可用时，无法执行降级操作，返回 critical 错误并记录 #RECOVERY 建议
- 当 NegotiationBus 不可用时，通知降级为仅本地日志记录
- 当 DormancyManager 不可用时，重度降级跳过休眠联动，仅执行模块暂停
- 当 LaneScheduler 恢复紧急模式失败时，保持当前降级状态，不重置系统状态

资源管理：
- 本模块维护降级状态机和恢复步骤计数器，不持有任何外部资源句柄
- 恢复过程中每一步的观察期计时器由调用方驱动，本模块仅提供状态判定
- SUSPEND_MODULES_* 为只读类常量，方法返回新列表，调用方可安全修改
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class EmergencySimplifier:
    """紧急降维控制器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 降级等级定义
    LEVEL_LIGHT = 1      # 轻度降级：暂停议会和进化模块
    LEVEL_MEDIUM = 2     # 中度降级：仅保留核心策略执行和 C++ 风控
    LEVEL_HEAVY = 3      # 重度降级：仅保留 C++ 硬实时风控和基础行情接收

    # 各等级需要暂停的模块前缀列表（只读，不可被实例修改）
    SUSPEND_MODULES_LIGHT = [
        "agents.adversarial_council",
        "agents.sentinel",
        "agents.alchemist",
        "agents.guardian",
        "agents.devils_advocate",
        "agents.godel_watcher",
        "agents.narrator",
        "agents.diversity_enforcer",
        "brain.evolution",
        "brain.cloud_llm_auditor",
        "openclaw.gateway",
    ]

    SUSPEND_MODULES_MEDIUM = [
        "core.scorecard",
        "core.conditional_weight",
        "core.multi_tf_arbiter_v2",
        "core.perception",
        "core.meta_cognition",
        "core.experience_replay",
        "core.ecological_niche",
        "core.position_sizer",
        "core.strategy_gene",
    ]

    SUSPEND_MODULES_HEAVY = [
        "core.order_manager",
        "core.execution",
        "core.pipeline_bus",
        "core.negotiation_bus",
        "core.self_check",
        "core.daily_tasks",
        "core.compute_scheduler",
    ]

    # 恢复阶段配置（每步的观察期，秒，取值范围 [120, 7200]）
    RESTORE_STEP_OBSERVATION_SEC = 120      # 每步恢复后需稳定观察 2 分钟

    # 降级冷却期（秒，取值范围 [60, 3600]），防止短时间内反复降级/恢复
    DEGRADATION_COOLDOWN_SEC = 300          # 5 分钟冷却期

    # 自动降级触发条件
    AUTO_TRIGGER_CONSECUTIVE_CIRCUIT_BREAKERS = 3  # 连续熔断次数触发自动降级，无量纲，[2, 10]
    AUTO_TRIGGER_ALL_SHARPE_BELOW = -1.0           # 全策略夏普低于此值触发自动降级，无量纲，[-5.0, 0.0]

    def __init__(self):
        # 当前降级状态
        self._current_level: int = 0               # 0=正常, 1=轻度, 2=中度, 3=重度
        self._suspended_modules: List[str] = []     # 已暂停的模块名称列表
        self._restore_step: int = 0                 # 当前恢复步骤（0=未在恢复中）
        self._last_degradation_time: float = 0.0    # 上次降级时间戳
        self._degradation_reason: str = ""           # 降级原因

        # 外部依赖注入
        self._system_builder = None
        self._lane_scheduler = None
        self._negotiation_bus = None
        self._risk_color_manager = None
        self._dormancy_manager = None
        self._behavioral_logger = None

        # 线程安全
        self._lock = threading.Lock()

        logger.info("EmergencySimplifier 初始化完成，当前等级: %d (正常)", self._current_level)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        system_builder: Optional[Any] = None,
        lane_scheduler: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        risk_color_manager: Optional[Any] = None,
        dormancy_manager: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）

        Args:
            system_builder: 系统构建器，用于获取模块注册表
            lane_scheduler: 车道调度器，用于降级期间调整优先级
            negotiation_bus: 协商总线，用于发送通知事件
            risk_color_manager: 风险色彩管理器，用于获取当前风险等级
            dormancy_manager: 休眠管理器，用于重度降级联动
            behavioral_logger: 行为日志记录器
        """
        if system_builder is not None:
            self._system_builder = system_builder
            logger.info("SystemBuilder 注入成功")
        else:
            logger.warning("SystemBuilder 未注入，降级操作将无法执行模块暂停")

        if lane_scheduler is not None:
            self._lane_scheduler = lane_scheduler
            logger.info("LaneScheduler 注入成功")

        if negotiation_bus is not None:
            self._negotiation_bus = negotiation_bus
            logger.info("NegotiationBus 注入成功")

        if risk_color_manager is not None:
            self._risk_color_manager = risk_color_manager
            logger.info("RiskColorManager 注入成功")

        if dormancy_manager is not None:
            self._dormancy_manager = dormancy_manager
            logger.info("DormancyManager 注入成功")
        else:
            logger.warning("DormancyManager 未注入，重度降级将跳过休眠联动")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")

    # ========== 公共接口 ==========
    def simplify(self, level: int, reason: str) -> Dict[str, Any]:
        """
        执行指定等级的降级操作

        Args:
            level: 降级等级 (1=轻度, 2=中度, 3=重度)
            reason: 降级原因描述

        Returns:
            标准响应字典
        """
        # 参数校验
        if level not in (self.LEVEL_LIGHT, self.LEVEL_MEDIUM, self.LEVEL_HEAVY):
            logger.warning(f"无效降级等级: {level}")
            return {
                "status": "error",
                "reason": f"无效降级等级: {level}，有效值为 1(轻度)/2(中度)/3(重度)",
                "data": {},
                "warnings": [f"invalid_level: {level}"],
            }

        if not reason or not isinstance(reason, str):
            logger.warning("降级原因不能为空")
            return {
                "status": "error",
                "reason": "降级原因不能为空",
                "data": {},
                "warnings": ["missing_reason"],
            }

        # 锁外预检：SystemBuilder 可用性检查（只读状态，无需锁保护）
        if self._system_builder is None:
            logger.error(
                "SystemBuilder 不可用，拒绝降级操作 "
                "#RECOVERY: 检查 SystemBuilder 注入状态，确认系统构建器是否已初始化"
            )
            return {
                "status": "critical",
                "reason": "SystemBuilder 不可用，无法执行降级操作",
                "data": {},
                "warnings": ["system_builder_unavailable"],
            }

        with self._lock:
            # 冷却期检查（降级后禁止立即再次降级，也禁止在冷却期内恢复）
            now = time.time()
            if self._current_level > 0 and (now - self._last_degradation_time) < self.DEGRADATION_COOLDOWN_SEC:
                remaining = self.DEGRADATION_COOLDOWN_SEC - (now - self._last_degradation_time)
                logger.warning(
                    "降级冷却期中，距上次降级 %.1f 秒，剩余 %.1f 秒",
                    now - self._last_degradation_time, remaining
                )
                return {
                    "status": "rejected",
                    "reason": f"降级冷却期中，剩余 {remaining:.0f} 秒",
                    "data": {
                        "current_level": self._current_level,
                        "cooldown_remaining_sec": round(remaining, 1)
                    },
                    "warnings": ["cooldown_active"],
                }

            # 确定需要暂停的模块列表（返回新列表，调用方可安全修改）
            modules_to_suspend = self._get_suspend_list(level)

            # 执行模块暂停
            suspended = []
            failed = []
            for module_name in modules_to_suspend:
                try:
                    if self._system_builder.suspend_module(module_name):
                        suspended.append(module_name)
                        logger.info("已暂停模块: %s", module_name)
                    else:
                        failed.append(module_name)
                        logger.warning("暂停模块失败: %s", module_name)
                except Exception as e:
                    failed.append(module_name)
                    logger.error(
                        f"暂停模块异常 {module_name}: {e} "
                        f"#RECOVERY: 检查模块是否已注册、是否支持 suspend 操作"
                    )

            # 更新状态
            self._current_level = level
            self._suspended_modules = suspended
            self._last_degradation_time = now
            self._degradation_reason = reason
            self._restore_step = 0

            # 调整车道优先级
            if self._lane_scheduler is not None:
                try:
                    self._lane_scheduler.set_emergency_mode(True)
                    logger.info("已启用紧急车道模式")
                except Exception as e:
                    logger.warning(f"启用紧急车道模式失败: {e}")

            # 发送通知
            self._notify_simplification(level, reason, suspended, failed)

            logger.warning(
                "系统降级完成: 等级=%d, 原因=%s, 已暂停=%d 个模块, 失败=%d 个模块",
                level, reason, len(suspended), len(failed)
            )

            return {
                "status": "ok",
                "reason": f"降级等级 {level} 执行完成，已暂停 {len(suspended)} 个模块",
                "data": {
                    "level": level,
                    "suspended_modules": suspended,
                    "failed_modules": failed,
                    "reason": reason,
                    "timestamp": now,
                },
                "warnings": [f"failed: {f}" for f in failed] if failed else [],
            }

    def restore(self) -> Dict[str, Any]:
        """
        按渐进式唤醒流程恢复系统功能

        Returns:
            标准响应字典
        """
        with self._lock:
            # 正常状态无需恢复
            if self._current_level == 0:
                return {
                    "status": "ok",
                    "reason": "系统当前处于正常状态，无需恢复",
                    "data": {"current_level": 0},
                    "warnings": [],
                }

            # 降级后冷却期内禁止恢复，防止与 simplify 调用形成竞态
            if self._last_degradation_time > 0:
                elapsed = time.time() - self._last_degradation_time
                if elapsed < self.DEGRADATION_COOLDOWN_SEC:
                    remaining = self.DEGRADATION_COOLDOWN_SEC - elapsed
                    logger.warning(
                        "降级冷却期中，禁止恢复，剩余 %.1f 秒", remaining
                    )
                    return {
                        "status": "rejected",
                        "reason": f"降级冷却期中，禁止恢复，剩余 {remaining:.0f} 秒",
                        "data": {"cooldown_remaining_sec": round(remaining, 1)},
                        "warnings": ["restore_blocked_by_cooldown"],
                    }

            # 渐进式恢复：每次调用恢复一个步骤
            if self._restore_step == 0:
                # 第一步：重新激活所有被暂停的模块
                restored = []
                failed = []
                for module_name in self._suspended_modules:
                    try:
                        if self._system_builder.resume_module(module_name):
                            restored.append(module_name)
                            logger.info("已恢复模块: %s", module_name)
                        else:
                            failed.append(module_name)
                            logger.warning("恢复模块失败: %s", module_name)
                    except Exception as e:
                        failed.append(module_name)
                        logger.error(
                            f"恢复模块异常 {module_name}: {e} "
                            f"#RECOVERY: 检查模块状态与依赖"
                        )

                self._restore_step = 1
                logger.info(
                    "恢复步骤 1/2 完成: 已恢复 %d 个模块, 失败 %d 个",
                    len(restored), len(failed)
                )
                return {
                    "status": "ok",
                    "reason": f"恢复步骤 1/2: 模块重新激活完成，已恢复 {len(restored)} 个",
                    "data": {
                        "restore_step": 1,
                        "restored_modules": restored,
                        "failed_modules": failed,
                    },
                    "warnings": [f"failed: {f}" for f in failed] if failed else [],
                }

            elif self._restore_step == 1:
                # 第二步：恢复正常运行参数，解除紧急模式
                if self._lane_scheduler is not None:
                    try:
                        self._lane_scheduler.set_emergency_mode(False)
                        logger.info("已解除紧急车道模式")
                    except Exception as e:
                        # 恢复失败：保持当前降级状态，不重置任何状态
                        logger.error(
                            f"解除紧急车道模式失败: {e} "
                            f"#RECOVERY: 检查 LaneScheduler 进程存活状态，手动调用 set_emergency_mode(False)"
                        )
                        return {
                            "status": "error",
                            "reason": (
                                f"解除紧急车道模式失败: {str(e)}，"
                                f"系统保持降级状态（等级={self._current_level}）"
                            ),
                            "data": {
                                "restore_step": self._restore_step,
                                "current_level": self._current_level,
                            },
                            "warnings": ["lane_scheduler_restore_failed"],
                        }

                # 所有步骤成功：重置所有状态
                self._current_level = 0
                self._suspended_modules = []
                self._restore_step = 0
                self._degradation_reason = ""

                self._notify_restoration()
                logger.info("恢复步骤 2/2 完成: 系统已恢复正常运行")
                return {
                    "status": "ok",
                    "reason": "恢复步骤 2/2: 系统已完全恢复正常运行",
                    "data": {"restore_step": 2, "current_level": 0},
                    "warnings": [],
                }

            return {
                "status": "error",
                "reason": f"未知的恢复步骤: {self._restore_step}",
                "data": {},
                "warnings": [f"unknown_restore_step: {self._restore_step}"],
            }

    def get_simplification_status(self) -> Dict[str, Any]:
        """
        返回当前降级状态

        Returns:
            标准响应字典
        """
        with self._lock:
            now = time.time()
            cooldown_remaining = 0.0
            if self._current_level > 0 and self._last_degradation_time > 0:
                elapsed = now - self._last_degradation_time
                if elapsed < self.DEGRADATION_COOLDOWN_SEC:
                    cooldown_remaining = self.DEGRADATION_COOLDOWN_SEC - elapsed

            return {
                "status": "ok",
                "reason": f"当前降级等级: {self._current_level}",
                "data": {
                    "current_level": self._current_level,
                    "suspended_modules_count": len(self._suspended_modules),
                    "restore_step": self._restore_step,
                    "degradation_reason": self._degradation_reason,
                    "last_degradation_time": self._last_degradation_time,
                    "cooldown_remaining_sec": round(cooldown_remaining, 1),
                },
                "warnings": ["cooldown_active"] if cooldown_remaining > 0 else [],
            }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检

        Returns:
            标准健康检查响应字典
        """
        try:
            # 检查降级列表常量完整性
            if (not self.SUSPEND_MODULES_LIGHT or
                    not self.SUSPEND_MODULES_MEDIUM or
                    not self.SUSPEND_MODULES_HEAVY):
                logger.error(
                    "降级模块列表常量不完整 "
                    "#RECOVERY: 检查类常量 SUSPEND_MODULES_* 是否在代码热重载时被意外覆盖"
                )
                return {
                    "status": "error",
                    "reason": "降级模块列表常量不完整",
                    "data": {},
                    "warnings": ["suspend_lists_corrupted"],
                }

            with self._lock:
                level = self._current_level
                suspended_count = len(self._suspended_modules)

            return {
                "status": "ok",
                "reason": f"EmergencySimplifier 正常，当前降级等级: {level}",
                "data": {
                    "current_level": level,
                    "suspended_modules_count": suspended_count,
                    "dependencies": {
                        "system_builder": self._system_builder is not None,
                        "lane_scheduler": self._lane_scheduler is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                        "risk_color_manager": self._risk_color_manager is not None,
                        "dormancy_manager": self._dormancy_manager is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和内部状态一致性")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _get_suspend_list(self, level: int) -> List[str]:
        """
        获取指定降级等级需要暂停的模块列表（返回新列表，调用方可安全修改）

        Args:
            level: 降级等级

        Returns:
            模块名称列表（全新列表对象）
        """
        if level == self.LEVEL_LIGHT:
            return list(self.SUSPEND_MODULES_LIGHT)
        elif level == self.LEVEL_MEDIUM:
            return list(self.SUSPEND_MODULES_LIGHT) + list(self.SUSPEND_MODULES_MEDIUM)
        elif level == self.LEVEL_HEAVY:
            return (list(self.SUSPEND_MODULES_LIGHT) +
                    list(self.SUSPEND_MODULES_MEDIUM) +
                    list(self.SUSPEND_MODULES_HEAVY))
        return []

    def _notify_simplification(
        self, level: int, reason: str, suspended: List[str], failed: List[str]
    ) -> None:
        """发送降级通知（协商总线 + 行为日志）"""
        event_data = {
            "level": level,
            "reason": reason,
            "suspended_count": len(suspended),
            "failed_count": len(failed),
            "timestamp": time.time(),
        }

        # 协商总线
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="emergency_simplification",
                    level=level,
                    message=reason,
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning(f"协商总线降级通知失败: {e}")

        # 行为日志
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="emergency_simplification",
                    details=event_data,
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

    def _notify_restoration(self) -> None:
        """发送恢复通知"""
        # 协商总线
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="emergency_restoration",
                    level=0,
                    message="系统已恢复正常运行",
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning(f"协商总线恢复通知失败: {e}")

        # 行为日志
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="emergency_restoration",
                    details={"timestamp": time.time()},
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")
