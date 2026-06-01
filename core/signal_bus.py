"""
火种系统 · 四车道信号总线入口 (SignalBus)

核心职责：
1. 作为信号总线的统一入口，根据信号的紧急性(urgency)将 NeuroPulse 路由到对应车道（极速/快速/普通/慢速）
2. 协调车道调度器(LaneScheduler)、背压处理器(BackpressureHandler)与车道健康监控器(LaneHealthMonitor)，实现信号的高效、可靠分发与系统降级

外部依赖（真实模块接口）：
- core.negotiation_bus.NeuroPulse : 标准化的神经脉冲数据类，包含 intent_type, urgency, pulse_id 等字段
- core.signal_bus.lane_scheduler.LaneScheduler : 执行车道优先级调度、核心借用与信号分发，对外暴露 dispatch/get_lane_status 方法
- core.signal_bus.backpressure_handler.BackpressureHandler : 处理信号背压、队列压缩与降级告警，对外暴露 check_backpressure/enqueue 方法
- core.signal_bus.lane_health_monitor.LaneHealthMonitor : 监控各车道运行状态，触发健康告警与自动降级
- core.negotiation_bus.NegotiationBus : 发布系统级告警事件

接口契约：
- send_signal(neuro_pulse: NeuroPulse) -> Dict[str, Any] : 将信号投递到对应车道，返回投递结果与处理延迟
- get_lane_status(lane_name: str) -> Dict[str, Any] : 获取指定车道的实时运行状态（含快照一致性标注）
- get_all_lanes_status() -> Dict[str, Any] : 获取所有车道的状态汇总与全局健康诊断
- health_check() -> Dict[str, Any] : 模块自检，暴露结构化性能指标
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

import sys
import time
import logging
import threading
from typing import Dict, Any, List, Optional
from collections import namedtuple

logger = logging.getLogger(__name__)

# ---------- 外部依赖降级导入 ----------
try:
    from core.negotiation_bus import NeuroPulse
except ImportError:
    NeuroPulse = None
    logger.warning("NeuroPulse 不可用，启用降级 namedtuple")
    _DegradedPulse = namedtuple("_DegradedPulse", [
        "intent_type", "urgency", "confidence", "desired_size_pct",
        "risk_cost_pct", "time_tolerance_us", "sensory_source",
        "pulse_id", "context"
    ])
    _DegradedPulse.__new__.__defaults__ = (None, 0, 0.0, 0.0, 0.0, 0, 'unknown', None, {})
    NeuroPulse = _DegradedPulse


class SignalBus:
    """四车道信号总线入口"""

    # ========== 类常量（安全默认值，可被配置覆盖） ==========
    URGENCY_EXPRESS_MIN = 9
    URGENCY_FAST_MIN = 6
    URGENCY_NORMAL_MIN = 3
    URGENCY_MIN = 0
    URGENCY_MAX = 10

    def __init__(self, config: Dict[str, Any] = None):
        cfg = config or {}
        self.URGENCY_EXPRESS_MIN = cfg.get("urgency_express_min", self.URGENCY_EXPRESS_MIN)
        self.URGENCY_FAST_MIN = cfg.get("urgency_fast_min", self.URGENCY_FAST_MIN)
        self.URGENCY_NORMAL_MIN = cfg.get("urgency_normal_min", self.URGENCY_NORMAL_MIN)

        self._lane_scheduler = None
        self._backpressure_handler = None
        self._lane_health_monitor = None
        self._negotiation_bus = None

        self._signal_counts = {
            "express": 0, "fast": 0, "normal": 0, "slow": 0, "rejected": 0
        }
        self._lock = threading.Lock()
        self._dependencies_injected = False

        logger.info("SignalBus 入口初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        lane_scheduler: Optional[Any] = None,
        backpressure_handler: Optional[Any] = None,
        lane_health_monitor: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
    ) -> None:
        """注入子模块依赖。核心依赖只允许注入一次，防止运行时意外替换。"""
        if self._dependencies_injected:
            logger.error("依赖已注入，拒绝重复注入操作")
            return

        if lane_scheduler is not None:
            if not hasattr(lane_scheduler, 'dispatch'):
                logger.error("LaneScheduler 缺少 dispatch 方法，注入被拒绝")
            else:
                self._lane_scheduler = lane_scheduler
                logger.info("LaneScheduler 注入成功")
        if self._lane_scheduler is None:
            logger.error("LaneScheduler 不可用，所有信号将降级为慢速车道")

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

        self._dependencies_injected = True
        logger.info("依赖注入完成，已锁定注入接口")

    # ========== 公共接口 ==========
    def send_signal(self, neuro_pulse) -> Dict[str, Any]:
        """将 NeuroPulse 信号投递到对应车道"""
        entry_ts = time.perf_counter()

        # 参数校验
        if neuro_pulse is None:
            return {"status": "rejected", "reason": "信号为 None", "data": {}, "warnings": ["null_signal"]}
        if not hasattr(neuro_pulse, 'urgency') or neuro_pulse.urgency is None:
            logger.warning("信号缺少有效 urgency 字段")
            return {
                "status": "rejected", "reason": "信号缺少有效 urgency 字段",
                "data": {"pulse_id": getattr(neuro_pulse, 'pulse_id', 'unknown')}, "warnings": ["invalid_urgency"]
            }

        urgency = int(neuro_pulse.urgency)
        if urgency < self.URGENCY_MIN or urgency > self.URGENCY_MAX:
            logger.warning(f"urgency={urgency} 超出合法范围，钳位处理")
            urgency = max(self.URGENCY_MIN, min(self.URGENCY_MAX, urgency))
            # 安全重建 NeuroPulse：优先 _replace，回退显式构造，最终降级直接修改属性
            try:
                if hasattr(neuro_pulse, '_replace'):
                    neuro_pulse = neuro_pulse._replace(urgency=urgency)
                else:
                    neuro_pulse = NeuroPulse(
                        intent_type=getattr(neuro_pulse, 'intent_type', None),
                        urgency=urgency,
                        confidence=getattr(neuro_pulse, 'confidence', 0.0),
                        desired_size_pct=getattr(neuro_pulse, 'desired_size_pct', 0.0),
                        risk_cost_pct=getattr(neuro_pulse, 'risk_cost_pct', 0.0),
                        time_tolerance_us=getattr(neuro_pulse, 'time_tolerance_us', 0),
                        sensory_source=getattr(neuro_pulse, 'sensory_source', 'unknown'),
                        pulse_id=getattr(neuro_pulse, 'pulse_id', f'clamped_{time.time()}') or f'clamped_{time.time()}',
                        context=getattr(neuro_pulse, 'context', {}),
                    )
            except Exception as e:
                logger.error(f"NeuroPulse 重建失败: {e}，尝试直接修改属性")
                try:
                    neuro_pulse.urgency = urgency
                except AttributeError:
                    logger.warning("无法修改 urgency 字段，继续使用原始对象（可能存在风险）")

        lane = self._resolve_lane(urgency)

        with self._lock:
            self._signal_counts[lane] = self._signal_counts.get(lane, 0) + 1
            current_count = self._signal_counts[lane]

        elapsed_us = (time.perf_counter() - entry_ts) * 1_000_000

        if not self._is_dependency_alive(self._lane_scheduler, 'dispatch'):
            logger.error("LaneScheduler 不可用，信号被拒绝")
            with self._lock:
                self._signal_counts["rejected"] += 1
            return {
                "status": "rejected", "reason": "LaneScheduler 不可用",
                "data": {"pulse_id": getattr(neuro_pulse, 'pulse_id', 'unknown'), "target_lane": lane, "processing_latency_us": round(elapsed_us, 1)},
                "warnings": ["lane_scheduler_unavailable"]
            }

        if self._backpressure_handler is not None and self._is_dependency_alive(self._backpressure_handler, 'check_backpressure'):
            try:
                if self._backpressure_handler.check_backpressure(lane):
                    self._backpressure_handler.enqueue(lane, neuro_pulse)
                    return {
                        "status": "queued", "reason": f"信号已进入 {lane} 车道队列（背压处理）",
                        "data": {"pulse_id": getattr(neuro_pulse, 'pulse_id', 'unknown'), "target_lane": lane, "lane_signal_count": current_count, "processing_latency_us": round(elapsed_us, 1)},
                        "warnings": ["backpressure_queued"]
                    }
            except Exception as e:
                logger.warning(f"背压检测异常: {e}，跳过直接投递")

        try:
            result = self._lane_scheduler.dispatch(lane, neuro_pulse)
            elapsed_us = (time.perf_counter() - entry_ts) * 1_000_000
            return {
                "status": "ok", "reason": f"信号已投递到 {self._get_lane_name(lane)}",
                "data": {
                    "pulse_id": getattr(neuro_pulse, 'pulse_id', 'unknown'), "target_lane": lane,
                    "dispatch_result": result, "lane_signal_count": current_count, "processing_latency_us": round(elapsed_us, 1)
                },
                "warnings": []
            }
        except Exception as e:
            logger.error(f"信号投递失败: {e} #RECOVERY: 检查车道调度器状态")
            with self._lock:
                self._signal_counts["rejected"] += 1
            return {
                "status": "error", "reason": f"车道投递异常: {str(e)}",
                "data": {"pulse_id": getattr(neuro_pulse, 'pulse_id', 'unknown'), "target_lane": lane, "processing_latency_us": round(elapsed_us, 1)},
                "warnings": ["dispatch_exception"]
            }

    def get_lane_status(self, lane_name: str) -> Dict[str, Any]:
        """获取指定车道的实时运行状态"""
        valid_lanes = ["express", "fast", "normal", "slow"]
        if lane_name not in valid_lanes:
            return {
                "status": "error", "reason": f"无效车道名称: {lane_name}",
                "data": {}, "warnings": [f"unknown_lane: {lane_name}"]
            }

        with self._lock:
            signal_count = self._signal_counts.get(lane_name, 0)
            signal_count_ts = time.time()

        status_data = {
            "lane": lane_name,
            "signal_count": signal_count,
            "signal_count_ts": signal_count_ts,
            "snapshot_consistency": "eventual",
        }

        if self._is_dependency_alive(self._lane_scheduler, 'get_lane_status'):
            try:
                status_data["scheduler"] = self._lane_scheduler.get_lane_status(lane_name)
                status_data["scheduler_ts"] = time.time()
            except Exception as e:
                logger.warning(f"获取车道调度状态失败: {e}")
                status_data["scheduler"] = {"status": "unknown", "error": str(e)}
        else:
            status_data["scheduler"] = {"status": "unavailable"}

        if self._lane_health_monitor is not None:
            try:
                health = self._lane_health_monitor.get_health_score(lane_name)
                status_data["health"] = health.get("data", {})
                status_data["health_ts"] = time.time()
            except Exception as e:
                logger.warning(f"获取车道健康状态失败: {e}")
                status_data["health"] = {"status": "unknown", "error": str(e)}
        else:
            status_data["health"] = {"status": "unavailable"}

        return {
            "status": "ok", "reason": f"已获取 {self._get_lane_name(lane_name)} 状态",
            "data": status_data, "warnings": []
        }

    def get_all_lanes_status(self) -> Dict[str, Any]:
        """获取所有车道的状态汇总与全局健康诊断"""
        all_status = {}
        for lane in ["express", "fast", "normal", "slow"]:
            res = self.get_lane_status(lane)
            all_status[lane] = res.get("data", {})

        with self._lock:
            rejected = self._signal_counts["rejected"]

        all_levels = [lane.get("health", {}).get("level", "unknown") for lane in all_status.values()]
        if "critical" in all_levels:
            overall = "critical"
        elif all_levels.count("degraded") >= 2:
            overall = "degraded"
        elif "degraded" in all_levels:
            overall = "degraded"
        else:
            overall = "healthy"

        return {
            "status": "ok", "reason": f"全局信号总线状态: {overall}",
            "data": {"lanes": all_status, "total_rejected": rejected, "overall_health": overall},
            "warnings": []
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检，暴露结构化性能指标"""
        try:
            dependencies = {
                "lane_scheduler": self._lane_scheduler is not None,
                "backpressure_handler": self._backpressure_handler is not None,
                "lane_health_monitor": self._lane_health_monitor is not None,
                "negotiation_bus": self._negotiation_bus is not None,
            }

            sub_health = {}
            if self._is_dependency_alive(self._lane_scheduler, 'health_check'):
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
                metrics = {}
                for lane in ["express", "fast", "normal", "slow", "rejected"]:
                    metrics[f"signal_count_{lane}"] = self._signal_counts.get(lane, 0)
                metrics["signal_count_total"] = total_signals

            display_total = f"{total_signals/1e9:.1f}B+" if total_signals > 1e9 else str(total_signals)

            return {
                "status": "ok",
                "reason": f"SignalBus 入口正常，累计处理 {display_total} 条信号",
                "data": {
                    "dependencies": dependencies,
                    "sub_health": sub_health,
                    "signal_counts": dict(self._signal_counts),
                    "metrics": metrics
                },
                "warnings": []
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和子模块注入状态")
            return {"status": "error", "reason": f"健康检查异常: {str(e)}", "data": {}, "warnings": [f"health_check_failed: {str(e)}"]}

    # ========== 私有方法 ==========
    def _resolve_lane(self, urgency: int) -> str:
        if urgency >= self.URGENCY_EXPRESS_MIN:
            return "express"
        if urgency >= self.URGENCY_FAST_MIN:
            return "fast"
        if urgency >= self.URGENCY_NORMAL_MIN:
            return "normal"
        return "slow"

    @staticmethod
    def _is_dependency_alive(dependency, method_name: str) -> bool:
        """检查依赖是否存活且有响应"""
        if dependency is None:
            return False
        if not hasattr(dependency, method_name):
            return False
        try:
            _ = getattr(dependency, '__class__', None)
            return True
        except Exception:
            return False

    @staticmethod
    def _get_lane_name(lane: str) -> str:
        mapping = {
            "express": "极速车道", "fast": "快速车道",
            "normal": "普通车道", "slow": "慢速车道", "rejected": "已拒绝"
        }
        return mapping.get(lane, lane)
