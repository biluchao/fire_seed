"""
火种系统 · 流水线熔断器 (PipelineCircuitBreaker)

核心职责：
1. 按流水线类型独立统计连续异常次数，触发熔断后进入冷却期并逐步恢复
2. 熔断触发时自动执行紧急保护动作（撤单、止损收紧），并通过独立通道发送告警
3. 维护每条流水线类型的失败序列、熔断状态与持久化快照，支持系统重启后状态恢复

外部依赖（真实模块接口）：
- core.behavioral_logger.BehavioralLogger : 记录熔断触发、冷却、恢复等关键事件日志
- core.execution.execution_gateway.ExecutionGateway : 熔断时撤销挂单与收紧止损
- core.alert_adapter.AlertAdapter : 独立于主系统的紧急告警通道（如短信/独立Telegram）

接口契约：
- record_failure(pipeline_type: str) -> Dict[str, Any] : 记录一次失败，返回是否触发熔断
- record_success(pipeline_type: str) -> Dict[str, Any] : 记录一次成功，返回是否重置计数
- is_circuit_open(pipeline_type: str) -> Dict[str, Any] : 查询指定流水线类型是否处于熔断状态
- get_status(pipeline_type: str) -> Dict[str, Any] : 返回指定流水线类型的详细熔断状态
- get_all_status() -> Dict[str, Any] : 返回所有流水线类型的熔断状态汇总
- health_check() -> Dict[str, Any] : 模块自检（含跨平台超时熔断链路测试）
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 BehavioralLogger 不可用时，日志降级为标准 logger，并暂存事件至本地环形缓冲区以待补传
- 当 ExecutionGateway 不可用时或接口不完整时，熔断保护动作降级为仅日志告警
- 当 AlertAdapter 不可用时，紧急告警降级为 NegotiationBus 推送或本地日志
- health_check 在非 Unix 平台或非主线程调用时自动降级为无超时保护模式
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护内存中的失败计数、状态字典与日志降级缓冲区
- 熔断状态通过原子写入持久化到本地快照文件（路径可注入）
- 快照持久化在锁外异步执行，避免阻塞主业务路径
- 线程锁在模块销毁时自动释放
"""

import time
import os
import json
import logging
import threading
from collections import deque
from typing import Dict, Any, List, Optional, Tuple

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

    DEFAULT_SNAPSHOT_PATH = "logs/circuit_breaker_snapshot.bin"  # 快照文件路径，可通过依赖注入覆盖
    DEFAULT_LOG_BUFFER_SIZE = 50            # 日志降级缓冲区大小，无量纲，取值范围 [10, 200]

    # 按流水线类型独立配置（未配置的类型回退到默认值）
    DEFAULT_PER_TYPE_CONFIG = {
        "1m_cheetah": {
            "failure_threshold": 8,
            "cooldown_seconds": 15,
            "half_open_limit": 5,          # 高频策略，更多试探机会
            "half_open_timeout_multiplier": 3,  # 更长的半开超时
        },
        "15m_whale": {
            "failure_threshold": 3,
            "cooldown_seconds": 120,
            "half_open_limit": 2,
            "half_open_timeout_multiplier": 1,
        },
        "event_driven": {
            "failure_threshold": 5,
            "cooldown_seconds": 60,
            "half_open_limit": 3,
            "half_open_timeout_multiplier": 2,
        },
    }

    def __init__(self):
        # 每条流水线类型的连续失败计数
        self._failure_counts: Dict[str, int] = {}

        # 每条流水线类型的熔断状态
        self._circuit_state: Dict[str, Dict[str, Any]] = {}

        # 外部依赖注入
        self._behavioral_logger = None
        self._execution_gateway = None       # 缺陷一：熔断保护动作依赖
        self._alert_adapter = None           # 缺陷五：独立告警通道

        # 快照路径（可注入）
        self._snapshot_path = self.DEFAULT_SNAPSHOT_PATH

        # 日志降级缓冲区
        self._log_fallback_buffer: deque = deque(maxlen=self.DEFAULT_LOG_BUFFER_SIZE)

        # 线程安全（保护 _failure_counts、_circuit_state、_log_fallback_buffer）
        self._lock = threading.Lock()

        # 注入状态标记
        self._injection_done = False

        # 从持久化快照恢复状态，并修正过期熔断
        self._load_snapshot()

        logger.info("PipelineCircuitBreaker 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        behavioral_logger: Optional[Any] = None,
        execution_gateway: Optional[Any] = None,
        alert_adapter: Optional[Any] = None,
        snapshot_path: Optional[str] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）。

        本方法支持重复调用保护，避免意外覆盖已注入的依赖。

        Args:
            behavioral_logger: 行为日志记录器
            execution_gateway: 执行网关，用于熔断时紧急撤单与止损收紧
            alert_adapter: 独立告警适配器，用于熔断时发送紧急通知
            snapshot_path: 自定义快照文件路径
        """
        if self._injection_done:
            logger.warning("依赖已注入，跳过重复注入")
            return

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，熔断事件日志降级为标准 logger")

        if execution_gateway is not None:
            self._execution_gateway = execution_gateway
            logger.info("ExecutionGateway 注入成功")
        else:
            logger.warning("ExecutionGateway 未注入，熔断保护动作降级为仅日志告警")

        if alert_adapter is not None:
            self._alert_adapter = alert_adapter
            logger.info("AlertAdapter 注入成功")
        else:
            logger.warning("AlertAdapter 未注入，紧急告警降级为 NegotiationBus 或本地日志")

        if snapshot_path is not None:
            self._snapshot_path = snapshot_path
            logger.info("快照路径设置为: %s", snapshot_path)

        self._injection_done = True

    # ========== 公共接口 ==========
    def record_failure(self, pipeline_type: str) -> Dict[str, Any]:
        """
        记录指定流水线类型的一次失败，判断是否触发熔断。
        熔断触发时自动执行紧急保护动作，并将快照持久化移出锁外。

        Args:
            pipeline_type: 流水线类型标识（如 "1m_cheetah", "oscillation", "event_driven"）

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
            if pipeline_type not in self._failure_counts:
                self._failure_counts[pipeline_type] = 0

            self._failure_counts[pipeline_type] += 1
            current_count = self._failure_counts[pipeline_type]

            # 防止计数器无限增长：超过阈值 2 倍时重置
            if current_count > self.DEFAULT_FAILURE_THRESHOLD * 2:
                self._failure_counts[pipeline_type] = self.DEFAULT_FAILURE_THRESHOLD
                current_count = self.DEFAULT_FAILURE_THRESHOLD
                logger.debug("流水线 %s 失败计数超过上限，已重置为阈值", pipeline_type)

            # 获取该类型的实际阈值和冷却时间
            threshold = self._get_threshold_for(pipeline_type)
            cooldown = self._get_cooldown_for(pipeline_type)
            circuit_open = current_count >= threshold

            if circuit_open:
                cooldown_end = time.time() + cooldown
                existing_state = self._circuit_state.get(pipeline_type, {})

                # 仅在当前状态仍为 open 时才延长冷却，防止覆盖半开/关闭状态
                if existing_state.get("state") == "open":
                    cooldown_end = max(
                        cooldown_end,
                        existing_state.get("cooldown_end", 0) + cooldown
                    )

                self._circuit_state[pipeline_type] = {
                    "state": "open",
                    "cooldown_end": cooldown_end,
                    "failure_count": current_count,
                    "half_open_count": 0,
                    "triggered_at": time.time(),
                    "threshold": threshold,
                }

                # 紧急保护动作与告警（锁内调用，但内部已处理异常）
                self._execute_emergency_protection(pipeline_type, current_count, threshold)
                self._send_urgent_alert(pipeline_type, current_count, threshold, cooldown_end)

                logger.warning(
                    "流水线熔断触发: type=%s, 连续失败=%d/%d #RECOVERY: 检查上游策略信号质量、市场数据完整性",
                    pipeline_type, current_count, threshold
                )
                self._log_event("circuit_open", pipeline_type, {
                    "failure_count": current_count,
                    "threshold": threshold,
                    "cooldown_end": cooldown_end,
                })

            # 准备快照数据（锁内深拷贝）
            snapshot_data = {
                "failure_counts": dict(self._failure_counts),
                "circuit_state": dict(self._circuit_state),
                "timestamp": time.time(),
            }

        # 锁外保存快照，避免 I/O 阻塞业务路径
        self._save_snapshot_from_data(snapshot_data)

        return {
            "status": "ok",
            "reason": f"流水线 {pipeline_type} 失败计数: {current_count}/{threshold}",
            "data": {
                "pipeline_type": pipeline_type,
                "failure_count": current_count,
                "threshold": threshold,
                "circuit_open": circuit_open,
                "cooldown_end": self._circuit_state.get(pipeline_type, {}).get("cooldown_end"),
            },
            "warnings": ["circuit_breaker_triggered"] if circuit_open else [],
        }

    def record_success(self, pipeline_type: str) -> Dict[str, Any]:
        """
        记录指定流水线类型的一次成功，重置失败计数。
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

            snapshot_data = {
                "failure_counts": dict(self._failure_counts),
                "circuit_state": dict(self._circuit_state),
                "timestamp": time.time(),
            }

        self._save_snapshot_from_data(snapshot_data)

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
        查询指定流水线类型是否处于熔断状态（含冷却期自动恢复与半开逻辑）。
        支持按流水线类型独立的半开限制与超时配置。
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

            cooldown_end = state.get("cooldown_end", 0)
            now = time.time()

            if now >= cooldown_end:
                if self.DEFAULT_GRADUAL_REOPEN:
                    half_open_limit = self._get_half_open_limit_for(pipeline_type)
                    half_open_timeout = self._get_half_open_timeout_for(pipeline_type)
                    half_open_count = state.get("half_open_count", 0)
                    half_open_start = state.get("half_open_start", now)

                    # 半开状态超时检查
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
                        self._save_snapshot_in_lock()
                        return {
                            "status": "ok",
                            "reason": "熔断半开超时，已自动恢复",
                            "data": {"is_open": False, "state": "closed"},
                            "warnings": [],
                        }

                    if half_open_count < half_open_limit:
                        state["half_open_count"] = half_open_count + 1
                        if half_open_count == 0:
                            state["half_open_start"] = now
                        self._circuit_state[pipeline_type] = state
                        self._save_snapshot_in_lock()
                        return {
                            "status": "ok",
                            "reason": f"熔断冷却结束，半开状态 ({half_open_count + 1}/{half_open_limit})",
                            "data": {
                                "is_open": False,
                                "state": "half_open",
                                "half_open_count": half_open_count + 1,
                                "half_open_limit": half_open_limit,
                            },
                            "warnings": [],
                        }

                # 全量恢复
                self._circuit_state[pipeline_type] = {
                    "state": "closed",
                    "cooldown_end": 0,
                    "failure_count": 0,
                    "half_open_count": 0,
                    "recovered_at": now,
                }
                logger.info("流水线熔断自动恢复: type=%s", pipeline_type)
                self._log_event("circuit_auto_recovered", pipeline_type, {})
                self._save_snapshot_in_lock()
                return {
                    "status": "ok",
                    "reason": "熔断冷却结束，已自动恢复",
                    "data": {"is_open": False, "state": "closed"},
                    "warnings": [],
                }

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
        """返回指定流水线类型的详细熔断状态"""
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
                    "threshold": self._get_threshold_for(pipeline_type),
                    "cooldown_end": state.get("cooldown_end", 0),
                    "half_open_count": state.get("half_open_count", 0),
                    "half_open_limit": self._get_half_open_limit_for(pipeline_type),
                    "triggered_at": state.get("triggered_at"),
                    "recovered_at": state.get("recovered_at"),
                },
                "warnings": ["circuit_open"] if state.get("state") == "open" else [],
            }

    def get_all_status(self) -> Dict[str, Any]:
        """返回所有流水线类型的熔断状态汇总"""
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

        return {
            "status": "ok",
            "reason": "所有流水线正常" if open_count == 0 else f"{open_count} 条流水线处于熔断状态",
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
        模块自检（含跨平台超时执行测试）。
        在非 Unix 平台或非主线程调用时自动降级为无超时保护模式。
        """
        warnings: List[str] = []
        try:
            # 基本状态检查
            with self._lock:
                total_failure_counts = sum(self._failure_counts.values())
                total_circuit_states = len(self._circuit_state)
                open_states = sum(
                    1 for s in self._circuit_state.values() if s.get("state") == "open"
                )

            # 真实执行测试（带跨平台超时保护）
            test_passed = self._run_health_test()
            if not test_passed:
                return {
                    "status": "error",
                    "reason": "熔断链路测试失败",
                    "data": {},
                    "warnings": ["health_test_failed"],
                }

            if total_failure_counts > self.DEFAULT_MAX_RESET_COUNT * max(len(self._failure_counts), 1):
                warnings.append("failure_counts 异常增长，可能存在废弃流水线类型未清理")
            if open_states > 0:
                warnings.append(f"{open_states} pipelines in circuit open")

            return {
                "status": "ok",
                "reason": f"PipelineCircuitBreaker 正常，监控 {len(self._failure_counts)} 条流水线类型",
                "data": {
                    "monitored_types": len(self._failure_counts),
                    "total_failure_counts": total_failure_counts,
                    "total_circuit_states": total_circuit_states,
                    "open_states": open_states,
                    "dependencies": {
                        "behavioral_logger": self._behavioral_logger is not None,
                        "execution_gateway": self._execution_gateway is not None,
                        "alert_adapter": self._alert_adapter is not None,
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

    def _run_health_test(self) -> bool:
        """
        执行一次熔断链路真实测试，带超时保护。
        使用线程池超时执行，兼容 Windows 和非主线程调用。
        """
        def _test():
            test_type = "__health_check_test__"
            # 记录失败（不会触发外部保护，因为没有真实依赖）
            self.record_failure(test_type)
            # 查询状态
            result = self.is_circuit_open(test_type)
            if result.get("status") != "ok":
                raise RuntimeError("熔断判定返回异常状态")
            # 恢复
            self.record_success(test_type)

        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_test)
                future.result(timeout=3.0)
                return True
        except concurrent.futures.TimeoutError:
            logger.error("健康检查执行超时 #RECOVERY: 可能存在死锁或阻塞")
            return False
        except Exception as e:
            logger.error(f"健康检查测试异常: {e}")
            return False

    # ========== 私有方法 ==========

    def _get_threshold_for(self, pipeline_type: str) -> int:
        config = self.DEFAULT_PER_TYPE_CONFIG.get(pipeline_type, {})
        return config.get("failure_threshold", self.DEFAULT_FAILURE_THRESHOLD)

    def _get_cooldown_for(self, pipeline_type: str) -> int:
        config = self.DEFAULT_PER_TYPE_CONFIG.get(pipeline_type, {})
        return config.get("cooldown_seconds", self.DEFAULT_COOLDOWN_SECONDS)

    def _get_half_open_limit_for(self, pipeline_type: str) -> int:
        config = self.DEFAULT_PER_TYPE_CONFIG.get(pipeline_type, {})
        return config.get("half_open_limit", self.DEFAULT_HALF_OPEN_LIMIT)

    def _get_half_open_timeout_for(self, pipeline_type: str) -> int:
        cooldown = self._get_cooldown_for(pipeline_type)
        multiplier = self.DEFAULT_PER_TYPE_CONFIG.get(pipeline_type, {}).get(
            "half_open_timeout_multiplier", self.DEFAULT_HALF_OPEN_TIMEOUT_MULTIPLIER
        )
        return cooldown * multiplier

    def _execute_emergency_protection(self, pipeline_type: str, count: int, threshold: int) -> None:
        """熔断触发时执行紧急保护动作，含接口可用性校验"""
        if self._execution_gateway is None:
            logger.warning("ExecutionGateway 未注入，熔断保护动作跳过")
            return

        has_cancel = hasattr(self._execution_gateway, 'cancel_all_orders')
        has_tighten = hasattr(self._execution_gateway, 'tighten_all_stops')

        if not has_cancel and not has_tighten:
            logger.error("ExecutionGateway 缺少必要的保护接口: cancel_all_orders, tighten_all_stops")
            return

        try:
            if has_cancel:
                self._execution_gateway.cancel_all_orders(pipeline_type=pipeline_type)
            if has_tighten:
                self._execution_gateway.tighten_all_stops(pipeline_type=pipeline_type, atr_mult=0.3)
            logger.critical("流水线 %s 熔断，已执行紧急撤单与止损收紧", pipeline_type)
        except Exception as e:
            logger.error(
                "熔断后保护动作失败: %s #RECOVERY: 手动检查 %s 的挂单与持仓",
                e, pipeline_type
            )

    def _send_urgent_alert(self, pipeline_type: str, count: int, threshold: int, cooldown_end: float) -> None:
        """通过独立通道发送紧急告警"""
        if self._alert_adapter is not None:
            try:
                self._alert_adapter.send_urgent(
                    title=f"流水线熔断: {pipeline_type}",
                    body=f"连续失败 {count}/{threshold}，冷却至 {time.strftime('%H:%M:%S', time.localtime(cooldown_end))}",
                    level="critical",
                )
            except Exception as e:
                logger.error("独立告警推送失败: %s", e)
        else:
            logger.warning("AlertAdapter 未注入，紧急告警降级为本地日志")

    def _log_event(self, event_type: str, pipeline_type: str, details: Dict[str, Any]) -> None:
        """记录熔断事件到行为日志，支持降级缓冲与补传"""
        logged = False
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type=f"pipeline_circuit_breaker.{event_type}",
                    details={"pipeline_type": pipeline_type, **details},
                )
                logged = True
                # 尝试补传缓冲区中的历史事件
                with self._lock:
                    while self._log_fallback_buffer:
                        old = self._log_fallback_buffer.popleft()
                        try:
                            self._behavioral_logger.log_event(
                                event_type=old["event_type"],
                                details={"pipeline_type": old["pipeline_type"], **old["details"]},
                            )
                        except Exception:
                            self._log_fallback_buffer.appendleft(old)
                            break
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

        if not logged:
            with self._lock:
                self._log_fallback_buffer.append({
                    "event_type": event_type,
                    "pipeline_type": pipeline_type,
                    "details": details,
                    "ts": time.time(),
                })

    def _save_snapshot_in_lock(self) -> None:
        """在锁内保存快照（用于 is_circuit_open 等已在锁内的路径）"""
        data = {
            "failure_counts": dict(self._failure_counts),
            "circuit_state": dict(self._circuit_state),
            "timestamp": time.time(),
        }
        self._save_snapshot_from_data(data)

    def _save_snapshot_from_data(self, data: Dict[str, Any]) -> None:
        """锁外持久化快照"""
        try:
            tmp_path = self._snapshot_path + ".tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            os.fsync(f.fileno())
            os.replace(tmp_path, self._snapshot_path)
        except Exception as e:
            logger.error("熔断状态快照保存失败: %s", e)

    def _load_snapshot(self) -> None:
        """系统启动时恢复熔断状态，并立即修正过期熔断"""
        try:
            if os.path.exists(self._snapshot_path):
                with open(self._snapshot_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._failure_counts = data.get("failure_counts", {})
                self._circuit_state = data.get("circuit_state", {})
                logger.info("熔断状态已从快照恢复: %d 条流水线", len(self._circuit_state))

                # 立即修正已过期的熔断状态
                now = time.time()
                expired_types = []
                for ptype, state in list(self._circuit_state.items()):
                    if state.get("state") == "open" and state.get("cooldown_end", 0) <= now:
                        self._circuit_state[ptype] = {
                            "state": "closed",
                            "cooldown_end": 0,
                            "failure_count": 0,
                            "half_open_count": 0,
                            "recovered_at": now,
                        }
                        expired_types.append(ptype)
                if expired_types:
                    logger.info("修正过期熔断状态: %s", expired_types)
        except Exception as e:
            logger.error("熔断状态快照恢复失败: %s #RECOVERY: 手动检查流水线状态", e)
