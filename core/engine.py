#!/usr/bin/env python3
"""
火种系统 · 主引擎入口 (Engine)

核心职责：
1. 原子化启动：任意关键子系统初始化失败，整体回滚至启动前状态。
2. 交易优先级保障：主循环由内置纳秒级自旋等待驱动，不依赖外部定时器实现质量。
3. 运行时存活校验：以轻量级被动心跳为主、主动深度探测为辅，僵死模块自动隔离、通知依赖方降级、并尝试恢复。
4. 防泄漏优雅停止：不可变退出序列确保共享内存、大页内存、C++子进程等全部释放，退出时验证并强制清理残留资源。
5. 模块自注册：通过 SystemBuilder 统一装配并注入，引擎不直接依赖具体模块实例化细节。

外部依赖（真实模块接口）：
- core.system_builder.SystemBuilder : 依赖注入工厂，装配、回滚、模块重启与资源释放
- core.engine.elastic_time.ElasticTimeManager : 弹性时间管理器
- core.engine.dormancy_manager.DormancyManager : 分层休眠控制器
- core.engine.sabbath_controller.SabbathController : 安息日调度器
- core.engine.emergency_simplifier.EmergencySimplifier : 一键降维与渐进恢复
- core.engine.global_exception_handler.GlobalExceptionHandler : 全局异常捕获与自愈协调
- core.signal_bus.SignalBus : 四车道信号总线
- core.negotiation_bus.NegotiationBus : 跨模块协商总线
- core.pipeline_bus.PipelineBus : 六阶段流水线调度器
- core.behavioral_logger.BehavioralLogger : 行为日志记录器
- core.memory_guard.MemoryGuard : 内存保护与 OOM 预警
- core.position_snapshot.PositionSnapshot : 持仓快照与崩溃恢复
- core.self_destruct.SelfDestruct : 防破解自毁机制
- core.account_ledger.AccountLedger : 账户财务状态原子计算
- core.symbol_mapper.SymbolMapper : 交易对名称标准化映射
- core.log_forwarder.LogForwarder : 独立日志守护进程

接口契约：
- start() -> Dict[str, Any] : 原子化启动所有子系统，返回启动状态字典
- stop(reason: str = "manual") -> Dict[str, Any] : 优雅停止，平仓并释放资源
- health_check() -> Dict[str, Any] : 全系统深度健康自检
- update_heartbeat(module_name: str) -> None : 模块主动上报存活（必须定期调用，否则引擎无法感知）
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 关键子系统启动失败，拒绝启动并自动回滚至快照。
- 模块连续心跳超时或深度健康检查失败，标记为 degraded，广播告警并尝试重启。
- 主循环异常由 GlobalExceptionHandler 兜底，确保进程不意外终止。

资源管理：
- 退出时严格按序释放资源，任何一步失败不阻塞后续清理。
- 持仓快照在停止前强制持久化。
- 主循环不直接持有文件句柄或网络连接。

模块存活契约（必须严格遵守）：
- 所有被引擎监控的关键模块，必须在自己的主循环中以不超过 1 秒的间隔调用 engine.update_heartbeat(模块名)。
- 若模块无法满足此要求，引擎会在心跳超时后判定其失效，触发降级与恢复。
"""

import os
import sys
import time
import signal
import logging
import threading
import subprocess
from typing import Dict, Any, Optional, List
from collections import OrderedDict

logger = logging.getLogger(__name__)


class EssentialModules:
    """SystemBuilder 返回的关键模块强类型容器"""

    MONITORED_FIELDS = (
        'signal_bus', 'negotiation_bus', 'pipeline_bus', 'account_ledger',
        'elastic_time', 'dormancy_manager', 'memory_guard', 'precision_timer',
        'position_snapshot', 'emergency_simplifier', 'global_exception_handler',
    )

    def __init__(
        self,
        signal_bus: Any = None,
        negotiation_bus: Any = None,
        pipeline_bus: Any = None,
        account_ledger: Any = None,
        elastic_time: Any = None,
        dormancy_manager: Any = None,
        memory_guard: Any = None,
        precision_timer: Any = None,
        position_snapshot: Any = None,
        emergency_simplifier: Any = None,
        global_exception_handler: Any = None,
        sabbath_controller: Any = None,
        self_destruct: Any = None,
        behavioral_logger: Any = None,
        log_forwarder: Any = None,
    ):
        self.signal_bus = signal_bus
        self.negotiation_bus = negotiation_bus
        self.pipeline_bus = pipeline_bus
        self.account_ledger = account_ledger
        self.elastic_time = elastic_time
        self.dormancy_manager = dormancy_manager
        self.memory_guard = memory_guard
        self.precision_timer = precision_timer
        self.position_snapshot = position_snapshot
        self.emergency_simplifier = emergency_simplifier
        self.global_exception_handler = global_exception_handler
        self.sabbath_controller = sabbath_controller
        self.self_destruct = self_destruct
        self.behavioral_logger = behavioral_logger
        self.log_forwarder = log_forwarder


class Engine:
    """火种主引擎"""

    # ========== 类常量 ==========
    DEFAULT_MAIN_LOOP_TICK_NS = 1_000_000
    DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SEC = 60
    DEFAULT_DEEP_HEALTH_INTERVAL_SEC = 30
    DEFAULT_HEARTBEAT_LOG_INTERVAL_SEC = 5
    MODULE_HEARTBEAT_TIMEOUT_SEC = 10.0
    MODULE_FAILURE_THRESHOLD = 3
    MAX_RECOVERY_RETRIES = 3
    SHUTDOWN_RETRY_COUNT = 3
    POSITION_POLL_INTERVAL_SEC = 0.1
    SPIN_WAIT_THRESHOLD_NS = 50_000

    # 模块依赖关系
    MODULE_DEPENDENCIES: Dict[str, List[str]] = {
        "signal_bus": ["scorecard", "execution", "risk_monitor"],
        "negotiation_bus": ["signal_bus", "pipeline_bus", "scorecard"],
        "pipeline_bus": ["execution", "order_manager"],
        "account_ledger": ["position_sizer", "risk_monitor", "order_manager"],
        "elastic_time": ["scorecard", "dormancy_manager"],
        "dormancy_manager": ["scorecard", "order_manager"],
        "memory_guard": [],
        "precision_timer": ["engine"],
        "position_snapshot": ["engine"],
        "emergency_simplifier": ["engine"],
        "global_exception_handler": ["engine"],
    }

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self._running = threading.Event()
        self._started = False
        self._shutdown_reason = ""
        self._startup_lock = threading.Lock()
        self._health_lock = threading.Lock()
        self.essential_modules: Optional[EssentialModules] = None
        self.system_builder = None
        self._heartbeats: Dict[str, float] = {}
        self._consecutive_failures: Dict[str, int] = {}
        self._recovery_attempts: Dict[str, int] = {}
        self._degraded_modules: set = set()
        logger.info("Engine 实例已创建")

    # ========== 依赖注入 ==========
    def inject_dependencies(self, system_builder: Optional[Any] = None) -> None:
        if system_builder is not None:
            self.system_builder = system_builder
            logger.info("SystemBuilder 注入成功")
        else:
            logger.warning("SystemBuilder 未注入，引擎将无法启动")

    # ========== 公共接口 ==========
    def start(self) -> Dict[str, Any]:
        if self._started:
            return {"status": "error", "reason": "引擎已启动", "data": {}, "warnings": []}

        with self._startup_lock:
            if self.system_builder is None:
                return self._fatal_result("SystemBuilder未注入", "inject_dependencies 未调用")
            logger.info("=" * 60)
            logger.info("火种系统原子化启动中...")
            logger.info("=" * 60)

            try:
                self.system_builder.create_startup_snapshot()
            except Exception as e:
                logger.critical(f"启动快照创建失败: {e}")
                return self._fatal_result("启动快照失败", str(e))

            try:
                self.system_builder.init_all()
                self._load_and_verify_modules()
            except Exception as e:
                logger.critical(f"启动失败，执行整体回滚: {e}")
                self.system_builder.rollback_to_snapshot()
                return self._fatal_result("启动失败", str(e))

            self._started = True
            self._running.set()
            self._setup_signal_handlers()
            logger.info("火种系统启动完成，进入主循环")
            return {"status": "ok", "reason": "所有子系统启动完成", "data": {}, "warnings": []}

    def run(self) -> None:
        if not self._started:
            logger.critical("引擎未启动，无法进入主循环")
            return

        last_deep_health = time.time()
        last_heartbeat_log = time.time()

        while self._running.is_set():
            try:
                self._pulse_core_modules()
                self._check_heartbeats()

                now = time.time()
                if now - last_deep_health > self.DEFAULT_DEEP_HEALTH_INTERVAL_SEC:
                    self._run_deep_health_check()
                    last_deep_health = now

                if now - last_heartbeat_log > self.DEFAULT_HEARTBEAT_LOG_INTERVAL_SEC:
                    logger.debug("主循环心跳正常")
                    last_heartbeat_log = now

                next_tick = time.perf_counter_ns() + self.DEFAULT_MAIN_LOOP_TICK_NS
                self._precise_wait(next_tick)
            except Exception as e:
                if self.essential_modules.global_exception_handler:
                    self.essential_modules.global_exception_handler.handle_exception(e, context="main_loop")
                else:
                    logger.error(f"主循环异常: {e} #RECOVERY: 检查全局异常处理器")

        self._perform_shutdown()

    def stop(self, reason: str = "manual") -> Dict[str, Any]:
        self._shutdown_reason = reason
        logger.info(f"收到停止指令: {reason}")
        self._running.clear()
        return {"status": "ok", "reason": f"停止原因: {reason}", "data": {}, "warnings": []}

    def update_heartbeat(self, module_name: str) -> None:
        with self._health_lock:
            if module_name in self._heartbeats:
                self._heartbeats[module_name] = time.time()

    def health_check(self) -> Dict[str, Any]:
        with self._health_lock:
            components = {}
            for name in EssentialModules.MONITORED_FIELDS:
                mod = getattr(self.essential_modules, name, None)
                components[name] = self._is_module_alive(mod)
            failed = [k for k, v in components.items() if not v]
            status = "ok" if not failed else "degraded"
            reason = "全部正常" if not failed else f"异常模块: {failed}"
            return {"status": status, "reason": reason, "data": components, "warnings": []}

    # ========== 纳秒级精确等待 ==========
    @staticmethod
    def _precise_wait(target_time_ns: int) -> None:
        while True:
            current = time.perf_counter_ns()
            remaining = target_time_ns - current
            if remaining <= 0:
                return
            if remaining > Engine.SPIN_WAIT_THRESHOLD_NS:
                time.sleep(0)
            # 最后50μs使用自旋等待精确对齐

    # ========== 内部初始化 ==========
    def _load_and_verify_modules(self) -> None:
        modules = self.system_builder.get_essential_modules()
        if not isinstance(modules, EssentialModules):
            raise TypeError("get_essential_modules 必须返回 EssentialModules 实例")
        self.essential_modules = modules

        now = time.time()
        for name in EssentialModules.MONITORED_FIELDS:
            self._heartbeats[name] = now
            self._consecutive_failures[name] = 0
            self._recovery_attempts[name] = 0
        failed = []
        for name in EssentialModules.MONITORED_FIELDS:
            mod = getattr(self.essential_modules, name, None)
            if mod is None:
                failed.append(f"{name}(未注入)")
            elif not self._is_module_alive(mod):
                failed.append(f"{name}(初始健康检查失败)")
        if failed:
            raise RuntimeError(f"关键模块初始验证失败: {failed}")
        logger.info("所有关键模块初始验证通过")

    # ========== 运行时存活校验 ==========
    def _pulse_core_modules(self) -> None:
        em = self.essential_modules
        for name in ('elastic_time', 'dormancy_manager', 'memory_guard', 'emergency_simplifier'):
            if name in self._degraded_modules:
                continue
            mod = getattr(em, name, None)
            if mod:
                mod.pulse()

    def _check_heartbeats(self) -> None:
        now = time.time()
        with self._health_lock:
            for name in EssentialModules.MONITORED_FIELDS:
                if name in self._degraded_modules:
                    continue
                last = self._heartbeats.get(name, 0)
                if last > 0 and (now - last) > self.MODULE_HEARTBEAT_TIMEOUT_SEC:
                    logger.error(f"模块 {name} 心跳超时 #RECOVERY: 启动恢复流程")
                    self._handle_module_failure(name)

    def _run_deep_health_check(self) -> None:
        for name in EssentialModules.MONITORED_FIELDS:
            if name in self._degraded_modules:
                continue
            mod = getattr(self.essential_modules, name, None)
            alive = self._is_module_alive(mod)
            with self._health_lock:
                if alive:
                    self._heartbeats[name] = time.time()
                    self._consecutive_failures[name] = 0
                else:
                    self._consecutive_failures[name] += 1
                    if self._consecutive_failures[name] >= self.MODULE_FAILURE_THRESHOLD:
                        logger.error(f"模块 {name} 连续深度检查失败 {self._consecutive_failures[name]} 次")
                        self._handle_module_failure(name)

    def _is_module_alive(self, mod: Any) -> bool:
        if mod is None:
            return False
        if callable(getattr(mod, 'health_check', None)):
            try:
                result = mod.health_check()
                return result.get("status") == "ok"
            except Exception as e:
                logger.warning(f"模块健康检查异常: {type(e).__name__}: {e}")
                return False
        return True

    def _handle_module_failure(self, name: str) -> None:
        """隔离、通知依赖方、广播、重启恢复"""
        with self._health_lock:
            self._degraded_modules.add(name)
        self._notify_dependents(name)

        if self.essential_modules.negotiation_bus:
            try:
                self.essential_modules.negotiation_bus.publish_alert(
                    alert_type="module_degraded",
                    level="critical",
                    message=f"模块 {name} 已降级隔离",
                    timestamp=time.time()
                )
            except Exception as e:
                logger.warning(f"广播降级事件失败: {e}")

        attempts = self._recovery_attempts.get(name, 0) + 1
        self._recovery_attempts[name] = attempts
        if attempts <= self.MAX_RECOVERY_RETRIES:
            logger.info(f"尝试重启模块 {name} (第{attempts}次)")
            if self.system_builder and hasattr(self.system_builder, 'restart_module'):
                try:
                    new_mod = self.system_builder.restart_module(name)
                    if new_mod:
                        setattr(self.essential_modules, name, new_mod)
                        with self._health_lock:
                            self._degraded_modules.discard(name)
                            self._heartbeats[name] = time.time()
                            self._consecutive_failures[name] = 0
                            self._recovery_attempts[name] = 0
                        logger.info(f"模块 {name} 重启成功，已重新加入监控")
                    else:
                        logger.warning(f"模块 {name} 重启失败")
                except Exception as e:
                    logger.error(f"重启接口异常: {e}")
            else:
                logger.warning("SystemBuilder 未提供 restart_module 接口，无法自动恢复")
        else:
            logger.critical(f"模块 {name} 多次恢复失败，触发全系统降级")
            if self.essential_modules.emergency_simplifier:
                self.essential_modules.emergency_simplifier.trigger("module_failure")

    def _notify_dependents(self, failed_module: str) -> None:
        """通知依赖方切换降级路径"""
        dependents = self.MODULE_DEPENDENCIES.get(failed_module, [])
        if not dependents:
            return
        logger.warning(f"模块 {failed_module} 失效，通知依赖方降级: {dependents}")
        for dep_name in dependents:
            dep_mod = getattr(self.essential_modules, dep_name, None)
            if dep_mod and hasattr(dep_mod, 'on_dependency_degraded'):
                try:
                    dep_mod.on_dependency_degraded(failed_module)
                    logger.info(f"已通知 {dep_name} 切换到降级路径")
                except Exception as e:
                    logger.error(f"通知 {dep_name} 降级失败: {e}")

    # ========== 优雅停止 ==========
    def _perform_shutdown(self) -> None:
        logger.info("开始执行防泄漏停止流程...")
        sequence = OrderedDict([
            ("广播停止通知", self._shutdown_notify),
            ("等待平仓", self._shutdown_wait_positions),
            ("持久化快照", self._shutdown_save_snapshot),
            ("销毁SystemBuilder", self._shutdown_destroy_builder),
            ("验证共享内存清理", self._shutdown_verify_shm),
            ("验证大页内存清理", self._shutdown_verify_hugepages),
            ("终止辅助进程", self._shutdown_kill_aux),
            ("最终兜底退出", self._shutdown_hard_exit),
        ])
        for description, task in sequence.items():
            for attempt in range(1, self.SHUTDOWN_RETRY_COUNT + 1):
                try:
                    task()
                    logger.info(f"停止步骤 [{description}] 完成")
                    break
                except Exception as e:
                    logger.error(f"停止步骤 [{description}] 失败 (attempt {attempt}): {e}")
                    if attempt == self.SHUTDOWN_RETRY_COUNT:
                        logger.critical(f"停止步骤 [{description}] 多次重试仍失败，继续执行下一步骤")

    def _shutdown_notify(self) -> None:
        if self.essential_modules and self.essential_modules.negotiation_bus:
            self.essential_modules.negotiation_bus.publish_alert(
                alert_type="system_shutdown",
                level="critical",
                message="系统正在停止，暂停所有开仓",
                timestamp=time.time()
            )

    def _shutdown_wait_positions(self) -> None:
        if not self.essential_modules or not self.essential_modules.account_ledger:
            logger.warning("AccountLedger 不可用，使用固定等待时间")
            time.sleep(10)
            return
        deadline = time.time() + self.DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SEC
        last_log = time.time()
        while time.time() < deadline:
            try:
                positions = self.essential_modules.account_ledger.get_open_positions()
                if not positions:
                    logger.info("所有持仓已平仓")
                    break
                now = time.time()
                if now - last_log > 5:
                    logger.info(f"等待平仓中，剩余持仓: {len(positions)}，超时剩余 {deadline - now:.0f}s")
                    last_log = now
            except Exception as e:
                logger.error(f"查询持仓失败: {e}")
            time.sleep(self.POSITION_POLL_INTERVAL_SEC)
        else:
            logger.error("平仓等待超时，部分持仓可能未平仓")

    def _shutdown_save_snapshot(self) -> None:
        if self.essential_modules and self.essential_modules.position_snapshot:
            self.essential_modules.position_snapshot.save_snapshot()
            logger.info("持仓快照已保存")

    def _shutdown_destroy_builder(self) -> None:
        if self.system_builder:
            self.system_builder.shutdown_all()
            logger.info("SystemBuilder 已释放")

    def _shutdown_verify_shm(self) -> None:
        try:
            result = subprocess.run(["ipcs", "-m"], capture_output=True, text=True, timeout=5)
            if "fire_seed" in result.stdout or "realtime_guard" in result.stdout:
                logger.warning("检测到残留共享内存段，尝试强制清理")
                for line in result.stdout.split("\n"):
                    if "fire_seed" in line or "realtime_guard" in line:
                        parts = line.split()
                        if parts:
                            shm_id = parts[1]
                            try:
                                subprocess.run(["ipcrm", "-m", shm_id], timeout=2)
                                logger.info(f"已强制清理共享内存段 {shm_id}")
                            except Exception as e:
                                logger.error(f"清理共享内存段 {shm_id} 失败: {e}")
            else:
                logger.info("共享内存清理验证通过")
        except Exception as e:
            logger.warning(f"共享内存验证异常: {e}")

    def _shutdown_verify_hugepages(self) -> None:
        try:
            hugepage_dir = "/dev/hugepages"
            if os.path.exists(hugepage_dir):
                remaining = [f for f in os.listdir(hugepage_dir) if "fire_seed" in f]
                if remaining:
                    logger.warning(f"检测到残留大页内存文件: {remaining}")
                    for f in remaining:
                        try:
                            os.remove(os.path.join(hugepage_dir, f))
                        except Exception as e:
                            logger.error(f"清理大页内存文件 {f} 失败: {e}")
            logger.info("大页内存清理验证通过")
        except Exception as e:
            logger.warning(f"大页内存验证异常: {e}")

    def _shutdown_kill_aux(self) -> None:
        if self.essential_modules:
            if self.essential_modules.log_forwarder:
                self.essential_modules.log_forwarder.stop()
            if self.essential_modules.sabbath_controller:
                self.essential_modules.sabbath_controller.stop()
        logger.info("辅助进程已终止")

    def _shutdown_hard_exit(self) -> None:
        logger.info("执行最终退出...")
        exit_event = threading.Event()

        def force_exit():
            if not exit_event.wait(timeout=5):
                logger.critical("正常退出超时，执行 os._exit(1)")
                os._exit(1)

        t = threading.Thread(target=force_exit, daemon=True)
        t.start()
        try:
            sys.exit(0)
        except SystemExit:
            pass
        finally:
            exit_event.set()
            t.join(timeout=1)

    # ========== 信号处理 ==========
    def _setup_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        logger.info("已注册 SIGTERM/SIGINT 信号处理器")

    def _signal_handler(self, signum: int, frame: Any) -> None:
        sig_name = signal.Signals(signum).name
        logger.info(f"收到信号 {sig_name}，开始优雅停止")
        self.stop(reason=f"signal:{sig_name}")

    @staticmethod
    def _fatal_result(reason: str, detail: str) -> Dict[str, Any]:
        return {"status": "fatal", "reason": reason, "data": {"detail": detail}, "warnings": []}
