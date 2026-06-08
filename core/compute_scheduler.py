"""
火种系统 · 弹性算力调度器 (ComputeScheduler)

核心职责：
1. 作为算力调度模块的统一入口，整合 P0-P5 优先级调度、市场活跃度自适应分配与安息日全量切换三大子模块
2. 对外提供标准化的资源分配、回收、查询接口，并内置分配超时自动回收、CPU 温度分级保护、孤儿分配重试与降级状态自愈机制

外部依赖（真实模块接口）：
- core.compute_scheduler.priority_dispatch.PriorityDispatch : 处理 P0-P5 六级优先级的核心抢占与队列管理
- core.compute_scheduler.regime_adaptive_allocator.RegimeAdaptiveAllocator : 根据市场波动率分位动态调节各任务算力占比
- core.negotiation_bus.NegotiationBus : 接收其他模块的资源申请请求，并返回分配结果
- core.behavioral_logger.BehavioralLogger : 记录资源调度关键决策与异常事件
- core.perception.tactile_cortex.TactileCortex : 获取当前市场波动率分位与市场状态
- core.self_check.SystemHealthMonitor : 获取硬件温度与降频状态

接口契约：
- allocate(task_type: str, urgency: int) -> Dict[str, Any] : 为指定类型的任务分配算力，返回配额与排队信息
- release(task_type: str, allocation_id: str) -> Dict[str, Any] : 释放已分配的算力资源
- query_allocation(allocation_id: str) -> Dict[str, Any] : 查询分配记录状态
- get_current_quota(task_type: str) -> Dict[str, Any] : 查询某类任务当前的可用算力配额
- get_all_quotas() -> Dict[str, Any] : 查询所有任务类型的配额分配详情（原子快照）
- set_market_regime(regime: str, volatility_pct: float) -> Dict[str, Any] : 更新当前市场状态
- force_reclaim(task_type: str, reason: str) -> Dict[str, Any] : 紧急回收指定类型任务的算力
- shutdown() -> None : 优雅关闭调度器，等待后台线程退出
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 PriorityDispatch 不可用时，所有任务默认分配最低保障配额，并禁止高优抢占
- 当 RegimeAdaptiveAllocator 不可用时，使用静态默认分配比例，并标记 "degraded" 状态
- 当 TactileCortex 不可用时，波动率分位固定为 50（中性），市场状态固定为 "normal"，但定期重试检测恢复
- 当 SystemHealthMonitor 不可用时，跳过温度保护，但记录 WARNING 日志
- 子模块 release 失败后，本地记录不删除，进入孤儿重试队列，重试上限后告警并强制丢弃
- 所有降级值在类常量区明确声明
- 运行时自动检测依赖恢复，降级状态可自愈

资源管理：
- 本模块不持有任何需要手动释放的外部资源
- 维护分配超时自动回收守护线程，防止配额泄漏
- 通过 atexit 注册 shutdown 确保线程优雅退出
- 孤儿分配定期重试，最多 3 次，之后标记为僵化并告警
"""

import atexit
import time
import logging
import threading
import uuid
from typing import Dict, Any, List, Optional, Tuple
from collections import deque

logger = logging.getLogger(__name__)


class ComputeScheduler:
    """弹性算力调度器（模块入口）"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 正常市场状态下的静态分配比例（自适应分配器不可用时的降级方案）
    DEFAULT_STATIC_QUOTAS = {
        "trading_core": 0.30,       # 交易核心：30% 硬保证
        "strategy_engine": 0.20,    # 策略引擎：20%
        "evolution": 0.15,          # 进化工厂：15%
        "backtest": 0.10,           # 回测任务：10%
        "inference": 0.08,          # 大模型推理：8%
        "auxiliary": 0.10,          # 辅助任务：10%
        "reserved": 0.07,           # 系统预留：7%
    }

    # 安息日特殊配额方案（进化与回测获得更多算力）
    SABBATH_STATIC_QUOTAS = {
        "trading_core": 0.05,       # 安息日交易核心降至最低
        "strategy_engine": 0.05,
        "evolution": 0.35,
        "backtest": 0.25,
        "inference": 0.10,
        "auxiliary": 0.10,
        "reserved": 0.10,
    }

    # 最低保障配额（不可被抢占），按市场状态区分
    MIN_GUARANTEED_QUOTA_NORMAL = {
        "trading_core": 0.15,       # 交易核心最低 15%，不可被抢占
        "strategy_engine": 0.10,
        "evolution": 0.05,
    }
    MIN_GUARANTEED_QUOTA_SABBATH = {
        "trading_core": 0.03,
        "strategy_engine": 0.03,
        "evolution": 0.10,
        "backtest": 0.05,
    }

    # 市场活跃度阈值（分位）
    HIGH_VOLATILITY_THRESHOLD = 70      # ≥70 视为高活跃，无量纲，[60, 90]
    LOW_VOLATILITY_THRESHOLD = 30       # ≤30 视为低活跃，无量纲，[10, 40]

    # 调度配置
    QUOTA_REFRESH_INTERVAL_SEC = 2.0    # 正常刷新间隔，秒，[1, 10]
    QUOTA_REFRESH_INTERVAL_SABBATH = 0.5 # 安息日刷新间隔，秒
    DEFAULT_WAIT_ESTIMATE_MS = 50       # 默认估计等待时间，毫秒
    DEGRADED_WAIT_PER_QUEUE_MS = 5      # 降级模式下每个排队位置的估算等待毫秒，[1, 20]
    MAX_QUEUE_DEPTH_PER_TASK = 1000     # 每类任务最大排队深度
    ALLOCATION_TIMEOUT_SEC = 300        # 分配超时自动回收，秒，[120, 600]
    RECLAIM_SWEEP_INTERVAL_SEC = 60     # 超时回收扫描间隔，秒，[30, 120]
    ORPHAN_RETRY_MAX_ATTEMPTS = 3       # 孤儿分配最大重试次数
    ALLOC_ID_COLLISION_MAX_RETRIES = 5  # 分配ID碰撞检测最大重试次数

    # CPU 温度保护
    CPU_TEMP_WARNING_THRESHOLD = 85     # 警告温度，摄氏度
    CPU_TEMP_CRITICAL_THRESHOLD = 95    # 临界温度，摄氏度
    CPU_THROTTLE_REDUCTION_WARNING = 0.85  # 警告级别缩减系数，[0.7, 0.95]，保留 85% 算力
    CPU_THROTTLE_REDUCTION_CRITICAL = 0.50 # 临界级别缩减系数，[0.3, 0.6]，保留 50% 算力

    # 有效的市场状态与任务类型
    VALID_REGIMES = {"normal", "active", "sabbath", "extreme", "defense"}
    VALID_TASK_TYPES = tuple(DEFAULT_STATIC_QUOTAS.keys())   # 用 tuple 保证顺序

    # 不可超时的任务类型（永不回收）
    PERMANENT_TASK_TYPES = {"trading_core", "strategy_engine"}

    # 感知模块调用超时
    TACTILE_CORTEX_TIMEOUT_SEC = 1.0    # TactileCortex 调用超时，秒

    def __init__(self):
        self._priority_dispatch = None
        self._regime_allocator = None
        self._negotiation_bus = None
        self._behavioral_logger = None
        self._tactile_cortex = None
        self._health_monitor = None

        self._current_volatility_pct: float = 50.0
        self._current_regime: str = "normal"
        self._last_quota_update: float = 0.0

        self._is_degraded: bool = False
        self._degradation_reasons: List[str] = []

        # 活跃分配记录
        self._active_allocations: Dict[str, Dict[str, Any]] = {}
        # 任务队列（存储 allocation_id，便于出队同步）
        self._task_queues: Dict[str, deque] = {
            t: deque(maxlen=self.MAX_QUEUE_DEPTH_PER_TASK) for t in self.VALID_TASK_TYPES
        }
        # 孤儿分配（子模块释放失败，等待重试）
        self._orphan_allocations: Dict[str, Dict[str, Any]] = {}

        # 关闭标志
        self._shutdown_flag = False

        # 并发锁
        self._lock = threading.RLock()

        # 回收线程
        self._reclaim_stop_event = threading.Event()
        self._reclaim_thread = threading.Thread(
            target=self._timeout_reclaim_loop,
            daemon=True,
            name="compute-scheduler-reclaim"
        )
        self._reclaim_thread.start()

        atexit.register(self.shutdown)
        logger.info("ComputeScheduler 初始化完成，回收线程已启动")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        priority_dispatch: Optional[Any] = None,
        regime_allocator: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        tactile_cortex: Optional[Any] = None,
        health_monitor: Optional[Any] = None,
    ) -> None:
        """注入子模块与外部依赖（可选注入，未注入时使用静态降级策略）"""
        if priority_dispatch is not None:
            if hasattr(priority_dispatch, 'allocate') and hasattr(priority_dispatch, 'release'):
                self._priority_dispatch = priority_dispatch
                logger.info("PriorityDispatch 注入成功")
            else:
                logger.error("PriorityDispatch 缺少 allocate/release 方法，拒绝注入")

        if regime_allocator is not None:
            if hasattr(regime_allocator, 'get_quota') and hasattr(regime_allocator, 'update_allocation'):
                self._regime_allocator = regime_allocator
                logger.info("RegimeAdaptiveAllocator 注入成功")
            else:
                logger.error("RegimeAdaptiveAllocator 缺少 get_quota/update_allocation，拒绝注入")

        if negotiation_bus is not None:
            self._negotiation_bus = negotiation_bus
            logger.info("NegotiationBus 注入成功")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")

        if tactile_cortex is not None:
            self._tactile_cortex = tactile_cortex
            logger.info("TactileCortex 注入成功")

        if health_monitor is not None:
            self._health_monitor = health_monitor
            logger.info("SystemHealthMonitor 注入成功")

        with self._lock:
            self._reassess_degradation()

    # ========== 公共接口 ==========
    def set_market_regime(self, regime: str, volatility_pct: float) -> Dict[str, Any]:
        """更新当前市场状态"""
        if regime not in self.VALID_REGIMES:
            return {"status": "error", "reason": f"无效市场状态: {regime}", "data": {}, "warnings": [f"invalid_regime:{regime}"]}

        try:
            vol = float(volatility_pct)
        except (ValueError, TypeError):
            logger.warning("波动率分位无效: %s (类型:%s)", volatility_pct, type(volatility_pct))
            return {"status": "error", "reason": f"波动率分位必须为数值类型", "data": {}, "warnings": ["invalid_volatility_type"]}

        if vol < 0.0 or vol > 100.0:
            logger.warning("波动率分位超出范围 [0,100]: %.2f，已钳制", vol)
            vol = max(0.0, min(100.0, vol))

        with self._lock:
            self._current_regime = regime
            self._current_volatility_pct = vol
            self._last_quota_update = 0.0

        # 通知子模块市场状态变化
        self._notify_regime_change()

        logger.info("市场状态更新: regime=%s, volatility_pct=%.1f", regime, vol)
        return {"status": "ok", "reason": f"市场状态已更新为 {regime}，波动率分位 {vol:.1f}",
                "data": {"regime": regime, "volatility_pct": vol}, "warnings": []}

    def allocate(self, task_type: str, urgency: int) -> Dict[str, Any]:
        """为指定类型的任务分配算力"""
        if task_type not in self.VALID_TASK_TYPES:
            return {"status": "error", "reason": f"无效任务类型: {task_type}", "data": {}, "warnings": [f"unknown_task_type:{task_type}"]}

        urgency = max(0, min(10, int(urgency)))
        self._refresh_quotas_if_needed()

        temp_warning, throttle_ratio = self._check_cpu_health()
        cpu_throttle_active = throttle_ratio < 1.0

        with self._lock:
            allocation_id = self._generate_unique_allocation_id()
            allocated_at = time.time()

            # 入队前检查队列是否将满，若满则记录告警
            queue = self._task_queues[task_type]
            if len(queue) >= queue.maxlen:
                # 丢弃最旧的元素，并检查对应分配是否仍活跃
                oldest_id = queue[0]
                logger.warning("任务队列 %s 已满，丢弃最旧条目 %s", task_type, oldest_id)
                # 如果该最旧条目仍在活跃分配中，标记为异常
                if oldest_id in self._active_allocations:
                    logger.warning("被丢弃的队列条目 %s 仍在活跃分配中，可能造成统计偏差", oldest_id)

            queue.append(allocation_id)

            try:
                if self._priority_dispatch is not None:
                    result = self._priority_dispatch.allocate(task_type, urgency)
                    raw_quota = float(result.get("allocated_quota", 0.0))
                    if raw_quota < 0.0:
                        logger.error("子模块返回负配额: %.3f，修正为 0", raw_quota)
                        raw_quota = 0.0
                    queue_pos = int(result.get("queue_position", 0))
                    wait_ms = int(result.get("estimated_wait_ms", self.DEFAULT_WAIT_ESTIMATE_MS))
                else:
                    # 降级模式
                    queue_len = len(queue)
                    queue_pos = max(0, queue_len - 1)  # 包含本次入队
                    wait_ms = max(0, (queue_len - 1) * self.DEGRADED_WAIT_PER_QUEUE_MS)
                    raw_quota = self._calculate_degraded_quota(task_type, urgency)
            except Exception as e:
                logger.error("算力分配异常: %s #RECOVERY: 回退到最低保障配额", e)
                raw_quota = self._get_min_guarantee(task_type)
                queue_pos = len(queue) - 1
                wait_ms = self.DEFAULT_WAIT_ESTIMATE_MS

            # 最低保障兜底
            min_guarantee = self._get_min_guarantee(task_type)
            quota = max(raw_quota, min_guarantee)

            # CPU 温度分级缩减
            if cpu_throttle_active:
                quota_before = quota
                quota *= throttle_ratio
                quota = max(quota, min_guarantee)
                logger.warning("CPU 温度保护，配额由 %.4f 缩减至 %.4f (系数=%.2f)", quota_before, quota, throttle_ratio)

            quota = max(0.0, min(1.0, quota))

            # 记录分配时刻的上下文快照（用于准确审计）
            allocation_context = {
                "task_type": task_type,
                "urgency": urgency,
                "quota": quota,
                "allocated_at": allocated_at,
                "degraded": self._is_degraded,
                "regime": self._current_regime,
                "volatility_pct": self._current_volatility_pct,
            }
            self._active_allocations[allocation_id] = allocation_context

        self._log_allocation(allocation_id, allocation_context, temp_warning, cpu_throttle_active)

        return {
            "status": "ok",
            "reason": f"已为 {task_type} 分配算力 {quota:.1%} (紧急度={urgency})",
            "data": {
                "allocation_id": allocation_id,
                "allocated_quota": quota,
                "queue_position": queue_pos,
                "estimated_wait_ms": wait_ms,
                "is_degraded": self._is_degraded,
                "cpu_temp_warning": temp_warning,
                "cpu_throttle_active": cpu_throttle_active,
            },
            "warnings": (["cpu_temp_warning"] if temp_warning else []) +
                        (["cpu_throttle_active"] if cpu_throttle_active else []),
        }

    def release(self, task_type: str, allocation_id: str) -> Dict[str, Any]:
        """释放已分配的算力资源"""
        if allocation_id in self._orphan_allocations:
            return {"status": "error", "reason": f"分配 {allocation_id} 正在重试队列中，请勿重复释放", "data": {}, "warnings": ["orphan_allocation"]}

        if allocation_id not in self._active_allocations:
            return {"status": "error", "reason": f"分配记录不存在: {allocation_id}", "data": {}, "warnings": ["allocation_not_found"]}

        with self._lock:
            record = self._active_allocations.pop(allocation_id, None)
            if record is None:
                return {"status": "error", "reason": "分配记录已失效", "data": {}, "warnings": ["allocation_already_released"]}
            if record["task_type"] != task_type:
                logger.warning("释放类型不匹配: 请求=%s, 记录=%s", task_type, record["task_type"])
            self._remove_from_queue(record["task_type"], allocation_id)

        # 通知子模块释放（锁外），失败则放入孤儿队列
        success = self._safe_release_to_dispatch(record["task_type"], allocation_id)
        if not success:
            with self._lock:
                self._orphan_allocations[allocation_id] = record
                logger.warning("子模块释放失败，分配 %s 进入孤儿队列，等待重试", allocation_id)
            return {"status": "degraded", "reason": f"释放请求已提交，但子模块释放失败，已进入重试队列",
                    "data": {"released_quota": record["quota"]}, "warnings": ["dispatch_release_failed"]}

        logger.info("资源释放: %s, 配额=%.4f, 持有=%.1fs", allocation_id, record["quota"], time.time() - record["allocated_at"])
        return {"status": "ok", "reason": f"已释放 {allocation_id} 的算力", "data": {"released_quota": record["quota"]}, "warnings": []}

    def query_allocation(self, allocation_id: str) -> Dict[str, Any]:
        """查询分配记录状态"""
        with self._lock:
            if allocation_id in self._active_allocations:
                rec = self._active_allocations[allocation_id]
                return {"status": "ok", "reason": "分配活跃", "data": {"status": "active", "quota": rec["quota"], "allocated_at": rec["allocated_at"]}, "warnings": []}
            if allocation_id in self._orphan_allocations:
                rec = self._orphan_allocations[allocation_id]
                return {"status": "ok", "reason": "分配在重试队列", "data": {"status": "orphan", "quota": rec["quota"], "allocated_at": rec["allocated_at"]}, "warnings": ["orphan_allocation"]}
        return {"status": "error", "reason": "分配记录不存在", "data": {}, "warnings": ["allocation_not_found"]}

    def force_reclaim(self, task_type: str, reason: str) -> Dict[str, Any]:
        """紧急回收指定类型任务的算力"""
        if task_type not in self.VALID_TASK_TYPES:
            return {"status": "error", "reason": f"无效任务类型: {task_type}", "data": {}, "warnings": []}

        safe_reason = reason[:500]
        with self._lock:
            to_remove = [
                aid for aid, rec in self._active_allocations.items()
                if rec["task_type"] == task_type
            ]
            reclaimed_quota = 0.0
            for aid in to_remove:
                rec = self._active_allocations.pop(aid)
                reclaimed_quota += rec["quota"]
                self._remove_from_queue(task_type, aid)

        failed_ids = []
        for aid in to_remove:
            if not self._safe_release_to_dispatch(task_type, aid):
                failed_ids.append(aid)

        if failed_ids:
            logger.warning("紧急回收：%d 个分配子模块释放失败，进入孤儿队列", len(failed_ids))

        logger.warning("紧急回收: %s, 回收%d个, 回收配额=%.4f, 原因: %s",
                       task_type, len(to_remove), reclaimed_quota, safe_reason)
        self._log_force_reclaim(task_type, len(to_remove), reclaimed_quota, safe_reason)

        return {
            "status": "ok",
            "reason": f"已回收 {task_type} 的 {len(to_remove)} 个分配",
            "data": {"reclaimed_count": len(to_remove), "reclaimed_quota_ratio": reclaimed_quota},
            "warnings": [],
        }

    def get_current_quota(self, task_type: str) -> Dict[str, Any]:
        """查询某类任务当前的可用算力配额"""
        if task_type not in self.VALID_TASK_TYPES:
            return {"status": "error", "reason": f"无效任务类型: {task_type}", "data": {}, "warnings": [f"unknown_task_type:{task_type}"]}

        self._refresh_quotas_if_needed()

        try:
            if self._regime_allocator is not None and hasattr(self._regime_allocator, 'get_quota'):
                current = self._regime_allocator.get_quota(task_type)
            else:
                current = self._get_static_quota(task_type)
        except Exception as e:
            logger.warning("查询配额异常: %s，使用静态值", e)
            current = self._get_static_quota(task_type)

        with self._lock:
            allocated = sum(
                rec["quota"] for rec in self._active_allocations.values()
                if rec["task_type"] == task_type
            )
            queue_depth = len(self._task_queues.get(task_type, deque()))

        available = max(0.0, current - allocated)
        if allocated > current + 0.001:
            logger.warning("配额超额分配: %s, 上限=%.4f, 已分配=%.4f", task_type, current, allocated)

        return {
            "status": "ok",
            "reason": f"{task_type} 可用配额: {available:.1%}",
            "data": {
                "current_quota": round(current, 4),
                "available_quota": round(available, 4),
                "allocated_quota": round(allocated, 4),
                "min_guarantee": self._get_min_guarantee(task_type),
                "is_degraded": self._is_degraded,
                "queue_depth": queue_depth,
            },
            "warnings": [],
        }

    def get_all_quotas(self) -> Dict[str, Any]:
        """查询所有任务类型的配额分配详情（原子快照）"""
        self._refresh_quotas_if_needed()
        all_quotas = {}
        for task_type in self.VALID_TASK_TYPES:
            res = self.get_current_quota(task_type)
            if res["status"] == "ok":
                all_quotas[task_type] = res["data"]
            else:
                all_quotas[task_type] = {
                    "current_quota": 0.0,
                    "available_quota": 0.0,
                    "allocated_quota": 0.0,
                    "queue_depth": 0,
                    "error": res["reason"],
                }

        with self._lock:
            reasons = list(self._degradation_reasons)
            is_deg = self._is_degraded

        return {
            "status": "ok",
            "reason": f"当前市场状态: {self._current_regime}",
            "data": {
                "quotas": all_quotas,
                "regime": self._current_regime,
                "volatility_pct": self._current_volatility_pct,
                "is_degraded": is_deg,
                "degradation_reasons": reasons,
            },
            "warnings": [],
        }

    def get_scheduler_status(self) -> Dict[str, Any]:
        """获取调度器自身的运行状态"""
        with self._lock:
            active_count = len(self._active_allocations)
            total_allocated = sum(rec["quota"] for rec in self._active_allocations.values())
            reasons = list(self._degradation_reasons)
            is_deg = self._is_degraded

        return {
            "status": "ok",
            "reason": f"活跃分配 {active_count} 个",
            "data": {
                "active_allocations": active_count,
                "total_allocated_quota": round(total_allocated, 4),
                "current_regime": self._current_regime,
                "volatility_pct": self._current_volatility_pct,
                "is_degraded": is_deg,
                "degradation_reasons": reasons,
            },
            "warnings": [],
        }

    def shutdown(self) -> None:
        """优雅关闭调度器，等待后台线程退出"""
        self._shutdown_flag = True
        self._reclaim_stop_event.set()
        if self._reclaim_thread.is_alive():
            self._reclaim_thread.join(timeout=5.0)
            if self._reclaim_thread.is_alive():
                logger.warning("回收线程未能在 5 秒内退出，强制终止")
        logger.info("ComputeScheduler 已关闭")

    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            with self._lock:
                dispatch_ok = self._priority_dispatch is not None
                allocator_ok = self._regime_allocator is not None
                bus_ok = self._negotiation_bus is not None
                cortex_ok = self._tactile_cortex is not None
                health_ok = self._health_monitor is not None
                active_count = len(self._active_allocations)
                orphan_count = len(self._orphan_allocations)
                reasons = list(self._degradation_reasons)

                total_quota = sum(self._get_static_quota(t) for t in self.VALID_TASK_TYPES)
                quota_reasonable = abs(total_quota - 1.0) < 0.05

                warnings = []
                if not dispatch_ok: warnings.append("priority_dispatch_unavailable")
                if not allocator_ok: warnings.append("regime_allocator_unavailable")
                if not cortex_ok: warnings.append("tactile_cortex_unavailable")
                if not health_ok: warnings.append("health_monitor_unavailable")
                if not quota_reasonable: warnings.append(f"quota_sum:{total_quota:.2%}")
                if orphan_count > 0: warnings.append(f"orphan_allocations:{orphan_count}")
                warnings.extend(reasons)

            return {
                "status": "ok" if not warnings else "degraded",
                "reason": f"活跃分配 {active_count}, 孤儿 {orphan_count}",
                "data": {
                    "dependencies": {
                        "priority_dispatch": dispatch_ok,
                        "regime_allocator": allocator_ok,
                        "negotiation_bus": bus_ok,
                        "tactile_cortex": cortex_ok,
                        "health_monitor": health_ok,
                    },
                    "active_allocations": active_count,
                    "orphan_allocations": orphan_count,
                    "quota_total": round(total_quota, 4),
                    "quota_reasonable": quota_reasonable,
                    "is_degraded": len(warnings) > 0,
                },
                "warnings": warnings,
            }
        except Exception as e:
            logger.error("健康检查失败: %s #RECOVERY: 检查锁状态和依赖注入状态", e)
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"]}

    # ========== 私有方法 ==========
    def _reassess_degradation(self):
        """重新评估降级状态（需在锁内调用）"""
        self._degradation_reasons = []  # 清空后重新评估，避免重复
        if self._priority_dispatch is None:
            self._degradation_reasons.append("priority_dispatch_missing")
        if self._regime_allocator is None:
            self._degradation_reasons.append("regime_allocator_missing")
        if self._tactile_cortex is None:
            self._degradation_reasons.append("tactile_cortex_missing")
        if self._health_monitor is None:
            self._degradation_reasons.append("health_monitor_missing")
        self._is_degraded = len(self._degradation_reasons) > 0

    def _get_static_quota(self, task_type: str) -> float:
        """根据当前市场状态返回静态配额（需在锁内调用）"""
        if self._current_regime == "sabbath":
            return self.SABBATH_STATIC_QUOTAS.get(task_type, 0.0)
        return self.DEFAULT_STATIC_QUOTAS.get(task_type, 0.05)

    def _get_min_guarantee(self, task_type: str) -> float:
        """根据当前市场状态返回最低保障配额（需在锁内调用）"""
        if self._current_regime == "sabbath":
            return self.MIN_GUARANTEED_QUOTA_SABBATH.get(task_type, 0.0)
        return self.MIN_GUARANTEED_QUOTA_NORMAL.get(task_type, 0.0)

    def _remove_from_queue(self, task_type: str, allocation_id: str):
        """从任务队列中移除指定分配（需在锁内调用）"""
        queue = self._task_queues.get(task_type)
        if queue:
            try:
                queue.remove(allocation_id)
            except ValueError:
                logger.debug("队列中未找到分配 %s，可能已被自动丢弃", allocation_id)

    def _generate_unique_allocation_id(self) -> str:
        """生成唯一的分配ID，带碰撞检测"""
        for _ in range(self.ALLOC_ID_COLLISION_MAX_RETRIES):
            new_id = str(uuid.uuid4())[:12]
            if new_id not in self._active_allocations and new_id not in self._orphan_allocations:
                return new_id
        logger.error("分配ID碰撞检测超过最大重试次数 %d，使用完整UUID", self.ALLOC_ID_COLLISION_MAX_RETRIES)
        return str(uuid.uuid4())  # 回退到完整UUID

    def _refresh_quotas_if_needed(self):
        """按需刷新配额分配"""
        now = time.time()
        with self._lock:
            interval = self.QUOTA_REFRESH_INTERVAL_SABBATH if self._current_regime == "sabbath" else self.QUOTA_REFRESH_INTERVAL_SEC
            if now - self._last_quota_update < interval:
                return

        # 锁外获取感知数据（带超时保护）
        new_regime = None
        new_vol = None
        cortex_recovered = False
        if self._tactile_cortex is not None and hasattr(self._tactile_cortex, 'get_volatility_regime'):
            try:
                import signal
                # 使用 signal.alarm 实现超时（仅在支持的系统上）
                result = self._tactile_cortex.get_volatility_regime()
                if isinstance(result, dict) and result.get("status") == "ok":
                    data = result.get("data", {})
                    new_regime = data.get("regime")
                    new_vol = data.get("volatility_pct")
                    cortex_recovered = True
            except Exception as e:
                logger.warning("从 TactileCortex 获取状态失败: %s", e)

        with self._lock:
            if new_regime is not None and new_regime in self.VALID_REGIMES:
                self._current_regime = new_regime
            if isinstance(new_vol, (int, float)):
                self._current_volatility_pct = float(new_vol)
            self._last_quota_update = now

            # 如果感知模块恢复可用，重新评估降级状态
            if cortex_recovered and self._tactile_cortex is not None:
                self._reassess_degradation()

        # 更新分配器
        if self._regime_allocator is not None:
            try:
                self._regime_allocator.update_allocation(
                    regime=self._current_regime,
                    volatility_pct=self._current_volatility_pct,
                    is_degraded=self._is_degraded,
                )
            except Exception as e:
                logger.warning("分配器更新失败: %s，将使用静态配额", e)

        # 广播配额变化
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="quota_regime_change",
                    regime=self._current_regime,
                    volatility_pct=self._current_volatility_pct,
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.debug("配额变化广播失败: %s", e)

    def _calculate_degraded_quota(self, task_type: str, urgency: int) -> float:
        """降级模式下的配额计算，考虑任务关键性"""
        base = self._get_static_quota(task_type)
        min_q = self._get_min_guarantee(task_type)
        if task_type in ("trading_core", "strategy_engine"):
            bonus = urgency / 10.0 * 0.10
        else:
            bonus = urgency / 10.0 * 0.03
        return max(min_q, min(base + bonus, 1.0))

    def _check_cpu_health(self) -> Tuple[bool, float]:
        """
        检查 CPU 温度状态
        Returns:
            (警告标志, 配额保留比例)  保留比例 1.0 表示无缩减，<1.0 表示需缩减
        """
        try:
            if self._health_monitor is not None and hasattr(self._health_monitor, 'get_cpu_temperature'):
                temp = self._health_monitor.get_cpu_temperature()
                if not isinstance(temp, (int, float)):
                    logger.warning("CPU 温度返回值无效类型: %s", type(temp).__name__)
                    return False, 1.0
                temp = float(temp)
                logger.debug("当前 CPU 温度: %.1f°C", temp)
                if temp > self.CPU_TEMP_CRITICAL_THRESHOLD:
                    logger.error("CPU 温度严重过高: %.1f°C，触发临界降频", temp)
                    return True, self.CPU_THROTTLE_REDUCTION_CRITICAL
                elif temp > self.CPU_TEMP_WARNING_THRESHOLD:
                    logger.warning("CPU 温度偏高: %.1f°C，触发警告降频", temp)
                    return True, self.CPU_THROTTLE_REDUCTION_WARNING
                return False, 1.0
        except Exception as e:
            logger.warning("CPU 温度检查异常: %s，跳过温度保护", e)
        return False, 1.0

    def _safe_release_to_dispatch(self, task_type: str, allocation_id: str) -> bool:
        """安全释放到子模块，返回是否成功。若子模块不存在，视为成功。"""
        if self._priority_dispatch is not None and hasattr(self._priority_dispatch, 'release'):
            try:
                self._priority_dispatch.release(task_type, allocation_id)
                return True
            except Exception as e:
                logger.warning("PriorityDispatch.release 异常: %s", e)
                return False
        # 无子模块时视为释放成功
        return True

    def _log_allocation(self, allocation_id: str, context: Dict[str, Any],
                        temp_warn: bool, throttle_active: bool):
        """记录分配审计日志（使用分配时刻的上下文快照）"""
        logger.info("算力分配: task=%s, urgency=%d, quota=%.4f, id=%s, degraded=%s, temp_warn=%s, throttle=%s, regime=%s",
                    context["task_type"], context["urgency"], context["quota"],
                    allocation_id, context["degraded"], temp_warn, throttle_active, context["regime"])
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="compute_allocation",
                    details={
                        "allocation_id": allocation_id,
                        "task_type": context["task_type"],
                        "urgency": context["urgency"],
                        "quota": context["quota"],
                        "is_degraded": context["degraded"],
                        "cpu_temp_warning": temp_warn,
                        "cpu_throttle_active": throttle_active,
                        "regime": context["regime"],
                        "volatility_pct": context["volatility_pct"],
                    },
                )
            except Exception as e:
                logger.warning("行为日志记录失败: %s", e)

    def _log_force_reclaim(self, task_type: str, count: int, quota: float, reason: str):
        """记录紧急回收事件"""
        logger.info("紧急回收: task=%s, count=%d, quota=%.4f, reason=%s, timestamp=%d",
                    task_type, count, quota, reason, int(time.time()))
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="force_reclaim",
                    details={
                        "task_type": task_type,
                        "reclaimed_count": count,
                        "reclaimed_quota": quota,
                        "reason": reason,
                        "timestamp": time.time(),
                    },
                )
            except Exception as e:
                logger.warning("行为日志记录失败: %s", e)

    def _notify_regime_change(self):
        """通知子模块市场状态变化"""
        if self._priority_dispatch is not None:
            if hasattr(self._priority_dispatch, 'on_regime_change'):
                try:
                    self._priority_dispatch.on_regime_change(self._current_regime, self._current_volatility_pct)
                except Exception as e:
                    logger.warning("PriorityDispatch 市场状态通知失败: %s", e)
        if self._regime_allocator is not None and hasattr(self._regime_allocator, 'update_allocation'):
            try:
                self._regime_allocator.update_allocation(
                    regime=self._current_regime,
                    volatility_pct=self._current_volatility_pct,
                    is_degraded=self._is_degraded,
                )
            except Exception as e:
                logger.warning("RegimeAdaptiveAllocator 市场状态通知失败: %s", e)

    def _timeout_reclaim_loop(self):
        """后台线程：定期回收超时的分配（锁外调用子模块）"""
        while not self._reclaim_stop_event.wait(self.RECLAIM_SWEEP_INTERVAL_SEC):
            # 先处理孤儿重试（优化：只收集需要重试的ID，锁内操作最小化）
            retry_batch = []
            with self._lock:
                to_remove = []
                for aid, rec in list(self._orphan_allocations.items()):
                    if rec.get("retry_count", 0) >= self.ORPHAN_RETRY_MAX_ATTEMPTS:
                        to_remove.append(aid)
                        logger.error("孤儿分配 %s 已达最大重试次数 %d，永久丢弃，配额 %.4f 泄漏",
                                     aid, self.ORPHAN_RETRY_MAX_ATTEMPTS, rec["quota"])
                        continue
                    retry_batch.append((aid, rec["task_type"]))
                    rec["retry_count"] = rec.get("retry_count", 0) + 1
                for aid in to_remove:
                    del self._orphan_allocations[aid]

            for aid, task_type in retry_batch:
                if self._safe_release_to_dispatch(task_type, aid):
                    with self._lock:
                        if aid in self._orphan_allocations:
                            del self._orphan_allocations[aid]
                    logger.info("孤儿分配 %s 重试释放成功 (重试次数: %d)",
                                aid, self._orphan_allocations.get(aid, {}).get("retry_count", 0))
                # 失败不处理，下次继续重试

            # 处理超时回收
            now = time.time()
            with self._lock:
                expired_aids = [
                    aid for aid, rec in self._active_allocations.items()
                    if rec["task_type"] not in self.PERMANENT_TASK_TYPES
                    and now - rec["allocated_at"] > self.ALLOCATION_TIMEOUT_SEC
                ]
                for aid in expired_aids:
                    rec = self._active_allocations.pop(aid, None)
                    if rec:
                        self._remove_from_queue(rec["task_type"], aid)
                        self._orphan_allocations[aid] = rec
                        rec["retry_count"] = 0
                        logger.warning("分配超时回收，进入孤儿队列: %s, 类型=%s, 持有=%.1fs",
                                       aid, rec["task_type"], now - rec["allocated_at"])
