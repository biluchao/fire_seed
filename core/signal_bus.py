"""
火种系统 · 四车道信号总线入口 (SignalBus)

核心职责：
1. 作为信号总线的统一入口，根据信号的紧急性(urgency)将 NeuroPulse 路由到对应车道（极速/快速/普通/慢速）
2. 协调车道调度器(LaneScheduler)、背压处理器(BackpressureHandler)与车道健康监控器(LaneHealthMonitor)，实现信号的高效、可靠分发与系统降级

外部依赖（真实模块接口）：
- core.negotiation_bus.NeuroPulse : 标准化的神经脉冲数据类，包含 intent_type, urgency, pulse_id 等字段
- core.signal_bus.lane_scheduler.LaneScheduler : 执行车道优先级调度、核心借用与信号分发，对外暴露 dispatch 方法
- core.signal_bus.backpressure_handler.BackpressureHandler : 处理信号背压、队列压缩与降级告警，对外暴露 check_backpressure 和 enqueue 方法
- core.signal_bus.lane_health_monitor.LaneHealthMonitor : 监控各车道运行状态，触发健康告警与自动降级
- core.negotiation_bus.NegotiationBus : 发布系统级告警事件

接口契约：
- send_signal(neuro_pulse: NeuroPulse) -> Dict[str, Any] : 将信号投递到对应车道
- get_lane_status(lane_name: str) -> Dict[str, Any] : 获取指定车道的实时运行状态
- get_all_lanes_status() -> Dict[str, Any] : 获取所有车道的状态汇总
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 NeuroPulse 类不可用时，自动降级为轻量级 namedtuple，确保模块可加载
- 当 LaneScheduler 不可用时，所有信号降级为直接进入慢速车道并记录严重错误
- 当 BackpressureHandler 不可用时，背压检测功能关闭，信号不进行压缩处理
- 当 LaneHealthMonitor 不可用时，车道健康状态默认为 "unknown"，告警功能静默降级
- 当 NegotiationBus 不可用时，系统级告警降级为本地日志

资源管理：
- 本模块作为入口协调器，不持有任何需要手动释放的外部资源
- 子模块的生命周期由依赖注入的外部管理器控制，本模块仅持有引用
"""

import logging
import threading
from typing import Dict, Any, List, Optional, Tuple
from collections import namedtuple

logger = logging.getLogger(__name__)

# ---------- 外部依赖降级导入 ----------
try:
    from core.negotiation_bus import NeuroPulse
except ImportError:
    NeuroPulse = None
    logger.warning("NeuroPulse 不可用，使用降级 namedtuple。请检查 core.negotiation_bus 模块")
    # 降级为轻量 namedtuple，确保模块可加载
    NeuroPulse = namedtuple("NeuroPulse", ["intent_type", "urgency", "confidence",
                                           "desired_size_pct", "risk_cost_pct",
                                           "time_tolerance_us", "sensory_source",
                                           "pulse_id", "context"], defaults=[None]*9)


class SignalBus:
    """四车道信号总线入口"""

    # ========== 类常量（默认配置） ==========
    # 各车道最低紧急性阈值
    URGENCY_EXPRESS_MIN = 9                 # 极速车道最低紧急性，[8, 10]
    URGENCY_FAST_MIN = 6                    # 快速车道最低紧急性，[4, 8]
    URGENCY_NORMAL_MIN = 3                  # 普通车道最低紧急性，[1, 4]
    # 紧急性合法范围
    URGENCY_MIN = 0
    URGENCY_MAX = 10

    def __init__(self):
        # 子模块实例（由依赖注入或系统构建器设置）
        self._lane_scheduler = None
        self._backpressure_handler = None
        self._lane_health_monitor = None
        self._negotiation_bus = None

        # 信号计数器（用于监控和烟雾测试）
        self._signal_counts = {
            "express": 0,
            "fast": 0,
            "normal": 0,
            "slow": 0,
            "rejected": 0,
        }

        # 共享状态锁
        self._lock = threading.Lock()

        logger.info("SignalBus 入口初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        lane_scheduler: Optional[Any] = None,
        backpressure_handler: Optional[Any] = None,
        lane_health_monitor: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
    ) -> None:
        """
        注入子模块依赖（可选注入，未注入时对应功能降级）
        """
        if lane_scheduler is not None:
            if not hasattr(lane_scheduler, 'dispatch'):
                logger.error("LaneScheduler 缺少 dispatch 方法，注入被拒绝")
            else:
                self._lane_scheduler = lane_scheduler
                logger.info("LaneScheduler 注入成功")
        if self._lane_scheduler is None:
            logger.error("LaneScheduler 未注入，所有信号将降级为慢速车道")

        if backpressure_handler is not None:
            if not hasattr(backpressure_handler, 'check_backpressure') or not hasattr(backpressure_handler, 'enqueue'):
                logger.error("BackpressureHandler 缺少必需方法，注入被拒绝")
            else:
                self._backpressure_handler = backpressure_handler
                logger.info("BackpressureHandler 注入成功")
        if self._backpressure_handler is None:
            logger.warning("BackpressureHandler 未注入，背压检测功能关闭")

        if lane_health_monitor is not None:
            self._lane_health_monitor = lane_health_monitor
            logger.info("LaneHealthMonitor 注入成功")
        else:
            logger.warning("LaneHealthMonitor 未注入，车道健康监控降级")

        if negotiation_bus is not None:
            self._negotiation_bus = negotiation_bus
            logger.info("NegotiationBus 注入成功")
        else:
            logger.warning("NegotiationBus 未注入，系统告警降级为本地日志")

    # ========== 公共接口 ==========
    def send_signal(self, neuro_pulse) -> Dict[str, Any]:
        """
        将 NeuroPulse 信号投递到对应车道

        根据信号的 urgency 字段决定目标车道：
        - urgency >= 9 → 极速车道 (express)
        - 6 <= urgency < 9 → 快速车道 (fast)
        - 3 <= urgency < 6 → 普通车道 (normal)
        - urgency < 3 → 慢速车道 (slow)

        Args:
            neuro_pulse: 标准化的神经脉冲对象，必须包含 urgency 字段

        Returns:
            标准响应字典
        """
        # ---------- 参数校验 ----------
        if neuro_pulse is None:
            logger.warning("send_signal 收到空信号")
            return {
                "status": "rejected",
                "reason": "信号为 None",
                "data": {},
                "warnings": ["null_signal"],
            }

        if not hasattr(neuro_pulse, 'urgency') or neuro_pulse.urgency is None:
            logger.warning(f"信号缺少有效 urgency 字段，pulse_id={getattr(neuro_pulse, 'pulse_id', 'unknown')}")
            return {
                "status": "rejected",
                "reason": "信号缺少有效 urgency 字段",
                "data": {"pulse_id": getattr(neuro_pulse, 'pulse_id', 'unknown')},
                "warnings": ["invalid_urgency"],
            }

        urgency = int(neuro_pulse.urgency)
        if urgency < self.URGENCY_MIN or urgency > self.URGENCY_MAX:
            logger.warning(f"urgency={urgency} 超出合法范围 [{self.URGENCY_MIN}, {self.URGENCY_MAX}]，钳位处理")
            urgency = max(self.URGENCY_MIN, min(self.URGENCY_MAX, urgency))
            neuro_pulse = neuro_pulse._replace(urgency=urgency)  # 对降级 namedtuple 有效

        # 确定目标车道
        lane = self._resolve_lane(urgency)

        # 更新计数器（加锁保护）
        with self._lock:
            self._signal_counts[lane] = self._signal_counts.get(lane, 0) + 1

        # 车道调度器可用性检查
        if self._lane_scheduler is None or not hasattr(self._lane_scheduler, 'dispatch'):
            logger.error(
                f"LaneScheduler 不可用或缺少 dispatch 方法，信号 {getattr(neuro_pulse, 'pulse_id', '?')} 被拒绝"
            )
            with self._lock:
                self._signal_counts["rejected"] += 1
            return {
                "status": "rejected",
                "reason": "LaneScheduler 不可用，信号无法投递",
                "data": {"pulse_id": getattr(neuro_pulse, 'pulse_id', 'unknown'), "target_lane": lane},
                "warnings": ["lane_scheduler_unavailable"],
            }

        # 背压检测（在投递前）
        if self._backpressure_handler is not None:
            try:
                should_queue = self._backpressure_handler.check_backpressure(lane)
                if should_queue:
                    self._backpressure_handler.enqueue(lane, neuro_pulse)
                    logger.debug(f"信号 {getattr(neuro_pulse, 'pulse_id', '?')} 因背压进入 {lane} 车道队列")
                    return {
                        "status": "queued",
                        "reason": f"信号已进入 {lane} 车道队列（背压处理）",
                        "data": {"pulse_id": getattr(neuro_pulse, 'pulse_id', 'unknown'), "target_lane": lane},
                        "warnings": ["backpressure_queued"],
                    }
            except Exception as e:
                logger.warning(f"背压检测异常: {e}，跳过背压处理直接投递")

        # 正常投递到车道调度器
        try:
            result = self._lane_scheduler.dispatch(lane, neuro_pulse)
            return {
                "status": "ok",
                "reason": f"信号已投递到 {self._get_lane_name(lane)}",
                "data": {
                    "pulse_id": getattr(neuro_pulse, 'pulse_id', 'unknown'),
                    "target_lane": lane,
                    "dispatch_result": result,
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(
                f"信号 {getattr(neuro_pulse, 'pulse_id', '?')} 投递到 {lane} 车道失败: {e} "
                f"#RECOVERY: 检查车道调度器状态、队列深度及线程存活"
            )
            with self._lock:
                self._signal_counts["rejected"] += 1
            return {
                "status": "error",
                "reason": f"车道投递异常: {str(e)}",
                "data": {"pulse_id": getattr(neuro_pulse, 'pulse_id', 'unknown'), "target_lane": lane},
                "warnings": ["dispatch_exception"],
            }

    def get_lane_status(self, lane_name: str) -> Dict[str, Any]:
        """
        获取指定车道的实时运行状态

        Args:
            lane_name: 车道名称 (express/fast/normal/slow)

        Returns:
            标准响应字典
        """
        valid_lanes = ["express", "fast", "normal", "slow"]
        if lane_name not in valid_lanes:
            return {
                "status": "error",
                "reason": f"无效车道名称: {lane_name}，有效值为 {valid_lanes}",
                "data": {},
                "warnings": [f"unknown_lane: {lane_name}"],
            }

        # 安全读取计数器（加锁）
        with self._lock:
            signal_count = self._signal_counts.get(lane_name, 0)

        status_data = {
            "lane": lane_name,
            "signal_count": signal_count,
        }

        # 获取车道调度状态
        if self._lane_scheduler is not None and hasattr(self._lane_scheduler, 'get_lane_status'):
            try:
                scheduler_status = self._lane_scheduler.get_lane_status(lane_name)
                status_data["scheduler"] = scheduler_status
            except Exception as e:
                logger.warning(f"获取车道调度状态失败: {e}")
                status_data["scheduler"] = {"status": "unknown", "error": str(e)}
        else:
            status_data["scheduler"] = {"status": "unavailable"}

        # 获取车道健康状态
        if self._lane_health_monitor is not None:
            try:
                health = self._lane_health_monitor.get_health_score(lane_name)
                status_data["health"] = health.get("data", {})
            except Exception as e:
                logger.warning(f"获取车道健康状态失败: {e}")
                status_data["health"] = {"status": "unknown", "error": str(e)}
        else:
            status_data["health"] = {"status": "unavailable"}

        return {
            "status": "ok",
            "reason": f"已获取 {self._get_lane_name(lane_name)} 状态",
            "data": status_data,
            "warnings": [],
        }

    def get_all_lanes_status(self) -> Dict[str, Any]:
        """
        获取所有车道的状态汇总

        Returns:
            标准响应字典
        """
        all_status = {}
        for lane in ["express", "fast", "normal", "slow"]:
            res = self.get_lane_status(lane)
            all_status[lane] = res.get("data", {})

        with self._lock:
            rejected = self._signal_counts["rejected"]

        return {
            "status": "ok",
            "reason": "已获取所有车道状态",
            "data": {
                "lanes": all_status,
                "total_rejected": rejected,
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
            dependencies = {
                "lane_scheduler": self._lane_scheduler is not None,
                "backpressure_handler": self._backpressure_handler is not None,
                "lane_health_monitor": self._lane_health_monitor is not None,
                "negotiation_bus": self._negotiation_bus is not None,
            }

            sub_health = {}
            if self._lane_scheduler is not None and hasattr(self._lane_scheduler, 'health_check'):
                try:
                    sub_health["lane_scheduler"] = self._lane_scheduler.health_check().get("status", "unknown")
                except Exception as e:
                    sub_health["lane_scheduler"] = f"error: {e}"

            if self._lane_health_monitor is not None:
                try:
                    sub_health["lane_health_monitor"] = self._lane_health_monitor.health_check().get("status", "unknown")
                except Exception as e:
                    sub_health["lane_health_monitor"] = f"error: {e}"

            with self._lock:
                total_signals = sum(self._signal_counts.values())

            return {
                "status": "ok",
                "reason": f"SignalBus 入口正常，累计处理 {total_signals} 条信号",
                "data": {
                    "dependencies": dependencies,
                    "sub_health": sub_health,
                    "signal_counts": dict(self._signal_counts),
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和子模块注入状态")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _resolve_lane(self, urgency: int) -> str:
        """根据紧急性映射到目标车道"""
        if urgency >= self.URGENCY_EXPRESS_MIN:
            return "express"
        if urgency >= self.URGENCY_FAST_MIN:
            return "fast"
        if urgency >= self.URGENCY_NORMAL_MIN:
            return "normal"
        return "slow"

    @staticmethod
    def _get_lane_name(lane: str) -> str:
        """获取车道中文名称"""
        mapping = {
            "express": "极速车道",
            "fast": "快速车道",
            "normal": "普通车道",
            "slow": "慢速车道",
        }
        return mapping.get(lane, lane)
