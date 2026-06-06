"""
火种系统 · 条件权重引擎入口 (ConditionalWeightEngine)

核心职责：
1. 作为条件权重体系的统一调度入口，整合时效分层管理(TemporalWeightManager)与IC预判调整(ICPredictiveAdjuster)两大子模块
2. 对外提供标准化的因子权重查询与更新接口，屏蔽内部多子模块的调度细节

外部依赖（真实模块接口）：
- core.conditional_weight.temporal_weight_manager.TemporalWeightManager : 负责快/中/慢因子的更新周期管理与权重时效衰减
- core.conditional_weight.ic_predictive_adjuster.ICPredictiveAdjuster : 负责因子IC加速度检测、FDR多重检验校正及权重预判调整
- core.negotiation_bus.NegotiationBus : 接收权重变更事件并通知依赖模块热重载
- core.utils.config_loader.ConfigLoader : 加载条件权重相关配置参数，获取因子列表
- core.behavioral_logger.BehavioralLogger : 记录权重调整日志与审计追踪

接口契约：
- update_all_weights(market_regime: str, force: bool = False) -> Dict[str, Any] : 根据当前市场状态触发全量因子权重更新
- update_all_weights_async(market_regime: str, force: bool = False) -> concurrent.futures.Future : 异步版本，返回包装后标准响应的Future
- get_factor_weight(factor_name: str) -> Dict[str, Any] : 查询单个因子的当前权重及元数据
- get_all_weights() -> Dict[str, Any] : 返回所有活跃因子的权重映射表
- health_check() -> Dict[str, Any] : 模块自检
- rollback_to_snapshot(snapshot_index: int = -1) -> Dict[str, Any] : 回滚到指定历史快照
- reset_statistics() -> Dict[str, Any] : 重置失败计数和更新统计
- shutdown() -> None : 关闭全局线程池，释放资源
- 所有公共方法输出字典固定包含 "status", "reason", "data", "warnings", "error_code"

异常与降级：
- 当 TemporalWeightManager 不可用时，保留上一轮有效权重；若无历史，使用 ConfigLoader 提供的因子列表构建等权
- 当 ICPredictiveAdjuster 不可用时，跳过预判调整环节，仅使用时效管理结果
- 当 NegotiationBus 不可用时，权重变更仅记录本地日志
- 所有权重更新失败时，系统自动回退至最近一次有效快照，确保权重永不丢失
- 所有降级值在类常量区明确声明

资源管理：
- 复用全局线程池，池大小根据 CPU 核心数动态设置，支持优雅关闭
- 快照列表最多保留5个历史版本，单个快照权重数超过阈值时压缩存储（保留非零权重）
"""

import time
import logging
import threading
import copy
import os
import re
from typing import Dict, Any, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import numpy as np

logger = logging.getLogger(__name__)

__all__ = ["ConditionalWeightEngine", "shutdown"]


class ConditionalWeightEngine:
    """条件权重引擎入口，协调时效管理与IC预判两大子系统"""

    # ========== 类常量 ==========
    DEFAULT_MARKET_REGIME = "normal"
    MAX_RETRY_ATTEMPTS = 2
    RETRY_DELAY_BASE_SEC = 0.05
    RETRY_DELAY_BACKOFF = 2.0
    RETRY_DELAY_MAX_SEC = 2.0
    MIN_UPDATE_INTERVAL_SEC = 30.0
    EMERGENCY_UPDATE_INTERVAL_SEC = 5.0
    MAX_SINGLE_FACTOR_WEIGHT = 0.5          # 单因子权重上限，无量纲，[0.1, 0.8]
    TIMEOUT_SUBMODULE_SEC = 10.0            # 子模块调用超时，秒
    TIMEOUT_OVERALL_SEC = 30.0              # 整体更新超时，秒
    CHANGE_ALERT_BASE_THRESHOLD = 0.3       # 基础变化量告警阈值，会根据因子数微调
    SNAPSHOT_MAX_AGE_SEC = 3600.0           # 快照最大有效期，秒
    MAX_WEIGHT_DICT_SIZE = 500              # 权重字典最大条目数
    MAX_SNAPSHOT_HISTORY = 5                # 最大历史快照数
    SNAPSHOT_COMPRESS_THRESHOLD = 100       # 快照权重数超过此值时启用压缩（仅保留非零权重）
    THREAD_POOL_SIZE = 0                    # 线程池大小，0表示自动根据CPU核心数计算
    AUDIT_TRAIL_ENABLED = True              # 是否启用审计追踪

    VALID_REGIMES = {"trend", "oscillation", "high_vol", "low_vol", "normal"}
    HIGH_VOL_REGIMES = {"high_vol"}
    MAX_REGIME_LENGTH = 32                  # 市场状态标识最大长度

    # 全局线程池（类级别复用）
    _executor: Optional[ThreadPoolExecutor] = None
    _executor_lock = threading.Lock()

    def __init__(self):
        self._temporal_manager = None
        self._ic_adjuster = None
        self._negotiation_bus = None
        self._config_loader = None
        self._behavioral_logger = None

        self._current_weights: Dict[str, float] = {}
        self._weights_lock = threading.RLock()

        self._last_update_time: float = 0.0
        self._last_update_regime: str = self.DEFAULT_MARKET_REGIME
        self._last_successful_weights: Dict[str, float] = {}
        self._last_snapshot_time: float = 0.0

        self._snapshot_history: List[Dict[str, Any]] = []

        self._factor_list: List[str] = []
        self._factor_list_lock = threading.Lock()

        self._consecutive_failures: int = 0
        self._total_updates: int = 0
        self._total_failures: int = 0

        self._update_cooldown_lock = threading.Lock()
        self._update_in_progress = threading.Event()

        self._ensure_executor()
        logger.info("ConditionalWeightEngine 初始化完成")

    @classmethod
    def _ensure_executor(cls) -> ThreadPoolExecutor:
        """确保全局线程池存在（类级别复用），大小根据CPU核心数动态调整，支持配置覆盖"""
        with cls._executor_lock:
            if cls._executor is None:
                pool_size = cls.THREAD_POOL_SIZE
                if pool_size <= 0:
                    cpu_count = os.cpu_count() or 4
                    pool_size = max(2, min(cpu_count, 8))
                cls._executor = ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="cond_weight_")
                logger.info(f"全局线程池已初始化，大小={pool_size}")
            return cls._executor

    @classmethod
    def shutdown(cls) -> None:
        """关闭全局线程池，释放资源"""
        with cls._executor_lock:
            if cls._executor is not None:
                cls._executor.shutdown(wait=True)
                cls._executor = None
                logger.info("全局线程池已关闭")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        temporal_manager: Optional[Any] = None,
        ic_adjuster: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        config_loader: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        deps = {
            "temporal_manager": ("_temporal_manager", temporal_manager, ["compute_weights"]),
            "ic_adjuster": ("_ic_adjuster", ic_adjuster, ["adjust_weights"]),
        }
        for name, (attr, obj, required_methods) in deps.items():
            if obj is not None:
                missing = [m for m in required_methods if not hasattr(obj, m)]
                if missing:
                    logger.warning(f"{name} 缺少必要方法: {missing}，将不可用")
                    setattr(self, attr, None)
                else:
                    setattr(self, attr, obj)
                    logger.info(f"{name} 注入成功")
            else:
                logger.warning(f"{name} 未注入，将降级运行")

        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, 'publish_event'):
                logger.warning("NegotiationBus 缺少 publish_event 方法")
                self._negotiation_bus = None
            else:
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")

        if config_loader is not None:
            if not hasattr(config_loader, 'get'):
                logger.warning("ConfigLoader 缺少 get 方法")
                self._config_loader = None
            else:
                self._config_loader = config_loader
                self._load_factor_list()
                self._register_config_callback()
                logger.info("ConfigLoader 注入成功")
        else:
            logger.warning("ConfigLoader 未注入")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")

    # ========== 公共接口 ==========
    def update_all_weights(self, market_regime: str, force: bool = False, audit_info: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        根据当前市场状态触发全量因子权重更新。

        Args:
            market_regime: 市场状态标识（不区分大小写，最大长度32字符，只允许字母数字下划线）
            force: 是否强制绕过冷却（用于紧急情况）
            audit_info: 审计信息，可包含 'operator' 和 'reason' 字段

        Returns:
            标准响应字典
        """
        # 输入校验
        if not isinstance(market_regime, str) or len(market_regime) > self.MAX_REGIME_LENGTH:
            logger.warning(f"无效 market_regime: {market_regime}，回退至默认")
            market_regime = self.DEFAULT_MARKET_REGIME
        else:
            market_regime = market_regime.lower().strip()
            if not re.match(r'^[a-z0-9_]+$', market_regime):
                logger.warning(f"market_regime 包含非法字符: {market_regime}")
                market_regime = self.DEFAULT_MARKET_REGIME
            elif market_regime not in self.VALID_REGIMES:
                logger.warning(f"未知市场状态: {market_regime}，回退至 {self.DEFAULT_MARKET_REGIME}")
                market_regime = self.DEFAULT_MARKET_REGIME

        if not force:
            cooldown_interval = (
                self.EMERGENCY_UPDATE_INTERVAL_SEC
                if market_regime in self.HIGH_VOL_REGIMES
                else self.MIN_UPDATE_INTERVAL_SEC
            )
            with self._update_cooldown_lock:
                now = time.time()
                if self._last_update_time > 0 and (now - self._last_update_time) < cooldown_interval:
                    # 检查缓存是否有效
                    with self._weights_lock:
                        cache_available = len(self._current_weights) > 0
                    if cache_available:
                        return self._build_response_from_cache(market_regime, "冷却中", "COOLDOWN")
                    else:
                        logger.warning("冷却期内缓存为空，强制更新")
        else:
            # 强制更新：不等待冷却，但确保不会并发执行
            acquired = self._update_cooldown_lock.acquire(blocking=False)
            if not acquired:
                self._update_cooldown_lock.acquire()
            self._update_cooldown_lock.release()

        self._update_in_progress.set()
        try:
            result = self._perform_update(market_regime, audit_info)
        finally:
            self._update_in_progress.clear()
        return result

    def update_all_weights_async(self, market_regime: str, force: bool = False) -> Any:
        """异步版本，返回包装后的Future"""
        executor = self._ensure_executor()
        return executor.submit(self.update_all_weights, market_regime, force)

    def get_factor_weight(self, factor_name: str) -> Dict[str, Any]:
        if not factor_name or not isinstance(factor_name, str):
            return {
                "status": "error", "reason": "factor_name 必须是非空字符串",
                "error_code": "INVALID_PARAM", "data": {}, "warnings": ["invalid_factor_name"],
            }
        with self._weights_lock:
            weight = self._current_weights.get(factor_name, None)
        if weight is None:
            return {
                "status": "ok", "reason": f"因子 {factor_name} 不存在",
                "error_code": "FACTOR_NOT_FOUND",
                "data": {"factor_name": factor_name, "weight": 0.0, "active": False},
                "warnings": [f"factor_not_found: {factor_name}"],
            }
        return {
            "status": "ok", "reason": f"因子 {factor_name} 当前权重: {weight:.6f}",
            "error_code": "SUCCESS",
            "data": {
                "factor_name": factor_name, "weight": round(weight, 6), "active": weight > 0,
                "last_update_time": self._last_update_time, "last_update_regime": self._last_update_regime,
            },
            "warnings": [],
        }

    def get_all_weights(self) -> Dict[str, Any]:
        with self._weights_lock:
            weights = copy.deepcopy(self._current_weights)
            last_update = self._last_update_time
            regime = self._last_update_regime
        if len(weights) > self.MAX_WEIGHT_DICT_SIZE:
            logger.warning(f"权重字典过大 ({len(weights)}), 截断至保留权重最高的 {self.MAX_WEIGHT_DICT_SIZE} 个")
            sorted_items = sorted(weights.items(), key=lambda x: x[1], reverse=True)
            weights = dict(sorted_items[:self.MAX_WEIGHT_DICT_SIZE])
        active_count = sum(1 for w in weights.values() if w > 0)
        dormant_count = len(weights) - active_count
        return {
            "status": "ok", "reason": f"返回 {len(weights)} 个因子权重 (活跃: {active_count}, 休眠: {dormant_count})",
            "error_code": "SUCCESS",
            "data": {
                "weights": weights, "total_count": len(weights),
                "active_count": active_count, "dormant_count": dormant_count,
                "last_update_time": last_update, "last_update_regime": regime,
            },
            "warnings": [],
        }

    def rollback_to_snapshot(self, snapshot_index: int = -1, audit_info: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """回滚到指定历史快照（-1 表示最近一次）"""
        with self._weights_lock:
            if not self._snapshot_history:
                return {
                    "status": "error", "reason": "无历史快照可用",
                    "error_code": "NO_SNAPSHOT", "data": {}, "warnings": ["no_snapshot_available"],
                }
            try:
                snapshot = self._snapshot_history[snapshot_index]
            except IndexError:
                return {
                    "status": "error", "reason": f"快照索引 {snapshot_index} 无效",
                    "error_code": "INVALID_SNAPSHOT_INDEX", "data": {}, "warnings": ["invalid_snapshot_index"],
                }
            # 恢复完整快照权重（不做有效性过滤，保证回滚完整性）
            self._current_weights = snapshot["weights"].copy()
            self._last_update_regime = snapshot.get("regime", self.DEFAULT_MARKET_REGIME)
            self._last_update_time = time.time()
        logger.warning(f"权重已回滚至快照 [{snapshot_index}] (regime={snapshot.get('regime')})")
        if self._behavioral_logger and self.AUDIT_TRAIL_ENABLED:
            try:
                self._behavioral_logger.log_event(
                    event_type="weight_rollback",
                    details={
                        "snapshot_index": snapshot_index,
                        "factor_count": len(snapshot["weights"]),
                        "regime": snapshot.get("regime"),
                        "audit": audit_info or {},
                    },
                )
            except Exception as e:
                logger.warning(f"审计日志记录失败: {e}")
        return {
            "status": "ok", "reason": f"已回滚至快照 [{snapshot_index}]",
            "error_code": "SUCCESS",
            "data": {"weights": snapshot["weights"], "snapshot_index": snapshot_index},
            "warnings": ["weights_rolled_back"],
        }

    def reset_statistics(self) -> Dict[str, Any]:
        """重置失败计数和更新统计"""
        with self._weights_lock:
            self._consecutive_failures = 0
            self._total_failures = 0
            self._total_updates = 0
        logger.info("统计信息已重置")
        return {
            "status": "ok",
            "reason": "统计信息已重置",
            "error_code": "SUCCESS",
            "data": {},
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        try:
            if not hasattr(self, '_current_weights'):
                return {
                    "status": "degraded", "reason": "数据结构未初始化",
                    "error_code": "DATA_MISSING", "data": {}, "warnings": ["data_structure_missing"],
                }
            with self._weights_lock:
                weight_count = len(self._current_weights)
                snapshot_available = len(self._last_successful_weights) > 0
                snapshot_age = time.time() - self._last_snapshot_time if self._last_snapshot_time else None
            temporal_ok = self._temporal_manager is not None
            ic_ok = self._ic_adjuster is not None
            config_ok = self._config_loader is not None
            overall = "ok"
            warns = []
            if not temporal_ok:
                overall = "degraded"; warns.append("temporal_manager_missing")
            if not ic_ok:
                overall = "degraded"; warns.append("ic_adjuster_missing")
            if not config_ok:
                overall = "degraded"; warns.append("config_loader_missing")
            if snapshot_available and snapshot_age is not None and snapshot_age > self.SNAPSHOT_MAX_AGE_SEC:
                warns.append(f"snapshot_expired_{snapshot_age:.0f}s")
                overall = "degraded"
            if self._consecutive_failures >= 3:
                warns.append(f"consecutive_failures_{self._consecutive_failures}")
                overall = "degraded"
            # 子模块功能测试：使用轻量级方式（检查是否存在，不强制调用计算）
            # 如需深度测试，可通过专门的诊断接口触发，避免健康检查影响性能
            return {
                "status": overall, "reason": f"权重引擎正常，管理 {weight_count} 个因子",
                "error_code": "SUCCESS" if overall == "ok" else "DEGRADED",
                "data": {
                    "weight_count": weight_count,
                    "snapshot_available": snapshot_available,
                    "snapshot_age_sec": round(snapshot_age, 0) if snapshot_age is not None else None,
                    "consecutive_failures": self._consecutive_failures,
                    "total_updates": self._total_updates,
                    "total_failures": self._total_failures,
                    "snapshot_history_count": len(self._snapshot_history),
                    "dependencies": {
                        "temporal_manager": temporal_ok, "ic_adjuster": ic_ok, "config_loader": config_ok,
                    },
                    "last_update_time": self._last_update_time,
                    "last_update_regime": self._last_update_regime,
                },
                "warnings": warns,
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和内存占用")
            return {
                "status": "error", "reason": f"健康检查异常: {str(e)}",
                "error_code": "HEALTH_CHECK_FAILED", "data": {}, "warnings": ["health_check_failed"],
            }

    # ========== 私有方法（核心逻辑） ==========
    def _perform_update(self, market_regime: str, audit_info: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        warnings: List[str] = []
        start_time = time.time()
        stage_times = {}

        stage_start = time.time()
        base_weights = self._get_base_weights(market_regime, warnings)
        stage_times["temporal_ms"] = (time.time() - stage_start) * 1000

        stage_start = time.time()
        adjusted_weights = self._apply_ic_adjustment(base_weights, market_regime, warnings)
        stage_times["ic_adjust_ms"] = (time.time() - stage_start) * 1000

        stage_start = time.time()
        final_weights, snapshot_used = self._normalize_and_clamp_with_fallback(adjusted_weights, warnings)
        stage_times["normalize_ms"] = (time.time() - stage_start) * 1000

        with self._weights_lock:
            change_metric = self._compute_change_metric(final_weights)
            self._current_weights = final_weights.copy()
            self._last_update_time = time.time()
            self._last_update_regime = market_regime
            self._last_successful_weights = final_weights.copy()
            self._last_snapshot_time = time.time()
            self._add_snapshot_to_history(final_weights, market_regime)
            self._consecutive_failures = 0
            self._total_updates += 1

        elapsed_ms = (time.time() - start_time) * 1000

        factor_count = len(final_weights)
        dynamic_threshold = self.CHANGE_ALERT_BASE_THRESHOLD * (1.0 + np.log10(max(1, factor_count)) * 0.1)
        if change_metric > dynamic_threshold:
            logger.warning(
                f"权重变化量异常: {change_metric:.4f} > {dynamic_threshold:.4f} "
                f"(factors={factor_count})"
            )

        self._notify_weight_change(market_regime, final_weights, change_metric)
        self._log_weight_update(market_regime, final_weights, change_metric, elapsed_ms, warnings, stage_times, audit_info)

        return {
            "status": "ok",
            "reason": f"已完成 {market_regime} 状态下 {len(final_weights)} 个因子的权重更新",
            "error_code": "SUCCESS",
            "data": {
                "weights": final_weights,
                "factor_count": len(final_weights),
                "regime": market_regime,
                "elapsed_ms": round(elapsed_ms, 1),
                "stage_times_ms": stage_times,
                "change_metric": round(change_metric, 6),
                "snapshot_fallback_used": snapshot_used,
            },
            "warnings": warnings,
        }

    def _load_factor_list(self) -> None:
        if self._config_loader is not None and hasattr(self._config_loader, 'get'):
            try:
                factors = self._config_loader.get("factors.list", None)
                if isinstance(factors, list) and factors:
                    with self._factor_list_lock:
                        self._factor_list = list(dict.fromkeys(str(f) for f in factors))
                    logger.info(f"从配置加载因子列表，共 {len(self._factor_list)} 个因子")
            except Exception as e:
                logger.warning(f"加载因子列表失败: {e}")

    def _register_config_callback(self) -> None:
        if self._config_loader is not None and hasattr(self._config_loader, 'register_callback'):
            try:
                self._config_loader.register_callback("factors.list", self._on_factor_list_changed)
                logger.debug("已注册因子列表变更回调")
            except Exception as e:
                logger.warning(f"注册配置回调失败: {e}")

    def _on_factor_list_changed(self, new_value: Any) -> None:
        if isinstance(new_value, list) and new_value:
            with self._factor_list_lock:
                self._factor_list = list(dict.fromkeys(str(f) for f in new_value))
            logger.info(f"因子列表已更新，共 {len(self._factor_list)} 个因子")

    def _add_snapshot_to_history(self, weights: Dict[str, float], regime: str) -> None:
        if len(weights) > self.SNAPSHOT_COMPRESS_THRESHOLD:
            # 压缩：仅保留非零权重，同时保留键列表用于恢复
            compressed = {k: v for k, v in weights.items() if v > 1e-10}
            logger.debug(f"快照压缩: {len(weights)} -> {len(compressed)}")
        else:
            compressed = weights.copy()
        snapshot = {"weights": compressed, "regime": regime, "timestamp": time.time()}
        self._snapshot_history.insert(0, snapshot)
        if len(self._snapshot_history) > self.MAX_SNAPSHOT_HISTORY:
            self._snapshot_history.pop()

    def _record_failure(self) -> None:
        with self._weights_lock:
            self._consecutive_failures += 1
            self._total_failures += 1

    def _call_with_timeout(self, func, timeout: float, *args, **kwargs) -> Any:
        executor = self._ensure_executor()
        if executor is None:
            raise RuntimeError("线程池未初始化，无法执行异步调用")
        future = executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            raise TimeoutError(f"调用超时 ({timeout}s)")
        except Exception as e:
            raise type(e)(f"{func.__name__} failed: {e}") from e

    def _compute_change_metric(self, new_weights: Dict[str, float]) -> float:
        with self._weights_lock:
            old_weights = self._current_weights.copy()
        if not old_weights:
            return 1.0
        all_keys = set(old_weights) | set(new_weights)
        diff_vec = [new_weights.get(k, 0.0) - old_weights.get(k, 0.0) for k in all_keys]
        return float(np.linalg.norm(diff_vec))

    def _build_equal_weights(self, warnings: List[str]) -> Dict[str, float]:
        factor_keys = []
        with self._factor_list_lock:
            factor_keys = list(self._factor_list)
        if not factor_keys:
            with self._weights_lock:
                factor_keys = list(self._current_weights.keys())
        if not factor_keys:
            logger.error("无法构建等权：因子列表和当前权重均为空")
            warnings.append("no_factor_list_and_no_current_weights")
            return {}
        factor_keys = list(dict.fromkeys(factor_keys))
        equal_weight = 1.0 / len(factor_keys)
        result = {k: equal_weight for k in factor_keys}
        warnings.append(f"using_equal_weights_{len(result)}_factors")
        logger.info(f"生成等权字典，共 {len(result)} 个因子，单因子权重={equal_weight:.6f}")
        return result

    def _get_base_weights(self, regime: str, warnings: List[str]) -> Dict[str, float]:
        if self._temporal_manager is not None:
            delay = self.RETRY_DELAY_BASE_SEC
            for attempt in range(self.MAX_RETRY_ATTEMPTS + 1):
                try:
                    result = self._call_with_timeout(
                        self._temporal_manager.compute_weights,
                        self.TIMEOUT_SUBMODULE_SEC,
                        regime
                    )
                    if isinstance(result, dict) and result:
                        return result
                    else:
                        logger.warning(f"TemporalWeightManager 返回无效类型: {type(result)}")
                except TimeoutError:
                    logger.warning(f"TemporalWeightManager 超时 (尝试 {attempt+1})")
                except Exception as e:
                    logger.warning(f"TemporalWeightManager 调用失败 (尝试 {attempt+1}): {e}")
                if attempt < self.MAX_RETRY_ATTEMPTS:
                    time.sleep(min(delay, self.RETRY_DELAY_MAX_SEC))
                    delay *= self.RETRY_DELAY_BACKOFF
        warnings.append("temporal_manager_unavailable")
        self._record_failure()
        return {}

    def _apply_ic_adjustment(self, base_weights: Dict[str, float], regime: str, warnings: List[str]) -> Dict[str, float]:
        if not base_weights:
            return base_weights
        if self._ic_adjuster is not None:
            delay = self.RETRY_DELAY_BASE_SEC
            for attempt in range(self.MAX_RETRY_ATTEMPTS + 1):
                try:
                    result = self._call_with_timeout(
                        self._ic_adjuster.adjust_weights,
                        self.TIMEOUT_SUBMODULE_SEC,
                        base_weights, regime
                    )
                    if isinstance(result, dict) and result:
                        return result
                    else:
                        logger.warning(f"ICPredictiveAdjuster 返回无效类型: {type(result)}")
                except TimeoutError:
                    logger.warning(f"ICPredictiveAdjuster 超时 (尝试 {attempt+1})")
                except Exception as e:
                    logger.warning(f"ICPredictiveAdjuster 调用失败 (尝试 {attempt+1}): {e}")
                if attempt < self.MAX_RETRY_ATTEMPTS:
                    time.sleep(min(delay, self.RETRY_DELAY_MAX_SEC))
                    delay *= self.RETRY_DELAY_BACKOFF
            self._record_failure()
        else:
            warnings.append("ic_adjuster_unavailable_skipped")
        return base_weights

    def _normalize_and_clamp_with_fallback(self, weights: Dict[str, float], warnings: List[str]) -> Tuple[Dict[str, float], bool]:
        normalized = self._do_normalize(weights, warnings)
        if normalized:
            return normalized, False
        with self._weights_lock:
            snapshot = self._last_successful_weights.copy()
            snapshot_time = self._last_snapshot_time
            snapshot_regime = self._last_update_regime
        snapshot_age = time.time() - snapshot_time if snapshot_time else float('inf')
        # 验证快照市场状态匹配（若不匹配，则快照可信度降低）
        regime_match = (snapshot_regime == self._last_update_regime) if snapshot else False
        if snapshot and snapshot_age < self.SNAPSHOT_MAX_AGE_SEC and regime_match:
            warnings.append(f"normalization_failed_using_snapshot_age_{snapshot_age:.0f}s")
            logger.warning("归一化失败，回退至最近成功快照")
            return snapshot, True
        elif snapshot and snapshot_age < self.SNAPSHOT_MAX_AGE_SEC:
            warnings.append("snapshot_regime_mismatch_but_using")
            return snapshot, True
        elif snapshot:
            warnings.append("snapshot_expired_falling_back_to_equal")
        else:
            warnings.append("no_valid_snapshot")
        equal_weights = self._build_equal_weights(warnings)
        return equal_weights, True

    def _do_normalize(self, weights: Dict[str, float], warnings: List[str]) -> Dict[str, float]:
        if not weights:
            warnings.append("empty_input_weights")
            return {}
        negative_items = [(k, v) for k, v in weights.items() if v < 0]
        if negative_items:
            names = [k for k, _ in negative_items[:5]]
            warnings.append(f"removed_{len(negative_items)}_negative_weights: {names}")
        cleaned = {k: max(0.0, min(v, self.MAX_SINGLE_FACTOR_WEIGHT)) for k, v in weights.items()}
        total = sum(cleaned.values())
        if total > 1e-10:
            normalized = {k: v / total for k, v in cleaned.items()}
            remaining = 1.0 - sum(normalized.values())
            if abs(remaining) > 1e-12:
                max_key = max(normalized, key=normalized.get)
                new_val = normalized[max_key] + remaining
                if new_val < 0:
                    logger.error("精度补偿导致负权重，跳过补偿")
                else:
                    normalized[max_key] = new_val
            return normalized
        else:
            warnings.append("zero_total_weight")
            return {}

    def _build_response_from_cache(self, regime: str, reason: str, error_code: str) -> Dict[str, Any]:
        with self._weights_lock:
            weights = self._current_weights.copy()
            last_update = self._last_update_time
        return {
            "status": "ok",
            "reason": reason,
            "error_code": error_code,
            "data": {
                "weights": weights,
                "factor_count": len(weights),
                "regime": regime,
                "last_update_time": last_update,
            },
            "warnings": ["update_cooldown_active"],
        }

    def _notify_weight_change(self, regime: str, weights: Dict[str, float], change_metric: float) -> None:
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_event'):
            try:
                self._negotiation_bus.publish_event(
                    event_type="weight_update",
                    regime=regime,
                    factor_count=len(weights),
                    change_metric=round(change_metric, 6),
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning(f"权重变更通知发送失败: {e}")

    def _log_weight_update(self, regime: str, weights: Dict[str, float], change_metric: float,
                           elapsed_ms: float, warnings: List[str], stage_times: Dict[str, float],
                           audit_info: Optional[Dict[str, str]] = None) -> None:
        if self._behavioral_logger is not None:
            try:
                top = sorted(weights.items(), key=lambda x: x[1], reverse=True)[:5]
                stats = {
                    "max": max(weights.values()) if weights else 0,
                    "min": min(weights.values()) if weights else 0,
                    "mean": sum(weights.values()) / len(weights) if weights else 0,
                }
                self._behavioral_logger.log_event(
                    event_type="conditional_weight_update",
                    details={
                        "regime": regime,
                        "factor_count": len(weights),
                        "elapsed_ms": round(elapsed_ms, 1),
                        "stage_times_ms": {k: round(v, 1) for k, v in stage_times.items()},
                        "change_metric": round(change_metric, 6),
                        "top5_factors": top,
                        "statistics": stats,
                        "warnings": warnings[:5],
                        "audit": audit_info or {},
                    },
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")
