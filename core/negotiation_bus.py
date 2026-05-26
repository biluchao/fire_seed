"""
火种系统 · 协商总线入口 (NegotiationBus)

核心职责：
1. 作为跨模块协商层的统一入口，负责初始化、管理和调度四个子模块（语义向量、冲突仲裁、预协商缓存、超时回退）
2. 对外提供标准化的协商接口，所有模块的意图与约束均通过此入口转换为标准化 NeuroPulse 并获取 NeuroConstraint 响应

外部依赖（真实模块接口）：
- core.negotiation_bus.neuro_pulse.NeuroPulse : 通用语义向量与约束响应的定义与验证
- core.negotiation_bus.conflict_arbiter.ConflictArbiter : 多模块约束冲突的优先级仲裁
- core.negotiation_bus.predictive_cache.PredictiveCache : 预协商缓存管理与命中检测
- core.negotiation_bus.timeout_fallback.TimeoutFallback : 硬超时安全回退与模块降级

接口契约：
- register_module(module_name: str, constraints: Dict) -> Dict[str, Any] : 注册模块约束到协商层
- negotiate(intent: Dict[str, Any]) -> Dict[str, Any] : 发起一次完整协商，返回最终执行方案
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当任一子模块初始化失败时，该子模块功能自动降级为保守默认值（如冲突仲裁失败则返回最保守约束）
- 当 NeuroPulse 不可用时，拒绝所有协商请求并返回错误码
- 所有降级值在子模块的类常量区明确声明

资源管理：
- 本模块不持有任何外部资源句柄
- 子模块均为无状态或自包含状态，生命周期随本模块实例自动管理
- 线程锁用于保护协商流程的串行化，确保并发安全
"""

import logging
import threading
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class NegotiationBus:
    """协商总线入口，负责子模块装配与请求路由"""

    def __init__(self):
        # 子模块实例（延迟初始化）
        self._neuro_pulse = None
        self._conflict_arbiter = None
        self._predictive_cache = None
        self._timeout_fallback = None

        # 子模块可用性标记
        self._submodule_status = {
            "neuro_pulse": False,
            "conflict_arbiter": False,
            "predictive_cache": False,
            "timeout_fallback": False,
        }

        # 线程安全锁：确保协商流程的原子性，防止并发读写缓存和仲裁器状态
        self._lock = threading.Lock()

        # 初始化子模块（带降级）
        self._init_submodules()

        logger.info("NegotiationBus 初始化完成，子模块状态: %s", self._submodule_status)

    # ========== 子模块初始化 ==========
    def _init_submodules(self) -> None:
        """按依赖顺序初始化子模块，任一失败自动降级"""
        # 1. 语义向量（核心依赖，必须可用）
        try:
            from core.negotiation_bus.neuro_pulse import NeuroPulse
            self._neuro_pulse = NeuroPulse()
            self._submodule_status["neuro_pulse"] = True
            logger.info("NeuroPulse 子模块初始化成功")
        except Exception as e:
            logger.error(
                "NeuroPulse 子模块初始化失败，协商层不可用: %s #RECOVERY: 检查 core/negotiation_bus/neuro_pulse.py 是否存在",
                e
            )
            return

        # 2. 冲突仲裁
        try:
            from core.negotiation_bus.conflict_arbiter import ConflictArbiter
            self._conflict_arbiter = ConflictArbiter()
            self._submodule_status["conflict_arbiter"] = True
            logger.info("ConflictArbiter 子模块初始化成功")
        except Exception as e:
            logger.warning("ConflictArbiter 子模块初始化失败，将使用保守默认值: %s", e)

        # 3. 预协商缓存
        try:
            from core.negotiation_bus.predictive_cache import PredictiveCache
            self._predictive_cache = PredictiveCache()
            self._submodule_status["predictive_cache"] = True
            logger.info("PredictiveCache 子模块初始化成功")
        except Exception as e:
            logger.warning("PredictiveCache 子模块初始化失败，将跳过预协商加速: %s", e)

        # 4. 超时回退
        try:
            from core.negotiation_bus.timeout_fallback import TimeoutFallback
            self._timeout_fallback = TimeoutFallback()
            self._submodule_status["timeout_fallback"] = True
            logger.info("TimeoutFallback 子模块初始化成功")
        except Exception as e:
            logger.warning("TimeoutFallback 子模块初始化失败，将使用固定超时值: %s", e)

    # ========== 公共接口 ==========
    def register_module(self, module_name: str, constraints: Dict[str, Any]) -> Dict[str, Any]:
        """
        向协商层注册一个模块的约束条件

        Args:
            module_name: 模块唯一标识名
            constraints: 模块约束字典，必须包含 priority (int), evaluator (callable) 等字段

        Returns:
            标准响应字典
        """
        if not self._neuro_pulse:
            return {
                "status": "error",
                "reason": "协商层核心不可用，拒绝注册",
                "data": {},
                "warnings": ["neuro_pulse_unavailable"],
            }

        if not module_name or not isinstance(module_name, str):
            return {
                "status": "error",
                "reason": "无效的模块名称",
                "data": {},
                "warnings": ["invalid_module_name"],
            }

        with self._lock:
            try:
                # 鸭子类型校验：确保 NeuroPulse 提供约束验证方法
                if not hasattr(self._neuro_pulse, 'validate_constraints'):
                    logger.error("NeuroPulse 缺少 validate_constraints 方法")
                    return {
                        "status": "error",
                        "reason": "核心子模块接口不兼容",
                        "data": {},
                        "warnings": ["interface_mismatch"],
                    }

                validated = self._neuro_pulse.validate_constraints(constraints)
                if not validated.get("valid", False):
                    return {
                        "status": "error",
                        "reason": f"约束格式验证失败: {validated.get('reason', '未知')}",
                        "data": validated,
                        "warnings": ["constraint_validation_failed"],
                    }

                # 注册到冲突仲裁器（若可用且接口兼容）
                if self._conflict_arbiter and hasattr(self._conflict_arbiter, 'register'):
                    self._conflict_arbiter.register(module_name, validated["constraints"])
                    logger.info("模块 %s 已注册到协商层，优先级: %d", module_name, constraints.get("priority", 0))

                return {
                    "status": "ok",
                    "reason": f"模块 {module_name} 注册成功",
                    "data": {"module": module_name, "registered": True},
                    "warnings": [],
                }
            except Exception as e:
                logger.error("注册模块失败: %s #RECOVERY: 检查约束格式是否合法", e)
                return {
                    "status": "error",
                    "reason": f"注册异常: {str(e)}",
                    "data": {},
                    "warnings": [f"registration_error: {str(e)}"],
                }

    def negotiate(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        """
        发起一次完整的协商流程，将策略意图转换为最终可执行的决策

        Args:
            intent: 原始意图字典，至少包含 intent_type, urgency, desired_size_pct 等字段

        Returns:
            标准响应字典，data 中包含最终执行方案（allowed, allowed_size_pct, preferred_method 等）
        """
        if not self._neuro_pulse:
            return {
                "status": "error",
                "reason": "协商层核心不可用",
                "data": {"allowed": False, "reason": "negotiation_bus_unavailable"},
                "warnings": ["neuro_pulse_unavailable"],
            }

        with self._lock:
            # 1. 标准化意图为 NeuroPulse
            try:
                if not hasattr(self._neuro_pulse, 'encode_intent'):
                    return {
                        "status": "error",
                        "reason": "核心子模块接口不兼容：缺少 encode_intent",
                        "data": {"allowed": False},
                        "warnings": ["interface_mismatch"],
                    }
                pulse = self._neuro_pulse.encode_intent(intent)
            except Exception as e:
                logger.error("意图编码失败: %s #RECOVERY: 检查 intent 字段是否完整", e)
                return {
                    "status": "error",
                    "reason": f"意图编码失败: {str(e)}",
                    "data": {"allowed": False},
                    "warnings": ["intent_encode_failed"],
                }

            # 2. 尝试从预协商缓存获取结果
            if self._predictive_cache and hasattr(self._predictive_cache, 'get'):
                try:
                    cached_response = self._predictive_cache.get(pulse)
                    if cached_response:
                        logger.debug("预协商缓存命中，跳过完整协商流程")
                        return {
                            "status": "ok",
                            "reason": "预协商缓存命中",
                            "data": cached_response,
                            "warnings": [],
                        }
                except Exception as e:
                    logger.warning("预协商缓存读取失败，将执行完整协商: %s", e)

            # 3. 收集约束响应
            constraints = self._collect_constraints(pulse)

            # 4. 冲突仲裁
            if self._conflict_arbiter and hasattr(self._conflict_arbiter, 'resolve'):
                final_decision = self._conflict_arbiter.resolve(pulse, constraints)
            else:
                final_decision = self._get_conservative_decision(pulse)
                logger.warning("冲突仲裁器不可用或接口不兼容，使用保守默认决策")

            # 5. 超时回退检查
            if self._timeout_fallback and hasattr(self._timeout_fallback, 'apply'):
                final_decision = self._timeout_fallback.apply(pulse, final_decision)

            # 6. 更新预协商缓存
            if self._predictive_cache and hasattr(self._predictive_cache, 'set') and final_decision.get("allowed", False):
                try:
                    self._predictive_cache.set(pulse, final_decision)
                except Exception as e:
                    logger.warning("预协商缓存写入失败: %s", e)

            return {
                "status": "ok",
                "reason": f"协商完成: {'允许' if final_decision.get('allowed') else '拒绝'}",
                "data": final_decision,
                "warnings": final_decision.get("warnings", []),
            }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检

        Returns:
            标准健康检查响应字典
        """
        try:
            if not self._neuro_pulse:
                return {
                    "status": "error",
                    "reason": "核心子模块 NeuroPulse 不可用，协商层已瘫痪",
                    "data": {"submodules": self._submodule_status},
                    "warnings": ["neuro_pulse_critical_failure"],
                }

            sub_health = {}
            for name, available in self._submodule_status.items():
                if not available:
                    continue
                instance = getattr(self, f"_{name}", None)
                if instance is None:
                    continue
                try:
                    if hasattr(instance, "health_check"):
                        sub_health[name] = instance.health_check()
                    else:
                        sub_health[name] = {"status": "ok", "message": "无 health_check 方法"}
                except Exception as e:
                    sub_health[name] = {"status": "error", "message": str(e)}

            all_ok = all(self._submodule_status.values())

            return {
                "status": "ok" if all_ok else "degraded",
                "reason": f"协商层状态: {'正常' if all_ok else '部分降级'}",
                "data": {
                    "submodules": self._submodule_status,
                    "sub_health": sub_health,
                },
                "warnings": [] if all_ok else [
                    f"degraded_submodules: {[k for k, v in self._submodule_status.items() if not v]}"
                ],
            }
        except Exception as e:
            logger.error("健康检查失败: %s #RECOVERY: 检查子模块文件完整性", e)
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _collect_constraints(self, pulse: Any) -> Dict[str, Any]:
        """收集所有已注册模块的约束响应（若仲裁器不可用或接口不兼容，返回空字典）"""
        if self._conflict_arbiter and hasattr(self._conflict_arbiter, 'collect'):
            try:
                return self._conflict_arbiter.collect(pulse)
            except Exception as e:
                logger.warning("约束收集失败: %s", e)
                return {}
        return {}

    def _get_conservative_decision(self, pulse: Any) -> Dict[str, Any]:
        """返回最保守决策（仲裁器降级时使用）"""
        return {
            "allowed": False,
            "reason": "冲突仲裁器不可用，采用最保守策略拒绝所有请求",
            "allowed_size_pct": 0.0,
            "preferred_method": "none",
            "warnings": ["conflict_arbiter_unavailable"],
        }
