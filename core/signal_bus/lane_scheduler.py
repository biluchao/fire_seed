"""
火种系统 · 车道调度器 (LaneScheduler)

核心职责：
1. 根据 NeuroPulse 的 urgency 字段，将任务路由到四车道（极速/快速/普通/慢速）中的合适车道
2. 实现动态核心借用机制：极速车道空闲时允许快速车道临时借用其 CPU 核心，高优先级任务到达时立即归还

外部依赖（真实模块接口）：
- core.negotiation_bus.NegotiationBus : 发送核心借用状态变更事件与信号丢弃告警
- core.behavioral_logger.BehavioralLogger : 记录调度决策日志与异常事件

接口契约：
- dispatch(signal: Dict[str, Any]) -> Dict[str, Any] : 将信号路由到合适车道，返回调度结果
- get_lane_stats(lane_name: str) -> Dict[str, Any] : 返回指定车道的实时性能统计
- get_all_lane_stats() -> Dict[str, Any] : 返回所有车道的性能统计汇总
- borrow_express_core() -> Dict[str, Any] : 快速车道借用极速核心（需空闲判定）
- release_express_core() -> Dict[str, Any] : 释放借用的极速核心
- report_executed(lane_name: str, latency_us: float) -> Dict[str, Any] : 上报任务执行完成
- mark_express_task_start() -> Dict[str, Any] : 标记极速任务开始执行
- mark_express_task_done() -> Dict[str, Any] : 标记极速任务执行完成
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 NegotiationBus 不可用时，核心借用事件与信号丢弃告警降级为仅本地日志记录
- 当 BehavioralLogger 不可用时，日志降级为标准 logger
- 当车道队列满载时，低优先级信号自动降级至下一级车道或丢弃（附带告警）
- 所有降级值在类常量区明确声明

资源管理：
- 本模块使用 threading.Lock 保护共享统计数据结构
- 不持有任何需要手动释放的外部资源句柄
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional
from collections import deque
from enum import IntEnum

logger = logging.getLogger(__name__)


class LanePriority(IntEnum):
    """车道优先级枚举"""
    EXPRESS = 0
    FAST = 1
    NORMAL = 2
    SLOW = 3


class LaneScheduler:
    """四车道调度器，实现优先级路由与动态核心借用"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_EXPRESS_QUEUE_SIZE = 64        # 极速车道队列容量，无量纲，[32, 256]
    DEFAULT_FAST_QUEUE_SIZE = 256          # 快速车道队列容量，无量纲，[128, 1024]
    DEFAULT_NORMAL_QUEUE_SIZE = 1024       # 普通车道队列容量，无量纲，[512, 4096]
    DEFAULT_SLOW_QUEUE_SIZE = 4096         # 慢速车道队列容量，无量纲，[1024, 16384]
    DEFAULT_CORE_BORROW_TIMEOUT_US = 50    # 核心归还超时，微秒，[10, 500]
    DEFAULT_STATS_WINDOW = 300             # 统计窗口保留样本数，无量纲，[100, 1000]
    URGENCY_EXPRESS_MIN = 9                # 极速车道最低紧急度
    URGENCY_FAST_MIN = 6                   # 快速车道最低紧急度
    URGENCY_NORMAL_MIN = 3                 # 普通车道最低紧急度
    # urgency < 3 为慢速车道

    def __init__(self):
        # 四车道任务队列
        self._queues: Dict[str, deque] = {
            "express": deque(maxlen=self.DEFAULT_EXPRESS_QUEUE_SIZE),
            "fast": deque(maxlen=self.DEFAULT_FAST_QUEUE_SIZE),
            "normal": deque(maxlen=self.DEFAULT_NORMAL_QUEUE_SIZE),
            "slow": deque(maxlen=self.DEFAULT_SLOW_QUEUE_SIZE),
        }

        # 车道统计信息
        self._stats: Dict[str, Dict[str, Any]] = {
            lane: {
                "dispatched": 0,               # 已调度任务数
                "executed": 0,                 # 已执行任务数
                "dropped": 0,                  # 丢弃任务数
                "degraded_from": 0,            # 从该车道降级出去的任务数
                "queue_full_events": 0,        # 队列满载次数
                "latency_history": deque(maxlen=self.DEFAULT_STATS_WINDOW),
                "core_borrowed_by": None,       # 当前被哪个车道借用（仅对express有效）
                "core_borrowed_at": 0,         # 借用开始时间
            }
            for lane in ["express", "fast", "normal", "slow"]
        }

        # 极速车道正在执行的任务计数
        self._express_active_count = 0

        # 动态核心借用状态
        self._core_borrowed = False           # 极速核心是否被快速车道借用
        self._core_borrow_start = 0.0        # 借用开始时间戳
        self._core_borrower = None            # 借用者车道名

        # 外部依赖注入
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 线程安全
        self._lock = threading.Lock()

        logger.info("LaneScheduler 初始化完成，极速队列=%d, 快速队列=%d, 普通队列=%d, 慢速队列=%d",
                    self.DEFAULT_EXPRESS_QUEUE_SIZE, self.DEFAULT_FAST_QUEUE_SIZE,
                    self.DEFAULT_NORMAL_QUEUE_SIZE, self.DEFAULT_SLOW_QUEUE_SIZE)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）
        """
        if negotiation_bus is not None:
            missing = []
            if not hasattr(negotiation_bus, 'publish_alert'):
                missing.append('publish_alert')
            if not hasattr(negotiation_bus, 'publish_status'):
                missing.append('publish_status')
            if missing:
                logger.warning("NegotiationBus 缺少方法: %s，告警推送不可用", ', '.join(missing))
                self._negotiation_bus = None
            else:
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")
        else:
            logger.warning("NegotiationBus 未注入，核心借用事件与告警降级为本地日志")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

    # ========== 公共接口 ==========
    def dispatch(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        """
        根据信号紧急度将任务路由到合适车道

        Args:
            signal: 标准化的 NeuroPulse 信号字典，必须包含 urgency (int)

        Returns:
            标准响应字典
        """
        urgency = signal.get("urgency", 0)
        if not isinstance(urgency, (int, float)) or urgency < 0 or urgency > 10:
            logger.warning(f"无效 urgency: {urgency}，降级至慢速车道")
            target_lane = "slow"
        elif urgency >= self.URGENCY_EXPRESS_MIN:
            target_lane = "express"
        elif urgency >= self.URGENCY_FAST_MIN:
            target_lane = "fast"
        elif urgency >= self.URGENCY_NORMAL_MIN:
            target_lane = "normal"
        else:
            target_lane = "slow"

        # 为信号附加调度元数据
        signal["_dispatched_at"] = time.time()
        signal["_target_lane"] = target_lane

        with self._lock:
            queue = self._queues[target_lane]
            if len(queue) >= queue.maxlen:
                # 队列满，尝试降级
                degraded = self._degrade_signal(signal, target_lane)
                self._stats[target_lane]["queue_full_events"] += 1
                logger.warning(
                    f"{target_lane} 队列已满，信号降级至 {degraded} #RECOVERY: "
                    f"检查下游消费速度或扩大队列容量"
                )
                return {
                    "status": "degraded",
                    "reason": f"{target_lane} 队列已满，信号降级至 {degraded}",
                    "data": {"lane": degraded, "urgency": urgency,
                             "degradation_chain": signal.get("_degradation_chain", [])},
                    "warnings": [f"{target_lane}_queue_full"],
                }

            queue.append(signal)
            self._stats[target_lane]["dispatched"] += 1

        logger.debug(f"信号已调度至 {target_lane} 车道, urgency={urgency}")
        return {
            "status": "ok",
            "reason": f"信号已路由至 {target_lane} 车道",
            "data": {"lane": target_lane, "urgency": urgency},
            "warnings": [],
        }

    def get_lane_stats(self, lane_name: str) -> Dict[str, Any]:
        """
        获取指定车道的实时性能统计

        Args:
            lane_name: 车道名称 (express/fast/normal/slow)

        Returns:
            标准响应字典
        """
        valid_lanes = ["express", "fast", "normal", "slow"]
        if lane_name not in valid_lanes:
            logger.warning(f"无效车道名称: {lane_name}")
            return {
                "status": "error",
                "reason": f"无效车道名称: {lane_name}，有效值为 {valid_lanes}",
                "data": {},
                "warnings": [f"未知车道: {lane_name}"],
            }

        with self._lock:
            stats = self._stats[lane_name].copy()
            stats["queue_depth"] = len(self._queues[lane_name])
            stats["queue_capacity"] = self._queues[lane_name].maxlen
            stats["queue_usage_pct"] = round(
                len(self._queues[lane_name]) / max(1, self._queues[lane_name].maxlen) * 100, 1
            )
            stats["core_borrowed"] = self._core_borrowed and self._core_borrower == lane_name
            if self._core_borrowed:
                stats["core_borrow_duration_ms"] = round(
                    (time.time() - self._core_borrow_start) * 1000, 1
                )

        return {
            "status": "ok",
            "reason": f"已获取 {lane_name} 车道统计",
            "data": stats,
            "warnings": [],
        }

    def get_all_lane_stats(self) -> Dict[str, Any]:
        """
        获取所有车道的性能统计汇总

        Returns:
            标准响应字典
        """
        all_stats = {}
        for lane in ["express", "fast", "normal", "slow"]:
            res = self.get_lane_stats(lane)
            if res["status"] == "ok":
                all_stats[lane] = res["data"]
            else:
                all_stats[lane] = {"error": res["reason"]}

        return {
            "status": "ok",
            "reason": "已获取所有车道统计汇总",
            "data": {"lanes": all_stats, "core_borrowed": self._core_borrowed},
            "warnings": [],
        }

    def borrow_express_core(self) -> Dict[str, Any]:
        """
        快速车道请求借用极速车道的 CPU 核心
        仅当极速车道空闲（队列为空、无正在执行的任务）且核心未被借用时生效

        Returns:
            标准响应字典
        """
        with self._lock:
            if self._core_borrowed:
                return {
                    "status": "rejected",
                    "reason": "极速核心已被借用",
                    "data": {},
                    "warnings": ["core_already_borrowed"],
                }

            if len(self._queues["express"]) > 0 or self._express_active_count > 0:
                return {
                    "status": "rejected",
                    "reason": "极速车道非空闲",
                    "data": {
                        "express_queue_depth": len(self._queues["express"]),
                        "express_active_count": self._express_active_count,
                    },
                    "warnings": ["express_not_idle"],
                }

            # 执行借用
            self._core_borrowed = True
            self._core_borrow_start = time.time()
            self._core_borrower = "fast"
            self._stats["express"]["core_borrowed_by"] = "fast"
            self._stats["express"]["core_borrowed_at"] = self._core_borrow_start

        logger.info("快速车道已借用极速核心")
        self._notify_core_status("borrowed", "fast")

        return {
            "status": "ok",
            "reason": "快速车道已成功借用极速核心",
            "data": {"borrowed_at": self._core_borrow_start},
            "warnings": [],
        }

    def release_express_core(self) -> Dict[str, Any]:
        """
        释放被借用的极速核心（由借用方主动调用或由调度器在极速任务到达时强制调用）

        Returns:
            标准响应字典
        """
        with self._lock:
            if not self._core_borrowed:
                return {
                    "status": "ok",
                    "reason": "极速核心未被借用，无需释放",
                    "data": {},
                    "warnings": [],
                }

            borrower = self._core_borrower
            borrow_duration = time.time() - self._core_borrow_start
            self._core_borrowed = False
            self._core_borrow_start = 0.0
            self._core_borrower = None
            self._stats["express"]["core_borrowed_by"] = None
            self._stats["express"]["core_borrowed_at"] = 0

        logger.info(f"极速核心已从 {borrower} 释放，借用时长: {borrow_duration*1000:.1f}ms")
        self._notify_core_status("released", borrower)

        return {
            "status": "ok",
            "reason": f"极速核心已从 {borrower} 释放",
            "data": {"borrow_duration_ms": round(borrow_duration * 1000, 1)},
            "warnings": [],
        }

    def report_executed(self, lane_name: str, latency_us: float) -> Dict[str, Any]:
        """
        下游执行模块上报任务执行完成，更新统计

        Args:
            lane_name: 车道名称
            latency_us: 执行延迟（微秒）

        Returns:
            标准响应字典
        """
        if lane_name not in self._stats:
            logger.warning(f"无效车道名称: {lane_name}")
            return {
                "status": "error",
                "reason": f"无效车道名称: {lane_name}",
                "data": {},
                "warnings": [f"未知车道: {lane_name}"],
            }

        # 值域校验
        if not isinstance(latency_us, (int, float)) or latency_us < 0 or latency_us > 10_000_000:
            logger.warning(f"无效 latency_us: {latency_us}，使用默认值 1000μs")
            latency_us = 1000.0

        with self._lock:
            self._stats[lane_name]["executed"] += 1
            self._stats[lane_name]["latency_history"].append(latency_us)

        return {
            "status": "ok",
            "reason": f"已记录 {lane_name} 车道执行完成",
            "data": {"latency_us": latency_us},
            "warnings": [],
        }

    def mark_express_task_start(self) -> Dict[str, Any]:
        """下游执行模块通知：极速任务开始执行"""
        with self._lock:
            self._express_active_count += 1
        return {"status": "ok", "reason": "极速任务计数+1", "data": {}, "warnings": []}

    def mark_express_task_done(self) -> Dict[str, Any]:
        """下游执行模块通知：极速任务执行完成"""
        with self._lock:
            self._express_active_count = max(0, self._express_active_count - 1)
        return {"status": "ok", "reason": "极速任务计数-1", "data": {}, "warnings": []}

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检

        Returns:
            标准健康检查响应字典
        """
        try:
            if not hasattr(self, '_queues') or not self._queues:
                return {
                    "status": "degraded",
                    "reason": "任务队列未初始化",
                    "data": {},
                    "warnings": ["queues_not_initialized"],
                }

            with self._lock:
                total_queued = sum(len(q) for q in self._queues.values())
                total_dispatched = sum(s["dispatched"] for s in self._stats.values())
                total_dropped = sum(s["dropped"] for s in self._stats.values())
                core_borrow_duration_ms = 0.0
                if self._core_borrowed:
                    core_borrow_duration_ms = (time.time() - self._core_borrow_start) * 1000

            return {
                "status": "ok",
                "reason": f"LaneScheduler 正常，总队列深度: {total_queued}, 已调度: {total_dispatched}",
                "data": {
                    "total_queued": total_queued,
                    "total_dispatched": total_dispatched,
                    "total_dropped": total_dropped,
                    "core_borrowed": self._core_borrowed,
                    "core_borrow_duration_ms": round(core_borrow_duration_ms, 1),
                    "dependencies": {
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和队列数据结构完整性")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _degrade_signal(self, signal: Dict[str, Any], current_lane: str) -> str:
        """
        信号降级处理：队列满载时，按优先级降级至下一级车道
        若已是慢速车道，则丢弃信号

        Args:
            signal: 待降级的信号
            current_lane: 当前车道

        Returns:
            降级后的目标车道名（若丢弃则返回 "dropped"）
        """
        degradation_chain = {
            "express": "fast",
            "fast": "normal",
            "normal": "slow",
        }

        target = degradation_chain.get(current_lane)
        if target is None:
            # 已是慢速车道，丢弃
            self._stats[current_lane]["dropped"] += 1
            pulse_id = signal.get('pulse_id', 'unknown')
            logger.error(
                f"信号已丢弃: {pulse_id} #RECOVERY: 检查慢速车道消费线程是否阻塞"
            )
            self._notify_signal_dropped(pulse_id, current_lane)
            return "dropped"

        # 记录降级链路
        chain = signal.get("_degradation_chain", [])
        chain.append({"from": current_lane, "to": target, "reason": "queue_full"})
        signal["_degradation_chain"] = chain
        self._stats[current_lane]["degraded_from"] += 1

        # 检查目标车道是否也满载
        target_queue = self._queues[target]
        if len(target_queue) >= target_queue.maxlen:
            return self._degrade_signal(signal, target)

        target_queue.append(signal)
        self._stats[target]["dispatched"] += 1
        return target

    def _notify_core_status(self, status: str, lane: str) -> None:
        """通知协商总线核心借用状态变更"""
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_status'):
            try:
                self._negotiation_bus.publish_status(
                    status_type="core_borrow",
                    lane=lane,
                    status=status,
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning(f"协商总线状态推送失败: {e}")

        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="core_borrow",
                    details={"lane": lane, "status": status},
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

    def _notify_signal_dropped(self, pulse_id: str, source_lane: str) -> None:
        """通知协商总线信号丢弃事件"""
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="signal_dropped",
                    pulse_id=pulse_id,
                    source_lane=source_lane,
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning(f"协商总线告警推送失败: {e}")

        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="signal_dropped",
                    details={"pulse_id": pulse_id, "source_lane": source_lane},
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")
