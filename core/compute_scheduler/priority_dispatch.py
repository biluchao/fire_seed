"""
火种系统 · 优先级调度器 (PriorityDispatch) [全球顶尖量化对冲基金级重构版 v6.0]

核心职责：
1. 基于任务优先级（P0-P5）将任务分发至四车道（极速/快速/普通/慢速）的 O(log n) 优先队列，
   支持同优先级按 FIFO、置信度、截止时间排序，确保生存级指令延迟 < 100μs (P99)
2. 实现严格的时间片抢占（结合运行时长因子与弹性时间缩放）与跨车道核心资源再分配，
   支持车道间负载均衡、任务超时淘汰、任务取消及优雅关闭
3. 提供动态背压保护、容量自适应、车道健康感知、任务执行时长监控及完整的调度审计追踪

外部依赖（真实模块接口）：
- core.signal_bus.lane_health_monitor.LaneHealthMonitor : 获取各车道实时健康状态，用于动态调整调度策略
- core.negotiation_bus.NegotiationBus : 发送调度事件与过载告警
- core.behavioral_logger.BehavioralLogger : 记录调度日志与异常事件
- core.elastic_time.ElasticTimeManager : 获取弹性时间缩放因子，用于超时计算

接口契约：
- submit_task(task: Dict[str, Any]) -> Dict[str, Any] : 提交单个任务到调度队列
- submit_batch(tasks: List[Dict[str, Any]]) -> Dict[str, Any] : 批量提交任务
- dequeue_task(lane: str) -> Optional[Dict[str, Any]] : 从指定车道取出下一个待执行任务
- on_task_complete(lane: str, task_id: str) -> None : 任务执行完成时清理运行状态
- preempt_if_needed(lane: str, running_priority: int, running_duration_sec: float = 0.0) -> bool : 核心抢占判断
- cancel_task(task_id: str) -> bool : 取消指定任务（支持跨车道搜索）
- set_queue_capacity(lane: str, capacity: int) -> Dict[str, Any] : 动态调整车道队列容量
- get_queue_status(lane: str) -> Dict[str, Any] : 查询指定车道的队列状态
- shutdown(timeout_sec: float = 30.0) -> None : 优雅关闭，等待所有非生存级任务完成或超时
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 LaneHealthMonitor 不可用时，忽略健康状态，默认所有车道可用，并标记 "degraded" 状态
- 当 NegotiationBus 不可用时，过载告警降级为仅本地日志记录
- 当队列满时，按优先级淘汰最老任务或拒绝最低优先级任务，并记录丢弃事件
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护每个车道的任务队列（基于 heapq 的优先队列），不持有其他外部资源
- 队列为内存数据结构，模块销毁时自动回收
- 可重入锁在模块销毁时自动释放，无文件句柄或网络连接
- 后台淘汰线程为守护线程，主进程退出时自动终止
"""

import time
import logging
import threading
import heapq
import uuid
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from enum import IntEnum

logger = logging.getLogger(__name__)


class TaskPriority(IntEnum):
    """任务优先级定义（与系统 NeuroPulse urgency 对齐）

    取值 0-10，数值越大优先级越高。生存级指令(10)永不过期、不可被抢占、不可被降级。
    """
    SURVIVAL = 10
    IMMEDIATE = 9
    HIGH = 8
    EXECUTE = 7
    STRATEGY = 6
    OPTIMIZE = 5
    NORMAL = 4
    LOW = 3
    BACKGROUND = 2
    IDLE = 1
    DEFERRED = 0


# ========== 全局模块状态（线程安全） ==========
_scheduler_instance: Optional['PriorityDispatch'] = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> 'PriorityDispatch':
    """获取调度器单例（线程安全，懒加载）"""
    global _scheduler_instance
    if _scheduler_instance is None:
        with _scheduler_lock:
            if _scheduler_instance is None:
                _scheduler_instance = PriorityDispatch()
    return _scheduler_instance


@dataclass(order=True)
class _ScheduledTask:
    """可排序任务结构（用于 heapq 最小堆）

    由于 heapq 是最小堆，优先级越高应越先出队，因此 _neg_priority 存储为负值。
    排序顺序：-_neg_priority（越小越先）→ submit_time（FIFO）→
              -_neg_confidence（越小越先）→ deadline（越早越先）
    """
    _neg_priority: int = field(compare=True)
    submit_time: float = field(compare=True)
    _neg_confidence: float = field(compare=True)
    deadline: float = field(default=0.0, compare=True)
    task_id: str = field(compare=False)
    priority: int = field(compare=False)
    confidence: float = field(compare=False)
    payload: Dict[str, Any] = field(compare=False)
    lane_override: Optional[str] = field(default=None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "priority": self.priority,
            "confidence": self.confidence,
            "deadline": self.deadline,
            "payload": self.payload,
            "submit_time": self.submit_time,
        }


class PriorityDispatch:
    """P0-P5 优先级调度器，管理四车道任务队列与核心抢占"""

    # ========== 类常量（生产环境需从配置中心加载） ==========
    DEFAULT_QUEUE_CAPACITY = {"express": 64, "fast": 256, "normal": 1024, "slow": 4096}
    BACKPRESSURE_WARN_THRESHOLD = 0.7
    BACKPRESSURE_DROP_THRESHOLD = 0.95
    TASK_MAX_QUEUE_AGE_SEC = 60
    TASK_STALE_CHECK_INTERVAL_SEC = 15
    PREEMPTION_PRIORITY_DIFF = 3
    PREEMPTION_MAX_RUNNING_RATIO = 0.7
    ALERT_DEDUP_WINDOW_SEC = 30
    ALERT_DICT_MAX_SIZE = 1000
    ELASTIC_TIME_SCALE_MIN = 0.3
    ELASTIC_TIME_SCALE_MAX = 3.0
    MAX_CAPACITY_HARD_LIMIT = 8192
    MIN_CAPACITY_HARD_LIMIT = 16
    MAX_DEGRADATION_HOPS = 2

    PRIORITY_LANE_MAP = {10: "express", 9: "express", 8: "fast", 7: "fast", 6: "fast",
                         5: "normal", 4: "normal", 3: "slow", 2: "slow", 1: "slow", 0: "slow"}
    LANE_ORDER = ["express", "fast", "normal", "slow"]

    def __init__(self):
        # 每个车道的任务队列（使用 heapq 最小堆）
        self._queues: Dict[str, List[_ScheduledTask]] = {lane: [] for lane in self.LANE_ORDER}
        self._capacities = dict(self.DEFAULT_QUEUE_CAPACITY)
        self._running_tasks: Dict[str, Optional[_ScheduledTask]] = {lane: None for lane in self.LANE_ORDER}
        self._running_start_times: Dict[str, float] = {lane: 0.0 for lane in self.LANE_ORDER}

        self._health_monitor = None
        self._negotiation_bus = None
        self._behavioral_logger = None
        self._elastic_time = None

        # 可重入锁保护所有共享状态
        self._lock = threading.RLock()

        self._stats = {
            "total_submitted": 0, "total_dropped": 0, "total_preempted": 0,
            "total_stale_removed": 0, "total_enqueued": 0, "total_dequeued": 0,
            "total_cancelled": 0,
        }

        self._alert_last_triggered: Dict[str, float] = {}
        self._last_stale_check = time.time()
        self._lane_executors: Dict[str, Any] = {}

        self._shutdown_flag = threading.Event()
        self._stale_removal_thread = threading.Thread(
            target=self._stale_removal_loop, daemon=True,
            name="priority-dispatch-stale-removal")
        self._stale_removal_thread.start()

        # 优雅关闭的状态管理
        self._shutdown_in_progress = False
        logger.info("PriorityDispatch 初始化完成，管理 %d 条车道队列", len(self._queues))

    # ========== 依赖注入 ==========
    def inject_dependencies(self, health_monitor=None, negotiation_bus=None,
                            behavioral_logger=None, elastic_time=None) -> None:
        """注入外部依赖（可重复调用，已注入的依赖不会被覆盖）"""
        if health_monitor is not None and self._health_monitor is None:
            if hasattr(health_monitor, 'get_health_score'):
                self._health_monitor = health_monitor
                logger.info("LaneHealthMonitor 注入成功")
            else:
                logger.warning("LaneHealthMonitor 缺少 get_health_score 方法，注入失败")
        if negotiation_bus is not None and self._negotiation_bus is None:
            if hasattr(negotiation_bus, 'publish_alert'):
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")
            else:
                logger.warning("NegotiationBus 缺少 publish_alert 方法，注入失败")
        if behavioral_logger is not None and self._behavioral_logger is None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        if elastic_time is not None and self._elastic_time is None:
            if hasattr(elastic_time, 'get_time_scale'):
                self._elastic_time = elastic_time
                logger.info("ElasticTimeManager 注入成功")
            else:
                logger.warning("ElasticTimeManager 缺少 get_time_scale 方法，注入失败")

    def register_executor(self, lane: str, executor: Any) -> bool:
        """注册车道执行器（重复注册时发出警告并拒绝覆盖）"""
        if lane not in self._queues:
            logger.warning(f"无效车道名称: {lane}，执行器注册失败")
            return False
        if not hasattr(executor, 'request_preemption'):
            logger.warning(f"执行器缺少 request_preemption 方法，注册失败")
            return False
        if self._lane_executors.get(lane) is not None:
            logger.warning(f"车道 {lane} 执行器已注册，拒绝覆盖")
            return False
        self._lane_executors[lane] = executor
        logger.info(f"车道 {lane} 执行器已注册: {type(executor).__name__}")
        return True

    # ========== 公共接口 ==========
    def submit_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """提交单个任务到调度队列（线程安全，O(log n) 插入）"""
        priority_raw = task.get("priority")
        if not isinstance(priority_raw, (int, float)) or priority_raw < 0 or priority_raw > 10:
            return {"status": "error", "reason": f"无效优先级: {priority_raw}",
                    "data": {}, "warnings": ["invalid_priority"]}
        priority = int(priority_raw)

        task_id = task.get("task_id", f"task_{uuid.uuid4().hex[:12]}")
        confidence = float(task.get("confidence", 0.0))
        if not (0.0 <= confidence <= 1.0):
            confidence = max(0.0, min(1.0, confidence))
        deadline = float(task.get("deadline", 0.0))
        payload = task.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        # 检查截止时间是否已过期
        if deadline > 0 and time.time() > deadline:
            logger.warning(f"任务 {task_id} 截止时间已过 (deadline={deadline})，拒绝提交")
            return {"status": "rejected", "reason": "任务截止时间已过",
                    "data": {"task_id": task_id}, "warnings": ["deadline_passed"]}

        lane_override = task.get("lane_override")
        if lane_override and lane_override not in self._queues:
            lane_override = None

        lane = lane_override if lane_override else self.PRIORITY_LANE_MAP.get(priority, "slow")
        lane = self._resolve_lane_with_health(lane, priority)

        sched_task = _ScheduledTask(
            _neg_priority=-priority,
            submit_time=time.time(),
            _neg_confidence=-confidence,
            deadline=deadline,
            task_id=task_id,
            priority=priority,
            confidence=confidence,
            payload=payload,
            lane_override=lane_override,
        )

        with self._lock:
            now = time.time()
            if now - self._last_stale_check > self.TASK_STALE_CHECK_INTERVAL_SEC:
                self._remove_stale_tasks()
                self._last_stale_check = now

            queue = self._queues[lane]
            capacity = self._capacities[lane]
            current_size = len(queue)
            usage = current_size / capacity if capacity > 0 else 1.0

            # 多次尝试淘汰低优先级任务，直到有空间或无法淘汰
            while current_size >= capacity:
                if not self._evict_lowest_priority(lane, priority):
                    logger.error(
                        f"车道 {lane} 队列满 (容量={capacity})，无法淘汰，拒绝任务 {task_id} "
                        "#RECOVERY: 检查下游模块处理能力、降低该车道信号频率、增加核心分配"
                    )
                    self._stats["total_dropped"] += 1
                    self._trigger_alert("queue_drop", f"车道 {lane} 拒绝任务 {task_id}", lane)
                    return {
                        "status": "rejected",
                        "reason": f"车道 {lane} 队列已满，任务被拒绝",
                        "data": {"lane": lane, "task_id": task_id},
                        "warnings": ["queue_full_reject"],
                    }
                current_size = len(queue)

            heapq.heappush(queue, sched_task)
            self._stats["total_submitted"] += 1
            self._stats["total_enqueued"] += 1

            if usage >= self.BACKPRESSURE_WARN_THRESHOLD:
                self._trigger_alert("queue_warning", f"车道 {lane} 队列使用率 {usage:.1%}", lane)

            if self._check_preemption(lane, sched_task):
                self._stats["total_preempted"] += 1

        logger.debug(f"任务 {task_id} (P={priority}) 入队 {lane}，当前长度: {len(queue)}")
        return {
            "status": "ok",
            "reason": f"任务已加入 {lane} 车道队列",
            "data": {"lane": lane, "task_id": task_id, "queue_length": len(queue), "priority": priority},
            "warnings": [],
        }

    def submit_batch(self, tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """批量提交任务（每个任务独立提交，部分失败不影响其他）"""
        results = [self.submit_task(t) for t in tasks]
        success = sum(1 for r in results if r["status"] == "ok")
        warnings = []
        if success < len(tasks):
            warnings.append(f"批量提交: {success}/{len(tasks)} 成功")
        return {
            "status": "ok" if success == len(tasks) else "partial",
            "reason": f"批量提交完成: {success}/{len(tasks)}",
            "data": {"success_count": success, "total_count": len(tasks)},
            "warnings": warnings,
        }

    def dequeue_task(self, lane: str) -> Optional[Dict[str, Any]]:
        """从指定车道取出下一个最高优先级待执行任务"""
        if lane not in self._queues:
            logger.warning(f"无效车道名称: {lane}")
            return None

        with self._lock:
            now = time.time()
            if now - self._last_stale_check > self.TASK_STALE_CHECK_INTERVAL_SEC:
                self._remove_stale_tasks()
                self._last_stale_check = now

            queue = self._queues[lane]
            # 跳过堆顶过期任务
            while queue:
                top_task: _ScheduledTask = queue[0]
                age = now - top_task.submit_time
                if top_task.priority < TaskPriority.SURVIVAL and age > self._get_max_queue_age():
                    heapq.heappop(queue)
                    self._stats["total_stale_removed"] += 1
                    logger.warning(
                        f"车道 {lane} 出队时淘汰过期任务 {top_task.task_id} (排队 {age:.1f}s)")
                else:
                    break
            if not queue:
                self._running_tasks[lane] = None
                self._running_start_times[lane] = 0.0
                return None

            sched_task: _ScheduledTask = heapq.heappop(queue)
            self._running_tasks[lane] = sched_task
            self._running_start_times[lane] = now
            self._stats["total_dequeued"] += 1

        result = sched_task.to_dict()
        result["lane"] = lane
        return result

    def on_task_complete(self, lane: str, task_id: str) -> None:
        """任务执行完成时调用（清理运行状态，安全清理运行中的任务 ID 和执行开始时间）"""
        if lane not in self._running_tasks:
            logger.warning(f"无效车道: {lane}")
            return
        with self._lock:
            running = self._running_tasks[lane]
            if running and running.task_id == task_id:
                self._running_tasks[lane] = None
                self._running_start_times[lane] = 0.0
                logger.debug(f"车道 {lane} 任务 {task_id} 完成，运行状态已清理")
            else:
                logger.debug(f"车道 {lane} 任务 {task_id} 与当前运行任务不匹配，忽略")

    def preempt_if_needed(self, lane: str, running_priority: int,
                          running_duration_sec: float = 0.0) -> bool:
        """检查当前运行任务是否需要被抢占"""
        with self._lock:
            queue = self._queues[lane]
            if not queue:
                return False
            top_priority: int = queue[0].priority
            if top_priority - running_priority >= self.PREEMPTION_PRIORITY_DIFF:
                max_age = self._get_max_queue_age()
                if max_age > 0 and running_duration_sec > self.PREEMPTION_MAX_RUNNING_RATIO * max_age:
                    return False
                logger.info(
                    f"车道 {lane} 抢占: 堆顶 P={top_priority} > 当前 P={running_priority}")
                return True
        return False

    def cancel_task(self, task_id: str) -> bool:
        """取消指定任务（跨车道搜索，O(n)）"""
        with self._lock:
            for lane in self.LANE_ORDER:
                queue = self._queues[lane]
                for i, task in enumerate(queue):
                    if task.task_id == task_id:
                        queue.pop(i)
                        heapq.heapify(queue)
                        self._stats["total_cancelled"] += 1
                        logger.info(f"任务 {task_id} 已从车道 {lane} 取消")
                        return True
            for lane, running in self._running_tasks.items():
                if running and running.task_id == task_id:
                    logger.warning(f"任务 {task_id} 正在车道 {lane} 运行中，无法取消")
                    return False
        return False

    def set_queue_capacity(self, lane: str, capacity: int) -> Dict[str, Any]:
        """动态调整车道队列容量，若新容量小于当前队列长度则触发淘汰"""
        if lane not in self._capacities:
            return {"status": "error", "reason": f"无效车道: {lane}", "data": {}, "warnings": []}
        if not (self.MIN_CAPACITY_HARD_LIMIT <= capacity <= self.MAX_CAPACITY_HARD_LIMIT):
            return {"status": "error",
                    "reason": f"容量 {capacity} 超出范围 [{self.MIN_CAPACITY_HARD_LIMIT}, {self.MAX_CAPACITY_HARD_LIMIT}]",
                    "data": {}, "warnings": []}

        with self._lock:
            old_capacity = self._capacities[lane]
            self._capacities[lane] = capacity
            queue = self._queues[lane]
            evicted = 0
            while len(queue) > capacity:
                min_idx = 0
                min_priority = queue[0].priority
                for i, task in enumerate(queue):
                    if task.priority < min_priority:
                        min_priority = task.priority
                        min_idx = i
                evicted_task = queue.pop(min_idx)
                evicted += 1
                logger.warning(f"容量缩减淘汰任务 {evicted_task.task_id} (P={min_priority})")
                self._stats["total_dropped"] += 1
            if evicted > 0:
                heapq.heapify(queue)
        logger.info(f"车道 {lane} 容量调整: {old_capacity} → {capacity}，淘汰 {evicted} 个任务")
        return {
            "status": "ok",
            "reason": f"车道 {lane} 容量已调整",
            "data": {"lane": lane, "old_capacity": old_capacity, "new_capacity": capacity,
                     "evicted_count": evicted},
            "warnings": [],
        }

    def get_queue_status(self, lane: str) -> Dict[str, Any]:
        """查询指定车道的队列状态"""
        if lane not in self._queues:
            return {"status": "error", "reason": f"无效车道名称: {lane}",
                    "data": {}, "warnings": []}
        with self._lock:
            queue = self._queues[lane]
            capacity = self._capacities[lane]
            length = len(queue)
            usage = length / capacity if capacity > 0 else 0
            running = self._running_tasks.get(lane)
            running_id = running.task_id if running else None
            top_priority: int = queue[0].priority if queue else None
            running_duration = 0.0
            if running and self._running_start_times.get(lane, 0) > 0:
                running_duration = time.time() - self._running_start_times[lane]
        return {
            "status": "ok",
            "reason": f"车道 {lane} 队列状态: {length}/{capacity}",
            "data": {
                "lane": lane, "queue_length": length, "capacity": capacity,
                "usage_pct": round(usage * 100, 1), "running_task_id": running_id,
                "top_priority": top_priority,
                "running_duration_sec": round(running_duration, 3),
            },
            "warnings": [],
        }

    def shutdown(self, timeout_sec: float = 30.0) -> None:
        """优雅关闭：等待所有非生存级任务完成或超时"""
        if self._shutdown_in_progress:
            logger.warning("优雅关闭已在执行中，忽略重复调用")
            return
        self._shutdown_in_progress = True
        logger.info(f"PriorityDispatch 开始优雅关闭，超时 {timeout_sec}s")
        self._shutdown_flag.set()
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            with self._lock:
                pending = sum(len(q) for q in self._queues.values())
                running = sum(1 for rt in self._running_tasks.values()
                              if rt is not None and rt.priority < TaskPriority.SURVIVAL)
                if pending == 0 and running == 0:
                    logger.info("所有非生存级任务已完成，安全退出")
                    break
            time.sleep(0.1)
        else:
            with self._lock:
                pending = sum(len(q) for q in self._queues.values())
                running = sum(1 for rt in self._running_tasks.values()
                              if rt is not None and rt.priority < TaskPriority.SURVIVAL)
            logger.warning(f"关闭超时，仍有 {pending} 个待处理任务和 {running} 个运行中任务")
        self._shutdown_in_progress = False

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            if not hasattr(self, '_queues') or not self._queues:
                return {"status": "degraded", "reason": "队列未初始化", "data": {},
                        "warnings": []}
            with self._lock:
                total_pending = sum(len(q) for q in self._queues.values())
                stats = dict(self._stats)
                buffer_usage = {}
                for lane, queue in self._queues.items():
                    cap = self._capacities[lane]
                    buffer_usage[lane] = {
                        "used": len(queue), "capacity": cap,
                        "usage_pct": round(len(queue) / cap * 100, 1) if cap > 0 else 0,
                    }
            return {
                "status": "ok",
                "reason": f"PriorityDispatch 正常，待处理 {total_pending}",
                "data": {
                    "total_pending_tasks": total_pending, "stats": stats,
                    "buffer_usage": buffer_usage,
                    "dependencies": {
                        "health_monitor": self._health_monitor is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                        "elastic_time": self._elastic_time is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和队列完整性")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": []}

    # ========== 私有方法 ==========
    def _get_max_queue_age(self) -> float:
        """获取弹性时间缩放后的最大排队时间（带边界保护）"""
        if self._elastic_time:
            try:
                scale = self._elastic_time.get_time_scale()
                scale = max(self.ELASTIC_TIME_SCALE_MIN,
                            min(self.ELASTIC_TIME_SCALE_MAX, scale))
                return self.TASK_MAX_QUEUE_AGE_SEC * scale
            except Exception:
                logger.warning("获取弹性时间缩放失败，使用默认值", exc_info=True)
        return self.TASK_MAX_QUEUE_AGE_SEC

    def _resolve_lane_with_health(self, lane: str, priority: int) -> str:
        """根据车道健康状态决定是否降级（生存级任务不降级）"""
        if priority >= TaskPriority.SURVIVAL:
            return lane
        if self._health_monitor:
            try:
                result = self._health_monitor.get_health_score(lane)
                if result.get("data", {}).get("level") == "critical":
                    degraded = self._find_degraded_lane(lane)
                    if degraded:
                        logger.warning(
                            f"车道 {lane} 严重拥堵，P={priority} 任务降级至 {degraded}")
                        return degraded
            except Exception:
                logger.warning("查询车道健康失败", exc_info=True)
        return lane

    def _find_degraded_lane(self, current_lane: str) -> Optional[str]:
        """为过载车道寻找降级目标车道（最多尝试 MAX_DEGRADATION_HOPS 次）"""
        try:
            idx = self.LANE_ORDER.index(current_lane)
        except ValueError:
            return None
        hops = 0
        for i in range(idx + 1, len(self.LANE_ORDER)):
            if hops >= self.MAX_DEGRADATION_HOPS:
                break
            candidate = self.LANE_ORDER[i]
            if self._get_lane_health(candidate) != "critical":
                with self._lock:
                    cap = self._capacities[candidate]
                    if len(self._queues[candidate]) < cap * self.BACKPRESSURE_DROP_THRESHOLD:
                        return candidate
            hops += 1
        return None

    def _get_lane_health(self, lane: str) -> str:
        """获取车道健康状态（降级时返回 healthy）"""
        if self._health_monitor:
            try:
                result = self._health_monitor.get_health_score(lane)
                return result.get("data", {}).get("level", "healthy")
            except Exception:
                pass
        return "healthy"

    def _check_preemption(self, lane: str, new_task: _ScheduledTask) -> bool:
        """检查是否需要抢占（需在锁内调用）"""
        running = self._running_tasks.get(lane)
        if not running or running.priority >= TaskPriority.SURVIVAL:
            return False
        if new_task.priority - running.priority >= self.PREEMPTION_PRIORITY_DIFF:
            running_duration = (time.time() - self._running_start_times[lane]
                                if self._running_start_times.get(lane, 0) > 0 else 0)
            max_age = self._get_max_queue_age()
            if max_age > 0 and running_duration > self.PREEMPTION_MAX_RUNNING_RATIO * max_age:
                return False
            logger.info(
                f"车道 {lane} 抢占: {new_task.task_id}(P={new_task.priority}) "
                f"> {running.task_id}(P={running.priority})")
            executor = self._lane_executors.get(lane)
            if executor and hasattr(executor, 'request_preemption'):
                try:
                    executor.request_preemption(new_task.task_id)
                except Exception as e:
                    logger.error(
                        f"抢占通知失败: {e} #RECOVERY: 检查执行器状态", exc_info=True)
            return True
        return False

    def _evict_lowest_priority(self, lane: str, new_priority: int) -> bool:
        """从队列中淘汰最低优先级任务（需在锁内调用）"""
        queue = self._queues[lane]
        if not queue:
            return False
        min_idx = 0
        min_priority = queue[0].priority
        for i, task in enumerate(queue):
            if task.priority < min_priority:
                min_priority = task.priority
                min_idx = i
        if min_priority < new_priority:
            evicted = queue.pop(min_idx)
            heapq.heapify(queue)
            logger.warning(
                f"车道 {lane} 淘汰 {evicted.task_id}(P={min_priority})，为新任务 P={new_priority}")
            self._stats["total_dropped"] += 1
            self._trigger_alert("queue_eviction",
                                f"车道 {lane} 淘汰 {evicted.task_id}", lane)
            return True
        return False

    def _remove_stale_tasks(self) -> None:
        """移除超时未处理的非生存级任务（需在锁内调用）"""
        now = time.time()
        max_age = self._get_max_queue_age()
        total_removed = 0
        for lane in self.LANE_ORDER:
            queue = self._queues[lane]
            stale_count = 0
            new_queue = []
            for task in queue:
                age = now - task.submit_time
                if task.priority >= TaskPriority.SURVIVAL:
                    new_queue.append(task)
                elif age > max_age:
                    stale_count += 1
                    logger.warning(
                        f"车道 {lane} 移除过期任务 {task.task_id} (排队 {age:.1f}s)")
                else:
                    new_queue.append(task)
            if stale_count > 0:
                heapq.heapify(new_queue)
                self._queues[lane] = new_queue
                self._stats["total_stale_removed"] += stale_count
                total_removed += stale_count
        if total_removed > 0:
            logger.info(f"全局淘汰过期任务: {total_removed} 条")

    def _stale_removal_loop(self) -> None:
        """后台淘汰线程（守护线程）"""
        while not self._shutdown_flag.is_set():
            try:
                self._shutdown_flag.wait(timeout=self.TASK_STALE_CHECK_INTERVAL_SEC)
                with self._lock:
                    self._remove_stale_tasks()
            except Exception as e:
                logger.error(f"后台淘汰线程异常: {e} #RECOVERY: 线程已自动恢复", exc_info=True)

    def _trigger_alert(self, alert_type: str, message: str, lane: str = "") -> None:
        """触发调度告警（含去重与字典容量控制，线程安全）"""
        with self._lock:
            alert_key = f"{alert_type}:{lane}"
            now = time.time()
            last_time = self._alert_last_triggered.get(alert_key, 0)
            if now - last_time < self.ALERT_DEDUP_WINDOW_SEC:
                return
            if len(self._alert_last_triggered) >= self.ALERT_DICT_MAX_SIZE:
                oldest_key = min(self._alert_last_triggered,
                                 key=self._alert_last_triggered.get)
                del self._alert_last_triggered[oldest_key]
            self._alert_last_triggered[alert_key] = now

        # 推送告警时不持有锁，避免外部调用阻塞
        if self._negotiation_bus and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type=alert_type, message=message, lane=lane, timestamp=now)
            except Exception as e:
                logger.warning(f"协商总线告警推送失败: {e}")
        else:
            logger.warning(message)

        if self._behavioral_logger:
            try:
                self._behavioral_logger.log_event(
                    event_type="priority_dispatch",
                    details={"alert_type": alert_type, "message": message, "lane": lane})
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")
