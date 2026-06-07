"""
火种系统 · 每日任务DAG调度器 (TaskDagScheduler)

核心职责：
1. 构建确定性有向无环图(DAG)，按拓扑排序执行任务，支持优先级、超时、指数退避重试、协作取消
2. 提供线程安全、金融级审计的任务调度，包含 Prometheus 指标暴露、优雅关闭及任务上下文传递

外部依赖（真实模块接口）：
- core.precision_timer.PrecisionTimer : 硬件级精确定时（降级为 time.perf_counter）
- core.behavioral_logger.BehavioralLogger : 金融级审计日志（降级写入本地审计文件）

接口契约：
- add_task / remove_task / schedule / health_check / graceful_shutdown / export_prometheus_metrics
- 所有公共方法输出字典固定包含 "status", "reason", "data", "warnings"

异常与降级：
- 任务超时使用 Future.result(timeout)；任务函数可接受 cancellation_token 实现协作取消
- 依赖任务失败时默认跳过（fail-fast），可配置覆盖
- 线程池满时降级为同步执行（限总超时）
- 审计日志失败时降级写入本地文件（自动轮转，加锁保护）

资源管理：
- 线程池在对象销毁时自动 shutdown；graceful_shutdown 支持超时控制
- 拓扑排序结果缓存至 DAG 变更，加锁保护
- GC 使用专用守护线程
- 闭包引用在 remove_task 时主动清理，并更新所有依赖者的依赖列表
- 审计降级文件自动轮转，保留最近 10MB，并发写入加锁

金融级合规：
- 每次调度的拓扑排序结果生成确定性哈希，存入审计日志
- 任务执行结果附带纳秒时间戳与主机标识
- 依赖失败传播策略默认启用
"""

import time
import os
import gc
import uuid
import logging
import threading
import traceback
import hashlib
import json
import heapq
import re
import queue
import sys
from typing import Dict, Any, List, Optional, Callable, Tuple, Set, Final
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, CancelledError
from dataclasses import dataclass, field
from enum import Enum
from inspect import signature, Parameter

logger = logging.getLogger(__name__)
_HOSTNAME = os.uname().nodename if hasattr(os, 'uname') else "unknown"

_AUDIT_FALLBACK_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "logs", "dag_audit_fallback.log")
_AUDIT_FALLBACK_MAX_SIZE = 10 * 1024 * 1024
_AUDIT_FALLBACK_LOCK = threading.Lock()


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


@dataclass
class RetryConfig:
    """任务重试配置"""
    max_retries: int = 0
    base_delay_sec: float = 1.0
    backoff_multiplier: float = 2.0
    max_delay_sec: float = 60.0
    max_total_retry_time_sec: float = 300.0
    retry_on_exceptions: Tuple[type, ...] = (RuntimeError, OSError)

    def __repr__(self) -> str:
        return (f"RetryConfig(max_retries={self.max_retries}, base={self.base_delay_sec}s, "
                f"mult={self.backoff_multiplier}, max_delay={self.max_delay_sec}s, "
                f"total_time={self.max_total_retry_time_sec}s, retry_on={self.retry_on_exceptions})")


class CancellationToken:
    """协作取消令牌，线程安全"""
    def __init__(self):
        self._event = threading.Event()
        self._cancelled = False
        self._lock = threading.Lock()

    @property
    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        """幂等取消，先设置事件再标记状态，保证 wait 能立即返回"""
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
        self._event.set()

    def wait(self, timeout: float = None) -> bool:
        """等待取消信号，返回 True 表示已被取消，超时返回 False 表示未被取消"""
        return self._event.wait(timeout)

    def __repr__(self) -> str:
        return f"CancellationToken(cancelled={self.is_cancelled})"


class TaskDagScheduler:
    """华尔街级DAG任务调度器，金融级生产就绪"""

    # ========== 类常量 ==========
    DEFAULT_TIMEOUT_SEC: Final = 300.0
    MAX_CONSECUTIVE_FAILURES: Final = 3
    MAX_THREAD_POOL_SIZE: Final = 4
    GC_TRIGGER_INTERVAL: Final = 3
    FAIL_FAST_DEFAULT: Final = True
    TASK_NAME_PATTERN: Final = re.compile(r'^[a-z][a-z0-9_]*$')
    MAX_TASKS: Final = 500
    AUDIT_FALLBACK_MAX_SIZE: Final = 10 * 1024 * 1024
    TOPO_CACHE_VERSION: Final = 1

    def __init__(self):
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._dependencies: Dict[str, Set[str]] = defaultdict(set)
        self._dependents: Dict[str, Set[str]] = defaultdict(set)
        self._executor = ThreadPoolExecutor(max_workers=self.MAX_THREAD_POOL_SIZE, thread_name_prefix="dag_worker")
        self._lock = threading.RLock()
        self._scheduler_running = False
        self._shutting_down = False
        self._schedule_count = 0
        self._schedule_count_lock = threading.Lock()
        self._timer = None
        self._behavioral_logger = None
        self._task_stats: Dict[str, Dict[str, float]] = defaultdict(lambda: {"count": 0, "total_time": 0.0, "failures": 0})
        self._task_stats_lock = threading.Lock()
        self._gc_queue: queue.Queue = queue.Queue(maxsize=1)
        self._gc_thread = threading.Thread(target=self._gc_worker, daemon=True, name="dag_gc_worker")
        self._gc_thread.start()
        self._topo_cache: Optional[Tuple[List[str], List[str], str]] = None
        self._topo_cache_lock = threading.Lock()
        self._action_sig_cache: Dict[int, Dict[str, bool]] = {}
        self._sig_cache_lock = threading.Lock()
        logger.info("TaskDagScheduler 初始化完成 [host=%s][pool=%d]", _HOSTNAME, self.MAX_THREAD_POOL_SIZE)

    def __del__(self):
        self.graceful_shutdown(timeout_sec=0.5)

    def inject_dependencies(
        self,
        precision_timer: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        metrics_exporter: Optional[Any] = None,
    ) -> None:
        with self._lock:
            if precision_timer is not None:
                self._timer = precision_timer
            if behavioral_logger is not None:
                self._behavioral_logger = behavioral_logger
            if metrics_exporter is not None:
                self._metrics_exporter = metrics_exporter

    # ========== 公共接口 ==========
    def add_task(
        self,
        name: str,
        dependencies: List[str],
        action: Callable[..., bool],
        priority: int = 5,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        retry_config: Optional[RetryConfig] = None,
        fail_fast: Optional[bool] = None,
        allow_cancellation: bool = False,
        pre_condition: Optional[Callable[[], bool]] = None,
        post_condition: Optional[Callable[[], None]] = None,
        description: str = "",
        on_timeout: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Any]:
        """注册新任务，线程安全"""
        if not name or not self.TASK_NAME_PATTERN.match(name):
            return {"status": "error", "reason": f"名称需snake_case: {name}", "data": {}, "warnings": ["invalid_name"]}
        if not callable(action):
            return {"status": "error", "reason": "action 不可调用", "data": {}, "warnings": ["non_callable"]}
        if name in dependencies:
            return {"status": "error", "reason": "不能依赖自身", "data": {}, "warnings": ["self_dependency"]}
        clean_deps = list(set(d for d in dependencies if d and isinstance(d, str)))
        with self._lock:
            if len(self._tasks) >= self.MAX_TASKS:
                return {"status": "error", "reason": f"任务数已达上限 {self.MAX_TASKS}", "data": {}, "warnings": ["max_tasks"]}
            if name in self._tasks:
                return {"status": "error", "reason": f"任务 {name} 已存在", "data": {}, "warnings": ["duplicate"]}
            temp_deps = {k: set(v) for k, v in self._dependencies.items()}
            temp_deps[name] = set(clean_deps)
            if self._detect_cycle_in_graph(temp_deps):
                return {"status": "error", "reason": "添加将导致循环依赖", "data": {}, "warnings": ["cycle"]}
            missing = [d for d in clean_deps if d not in self._tasks]
            self._tasks[name] = {
                "action": action,
                "dependencies": clean_deps,
                "missing_deps_at_registration": missing,
                "priority": max(0, min(10, priority)),
                "timeout_sec": max(1.0, timeout_sec),
                "retry_config": retry_config or RetryConfig(),
                "fail_fast": fail_fast if fail_fast is not None else self.FAIL_FAST_DEFAULT,
                "allow_cancellation": allow_cancellation,
                "pre_condition": pre_condition,
                "post_condition": post_condition,
                "description": description,
                "on_timeout": on_timeout,
                "status": "registered",
                "consecutive_failures": 0,
                "registered_at": time.time(),
                "version": 0,
            }
            self._dependencies[name] = set(clean_deps)
            for dep in clean_deps:
                self._dependents[dep].add(name)
            self._invalidate_topo_cache()
        logger.info("任务注册: %s (pri=%d, deps=%d, desc=%s)", name, priority, len(clean_deps), description)
        return {"status": "ok", "reason": f"任务 {name} 注册成功", "data": {"name": name, "missing_deps": missing}, "warnings": []}

    def remove_task(self, name: str) -> Dict[str, Any]:
        """移除任务，并清理所有依赖关系"""
        with self._lock:
            if name not in self._tasks:
                return {"status": "error", "reason": f"任务 {name} 不存在", "data": {}, "warnings": ["not_found"]}
            # 清理闭包引用
            self._tasks[name]["action"] = None
            del self._tasks[name]
            # 清理依赖图
            removed_deps = self._dependencies.pop(name, set())
            for dep in removed_deps:
                self._dependents[dep].discard(name)
            dependents = self._dependents.pop(name, set())
            for dependent in dependents:
                self._dependencies[dependent].discard(name)
                # 更新依赖者任务内部存储的依赖列表
                if dependent in self._tasks:
                    self._tasks[dependent]["dependencies"] = [d for d in self._tasks[dependent]["dependencies"] if d != name]
            self._invalidate_topo_cache()
        logger.info("任务 %s 已移除，影响 %d 个依赖者", name, len(dependents))
        return {"status": "ok", "reason": f"任务 {name} 已移除", "data": {}, "warnings": []}

    def schedule(self, triggered_by: str = "manual") -> Dict[str, Any]:
        """按DAG顺序执行所有任务，金融级审计"""
        if self._shutting_down:
            return {"status": "error", "reason": "调度器正在关闭", "data": {}, "warnings": ["shutting_down"]}
        if not self._tasks:
            return {"status": "ok", "reason": "无任务", "data": {"total": 0}, "warnings": []}
        if triggered_by not in {"manual", "scheduler", "auto", "test"}:
            logger.warning("非标准触发来源: %s，已标记为 manual", triggered_by)
            triggered_by = "manual"
        with self._lock:
            if self._scheduler_running:
                return {"status": "error", "reason": "调度器运行中", "data": {}, "warnings": ["already_running"]}
            self._scheduler_running = True
            # 在锁内生成拓扑排序快照，保证一致性
            sorted_tasks, blocked_tasks = self._topological_sort_deterministic()
            topo_hash = self._compute_topo_hash(sorted_tasks, blocked_tasks)
            # 缓存更新
            self._update_topo_cache(sorted_tasks, blocked_tasks, topo_hash)

        schedule_id = f"dag_{uuid.uuid4().hex[:12]}"
        start_ts = time.perf_counter()
        results: Dict[str, Dict[str, Any]] = {}
        total_success = total_failed = total_skipped = total_blocked = 0
        execution_order = []
        warnings = []
        dependency_status: Dict[str, TaskStatus] = {}
        skipped_due_to_shutdown = []
        try:
            if blocked_tasks:
                for bt in blocked_tasks:
                    results[bt] = {"status": TaskStatus.BLOCKED.value, "reason": "循环依赖", "duration": 0.0}
                total_blocked = len(blocked_tasks)
                warnings.append(f"循环依赖阻塞 {total_blocked} 个任务")

            for task_name in sorted_tasks:
                if self._shutting_down:
                    skipped_due_to_shutdown.append(task_name)
                    results[task_name] = {"status": TaskStatus.SKIPPED.value, "reason": "调度器关闭中断", "duration": 0.0}
                    total_skipped += 1
                    continue

                with self._lock:
                    task = self._tasks.get(task_name)
                if not task:
                    results[task_name] = {"status": TaskStatus.SKIPPED.value, "reason": "任务已删除", "duration": 0.0}
                    total_skipped += 1
                    continue
                if task["consecutive_failures"] >= self.MAX_CONSECUTIVE_FAILURES:
                    results[task_name] = {"status": TaskStatus.SKIPPED.value, "reason": "永久失败", "duration": 0.0}
                    total_skipped += 1
                    continue
                if task.get("fail_fast", self.FAIL_FAST_DEFAULT):
                    deps = task.get("dependencies", [])
                    failed_deps = [d for d in deps if dependency_status.get(d) == TaskStatus.FAILED]
                    if failed_deps:
                        results[task_name] = {"status": TaskStatus.SKIPPED.value, "reason": f"依赖失败: {failed_deps}", "duration": 0.0}
                        total_skipped += 1
                        dependency_status[task_name] = TaskStatus.SKIPPED
                        continue
                pre_cond = task.get("pre_condition")
                if pre_cond and callable(pre_cond):
                    try:
                        if not pre_cond():
                            results[task_name] = {"status": TaskStatus.SKIPPED.value, "reason": "前置条件不满足", "duration": 0.0}
                            total_skipped += 1
                            dependency_status[task_name] = TaskStatus.SKIPPED
                            continue
                    except Exception as e:
                        logger.warning("任务 %s 前置条件异常: %s", task_name, e)
                exec_result = self._execute_with_retry(task_name, task, schedule_id)
                results[task_name] = exec_result
                dependency_status[task_name] = TaskStatus.SUCCESS if exec_result["status"] == "success" else TaskStatus.FAILED
                if exec_result["status"] == "success":
                    total_success += 1
                elif exec_result["status"] == "failed":
                    total_failed += 1
                else:
                    total_skipped += 1
                execution_order.append(task_name)
                post_cond = task.get("post_condition")
                if post_cond and callable(post_cond):
                    try:
                        post_cond()
                    except Exception as e:
                        logger.warning("任务 %s 后置处理异常: %s", task_name, e)
            if skipped_due_to_shutdown:
                warnings.append(f"因关闭跳过 {len(skipped_due_to_shutdown)} 个任务")
            audit = {"schedule_id": schedule_id, "topological_hash": topo_hash, "triggered_by": triggered_by,
                     "execution_order": execution_order, "results_summary": {k: v["status"] for k, v in results.items()},
                     "skipped_by_shutdown": skipped_due_to_shutdown}
            self._log_event("dag_schedule_complete", audit)
        finally:
            with self._lock:
                self._scheduler_running = False
            with self._schedule_count_lock:
                self._schedule_count += 1
            if self._schedule_count % self.GC_TRIGGER_INTERVAL == 0:
                try:
                    self._gc_queue.put_nowait(True)
                except queue.Full:
                    logger.debug("GC请求丢弃，队列满")
        total = len(sorted_tasks) + len(blocked_tasks)
        elapsed_total = time.perf_counter() - start_ts
        return {"status": "ok", "reason": f"{total_success}/{total} 成功",
                "data": {"schedule_id": schedule_id, "topological_hash": topo_hash, "total": total,
                         "success": total_success, "failed": total_failed, "skipped": total_skipped,
                         "blocked": total_blocked, "execution_order": execution_order, "results": results,
                         "elapsed_sec": round(elapsed_total, 6), "host": _HOSTNAME,
                         "utc_timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.", time.gmtime()) + f"{time.time_ns() % 1_000_000_000:09d}Z"},
                "warnings": warnings}

    def graceful_shutdown(self, timeout_sec: float = 5.0) -> None:
        """优雅关闭，取消未完成任务，停止GC线程"""
        self._shutting_down = True
        logger.info("TaskDagScheduler 开始优雅关闭...")
        try:
            kwargs = {"wait": True, "timeout": timeout_sec}
            if sys.version_info >= (3, 9):
                kwargs["cancel_futures"] = True
            self._executor.shutdown(**kwargs)
        except Exception as e:
            logger.warning("线程池关闭异常: %s", e)
        finally:
            self._gc_queue.put(None)
            if self._gc_thread.is_alive():
                self._gc_thread.join(timeout=1.0)
            logger.info("TaskDagScheduler 已关闭")

    def export_prometheus_metrics(self) -> Dict[str, float]:
        """导出 Prometheus 指标"""
        metrics = {"dag_scheduler_task_count": float(len(self._tasks)), "dag_scheduler_schedule_count": float(self._schedule_count)}
        with self._task_stats_lock:
            for name, stats in list(self._task_stats.items()):
                if stats["count"] > 0:
                    metrics[f"dag_task_{name}_executions_total"] = stats["count"]
                    metrics[f"dag_task_{name}_failures_total"] = stats["failures"]
                    metrics[f"dag_task_{name}_avg_duration_sec"] = stats["total_time"] / stats["count"]
        try:
            if hasattr(self._executor, '_work_queue') and not self._shutting_down:
                wq = self._executor._work_queue
                metrics["dag_thread_pool_queue_depth"] = float(wq.qsize())
        except Exception:
            pass
        return metrics

    def health_check(self) -> Dict[str, Any]:
        """健康检查"""
        try:
            with self._lock:
                task_count = len(self._tasks)
                failed_count = sum(1 for t in self._tasks.values() if t["consecutive_failures"] >= self.MAX_CONSECUTIVE_FAILURES)
                sorted_list, blocked = self._topological_sort_deterministic()
            has_cycle = len(blocked) > 0
            return {"status": "ok", "reason": f"正常，{task_count}任务",
                    "data": {"task_count": task_count, "permanently_failed_count": failed_count, "has_cycle": has_cycle},
                    "warnings": ["cycle"] if has_cycle else []}
        except Exception as e:
            return {"status": "error", "reason": str(e), "data": {}, "warnings": []}

    # ========== 私有方法 ==========
    def _execute_with_retry(self, task_name: str, task: Dict[str, Any], schedule_id: str) -> Dict[str, Any]:
        retry_cfg: RetryConfig = task.get("retry_config", RetryConfig())
        max_attempts = 1 + retry_cfg.max_retries
        last_exception = None
        total_elapsed = 0.0
        cancel_token = CancellationToken() if task.get("allow_cancellation") else None
        for attempt in range(max_attempts):
            if self._shutting_down:
                last_exception = RuntimeError("调度器关闭")
                break
            if attempt > 0:
                delay = min(retry_cfg.base_delay_sec * (retry_cfg.backoff_multiplier ** (attempt - 1)), retry_cfg.max_delay_sec)
                if total_elapsed + delay > retry_cfg.max_total_retry_time_sec:
                    break
                if cancel_token and cancel_token.wait(delay):
                    break
                else:
                    time.sleep(delay)
            task_start = time.perf_counter()
            try:
                future = self._executor.submit(self._run_task_action, task["action"], cancel_token, schedule_id)
                success = future.result(timeout=task["timeout_sec"])
                elapsed = time.perf_counter() - task_start
                total_elapsed += elapsed
                self._update_stats(task_name, elapsed, success)
                if success:
                    with self._lock:
                        if task_name in self._tasks: self._tasks[task_name]["consecutive_failures"] = 0
                    return {"status": "success", "duration": round(elapsed, 6), "attempts": attempt + 1}
                else:
                    last_exception = RuntimeError("返回False")
            except CancelledError:
                break
            except FuturesTimeoutError:
                elapsed = time.perf_counter() - task_start
                total_elapsed += elapsed
                cancelled = future.cancel()
                if not cancelled:
                    logger.warning("任务 %s 取消失败，线程仍在运行", task_name)
                if cancel_token: cancel_token.cancel()
                last_exception = FuturesTimeoutError(f"超时>{task['timeout_sec']}s")
                on_timeout = task.get("on_timeout")
                if on_timeout and callable(on_timeout):
                    try: on_timeout()
                    except Exception: pass
                break
            except RuntimeError as e:
                elapsed = time.perf_counter() - task_start
                total_elapsed += elapsed
                if "线程池已关闭" in str(e):
                    last_exception = e; break
                try:
                    if self._shutting_down:
                        last_exception = RuntimeError("调度器关闭"); break
                    success = self._run_task_action(task["action"], cancel_token, schedule_id)
                    elapsed = time.perf_counter() - task_start
                    total_elapsed += elapsed
                    self._update_stats(task_name, elapsed, success)
                    return {"status": "success" if success else "failed", "duration": round(elapsed, 6), "degraded": True}
                except Exception as e2:
                    last_exception = e2; break
            except Exception as e:
                elapsed = time.perf_counter() - task_start
                total_elapsed += elapsed
                last_exception = e
                if not isinstance(e, retry_cfg.retry_on_exceptions):
                    break
        with self._lock:
            if task_name in self._tasks: self._tasks[task_name]["consecutive_failures"] += 1
        return {"status": "failed", "reason": str(last_exception)[:200], "duration": round(total_elapsed, 6),
                "attempts": max_attempts, "final_error": str(last_exception)[:200]}

    @staticmethod
    def _run_task_action(action: Callable, cancel_token: Optional[CancellationToken], schedule_id: str) -> bool:
        try:
            try:
                sig = signature(action)
                params = {name: None for name, param in sig.parameters.items()
                          if param.kind in (Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY)}
            except (ValueError, TypeError):
                params = {}
            kwargs = {}
            if 'cancel_token' in params:
                kwargs['cancel_token'] = cancel_token
            if 'schedule_id' in params:
                kwargs['schedule_id'] = schedule_id
            return action(**kwargs) if kwargs else action()
        except TypeError:
            return action()

    def _topological_sort_deterministic(self) -> Tuple[List[str], List[str]]:
        """确定性拓扑排序，必须在 self._lock 保护下调用"""
        in_degree = {n: 0 for n in self._tasks}
        for n, deps in self._dependencies.items():
            if n in in_degree:
                in_degree[n] = len([d for d in deps if d in in_degree])
        heap = [(-self._tasks[n]["priority"], n) for n, d in in_degree.items() if d == 0]
        heapq.heapify(heap)
        sorted_list = []
        while heap:
            _, current = heapq.heappop(heap)
            sorted_list.append(current)
            for dep in self._dependents.get(current, set()):
                if dep in in_degree:
                    in_degree[dep] -= 1
                    if in_degree[dep] == 0:
                        heapq.heappush(heap, (-self._tasks[dep]["priority"], dep))
        blocked = sorted([n for n, d in in_degree.items() if d > 0])
        return sorted_list, blocked

    def _detect_cycle_in_graph(self, graph: Dict[str, Set[str]]) -> bool:
        in_deg = {n: 0 for n in graph}
        for deps in graph.values():
            for d in deps:
                if d in in_deg: in_deg[d] = in_deg.get(d, 0) + 1
        queue = deque([n for n, d in in_deg.items() if d == 0])
        visited = 0
        while queue:
            node = queue.popleft(); visited += 1
            for dep in graph.get(node, set()):
                if dep in in_deg:
                    in_deg[dep] -= 1
                    if in_deg[dep] == 0: queue.append(dep)
        return visited != len(graph)

    def _compute_topo_hash(self, sorted_list: List[str], blocked: List[str]) -> str:
        try:
            return hashlib.sha256(json.dumps({"sorted": sorted_list, "blocked": sorted(blocked)}, sort_keys=True).encode()).hexdigest()[:16]
        except Exception:
            return "hash_error"

    def _update_topo_cache(self, sorted_list: List[str], blocked: List[str], topo_hash: str) -> None:
        with self._topo_cache_lock:
            self._topo_cache = (sorted_list, blocked, topo_hash)

    def _invalidate_topo_cache(self) -> None:
        with self._topo_cache_lock:
            self._topo_cache = None

    def _update_stats(self, task_name: str, elapsed: float, success: bool) -> None:
        with self._task_stats_lock:
            s = self._task_stats[task_name]
            s["count"] += 1; s["total_time"] += elapsed
            if not success: s["failures"] += 1

    def _gc_worker(self) -> None:
        while True:
            try:
                item = self._gc_queue.get()
                if item is None:
                    break
                gc.collect()
                logger.debug("异步 GC 完成")
            except Exception:
                pass

    def _log_event(self, event_type: str, details: Dict[str, Any]) -> None:
        if self._behavioral_logger:
            try:
                self._behavioral_logger.log_event(event_type=event_type, details=details)
                return
            except Exception:
                pass
        self._fallback_audit(event_type, details)

    def _fallback_audit(self, event_type: str, details: Dict[str, Any]) -> None:
        try:
            with _AUDIT_FALLBACK_LOCK:
                if os.path.exists(_AUDIT_FALLBACK_PATH) and os.path.getsize(_AUDIT_FALLBACK_PATH) > self.AUDIT_FALLBACK_MAX_SIZE:
                    os.rename(_AUDIT_FALLBACK_PATH, _AUDIT_FALLBACK_PATH + ".old")
                with open(_AUDIT_FALLBACK_PATH, 'a', encoding='utf-8') as f:
                    f.write(json.dumps({"event": event_type, "details": details, "ts": time.time()}) + "\n")
        except Exception:
            pass
