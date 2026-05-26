"""
火种系统 · 流水线看门狗 (PipelineWatchdog)

核心职责：
1. 为每条活跃的六阶段流水线提供独立超时监控，记录最后活跃时间
2. 由主循环周期性检查，当流水线在指定时限内未喂狗时，自动触发超时回调并清理资源

外部依赖（真实模块接口）：
- core.behavioral_logger.BehavioralLogger : 记录超时事件日志
- core.negotiation_bus.NegotiationBus : 发送超时告警与状态变更通知

接口契约：
- register(pipeline_id: str, timeout_ms: int, callback: Callable) -> Dict[str, Any] : 为流水线注册看门狗
- feed(pipeline_id: str) -> Dict[str, Any] : 喂狗，更新流水线的最后活跃时间
- unregister(pipeline_id: str) -> Dict[str, Any] : 正常结束流水线时注销看门狗
- check_timeout() -> Dict[str, Any] : 由主循环调用，检查超时并执行回调
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 若 BehavioralLogger 不可用，超时日志仅输出到标准 logger
- 若 NegotiationBus 不可用，超时告警降级为本地日志
- 回调函数执行异常时，记录错误并继续清理流程，保证不阻塞主循环
- 对未注册的 pipeline_id 执行 feed/unregister 时返回错误并记录 WARNING

资源管理：
- 本模块维护一个内部字典存储活跃监控信息，不持有任何外部资源句柄
- 超时或主动注销后，关联的条目自动从字典中移除，并释放回调引用
"""

import time
import logging
from typing import Dict, Any, List, Optional, Callable
from threading import Lock

logger = logging.getLogger(__name__)


class PipelineWatchdog:
    """流水线看门狗，负责超时检测与资源回收"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_STALE_THRESHOLD_MS = 60000     # 僵死判定阈值（超过此时间未喂狗视为异常），毫秒，[10000, 300000]

    def __init__(self):
        # 活跃监控条目：pipeline_id -> {
        #   "last_feed": float (最后活跃时间戳),
        #   "timeout_ms": int (超时阈值毫秒),
        #   "callback": Callable (超时回调)
        # }
        self._watchers: Dict[str, Dict[str, Any]] = {}

        # 外部依赖注入
        self._behavioral_logger = None
        self._negotiation_bus = None

        # 线程安全
        self._lock = Lock()

        # 最后一次检查时间（用于自检）
        self._last_check_time: float = 0.0

        logger.info("PipelineWatchdog 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        behavioral_logger: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）

        Args:
            behavioral_logger: 行为日志实例
            negotiation_bus: 协商总线实例
        """
        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，超时日志降级为标准 logger")

        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, 'publish_alert'):
                logger.warning("NegotiationBus 缺少 publish_alert 方法，超时告警降级为本地日志")
                self._negotiation_bus = None
            else:
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")

    # ========== 公共接口 ==========
    def register(self, pipeline_id: str, timeout_ms: int, callback: Callable) -> Dict[str, Any]:
        """
        为指定流水线注册超时监控

        Args:
            pipeline_id: 流水线唯一标识
            timeout_ms: 超时阈值（毫秒），取值范围 [1, 10000]
            callback: 超时触发的回调函数，无参数，无返回值

        Returns:
            标准响应字典，data 包含注册确认信息
        """
        # 参数校验
        if not pipeline_id:
            logger.warning("无效 pipeline_id (空字符串)")
            return {
                "status": "error",
                "reason": "pipeline_id 不能为空",
                "data": {},
                "warnings": ["invalid_pipeline_id"],
            }

        if not isinstance(timeout_ms, int) or timeout_ms <= 0:
            logger.warning(f"无效 timeout_ms: {timeout_ms}")
            return {
                "status": "error",
                "reason": f"timeout_ms 必须为正整数，当前值: {timeout_ms}",
                "data": {},
                "warnings": ["invalid_timeout"],
            }

        if not callable(callback):
            logger.warning("回调函数不可调用")
            return {
                "status": "error",
                "reason": "callback 必须为可调用对象",
                "data": {},
                "warnings": ["invalid_callback"],
            }

        with self._lock:
            if pipeline_id in self._watchers:
                logger.warning("流水线 %s 已注册，原监控将被覆盖", pipeline_id)
                # 显式释放旧回调，帮助 GC 回收可能持有的外部资源
                old_callback = self._watchers[pipeline_id].get("callback")
                if old_callback is not None:
                    del old_callback
                # 更新为新的监控参数
                self._watchers[pipeline_id]["last_feed"] = time.time()
                self._watchers[pipeline_id]["timeout_ms"] = timeout_ms
                self._watchers[pipeline_id]["callback"] = callback
                return {
                    "status": "ok",
                    "reason": f"流水线 {pipeline_id} 的超时监控已更新",
                    "data": {"pipeline_id": pipeline_id, "timeout_ms": timeout_ms},
                    "warnings": ["overwritten"],
                }

            # 新注册
            self._watchers[pipeline_id] = {
                "last_feed": time.time(),
                "timeout_ms": timeout_ms,
                "callback": callback,
            }

        logger.info("流水线 %s 注册超时监控 (超时=%dms)", pipeline_id, timeout_ms)
        return {
            "status": "ok",
            "reason": f"流水线 {pipeline_id} 已注册超时监控",
            "data": {"pipeline_id": pipeline_id, "timeout_ms": timeout_ms},
            "warnings": [],
        }

    def feed(self, pipeline_id: str) -> Dict[str, Any]:
        """
        喂狗，更新指定流水线的最后活跃时间

        Args:
            pipeline_id: 流水线唯一标识

        Returns:
            标准响应字典
        """
        with self._lock:
            watcher = self._watchers.get(pipeline_id)
            if watcher is None:
                logger.warning("尝试喂狗未注册的流水线: %s", pipeline_id)
                return {
                    "status": "error",
                    "reason": f"流水线 {pipeline_id} 未注册",
                    "data": {},
                    "warnings": ["pipeline_not_found"],
                }
            watcher["last_feed"] = time.time()

        logger.debug("流水线 %s 已喂狗", pipeline_id)
        return {
            "status": "ok",
            "reason": f"流水线 {pipeline_id} 喂狗成功",
            "data": {"pipeline_id": pipeline_id},
            "warnings": [],
        }

    def unregister(self, pipeline_id: str) -> Dict[str, Any]:
        """
        正常结束流水线时注销看门狗，并释放回调引用

        Args:
            pipeline_id: 流水线唯一标识

        Returns:
            标准响应字典
        """
        with self._lock:
            watcher = self._watchers.pop(pipeline_id, None)
            if watcher is None:
                logger.warning("尝试注销未注册的流水线: %s", pipeline_id)
                return {
                    "status": "error",
                    "reason": f"流水线 {pipeline_id} 未注册",
                    "data": {},
                    "warnings": ["pipeline_not_found"],
                }
            # 显式释放回调引用，帮助 GC
            if "callback" in watcher:
                del watcher["callback"]

        logger.info("流水线 %s 正常注销", pipeline_id)
        return {
            "status": "ok",
            "reason": f"流水线 {pipeline_id} 已注销",
            "data": {},
            "warnings": [],
        }

    def check_timeout(self) -> Dict[str, Any]:
        """
        由主循环调用，检查所有已注册流水线，对超时条目执行回调并清理

        Returns:
            标准响应字典，data 包含本周期超时流水线列表及活跃数量变化
        """
        now = time.time()
        timeout_entries = []  # 保存 (pipeline_id, callback)

        with self._lock:
            active_before = len(self._watchers)
            # 收集超时条目并立即从字典中移除，保证原子性
            for pid in list(self._watchers.keys()):
                watcher = self._watchers[pid]
                elapsed_ms = (now - watcher["last_feed"]) * 1000
                if elapsed_ms >= watcher["timeout_ms"]:
                    # 取出回调并保存，删除条目
                    callback = watcher.get("callback")
                    timeout_entries.append((pid, callback))
                    del self._watchers[pid]
                    # 显式释放回调引用
                    if callback is not None:
                        del callback
            active_after = len(self._watchers)

        # 更新最后一次检查时间
        self._last_check_time = now

        # 在锁外执行回调，避免死锁，并记录每个回调的执行耗时
        total_callback_duration = 0.0
        for pid, callback in timeout_entries:
            if callback is None:
                continue
            start = time.perf_counter()
            try:
                callback()
            except Exception as e:
                logger.error(
                    "流水线 %s 超时回调执行异常: %s #RECOVERY: 检查回调函数实现，确保内部捕获异常",
                    pid, e
                )
            finally:
                duration_ms = (time.perf_counter() - start) * 1000
                total_callback_duration += duration_ms
                if duration_ms > 10:  # 超过10ms记录慢回调
                    logger.warning("流水线 %s 超时回调执行耗时 %.2fms", pid, duration_ms)

        if timeout_entries:
            logger.warning(
                "发现 %d 条超时流水线: %s (总回调耗时 %.2fms)",
                len(timeout_entries),
                [pid for pid, _ in timeout_entries],
                total_callback_duration,
            )
            # 触发告警
            self._alert_timeout([pid for pid, _ in timeout_entries])
        else:
            logger.debug("无超时流水线")

        return {
            "status": "ok",
            "reason": f"检查完成，发现 {len(timeout_entries)} 条超时流水线",
            "data": {
                "timeout_count": len(timeout_entries),
                "timeout_ids": [pid for pid, _ in timeout_entries],
                "active_count_before": active_before,
                "active_count_after": active_after,
                "total_callback_duration_ms": round(total_callback_duration, 2),
            },
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检，检测是否有长期未喂狗的僵死流水线

        Returns:
            标准健康检查响应字典
        """
        try:
            with self._lock:
                active_count = len(self._watchers)
                now = time.time()
                stale_count = sum(
                    1 for w in self._watchers.values()
                    if (now - w["last_feed"]) * 1000 > self.DEFAULT_STALE_THRESHOLD_MS
                )
            # 检查自身是否被主循环按时调用（若超过 60 秒未被调度则预警）
            scheduling_delay = (time.time() - self._last_check_time) if self._last_check_time > 0 else 0
            warnings = []
            if scheduling_delay > 60:
                warnings.append(f"看门狗调度延迟 {scheduling_delay:.1f}s，可能主循环阻塞")
            if stale_count > 0:
                warnings.append(f"存在 {stale_count} 条可能僵死的流水线")

            return {
                "status": "ok",
                "reason": f"PipelineWatchdog 正常，活跃监控数: {active_count}",
                "data": {
                    "active_watchers": active_count,
                    "stale_watchers": stale_count,
                    "last_check_time": self._last_check_time,
                    "scheduling_delay_seconds": round(scheduling_delay, 1),
                    "dependencies": {
                        "behavioral_logger": self._behavioral_logger is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                    },
                },
                "warnings": warnings,
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查内部字典完整性和锁状态")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _alert_timeout(self, pipeline_ids: List[str]) -> None:
        """发送超时告警"""
        if not pipeline_ids:
            return
        message = f"流水线超时: {', '.join(pipeline_ids)}"
        # 尝试通过协商总线推送告警
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="pipeline_timeout",
                    pipeline_ids=pipeline_ids,
                    message=message,
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning(f"协商总线告警推送失败: {e}")
        # 本地日志
        logger.error(f"{message} #RECOVERY: 检查相关流水线阶段处理逻辑、上游数据延迟或死锁")

        # 行为日志
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="pipeline_timeout",
                    details={"pipeline_ids": pipeline_ids, "message": message},
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")
