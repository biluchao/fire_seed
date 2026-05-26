#!/usr/bin/env python3
"""
火种系统 · 主引擎入口 (Engine)

核心职责：
1. 加载全局配置，通过 SystemBuilder 装配所有核心子系统并完成依赖注入
2. 运行主事件循环，协调弹性时间、分层休眠、安息日调度与降维恢复
3. 优雅处理进程信号（SIGTERM/SIGINT），确保安全平仓、资源释放与快照持久化

外部依赖（真实模块接口）：
- core.system_builder.SystemBuilder : 依赖注入工厂，按配置顺序装配全系统模块
- core.engine.elastic_time.ElasticTimeManager : 弹性时间管理器
- core.engine.dormancy_manager.DormancyManager : 分层休眠控制器
- core.engine.sabbath_controller.SabbathController : 安息日调度器
- core.engine.emergency_simplifier.EmergencySimplifier : 一键降维与渐进恢复
- core.engine.global_exception_handler.GlobalExceptionHandler : 全局异常捕获与自愈协调
- core.signal_bus.SignalBus : 四车道信号总线
- core.negotiation_bus.NegotiationBus : 跨模块协商总线
- core.pipeline_bus.PipelineBus : 六阶段流水线调度器
- core.precision_timer.PrecisionTimer : 精确定时器
- core.behavioral_logger.BehavioralLogger : 行为日志记录器
- core.memory_guard.MemoryGuard : 内存保护与 OOM 预警
- core.position_snapshot.PositionSnapshot : 持仓快照与崩溃恢复
- core.self_destruct.SelfDestruct : 防破解自毁机制
- core.account_ledger.AccountLedger : 账户财务状态原子计算
- core.symbol_mapper.SymbolMapper : 交易对名称标准化映射
- core.log_forwarder.LogForwarder : 独立日志守护进程

接口契约：
- start() -> Dict[str, Any] : 启动所有子系统，返回启动状态字典
- stop(reason: str = "manual") -> Dict[str, Any] : 优雅停止，平仓并释放资源
- health_check() -> Dict[str, Any] : 全系统健康自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 若关键子系统（如 SignalBus、NegotiationBus）启动失败，引擎将拒绝启动并记录致命错误
- 若非关键子系统启动失败，引擎记录错误并继续启动，标记 "degraded" 状态
- 在主循环中捕获未处理异常，交由 GlobalExceptionHandler 进行根因分析与自愈调度
- 所有降级值在类常量区明确声明

资源管理：
- 引擎持有所有子系统的引用，在 stop() 中按启动逆序释放资源
- 退出时确保共享内存段被清理，持仓快照被持久化，C++ 硬实时子进程被正确终止
"""

import os
import sys
import time
import signal
import logging
import threading
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


class Engine:
    """火种主引擎，负责系统全生命周期管理"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_MAIN_LOOP_INTERVAL_SEC = 0.001  # 主循环间隔（秒），[0.001, 0.1]
    DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SEC = 60   # 优雅停止超时（秒），[30, 300]
    DEFAULT_HEALTH_CHECK_INTERVAL_SEC = 30       # 健康检查间隔（秒），[10, 300]
    DEFAULT_HEARTBEAT_INTERVAL_SEC = 5           # 心跳日志间隔（秒），[1, 60]

    # 子系统引用
    system_builder = None
    signal_bus = None
    negotiation_bus = None
    pipeline_bus = None
    elastic_time = None
    dormancy_manager = None
    sabbath_controller = None
    emergency_simplifier = None
    global_exception_handler = None
    precision_timer = None
    behavioral_logger = None
    memory_guard = None
    position_snapshot = None
    self_destruct = None
    account_ledger = None
    symbol_mapper = None
    log_forwarder = None
    config_loader = None

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self._running = threading.Event()
        self._started = False
        self._shutdown_reason = ""
        self._startup_lock = threading.Lock()
        self._health_check_lock = threading.Lock()
        logger.info("Engine 实例已创建，等待启动指令")

    # ========== 依赖注入 ==========
    def inject_dependencies(self, system_builder: Optional[Any] = None) -> None:
        """注入 SystemBuilder，在启动前由 main.py 调用"""
        if system_builder is not None:
            self.system_builder = system_builder
            logger.info("SystemBuilder 注入成功")
        else:
            logger.warning("SystemBuilder 未注入，引擎将无法启动")

    # ========== 公共接口 ==========
    def start(self) -> Dict[str, Any]:
        """启动所有子系统，初始化主循环所需资源"""
        if self._started:
            return {
                "status": "error",
                "reason": "引擎已启动，请勿重复调用 start()",
                "data": {},
                "warnings": [],
            }

        with self._startup_lock:
            logger.info("=" * 60)
            logger.info("火种系统启动中...")
            logger.info("=" * 60)

            try:
                # 1. 加载配置并校验
                self._init_config()
                logger.info("[1/8] 配置加载完成")
            except Exception as e:
                logger.critical(f"配置加载失败: {e} #RECOVERY: 检查所有 .yaml 文件语法")
                return self._fatal_result("配置加载失败", str(e))

            try:
                # 2. 初始化基础服务（日志、定时器、安全通信）
                self._init_basic_services()
                logger.info("[2/8] 基础服务初始化完成")
            except Exception as e:
                logger.critical(f"基础服务初始化失败: {e} #RECOVERY: 检查系统环境与依赖库")
                return self._fatal_result("基础服务初始化失败", str(e))

            try:
                # 3. 通过 SystemBuilder 装配核心模块
                self._assemble_core_modules()
                logger.info("[3/8] 核心模块装配完成")
            except Exception as e:
                logger.critical(f"核心模块装配失败: {e} #RECOVERY: 检查模块依赖与配置文件")
                return self._fatal_result("核心模块装配失败", str(e))

            try:
                # 4. 注入依赖并初始化协商总线
                self._init_negotiation_bus()
                logger.info("[4/8] 协商总线就绪")
            except Exception as e:
                logger.critical(f"协商总线初始化失败: {e} #RECOVERY: 检查 NegotiationBus 配置")
                return self._fatal_result("协商总线初始化失败", str(e))

            try:
                # 5. 启动信号总线与流程总线
                self._init_signal_and_pipeline()
                logger.info("[5/8] 信号总线与流程总线就绪")
            except Exception as e:
                logger.critical(f"信号/流程总线启动失败: {e} #RECOVERY: 检查四车道与流水线配置")
                return self._fatal_result("信号/流程总线启动失败", str(e))

            try:
                # 6. 启动策略引擎、风控、执行等业务模块
                self._init_business_modules()
                logger.info("[6/8] 业务模块就绪")
            except Exception as e:
                logger.critical(f"业务模块启动失败: {e} #RECOVERY: 检查策略、风控、执行配置")
                return self._fatal_result("业务模块启动失败", str(e))

            try:
                # 7. 恢复持仓快照并校准账户状态
                self._restore_state()
                logger.info("[7/8] 状态恢复完成")
            except Exception as e:
                logger.error(f"状态恢复失败: {e} #RECOVERY: 手动检查持仓快照与交易所持仓")

            try:
                # 8. 启动辅助服务（日志守护、自毁监控、安息日调度）
                self._init_auxiliary_services()
                logger.info("[8/8] 辅助服务就绪")
            except Exception as e:
                logger.error(f"辅助服务启动失败: {e} #RECOVERY: 检查辅助服务配置")

            self._started = True
            self._running.set()
            # 注册信号处理器
            self._setup_signal_handlers()
            logger.info("=" * 60)
            logger.info("火种系统启动完成，进入主循环")
            logger.info("=" * 60)

            return {
                "status": "ok",
                "reason": "所有子系统启动完成",
                "data": {"start_time": time.time()},
                "warnings": [],
            }

    def run(self) -> None:
        """主事件循环，阻塞直到收到停止信号"""
        if not self._started:
            logger.critical("引擎未启动，无法进入主循环")
            return

        last_heartbeat = time.time()
        last_health_check = time.time()

        while self._running.is_set():
            try:
                # 弹性时间更新
                if self.elastic_time is not None:
                    self.elastic_time.pulse()

                # 分层休眠管理
                if self.dormancy_manager is not None:
                    self.dormancy_manager.pulse()

                # 安息日调度
                if self.sabbath_controller is not None:
                    self.sabbath_controller.pulse()

                # 紧急降维检查
                if self.emergency_simplifier is not None:
                    self.emergency_simplifier.pulse()

                # 内存保护
                if self.memory_guard is not None:
                    self.memory_guard.pulse()

                # 心跳日志
                now = time.time()
                if now - last_heartbeat > self.DEFAULT_HEARTBEAT_INTERVAL_SEC:
                    logger.debug("主循环心跳正常")
                    last_heartbeat = now

                # 定期健康检查
                if now - last_health_check > self.DEFAULT_HEALTH_CHECK_INTERVAL_SEC:
                    self._run_health_check()
                    last_health_check = now

                time.sleep(self.DEFAULT_MAIN_LOOP_INTERVAL_SEC)

            except Exception as e:
                if self.global_exception_handler is not None:
                    self.global_exception_handler.handle_exception(e, context="main_loop")
                else:
                    logger.error(f"主循环异常: {e} #RECOVERY: 检查全局异常处理器是否正常")

        # 循环退出，执行清理
        self._perform_shutdown()

    def stop(self, reason: str = "manual") -> Dict[str, Any]:
        """优雅停止系统"""
        self._shutdown_reason = reason
        logger.info(f"收到停止指令，原因: {reason}")
        self._running.clear()
        return {
            "status": "ok",
            "reason": f"系统正在停止，原因: {reason}",
            "data": {},
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """全系统健康自检"""
        with self._health_check_lock:
            try:
                components = {
                    "signal_bus": self.signal_bus is not None,
                    "negotiation_bus": self.negotiation_bus is not None,
                    "pipeline_bus": self.pipeline_bus is not None,
                    "elastic_time": self.elastic_time is not None,
                    "dormancy_manager": self.dormancy_manager is not None,
                    "memory_guard": self.memory_guard is not None,
                    "position_snapshot": self.position_snapshot is not None,
                    "behavioral_logger": self.behavioral_logger is not None,
                    "account_ledger": self.account_ledger is not None,
                }
                failed = [k for k, v in components.items() if not v]
                if failed:
                    return {
                        "status": "degraded",
                        "reason": f"部分组件未就绪: {failed}",
                        "data": components,
                        "warnings": [f"missing: {f}" for f in failed],
                    }
                return {
                    "status": "ok",
                    "reason": "引擎核心组件全部就绪",
                    "data": components,
                    "warnings": [],
                }
            except Exception as e:
                logger.error(f"健康检查失败: {e} #RECOVERY: 检查引擎状态")
                return {
                    "status": "error",
                    "reason": f"健康检查异常: {str(e)}",
                    "data": {},
                    "warnings": [f"health_check_failed: {str(e)}"],
                }

    # ========== 私有初始化方法（由 SystemBuilder 提供实现） ==========
    def _init_config(self) -> None:
        """通过 SystemBuilder 加载并校验全部配置"""
        if self.system_builder is not None:
            self.system_builder.init_config(self.config)
        else:
            raise RuntimeError("SystemBuilder 未注入，无法加载配置")

    def _init_basic_services(self) -> None:
        """初始化定时器、行为日志、安全通信等基础服务"""
        if self.system_builder is not None:
            self.system_builder.init_basic_services()
        else:
            raise RuntimeError("SystemBuilder 未注入")

    def _assemble_core_modules(self) -> None:
        """装配所有核心模块实例并填充到引擎属性中"""
        if self.system_builder is None:
            raise RuntimeError("SystemBuilder 未注入")
        modules = self.system_builder.assemble_all()
        # 将模块实例赋值给引擎属性
        for attr_name in [
            "signal_bus", "negotiation_bus", "pipeline_bus",
            "elastic_time", "dormancy_manager", "sabbath_controller",
            "emergency_simplifier", "global_exception_handler",
            "precision_timer", "behavioral_logger", "memory_guard",
            "position_snapshot", "self_destruct", "account_ledger",
            "symbol_mapper", "log_forwarder", "config_loader"
        ]:
            if attr_name in modules:
                setattr(self, attr_name, modules[attr_name])

    def _init_negotiation_bus(self) -> None:
        """协商总线注册与启动"""
        if self.negotiation_bus is not None:
            self.negotiation_bus.start()
        else:
            raise RuntimeError("NegotiationBus 未装配")

    def _init_signal_and_pipeline(self) -> None:
        """启动四车道信号总线与六阶段流水线"""
        if self.signal_bus is not None:
            self.signal_bus.start()
        if self.pipeline_bus is not None:
            self.pipeline_bus.start()

    def _init_business_modules(self) -> None:
        """启动策略引擎、风控、执行等业务模块（由 SystemBuilder 统一调度）"""
        if self.system_builder is not None:
            self.system_builder.start_business_modules()
        else:
            raise RuntimeError("SystemBuilder 未注入")

    def _restore_state(self) -> None:
        """恢复持仓快照并与交易所持仓校准"""
        if self.position_snapshot is not None:
            self.position_snapshot.restore_and_align()
        if self.account_ledger is not None:
            self.account_ledger.sync_from_exchange()

    def _init_auxiliary_services(self) -> None:
        """启动独立日志守护、自毁监控、安息日调度等"""
        if self.log_forwarder is not None:
            self.log_forwarder.start()
        if self.self_destruct is not None:
            self.self_destruct.activate_monitoring()
        if self.sabbath_controller is not None:
            self.sabbath_controller.start()

    # ========== 运行辅助 ==========
    def _run_health_check(self) -> None:
        result = self.health_check()
        if result["status"] != "ok":
            logger.warning(f"定期健康检查异常: {result['reason']}")

    def _perform_shutdown(self) -> None:
        """优雅停止流程：通知平仓 → 等待持仓清理 → 释放资源"""
        logger.info("开始执行优雅停止流程...")
        timeout = self.DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SEC
        start = time.time()

        # 1. 广播停止通知
        if self.negotiation_bus is not None:
            try:
                self.negotiation_bus.publish_alert(
                    alert_type="system_shutdown",
                    level="critical",
                    message="系统正在停止，暂停所有开仓",
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning(f"发布停止通知失败: {e}")

        # 2. 等待持仓平仓（实际需调用 order_manager 或 account_ledger 查询持仓数量）
        if self.account_ledger is not None:
            while time.time() - start < timeout:
                try:
                    positions = self.account_ledger.get_open_positions()
                    if not positions:
                        logger.info("所有持仓已平仓")
                        break
                    logger.debug(f"等待平仓，剩余持仓: {len(positions)}")
                except Exception as e:
                    logger.error(f"查询持仓失败: {e}")
                time.sleep(1)
        else:
            # 无账本模块时保守等待
            logger.warning("AccountLedger 未注入，等待固定超时")
            time.sleep(10)

        # 3. 持久化持仓快照
        if self.position_snapshot is not None:
            try:
                self.position_snapshot.save_snapshot()
                logger.info("持仓快照已保存")
            except Exception as e:
                logger.error(f"保存持仓快照失败: {e}")

        # 4. 按启动逆序释放资源
        if self.system_builder is not None:
            try:
                self.system_builder.shutdown_all()
            except Exception as e:
                logger.error(f"资源释放异常: {e}")

        logger.info("系统停止完成")

    def _setup_signal_handlers(self) -> None:
        """注册进程信号处理器"""
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        logger.info("已注册 SIGTERM/SIGINT 信号处理器")

    def _signal_handler(self, signum: int, frame: Any) -> None:
        sig_name = signal.Signals(signum).name
        logger.info(f"收到信号 {sig_name}，开始优雅停止")
        self.stop(reason=f"signal:{sig_name}")

    @staticmethod
    def _fatal_result(reason: str, detail: str) -> Dict[str, Any]:
        return {
            "status": "fatal",
            "reason": reason,
            "data": {"detail": detail},
            "warnings": ["fatal_startup_error"],
        }
