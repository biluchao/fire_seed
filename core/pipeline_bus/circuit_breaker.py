"""
火种系统 · 流水线熔断器 (PipelineCircuitBreaker)

核心职责：
1. 按流水线类型独立统计连续异常次数，触发熔断后进入冷却期并逐步恢复
2. 维护每条流水线类型的失败序列与熔断状态，提供熔断判定、冷却管理、状态查询与健康检查

外部依赖（真实模块接口）：
- core.behavioral_logger.BehavioralLogger : 记录熔断触发、冷却、恢复等关键事件日志

接口契约：
- record_failure(pipeline_type: str) -> Dict[str, Any] : 记录一次失败，返回是否触发熔断
- record_success(pipeline_type: str) -> Dict[str, Any] : 记录一次成功，返回是否重置计数
- is_circuit_open(pipeline_type: str) -> Dict[str, Any] : 查询指定流水线类型是否处于熔断状态
- get_status(pipeline_type: str) -> Dict[str, Any] : 返回指定流水线类型的详细熔断状态
- get_all_status() -> Dict[str, Any] : 返回所有流水线类型的熔断状态汇总
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 BehavioralLogger 不可用时，日志降级为标准 logger，不影响熔断核心逻辑
- 所有降级值在类常量区明确声明

资源管理：
- 本模块仅维护内存中的失败计数与状态字典，不持有任何需要手动释放的外部资源
- 线程锁在模块销毁时自动释放
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class PipelineCircuitBreaker:
    """流水线熔断器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_FAILURE_WINDOW_SIZE = 10        # 连续失败计数窗口，无量纲，取值范围 [5, 50]
    DEFAULT_FAILURE_THRESHOLD = 5           # 触发熔断的连续失败次数，无量纲，取值范围 [3, 20]
    DEFAULT_COOLDOWN_SECONDS = 30           # 熔断冷却时间，秒，取值范围 [10, 300]
    DEFAULT_HALF_OPEN_LIMIT = 3             # 半开状态允许通过的最大请求数，无量纲，取值范围 [1, 10]
    DEFAULT_GRADUAL_REOPEN = True           # 冷却结束后是否逐步恢复，布尔值
    DEFAULT_MAX_RESET_COUNT = 1000          # 失败计数器的最大重置阈值，无量纲，取值范围 [100, 5000]
    DEFAULT_HALF_OPEN_TIMEOUT_MULTIPLIER = 2  # 半开状态超时为冷却时间的倍数，无量纲，取值范围 [1, 5]

    def __init__(self):
        # 每条流水线类型的连续失败计数
        self._failure_counts: Dict[str, int] = {}

        # 每条流水线类型的熔断状态
        self._circuit_state: Dict[str, Dict[str, Any]] = {}

        # 外部依赖注入
        self._behavioral_logger = None

        # 线程安全（保护 _failure_counts 和 _circuit_state 的并发访问）
        self._lock = threading.Lock()

        logger.info("PipelineCircuitBreaker 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）

        Args:
            behavioral_logger: 行为日志记录器，用于记录熔断事件
        """
        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，熔断事件日志降级为标准 logger")

    # ========== 公共接口 ==========
    def record_failure(self, pipeline_type: str) -> Dict[str, Any]:
        """
        记录指定流水线类型的一次失败，判断是否触发熔断

        Args:
            pipeline_type: 流水线类型标识（如 "trend_1m", "oscillation", "event_driven"）

        Returns:
            标准响应字典，data 中包含 failure_count、threshold、circuit_open 等字段
        """
        if not pipeline_type or not isinstance(pipeline_type, str):
            logger.warning(f"无效的流水线类型: {pipeline_type}")
            return {
                "status": "error",
                "reason": f"无效的流水线类型: {pipeline_type}",
                "data": {},
                "warnings": ["invalid_pipeline_type"],
            }

        with self._lock:
            # 初始化计数器
            if pipeline_type not in self._failure_counts:
                self._failure_counts[pipeline_type] = 0

            # 递增失败计数
            self._failure_counts[pipeline_type] += 1
            current_count = self._failure_counts[pipeline_type]

            # 防止计数器无限增长：超过阈值 2 倍时重置（缺陷四修复）
            if current_count > self.DEFAULT_FAILURE_THRESHOLD * 2:
                self._failure_counts[pipeline_type] = self.DEFAULT_FAILURE_THRESHOLD
                current_count = self.DEFAULT_FAILURE_THRESHOLD
                logger.debug("流水线 %s 失败计数超过上限，已重置为阈值", pipeline_type)

            # 判断是否触发熔断
            circuit_open = current_count >= self.DEFAULT_FAILURE_THRESHOLD

            if circuit_open:
                cooldown_end = time.time() + self.DEFAULT_COOLDOWN_SECONDS
                existing_state = self._circuit_state.get(pipeline_type, {})
                # 缺陷一修复：仅在当前状态仍为 open 时才延长冷却，防止覆盖半开/关闭状态
                if existing_state.get("state") == "open":
                    cooldown_end = max(
                        cooldown_end,
                        existing_state.get("cooldown_end", 0) + self.DEFAULT_COOLDOWN_SECONDS
                    )

                self._circuit_state[pipeline_type] = {
                    "state": "open",
                    "cooldown_end": cooldown_end,
                    "failure_count": current_count,
                    "half_open_count": 0,
                    "triggered_at": time.time(),
                }

                logger.warning(
                    "流水线熔断触发: type=%s, 连续失败=%d/%d #RECOVERY: 检查上游策略信号质量、市场数据完整性",
                    pipeline_type, current_count, self.DEFAULT_FAILURE_THRESHOLD
                )
                self._log_event("circuit_open", pipeline_type, {
                    "failure_count": current_count,
                    "cooldown_end": cooldown_end,
                })

            return {
                "status": "ok",
                "reason": f"流水线 {pipeline_type} 失败计数: {current_count}/{self.DEFAULT_FAILURE_THRESHOLD}",
                "data": {
                    "pipeline_type": pipeline_type,
                    "failure_count": current_count,
                    "threshold": self.DEFAULT_FAILURE_THRESHOLD,
                    "circuit_open": circuit_open,
                    "cooldown_end": self._circuit_state.get(pipeline_type, {}).get("cooldown_end"),
                },
                "warnings": ["circuit_breaker_triggered"] if circuit_open else [],
            }

    def record_success(self, pipeline_type: str) -> Dict[str, Any]:
        """
        记录指定流水线类型的一次成功，重置失败计数

        Args:
            pipeline_type: 流水线类型标识

        Returns:
            标准响应字典
        """
        if not pipeline_type or not isinstance(pipeline_type, str):
            logger.warning(f"无效的流水线类型: {pipeline_type}")
            return {
                "status": "error",
                "reason": f"无效的流水线类型: {pipeline_type}",
                "data": {},
                "warnings": ["invalid_pipeline_type"],
            }

        with self._lock:
            old_count = self._failure_counts.get(pipeline_type, 0)
            self._failure_counts[pipeline_type] = 0

            # 缺陷三修复：原子更新状态，确保失败计数与状态一致
            state_before = self._circuit_state.get(pipeline_type, {}).get("state")
            if state_before in ("open", "half_open"):
                self._circuit_state[pipeline_type] = {
                    "state": "closed",
                    "cooldown_end": 0,
                    "failure_count": 0,
                    "half_open_count": 0,
                    "recovered_at": time.time(),
                }
                logger.info("流水线熔断恢复: type=%s, 重置前失败计数=%d", pipeline_type, old_count)
                self._log_event("circuit_closed", pipeline_type, {"previous_failure_count": old_count})

        return {
            "status": "ok",
            "reason": f"流水线 {pipeline_type} 失败计数已重置 (重置前: {old_count})",
            "data": {
                "pipeline_type": pipeline_type,
                "previous_failure_count": old_count,
                "current_state": self._circuit_state.get(pipeline_type, {}).get("state", "closed"),
            },
            "warnings": [],
        }

    def is_circuit_open(self, pipeline_type: str) -> Dict[str, Any]:
        """
        查询指定流水线类型是否处于熔断状态（含冷却期自动恢复与半开逻辑）

        Args:
            pipeline_type: 流水线类型标识

        Returns:
            标准响应字典，data 中包含 is_open、reason、remaining_cooldown_seconds 等字段
        """
        if not pipeline_type or not isinstance(pipeline_type, str):
            return {
                "status": "error",
                "reason": f"无效的流水线类型: {pipeline_type}",
                "data": {"is_open": False},
                "warnings": ["invalid_pipeline_type"],
            }

        with self._lock:
            state = self._circuit_state.get(pipeline_type)

            # 无历史状态，熔断关闭
            if state is None:
                return {
                    "status": "ok",
                    "reason": "无历史熔断记录，电路关闭",
                    "data": {
                        "is_open": False,
                        "state": "closed",
                        "failure_count": self._failure_counts.get(pipeline_type, 0),
                    },
                    "warnings": [],
                }

            # 熔断已关闭
            if state.get("state") == "closed":
                return {
                    "status": "ok",
                    "reason": "熔断已关闭",
                    "data": {
                        "is_open": False,
                        "state": "closed",
                        "failure_count": self._failure_counts.get(pipeline_type, 0),
                    },
                    "warnings": [],
                }

            # 熔断打开中，检查冷却是否结束
            cooldown_end = state.get("cooldown_end", 0)
            now = time.time()

            if now >= cooldown_end:
                # 冷却结束，根据配置决定是否逐步恢复
                if self.DEFAULT_GRADUAL_REOPEN:
                    half_open_count = state.get("half_open_count", 0)
                    half_open_start = state.get("half_open_start", now)

                    # 缺陷二修复：半开状态超时检查，超过 2 倍冷却时间仍无法恢复则强制关闭
                    half_open_timeout = self.DEFAULT_COOLDOWN_SECONDS * self.DEFAULT_HALF_OPEN_TIMEOUT_MULTIPLIER
                    if now - half_open_start > half_open_timeout:
                        self._circuit_state[pipeline_type] = {
                            "state": "closed",
                            "cooldown_end": 0,
                            "failure_count": 0,
                            "half_open_count": 0,
                            "recovered_at": now,
                        }
                        logger.warning(
                            "流水线熔断半开超时自动恢复: type=%s, 半开持续 %.0f 秒",
                            pipeline_type, now - half_open_start
                        )
                        self._log_event("circuit_auto_recovered", pipeline_type, {"reason": "half_open_timeout"})
                        return {
                            "status": "ok",
                            "reason": "熔断半开超时，已自动恢复",
                            "data": {
                                "is_open": False,
                                "state": "closed",
                            },
                            "warnings": [],
                        }

                    if half_open_count < self.DEFAULT_HALF_OPEN_LIMIT:
                        # 半开状态：允许有限请求通过
                        state["half_open_count"] = half_open_count + 1
                        if half_open_count == 0:
                            state["half_open_start"] = now
                        self._circuit_state[pipeline_type] = state
                        return {
                            "status": "ok",
                            "reason": f"熔断冷却结束，半开状态 ({half_open_count + 1}/{self.DEFAULT_HALF_OPEN_LIMIT})",
                            "data": {
                                "is_open": False,
                                "state": "half_open",
                                "half_open_count": half_open_count + 1,
                                "half_open_limit": self.DEFAULT_HALF_OPEN_LIMIT,
                            },
                            "warnings": [],
                        }

                # 逐步恢复完成或不需要逐步恢复，自动关闭
                self._circuit_state[pipeline_type] = {
                    "state": "closed",
                    "cooldown_end": 0,
                    "failure_count": 0,
                    "half_open_count": 0,
                    "recovered_at": now,
                }
                logger.info("流水线熔断自动恢复: type=%s", pipeline_type)
                self._log_event("circuit_auto_recovered", pipeline_type, {})
                return {
                    "status": "ok",
                    "reason": "熔断冷却结束，已自动恢复",
                    "data": {
                        "is_open": False,
                        "state": "closed",
                    },
                    "warnings": [],
                }

            # 仍在冷却中
            remaining = cooldown_end - now
            return {
                "status": "ok",
                "reason": f"熔断打开中，剩余冷却 {remaining:.1f} 秒",
                "data": {
                    "is_open": True,
                    "state": "open",
                    "failure_count": state.get("failure_count", 0),
                    "remaining_cooldown_seconds": round(remaining, 1),
                    "cooldown_end": cooldown_end,
                },
                "warnings": ["circuit_open"],
            }

    def get_status(self, pipeline_type: str) -> Dict[str, Any]:
        """
        返回指定流水线类型的详细熔断状态

        Args:
            pipeline_type: 流水线类型标识

        Returns:
            标准响应字典
        """
        if not pipeline_type or not isinstance(pipeline_type, str):
            return {
                "status": "error",
                "reason": f"无效的流水线类型: {pipeline_type}",
                "data": {},
                "warnings": ["invalid_pipeline_type"],
            }

        with self._lock:
            failure_count = self._failure_counts.get(pipeline_type, 0)
            state = self._circuit_state.get(pipeline_type, {
                "state": "closed",
                "cooldown_end": 0,
                "failure_count": 0,
                "half_open_count": 0,
            })

            return {
                "status": "ok",
                "reason": f"流水线 {pipeline_type} 熔断状态: {state.get('state', 'unknown')}",
                "data": {
                    "pipeline_type": pipeline_type,
                    "state": state.get("state", "unknown"),
                    "failure_count": failure_count,
                    "threshold": self.DEFAULT_FAILURE_THRESHOLD,
                    "cooldown_end": state.get("cooldown_end", 0),
                    "half_open_count": state.get("half_open_count", 0),
                    "half_open_limit": self.DEFAULT_HALF_OPEN_LIMIT,
                    "triggered_at": state.get("triggered_at"),
                    "recovered_at": state.get("recovered_at"),
                },
                "warnings": ["circuit_open"] if state.get("state") == "open" else [],
            }

    def get_all_status(self) -> Dict[str, Any]:
        """
        返回所有流水线类型的熔断状态汇总

        Returns:
            标准响应字典
        """
        all_types = set(list(self._failure_counts.keys()) + list(self._circuit_state.keys()))
        all_status = {}
        open_count = 0
        half_open_count = 0

        for ptype in sorted(all_types):
            res = self.get_status(ptype)
            if res["status"] == "ok":
                all_status[ptype] = res["data"]
                state = res["data"].get("state")
                if state == "open":
                    open_count += 1
                elif state == "half_open":
                    half_open_count += 1

        overall = "healthy" if open_count == 0 else "degraded"
        reason = "所有流水线正常" if open_count == 0 else f"{open_count} 条流水线处于熔断状态"

        return {
            "status": "ok",
            "reason": reason,
            "data": {
                "overall": overall,
                "total_types": len(all_status),
                "open_count": open_count,
                "half_open_count": half_open_count,
                "pipelines": all_status,
            },
            "warnings": [f"{open_count} pipelines in circuit open"] if open_count > 0 else [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检

        Returns:
            标准健康检查响应字典
        """
        try:
            with self._lock:
                total_failure_counts = sum(self._failure_counts.values())
                total_circuit_states = len(self._circuit_state)
                open_states = sum(
                    1 for s in self._circuit_state.values() if s.get("state") == "open"
                )

            # 缺陷五修复：对求和结果设置合理性校验
            warnings = []
            if total_failure_counts > self.DEFAULT_MAX_RESET_COUNT * max(len(self._failure_counts), 1):
                warnings.append("failure_counts 异常增长，可能存在废弃流水线类型未清理")
                total_failure_counts_display = f">{self.DEFAULT_MAX_RESET_COUNT * len(self._failure_counts)}"
            else:
                total_failure_counts_display = str(total_failure_counts)

            if open_states > 0:
                warnings.append(f"{open_states} pipelines in circuit open")

            return {
                "status": "ok",
                "reason": f"PipelineCircuitBreaker 正常，监控 {len(self._failure_counts)} 条流水线类型",
                "data": {
                    "monitored_types": len(self._failure_counts),
                    "total_failure_counts": total_failure_counts_display,
                    "total_circuit_states": total_circuit_states,
                    "open_states": open_states,
                    "dependencies": {
                        "behavioral_logger": self._behavioral_logger is not None,
                    },
                    "config": {
                        "failure_threshold": self.DEFAULT_FAILURE_THRESHOLD,
                        "cooldown_seconds": self.DEFAULT_COOLDOWN_SECONDS,
                        "half_open_limit": self.DEFAULT_HALF_OPEN_LIMIT,
                        "gradual_reopen": self.DEFAULT_GRADUAL_REOPEN,
                    },
                },
                "warnings": warnings,
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查内部状态字典与线程锁")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _log_event(self, event_type: str, pipeline_type: str, details: Dict[str, Any]) -> None:
        """
        记录熔断事件到行为日志（降级安全）

        Args:
            event_type: 事件类型 (circuit_open, circuit_closed, circuit_auto_recovered)
            pipeline_type: 流水线类型标识
            details: 事件详情
        """
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type=f"pipeline_circuit_breaker.{event_type}",
                    details={
                        "pipeline_type": pipeline_type,
                        **details,
                    },
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")
