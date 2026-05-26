"""
火种系统 · 冲突仲裁器 (ConflictArbiter)

核心职责：
1. 接收多个模块的标准化约束响应（NeuroConstraint），基于优先级规则进行冲突裁决，输出唯一确定的最终约束
2. 支持生存级指令无条件覆盖、风控优先于策略、紧缩利润联动加仓限制、执行能力反馈等分层仲裁逻辑

外部依赖（真实模块接口）：
- core.negotiation_bus.neuro_pulse.NeuroPulse : 标准化语义向量，携带意图类型、紧急性、期望仓位等信息
- core.negotiation_bus.neuro_pulse.NeuroConstraint : 标准化约束响应，携带是否允许、允许上限、建议执行方式等
- core.behavioral_logger.BehavioralLogger : 记录仲裁过程与冲突事件日志

接口契约：
- arbitrate(pulse: NeuroPulse, constraints: List[NeuroConstraint]) -> Dict[str, Any] : 对单个脉冲的多个约束响应进行仲裁，返回最终决策
- batch_arbitrate(pulses_and_constraints: List[Tuple[NeuroPulse, List[NeuroConstraint]]]) -> Dict[str, Any] : 批量仲裁
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当传入的 constraints 列表为空时，使用保守默认约束（不允许开仓、仅允许平仓）
- 当 NeuroPulse 或 NeuroConstraint 类型校验失败时，自动修正为安全默认值并记录告警
- 仲裁过程中任何异常均返回保守结果，确保系统安全

资源管理：
- 本模块为纯计算逻辑，不持有任何外部资源句柄
- 所有中间变量在方法返回后自动回收
"""

import logging
import threading
from typing import Dict, Any, List, Tuple, Optional
from enum import IntEnum

logger = logging.getLogger(__name__)


class ArbitrationPriority(IntEnum):
    """仲裁优先级枚举，数值越大优先级越高"""
    STRATEGY = 0       # 策略引擎期望
    EVOLUTION = 1      # 进化工厂状态
    EXECUTION = 2      # 执行网关能力反馈
    PROFIT_COMPRESSION = 3  # 紧缩利润模块联动
    RISK = 4            # 风控硬约束
    SURVIVAL = 5        # 生存级指令（C++硬实时风控）


class ConflictArbiter:
    """协商总线冲突仲裁器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_CONSERVATIVE_SIZE_PCT = 0.0     # 默认保守仓位（不允许新开仓），无量纲，[0.0, 1.0]
    DEFAULT_CONSERVATIVE_METHOD = "limit_order"  # 默认保守执行方式
    DEFAULT_COMPRESSION_LARGE_PROFIT = 0.7  # 大盈阶段仓位缩减系数，无量纲，[0.5, 0.8]
    DEFAULT_COMPRESSION_EXTREME = 0.5       # 极端紧缩阶段仓位缩减系数，无量纲，[0.3, 0.6]
    DEFAULT_BALANCE_COEFFICIENT = 0.7       # 策略与风控平衡系数，无量纲，[0.5, 0.9]
    ARBITRATION_LATENCY_WARN_US = 100       # 仲裁耗时告警阈值，微秒，[50, 500]

    # 各优先级对应的模块标识（用于日志）
    PRIORITY_MODULE_MAP = {
        ArbitrationPriority.STRATEGY: "策略引擎",
        ArbitrationPriority.EVOLUTION: "进化工厂",
        ArbitrationPriority.EXECUTION: "执行网关",
        ArbitrationPriority.PROFIT_COMPRESSION: "紧缩利润模块",
        ArbitrationPriority.RISK: "风控中枢",
        ArbitrationPriority.SURVIVAL: "C++硬实时风控",
    }

    def __init__(self):
        # 统计信息（线程安全）
        self._stats_lock = threading.Lock()
        self._arbitration_count = 0
        self._conflict_count = 0
        self._survival_override_count = 0
        self._risk_override_count = 0
        self._profit_override_count = 0
        self._execution_override_count = 0

        # 性能统计
        self._latency_samples: deque = deque(maxlen=200)

        # 外部依赖注入
        self._behavioral_logger = None

        logger.info(
            "ConflictArbiter 初始化完成，优先级链: SURVIVAL(%d) > RISK(%d) > PROFIT(%d) > EXECUTION(%d) > EVOLUTION(%d) > STRATEGY(%d)",
            ArbitrationPriority.SURVIVAL.value,
            ArbitrationPriority.RISK.value,
            ArbitrationPriority.PROFIT_COMPRESSION.value,
            ArbitrationPriority.EXECUTION.value,
            ArbitrationPriority.EVOLUTION.value,
            ArbitrationPriority.STRATEGY.value,
        )

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）
        """
        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，仲裁日志降级为标准 logger")

    # ========== 公共接口 ==========
    def arbitrate(
        self,
        pulse: Any,  # NeuroPulse 实例，使用 Any 以避免循环导入
        constraints: List[Any],  # List[NeuroConstraint]
    ) -> Dict[str, Any]:
        """
        对单个脉冲的多个约束响应进行仲裁，输出最终决策

        Args:
            pulse: 标准化语义向量 (NeuroPulse)，包含 intent_type, urgency, desired_size_pct 等字段
            constraints: 各模块返回的约束响应列表 (List[NeuroConstraint])

        Returns:
            标准响应字典，data 中包含 final_allowed, final_size_pct, preferred_method, reason 等字段
        """
        import time
        start_time = time.perf_counter()
        warnings = []

        # 参数校验
        pulse_id = getattr(pulse, 'pulse_id', 'unknown') if pulse is not None else 'unknown'
        intent_type = getattr(pulse, 'intent_type', 'unknown') if pulse is not None else 'unknown'

        if pulse is None:
            logger.error("pulse 为 None #RECOVERY: 检查协商总线信号生成逻辑")
            return self._conservative_result("pulse 为空，采用保守默认值", warnings, pulse_id="unknown")

        if not constraints:
            logger.warning(f"constraints 列表为空，pulse_id={pulse_id}")
            return self._conservative_result("无约束响应，采用保守默认值", warnings, pulse_id=pulse_id, intent_type=intent_type)

        try:
            # 获取脉冲基本信息
            desired_size = getattr(pulse, 'desired_size_pct', 0.0)

            # 1. 按优先级从高到低排序约束（内部自动过滤无效元素）
            sorted_constraints = self._sort_by_priority(constraints)
            if not sorted_constraints:
                logger.warning(f"有效约束为空，pulse_id={pulse_id}，采用保守默认值")
                return self._conservative_result(
                    "有效约束列表为空，无法进行仲裁", warnings, pulse_id=pulse_id, intent_type=intent_type
                )

            # 2. 逐层应用约束
            final_allowed = True
            final_size = desired_size
            final_method = "market_order"  # 默认市价单
            final_reason = "初始状态"
            adjustment_log = []

            for constraint in sorted_constraints:
                priority = self._get_constraint_priority(constraint)
                module_name = self.PRIORITY_MODULE_MAP.get(priority, "未知模块")

                # 生存级约束：无条件覆盖
                if priority == ArbitrationPriority.SURVIVAL:
                    with self._stats_lock:
                        self._survival_override_count += 1
                    if not constraint.allowed:
                        final_allowed = False
                        final_size = 0.0
                        final_reason = f"[SURVIVAL] {module_name} 触发生存级否决: {constraint.adjustment_reason}"
                        adjustment_log.append(final_reason)
                        logger.warning(f"生存级否决: pulse_id={pulse_id}, {final_reason}")
                        break

                    if constraint.allowed_size_pct is not None:
                        final_size = min(final_size, constraint.allowed_size_pct)
                        final_reason = f"[SURVIVAL] {module_name} 允许，上限={final_size:.4%}"
                        adjustment_log.append(final_reason)
                    continue

                # 风控约束：不可被策略覆盖
                if priority == ArbitrationPriority.RISK:
                    with self._stats_lock:
                        self._risk_override_count += 1
                    if not constraint.allowed:
                        final_allowed = False
                        final_size = 0.0
                        final_reason = f"[RISK] {module_name} 否决: {constraint.adjustment_reason}"
                        adjustment_log.append(final_reason)
                        logger.info(f"风控否决: pulse_id={pulse_id}, {constraint.adjustment_reason}")
                        break

                    if constraint.allowed_size_pct is not None:
                        final_size = min(final_size, constraint.allowed_size_pct)
                        final_reason = f"[RISK] {module_name} 限制仓位上限={final_size:.4%}"
                        adjustment_log.append(final_reason)
                    if constraint.preferred_method:
                        final_method = constraint.preferred_method
                    continue

                # 紧缩利润联动（加仓场景）
                if priority == ArbitrationPriority.PROFIT_COMPRESSION:
                    with self._stats_lock:
                        self._profit_override_count += 1
                    if intent_type in ("add_position", "open_long", "open_short"):
                        compression_stage = getattr(constraint, 'compression_stage', None)
                        if compression_stage == "large_profit":
                            final_size *= self.DEFAULT_COMPRESSION_LARGE_PROFIT
                            final_reason = f"[PROFIT] 大盈阶段，加仓仓位缩减至 {final_size:.4%}"
                            adjustment_log.append(final_reason)
                            logger.debug(f"紧缩利润联动: pulse_id={pulse_id}, 大盈阶段缩减")
                            warnings.append("suggest_extended_stop_buffer")
                        elif compression_stage == "extreme":
                            final_size *= self.DEFAULT_COMPRESSION_EXTREME
                            final_reason = f"[PROFIT] 极端紧缩阶段，仓位缩减至 {final_size:.4%}"
                            adjustment_log.append(final_reason)
                            logger.debug(f"紧缩利润联动: pulse_id={pulse_id}, 极端紧缩阶段缩减")
                            warnings.append("suggest_extended_stop_buffer")
                    continue

                # 执行能力反馈
                if priority == ArbitrationPriority.EXECUTION:
                    with self._stats_lock:
                        self._execution_override_count += 1
                    if not constraint.allowed:
                        if constraint.adjustment_reason:
                            warnings.append(f"execution_suggestion: {constraint.adjustment_reason}")
                    if constraint.preferred_method:
                        final_method = constraint.preferred_method
                    if constraint.allowed_size_pct is not None:
                        final_size = min(final_size, constraint.allowed_size_pct)
                    continue

                # 进化状态检查（可选标记）
                if priority == ArbitrationPriority.EVOLUTION:
                    if getattr(constraint, 'epigenetic_override', False):
                        warnings.append("epigenetic_override_active")
                        logger.debug(f"表观遗传覆盖: pulse_id={pulse_id}")
                    continue

                # 策略引擎期望（最低优先级，仅提供初始值）
                if priority == ArbitrationPriority.STRATEGY:
                    if final_reason == "初始状态":
                        final_reason = f"[STRATEGY] 策略期望仓位={final_size:.4%}"
                        adjustment_log.append(final_reason)
                    continue

            # 3. 组装最终结果
            result_data = {
                "pulse_id": pulse_id,
                "intent_type": intent_type,
                "final_allowed": final_allowed,
                "final_size_pct": round(final_size, 6),
                "preferred_method": final_method,
                "arbitration_reason": final_reason,
                "adjustment_log": adjustment_log,
                "survival_override": self._survival_override_count > 0,
            }

            # 性能统计
            elapsed_us = (time.perf_counter() - start_time) * 1_000_000
            self._latency_samples.append(elapsed_us)
            if elapsed_us > self.ARBITRATION_LATENCY_WARN_US:
                logger.warning(
                    f"仲裁耗时超标: {elapsed_us:.1f}μs > {self.ARBITRATION_LATENCY_WARN_US}μs, pulse_id={pulse_id}"
                )

            logger.debug(
                f"仲裁完成: pulse_id={pulse_id}, allowed={final_allowed}, "
                f"size={final_size:.4%}, method={final_method}, adjustments={len(adjustment_log)}"
            )

            # 行为日志
            self._log_arbitration_event(pulse_id, result_data, warnings)

            with self._stats_lock:
                self._arbitration_count += 1

            return {
                "status": "ok",
                "reason": final_reason,
                "data": result_data,
                "warnings": warnings,
            }

        except Exception as e:
            logger.error(f"仲裁异常: {e} #RECOVERY: 检查 NeuroPulse/NeuroConstraint 结构完整性，已回退保守默认值")
            return self._conservative_result(
                f"仲裁异常: {str(e)}", warnings, pulse_id=pulse_id, intent_type=intent_type
            )

    def batch_arbitrate(
        self,
        pulses_and_constraints: List[Tuple[Any, List[Any]]],
    ) -> Dict[str, Any]:
        """
        批量仲裁多个脉冲

        Args:
            pulses_and_constraints: (脉冲, 约束列表) 的元组列表

        Returns:
            标准响应字典，data 中包含每个脉冲的仲裁结果列表
        """
        if not pulses_and_constraints:
            return {
                "status": "ok",
                "reason": "批量仲裁列表为空",
                "data": {"results": []},
                "warnings": [],
            }

        results = []
        all_warnings = []
        for pulse, constraints in pulses_and_constraints:
            pulse_id = getattr(pulse, 'pulse_id', 'unknown')
            try:
                result = self.arbitrate(pulse, constraints)
                if result["status"] == "ok":
                    result["data"]["batch_pulse_id"] = pulse_id
                    results.append(result["data"])
                else:
                    results.append({
                        "error": result["reason"],
                        "pulse_id": pulse_id,
                        "status": "failed",
                    })
            except Exception as e:
                results.append({
                    "error": str(e),
                    "pulse_id": pulse_id,
                    "status": "exception",
                })
                logger.error(f"批量仲裁单条异常: pulse_id={pulse_id}, {e}")
            all_warnings.extend(result.get("warnings", []))

        return {
            "status": "ok",
            "reason": f"批量仲裁完成，共 {len(results)} 条",
            "data": {"results": results},
            "warnings": all_warnings,
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检

        Returns:
            标准健康检查响应字典
        """
        try:
            expected_order = [
                ArbitrationPriority.SURVIVAL,
                ArbitrationPriority.RISK,
                ArbitrationPriority.PROFIT_COMPRESSION,
                ArbitrationPriority.EXECUTION,
                ArbitrationPriority.EVOLUTION,
                ArbitrationPriority.STRATEGY,
            ]
            is_chain_valid = expected_order == sorted(expected_order, reverse=True)

            # 获取性能统计
            latency_samples = list(self._latency_samples)
            p50 = sorted(latency_samples)[len(latency_samples) // 2] if latency_samples else 0
            p95 = sorted(latency_samples)[int(len(latency_samples) * 0.95)] if len(latency_samples) >= 20 else 0

            return {
                "status": "ok" if is_chain_valid else "degraded",
                "reason": (
                    f"ConflictArbiter 正常，仲裁次数: {self._arbitration_count}, "
                    f"生存级覆盖: {self._survival_override_count}, 风控覆盖: {self._risk_override_count}, "
                    f"紧缩覆盖: {self._profit_override_count}, 执行覆盖: {self._execution_override_count}"
                ),
                "data": {
                    "arbitration_count": self._arbitration_count,
                    "conflict_count": self._conflict_count,
                    "survival_override_count": self._survival_override_count,
                    "risk_override_count": self._risk_override_count,
                    "profit_override_count": self._profit_override_count,
                    "execution_override_count": self._execution_override_count,
                    "latency_p50_us": round(p50, 1),
                    "latency_p95_us": round(p95, 1),
                    "priority_chain_valid": is_chain_valid,
                    "dependencies": {
                        "behavioral_logger": self._behavioral_logger is not None,
                    },
                },
                "warnings": [] if is_chain_valid else ["priority_chain_misordered"],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查优先级枚举定义和统计计数器")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _sort_by_priority(self, constraints: List[Any]) -> List[Any]:
        """按优先级从高到低排序约束响应列表，自动过滤无效元素"""
        valid_constraints = [c for c in constraints if c is not None]
        if not valid_constraints:
            return []
        if len(valid_constraints) < len(constraints):
            logger.warning(
                f"constraints 列表中存在 {len(constraints) - len(valid_constraints)} 个无效元素，已过滤"
            )
        return sorted(
            valid_constraints,
            key=lambda c: self._get_constraint_priority(c),
            reverse=True,
        )

    @staticmethod
    def _get_constraint_priority(constraint: Any) -> ArbitrationPriority:
        """
        从约束对象中提取优先级，附带鸭子类型保护

        优先级映射规则：
        - 携带 survival_override 标记 → SURVIVAL
        - 携带显式 priority 字段 → 对应优先级
        - 通过 module_source 字段按关键词匹配
        - 默认 → STRATEGY
        """
        if not hasattr(constraint, 'allowed'):
            return ArbitrationPriority.STRATEGY

        if getattr(constraint, 'survival_override', False):
            return ArbitrationPriority.SURVIVAL

        # 优先通过显式的 priority 字段获取
        explicit_priority = getattr(constraint, 'priority', None)
        if explicit_priority is not None and isinstance(explicit_priority, int):
            for p in ArbitrationPriority:
                if p.value == explicit_priority:
                    return p

        module = getattr(constraint, 'module_source', '')
        if not module:
            logger.warning("约束对象缺少 module_source 字段，默认使用 STRATEGY 优先级")
            return ArbitrationPriority.STRATEGY

        module_lower = module.lower()
        if 'risk' in module_lower or 'circuit_breaker' in module_lower:
            return ArbitrationPriority.RISK
        if 'profit' in module_lower or 'compression' in module_lower:
            return ArbitrationPriority.PROFIT_COMPRESSION
        if 'execution' in module_lower or 'gateway' in module_lower:
            return ArbitrationPriority.EXECUTION
        if 'evolution' in module_lower or 'gene' in module_lower:
            return ArbitrationPriority.EVOLUTION

        return ArbitrationPriority.STRATEGY

    def _conservative_result(
        self,
        reason: str,
        warnings: List[str],
        pulse_id: str = "unknown",
        intent_type: str = "unknown",
    ) -> Dict[str, Any]:
        """生成保守默认结果（不允许开仓），附带脉冲标识以支持全链路追踪"""
        return {
            "status": "ok",
            "reason": reason,
            "data": {
                "pulse_id": pulse_id,
                "intent_type": intent_type,
                "final_allowed": False,
                "final_size_pct": self.DEFAULT_CONSERVATIVE_SIZE_PCT,
                "preferred_method": self.DEFAULT_CONSERVATIVE_METHOD,
                "arbitration_reason": reason,
                "adjustment_log": [reason],
                "survival_override": False,
            },
            "warnings": warnings,
        }

    def _log_arbitration_event(
        self,
        pulse_id: str,
        result_data: Dict[str, Any],
        warnings: List[str],
    ) -> None:
        """记录仲裁事件到行为日志"""
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="conflict_arbitration",
                    details={
                        "pulse_id": pulse_id,
                        "final_allowed": result_data.get("final_allowed"),
                        "final_size_pct": result_data.get("final_size_pct"),
                        "adjustment_log": result_data.get("adjustment_log"),
                        "warnings": warnings,
                    },
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")
