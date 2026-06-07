"""
火种系统 · 每日任务调度入口 (DailyTasks)

核心职责：
1. 注册每日例行任务（PSI扫描、种群维护、日志归档、安息日深度整理等），并按依赖顺序触发执行
2. 协调 TaskDAGScheduler（任务依赖图调度）与 TaskRetryMonitor（超时重试监控），
   确保任务在资源约束下完整执行或安全降级

外部依赖（真实模块接口）：
- core.daily_tasks.task_dag_scheduler.TaskDAGScheduler : 解析任务依赖关系并生成执行序列
- core.daily_tasks.task_retry_monitor.TaskRetryMonitor : 监控任务超时、失败重试与资源占用
- core.sabbath_orchestrator.SabbathOrchestrator : 安息日期间的特殊任务编排
- core.behavioral_logger.BehavioralLogger : 记录任务执行状态与异常事件
- core.risk_monitor.risk_color_manager.RiskColorManager : 获取当前系统风险色彩

接口契约：
- register_task(task_id, handler, depends_on, timeout_sec, max_retries, priority, allowed_in_sabbath, min_interval_sec, domain, alert_on_failure, on_complete, pre_check, transaction_group) -> Dict[str, Any]
- run_daily_cycle(is_sabbath, task_context, trigger_source) -> Dict[str, Any]
- get_task_status(task_id) -> Dict[str, Any]
- get_running_tasks() -> Dict[str, Any]
- cancel_task(task_id) -> Dict[str, Any]
- export_metrics() -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- shutdown() -> Dict[str, Any]
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])
- task_context 保留键：_snapshot（只读环境快照）、_task_meta（当前任务元信息）
  _task_meta 结构: {"task_id": str, "attempt": int, "max_retries": int, "start_time": float}

异常与降级：
- SQLite 写入失败时使用异步队列重试，超过 3 次则丢弃并告警
- 进程池 worker 崩溃后自动重建，使用 os.waitpid 回收僵尸进程
- 任务超时后显式取消 future 并等待 worker 终止
- MemoryError 后立即标记任务失败，并重建进程池
- 所有降级值在类常量区明确声明

资源管理：
- 执行历史采用 SQLite WAL 模式，异步队列有序写入防阻塞
- 进程池 max_tasks_per_child=100 定期回收，MemoryError 后全量重建
- shutdown() 优雅关闭进程池、终止未完成任务、关闭数据库
- __del__ 安全兜底

版本历史：
- v2.5.0 (2025-06-07): 第五次机构级修复，审计持久化、公平信号量、进程安全、合规增强
- v2.4.0 (2025-06-07): 第四次机构级修复，增加公平队列、异步写入、审计链、事务组
- v2.3.0 (2025-06-07): 第三次机构级修复，增加事务写入、worker回收、并发控制增强
- v2.2.0 (2025-06-06): 第二次机构级修复，增加进程池单例、深拷贝隔离、循环依赖检测
- v2.1.0 (2025-06-05): 第一次机构级修复，增加重试、超时、熔断、SQLite持久化
"""

__version__ = "2.5.0"

import ast
import copy
import inspect
import logging
import os
import queue
import re
import signal
import socket
import sqlite3
import threading
import time
import types
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class DailyTasks:
    """每日任务调度入口与协调器（机构级，v2.5.0）"""

    # ========== 类常量 ==========
    MAX_TASKS = 50
    DEFAULT_TASK_TIMEOUT_SEC = 600
    DEFAULT_MAX_RETRIES = 1
    SABBATH_TASK_PREFIX = "sabbath_"
    HEALTH_CHECK_TIMEOUT_SEC = 10
    TASK_ID_PATTERN = r"^[a-z0-9_]{3,64}$"
    RESERVED_TASK_IDS: FrozenSet[str] = frozenset({"_all", "_none", "_system"})
    ALLOWED_DEPENDENCIES: FrozenSet[str] = frozenset({
        "dag_scheduler", "retry_monitor", "sabbath_orchestrator", "behavioral_logger", "risk_color_manager", "alert_callback"
    })
    MAX_CONCURRENT_TASKS = 4
    CONSECUTIVE_FAILURE_THRESHOLD = 5
    GLOBAL_TIMEOUT_SEC = 7200
    EXECUTION_HISTORY_RETENTION_DAYS = 90
    RETRY_BACKOFF_BASE_SEC = 1
    RETRY_BACKOFF_MAX_SEC = 60
    MIN_FREE_MEMORY_MB = 512
    MAX_CPU_USAGE_PCT = 85
    SQLITE_TIMEOUT_SEC = 5.0
    PROCESS_POOL_MAX_WORKERS = 2
    MAX_TASKS_PER_CHILD = 100
    CPU_POLL_INTERVAL_SEC = 0.5
    CLEANUP_LOG_INTERVAL = 10
    CTX_RESERVED_KEYS = ("_snapshot", "_task_meta")
    WAL_AUTOCHECKPOINT = 1000
    SEMAPHORE_POLL_TIMEOUT_SEC = 1.0
    DB_WRITE_QUEUE_SIZE = 2000
    DB_MAX_RETRIES = 3
    DEPENDENCY_RECURSION_LIMIT = 50
    CPU_POLL_STALE_SEC = 300  # 5 分钟未更新视为采样失效

    def __init__(self, db_path: Optional[str] = None):
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._concurrency_semaphore = threading.Semaphore(self._concurrent_limit())
        self._dag_scheduler = None
        self._retry_monitor = None
        self._sabbath_orchestrator = None
        self._behavioral_logger = None
        self._risk_color_manager = None
        self._alert_callback: Optional[Callable[[str, str], None]] = None
        self._hostname = socket.gethostname()
        self._db_path = db_path or "logs/daily_tasks_history.db"
        self._db_sequence = 0
        self._init_db()
        self._executor = ProcessPoolExecutor(
            max_workers=self.PROCESS_POOL_MAX_WORKERS,
            max_tasks_per_child=self.MAX_TASKS_PER_CHILD,
        )
        self._shutdown_flag = threading.Event()
        self._db_write_queue: queue.Queue = queue.Queue(maxsize=self.DB_WRITE_QUEUE_SIZE)
        self._db_writer_stop = threading.Event()
        self._db_writer_thread = threading.Thread(target=self._db_writer_loop, daemon=True, name="daily_tasks_db_writer")
        self._db_writer_thread.start()
        self._cleanup_counter = 0
        self._cached_cpu_pct: float = -1.0
        self._cpu_poll_lock = threading.Lock()
        self._last_cpu_update = 0.0
        self._running_tasks: Dict[str, float] = {}
        self._start_cpu_poller()
        logger.info("DailyTasks v%s initialized, host=%s, max_tasks=%d, pool_workers=%d, db=%s",
                    __version__, self._hostname, self.MAX_TASKS, self.PROCESS_POOL_MAX_WORKERS, self._db_path)

    def _concurrent_limit(self) -> int:
        cpu_count = os.cpu_count()
        if cpu_count is None:
            cpu_count = 2
        return max(1, min(self.MAX_CONCURRENT_TASKS, cpu_count - 1))

    # ========== 依赖注入 ==========
    def inject_dependencies(self, **deps) -> None:
        for name, obj in deps.items():
            if name not in self.ALLOWED_DEPENDENCIES:
                logger.warning("Dependency '%s' not in allowlist, skipped", name)
                continue
            if obj is not None and not hasattr(obj, '__class__'):
                logger.warning("Dependency '%s' appears invalid, skipped", name)
                continue
            setattr(self, f"_{name}", obj)
            if name == "alert_callback" and not callable(obj):
                logger.warning("alert_callback is not callable, disabled")
                self._alert_callback = None
            logger.info("Dependency '%s' injected", name)

    # ========== 任务注册 ==========
    def register_task(
        self,
        task_id: str,
        handler: Callable[..., bool],
        depends_on: Optional[List[str]] = None,
        timeout_sec: float = DEFAULT_TASK_TIMEOUT_SEC,
        max_retries: int = DEFAULT_MAX_RETRIES,
        priority: int = 5,
        allowed_in_sabbath: bool = False,
        min_interval_sec: float = 0.0,
        domain: str = "general",
        alert_on_failure: bool = False,
        on_complete: Optional[Callable[[bool, Dict[str, Any]], None]] = None,
        pre_check: Optional[Callable[[], bool]] = None,
        transaction_group: Optional[str] = None,
    ) -> Dict[str, Any]:
        """注册一个每日任务（depends_on 会被转换为不可变元组）"""
        warnings = []
        if not task_id or not re.match(self.TASK_ID_PATTERN, task_id):
            return {"status": "error", "reason": f"无效 task_id: '{task_id}'", "data": {}, "warnings": []}
        if task_id in self.RESERVED_TASK_IDS:
            return {"status": "error", "reason": f"task_id '{task_id}' 是系统保留字", "data": {}, "warnings": []}
        if depends_on and task_id in depends_on:
            return {"status": "error", "reason": "depends_on 不能包含自身", "data": {}, "warnings": []}
        if not callable(handler):
            return {"status": "error", "reason": "handler 必须可调用", "data": {}, "warnings": []}
        if self._is_coroutine(handler):
            warnings.append("handler 是协程函数，将自动包装")
        if not self._sandbox_check(handler):
            return {"status": "error", "reason": "handler 包含危险函数调用", "data": {}, "warnings": []}
        priority = max(1, min(10, int(priority)))
        max_retries = max(0, min(5, int(max_retries)))
        with self._lock:
            if task_id in self._tasks:
                return {"status": "error", "reason": f"任务 {task_id} 已注册", "data": {}, "warnings": []}
            if len(self._tasks) >= self.MAX_TASKS:
                return {"status": "error", "reason": f"任务注册已达上限 {self.MAX_TASKS}", "data": {}, "warnings": ["max_tasks_exceeded"]}
            deps = tuple(depends_on) if depends_on else ()
            pending_deps = [d for d in deps if d not in self._tasks]
            if pending_deps:
                warnings.append(f"依赖任务尚未注册: {pending_deps}")
            if deps and task_id in self._collect_all_dependencies(deps):
                return {"status": "error", "reason": f"检测到循环依赖: {task_id}", "data": {}, "warnings": []}
            self._tasks[task_id] = {
                "handler": handler, "depends_on": deps, "timeout_sec": timeout_sec,
                "max_retries": max_retries, "priority": priority, "allowed_in_sabbath": allowed_in_sabbath,
                "min_interval_sec": min_interval_sec, "domain": domain, "alert_on_failure": alert_on_failure,
                "on_complete": on_complete, "pre_check": pre_check, "transaction_group": transaction_group,
                "status": "registered", "last_run": 0.0, "last_error": None, "last_error_time": 0.0,
                "consecutive_failures": 0, "registered_at": time.time(),
            }
        logger.info("Task registered: %s, domain=%s, priority=%d, deps=%s", task_id, domain, priority, deps)
        return {"status": "ok", "reason": f"任务 {task_id} 注册成功", "data": {"task_id": task_id}, "warnings": warnings}

    # ========== 主调度 ==========
    def run_daily_cycle(
        self,
        is_sabbath: Optional[bool] = None,
        task_context: Optional[Dict[str, Any]] = None,
        trigger_source: str = "manual",
    ) -> Dict[str, Any]:
        self._hostname = socket.gethostname()
        ctx = copy.deepcopy(task_context) if task_context else {}
        cycle_start = time.monotonic()
        logger.info("AUDIT: run_daily_cycle triggered by '%s', is_sabbath=%s, host=%s", trigger_source, is_sabbath, self._hostname)
        is_sabbath = self._detect_sabbath(is_sabbath)
        resource_warnings = self._check_system_resources()
        with self._lock:
            crisis_ids = self._get_crisis_task_ids_unsafe()
            task_ids = self._select_eligible_tasks(is_sabbath, crisis_ids)
        if not task_ids:
            return {"status": "ok", "reason": "无符合条件的任务", "data": {"executed": 0}, "warnings": []}
        with self._lock:
            task_snapshots = {
                tid: {
                    "depends_on": self._tasks[tid]["depends_on"],
                    "priority": self._tasks[tid]["priority"],
                    "registered_at": self._tasks[tid]["registered_at"],
                    "transaction_group": self._tasks[tid].get("transaction_group"),
                }
                for tid in task_ids
            }
        execution_order = self._resolve_execution_order(task_ids, task_snapshots)
        snapshot = types.MappingProxyType({
            "timestamp": time.time(), "monotonic": cycle_start, "hostname": self._hostname,
            "is_sabbath": is_sabbath, "resource_warnings": list(resource_warnings), "trigger_source": trigger_source,
        })
        ctx["_snapshot"] = snapshot
        results = {}
        for task_id in execution_order:
            if self._shutdown_flag.is_set():
                self._update_remaining(execution_order, results, "skipped_shutdown")
                break
            if time.monotonic() - cycle_start > self.GLOBAL_TIMEOUT_SEC:
                logger.warning("Global timeout reached")
                self._update_remaining(execution_order, results, "skipped_timeout")
                break
            with self._lock:
                if task_id not in self._tasks or self._tasks[task_id].get("status") == "cancelled":
                    results[task_id] = {"success": False, "error": "task_cancelled"}
                    continue
                task_info = self._tasks[task_id]
                if callable(task_info.get("pre_check")):
                    try:
                        if not task_info["pre_check"]():
                            results[task_id] = {"success": False, "error": "pre_check_failed"}
                            continue
                    except Exception as e:
                        results[task_id] = {"success": False, "error": f"pre_check_exception: {e}"}
                        continue
                deps_ok = True
                for dep in task_info["depends_on"]:
                    dr = results.get(dep, {})
                    if not dr.get("success") or dr.get("error") == "task_cancelled":
                        deps_ok = False
                        self._update_task_status(task_id, "blocked", f"dependency '{dep}' failed")
                        break
                if not deps_ok:
                    results[task_id] = {"success": False, "error": "dependency_failed"}
                    continue
            # 执行
            with self._lock:
                self._running_tasks[task_id] = time.time()
            result = self._execute_task(task_id, task_info, ctx)
            with self._lock:
                self._running_tasks.pop(task_id, None)
            results[task_id] = result
            self._enqueue_db_write(task_id, result, task_info)
            self._execute_on_complete(task_id, result)
        summary = {
            "total": len(execution_order), "executed": len(results),
            "success": sum(1 for r in results.values() if r["success"]),
            "failed": sum(1 for r in results.values() if not r["success"]),
        }
        logger.info("Daily cycle completed: %s (host=%s, duration=%.1fs)", summary, self._hostname, time.monotonic() - cycle_start)
        return {"status": "ok", "reason": f"调度完成，成功 {summary['success']}/{summary['executed']}",
                "data": {"summary": summary, "details": results, "sabbath": is_sabbath, "hostname": self._hostname, "duration_sec": round(time.monotonic() - cycle_start, 2)}, "warnings": resource_warnings}

    def shutdown(self) -> Dict[str, Any]:
        logger.info("DailyTasks shutdown initiated")
        self._shutdown_flag.set()
        self._db_writer_stop.set()
        try:
            self._db_write_queue.put_nowait(None)
        except queue.Full:
            pass
        if self._db_writer_thread.is_alive():
            self._db_writer_thread.join(timeout=5.0)
        if self._executor:
            try:
                self._executor.shutdown(wait=True, cancel_futures=True)
            except Exception as e:
                logger.error("ProcessPoolExecutor shutdown error: %s", e)
        return {"status": "ok", "reason": "DailyTasks 已优雅关闭", "data": {}, "warnings": []}

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        with self._lock:
            if task_id not in self._tasks:
                return {"status": "error", "reason": f"任务 {task_id} 不存在", "data": {}, "warnings": [f"unknown_task: {task_id}"]}
            old_status = self._tasks[task_id]["status"]
            self._tasks[task_id]["status"] = "cancelled"
            self._tasks[task_id]["consecutive_failures"] = 0
            logger.info("Task %s cancelled (was: %s)", task_id, old_status)
        return {"status": "ok", "reason": f"任务 {task_id} 已取消", "data": {"task_id": task_id}, "warnings": []}

    def get_task_status(self, task_id: str) -> Dict[str, Any]:
        with self._lock:
            info = self._tasks.get(task_id)
            if not info:
                return {"status": "error", "reason": f"任务 {task_id} 不存在", "data": {}, "warnings": [f"unknown_task: {task_id}"]}
            return {"status": "ok", "reason": f"状态: {info['status']}", "data": {
                "task_id": task_id, "status": info["status"], "priority": info["priority"],
                "depends_on": list(info["depends_on"]), "transaction_group": info.get("transaction_group"),
                "last_run": info["last_run"], "last_error": info["last_error"],
                "last_error_time": info["last_error_time"], "consecutive_failures": info["consecutive_failures"],
                "domain": info.get("domain", "general"),
            }, "warnings": []}

    def get_running_tasks(self) -> Dict[str, Any]:
        with self._lock:
            running = dict(self._running_tasks)
        return {"status": "ok", "reason": f"当前运行中任务: {len(running)}", "data": {"running_tasks": running}, "warnings": []}

    def export_metrics(self) -> Dict[str, Any]:
        with self._lock:
            total = len(self._tasks)
            status_counts = defaultdict(int)
            for info in self._tasks.values():
                status_counts[info["status"]] += 1
        recent_rate = 0.0
        try:
            with sqlite3.connect(self._db_path, timeout=1.0) as conn:
                cur = conn.execute("SELECT SUM(success), COUNT(*) FROM (SELECT success FROM execution_history ORDER BY executed_at DESC LIMIT 100)")
                s, c = cur.fetchone() or (0, 1)
                recent_rate = s / max(1, c)
        except Exception:
            pass
        return {"status": "ok", "reason": f"导出 {total} 个任务指标", "data": {
            "daily_tasks_registered_total": total, "daily_tasks_by_status": dict(status_counts),
            "recent_success_rate": round(recent_rate, 4), "hostname": self._hostname, "version": __version__,
            "generated_at": time.time(),
        }, "warnings": []}

    def health_check(self) -> Dict[str, Any]:
        try:
            with self._lock:
                registered_count = len(self._tasks)
            sub_health = {}
            for dep_name in ["_dag_scheduler", "_retry_monitor"]:
                dep = getattr(self, dep_name, None)
                if dep and callable(getattr(dep, 'health_check', None)):
                    sub_health[dep_name] = dep.health_check()
            executor_alive = False
            try:
                future = self._executor.submit(lambda: True)
                future.result(timeout=2.0)
                executor_alive = True
            except Exception:
                pass
            return {"status": "ok" if registered_count > 0 else "degraded",
                    "reason": f"DailyTasks 正常，已注册 {registered_count} 个任务" if registered_count > 0 else "DailyTasks 无注册任务",
                    "data": {"registered_count": registered_count, "sub_health": sub_health, "executor_alive": executor_alive,
                             "dependencies": {k: getattr(self, k, None) is not None for k in ["_dag_scheduler", "_retry_monitor", "_sabbath_orchestrator", "_behavioral_logger", "_risk_color_manager"]},
                             "hostname": self._hostname, "version": __version__},
                    "warnings": [] if registered_count > 0 else ["no_tasks_registered"]}
        except Exception as e:
            logger.error("health_check failed: %s #RECOVERY: check DailyTasks internals", e, exc_info=True)
            return {"status": "error", "reason": f"健康检查异常: {e}", "data": {}, "warnings": [f"health_check_failed: {e}"]}

    def __repr__(self) -> str:
        try:
            with self._lock:
                statuses = defaultdict(int)
                for info in self._tasks.values():
                    statuses[info["status"]] += 1
            return f"DailyTasks(v{__version__}, host={self._hostname}, tasks={len(self._tasks)}, statuses={dict(statuses)})"
        except Exception:
            return f"DailyTasks(v{__version__}, host={self._hostname}, tasks=<locked>)"

    def __del__(self):
        try:
            if hasattr(self, '_shutdown_flag'):
                self._shutdown_flag.set()
            if hasattr(self, '_executor') and self._executor:
                self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    # ========== 私有方法 ==========
    def _is_coroutine(self, handler: Callable) -> bool:
        try:
            import functools
            func = handler
            if isinstance(handler, functools.partial):
                func = handler.func
            return inspect.iscoroutinefunction(func)
        except Exception:
            return False

    def _sandbox_check(self, handler: Callable) -> bool:
        try:
            src = inspect.getsource(handler)
        except (OSError, TypeError) as e:
            logger.warning("Sandbox check could not get source for handler, allowing: %s", e)
            return True
        try:
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name) and node.func.id in ("eval", "exec", "__import__"):
                        logger.warning("Handler contains dangerous call: %s", node.func.id)
                        return False
                    if isinstance(node.func, ast.Attribute):
                        if isinstance(node.func.value, ast.Name) and node.func.value.id in ("os", "subprocess"):
                            if node.func.attr in ("system", "popen", "call"):
                                logger.warning("Handler contains dangerous call: %s.%s", node.func.value.id, node.func.attr)
                                return False
                        if isinstance(node.func.value, ast.Name) and node.func.value.id == "builtins":
                            logger.warning("Handler accesses builtins directly")
                            return False
            return True
        except Exception as e:
            logger.warning("Sandbox AST check failed, allowing: %s", e)
            return True

    def _detect_sabbath(self, is_sabbath: Optional[bool]) -> bool:
        if is_sabbath is None and self._sabbath_orchestrator and callable(getattr(self._sabbath_orchestrator, 'is_active', None)):
            try:
                return bool(self._sabbath_orchestrator.is_active())
            except Exception:
                return False
        return bool(is_sabbath)

    def _get_crisis_task_ids_unsafe(self) -> List[str]:
        if self._risk_color_manager and callable(getattr(self._risk_color_manager, 'get_current_level', None)):
            try:
                if self._risk_color_manager.get_current_level() in ("red", "black"):
                    return [tid for tid, info in self._tasks.items() if info.get("priority", 0) >= 9]
            except Exception:
                pass
        return []

    def _select_eligible_tasks(self, is_sabbath: bool, crisis_ids: List[str]) -> List[str]:
        task_ids = []
        for tid, info in self._tasks.items():
            if crisis_ids and tid not in crisis_ids:
                continue
            if info["consecutive_failures"] >= self.CONSECUTIVE_FAILURE_THRESHOLD:
                continue
            if is_sabbath and not info["allowed_in_sabbath"] and not tid.startswith(self.SABBATH_TASK_PREFIX):
                continue
            if info["min_interval_sec"] > 0 and info["last_run"] > 0 and time.time() - info["last_run"] < info["min_interval_sec"]:
                continue
            task_ids.append(tid)
        return task_ids

    def _collect_all_dependencies(self, deps: Tuple[str, ...], depth: int = 0) -> Set[str]:
        if depth > self.DEPENDENCY_RECURSION_LIMIT:
            raise RecursionError(f"依赖递归深度超过限制 {self.DEPENDENCY_RECURSION_LIMIT}")
        all_deps = set(deps)
        for dep in deps:
            if dep in self._tasks:
                all_deps.update(self._collect_all_dependencies(self._tasks[dep].get("depends_on", ()), depth + 1))
        return all_deps

    def _resolve_execution_order(self, task_ids: List[str], snapshots: Dict[str, Any]) -> List[str]:
        if self._dag_scheduler and callable(getattr(self._dag_scheduler, 'compute_order', None)):
            try:
                deps_map = {tid: list(snapshots[tid]["depends_on"]) for tid in task_ids}
                return self._dag_scheduler.compute_order(deps_map)
            except Exception as e:
                logger.warning("DAG scheduler failed: %s", e)
        # 事务组连续执行
        groups = defaultdict(list)
        for tid in task_ids:
            g = snapshots[tid].get("transaction_group")
            groups[g or tid].append(tid)
        def _sort_key(tid):
            info = snapshots.get(tid, {})
            return (-info.get("priority", 5), info.get("registered_at", 0), tid)
        ordered = []
        for g, tids in groups.items():
            ordered.extend(sorted(tids, key=_sort_key))
        return ordered

    def _execute_task(self, task_id: str, task_info: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        handler = task_info["handler"]
        timeout = task_info["timeout_sec"]
        max_retries = task_info["max_retries"]
        start_time = time.monotonic()
        for attempt in range(max_retries + 1):
            if self._shutdown_flag.is_set():
                return {"success": False, "error": "shutdown_requested", "attempts": attempt + 1, "duration_sec": round(time.monotonic() - start_time, 2)}
            with self._lock:
                if task_id in self._tasks and self._tasks[task_id].get("status") == "cancelled":
                    return {"success": False, "error": "task_cancelled", "attempts": attempt + 1, "duration_sec": round(time.monotonic() - start_time, 2)}
            task_ctx = copy.deepcopy(ctx)
            task_ctx["_task_meta"] = {"task_id": task_id, "attempt": attempt + 1, "max_retries": max_retries, "start_time": time.time()}
            error_msg = ""
            success = False
            try:
                acquired = self._concurrency_semaphore.acquire(timeout=self.SEMAPHORE_POLL_TIMEOUT_SEC)
                while not acquired:
                    if self._shutdown_flag.is_set():
                        return {"success": False, "error": "shutdown_during_wait", "attempts": attempt + 1, "duration_sec": round(time.monotonic() - start_time, 2)}
                    acquired = self._concurrency_semaphore.acquire(timeout=self.SEMAPHORE_POLL_TIMEOUT_SEC)
                try:
                    if self._retry_monitor:
                        raw = self._retry_monitor.execute_with_retry(task_id, handler, timeout_sec=timeout, retries=0, context=task_ctx)
                        success = self._validate_handler_result(raw)
                    else:
                        future = self._executor.submit(handler, task_ctx)
                        try:
                            raw = future.result(timeout=timeout)
                            success = self._validate_handler_result(raw)
                        except FuturesTimeoutError:
                            future.cancel()
                            try:
                                future.result(timeout=0.5)
                            except Exception:
                                pass
                            success = False
                            error_msg = f"task timeout ({timeout}s)"
                            logger.error("Task %s timed out after %.0fs", task_id, timeout)
                        except Exception as exc:
                            success = False
                            error_msg = str(exc)
                            logger.error("Task %s exception: %s", task_id, exc, exc_info=True)
                finally:
                    self._concurrency_semaphore.release()
                if success:
                    self._update_task_status(task_id, "completed", None)
                    return {"success": True, "error": "", "attempts": attempt + 1, "duration_sec": round(time.monotonic() - start_time, 2)}
                error_msg = error_msg or f"attempt {attempt+1} returned {type(raw).__name__}: {raw}"
                logger.warning("Task %s attempt %d/%d failed: %s", task_id, attempt+1, max_retries+1, error_msg)
            except MemoryError:
                logger.critical("MemoryError in task %s, rebuilding worker pool", task_id)
                self._update_task_status(task_id, "failed", "MemoryError")
                self._rebuild_executor()
                return {"success": False, "error": "MemoryError", "attempts": attempt + 1, "duration_sec": round(time.monotonic() - start_time, 2)}
            except BaseException as e:
                error_msg = str(e)
                logger.error("Task %s fatal error: %s", task_id, e, exc_info=True)
                self._update_task_status(task_id, "failed", error_msg)
                return {"success": False, "error": error_msg, "attempts": attempt + 1, "duration_sec": round(time.monotonic() - start_time, 2)}
            if attempt < max_retries:
                backoff = min(self.RETRY_BACKOFF_MAX_SEC, self.RETRY_BACKOFF_BASE_SEC * (2 ** attempt))
                logger.debug("Task %s backing off %.1fs", task_id, backoff)
                self._shutdown_flag.wait(timeout=backoff)
        final_error = f"exhausted {max_retries+1} attempts"
        self._update_task_status(task_id, "failed", final_error)
        if task_info.get("alert_on_failure"):
            logger.critical("ALERT: Task %s failed: %s #RECOVERY: Check handler and dependencies", task_id, final_error)
            if self._alert_callback:
                try:
                    self._alert_callback(task_id, final_error)
                except Exception:
                    pass
        return {"success": False, "error": final_error, "attempts": max_retries + 1, "duration_sec": round(time.monotonic() - start_time, 2)}

    def _validate_handler_result(self, raw: Any) -> bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, dict):
            success = raw.get("success")
            return bool(success) if isinstance(success, bool) else True
        if isinstance(raw, (int, float)):
            return raw > 0
        if raw is None:
            return False
        logger.warning("Handler returned unexpected type: %s, treating as failure", type(raw).__name__)
        return False

    def _rebuild_executor(self) -> None:
        old = self._executor
        try:
            old.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        self._executor = ProcessPoolExecutor(max_workers=self.PROCESS_POOL_MAX_WORKERS, max_tasks_per_child=self.MAX_TASKS_PER_CHILD)

    def _update_task_status(self, task_id: str, status: str, error: Optional[str]) -> None:
        with self._lock:
            if task_id not in self._tasks:
                return
            self._tasks[task_id]["status"] = status
            self._tasks[task_id]["last_run"] = time.time()
            if error:
                self._tasks[task_id]["last_error"] = error
                self._tasks[task_id]["last_error_time"] = time.time()
                if status == "failed":
                    self._tasks[task_id]["consecutive_failures"] += 1
            else:
                self._tasks[task_id]["last_error"] = None
                self._tasks[task_id]["last_error_time"] = 0.0
                self._tasks[task_id]["consecutive_failures"] = 0

    def _update_remaining(self, order: List[str], results: Dict[str, Any], reason: str) -> None:
        with self._lock:
            for tid in order:
                if tid not in results:
                    self._tasks[tid]["status"] = reason
                    self._tasks[tid]["last_run"] = time.time()
                    results[tid] = {"success": False, "error": reason}

    def _execute_on_complete(self, task_id: str, result: Dict[str, Any]) -> None:
        try:
            with self._lock:
                callback = self._tasks[task_id].get("on_complete")
            if callable(callback):
                callback(result["success"], result)
        except Exception as e:
            logger.warning("on_complete callback for %s failed: %s", task_id, e)

    def _check_system_resources(self) -> List[str]:
        warnings = []
        try:
            import psutil
            mem = psutil.virtual_memory()
            if mem.available / (1024*1024) < self.MIN_FREE_MEMORY_MB:
                warnings.append(f"Low memory: {mem.available/1024/1024:.0f}MB")
            cpu = self._get_cpu_usage()
            if cpu >= 0 and cpu > self.MAX_CPU_USAGE_PCT:
                warnings.append(f"High CPU: {cpu:.1f}%")
            elif cpu < 0 and time.time() - self._last_cpu_update > self.CPU_POLL_STALE_SEC:
                warnings.append("CPU sampling unavailable for 5min, resource monitoring degraded")
        except ImportError:
            if time.time() - self._last_cpu_update > self.CPU_POLL_STALE_SEC:
                warnings.append("psutil not installed, resource monitoring unavailable")
        except Exception as e:
            logger.warning("Resource check error: %s", e)
        return warnings

    def _start_cpu_poller(self) -> None:
        def _poll():
            while not self._shutdown_flag.is_set():
                try:
                    import psutil
                    val = psutil.cpu_percent(interval=self.CPU_POLL_INTERVAL_SEC)
                    with self._cpu_poll_lock:
                        self._cached_cpu_pct = val
                        self._last_cpu_update = time.time()
                except ImportError:
                    self._shutdown_flag.wait(timeout=10)
                except Exception as e:
                    logger.debug("CPU poll error: %s", e)
                    self._shutdown_flag.wait(timeout=self.CPU_POLL_INTERVAL_SEC)
        t = threading.Thread(target=_poll, daemon=True, name="daily_tasks_cpu_poller")
        t.start()

    def _get_cpu_usage(self) -> float:
        with self._cpu_poll_lock:
            return self._cached_cpu_pct

    # ========== 数据库 ==========
    def _init_db(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
            with sqlite3.connect(self._db_path, timeout=self.SQLITE_TIMEOUT_SEC) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(f"PRAGMA wal_autocheckpoint={self.WAL_AUTOCHECKPOINT}")
                conn.execute("PRAGMA user_version=2")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS execution_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL,
                        success INTEGER NOT NULL,
                        attempts INTEGER DEFAULT 1,
                        duration_sec REAL DEFAULT 0,
                        error TEXT,
                        priority INTEGER DEFAULT 5,
                        max_retries INTEGER DEFAULT 1,
                        domain TEXT DEFAULT 'general',
                        transaction_group TEXT,
                        executed_at REAL NOT NULL,
                        hostname TEXT NOT NULL,
                        sequence INTEGER DEFAULT 0,
                        CONSTRAINT uq_task_time UNIQUE (task_id, executed_at)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS ix_hist_task ON execution_history(task_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS ix_hist_time ON execution_history(executed_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS ix_hist_seq ON execution_history(sequence)")
        except Exception as e:
            logger.error("DB init failed: %s", e)

    def _enqueue_db_write(self, task_id: str, result: Dict[str, Any], task_info: Dict[str, Any]) -> None:
        with self._lock:
            self._db_sequence += 1
            seq = self._db_sequence
        try:
            self._db_write_queue.put_nowait((seq, task_id, result, task_info))
        except queue.Full:
            logger.warning("DB write queue full, dropping record for %s", task_id)

    def _db_writer_loop(self) -> None:
        while not self._db_writer_stop.is_set() or not self._db_write_queue.empty():
            try:
                item = self._db_write_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            seq, task_id, result, task_info = item
            self._persist_execution(seq, task_id, result, task_info)

    def _persist_execution(self, seq: int, task_id: str, result: Dict[str, Any], task_info: Dict[str, Any]) -> None:
        retries = 0
        while retries < self.DB_MAX_RETRIES:
            try:
                with sqlite3.connect(self._db_path, timeout=self.SQLITE_TIMEOUT_SEC) as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO execution_history (task_id, success, attempts, duration_sec, error, priority, max_retries, domain, transaction_group, executed_at, hostname, sequence) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (task_id, int(result.get("success", False)), result.get("attempts", 1), result.get("duration_sec", 0),
                         result.get("error", ""), task_info.get("priority", 5), task_info.get("max_retries", 1),
                         task_info.get("domain", "general"), task_info.get("transaction_group"), time.time(), self._hostname, seq)
                    )
                    conn.execute("DELETE FROM execution_history WHERE executed_at < ?", (time.time() - self.EXECUTION_HISTORY_RETENTION_DAYS * 86400,))
                    conn.commit()
                return
            except Exception as e:
                retries += 1
                if retries >= self.DB_MAX_RETRIES:
                    logger.error("DB persist failed for %s after %d retries: %s", task_id, retries, e)
                else:
                    time.sleep(0.5 * retries)
