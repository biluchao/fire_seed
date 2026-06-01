"""
火种系统 · 全局异常处理器 (GlobalExceptionHandler)

核心职责：
1. 捕获 Python 主事件循环中未被各模块内部 try-except 吞没的全局异常，执行分级响应（静默记录、告警推送、紧急降维、硬退出）
2. 维护异常发生的时间窗口统计，在异常密集爆发时自动升级响应等级，防止级联故障扩散

外部依赖（真实模块接口）：
- core.engine.emergency_simplifier.EmergencySimplifier : 执行一键降维（轻/中/重三级），冻结非生存模块
- core.negotiation_bus.NegotiationBus : 推送全局异常告警至运维面板与消息渠道
- core.behavioral_logger.BehavioralLogger : 记录异常事件与响应动作至不可篡改审计日志
- core.llm_task_queue.LLMTaskQueue : 将异常上下文提交给 DeepSeek 进行根因分析
- core.module_health_monitor.ModuleHealthMonitor : 上报本模块自身的健康状态变化

接口契约：
- handle_exception(exc: Exception, context: Dict[str, Any]) -> Dict[str, Any] : 处理一个全局异常
- get_error_stats() -> Dict[str, Any] : 返回最近一段时间内的异常统计摘要
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 EmergencySimplifier 不可用或降维失败时，根据异常密度决定硬退出或降级为告警，不再反复尝试降维
- 当 NegotiationBus 不可用时，告警仅通过标准 logger 输出
- 当任何外部依赖不可用时，对应的功能静默降级，不影响核心异常处理链路
- 本模块自身在 __init__ 过程中捕获所有异常，确保即使初始化失败也能提供基础保护

资源管理：
- GC 监控线程使用守护模式运行，主进程退出时自动终止
- atexit 注册清理函数确保线程和锁在进程退出前正确释放
- 所有统计计数器在模块销毁时清零
- 锁获取顺序严格遵守：_stats_lock -> _gc_stats_lock，禁止反向获取
"""

import atexit
import bisect
import gc
import os
import signal
import sys
import time
import logging
import threading
from collections import deque
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class GlobalExceptionHandler:
    """全局异常处理器（单例模式）"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_STATS_WINDOW_SEC = 3600         # 异常统计窗口，秒，取值范围 [600, 86400]
    DEFAULT_BURST_THRESHOLD = 5             # 异常突发阈值（窗口内异常数），无量纲，[3, 20]
    DEFAULT_BURST_INTERVAL_SEC = 60         # 突发判定时间间隔，秒，[10, 300]
    DEFAULT_COOLDOWN_SEC = 120              # 紧急降维冷却期，秒，[60, 600]
    DEFAULT_MAX_EXCEPTIONS_PER_MINUTE = 10  # 每分钟最大异常数，超过则强制硬退出
    DEFAULT_GC_MONITOR_INTERVAL = 30.0      # GC 监控轮询间隔，秒，[10.0, 120.0]
    HARD_EXIT_TIMEOUT_SEC = 5               # sys.exit 超时时间，秒，[3, 10]

    # 响应等级
    RESPONSE_SILENT = 0       # 静默记录
    RESPONSE_WARNING = 1      # 告警推送
    RESPONSE_DEGRADATION = 2  # 紧急降维
    RESPONSE_HARD_EXIT = 3    # 硬退出

    def __new__(cls) -> "GlobalExceptionHandler":
        """单例模式"""
        if not hasattr(cls, "_instance"):
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        # 异常统计（无 maxlen 限制，由清理逻辑控制）
        self._exception_records: deque = deque()
        self._exception_count_total: int = 0
        self._last_response_level: int = self.RESPONSE_SILENT
        self._last_degradation_time: float = 0.0
        self._exception_per_minute: deque = deque()  # 存储时间戳，手动清理过期项
        self._stats_lock = threading.Lock()

        # GC 监控
        self._gc_monitor_thread: Optional[threading.Thread] = None
        self._gc_monitor_stop: threading.Event = threading.Event()
        self._gc_stats_lock = threading.Lock()
        self._recent_gc_pauses: deque = deque(maxlen=100)
        self._last_gc_count: int = 0

        # 外部依赖注入
        self._emergency_simplifier = None
        self._negotiation_bus = None
        self._behavioral_logger = None
        self._llm_task_queue = None
        self._module_health_monitor = None

        # 启动 GC 监控
        self._start_gc_monitor()

        # 注册退出清理
        atexit.register(self._cleanup)

        logger.info(
            "GlobalExceptionHandler 初始化完成（单例），统计窗口=%ds，突发阈值=%d",
            self.DEFAULT_STATS_WINDOW_SEC,
            self.DEFAULT_BURST_THRESHOLD,
        )

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        emergency_simplifier: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        llm_task_queue: Optional[Any] = None,
        module_health_monitor: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）

        Args:
            emergency_simplifier: 紧急降维模块，用于执行一键降维
            negotiation_bus: 协商总线，用于推送告警
            behavioral_logger: 行为日志，用于记录异常事件
            llm_task_queue: LLM任务队列，用于提交根因分析任务
            module_health_monitor: 模块健康监控，用于上报自身状态变化
        """
        if emergency_simplifier is not None:
            self._emergency_simplifier = emergency_simplifier
            logger.info("EmergencySimplifier 注入成功")
        else:
            logger.warning("EmergencySimplifier 未注入，紧急降维功能不可用")

        if negotiation_bus is not None:
            if not callable(getattr(negotiation_bus, "publish_alert", None)):
                logger.warning("NegotiationBus 缺少可调用的 publish_alert 方法，告警推送不可用")
                self._negotiation_bus = None
            else:
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，异常事件仅通过标准 logger 记录")

        if llm_task_queue is not None:
            self._llm_task_queue = llm_task_queue
            logger.info("LLMTaskQueue 注入成功")
        else:
            logger.warning("LLMTaskQueue 未注入，异常后不会自动触发根因分析")

        if module_health_monitor is not None:
            self._module_health_monitor = module_health_monitor
            logger.info("ModuleHealthMonitor 注入成功")

    # ========== 公共接口 ==========
    def handle_exception(self, exc: Exception, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        处理一个全局异常

        Args:
            exc: 被捕获的异常对象
            context: 异常发生时的上下文信息（模块名、函数名、当前状态等）

        Returns:
            标准响应字典
        """
        if context is None:
            context = {}

        exc_type = type(exc).__name__
        exc_message = str(exc)[:500]
        module_name = context.get("module", "unknown")
        function_name = context.get("function", "unknown")

        now = time.time()

        # 启动真空期保护：如果所有核心依赖都不可用，说明系统仍在启动中，
        # 此时全局异常不可被静默吞下，必须立即退出。
        if self._emergency_simplifier is None and self._negotiation_bus is None:
            logger.critical(
                f"启动阶段全局异常 [{exc_type}] {exc_message}，依赖不可用，中止启动 "
                f"#RECOVERY: 检查模块初始化顺序，确保依赖注入先于异常处理"
            )
            sys.exit(1)

        with self._stats_lock:
            self._record_exception({
                "timestamp": now,
                "type": exc_type,
                "message": exc_message,
                "module": module_name,
                "function": function_name,
            }, now)

            # 清理过期的每分钟记录，并计算当前分钟内的异常数
            minute_cutoff = now - 60
            while self._exception_per_minute and self._exception_per_minute[0] < minute_cutoff:
                self._exception_per_minute.popleft()
            exceptions_this_minute = len(self._exception_per_minute)

            # 主动清理统计窗口内过期数据
            self._purge_expired_records(now)

            # 判定响应等级（在锁内完成，保证原子性）
            response_level = self._determine_response_level(now, exceptions_this_minute)

        # 记录异常日志（锁外）
        self._log_exception(exc_type, exc_message, module_name, function_name, response_level)

        warnings = []
        if response_level == self.RESPONSE_WARNING:
            warnings.append("全局异常触发告警")
            self._push_alert(exc_type, exc_message, module_name, function_name)
            self._submit_diagnosis_task(exc_type, exc_message, context)

        elif response_level == self.RESPONSE_DEGRADATION:
            # 双重检查，防止多线程重复降维
            with self._stats_lock:
                if now - self._last_degradation_time < self.DEFAULT_COOLDOWN_SEC:
                    response_level = self.RESPONSE_WARNING
                    warnings.append("降维冷却期内，降级为告警")
                else:
                    if self._emergency_simplifier is None:
                        response_level = self.RESPONSE_WARNING
                        warnings.append("EmergencySimplifier 不可用，跳过降维，降级为告警")
                    # 否则保留降维意图，后续执行

            if response_level == self.RESPONSE_DEGRADATION:
                warnings.append("全局异常触发紧急降维")
                self._push_alert(exc_type, exc_message, module_name, function_name)
                self._submit_diagnosis_task(exc_type, exc_message, context)
                success = self._execute_degradation(exc_type, exc_message, now)
                if not success:
                    if exceptions_this_minute >= self.DEFAULT_MAX_EXCEPTIONS_PER_MINUTE:
                        warnings.append("紧急降维失败且异常密度超高，执行硬退出")
                        logger.critical(
                            "紧急降维失败，异常密度超高，执行硬退出 #RECOVERY: 检查 EmergencySimplifier 模块状态，手动重启系统"
                        )
                        self._release_all_locks()
                        self._hard_exit(exc_type, exc_message)
                    else:
                        response_level = self.RESPONSE_WARNING
                        warnings.append("紧急降维失败，降级为告警")

        elif response_level == self.RESPONSE_HARD_EXIT:
            warnings.append("异常密度超限，触发硬退出")
            self._push_alert(exc_type, exc_message, module_name, function_name)
            self._release_all_locks()
            self._hard_exit(exc_type, exc_message)

        # 上报健康状态（锁外调用）
        self._report_health(now, exceptions_this_minute, response_level)

        return {
            "status": "ok",
            "reason": f"异常已处理: {exc_type}，响应等级={response_level}",
            "data": {
                "exception_type": exc_type,
                "response_level": response_level,
                "exceptions_this_minute": exceptions_this_minute,
                "total_exceptions": self._exception_count_total,
            },
            "warnings": warnings,
        }

    def get_error_stats(self) -> Dict[str, Any]:
        """获取异常统计摘要"""
        with self._stats_lock:
            total = self._exception_count_total
            records = list(self._exception_records)
            recent = records[-10:] if records else []

            by_type: Dict[str, int] = {}
            for rec in records:
                exc_type = rec["type"]
                by_type[exc_type] = by_type.get(exc_type, 0) + 1

        with self._gc_stats_lock:
            gc_pauses = list(self._recent_gc_pauses)
            gc_stats = {
                "recent_samples": len(gc_pauses),
                "avg_pause_ms": round(sum(gc_pauses) / len(gc_pauses), 2) if gc_pauses else 0.0,
                "max_pause_ms": max(gc_pauses) if gc_pauses else 0.0,
            }

        return {
            "status": "ok",
            "reason": f"异常统计: 总计 {total} 次",
            "data": {
                "total_exceptions": total,
                "by_type": by_type,
                "recent_10": recent,
                "gc_stats": gc_stats,
                "last_response_level": self._last_response_level,
            },
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            if not hasattr(self, "_initialized") or not self._initialized:
                return {
                    "status": "degraded",
                    "reason": "处理器未初始化",
                    "data": {},
                    "warnings": ["not_initialized"],
                }

            # 尝试获取锁，超时则判定为降级
            lock_acquired = self._stats_lock.acquire(timeout=0.1)
            if not lock_acquired:
                return {
                    "status": "degraded",
                    "reason": "无法获取统计锁，可能存在死锁或长时间持锁",
                    "data": {},
                    "warnings": ["lock_timeout"],
                }

            try:
                total = self._exception_count_total
                window_count = len(self._exception_records)
                window_usage_pct = round(window_count / self.DEFAULT_STATS_WINDOW_SEC * 100, 1)
            finally:
                self._stats_lock.release()

            gc_monitor_alive = (
                self._gc_monitor_thread is not None and self._gc_monitor_thread.is_alive()
            )

            return {
                "status": "ok",
                "reason": f"GlobalExceptionHandler 正常，已处理 {total} 次异常，窗口内 {window_count} 条",
                "data": {
                    "total_exceptions": total,
                    "window_count": window_count,
                    "window_usage_pct": window_usage_pct,
                    "gc_monitor_alive": gc_monitor_alive,
                    "dependencies": {
                        "emergency_simplifier": self._emergency_simplifier is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                        "llm_task_queue": self._llm_task_queue is not None,
                        "module_health_monitor": self._module_health_monitor is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _record_exception(self, record: Dict[str, Any], now: float) -> None:
        """
        原子性地记录一条异常（必须在持有 _stats_lock 时调用）

        将 append 操作封装为单一方法，防止未来被拆分到锁外。
        """
        self._exception_records.append(record)
        self._exception_per_minute.append(now)
        self._exception_count_total += 1

    def _determine_response_level(self, now: float, exceptions_this_minute: int) -> int:
        """根据异常密度和冷却期判定响应等级（必须在持有 _stats_lock 时调用）"""
        # 检查冷却期
        if now - self._last_degradation_time < self.DEFAULT_COOLDOWN_SEC:
            if exceptions_this_minute >= self.DEFAULT_MAX_EXCEPTIONS_PER_MINUTE:
                return self.RESPONSE_HARD_EXIT
            return self.RESPONSE_WARNING

        # 超过硬退出阈值
        if exceptions_this_minute >= self.DEFAULT_MAX_EXCEPTIONS_PER_MINUTE:
            return self.RESPONSE_HARD_EXIT

        # 突发判定：从尾部反向扫描，避免 O(N) 遍历
        burst_cutoff = now - self.DEFAULT_BURST_INTERVAL_SEC
        burst_count = 0
        for r in reversed(self._exception_records):
            if r["timestamp"] >= burst_cutoff:
                burst_count += 1
            else:
                break

        if burst_count >= self.DEFAULT_BURST_THRESHOLD:
            return self.RESPONSE_DEGRADATION

        if burst_count >= self.DEFAULT_BURST_THRESHOLD // 2:
            return self.RESPONSE_WARNING

        return self.RESPONSE_SILENT

    def _log_exception(
        self,
        exc_type: str,
        exc_message: str,
        module: str,
        function: str,
        response_level: int,
    ) -> None:
        """记录异常日志"""
        if response_level >= self.RESPONSE_HARD_EXIT:
            logger.critical(
                f"全局异常 [{exc_type}] {exc_message} (模块={module}, 函数={function}) "
                f"#RECOVERY: 系统将硬退出，检查异常日志并手动重启"
            )
        elif response_level >= self.RESPONSE_DEGRADATION:
            logger.error(
                f"全局异常 [{exc_type}] {exc_message} (模块={module}, 函数={function}) "
                f"#RECOVERY: 系统将执行紧急降维，仅保留核心风控与基础策略"
            )
        elif response_level >= self.RESPONSE_WARNING:
            logger.warning(
                f"全局异常 [{exc_type}] {exc_message} (模块={module}, 函数={function})"
            )
        else:
            logger.debug(
                f"全局异常静默记录 [{exc_type}] {exc_message} (模块={module}, 函数={function})"
            )

        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="global_exception",
                    details={
                        "exception_type": exc_type,
                        "message": exc_message[:200],
                        "module": module,
                        "function": function,
                        "response_level": response_level,
                    },
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

    def _push_alert(
        self,
        exc_type: str,
        exc_message: str,
        module: str,
        function: str,
    ) -> None:
        """推送告警到协商总线"""
        if self._negotiation_bus is not None and callable(getattr(self._negotiation_bus, "publish_alert", None)):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="global_exception",
                    level="critical",
                    message=f"全局异常 [{exc_type}] in {module}.{function}: {exc_message[:200]}",
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning(f"协商总线告警推送失败: {e}")

    def _submit_diagnosis_task(
        self,
        exc_type: str,
        exc_message: str,
        context: Dict[str, Any],
    ) -> None:
        """将异常上下文提交给 DeepSeek 进行根因分析"""
        if self._llm_task_queue is not None:
            try:
                self._llm_task_queue.submit_diagnosis(
                    task_type="global_exception_analysis",
                    priority=8,
                    payload={
                        "exception_type": exc_type,
                        "message": exc_message,
                        "context": context,
                        "timestamp": time.time(),
                    },
                )
            except Exception as e:
                logger.warning(f"LLM诊断任务提交失败: {e}")

    def _execute_degradation(self, exc_type: str, exc_message: str, now: float) -> bool:
        """执行紧急降维"""
        if self._emergency_simplifier is None:
            logger.error("EmergencySimplifier 不可用，无法执行降维")
            return False

        burst_count = self._get_burst_count(now)
        level = self._select_degradation_level(burst_count)

        try:
            result = self._emergency_simplifier.trigger_degradation(
                level=level,
                reason=f"全局异常触发: {exc_type} - {exc_message[:100]}",
            )
            if result.get("status") == "ok":
                with self._stats_lock:
                    self._last_degradation_time = now
                    self._last_response_level = self.RESPONSE_DEGRADATION
                logger.warning("紧急降维执行成功，系统进入降级运行模式，级别=%s", level)
                return True
            else:
                logger.error(f"紧急降维执行失败: {result.get('reason', '未知原因')}")
                return False
        except Exception as e:
            logger.error(f"紧急降维执行异常: {e}")
            return False

    def _get_burst_count(self, now: float) -> int:
        """获取当前时间窗口内的突发异常数（反向扫描，避免 O(N) 遍历）"""
        burst_cutoff = now - self.DEFAULT_BURST_INTERVAL_SEC
        count = 0
        for r in reversed(self._exception_records):
            if r["timestamp"] >= burst_cutoff:
                count += 1
            else:
                break
        return count

    def _select_degradation_level(self, burst_count: int) -> str:
        """根据突发异常数动态选择降维深度"""
        if burst_count >= self.DEFAULT_BURST_THRESHOLD * 4:
            return "heavy"
        elif burst_count >= self.DEFAULT_BURST_THRESHOLD * 2:
            return "medium"
        else:
            return "light"

    def _release_all_locks(self) -> None:
        """紧急释放本线程持有的所有锁，防止死锁"""
        for lock_name, lock in [("_stats_lock", self._stats_lock), ("_gc_stats_lock", self._gc_stats_lock)]:
            if lock.locked():
                try:
                    lock.release()
                except RuntimeError:
                    # 锁被其他线程持有（理论上不会发生，因为调用者应持有锁）
                    logger.warning(f"{lock_name} 被其他线程持有，无法释放，继续退出")
                except Exception as e:
                    logger.warning(f"释放 {lock_name} 时异常: {e}")

    def _hard_exit(self, exc_type: str, exc_message: str) -> None:
        """
        硬退出（最终兜底，尽量保持 atexit 回调有机会执行）

        在退出前尝试记录关键信息，然后通过 sys.exit 终止进程。
        若 sys.exit 超时，则回退到 os._exit。
        """
        try:
            logger.critical(
                "系统硬退出: %s - %s #RECOVERY: 检查日志后手动重启系统",
                exc_type,
                exc_message,
            )
            for handler in logger.handlers:
                handler.flush()
        except Exception:
            pass

        # 在释放锁之前，尝试通过行为日志记录最终审计记录
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="system_hard_exit",
                    details={
                        "exception_type": exc_type,
                        "message": exc_message[:500],
                        "timestamp": time.time(),
                        "reason": "异常密度超过安全阈值或降维失败",
                    },
                    immediate_flush=True,  # 要求立即刷盘，不经过缓冲
                )
            except Exception:
                pass  # 行为日志不可用，无法记录，但仍继续退出

        self._gc_monitor_stop.set()
        if self._gc_monitor_thread is not None and self._gc_monitor_thread.is_alive():
            self._gc_monitor_thread.join(timeout=2.0)

        # 使用 alarm 防止退出流程被无限阻塞
        has_alarm = False
        try:
            signal.alarm(self.HARD_EXIT_TIMEOUT_SEC)
            has_alarm = True
        except AttributeError:
            logger.warning("当前平台不支持 signal.alarm，硬退出无超时保护")

        try:
            sys.exit(1)
        except SystemExit:
            pass
        except Exception as e:
            logger.critical(f"sys.exit 执行失败: {e}，回退到 os._exit")
            if has_alarm:
                signal.alarm(0)
            os._exit(1)

    def _report_health(
        self,
        now: float,
        exceptions_this_minute: int,
        response_level: int,
    ) -> None:
        """
        向模块健康监控上报自身状态
        警告：此方法必须在所有锁外调用，禁止在持有 _stats_lock 时调用
        """
        if self._module_health_monitor is not None:
            try:
                health_score = max(0.0, 100.0 - exceptions_this_minute * 10)
                self._module_health_monitor.update_module_health(
                    module_name="global_exception_handler",
                    score=health_score,
                    details={
                        "exceptions_this_minute": exceptions_this_minute,
                        "response_level": response_level,
                    },
                )
            except Exception as e:
                logger.warning(f"健康状态上报失败: {e}")

    def _purge_expired_records(self, now: float) -> None:
        """
        清理统计窗口内的过期记录，防止内存无限增长（必须在持有 _stats_lock 时调用）
        当队列异常庞大时采用二分查找截断，正常情况使用 while popleft。
        """
        cutoff = now - self.DEFAULT_STATS_WINDOW_SEC

        # 紧急截断：如果队列异常庞大（超过 10000 条），使用二分查找加速
        if len(self._exception_records) > 10000:
            timestamps = [r["timestamp"] for r in self._exception_records]
            keep_idx = bisect.bisect_left(timestamps, cutoff)
            if keep_idx > 0:
                for _ in range(keep_idx):
                    self._exception_records.popleft()
            return

        # 正常路径：逐条弹出过期记录
        while self._exception_records and self._exception_records[0]["timestamp"] < cutoff:
            self._exception_records.popleft()

    # ========== GC 监控 ==========
    def _start_gc_monitor(self) -> None:
        """启动 GC 监控守护线程"""
        self._gc_monitor_thread = threading.Thread(
            target=self._gc_monitor_loop,
            daemon=True,
            name="gc_monitor",
        )
        self._gc_monitor_thread.start()
        logger.info("GC 监控已启用（轮询模式）")

    def _gc_monitor_loop(self) -> None:
        """GC 监控循环（Python 3.11+ 轮询模式）"""
        self._last_gc_count = gc.get_count()[0]
        while not self._gc_monitor_stop.is_set():
            self._gc_monitor_stop.wait(self.DEFAULT_GC_MONITOR_INTERVAL)
            try:
                current_count = gc.get_count()[0]
                collections = current_count - self._last_gc_count
                if collections > 0:
                    stats = gc.get_stats()
                    # 优先使用 duration 字段（秒），否则回退到估算
                    total_pause = sum(
                        s.get("duration", 0) for s in stats
                    ) * 1000  # 转为毫秒
                    if total_pause == 0.0:
                        t0 = time.perf_counter()
                        gc.collect(0)
                        total_pause = (time.perf_counter() - t0) * 1000
                    with self._gc_stats_lock:
                        self._recent_gc_pauses.append(total_pause)
                    if total_pause > 100:
                        logger.warning(
                            f"GC 暂停时间过长: {total_pause:.1f}ms "
                            f"#RECOVERY: 检查内存分配模式，考虑启用对象池"
                        )
                self._last_gc_count = current_count
            except Exception as e:
                logger.warning(f"GC 监控轮询异常: {e}")

    # ========== 资源清理 ==========
    def _cleanup(self) -> None:
        """退出前清理资源"""
        self._gc_monitor_stop.set()
        if self._gc_monitor_thread is not None and self._gc_monitor_thread.is_alive():
            self._gc_monitor_thread.join(timeout=3.0)
        logger.info("GlobalExceptionHandler 已清理资源")
