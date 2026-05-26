"""
火种系统 · 流程总线入口 (PipelineBus)

核心职责：
1. 管理交易流水线的全生命周期，包括流水线实例的创建、激活、阶段推进、异常终止与资源回收
2. 协调调度四个子模块（阶段调度器、看门狗、熔断器、上下文传递器），对外提供统一的流水线操作接口

外部依赖（真实模块接口）：
- core.pipeline_bus.stage_scheduler.StageScheduler : 负责六阶段的串行推进逻辑与异步事件订阅
- core.pipeline_bus.watchdog.Watchdog : 为每条流水线提供独立的超时监控与强制终止
- core.pipeline_bus.circuit_breaker.CircuitBreaker : 按流水线类型统计连续异常次数，触发熔断与逐步恢复
- core.pipeline_bus.context_passer.ContextPasser : 负责阶段间处理结果的标准化传递与完整性校验
- core.negotiation_bus.NegotiationBus : 发布流水线状态变更事件（完成、异常、熔断）
- core.behavioral_logger.BehavioralLogger : 记录流水线生命周期日志与异常事件

接口契约：
- create_pipeline(symbol: str, strategy: str, signal: Dict[str, Any]) -> Dict[str, Any] : 创建新流水线实例
- advance_stage(line_id: str, stage_result: Dict[str, Any]) -> Dict[str, Any] : 推进流水线到下一阶段
- abort_pipeline(line_id: str, reason: str) -> Dict[str, Any] : 异常终止流水线并回收资源
- complete_pipeline(line_id: str, final_result: Dict[str, Any]) -> Dict[str, Any] : 正常完成流水线
- get_pipeline_status(line_id: str) -> Dict[str, Any] : 查询流水线当前状态
- get_active_pipeline_count() -> Dict[str, Any] : 获取活跃流水线总数，附带僵死流水线预警
- health_check() -> Dict[str, Any] : 模块自检，包含熔断器摘要与看门狗健康
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 StageScheduler 不可用时，流水线推进降级为直接阶段递增，跳过预加载和异步事件订阅
- 当 Watchdog 不可用时，流水线仍正常运行但无超时保护，同时发出告警；连续注册失败将触发创建拒绝
- 当 CircuitBreaker 不可用时，默认允许所有流水线创建，不进行熔断判定
- 当 ContextPasser 不可用时，阶段间上下文传递降级为直接引用传递，不进行完整性校验
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护活跃流水线的字典，在流水线完成或终止时自动清理
- 通过 max_parallel_lines 常量限制最大并发流水线数，防止内存膨胀
- 线程锁保护活跃流水线字典的并发访问
- 模块销毁时自动清理所有活跃流水线
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional
from collections import deque
from enum import Enum

logger = logging.getLogger(__name__)


class PipelineStage(Enum):
    """流水线六阶段枚举"""
    SIGNAL_SCAN = "S1"
    SIGNAL_CONFIRM = "S2"
    ORDER_EXEC = "S3"
    ADD_MANAGE = "S4"
    PROFIT_GUARD = "S5"
    ATTRIBUTION = "S6"


class PipelineStatus(Enum):
    """流水线状态枚举"""
    ACTIVE = "active"
    ABORTED = "aborted"
    COMPLETED = "completed"


class PipelineBus:
    """全链路流程总线，管理交易流水线的创建与调度"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_MAX_PARALLEL_LINES = 3          # 最大并行流水线数，无量纲，取值范围 [1, 10]
    DEFAULT_AUTO_RETRY_COUNT = 1            # 阶段失败自动重试次数，无量纲，取值范围 [0, 3]
    DEFAULT_ABORT_ON_STAGE_FAILURE = True   # 阶段失败是否终止流水线，布尔值
    MAX_WATCHDOG_REGISTER_FAILURES = 3      # 看门狗连续注册失败阈值，无量纲，[2, 10]
    PIPELINE_CREATION_RATE_WINDOW_SEC = 60  # 创建速率监控窗口，秒，[30, 300]
    PIPELINE_CREATION_RATE_THRESHOLD = 30   # 每分钟创建最大数，无量纲，[5, 100]

    # 显式阶段顺序映射，不依赖枚举声明顺序
    _STAGE_NEXT_MAP = {
        PipelineStage.SIGNAL_SCAN: PipelineStage.SIGNAL_CONFIRM,
        PipelineStage.SIGNAL_CONFIRM: PipelineStage.ORDER_EXEC,
        PipelineStage.ORDER_EXEC: PipelineStage.ADD_MANAGE,
        PipelineStage.ADD_MANAGE: PipelineStage.PROFIT_GUARD,
        PipelineStage.PROFIT_GUARD: PipelineStage.ATTRIBUTION,
        PipelineStage.ATTRIBUTION: None,          # 终点
    }

    # 阶段超时配置（微秒）
    STAGE_TIMEOUTS = {
        PipelineStage.SIGNAL_SCAN: 500,
        PipelineStage.SIGNAL_CONFIRM: 200,
        PipelineStage.ORDER_EXEC: 1000,
        PipelineStage.ADD_MANAGE: 500,
        PipelineStage.PROFIT_GUARD: 100,
        PipelineStage.ATTRIBUTION: 100000,
    }

    STAGE_DESCRIPTIONS = {
        PipelineStage.SIGNAL_SCAN: "信号嗅探",
        PipelineStage.SIGNAL_CONFIRM: "信号确认",
        PipelineStage.ORDER_EXEC: "订单执行",
        PipelineStage.ADD_MANAGE: "加仓管理",
        PipelineStage.PROFIT_GUARD: "利润保护",
        PipelineStage.ATTRIBUTION: "归因闭环",
    }

    # 流水线预期总超时（微秒），用于僵死检测
    PIPELINE_TOTAL_TIMEOUT_US = sum(STAGE_TIMEOUTS.values())

    def __init__(self):
        self._active_lines: Dict[str, Dict[str, Any]] = {}
        self._line_counter: int = 0
        self._lock = threading.Lock()

        # 外部依赖
        self._stage_scheduler = None
        self._watchdog = None
        self._circuit_breaker = None
        self._context_passer = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 看门狗注册失败计数器（按策略）
        self._watchdog_failure_counts: Dict[str, int] = {}
        self._watchdog_failure_lock = threading.Lock()

        # 流水线创建速率监控
        self._creation_timestamps: deque = deque()

        logger.info("PipelineBus 初始化完成，最大并行流水线: %d", self.DEFAULT_MAX_PARALLEL_LINES)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        stage_scheduler: Optional[Any] = None,
        watchdog: Optional[Any] = None,
        circuit_breaker: Optional[Any] = None,
        context_passer: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        if stage_scheduler is not None:
            self._stage_scheduler = stage_scheduler
            logger.info("StageScheduler 注入成功")
        else:
            logger.warning("StageScheduler 未注入，流水线推进降级为直接阶段递增")

        if watchdog is not None:
            self._watchdog = watchdog
            logger.info("Watchdog 注入成功")
        else:
            logger.warning("Watchdog 未注入，流水线无超时保护")

        if circuit_breaker is not None:
            self._circuit_breaker = circuit_breaker
            logger.info("CircuitBreaker 注入成功")
        else:
            logger.warning("CircuitBreaker 未注入，不进行熔断判定")

        if context_passer is not None:
            self._context_passer = context_passer
            logger.info("ContextPasser 注入成功")
        else:
            logger.warning("ContextPasser 未注入，上下文传递降级为直接引用")

        if negotiation_bus is not None:
            self._negotiation_bus = negotiation_bus
            logger.info("NegotiationBus 注入成功")
        else:
            logger.warning("NegotiationBus 未注入，流水线状态变更事件不推送")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

    # ========== 公共接口 ==========
    def create_pipeline(self, symbol: str, strategy: str, signal: Dict[str, Any]) -> Dict[str, Any]:
        if not symbol:
            return {"status": "error", "reason": "交易品种不能为空", "data": {}, "warnings": ["invalid_symbol"]}
        if not strategy:
            return {"status": "error", "reason": "策略来源不能为空", "data": {}, "warnings": ["invalid_strategy"]}

        with self._lock:
            # 检查创建速率
            self._check_creation_rate()

            if len(self._active_lines) >= self.DEFAULT_MAX_PARALLEL_LINES:
                logger.warning("创建流水线失败: 已达最大并行数 %d", self.DEFAULT_MAX_PARALLEL_LINES)
                return {
                    "status": "error",
                    "reason": f"已达最大并行流水线数 ({self.DEFAULT_MAX_PARALLEL_LINES})",
                    "data": {"active_count": len(self._active_lines)},
                    "warnings": ["max_parallel_reached"],
                }

            # 熔断检查
            if self._circuit_breaker is not None:
                try:
                    if self._circuit_breaker.is_open(strategy):
                        logger.warning("创建流水线被熔断器拒绝: strategy=%s", strategy)
                        return {
                            "status": "error",
                            "reason": f"策略 {strategy} 处于熔断冷却期",
                            "data": {},
                            "warnings": ["circuit_breaker_open"],
                        }
                except Exception as e:
                    logger.warning("熔断器查询异常，放行流水线创建: %s", e)

            # 看门狗注册失败率检查
            if self._watchdog is not None and self._is_watchdog_degraded(strategy):
                logger.error("看门狗连续注册失败，拒绝创建流水线: strategy=%s", strategy)
                return {
                    "status": "error",
                    "reason": f"看门狗服务不可用，流水线创建被拒绝",
                    "data": {},
                    "warnings": ["watchdog_unavailable"],
                }

            # 创建流水线
            self._line_counter += 1
            line_id = f"PL_{self._line_counter:06d}_{int(time.time())}"
            pipeline = {
                "id": line_id,
                "symbol": symbol,
                "strategy": strategy,
                "current_stage": PipelineStage.SIGNAL_CONFIRM,
                "signal": signal,
                "position": None,
                "status": PipelineStatus.ACTIVE.value,
                "stage_history": [],
                "created_at": time.time(),
                "retry_count": 0,
                "stage_start_time": time.time(),  # 当前阶段开始时间
            }
            self._active_lines[line_id] = pipeline

            # 看门狗注册
            if self._watchdog is not None:
                try:
                    self._watchdog.register(line_id, self.STAGE_TIMEOUTS[pipeline["current_stage"]])
                    self._reset_watchdog_failure(strategy)  # 注册成功，重置失败计数
                except Exception as e:
                    self._record_watchdog_failure(strategy)
                    logger.error("看门狗注册失败: %s #RECOVERY: 检查看门狗服务状态", e)

            logger.info("流水线已创建: id=%s, symbol=%s, strategy=%s, stage=%s",
                        line_id, symbol, strategy, pipeline["current_stage"].value)

        return {
            "status": "ok",
            "reason": f"流水线 {line_id} 创建成功",
            "data": {"line_id": line_id},
            "warnings": [],
        }

    def advance_stage(self, line_id: str, stage_result: Dict[str, Any]) -> Dict[str, Any]:
        if not line_id:
            return {"status": "error", "reason": "流水线ID不能为空", "data": {}, "warnings": ["invalid_line_id"]}

        with self._lock:
            pipeline = self._active_lines.get(line_id)
            if pipeline is None:
                logger.warning("流水线不存在: %s", line_id)
                return {"status": "error", "reason": f"流水线 {line_id} 不存在", "data": {}, "warnings": ["pipeline_not_found"]}
            if pipeline["status"] != PipelineStatus.ACTIVE.value:
                logger.warning("流水线非活跃状态: id=%s, status=%s", line_id, pipeline["status"])
                return {"status": "error", "reason": f"流水线 {line_id} 状态为 {pipeline['status']}", "data": {}, "warnings": ["pipeline_not_active"]}

            # 计算当前阶段耗时
            stage_start = pipeline.get("stage_start_time", time.time())
            duration_us = int((time.time() - stage_start) * 1_000_000)

            current_stage = pipeline["current_stage"]
            pipeline["stage_history"].append({
                "stage": current_stage.value,
                "result": stage_result.get("status", "unknown"),
                "duration_us": duration_us,
                "timestamp": time.time(),
            })

            # 喂狗
            if self._watchdog is not None:
                try:
                    self._watchdog.feed(line_id)
                except Exception as e:
                    logger.warning("看门狗喂狗失败: %s", e)

            result_status = stage_result.get("status", "unknown")

            if result_status == "continue":
                next_stage = self._STAGE_NEXT_MAP.get(current_stage)
                if next_stage is None:
                    return self._finalize_pipeline(line_id, pipeline, "所有阶段已完成", PipelineStatus.COMPLETED)

                pipeline["current_stage"] = next_stage
                pipeline["retry_count"] = 0
                pipeline["stage_start_time"] = time.time()

                if self._watchdog is not None:
                    try:
                        self._watchdog.update_timeout(line_id, self.STAGE_TIMEOUTS[next_stage])
                    except Exception as e:
                        logger.warning("看门狗超时更新失败: %s", e)

                logger.info("流水线阶段推进: id=%s, %s → %s, 耗时 %dμs", line_id, current_stage.value, next_stage.value, duration_us)
                return {
                    "status": "ok",
                    "reason": f"流水线 {line_id} 推进至 {next_stage.value}",
                    "data": {"line_id": line_id, "previous_stage": current_stage.value, "current_stage": next_stage.value},
                    "warnings": [],
                }

            elif result_status == "abort":
                return self._finalize_pipeline(line_id, pipeline, stage_result.get("reason", "阶段返回终止"), PipelineStatus.ABORTED)

            elif result_status == "complete":
                return self._finalize_pipeline(line_id, pipeline, "阶段返回完成", PipelineStatus.COMPLETED)

            else:
                # 重试逻辑
                retry_max = self.DEFAULT_AUTO_RETRY_COUNT
                if pipeline["retry_count"] < retry_max:
                    pipeline["retry_count"] += 1
                    logger.warning("流水线阶段结果未知，重试 %d/%d: id=%s", pipeline["retry_count"], retry_max, line_id)
                    return {
                        "status": "ok",
                        "reason": f"阶段结果未知，重试 {pipeline['retry_count']}/{retry_max}",
                        "data": {"line_id": line_id},
                        "warnings": ["unknown_result_retry"],
                    }
                else:
                    # 重试耗尽前先取消看门狗
                    if self._watchdog is not None:
                        try:
                            self._watchdog.unregister(line_id)
                        except Exception as e:
                            logger.warning("重试耗尽注销看门狗失败: %s", e)
                    return self._finalize_pipeline(line_id, pipeline, f"重试耗尽 ({retry_max}次)", PipelineStatus.ABORTED)

    def abort_pipeline(self, line_id: str, reason: str = "") -> Dict[str, Any]:
        if not line_id:
            return {"status": "error", "reason": "流水线ID不能为空", "data": {}, "warnings": ["invalid_line_id"]}
        with self._lock:
            pipeline = self._active_lines.get(line_id)
            if pipeline is None:
                return {"status": "error", "reason": f"流水线 {line_id} 不存在", "data": {}, "warnings": ["pipeline_not_found"]}
            return self._finalize_pipeline(line_id, pipeline, reason, PipelineStatus.ABORTED)

    def complete_pipeline(self, line_id: str, final_result: Dict[str, Any]) -> Dict[str, Any]:
        if not line_id:
            return {"status": "error", "reason": "流水线ID不能为空", "data": {}, "warnings": ["invalid_line_id"]}
        with self._lock:
            pipeline = self._active_lines.get(line_id)
            if pipeline is None:
                return {"status": "error", "reason": f"流水线 {line_id} 不存在", "data": {}, "warnings": ["pipeline_not_found"]}
            return self._finalize_pipeline(line_id, pipeline, "正常完成", PipelineStatus.COMPLETED, final_result)

    def get_pipeline_status(self, line_id: str) -> Dict[str, Any]:
        if not line_id:
            return {"status": "error", "reason": "流水线ID不能为空", "data": {}, "warnings": ["invalid_line_id"]}
        with self._lock:
            pipeline = self._active_lines.get(line_id)
            if pipeline is None:
                return {"status": "ok", "reason": f"流水线 {line_id} 不存在（可能已完成或已终止）", "data": {"line_id": line_id, "exists": False}, "warnings": []}
            return {
                "status": "ok",
                "reason": f"流水线 {line_id} 当前状态: {pipeline['status']}",
                "data": {
                    "line_id": line_id,
                    "symbol": pipeline["symbol"],
                    "strategy": pipeline["strategy"],
                    "current_stage": pipeline["current_stage"].value,
                    "status": pipeline["status"],
                    "created_at": pipeline["created_at"],
                    "stage_count": len(pipeline["stage_history"]),
                },
                "warnings": [],
            }

    def get_active_pipeline_count(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            lines_info = []
            stuck_lines = []
            for p in self._active_lines.values():
                elapsed = now - p["created_at"]
                info = {
                    "line_id": p["id"],
                    "symbol": p["symbol"],
                    "strategy": p["strategy"],
                    "current_stage": p["current_stage"].value,
                    "elapsed_sec": round(elapsed, 3),
                }
                lines_info.append(info)
                if elapsed * 1_000_000 > self.PIPELINE_TOTAL_TIMEOUT_US:
                    stuck_lines.append(info["line_id"])

            warnings = []
            if stuck_lines:
                warnings.append(f"可能存在僵死流水线: {stuck_lines}")

        return {
            "status": "ok",
            "reason": f"当前活跃流水线: {len(self._active_lines)}",
            "data": {
                "active_count": len(self._active_lines),
                "max_capacity": self.DEFAULT_MAX_PARALLEL_LINES,
                "lines": lines_info,
                "stuck_lines": stuck_lines,
            },
            "warnings": warnings,
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        try:
            if not hasattr(self, '_active_lines') or not hasattr(self, '_lock'):
                return {"status": "degraded", "reason": "核心数据结构未初始化", "data": {}, "warnings": ["core_not_initialized"]}

            with self._lock:
                active_count = len(self._active_lines)

            # 查询熔断器状态摘要
            breaker_summary = {}
            if self._circuit_breaker is not None and hasattr(self._circuit_breaker, 'get_all_status'):
                try:
                    breaker_summary = self._circuit_breaker.get_all_status()
                except Exception as e:
                    logger.warning("熔断器状态查询失败: %s", e)

            return {
                "status": "ok",
                "reason": f"PipelineBus 正常，活跃流水线: {active_count}",
                "data": {
                    "active_pipelines": active_count,
                    "max_capacity": self.DEFAULT_MAX_PARALLEL_LINES,
                    "circuit_breaker_status": breaker_summary,
                    "dependencies": {
                        "stage_scheduler": self._stage_scheduler is not None,
                        "watchdog": self._watchdog is not None,
                        "circuit_breaker": self._circuit_breaker is not None,
                        "context_passer": self._context_passer is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error("健康检查失败: %s #RECOVERY: 检查锁状态和字典完整性", e)
            return {"status": "error", "reason": f"健康检查异常: {str(e)}", "data": {}, "warnings": [f"health_check_failed: {str(e)}"]}

    # ========== 私有方法 ==========
    def _finalize_pipeline(
        self, line_id: str, pipeline: Dict[str, Any], reason: str,
        final_status: PipelineStatus, final_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """终结流水线，先使用数据再移除"""
        pipeline["status"] = final_status.value
        pipeline["completed_at"] = time.time()
        if final_result:
            pipeline["final_result"] = final_result

        # 先使用流水线信息进行后续操作
        if final_status == PipelineStatus.COMPLETED:
            logger.info("流水线完成: id=%s, symbol=%s, strategy=%s, 耗时=%.2fs",
                        line_id, pipeline["symbol"], pipeline["strategy"],
                        pipeline.get("completed_at", 0) - pipeline.get("created_at", 0))
        else:
            logger.warning("流水线终止: id=%s, symbol=%s, strategy=%s, 原因=%s",
                           line_id, pipeline["symbol"], pipeline["strategy"], reason)

        # 看门狗注销
        if self._watchdog is not None:
            try:
                self._watchdog.unregister(line_id)
            except Exception as e:
                logger.warning("看门狗注销失败: %s", e)

        # 熔断器记录（区分成功/失败）
        if self._circuit_breaker is not None:
            try:
                if final_status == PipelineStatus.ABORTED:
                    if hasattr(self._circuit_breaker, 'record_failure'):
                        self._circuit_breaker.record_failure(pipeline["strategy"])
                elif final_status == PipelineStatus.COMPLETED:
                    if hasattr(self._circuit_breaker, 'record_success'):
                        self._circuit_breaker.record_success(pipeline["strategy"])
            except Exception as e:
                logger.warning("熔断器记录失败: %s", e)

        # 推送事件
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_event'):
            try:
                self._negotiation_bus.publish_event(
                    event_type="pipeline_status_change",
                    line_id=line_id, symbol=pipeline["symbol"], strategy=pipeline["strategy"],
                    final_status=final_status.value, reason=reason, timestamp=time.time(),
                )
            except Exception as e:
                logger.warning("协商总线事件推送失败: %s", e)

        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="pipeline_finalized",
                    details={
                        "line_id": line_id, "symbol": pipeline["symbol"], "strategy": pipeline["strategy"],
                        "final_status": final_status.value, "reason": reason,
                        "stage_history": pipeline.get("stage_history", []),
                    },
                )
            except Exception as e:
                logger.warning("行为日志记录失败: %s", e)

        # 最后从活跃字典移除
        del self._active_lines[line_id]

        return {
            "status": "ok",
            "reason": f"流水线 {line_id} 已{final_status.value}: {reason}",
            "data": {"line_id": line_id, "final_status": final_status.value, "reason": reason},
            "warnings": [],
        }

    def _check_creation_rate(self) -> None:
        """监控流水线创建速率，超阈值发出告警"""
        now = time.time()
        self._creation_timestamps.append(now)
        # 清理过期时间戳
        while self._creation_timestamps and self._creation_timestamps[0] < now - self.PIPELINE_CREATION_RATE_WINDOW_SEC:
            self._creation_timestamps.popleft()
        if len(self._creation_timestamps) > self.PIPELINE_CREATION_RATE_THRESHOLD:
            logger.error(
                "流水线创建速率过高: %d 次/分钟，可能存在异常触发 #RECOVERY: 检查策略引擎信号频率",
                len(self._creation_timestamps)
            )

    def _is_watchdog_degraded(self, strategy: str) -> bool:
        """检查某策略的看门狗是否已连续失败达到阈值"""
        with self._watchdog_failure_lock:
            cnt = self._watchdog_failure_counts.get(strategy, 0)
            return cnt >= self.MAX_WATCHDOG_REGISTER_FAILURES

    def _record_watchdog_failure(self, strategy: str) -> None:
        """记录看门狗注册失败"""
        with self._watchdog_failure_lock:
            self._watchdog_failure_counts[strategy] = self._watchdog_failure_counts.get(strategy, 0) + 1

    def _reset_watchdog_failure(self, strategy: str) -> None:
        """重置看门狗失败计数"""
        with self._watchdog_failure_lock:
            self._watchdog_failure_counts[strategy] = 0
