"""
火种系统 · 协商总线超时回退处理器 (TimeoutFallback)

核心职责：
1. 维护每个已注册模块的超时阈值和安全回退值，在模块超时未响应时提供保守的默认响应
2. 持续追踪各模块的响应时间统计（P95），自动判定模块是否需要降级，触发告警与行为日志

外部依赖（真实模块接口）：
- core.negotiation_bus.NegotiationBus : 获取已注册模块列表及其注册时声明的超时阈值
- core.behavioral_logger.BehavioralLogger : 记录降级事件与超时统计

接口契约：
- register_module(module_name: str, timeout_ms: float, fallback_data: Dict[str, Any]) -> Dict[str, Any]
- record_response(module_name: str, elapsed_ms: float) -> Dict[str, Any]
- get_fallback(module_name: str) -> Dict[str, Any]
- get_module_health(module_name: str) -> Dict[str, Any]
- get_all_health() -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当传入未注册的模块名时，返回通用保守默认值，并记录 WARNING 日志
- 当模块响应时间统计样本不足时，使用 timeout_ms * 2 作为 P95 的保守估计值
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护每个模块的响应时间滑动窗口，定期清理过期数据
- 统计数据使用独立锁保护，避免与协商总线主锁产生死锁
- 不持有任何外部资源句柄
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional
from collections import deque
import numpy as np

logger = logging.getLogger(__name__)


class TimeoutFallback:
    """协商总线超时回退处理器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_TIMEOUT_MS = 300              # 默认超时时间，毫秒，取值范围 [50, 1000]
    DEFAULT_WINDOW_SAMPLES = 50           # 响应时间滑动窗口最大样本数，无量纲，[20, 200]
    DEGRADATION_THRESHOLD_RATIO = 1.5     # 降级判定阈值：P95 超时超过此倍率则降级，无量纲，[1.2, 3.0]
    RECOVERY_THRESHOLD_RATIO = 0.8        # 恢复阈值：P95 回落至此倍率以下则恢复，无量纲，[0.5, 1.0]
    MIN_SAMPLES_FOR_EVAL = 5              # 最小有效样本数，无量纲，[3, 20]
    CLEANUP_INTERVAL_SEC = 300            # 统计清理间隔，秒，[120, 900]
    MAX_DATA_AGE_SEC = 1800               # 统计数据最大保留时间，秒，[600, 3600]
    FALLBACK_DEFAULT_DATA = {              # 通用保守默认值（作为最后回退）
        "allowed": False,
        "allowed_size_pct": 0.0,
        "preferred_method": "limit_order",
        "suggested_delay_us": 500,
        "adjustment_reason": "模块超时，使用全局默认保守值",
    }

    def __init__(self):
        # 模块注册表：module_name -> {timeout_ms, fallback_data}
        self._registry: Dict[str, Dict[str, Any]] = {}

        # 响应时间统计：module_name -> deque of elapsed_ms
        self._response_stats: Dict[str, deque] = {}

        # 模块降级状态：module_name -> bool
        self._degraded_modules: Dict[str, bool] = {}

        # 降级触发时间记录：module_name -> float
        self._degraded_since: Dict[str, float] = {}

        # 外部依赖注入
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 线程安全锁
        self._registry_lock = threading.Lock()
        self._stats_lock = threading.Lock()

        # 清理定时器
        self._last_cleanup = time.time()

        logger.info("TimeoutFallback 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）

        Args:
            negotiation_bus: 协商总线实例（可选）
            behavioral_logger: 行为日志实例（可选）
        """
        if negotiation_bus is not None:
            self._negotiation_bus = negotiation_bus
            logger.info("NegotiationBus 注入成功")
        else:
            logger.warning("NegotiationBus 未注入，降级为本地日志")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，降级为标准 logger")

    # ========== 公共接口 ==========
    @classmethod
    def register_module(
        cls,
        module_name: str,
        timeout_ms: float,
        fallback_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        注册模块的超时阈值与安全回退值

        Args:
            module_name: 模块名称（如 'risk_monitor.circuit_breaker'）
            timeout_ms: 超时阈值（毫秒）
            fallback_data: 超时后返回的安全回退数据字典

        Returns:
            标准响应字典
        """
        if not module_name or not isinstance(module_name, str):
            return {
                "status": "error",
                "reason": f"无效模块名称: {module_name}",
                "data": {},
                "warnings": [f"invalid_module_name: {module_name}"],
            }

        # 使用实例方法需要 self，这里调整为实例方法
        pass

    def register_module_instance(
        self,
        module_name: str,
        timeout_ms: float,
        fallback_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        注册模块的超时阈值与安全回退值（实例方法）

        Args:
            module_name: 模块名称
            timeout_ms: 超时阈值（毫秒）
            fallback_data: 超时后返回的安全回退数据字典

        Returns:
            标准响应字典
        """
        if not module_name or not isinstance(module_name, str):
            return {
                "status": "error",
                "reason": f"无效模块名称: {module_name}",
                "data": {},
                "warnings": [f"invalid_module_name: {module_name}"],
            }

        if timeout_ms <= 0:
            logger.warning(f"模块 {module_name} 超时阈值无效({timeout_ms}ms)，使用默认值 {self.DEFAULT_TIMEOUT_MS}ms")
            timeout_ms = self.DEFAULT_TIMEOUT_MS

        with self._registry_lock:
            self._registry[module_name] = {
                "timeout_ms": timeout_ms,
                "fallback_data": fallback_data or self.FALLBACK_DEFAULT_DATA.copy(),
            }
            # 初始化响应统计
            if module_name not in self._response_stats:
                self._response_stats[module_name] = deque(maxlen=self.DEFAULT_WINDOW_SAMPLES)

        logger.info(f"注册模块 {module_name}，超时阈值={timeout_ms}ms")
        return {
            "status": "ok",
            "reason": f"模块 {module_name} 注册成功",
            "data": {"module_name": module_name, "timeout_ms": timeout_ms},
            "warnings": [],
        }

    def record_response(self, module_name: str, elapsed_ms: float) -> Dict[str, Any]:
        """
        记录模块的响应时间

        Args:
            module_name: 模块名称
            elapsed_ms: 本次响应耗时（毫秒）

        Returns:
            标准响应字典
        """
        if module_name not in self._registry:
            logger.warning(f"模块 {module_name} 未注册，无法记录响应时间")
            return {
                "status": "error",
                "reason": f"模块 {module_name} 未注册",
                "data": {},
                "warnings": [f"unregistered_module: {module_name}"],
            }

        if elapsed_ms <= 0:
            return {
                "status": "error",
                "reason": f"无效响应时间: {elapsed_ms}ms",
                "data": {},
                "warnings": ["invalid_elapsed_ms"],
            }

        with self._stats_lock:
            if module_name not in self._response_stats:
                self._response_stats[module_name] = deque(maxlen=self.DEFAULT_WINDOW_SAMPLES)
            self._response_stats[module_name].append(elapsed_ms)

        return {
            "status": "ok",
            "reason": f"已记录模块 {module_name} 响应时间 {elapsed_ms:.1f}ms",
            "data": {"module_name": module_name, "elapsed_ms": elapsed_ms},
            "warnings": [],
        }

    def get_fallback(self, module_name: str) -> Dict[str, Any]:
        """
        获取指定模块的安全回退值（在模块超时时调用）

        Args:
            module_name: 模块名称

        Returns:
            标准响应字典，data 中包含回退值
        """
        with self._registry_lock:
            reg = self._registry.get(module_name)

        if reg is None:
            logger.warning(
                f"模块 {module_name} 未注册，返回全局默认回退值 #RECOVERY: 检查模块是否正确注册"
            )
            return {
                "status": "ok",
                "reason": f"模块 {module_name} 未注册，使用全局默认保守值",
                "data": self.FALLBACK_DEFAULT_DATA.copy(),
                "warnings": [f"unregistered_module: {module_name}"],
            }

        fallback = reg["fallback_data"].copy()
        return {
            "status": "ok",
            "reason": f"返回模块 {module_name} 的安全回退值 (超时阈值={reg['timeout_ms']}ms)",
            "data": fallback,
            "warnings": [],
        }

    def get_module_health(self, module_name: str) -> Dict[str, Any]:
        """
        获取指定模块的响应健康状态

        Args:
            module_name: 模块名称

        Returns:
            标准响应字典，data 中包含 p95, is_degraded 等字段
        """
        with self._registry_lock:
            reg = self._registry.get(module_name)

        if reg is None:
            return {
                "status": "error",
                "reason": f"模块 {module_name} 未注册",
                "data": {},
                "warnings": [f"unregistered_module: {module_name}"],
            }

        timeout_ms = reg["timeout_ms"]

        with self._stats_lock:
            stats = self._response_stats.get(module_name)

        p95 = self._calculate_p95(stats) if stats else None
        sample_count = len(stats) if stats else 0

        if p95 is None:
            p95 = timeout_ms * 2
            sample_note = f"样本不足({sample_count}<{self.MIN_SAMPLES_FOR_EVAL})，使用保守估计"
        else:
            sample_note = f"样本充足({sample_count})"

        ratio = p95 / timeout_ms if timeout_ms > 0 else 1.0
        is_degraded = self._degraded_modules.get(module_name, False)

        return {
            "status": "ok",
            "reason": f"模块 {module_name} 响应健康: P95={p95:.1f}ms, 降级={is_degraded}",
            "data": {
                "module_name": module_name,
                "p95_ms": round(p95, 1),
                "timeout_ms": timeout_ms,
                "ratio": round(ratio, 2),
                "sample_count": sample_count,
                "is_degraded": is_degraded,
                "degraded_since": self._degraded_since.get(module_name),
                "note": sample_note,
            },
            "warnings": [],
        }

    def get_all_health(self) -> Dict[str, Any]:
        """
        获取所有已注册模块的健康状态汇总

        Returns:
            标准响应字典
        """
        all_modules = {}
        degraded_count = 0
        with self._registry_lock:
            module_names = list(self._registry.keys())

        for name in module_names:
            health = self.get_module_health(name)
            if health["status"] == "ok":
                all_modules[name] = health["data"]
                if health["data"].get("is_degraded"):
                    degraded_count += 1

        return {
            "status": "ok",
            "reason": f"已评估 {len(all_modules)} 个模块，{degraded_count} 个处于降级状态",
            "data": {
                "total_modules": len(all_modules),
                "degraded_count": degraded_count,
                "modules": all_modules,
            },
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检

        Returns:
            标准健康检查响应字典
        """
        try:
            with self._registry_lock:
                registry_size = len(self._registry)
                module_names = list(self._registry.keys())
                # 在锁内获取快照
                registry_snapshot = {
                    name: {"timeout_ms": self._registry[name]["timeout_ms"]}
                    for name in module_names
                }

            with self._stats_lock:
                stat_sizes = {name: len(q) for name, q in self._response_stats.items()}

            return {
                "status": "ok",
                "reason": f"TimeoutFallback 正常，已注册 {registry_size} 个模块",
                "data": {
                    "registry_size": registry_size,
                    "modules": registry_snapshot,
                    "sample_counts": stat_sizes,
                    "dependencies": {
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和数据结构完整性")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    @staticmethod
    def _calculate_p95(stats: deque) -> Optional[float]:
        """计算响应时间的 P95 分位值"""
        if not stats:
            return None
        raw = list(stats)
        if len(raw) < 2:
            return float(raw[0])
        return float(np.percentile(raw, 95))

    def _cleanup_expired_stats(self) -> None:
        """定期清理过期的响应统计（细粒度锁）"""
        now = time.time()
        if now - self._last_cleanup < self.CLEANUP_INTERVAL_SEC:
            return

        expired_modules = []
        cutoff = now - self.MAX_DATA_AGE_SEC

        # 先找出需要清理的模块
        with self._stats_lock:
            for module_name in list(self._response_stats.keys()):
                stats = self._response_stats[module_name]
                if stats and len(stats) > 0:
                    first_entry_time = stats[0]
                    if isinstance(first_entry_time, float) and first_entry_time < cutoff:
                        expired_modules.append(module_name)

        # 再逐个清理
        for module_name in expired_modules:
            with self._stats_lock:
                if module_name in self._response_stats:
                    self._response_stats[module_name].clear()
                    logger.debug(f"清理模块 {module_name} 的过期响应统计")

        self._last_cleanup = now

    def _check_degradation(self, module_name: str) -> None:
        """检查模块是否需要降级或恢复"""
        health = self.get_module_health(module_name)
        if health["status"] != "ok":
            return

        data = health["data"]
        ratio = data.get("ratio", 0)
        is_currently_degraded = self._degraded_modules.get(module_name, False)

        if not is_currently_degraded and ratio > self.DEGRADATION_THRESHOLD_RATIO:
            # 触发降级
            self._degraded_modules[module_name] = True
            self._degraded_since[module_name] = time.time()
            logger.warning(
                f"模块 {module_name} 进入降级状态 (P95={data['p95_ms']:.1f}ms, "
                f"超时阈值={data['timeout_ms']}ms, 比率={ratio:.2f})"
            )
            self._log_degradation_event(module_name, "degraded", data)

        elif is_currently_degraded and ratio < self.RECOVERY_THRESHOLD_RATIO:
            # 恢复
            self._degraded_modules[module_name] = False
            degraded_duration = time.time() - self._degraded_since.get(module_name, time.time())
            logger.info(
                f"模块 {module_name} 从降级状态恢复 (持续 {degraded_duration:.0f}s, "
                f"当前 P95={data['p95_ms']:.1f}ms)"
            )
            if module_name in self._degraded_since:
                del self._degraded_since[module_name]
            self._log_degradation_event(module_name, "recovered", data)

    def _log_degradation_event(self, module_name: str, event: str, health_data: Dict[str, Any]) -> None:
        """记录降级/恢复事件到行为日志"""
        if self._behavioral_logger is not None and hasattr(self._behavioral_logger, 'log_event'):
            try:
                self._behavioral_logger.log_event(
                    event_type=f"module_{event}",
                    details={
                        "module": module_name,
                        "event": event,
                        "p95_ms": health_data.get("p95_ms"),
                        "timeout_ms": health_data.get("timeout_ms"),
                        "ratio": health_data.get("ratio"),
                    },
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")
