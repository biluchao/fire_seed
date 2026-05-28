"""
火种系统 · 协商总线入口 (NegotiationBus)

核心职责：
1. 作为跨模块协商层的统一入口，负责初始化、管理和调度四个子模块（语义向量、冲突仲裁、预协商缓存、超时回退）
2. 对外提供标准化的协商接口，所有模块的意图与约束均通过此入口转换为标准化 NeuroPulse 并获取 NeuroConstraint 响应
3. 实现分级锁与优先级通道，保障极端行情下 P0/P1 级指令不被阻塞，同时防止高并发下的缓存击穿与约束空窗
4. 提供线程安全的快速通道，确保 P0 指令无锁执行；异步告警具备流控，防止线程风暴；健康检查具备超时保护
5. 采用“陈旧但可用”的缓存策略应对风险状态频繁变更，防止极端行情下的缓存雪崩
6. 约束收集失败时对 P0/P1 指令放行（使用生存级硬约束），仅拦截 P2 及以下请求，防止系统僵死

外部依赖（真实模块接口）：
- core.negotiation_bus.neuro_pulse.NeuroPulse : 通用语义向量与约束响应的定义与验证 (encode_intent 必须为纯函数、线程安全)
- core.negotiation_bus.conflict_arbiter.ConflictArbiter : 多模块约束冲突的优先级仲裁 (collect/resolve 必须为线程安全)
- core.negotiation_bus.predictive_cache.PredictiveCache : 预协商缓存管理与命中检测 (get/set 必须为线程安全)
- core.negotiation_bus.timeout_fallback.TimeoutFallback : 硬超时安全回退与模块降级
- core.risk_monitor.risk_color_manager.RiskColorManager : (可选) 获取当前风险状态哈希及等级，用于缓存时效性校验和“陈旧但可用”策略

接口契约：
- register_module(module_name: str, constraints: Dict) -> Dict[str, Any] : 注册模块约束到协商层
- negotiate(intent: Dict[str, Any], skip_cache: bool = False) -> Dict[str, Any] : 发起一次完整协商，返回最终执行方案
- health_check() -> Dict[str, Any] : 模块自检（含快速通道+标准通道穿透测试，不污染生产缓存，具备锁获取超时）
- inject_risk_monitor(risk_monitor) -> None : 注入风险监控模块，用于缓存风险状态校验
- inject_alert_handler(handler: Callable) -> None : 注入告警处理器（异步执行，具备流控与LRU去重）
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当任一子模块初始化失败时，该子模块功能自动降级为保守默认值（如冲突仲裁失败则返回最保守约束）
- 当 NeuroPulse 不可用时，拒绝所有协商请求并返回错误码
- 当冲突仲裁器不可用或约束收集失败时：P0/P1 指令使用生存级硬约束放行；P2 及以下返回保守决策
- 快速通道中若仲裁器方法抛出 RuntimeError（数据竞争），回退为保守决策
- 健康检查若在1秒内无法获取锁，返回“阻塞”状态，防止僵死

资源管理：
- 本模块不持有任何外部资源句柄
- 子模块均为无状态或自包含状态，生命周期随本模块实例自动管理
- 采用分级锁：`_register_lock` 保护注册表，高优先级与普通优先级协商分别使用独立锁，`_cache_lock` 保护缓存
- 异步告警使用线程池 + 有界队列，最大并发 2 线程，防止线程风暴。去重字典采用 LRU 策略防止内存泄漏
- 健康检查锁获取超时 1 秒，超时自动放弃并返回降级状态

技术债务：
- 本类当前承载了协商、缓存、告警、健康检查等多重职责，代码行数已超过 500。在未来迭代中建议将缓存管理、告警管理、健康检查拆分为独立的内部服务类，以降低复杂度熵增。
"""

import time
import logging
import threading
from typing import Dict, Any, Optional, Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import queue
from collections import OrderedDict

logger = logging.getLogger(__name__)


class NegotiationBus:
    """协商总线入口，负责子模块装配与请求路由"""

    # 类常量
    CACHE_TTL_SEC = 0.0005          # 缓存有效期 500 微秒
    P0_URGENCY_THRESHOLD = 9       # P0 级指令最低紧急度
    P1_URGENCY_THRESHOLD = 6       # P1 级指令最低紧急度（用于分级锁与降级豁免）
    HEALTH_CHECK_LOCK_TIMEOUT = 1.0  # 健康检查获取锁超时(秒)
    ALERT_MAX_WORKERS = 2           # 告警线程池最大线程数
    ALERT_QUEUE_SIZE = 50           # 告警队列容量
    ALERT_DEDUP_SECONDS = 5.0       # 告警去重窗口(秒)
    ALERT_DEDUP_MAX_SIZE = 500      # 去重字典最大容量，超出则LRU淘汰

    def __init__(self):
        # 子模块实例（延迟初始化）
        self._neuro_pulse = None
        self._conflict_arbiter = None
        self._predictive_cache = None
        self._timeout_fallback = None

        # 可选依赖
        self._risk_monitor = None
        self._alert_handler = None

        # 子模块可用性标记
        self._submodule_status = {
            "neuro_pulse": False,
            "conflict_arbiter": False,
            "predictive_cache": False,
            "timeout_fallback": False,
        }

        # 分级锁
        self._register_lock = threading.Lock()
        self._high_priority_lock = threading.Lock()   # P0/P1 级指令
        self._normal_priority_lock = threading.Lock() # 普通请求
        self._cache_lock = threading.Lock()

        # 异步告警设施（线程池 + 有界队列 + LRU去重）
        self._alert_executor = ThreadPoolExecutor(max_workers=self.ALERT_MAX_WORKERS, thread_name_prefix="negotiation_alert")
        self._alert_queue = queue.Queue(maxsize=self.ALERT_QUEUE_SIZE)
        self._alert_dedup: OrderedDict[str, float] = OrderedDict()
        self._alert_dedup_lock = threading.Lock()

        # 生存级硬约束（当约束收集失败时，P0/P1 指令使用的硬编码安全值）
        self._survival_constraints = {
            "max_size_pct": 0.0,       # 禁止开仓
            "allowed_actions": ["close_all", "emergency_flat"],  # 仅允许平仓
            "preferred_method": "market_order",  # 强制市价单
        }

        # 初始化子模块（带降级与告警）
        self._init_submodules()

        logger.info("NegotiationBus 初始化完成，子模块状态: %s", self._submodule_status)

    # ========== 可选依赖注入 ==========
    def inject_risk_monitor(self, risk_monitor: Any) -> None:
        """
        注入风险监控模块，用于缓存时效性校验。
        注入时会校验接口兼容性，不满足则拒绝注入。
        """
        required_methods = ['get_risk_state_hash', 'get_risk_level']
        for method in required_methods:
            if not hasattr(risk_monitor, method):
                logger.warning("RiskMonitor 缺少 %s 方法，拒绝注入", method)
                return
        self._risk_monitor = risk_monitor
        logger.info("RiskMonitor 注入协商层成功")

    def inject_alert_handler(self, handler: Callable) -> None:
        """注入告警处理器，用于推送严重故障。处理器接收 (level: str, message: str) 参数。"""
        self._alert_handler = handler

    # ========== 子模块初始化 ==========
    def _init_submodules(self) -> None:
        """按依赖顺序初始化子模块，任一失败自动降级并告警"""
        # 1. 语义向量（核心依赖，必须可用）
        try:
            from core.negotiation_bus.neuro_pulse import NeuroPulse
            self._neuro_pulse = NeuroPulse()
            self._submodule_status["neuro_pulse"] = True
            logger.info("NeuroPulse 子模块初始化成功")
        except Exception as e:
            self._report_fatal("NeuroPulse", e)
            return

        # 2. 冲突仲裁
        try:
            from core.negotiation_bus.conflict_arbiter import ConflictArbiter
            self._conflict_arbiter = ConflictArbiter()
            self._submodule_status["conflict_arbiter"] = True
            logger.info("ConflictArbiter 子模块初始化成功")
        except Exception as e:
            self._report_degraded("ConflictArbiter", e)

        # 3. 预协商缓存
        try:
            from core.negotiation_bus.predictive_cache import PredictiveCache
            self._predictive_cache = PredictiveCache()
            self._submodule_status["predictive_cache"] = True
            logger.info("PredictiveCache 子模块初始化成功")
        except Exception as e:
            self._report_degraded("PredictiveCache", e)

        # 4. 超时回退
        try:
            from core.negotiation_bus.timeout_fallback import TimeoutFallback
            self._timeout_fallback = TimeoutFallback()
            self._submodule_status["timeout_fallback"] = True
            logger.info("TimeoutFallback 子模块初始化成功")
        except Exception as e:
            self._report_degraded("TimeoutFallback", e)

    def _report_fatal(self, module: str, error: Exception) -> None:
        msg = f"致命: {module} 初始化失败，协商层不可用"
        logger.error("%s: %s #RECOVERY: 检查对应模块文件完整性", msg, error)
        self._push_alert("critical", msg)

    def _report_degraded(self, module: str, error: Exception) -> None:
        msg = f"降级: {module} 初始化失败，将使用保守默认值"
        logger.warning("%s: %s", msg, error)
        self._push_alert("warning", msg)

    def _push_alert(self, level: str, message: str) -> None:
        """
        异步推送告警，使用线程池 + 有界队列 + LRU去重，防止线程风暴和内存泄漏。
        """
        if not self._alert_handler:
            return

        # 去重检查（带LRU淘汰）
        dedup_key = f"{level}:{message[:100]}"
        now = time.time()
        with self._alert_dedup_lock:
            last_time = self._alert_dedup.get(dedup_key, 0)
            if now - last_time < self.ALERT_DEDUP_SECONDS:
                return
            # LRU 淘汰
            if len(self._alert_dedup) >= self.ALERT_DEDUP_MAX_SIZE:
                oldest_key = next(iter(self._alert_dedup))
                del self._alert_dedup[oldest_key]
            self._alert_dedup[dedup_key] = now

        # 尝试将告警任务放入队列
        try:
            self._alert_queue.put_nowait((level, message))
        except queue.Full:
            logger.warning("告警队列已满，丢弃告警: %s", message)
            return

        # 提交执行（线程池会自动限制并发）
        try:
            self._alert_executor.submit(self._do_push_alert, level, message)
        except RuntimeError:
            logger.warning("告警线程池已关闭，无法推送告警")

    def _do_push_alert(self, level: str, message: str) -> None:
        """实际执行告警推送的工作线程"""
        try:
            self._alert_handler(level, message)
        except Exception:
            pass

    # ========== 公共接口 ==========
    def register_module(self, module_name: str, constraints: Dict[str, Any]) -> Dict[str, Any]:
        """
        向协商层注册一个模块的约束条件
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

        with self._register_lock:
            try:
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

                if self._conflict_arbiter and hasattr(self._conflict_arbiter, 'register'):
                    self._conflict_arbiter.register(module_name, validated["constraints"])
                    logger.info("模块 %s 已注册到协商层", module_name)

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

    def negotiate(self, intent: Dict[str, Any], skip_cache: bool = False) -> Dict[str, Any]:
        """
        发起一次完整的协商流程。
        对于 P0 级生存指令（urgency >= 9），绕过锁队列直接走快速通道。
        对于 P1 级指令（urgency >= 6），使用高优先级锁。
        参数 skip_cache=True 将跳过缓存写入（用于健康检查等避免污染生产缓存）。
        """
        if not self._neuro_pulse:
            return {
                "status": "error",
                "reason": "协商层核心不可用",
                "data": {"allowed": False, "reason": "negotiation_bus_unavailable"},
                "warnings": ["neuro_pulse_unavailable"],
            }

        urgency = intent.get("urgency", 0)
        if isinstance(urgency, (int, float)) and urgency >= self.P0_URGENCY_THRESHOLD:
            return self._fast_negotiate(intent)

        # 根据紧急度选择锁
        if isinstance(urgency, (int, float)) and urgency >= self.P1_URGENCY_THRESHOLD:
            with self._high_priority_lock:
                return self._standard_negotiate(intent, skip_cache=skip_cache)
        else:
            with self._normal_priority_lock:
                return self._standard_negotiate(intent, skip_cache=skip_cache)

    # ========== 编码辅助 ==========
    def _encode_intent_safe(self, intent: Dict[str, Any]) -> Any:
        """
        安全编码意图。NeuroPulse.encode_intent 必须为纯函数，不修改内部状态。
        此方法对外保证线程安全。
        """
        if not hasattr(self._neuro_pulse, 'encode_intent'):
            raise AttributeError("NeuroPulse 缺少 encode_intent 方法")
        return self._neuro_pulse.encode_intent(intent)

    # ========== 快速通道 ==========
    def _fast_negotiate(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        """P0 级指令快速通道：无锁执行，直接仲裁。若仲裁器出现数据竞争则回退保守决策。"""
        try:
            pulse = self._encode_intent_safe(intent)
        except Exception as e:
            logger.error("快速通道意图编码失败: %s", e)
            return {
                "status": "error",
                "reason": f"意图编码失败: {str(e)}",
                "data": {"allowed": False},
                "warnings": ["intent_encode_failed"],
            }

        constraints = self._collect_constraints(pulse)
        if constraints is None:
            # P0 指令在约束收集失败时使用生存级硬约束，避免系统僵死
            return {
                "status": "degraded",
                "reason": "约束收集失败，P0指令使用生存级硬约束",
                "data": dict(self._survival_constraints, allowed=True, warnings=["survival_mode_override"]),
                "warnings": ["constraint_collection_failed", "survival_mode_override"],
            }

        try:
            if self._conflict_arbiter and hasattr(self._conflict_arbiter, 'resolve'):
                final_decision = self._conflict_arbiter.resolve(pulse, constraints)
            else:
                final_decision = self._get_conservative_decision(pulse)
        except RuntimeError as e:
            logger.error("快速通道仲裁数据竞争: %s，回退保守决策", e)
            final_decision = self._get_conservative_decision(pulse)

        if self._timeout_fallback and hasattr(self._timeout_fallback, 'apply'):
            final_decision = self._timeout_fallback.apply(pulse, final_decision)

        return {
            "status": "ok",
            "reason": f"快速协商完成: {'允许' if final_decision.get('allowed') else '拒绝'}",
            "data": final_decision,
            "warnings": final_decision.get("warnings", []),
        }

    # ========== 标准协商 ==========
    def _standard_negotiate(self, intent: Dict[str, Any], skip_cache: bool = False) -> Dict[str, Any]:
        """常规协商流程（需在对应优先级锁内调用）"""
        urgency = intent.get("urgency", 0)

        try:
            pulse = self._encode_intent_safe(intent)
        except Exception as e:
            logger.error("意图编码失败: %s", e)
            return {
                "status": "error",
                "reason": f"意图编码失败: {str(e)}",
                "data": {"allowed": False},
                "warnings": ["intent_encode_failed"],
            }

        # 1. 尝试从缓存获取（带风险状态感知的“陈旧但可用”策略）
        if not skip_cache:
            cached = self._get_cached_decision(pulse)
            if cached:
                return {
                    "status": "ok",
                    "reason": "预协商缓存命中",
                    "data": cached,
                    "warnings": [],
                }

        # 2. 收集约束
        constraints = self._collect_constraints(pulse)
        if constraints is None:
            # P0/P1 指令在约束收集失败时使用生存级硬约束放行，P2及以下返回保守决策
            if isinstance(urgency, (int, float)) and urgency >= self.P1_URGENCY_THRESHOLD:
                final_decision = dict(self._survival_constraints, allowed=True, warnings=["survival_mode_override"])
                logger.warning("约束收集失败，P1+ 指令使用生存级硬约束")
            else:
                final_decision = self._get_conservative_decision(pulse)
                logger.warning("约束收集失败，P2- 指令采用保守决策")
        else:
            # 3. 冲突仲裁
            if self._conflict_arbiter and hasattr(self._conflict_arbiter, 'resolve'):
                final_decision = self._conflict_arbiter.resolve(pulse, constraints)
            else:
                final_decision = self._get_conservative_decision(pulse)

        # 4. 超时回退
        if self._timeout_fallback and hasattr(self._timeout_fallback, 'apply'):
            final_decision = self._timeout_fallback.apply(pulse, final_decision)

        # 5. 更新缓存（健康检查不写入）
        if not skip_cache and final_decision.get("allowed", False):
            self._set_cached_decision(pulse, final_decision)

        return {
            "status": "ok",
            "reason": f"协商完成: {'允许' if final_decision.get('allowed') else '拒绝'}",
            "data": final_decision,
            "warnings": final_decision.get("warnings", []),
        }

    # ========== 缓存管理（陈旧但可用策略） ==========
    def _get_cached_decision(self, pulse: Any) -> Optional[Dict[str, Any]]:
        """
        从缓存获取决策，并校验风险哈希与TTL。
        采用“陈旧但可用”策略：若风险状态变化，仅当当前风险比缓存时更严格时采纳缓存，
        若更宽松则穿透查询，防止缓存雪崩。
        """
        if not self._predictive_cache or not hasattr(self._predictive_cache, 'get'):
            return None
        with self._cache_lock:
            entry = self._predictive_cache.get(pulse)
            if not entry:
                return None
            if time.time() - entry.get("timestamp", 0) > self.CACHE_TTL_SEC:
                return None
            if self._risk_monitor:
                try:
                    current_hash = self._risk_monitor.get_risk_state_hash()
                    cached_hash = entry.get("risk_hash")
                    if cached_hash and current_hash != cached_hash:
                        # 风险状态变化：检查是否当前风险更严格
                        current_level = self._risk_monitor.get_risk_level()
                        cached_level = entry.get("risk_level", 0)
                        if current_level > cached_level:
                            # 当前更严格，缓存中的决策是在更宽松环境下做出的，不可用
                            return None
                        # 否则当前更宽松，缓存决策（在更严格时做出）仍然保守安全，可采纳
                except Exception:
                    return None
            return entry.get("decision")

    def _set_cached_decision(self, pulse: Any, decision: Dict[str, Any]) -> None:
        """写入缓存，附带风险状态哈希、风险等级与时间戳"""
        if not self._predictive_cache or not hasattr(self._predictive_cache, 'set'):
            return
        entry = {
            "decision": decision,
            "timestamp": time.time(),
        }
        if self._risk_monitor:
            try:
                entry["risk_hash"] = self._risk_monitor.get_risk_state_hash()
                entry["risk_level"] = self._risk_monitor.get_risk_level()
            except Exception:
                pass
        with self._cache_lock:
            self._predictive_cache.set(pulse, entry)

    # ========== 约束收集 ==========
    def _collect_constraints(self, pulse: Any) -> Optional[Dict[str, Any]]:
        """收集约束，失败返回 None 而非空字典，避免无约束执行"""
        if self._conflict_arbiter and hasattr(self._conflict_arbiter, 'collect'):
            try:
                return self._conflict_arbiter.collect(pulse)
            except Exception as e:
                logger.error("约束收集异常: %s", e)
                return None
        return None

    def _get_conservative_decision(self, pulse: Any) -> Dict[str, Any]:
        """返回最保守决策（仲裁器降级时使用）"""
        return {
            "allowed": False,
            "reason": "协商层降级，采用最保守策略",
            "allowed_size_pct": 0.0,
            "preferred_method": "none",
            "warnings": ["negotiation_bus_degraded"],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检：包含快速通道与标准通道的双重穿透测试。
        使用带超时的锁获取避免在高负载下僵死。
        测试过程不污染生产缓存。
        """
        try:
            if not self._neuro_pulse:
                return {
                    "status": "error",
                    "reason": "核心子模块 NeuroPulse 不可用",
                    "data": {},
                    "warnings": ["neuro_pulse_critical_failure"],
                }

            # 测试1：快速通道穿透测试（模拟 P0 生存级指令）
            fast_test_intent = {
                "intent_type": "health_probe",
                "urgency": 10,  # P0 级别
                "desired_size_pct": 0.0,
            }
            fast_result = self._fast_negotiate(fast_test_intent)
            fast_passed = fast_result["status"] == "ok" and fast_result["data"].get("allowed", False)

            # 测试2：标准通道穿透测试（带锁超时）
            standard_passed = False
            acquired = self._normal_priority_lock.acquire(timeout=self.HEALTH_CHECK_LOCK_TIMEOUT)
            if not acquired:
                logger.warning("健康检查标准通道获取锁超时")
            else:
                try:
                    standard_test_intent = {
                        "intent_type": "health_probe",
                        "urgency": 0,
                        "desired_size_pct": 0.0,
                    }
                    standard_result = self._standard_negotiate(standard_test_intent, skip_cache=True)
                    standard_passed = (
                        standard_result["status"] == "ok" and
                        standard_result["data"].get("allowed", False)
                    )
                except Exception as e:
                    logger.error("标准通道健康检查异常: %s", e)
                finally:
                    self._normal_priority_lock.release()

            # 子模块健康检查
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

            all_ok = all(self._submodule_status.values()) and fast_passed and standard_passed

            warnings = []
            if not fast_passed:
                warnings.append("fast_lane_test_failed")
            if not standard_passed and acquired:
                warnings.append("standard_lane_test_failed")
            if not acquired:
                warnings.append("standard_lane_timeout")

            return {
                "status": "ok" if all_ok else "degraded",
                "reason": f"协商层状态: {'正常' if all_ok else '部分降级'}",
                "data": {
                    "submodules": self._submodule_status,
                    "sub_health": sub_health,
                    "fast_lane_test": "passed" if fast_passed else "failed",
                    "standard_lane_test": "passed" if standard_passed else ("timeout" if not acquired else "failed"),
                },
                "warnings": warnings,
            }
        except Exception as e:
            logger.error("健康检查失败: %s #RECOVERY: 检查子模块文件完整性", e)
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
                         }
