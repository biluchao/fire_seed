"""
火种系统 · 流水线阶段调度器 (StageScheduler)

核心职责：
1. 管理交易流水线的六阶段串行推进，支持同步推进和异步事件订阅触发，确保每次阶段推进为原子操作
2. 在阶段切换后异步触发预加载，提前准备下一阶段所需的上下文数据，消除阶段切换延迟

外部依赖（真实模块接口）：
- core.pipeline_bus.stage_scheduler.PipelineStage : 六阶段枚举定义 (S1_SIGNAL_SCAN, S2_SIGNAL_CONFIRM, ..., S6_ATTRIBUTION)
- core.pipeline_bus.stage_scheduler.PipelineStatus : 流水线状态枚举 (ACTIVE, ABORTED, COMPLETED)
- core.signal_bus.signal_bus.SignalBus : 用于发布异步推进事件和订阅回调
- core.negotiation_bus.negotiation_bus.NegotiationBus : 用于获取阶段间所需模块的预协商数据
- core.behavioral_logger.BehavioralLogger : 记录阶段调度日志与异常事件

接口契约：
- schedule_next_stage(pipeline: Dict[str, Any]) -> Dict[str, Any] : 将指定流水线推进到下一阶段
- abort_pipeline(pipeline: Dict[str, Any], reason: str) -> Dict[str, Any] : 异常终止流水线并释放所有关联资源
- subscribe_event(event_type: str, callback) -> Dict[str, Any] : 订阅异步事件，当事件触发时自动推进流水线
- unsubscribe_event(event_type: str, callback) -> Dict[str, Any] : 取消订阅异步事件
- trigger_preload(pipeline: Dict[str, Any], target_stage: PipelineStage) -> Dict[str, Any] : 触发目标阶段的预加载
- register_transition(from_stage: PipelineStage, to_stage: Optional[PipelineStage]) -> None : 动态注册阶段推进路径
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 SignalBus 不可用时，异步事件订阅功能降级为同步轮询模式
- 当 NegotiationBus 不可用时或接口契约校验失败，预加载功能降级为空操作，不影响阶段推进
- 当流水线对象缺少必要字段时，返回标准化错误并记录 WARNING 日志，不抛出异常
- 预加载失败、日志记录失败均视为非致命错误，仅记录 WARNING 日志，不阻断阶段推进
- 所有降级值在类常量区明确声明

资源管理：
- 本模块内部统一管理所有流水线的阶段锁，在流水线终止或异常中断时自动释放，避免锁泄漏
- 提供 unsubscribe_event 方法允许显式取消订阅；流水线终末阶段或异常终止时自动清理相关订阅
- 模块销毁时通过 cleanup 方法释放所有回调与锁资源
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional, Callable
from enum import Enum

logger = logging.getLogger(__name__)


class PipelineStage(Enum):
    """流水线六阶段枚举定义（降级副本，当外部注入不可用时使用）"""
    S1_SIGNAL_SCAN = "S1"
    S2_SIGNAL_CONFIRM = "S2"
    S3_ORDER_EXEC = "S3"
    S4_ADD_MANAGE = "S4"
    S5_PROFIT_GUARD = "S5"
    S6_ATTRIBUTION = "S6"


class PipelineStatus(Enum):
    """流水线状态枚举（降级副本）"""
    ACTIVE = "active"
    ABORTED = "aborted"
    COMPLETED = "completed"


class StageScheduler:
    """流水线阶段调度器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_EVENT_TIMEOUT_SEC = 5.0       # 异步事件超时时间，秒，[1.0, 30.0]
    DEFAULT_MAX_RETRIES = 1               # 阶段推进失败最大重试次数，无量纲，[0, 3]
    DEFAULT_PRELOAD_ADVANCE_US = 500      # 预加载提前触发时间，微秒，[100, 2000]
    DEFAULT_EVENT_CLEANUP_INTERVAL = 300  # 事件回调清理间隔，秒，[60, 600]
    DEFAULT_STAGE_LATENCY_WARN_MS = 500   # 阶段停留时长告警阈值，毫秒，[100, 5000]
    DEFAULT_LOCK_WAIT_WARN_US = 100       # 阶段锁等待时长告警阈值，微秒，[50, 1000]

    # 预加载需求映射表（按阶段键名索引，新增阶段时在此追加）
    PRELOAD_REQUIREMENTS_MAP = {
        "S3": ["risk_profile", "liquidity_rating"],
        "S4": ["position_context", "compression_stage"],
        "S5": ["current_atr", "ma12_direction"],
    }

    # 阶段推进顺序映射表（默认值，可通过 register_transition 动态覆盖）
    STAGE_TRANSITION = {
        PipelineStage.S1_SIGNAL_SCAN: PipelineStage.S2_SIGNAL_CONFIRM,
        PipelineStage.S2_SIGNAL_CONFIRM: PipelineStage.S3_ORDER_EXEC,
        PipelineStage.S3_ORDER_EXEC: PipelineStage.S4_ADD_MANAGE,
        PipelineStage.S4_ADD_MANAGE: PipelineStage.S5_PROFIT_GUARD,
        PipelineStage.S5_PROFIT_GUARD: PipelineStage.S6_ATTRIBUTION,
        PipelineStage.S6_ATTRIBUTION: None,
    }

    def __init__(self):
        # 事件回调注册表: event_type -> list of callback functions
        self._event_subscribers: Dict[str, List[Callable]] = {}
        self._subscribers_lock = threading.Lock()

        # 流水线阶段锁注册表: pipeline_id -> Lock
        self._pipeline_locks: Dict[str, threading.Lock] = {}
        self._pipeline_locks_lock = threading.Lock()

        # 预加载统计
        self._preload_success: int = 0
        self._preload_failure: int = 0
        self._preload_stats_lock = threading.Lock()

        # 外部依赖注入
        self._signal_bus = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 最后清理时间
        self._last_cleanup = time.time()

        logger.info("StageScheduler 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        signal_bus: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）
        """
        if signal_bus is not None:
            self._signal_bus = signal_bus
            logger.info("SignalBus 注入成功")
        else:
            logger.warning("SignalBus 未注入，异步事件功能降级为同步轮询")

        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, 'preload_context'):
                logger.warning("NegotiationBus 缺少 preload_context 方法，预加载功能降级为空操作")
                self._negotiation_bus = None
            else:
                try:
                    import inspect
                    sig = inspect.signature(negotiation_bus.preload_context)
                    required_params = {"pipeline_id", "target_stage", "requirements"}
                    param_names = set(sig.parameters.keys())
                    if not required_params.issubset(param_names):
                        logger.warning(
                            f"NegotiationBus.preload_context 缺少必要参数: {required_params - param_names}"
                        )
                        self._negotiation_bus = None
                    else:
                        self._negotiation_bus = negotiation_bus
                        logger.info("NegotiationBus 注入成功，接口契约校验通过")
                except Exception as e:
                    logger.warning(f"NegotiationBus 接口契约校验失败: {e}")
                    self._negotiation_bus = None
        else:
            logger.warning("NegotiationBus 未注入，预加载功能降级为空操作")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，事件日志降级为标准 logger")

    # ========== 阶段锁管理 ==========
    def _get_pipeline_lock(self, pipeline_id: str) -> threading.Lock:
        """获取或创建指定流水线的阶段锁"""
        with self._pipeline_locks_lock:
            if pipeline_id not in self._pipeline_locks:
                self._pipeline_locks[pipeline_id] = threading.Lock()
            return self._pipeline_locks[pipeline_id]

    def _release_pipeline_lock(self, pipeline_id: str) -> None:
        """在流水线终止时释放其阶段锁"""
        with self._pipeline_locks_lock:
            self._pipeline_locks.pop(pipeline_id, None)

    # ========== 公共接口 ==========
    def schedule_next_stage(self, pipeline: Dict[str, Any]) -> Dict[str, Any]:
        """
        将指定流水线从当前阶段原子推进到下一阶段

        Args:
            pipeline: 流水线对象，必须包含 id (str), current_stage (PipelineStage), status (str),
                      stage_history (List[Dict])，以及各阶段所需的上下文数据

        Returns:
            标准响应字典，data 中包含更新后的 pipeline 对象
        """
        if not pipeline or not isinstance(pipeline, dict):
            logger.warning("无效的流水线对象")
            return {
                "status": "error",
                "reason": "流水线对象为空或类型无效",
                "data": {},
                "warnings": ["invalid_pipeline"],
            }

        pipeline_id = pipeline.get("id")
        if not pipeline_id:
            logger.warning("流水线缺少 id 字段")
            return {
                "status": "error",
                "reason": "流水线缺少 id 字段",
                "data": {"pipeline": pipeline},
                "warnings": ["missing_pipeline_id"],
            }

        current_stage = pipeline.get("current_stage")
        if current_stage is None:
            logger.warning(f"流水线 {pipeline_id} 缺少 current_stage 字段")
            return {
                "status": "error",
                "reason": "流水线缺少 current_stage 字段",
                "data": {"pipeline": pipeline},
                "warnings": ["missing_current_stage"],
            }

        pipe_lock = self._get_pipeline_lock(pipeline_id)

        wait_start = time.perf_counter()
        acquired = pipe_lock.acquire(timeout=self.DEFAULT_EVENT_TIMEOUT_SEC)
        wait_duration_us = (time.perf_counter() - wait_start) * 1_000_000

        if not acquired:
            logger.error(f"流水线 {pipeline_id} 阶段锁获取超时 #RECOVERY: 检查是否有死锁或长时间持有锁的情况")
            return {
                "status": "error",
                "reason": "阶段锁获取超时，可能存在死锁",
                "data": {"pipeline_id": pipeline_id},
                "warnings": ["lock_timeout"],
            }

        if wait_duration_us > self.DEFAULT_LOCK_WAIT_WARN_US:
            logger.warning(
                f"流水线 {pipeline_id} 阶段锁等待时间 {wait_duration_us:.0f}μs，超过阈值 {self.DEFAULT_LOCK_WAIT_WARN_US}μs"
            )

        try:
            status = pipeline.get("status", "active")
            current_stage_snapshot = pipeline.get("current_stage")

            # 状态检查：若是 ABORTED 状态，也执行清理
            if status == PipelineStatus.ABORTED.value:
                logger.debug(f"流水线 {pipeline_id} 已中止，不再推进")
                return {
                    "status": "ok",
                    "reason": f"流水线已中止，无需推进",
                    "data": {"pipeline": pipeline, "advanced": False},
                    "warnings": [],
                }
            if status != PipelineStatus.ACTIVE.value:
                logger.debug(f"流水线 {pipeline_id} 状态为 {status}，不再推进")
                return {
                    "status": "ok",
                    "reason": f"流水线已处于终态 ({status})，无需推进",
                    "data": {"pipeline": pipeline, "advanced": False},
                    "warnings": [],
                }

            next_stage = self.STAGE_TRANSITION.get(current_stage_snapshot)
            if next_stage is None:
                # 终末阶段，标记为完成
                pipeline["status"] = PipelineStatus.COMPLETED.value
                pipeline["completed_at"] = time.time()
                stage_to_log = current_stage_snapshot.value
                pipeline_id_to_log = pipeline_id
                # 释放锁和订阅在锁外进行
            else:
                previous_stage = current_stage_snapshot
                now = time.time()
                pipeline["current_stage"] = next_stage
                pipeline["stage_history"].append({
                    "from_stage": previous_stage.value,
                    "to_stage": next_stage.value,
                    "timestamp": now,
                    "trigger": "scheduled",
                })

                last_ts = 0
                for entry in reversed(pipeline["stage_history"][:-1]):
                    if entry["to_stage"] == previous_stage.value:
                        last_ts = entry["timestamp"]
                        break
                if last_ts > 0 and (now - last_ts) * 1000 > self.DEFAULT_STAGE_LATENCY_WARN_MS:
                    logger.warning(
                        f"流水线 {pipeline_id} 阶段 {previous_stage.value} "
                        f"停留时长 {(now - last_ts)*1000:.0f}ms，超过阈值 {self.DEFAULT_STAGE_LATENCY_WARN_MS}ms"
                    )

                logger.info(
                    "流水线 %s 阶段推进: %s -> %s",
                    pipeline_id,
                    previous_stage.value,
                    next_stage.value,
                )
                pipeline_id_to_log = pipeline_id
                from_stage_str = previous_stage.value
                to_stage_str = next_stage.value
                preload_target = next_stage
        finally:
            pipe_lock.release()

        # 处理终末阶段
        if next_stage is None:
            self._release_pipeline_lock(pipeline_id_to_log)
            self._cleanup_pipeline_subscriptions(pipeline_id_to_log)
            self._log_event("pipeline_completed", pipeline_id_to_log, {
                "final_stage": stage_to_log,
            })
            return {
                "status": "ok",
                "reason": f"流水线已到达终末阶段 ({stage_to_log})",
                "data": {"pipeline": pipeline, "advanced": False, "terminal": True},
                "warnings": [],
            }

        # 锁外执行预加载
        try:
            self._trigger_preload_internal(pipeline, preload_target)
        except Exception as e:
            logger.warning(
                f"预加载触发异常(非致命): {e} #RECOVERY: 检查 NegotiationBus 连接状态",
                exc_info=True
            )

        # 锁外执行日志
        try:
            self._log_event("stage_advanced", pipeline_id_to_log, {
                "from_stage": from_stage_str,
                "to_stage": to_stage_str,
            })
        except Exception as e:
            logger.warning(f"事件日志记录失败(非致命): {e}")

        return {
            "status": "ok",
            "reason": f"流水线阶段推进成功: {from_stage_str} -> {to_stage_str}",
            "data": {"pipeline": pipeline, "advanced": True, "new_stage": to_stage_str},
            "warnings": [],
        }

    def abort_pipeline(self, pipeline: Dict[str, Any], reason: str) -> Dict[str, Any]:
        """
        异常终止指定流水线，释放所有关联资源

        Args:
            pipeline: 流水线对象
            reason: 终止原因

        Returns:
            标准响应字典
        """
        pipeline_id = pipeline.get("id")
        if not pipeline_id:
            return {
                "status": "error",
                "reason": "流水线缺少 id 字段",
                "data": {},
                "warnings": ["missing_pipeline_id"],
            }

        pipe_lock = self._get_pipeline_lock(pipeline_id)
        with pipe_lock:
            pipeline["status"] = PipelineStatus.ABORTED.value
            pipeline["aborted_at"] = time.time()
            pipeline["abort_reason"] = reason

        self._release_pipeline_lock(pipeline_id)
        self._cleanup_pipeline_subscriptions(pipeline_id)

        logger.warning(f"流水线 {pipeline_id} 已终止: {reason}")
        self._log_event("pipeline_aborted", pipeline_id, {"reason": reason})

        return {
            "status": "ok",
            "reason": f"流水线已终止: {reason}",
            "data": {"pipeline": pipeline},
            "warnings": ["pipeline_aborted"],
        }

    def subscribe_event(self, event_type: str, callback: Callable) -> Dict[str, Any]:
        if not event_type or not callable(callback):
            return {
                "status": "error",
                "reason": "event_type 不能为空且 callback 必须可调用",
                "data": {},
                "warnings": ["invalid_subscribe_params"],
            }

        with self._subscribers_lock:
            if event_type not in self._event_subscribers:
                self._event_subscribers[event_type] = []
            self._event_subscribers[event_type].append(callback)
            subscriber_count = len(self._event_subscribers[event_type])

        if self._signal_bus is not None and hasattr(self._signal_bus, 'subscribe'):
            try:
                self._signal_bus.subscribe(event_type, callback)
                logger.debug(f"事件 {event_type} 通过 SignalBus 注册成功")
            except Exception as e:
                logger.warning(f"SignalBus 事件注册失败: {e}，使用本地回调列表")
        else:
            logger.debug(f"事件 {event_type} 仅注册到本地回调列表 (SignalBus 不可用)")

        return {
            "status": "ok",
            "reason": f"事件 {event_type} 订阅成功 (当前订阅者: {subscriber_count})",
            "data": {"event_type": event_type, "subscriber_count": subscriber_count},
            "warnings": [],
        }

    def unsubscribe_event(self, event_type: str, callback: Callable) -> Dict[str, Any]:
        if not event_type or not callable(callback):
            return {
                "status": "error",
                "reason": "event_type 不能为空且 callback 必须可调用",
                "data": {},
                "warnings": ["invalid_unsubscribe_params"],
            }

        with self._subscribers_lock:
            if event_type in self._event_subscribers:
                try:
                    self._event_subscribers[event_type].remove(callback)
                    removed = True
                except ValueError:
                    removed = False
            else:
                removed = False

        if self._signal_bus is not None and hasattr(self._signal_bus, 'unsubscribe'):
            try:
                self._signal_bus.unsubscribe(event_type, callback)
            except Exception as e:
                logger.warning(f"SignalBus 取消订阅失败: {e}")

        return {
            "status": "ok",
            "reason": f"事件 {event_type} 取消订阅{'成功' if removed else '失败(未找到)'}",
            "data": {"event_type": event_type, "removed": removed},
            "warnings": [],
        }

    def trigger_preload(self, pipeline: Dict[str, Any], target_stage: PipelineStage) -> Dict[str, Any]:
        if not pipeline:
            return {
                "status": "error",
                "reason": "流水线对象为空",
                "data": {},
                "warnings": ["invalid_pipeline"],
            }

        if target_stage is None:
            return {
                "status": "error",
                "reason": "目标阶段不能为空",
                "data": {},
                "warnings": ["invalid_target_stage"],
            }

        success = self._trigger_preload_internal(pipeline, target_stage)
        return {
            "status": "ok",
            "reason": f"阶段 {target_stage.value} 预加载完成",
            "data": {"pipeline": pipeline, "preloaded_stage": target_stage.value},
            "warnings": [] if success else ["preload_degraded"],
        }

    @classmethod
    def register_transition(cls, from_stage: PipelineStage, to_stage: Optional[PipelineStage]) -> None:
        original = cls.STAGE_TRANSITION.get(from_stage)
        cls.STAGE_TRANSITION[from_stage] = to_stage
        if not cls._validate_reachability():
            cls.STAGE_TRANSITION[from_stage] = original
            logger.error(
                f"拒绝注册路径 {from_stage.value} -> {to_stage.value if to_stage else 'TERMINAL'}，产生循环依赖"
            )
            return
        logger.info(f"注册阶段路径: {from_stage.value} -> {to_stage.value if to_stage else 'TERMINAL'}")

    @classmethod
    def _validate_reachability(cls) -> bool:
        for start_stage in cls.STAGE_TRANSITION:
            visited = set()
            current = start_stage
            steps = 0
            while current is not None and current not in visited:
                visited.add(current)
                current = cls.STAGE_TRANSITION.get(current)
                steps += 1
                if steps > len(cls.STAGE_TRANSITION) + 1:
                    return False
        return True

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        try:
            with self._subscribers_lock:
                subscriber_count = sum(len(v) for v in self._event_subscribers.values())
                event_types = list(self._event_subscribers.keys())

            with self._preload_stats_lock:
                total = self._preload_success + self._preload_failure
                success_rate = (self._preload_success / total * 100) if total > 0 else 100.0

            with self._pipeline_locks_lock:
                active_locks = len(self._pipeline_locks)

            return {
                "status": "ok",
                "reason": (
                    f"StageScheduler 正常，订阅事件类型 {len(event_types)} 个，"
                    f"回调总数 {subscriber_count}，活跃阶段锁 {active_locks}，预加载成功率 {success_rate:.1f}%"
                ),
                "data": {
                    "subscriber_count": subscriber_count,
                    "event_types": event_types,
                    "active_pipeline_locks": active_locks,
                    "preload_success": self._preload_success,
                    "preload_failure": self._preload_failure,
                    "preload_success_rate": round(success_rate, 1),
                    "dependencies": {
                        "signal_bus": self._signal_bus is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查事件订阅者锁状态")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 显式资源清理 ==========
    def cleanup(self) -> None:
        try:
            if hasattr(self, '_signal_bus') and self._signal_bus is not None:
                with getattr(self, '_subscribers_lock', threading.Lock()):
                    subscribers = getattr(self, '_event_subscribers', {})
                    for event_type, callbacks in subscribers.items():
                        for cb in callbacks:
                            try:
                                if hasattr(self._signal_bus, 'unsubscribe'):
                                    self._signal_bus.unsubscribe(event_type, cb)
                            except Exception:
                                pass
            with getattr(self, '_pipeline_locks_lock', threading.Lock()):
                getattr(self, '_pipeline_locks', {}).clear()
            logger.debug("StageScheduler 资源清理完成")
        except Exception:
            pass

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass

    # ========== 私有方法 ==========
    def _trigger_preload_internal(self, pipeline: Dict[str, Any], target_stage: PipelineStage) -> bool:
        pipeline_id = pipeline.get("id", "unknown")
        requirements = self._get_preload_requirements(target_stage)
        if not requirements:
            return True

        if self._negotiation_bus is not None:
            try:
                context = self._negotiation_bus.preload_context(
                    pipeline_id=pipeline_id,
                    target_stage=target_stage.value,
                    requirements=requirements,
                )
                pipeline["preloaded_context"] = pipeline.get("preloaded_context", {})
                # 清理同一阶段的旧数据，防止无限膨胀
                pipeline["preloaded_context"][target_stage.value] = context
                for past_stage in self._get_past_stages(target_stage):
                    pipeline["preloaded_context"].pop(past_stage.value, None)
                with self._preload_stats_lock:
                    self._preload_success += 1
                logger.debug(f"流水线 {pipeline_id} 阶段 {target_stage.value} 预加载成功")
                return True
            except Exception as e:
                with self._preload_stats_lock:
                    self._preload_failure += 1
                logger.warning(
                    f"流水线 {pipeline_id} 阶段 {target_stage.value} 预加载失败: {e} "
                    f"(requirements={requirements}) #RECOVERY: 检查 NegotiationBus 连接与参数格式",
                    exc_info=True
                )
                return False
        else:
            logger.debug(f"NegotiationBus 不可用，阶段 {target_stage.value} 预加载降级为空操作")
            return False

    def _get_preload_requirements(self, stage: PipelineStage) -> Optional[List[str]]:
        return self.PRELOAD_REQUIREMENTS_MAP.get(stage.value)

    def _get_past_stages(self, current_stage: PipelineStage) -> List[PipelineStage]:
        """获取当前阶段之前的所有阶段"""
        past = []
        for stage, next_stage in self.STAGE_TRANSITION.items():
            if next_stage == current_stage:
                past.append(stage)
                # 递归添加更早的阶段
                past.extend(self._get_past_stages(stage))
        return past

    def _log_event(self, event_type: str, pipeline_id: str, details: Dict[str, Any]) -> None:
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type=f"pipeline_{event_type}",
                    details={
                        "pipeline_id": pipeline_id,
                        **details,
                    },
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

    def _cleanup_pipeline_subscriptions(self, pipeline_id: str) -> None:
        to_remove: List[tuple] = []
        with self._subscribers_lock:
            for event_type, callbacks in list(self._event_subscribers.items()):
                for cb in callbacks:
                    try:
                        if hasattr(cb, '__self__') and hasattr(cb.__self__, 'pipeline_id'):
                            if cb.__self__.pipeline_id == pipeline_id:
                                to_remove.append((event_type, cb))
                    except Exception:
                        pass
        for event_type, cb in to_remove:
            self.unsubscribe_event(event_type, cb)
        if to_remove:
            logger.debug(f"清理流水线 {pipeline_id} 的 {len(to_remove)} 个订阅")
