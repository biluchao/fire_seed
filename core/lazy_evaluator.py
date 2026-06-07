"""
火种系统 · 因子惰性求值器 (LazyEvaluator)
版本: 3.3.0

核心职责：
1. 管理所有注册因子的计算状态，在信号未触发时仅维护轻量级基态指标（如 OBI、CVD 实时值），
   信号触发时才调用完整的因子计算流程，大幅降低非决策期的 CPU 与内存开销。
2. 提供统一的因子值查询接口，确保下游模块（如评分卡）在任何时刻都能获得一致且最新的因子快照。

外部依赖（真实模块接口）：
- core.perception.factor_preprocessor.FactorPreprocessor : 对原始因子值进行去极值、平滑和缺失填充
- core.conditional_weight.ConditionalWeight : 获取各因子的当前有效权重，权重为 0 的因子可直接跳过
- core.negotiation_bus.NegotiationBus : 发送因子状态变更事件（恢复/隔离/退役通知）

接口契约：
- register_factor(name, base_indicator, full_compute, is_hot_path=False, dependencies=None) -> Dict[str, Any]
- unregister_factor(name) -> Dict[str, Any]
- evaluate(name) -> Dict[str, Any]
- evaluate_selected(names: List[str]) -> Dict[str, Any]
- evaluate_all() -> Dict[str, Any]
- evaluate_all_incremental(data_source_timestamps: Dict[str, float]) -> Dict[str, Any]
- force_evaluate(timeout_sec: float = 10.0) -> Dict[str, Any]
- set_signal_triggered(triggered: bool) -> Dict[str, Any]
- get_historical_value(name: str, version: int) -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- export_stats() -> Dict[str, Any]
- cleanup() -> None : 显式释放资源（线程池）
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 FactorPreprocessor 不可用时，因子值直接使用原始计算结果，并标记 "raw_value" 状态
- 当 ConditionalWeight 不可用时，默认所有因子均参与计算，不执行跳过逻辑
- 基态指标计算失败时，使用 DEGRADED_MISSING_VALUE_FILL 常量填充，并记录异常堆栈
- 连续计算失败 5 次的因子自动隔离 60 秒，期间返回降级值；恢复后通过 NegotiationBus 通知上游
- 因子计算函数应抛出 StaleDataError（数据源断连）、FactorComputationTimeout（计算超时）等自定义异常
- 预处理器（FactorPreprocessor）必须实现线程安全的 process 方法，或通过注入锁保护

资源管理：
- 本模块持有注册因子的回调函数引用和统计数据结构，使用 threading.RLock 保护共享状态
- 锁的生命周期与模块实例一致，实例销毁时自动释放
- 支持通过 unregister_factor 主动释放已淘汰因子的回调引用，避免内存泄漏
- _factor_stats 中的 latencies deque（maxlen=100）每个约 800 字节，200 个因子约 160KB
- _history_snapshots 保留最近 5 个版本的全量快照，每个版本约 2.4KB（300 因子），共约 12KB
- ThreadPoolExecutor 在 cleanup() 或 __del__ 中关闭，避免线程泄漏

推荐运行环境：Python 3.11+（利用 OrderedDict 性能优化）
"""

__version__ = "3.3.0"
__all__ = ["LazyEvaluator", "StaleDataError", "FactorComputationTimeout"]

import os
import re
import time
import logging
import threading
from typing import Dict, Any, List, Optional, Callable, Set, Tuple
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import numpy as np

logger = logging.getLogger(__name__)


# ========== 模块级自定义异常 ==========
class StaleDataError(Exception):
    """外部数据源断连导致因子值过期的专用异常。
    因子计算函数应在检测到数据源时间戳过期时抛出此异常。"""
    pass


class FactorComputationTimeout(Exception):
    """因子计算超时的专用异常。
    因子计算函数在内部计算超过预设时限时应抛出此异常。"""
    pass


class LazyEvaluator:
    """因子惰性求值器 - 全球顶级量化对冲基金级生产标准"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_BASE_INDICATOR_TTL_SEC = 0.1     # 基态指标缓存有效期，秒，[0.05, 0.5]
    DEFAULT_FULL_EVAL_RATE_LIMIT_SEC = 0.05  # 全量评估频率限制，秒，0 为无限制，[0, 1.0]
    MAX_FACTOR_REGISTRATIONS = 300           # 最大可注册因子数，无量纲，[100, 500]（下限保证最少策略覆盖，上限防止内存溢出）
    COMPUTE_RETRY_COUNT = 1                   # 计算失败重试次数，无量纲，[0, 3]
    COMPUTE_RETRY_DELAY_SEC = 0.001          # 重试间隔，秒，[0.0005, 0.01]
    COMPUTE_TIMEOUT_SEC = 1.0                # 单因子计算超时，秒，[0.1, 5.0]
    QUARANTINE_FAILURE_THRESHOLD = 5          # 连续失败次数触发隔离，无量纲，[3, 10]
    QUARANTINE_DURATION_SEC = 60             # 隔离持续时间，秒，[30, 300]
    DRIFT_ALERT_THRESHOLD = 0.20             # 基态与完整计算偏差告警阈值，无量纲，[0.1, 0.5]
    LATENCY_SPIKE_MULTIPLIER = 3.0           # 延迟尖峰检测倍数，无量纲，[2.0, 5.0]
    MAX_LATENCY_SAMPLES = 100                # 每因子最大延迟样本数，无量纲，[50, 500]
    MIN_SAMPLES_FOR_SPIKE_CHECK = 20         # 延迟尖峰检测最小样本数，无量纲，[10, 50]
    MIN_EVALUATE_INTERVAL_US = 100           # 单因子最小调用间隔，微秒，[50, 1000]
    HISTORICAL_VERSION_CACHE_SIZE = 5         # 历史版本快照保留数，无量纲，[3, 20]
    HOT_PATH_CACHE_TTL_MULTIPLIER = 0.5      # 热路径因子缓存 TTL 倍数，无量纲，[0.2, 0.8]
    EVALUATE_ALL_SLICE_SIZE = 50             # 分批计算时每批因子数，无量纲，[20, 100]
    FORCE_EVALUATE_TIMEOUT_SEC = 10.0        # 强制评估全局超时，秒，[5.0, 30.0]
    # 降级默认值
    DEGRADED_MISSING_VALUE_FILL = 0.0         # 缺失值默认填充值
    NAN_PLACEHOLDER = float('nan')            # 未计算标记值
    # 因子名称规范
    FACTOR_NAME_PATTERN = r'^[a-z][a-z0-9_]*$'
    # 超时执行线程池大小（基于 CPU 核心数自动计算）
    COMPUTE_EXECUTOR_MAX_WORKERS = max(1, min(4, os.cpu_count() or 2))
    # 预处理锁超时，秒
    PREPROCESS_LOCK_TIMEOUT_SEC = 0.01

    # 因子生命周期状态
    STATE_ACTIVE = "active"
    STATE_DEGRADED = "degraded"
    STATE_QUARANTINED = "quarantined"
    STATE_RETIRED = "retired"

    # 版本回退标记
    HISTORICAL_FALLBACK_VERSION = -1

    def __init__(self):
        # 因子注册表
        self._registry: Dict[str, Dict[str, Any]] = OrderedDict()
        # 基态缓存
        self._base_cache: Dict[str, Dict[str, float]] = {}
        # 信号触发标志
        self._signal_triggered = False
        # 强制评估标志（原子性保障）
        self._force_evaluate_active = False
        # 因子统计
        self._factor_stats: Dict[str, Dict[str, Any]] = {}
        # 隔离区
        self._quarantine: Dict[str, float] = {}
        # 全局版本号
        self._global_version = 0
        # 历史版本快照
        self._history_snapshots: Dict[int, Dict[str, float]] = OrderedDict()
        # 因子依赖拓扑
        self._dependency_graph: Dict[str, Set[str]] = {}
        # 频率限制
        self._last_call_timestamps: Dict[str, float] = {}
        # 外部依赖
        self._preprocessor = None
        self._conditional_weight = None
        self._negotiation_bus = None
        # 线程安全
        self._lock = threading.RLock()
        self._preprocess_lock = threading.Lock()  # 预处理器专用锁
        # 全量评估频率限制
        self._last_full_eval_time = 0.0
        # 超时执行器
        self._compute_executor = ThreadPoolExecutor(
            max_workers=self.COMPUTE_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="lazy_eval_"
        )

        logger.info("LazyEvaluator v%s 初始化完成，推荐 Python 3.11+，最大注册因子 %d，"
                     "计算超时线程池: %d",
                     __version__, self.MAX_FACTOR_REGISTRATIONS,
                     self.COMPUTE_EXECUTOR_MAX_WORKERS)

    def __del__(self):
        """析构时释放线程池资源"""
        self.cleanup()

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        preprocessor: Optional[Any] = None,
        conditional_weight: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选注入，未注入时对应功能降级）"""
        if preprocessor is not None:
            if not hasattr(preprocessor, 'process'):
                logger.warning("FactorPreprocessor 缺少 process 方法，降级为原始值")
            else:
                self._preprocessor = preprocessor
                logger.info("FactorPreprocessor 注入成功（请确保 process 方法是线程安全的）")
        else:
            logger.warning("FactorPreprocessor 未注入，因子值不做预处理")

        if conditional_weight is not None:
            if not hasattr(conditional_weight, 'get_weight'):
                logger.warning("ConditionalWeight 缺少 get_weight 方法，降级为所有因子参与计算")
            else:
                self._conditional_weight = conditional_weight
                logger.info("ConditionalWeight 注入成功")
        else:
            logger.warning("ConditionalWeight 未注入，默认所有因子参与计算")

        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, 'publish_alert'):
                logger.warning("NegotiationBus 缺少 publish_alert 方法，事件通知不可用")
            else:
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")
        else:
            logger.warning("NegotiationBus 未注入，因子事件通知降级为本地日志")

    # ========== 资源管理 ==========
    def cleanup(self) -> None:
        """显式释放资源（线程池）"""
        try:
            self._compute_executor.shutdown(wait=False)
            logger.debug("ThreadPoolExecutor 已关闭")
        except Exception:
            pass

    # ========== 公共接口 ==========
    def register_factor(
        self,
        name: str,
        base_indicator: Callable[[], float],
        full_compute: Callable[[], float],
        is_hot_path: bool = False,
        dependencies: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        注册一个新因子（线程安全）

        Args:
            name: 因子名称，全局唯一，须符合 [a-z][a-z0-9_]* 规范
            base_indicator: 基态指标计算函数，无参数，返回 float
            full_compute: 完整计算函数，无参数，返回 float
            is_hot_path: 是否为热路径因子（高频调用，享有更快缓存）
            dependencies: 本因子依赖的其他因子名称列表

        Returns:
            标准响应字典
        """
        if not re.match(self.FACTOR_NAME_PATTERN, name):
            return {
                "status": "error",
                "reason": f"因子名称 '{name}' 不符合命名规范 [a-z][a-z0-9_]*",
                "data": {},
                "warnings": ["invalid_factor_name_format"],
            }

        if not callable(base_indicator) or not callable(full_compute):
            return {
                "status": "error",
                "reason": f"因子 '{name}' 的计算函数必须是可调用对象",
                "data": {},
                "warnings": ["non_callable_handler"],
            }

        try:
            test_base = base_indicator()
            if not isinstance(test_base, (int, float)):
                return {
                    "status": "error",
                    "reason": f"因子 '{name}' 的 base_indicator 返回值类型错误: {type(test_base).__name__}",
                    "data": {},
                    "warnings": ["invalid_return_type"],
                }
            test_full = full_compute()
            if not isinstance(test_full, (int, float)):
                return {
                    "status": "error",
                    "reason": f"因子 '{name}' 的 full_compute 返回值类型错误: {type(test_full).__name__}",
                    "data": {},
                    "warnings": ["invalid_return_type"],
                }
        except (StaleDataError, FactorComputationTimeout):
            logger.info(f"因子 '{name}' 注册时数据源不可用，仍允许注册")
        except Exception as e:
            logger.warning(f"因子 '{name}' 注册前试调用失败: {e}，仍允许注册但建议检查")

        with self._lock:
            if name not in self._registry and len(self._registry) >= self.MAX_FACTOR_REGISTRATIONS:
                logger.warning(f"因子注册已达上限 {self.MAX_FACTOR_REGISTRATIONS}，拒绝注册: '{name}'")
                return {
                    "status": "error",
                    "reason": f"因子注册数已达上限 {self.MAX_FACTOR_REGISTRATIONS}",
                    "data": {},
                    "warnings": ["registration_limit_reached"],
                }
            is_new = name not in self._registry
            self._registry[name] = {
                "base_indicator": base_indicator,
                "full_compute": full_compute,
                "is_hot_path": is_hot_path,
                "registered_at": time.time(),
            }
            if name not in self._base_cache:
                init_val = float(test_base) if isinstance(test_base, (int, float)) else 0.0
                self._base_cache[name] = {"value": init_val, "timestamp": time.time(),
                                           "data_source_ts": 0.0}
            if name not in self._factor_stats:
                self._factor_stats[name] = {
                    "successes": 0,
                    "failures": 0,
                    "consecutive_failures": 0,
                    "latencies": deque(maxlen=self.MAX_LATENCY_SAMPLES),
                    "last_success_ts": 0.0,
                    "last_value": init_val,
                    "state": self.STATE_ACTIVE,
                }
            if dependencies:
                self._dependency_graph[name] = set(dependencies)
            else:
                self._dependency_graph[name] = set()

        level = logging.INFO if is_new else logging.DEBUG
        logger.log(level, f"因子{'注册' if is_new else '覆盖注册'}: '{name}' "
                    f"(热路径={is_hot_path}, 依赖={dependencies or '无'})")
        return {
            "status": "ok",
            "reason": f"因子 '{name}' 注册成功",
            "data": {"name": name, "is_new": is_new},
            "warnings": [],
        }

    def unregister_factor(self, name: str) -> Dict[str, Any]:
        """安全移除已注册的因子"""
        with self._lock:
            if name not in self._registry:
                return {
                    "status": "error",
                    "reason": f"因子 '{name}' 未注册，无法移除",
                    "data": {},
                    "warnings": ["unknown_factor"],
                }
            del self._registry[name]
            self._base_cache.pop(name, None)
            self._factor_stats.pop(name, None)
            self._quarantine.pop(name, None)
            self._dependency_graph.pop(name, None)
            self._last_call_timestamps.pop(name, None)
            for dep_set in self._dependency_graph.values():
                dep_set.discard(name)
        logger.info(f"因子已移除: '{name}'")
        return {
            "status": "ok",
            "reason": f"因子 '{name}' 已移除",
            "data": {"name": name},
            "warnings": [],
        }

    def evaluate(self, name: str) -> Dict[str, Any]:
        """获取指定因子的当前值"""
        if name not in self._registry:
            logger.warning(f"查询未注册因子: '{name}'")
            return {
                "status": "error",
                "reason": f"因子 '{name}' 未注册",
                "data": {},
                "warnings": ["unknown_factor"],
            }

        now = time.time()
        last_call = self._last_call_timestamps.get(name, 0.0)
        if (now - last_call) * 1e6 < self.MIN_EVALUATE_INTERVAL_US:
            with self._lock:
                cache_entry = self._base_cache.get(name)
                if cache_entry and not np.isnan(cache_entry["value"]):
                    return {
                        "status": "ok",
                        "reason": f"因子 '{name}' 频率限制，返回缓存值",
                        "data": {
                            "factor_name": name,
                            "value": cache_entry["value"],
                            "source": "rate_limited_cache",
                            "version": self._global_version,
                            "stale": True,
                            "latency_us": 0.0,
                        },
                        "warnings": ["rate_limited"],
                    }
        self._last_call_timestamps[name] = now

        with self._lock:
            self._purge_quarantine_expired()
            if name in self._quarantine:
                return {
                    "status": "ok",
                    "reason": f"因子 '{name}' 处于隔离期，返回降级值",
                    "data": {
                        "factor_name": name,
                        "value": self.DEGRADED_MISSING_VALUE_FILL,
                        "source": "quarantined",
                        "version": self._global_version,
                        "stale": True,
                        "latency_us": 0.0,
                    },
                    "warnings": ["factor_quarantined"],
                }

            factor_info = self._registry[name]
            weight = self._get_weight(name)
            is_hot = factor_info.get("is_hot_path", False)
            force_mode = self._force_evaluate_active
            should_full = force_mode or (self._signal_triggered and weight > 0.0)

            if should_full:
                raw_value, source, compute_latency = self._compute_with_retry(
                    name, factor_info["full_compute"], force_mode=force_mode)
                if source == "degraded":
                    cache_entry = self._base_cache.get(name)
                    if cache_entry:
                        raw_value = cache_entry["value"]
                        source = "fallback_cache"
            else:
                ttl = self.DEFAULT_BASE_INDICATOR_TTL_SEC
                if is_hot:
                    ttl *= self.HOT_PATH_CACHE_TTL_MULTIPLIER
                cache_entry = self._base_cache.get(name)
                if cache_entry and (now - cache_entry["timestamp"]) < ttl and not np.isnan(cache_entry["value"]):
                    raw_value = cache_entry["value"]
                    source = "cached_base"
                    compute_latency = 0.0
                else:
                    raw_value, source, compute_latency = self._compute_with_retry(
                        name, factor_info["base_indicator"], force_mode=force_mode)
                    if source != "degraded":
                        self._base_cache[name] = {"value": raw_value, "timestamp": now,
                                                   "data_source_ts": self._get_data_source_timestamp()}

            self._check_latency_spike(name, compute_latency)
            processed_value = self._apply_preprocessing(raw_value)

            return {
                "status": "ok",
                "reason": f"因子 '{name}' 评估完成 (来源: {source}, 耗时: {compute_latency*1e6:.0f}μs)",
                "data": {
                    "factor_name": name,
                    "value": processed_value,
                    "raw_value": raw_value,
                    "source": source,
                    "weight": weight,
                    "version": self._global_version,
                    "stale": source in ("degraded", "quarantined", "rate_limited_cache"),
                    "latency_us": round(compute_latency * 1e6, 1),
                },
                "warnings": [f"factor_degraded:{name}"] if source == "degraded" else [],
            }

    def evaluate_selected(self, names: List[str]) -> Dict[str, Any]:
        """批量计算指定因子（一次锁获取，批量计算）"""
        if not names:
            return {
                "status": "ok",
                "reason": "无指定因子",
                "data": {"factors": {}, "source_counts": {}, "version": self._global_version},
                "warnings": [],
            }

        valid_names = [n for n in names if n in self._registry]
        invalid_names = [n for n in names if n not in self._registry]

        if invalid_names:
            logger.warning(f"evaluate_selected 包含无效因子名称: {invalid_names}")

        with self._lock:
            self._purge_quarantine_expired()
            signal_snapshot = self._signal_triggered
            force_snapshot = self._force_evaluate_active
            version_snapshot = self._global_version

            # 获取注册表快照
            registry_snapshot = {n: self._registry[n] for n in valid_names}
            # 按依赖拓扑排序
            sorted_names = self._topological_sort(valid_names)
            # 检查隔离状态
            quarantined_names = set(self._quarantine.keys()) & set(valid_names)

        factors = {}
        source_counts = {}
        total_latency = 0.0

        for name in sorted_names:
            if name in quarantined_names:
                factors[name] = self.DEGRADED_MISSING_VALUE_FILL
                source_counts["quarantined"] = source_counts.get("quarantined", 0) + 1
                continue

            if name not in registry_snapshot:
                continue

            factor_info = registry_snapshot[name]
            should_full = force_snapshot or (signal_snapshot and self._get_weight(name) > 0.0)
            is_hot = factor_info.get("is_hot_path", False)

            if should_full:
                raw_value, source, latency = self._compute_with_retry(
                    name, factor_info["full_compute"], force_mode=force_snapshot)
                if source == "degraded":
                    with self._lock:
                        cache_entry = self._base_cache.get(name)
                    if cache_entry and not np.isnan(cache_entry["value"]):
                        raw_value = cache_entry["value"]
                        source = "fallback_cache"
            else:
                ttl = self.DEFAULT_BASE_INDICATOR_TTL_SEC
                if is_hot:
                    ttl *= self.HOT_PATH_CACHE_TTL_MULTIPLIER
                now = time.time()
                with self._lock:
                    cache_entry = self._base_cache.get(name)
                if cache_entry and (now - cache_entry["timestamp"]) < ttl and not np.isnan(cache_entry["value"]):
                    raw_value = cache_entry["value"]
                    source = "cached_base"
                    latency = 0.0
                else:
                    raw_value, source, latency = self._compute_with_retry(
                        name, factor_info["base_indicator"], force_mode=force_snapshot)
                    if source != "degraded":
                        with self._lock:
                            self._base_cache[name] = {"value": raw_value, "timestamp": now,
                                                       "data_source_ts": self._get_data_source_timestamp()}

            processed = self._apply_preprocessing(raw_value)
            factors[name] = processed
            source_counts[source] = source_counts.get(source, 0) + 1
            total_latency += latency

        return {
            "status": "ok",
            "reason": f"批量评估完成，{len(factors)}/{len(names)} 个因子",
            "data": {
                "factors": factors,
                "source_counts": source_counts,
                "batch_latency_us": round(total_latency * 1e6, 1),
                "version": version_snapshot,
                "invalid_names": invalid_names,
            },
            "warnings": [f"invalid_factor:{n}" for n in invalid_names],
        }

    def evaluate_all(self) -> Dict[str, Any]:
        """批量获取所有注册因子的当前值"""
        return self._evaluate_all_internal(incremental=False, data_source_timestamps=None)

    def evaluate_all_incremental(self, data_source_timestamps: Dict[str, float]) -> Dict[str, Any]:
        """增量评估：仅重算数据源已更新的因子"""
        return self._evaluate_all_internal(incremental=True, data_source_timestamps=data_source_timestamps)

    def force_evaluate(self, timeout_sec: Optional[float] = None) -> Dict[str, Any]:
        """强制全量完整计算（风控紧急使用）"""
        timeout = timeout_sec if timeout_sec is not None else self.FORCE_EVALUATE_TIMEOUT_SEC
        start_time = time.time()

        with self._lock:
            factor_names = list(self._registry.keys())
            self._global_version += 1
            current_version = self._global_version
            self._base_cache.clear()
            prev_triggered = self._signal_triggered
            self._signal_triggered = True
            self._force_evaluate_active = True
            registry_snapshot = {n: self._registry[n] for n in factor_names}

        logger.warning("强制全量评估已触发 (超时=%ss)，清空所有基态缓存", timeout)

        factors = {}
        for name in factor_names:
            if time.time() - start_time > timeout:
                logger.error(f"强制评估超时 ({timeout}s)，已计算 {len(factors)}/{len(factor_names)} 个因子 "
                             "#RECOVERY: 检查阻塞因子并考虑增加超时时间")
                break
            try:
                raw_value, source, _ = self._compute_with_retry(
                    name, registry_snapshot[name]["full_compute"], force_mode=True)
                if source == "degraded":
                    factors[name] = self.DEGRADED_MISSING_VALUE_FILL
                else:
                    factors[name] = self._apply_preprocessing(raw_value)
            except Exception:
                factors[name] = self.DEGRADED_MISSING_VALUE_FILL

        with self._lock:
            self._signal_triggered = prev_triggered
            self._force_evaluate_active = False

        return {
            "status": "ok" if len(factors) == len(factor_names) else "partial",
            "reason": f"强制评估完成 ({len(factors)}/{len(factor_names)} 个因子, 耗时 {time.time()-start_time:.1f}s)",
            "data": {"factors": factors, "version": current_version, "completed": len(factors) == len(factor_names)},
            "warnings": ["force_evaluate_timeout"] if len(factors) < len(factor_names) else [],
        }

    def set_signal_triggered(self, triggered: bool) -> Dict[str, Any]:
        """设置信号触发状态"""
        with self._lock:
            prev = self._signal_triggered
            switch_time = time.time()
            self._signal_triggered = triggered
            if triggered and not prev:
                for name in self._registry:
                    if name not in self._base_cache:
                        self._base_cache[name] = {
                            "value": self.NAN_PLACEHOLDER,
                            "timestamp": 0.0,
                            "data_source_ts": 0.0,
                        }
                logger.info("信号触发，切换到完整计算模式，基态缓存已预填充 NaN 标记")
            elif not triggered and prev:
                logger.info("信号解除，切换到基态维护模式")

        return {
            "status": "ok",
            "reason": f"信号触发状态已切换: {prev} -> {triggered} (于 {switch_time})",
            "data": {"signal_triggered": triggered, "previous": prev, "switch_timestamp": switch_time},
            "warnings": [],
        }

    def get_historical_value(self, name: str, version: int) -> Dict[str, Any]:
        """获取指定版本的历史因子值"""
        with self._lock:
            if version in self._history_snapshots:
                snapshot = self._history_snapshots[version]
                if name in snapshot:
                    return {
                        "status": "ok",
                        "reason": f"返回因子 '{name}' 在版本 {version} 的值",
                        "data": {"factor_name": name, "value": snapshot[name], "version": version},
                        "warnings": [],
                    }
                return {
                    "status": "error",
                    "reason": f"因子 '{name}' 在版本 {version} 中不存在",
                    "data": {},
                    "warnings": ["factor_not_in_version"],
                }
            # 回退到最近成功值
            if name in self._factor_stats:
                last_ts = self._factor_stats[name].get("last_success_ts", 0.0)
                last_val = self._factor_stats[name].get("last_value", self.DEGRADED_MISSING_VALUE_FILL)
                if last_ts > 0:
                    return {
                        "status": "partial",
                        "reason": f"版本 {version} 不存在，返回最近成功值 (ts={last_ts})",
                        "data": {"factor_name": name, "value": last_val,
                                 "version": self.HISTORICAL_FALLBACK_VERSION},
                        "warnings": ["version_not_found", "fallback_to_last_success"],
                    }
            return {
                "status": "error",
                "reason": f"版本 {version} 不存在（当前版本: {self._global_version}），且无历史成功记录",
                "data": {},
                "warnings": ["version_not_found"],
            }

    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            with self._lock:
                factor_count = len(self._registry)
                quarantine_count = len(self._quarantine)
                degraded_count = sum(
                    1 for s in self._factor_stats.values()
                    if s.get("consecutive_failures", 0) >= self.QUARANTINE_FAILURE_THRESHOLD
                )
                # 精确内存估算 (KB)
                registry_kb = factor_count * 1024 // 1000  # 约 1KB per factor
                history_kb = self.HISTORICAL_VERSION_CACHE_SIZE * factor_count * 8 // 1000
                estimated_memory_kb = registry_kb + history_kb

                stale_factors = []
                for name, stats in self._factor_stats.items():
                    if stats["consecutive_failures"] >= self.QUARANTINE_FAILURE_THRESHOLD:
                        stale_factors.append(name)

            status = "degraded" if len(stale_factors) > factor_count * 0.1 else "ok"

            return {
                "status": status,
                "reason": f"LazyEvaluator {status}，{factor_count} 因子，{quarantine_count} 隔离",
                "data": {
                    "version": __version__,
                    "factor_count": factor_count,
                    "quarantine_count": quarantine_count,
                    "degraded_count": degraded_count,
                    "estimated_memory_kb": estimated_memory_kb,
                    "stale_factors": stale_factors[:10],
                    "dependencies": {
                        "preprocessor": self._preprocessor is not None,
                        "conditional_weight": self._conditional_weight is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                    },
                },
                "warnings": [f"stale_factor:{f}" for f in stale_factors[:5]],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查注册表数据结构完整性")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": ["health_check_failed"],
            }

    def export_stats(self) -> Dict[str, Any]:
        """导出因子统计信息"""
        with self._lock:
            stats_export = {}
            for name, stats in self._factor_stats.items():
                latencies_list = list(stats["latencies"])
                clean_latencies = [x for x in latencies_list if 0 < x < 10.0]
                stats_export[name] = {
                    "successes": stats["successes"],
                    "failures": stats["failures"],
                    "consecutive_failures": stats["consecutive_failures"],
                    "latencies_raw": latencies_list,
                    "latencies_clean": clean_latencies,
                    "last_success_ts": stats["last_success_ts"],
                    "last_value": stats.get("last_value", 0.0),
                    "state": stats["state"],
                }
            return {
                "status": "ok",
                "reason": f"导出 {len(stats_export)} 个因子统计",
                "data": {
                    "factor_count": len(self._registry),
                    "global_version": self._global_version,
                    "stats": stats_export,
                },
                "warnings": [],
            }

    # ========== 私有方法 ==========
    def _evaluate_all_internal(self, incremental: bool = False,
                               data_source_timestamps: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """全量评估内部实现"""
        if not self._registry:
            return {
                "status": "ok",
                "reason": "无注册因子，返回空结果",
                "data": {"factors": {}, "source_counts": {}, "version": self._global_version,
                         "eval_type": "incremental" if incremental else "full"},
                "warnings": ["empty_registry"],
            }

        with self._lock:
            now = time.time()
            if not incremental and self.DEFAULT_FULL_EVAL_RATE_LIMIT_SEC > 0:
                if now - self._last_full_eval_time < self.DEFAULT_FULL_EVAL_RATE_LIMIT_SEC:
                    factors = {name: self._base_cache.get(name, {}).get("value", 0.0)
                              for name in self._registry}
                    return {
                        "status": "ok",
                        "reason": "频率限制，返回缓存值",
                        "data": {"factors": factors, "source_counts": {"cached": len(factors)},
                                 "version": self._global_version, "eval_type": "full"},
                        "warnings": [],
                    }
                self._last_full_eval_time = now

            self._purge_stale_cache()
            self._purge_quarantine_expired()

            # 确定计算列表
            if incremental and data_source_timestamps:
                stale_names = []
                for name in self._registry:
                    cache_entry = self._base_cache.get(name)
                    if not cache_entry or cache_entry.get("data_source_ts", 0) < data_source_timestamps.get(name, float('inf')):
                        stale_names.append(name)
                if not stale_names:
                    factors = {name: self._base_cache.get(name, {}).get("value", 0.0)
                              for name in self._registry}
                    return {
                        "status": "ok",
                        "reason": "增量评估：所有因子数据源未更新",
                        "data": {"factors": factors, "source_counts": {"cached": len(factors)},
                                 "version": self._global_version, "eval_type": "incremental"},
                        "warnings": [],
                    }
                factor_names = stale_names
            else:
                factor_names = list(self._registry.keys())

            # 拓扑排序（保持依赖约束）
            sorted_names = self._topological_sort(factor_names)
            # 按权重排序但不破坏拓扑约束：使用稳定排序，权重相同时保持拓扑序
            weights = {n: self._get_weight(n) for n in sorted_names}
            # 稳定排序，保持同权重因子的拓扑顺序
            sorted_names.sort(key=lambda n: weights.get(n, 1.0), reverse=True)

            self._global_version += 1
            current_version = self._global_version
            signal_snapshot = self._signal_triggered
            force_snapshot = self._force_evaluate_active

        # 批量计算
        factors = {}
        source_counts = {"full": 0, "base": 0, "cached_base": 0, "degraded": 0, "quarantined": 0}
        total_latency = 0.0

        for i in range(0, len(sorted_names), self.EVALUATE_ALL_SLICE_SIZE):
            batch = sorted_names[i:i + self.EVALUATE_ALL_SLICE_SIZE]
            for name in batch:
                result = self.evaluate(name)
                if result["status"] == "ok":
                    factors[name] = result["data"]["value"]
                    src = result["data"]["source"]
                    source_counts[src] = source_counts.get(src, 0) + 1
                    total_latency += result["data"].get("latency_us", 0)
                else:
                    factors[name] = 0.0
                    source_counts["degraded"] += 1
            if i + self.EVALUATE_ALL_SLICE_SIZE < len(sorted_names):
                time.sleep(0)

        # 保存历史快照（锁内）
        with self._lock:
            while len(self._history_snapshots) >= self.HISTORICAL_VERSION_CACHE_SIZE:
                oldest_key = next(iter(self._history_snapshots))
                del self._history_snapshots[oldest_key]
            self._history_snapshots[current_version] = factors.copy()

        return {
            "status": "ok",
            "reason": f"全量评估完成，共 {len(factors)} 个因子，总耗时: {total_latency:.0f}μs",
            "data": {
                "factors": factors,
                "source_counts": source_counts,
                "version": current_version,
                "batch_latency_us": round(total_latency, 1),
                "eval_type": "incremental" if incremental else "full",
            },
            "warnings": [],
        }

    def _topological_sort(self, names: List[str]) -> List[str]:
        """根据依赖图对因子名称列表进行拓扑排序（Kahn 算法）"""
        in_degree = {n: 0 for n in names}
        adj = {n: [] for n in names}
        for n in names:
            for dep in self._dependency_graph.get(n, set()):
                if dep in names:
                    in_degree[n] += 1
                    adj[dep].append(n)

        queue = deque([n for n in names if in_degree[n] == 0])
        result = []
        while queue:
            node = queue.popleft()
            result.append(node)
            for dependent in adj[node]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(result) < len(names):
            remaining = [n for n in names if n not in result]
            logger.error(f"因子依赖图中检测到环状依赖！环中节点: {remaining} "
                         "#RECOVERY: 检查 _dependency_graph 中的依赖关系")
            result.extend(remaining)

        return result

    def _get_weight(self, name: str) -> float:
        """获取因子权重"""
        if self._conditional_weight is not None and hasattr(self._conditional_weight, 'get_weight'):
            try:
                return self._conditional_weight.get_weight(name)
            except Exception:
                return 1.0
        return 1.0

    def _apply_preprocessing(self, raw_value: float) -> float:
        """对原始因子值进行预处理（线程安全）"""
        if not np.isfinite(raw_value):
            raw_value = self.DEGRADED_MISSING_VALUE_FILL
        if self._preprocessor is not None and hasattr(self._preprocessor, 'process'):
            try:
                # 使用预处理专用锁保护外部调用
                with self._preprocess_lock:
                    return self._preprocessor.process(raw_value)
            except Exception as e:
                logger.warning(f"因子预处理失败: {e}，返回原始值")
                return raw_value
        return raw_value

    def _compute_with_retry(self, name: str, compute_fn: Callable[[], float],
                            force_mode: bool = False) -> Tuple[float, str, float]:
        """带重试和超时保护的因子计算，返回 (value, source, latency_seconds)"""
        for attempt in range(self.COMPUTE_RETRY_COUNT + 1):
            start = time.perf_counter()
            try:
                future = self._compute_executor.submit(compute_fn)
                raw_value = future.result(timeout=self.COMPUTE_TIMEOUT_SEC)
                latency = time.perf_counter() - start

                if not isinstance(raw_value, (int, float)) or not np.isfinite(raw_value):
                    raise ValueError(f"因子计算返回非有限值: {raw_value}")

                with self._lock:
                    if name in self._factor_stats:
                        stats = self._factor_stats[name]
                        stats["successes"] += 1
                        stats["consecutive_failures"] = 0
                        stats["latencies"].append(latency)
                        stats["last_success_ts"] = time.time()
                        stats["last_value"] = raw_value
                        stats["state"] = self.STATE_ACTIVE
                        if name in self._quarantine:
                            del self._quarantine[name]
                            logger.info(f"因子 '{name}' 已从隔离区恢复")
                            self._notify_factor_recovery(name)

                source = "full" if (self._signal_triggered or force_mode) else "base"
                return raw_value, source, latency

            except FutureTimeoutError:
                latency = time.perf_counter() - start
                logger.error(f"因子计算超时 ({self.COMPUTE_TIMEOUT_SEC}s): '{name}' "
                             "#RECOVERY: 检查数据源响应或增加超时阈值")
            except (StaleDataError, FactorComputationTimeout) as e:
                latency = time.perf_counter() - start
                logger.error(f"因子计算失败 [{type(e).__name__}]: '{name}': {e} "
                             "#RECOVERY: 检查数据源连接状态")
            except ValueError as e:
                latency = time.perf_counter() - start
                logger.error(f"因子返回值异常: '{name}': {e} #RECOVERY: 检查因子计算函数逻辑")
            except Exception as e:
                latency = time.perf_counter() - start
                logger.error(f"因子计算异常: '{name}': {e} #RECOVERY: 检查因子计算函数逻辑", exc_info=True)

            # 更新失败统计
            with self._lock:
                if name in self._factor_stats:
                    stats = self._factor_stats[name]
                    stats["failures"] += 1
                    stats["consecutive_failures"] += 1
                    stats["latencies"].append(latency)
                    if stats["consecutive_failures"] >= self.QUARANTINE_FAILURE_THRESHOLD:
                        self._quarantine[name] = time.time() + self.QUARANTINE_DURATION_SEC
                        stats["state"] = self.STATE_QUARANTINED
                        logger.warning(f"因子 '{name}' 连续失败 {stats['consecutive_failures']} 次，"
                                       f"已隔离 {self.QUARANTINE_DURATION_SEC} 秒")

            if attempt < self.COMPUTE_RETRY_COUNT:
                time.sleep(self.COMPUTE_RETRY_DELAY_SEC)

        logger.error(f"因子 '{name}' 所有重试均失败，使用降级值 {self.DEGRADED_MISSING_VALUE_FILL}")
        return self.DEGRADED_MISSING_VALUE_FILL, "degraded", time.perf_counter() - start

    def _check_latency_spike(self, name: str, latency: float) -> None:
        """检测因子计算延迟尖峰"""
        with self._lock:
            if name not in self._factor_stats:
                return
            stats = self._factor_stats[name]
            latencies = stats["latencies"]
            if len(latencies) >= self.MIN_SAMPLES_FOR_SPIKE_CHECK:
                recent = [x for x in list(latencies)[-self.MIN_SAMPLES_FOR_SPIKE_CHECK:] if np.isfinite(x)]
                if len(recent) >= self.MIN_SAMPLES_FOR_SPIKE_CHECK:
                    median = np.median(recent)
                    if median > 0 and latency > median * self.LATENCY_SPIKE_MULTIPLIER:
                        logger.warning(
                            f"因子 '{name}' 延迟尖峰: {latency*1e6:.0f}μs "
                            f"(基线中位: {median*1e6:.0f}μs, 样本数: {len(recent)})"
                        )

    def _purge_stale_cache(self) -> None:
        """清理基态缓存中已不在注册表的条目（需在锁内调用）"""
        stale_keys = [k for k in self._base_cache if k not in self._registry]
        for k in stale_keys:
            del self._base_cache[k]
        if stale_keys:
            logger.debug(f"清理 {len(stale_keys)} 条过期缓存条目")

    def _purge_quarantine_expired(self) -> None:
        """清理隔离区中已过期的条目（需在锁内调用）"""
        now = time.time()
        expired = [k for k, v in self._quarantine.items() if now >= v]
        for k in expired:
            del self._quarantine[k]
            if k in self._factor_stats:
                self._factor_stats[k]["state"] = self.STATE_ACTIVE
                logger.info(f"因子 '{k}' 隔离期满，已移出隔离区")
                self._notify_factor_recovery(k)

    def _notify_factor_recovery(self, name: str) -> None:
        """通知上游模块因子已恢复"""
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="factor_recovery",
                    factor_name=name,
                    message=f"因子 '{name}' 已从隔离状态恢复",
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning(f"因子恢复通知失败: {e}")

    def _get_data_source_timestamp(self) -> float:
        """获取当前数据源时间戳。
        生产环境中必须覆盖此方法以返回真实数据源的更新时间戳（如订单簿快照时间）。
        默认实现返回 time.time()，会导致增量评估退化为全量评估。
        """
        return time.time()
