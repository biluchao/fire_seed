"""
火种系统 · 因子时效分层管理器 (TemporalWeightManager)

核心职责：
1. 根据因子的时效特性（快/中/慢/休眠），管理其IC更新周期和权重衰减节奏
2. 追踪每个因子的生命周期阶段（成长/成熟/衰退/休眠/复活），自动触发阶段迁移

外部依赖（真实模块接口）：
- core.conditional_weight.ic_predictive_adjuster.ICPredictiveAdjuster (需实现 get_latest_ic 方法，且必须线程安全)
- core.behavioral_logger.BehavioralLogger : 记录因子状态变更日志
- numpy : 数值计算库（必需依赖）
- config.weights.yaml : 动态权重快照配置

接口契约：
- update_factor_ic(factor_name: str, ic_value: float, tier: str) -> Dict[str, Any] : 更新指定因子在指定时效层级的IC值
- get_factor_weight(factor_name: str) -> Dict[str, Any] : 返回指定因子的当前权重（含阶段信息）
- get_active_factors() -> Dict[str, Any] : 返回所有活跃因子的权重列表（按权重降序排列）
- migrate_factor_stage(factor_name: str) -> Dict[str, Any] : 手动触发因子生命周期阶段迁移
- get_stage_distribution() -> Dict[str, Any] : 获取所有因子的生命周期阶段分布统计
- get_metrics() -> Dict[str, Any] : 获取运维监控指标
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 ICPredictiveAdjuster 不可用或超时时，使用因子滑动窗口IC均值作为回退
- 当 IC 值包含 NaN/Inf 时，自动过滤并记录告警
- 当 BehavioralLogger 未注入时，阶段变更日志降级为 WARNING 级别
- 当外部依赖调用线程池满时，使用滑动窗口IC（不阻塞主流程）
- 所有外部依赖调用均设置超时，超时后立即回退

资源管理：
- 生命周期阶段持续时间基于真实时间戳计算
- `_stage_history` 保留最近 10 条记录，防止内存无限增长
- `_decay_weights_cache` 最大 200 条，超出后 LRU 淘汰
- 滑动窗口容量由 `DEFAULT_IC_WINDOW_SIZE` 限制
- 所有共享状态受 `threading.RLock` 保护
- 外部依赖调用使用线程池 + 信号量，超时自动丢弃
"""

import time
import logging
import threading
import math
import re
from typing import Dict, Any, List, Optional, Tuple
from collections import deque, OrderedDict
from enum import Enum

import numpy as np

logger = logging.getLogger(__name__)


class FactorLifecycleStage(Enum):
    """因子生命周期阶段枚举"""
    GROWTH = "growth"
    MATURE = "mature"
    DECLINE = "decline"
    DORMANT = "dormant"
    REVIVAL = "revival"
    RETIRED = "retired"


class TemporalWeightManager:
    """因子时效分层管理器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_IC_WINDOW_SIZE = 30           # IC滑动窗口最大样本数，无量纲，[10, 90]
    DEFAULT_MIN_SAMPLES_FOR_EVAL = 10     # 最少样本数用于评估，无量纲，[5, 30]
    DEFAULT_GROWTH_MAX_SECONDS = 2592000  # 成长期最长时长（30天），秒，[1209600, 5184000]
    DEFAULT_MATURE_MIN_SECONDS = 2592000  # 成熟期最少稳定时长（30天），秒
    DEFAULT_DECLINE_IC_THRESHOLD = 0.01   # 衰退期IC阈值，无量纲，[0.001, 0.05]
    DEFAULT_DECLINE_CONSECUTIVE_COUNT = 3 # 衰退期需连续低于阈值次数，无量纲，[2, 10]
    DEFAULT_DECLINE_RECOVERY_CONSECUTIVE = 2 # 衰退期需连续高于恢复阈值次数，无量纲，[1, 5]
    DEFAULT_DORMANT_IC_THRESHOLD = 0.005  # 休眠期IC最低阈值，无量纲，[0.001, 0.02]
    DEFAULT_REVIVAL_TEST_INTERVAL_SEC = 1209600  # 休眠因子复活检测间隔（14天），秒
    DEFAULT_RETIRED_NO_REVIVAL_SEC = 15552000    # 退役前无复活时长（180天），秒
    DEFAULT_NEUTRAL_WEIGHT = 0.1          # 中性权重（回退值），无量纲，[0.01, 0.5]
    DEFAULT_IC_DECAY_HALFLIFE_DAYS = 15   # IC指数衰减半衰期，天，[5, 60]
    MAX_STAGE_HISTORY = 10                 # 每个因子保留的迁移历史记录数
    MAX_DECAY_CACHE_SIZE = 200             # 衰减权重缓存最大容量
    MAX_INACTIVE_FACTORS_IN_RESPONSE = 200 # API返回的非活跃因子最大数量

    # 各生命周期阶段的权重上限
    STAGE_WEIGHT_CAPS = {
        FactorLifecycleStage.GROWTH: 0.5,
        FactorLifecycleStage.MATURE: 1.0,
        FactorLifecycleStage.DECLINE: 0.4,
        FactorLifecycleStage.DORMANT: 0.0,
        FactorLifecycleStage.REVIVAL: 0.5,
        FactorLifecycleStage.RETIRED: 0.0,
    }

    # 各生命周期阶段的衰减系数
    STAGE_DECAY_FACTORS = {
        FactorLifecycleStage.GROWTH: 1.0,
        FactorLifecycleStage.MATURE: 1.0,
        FactorLifecycleStage.DECLINE: 0.9,
        FactorLifecycleStage.DORMANT: 0.0,
        FactorLifecycleStage.REVIVAL: 0.8,
        FactorLifecycleStage.RETIRED: 0.0,
    }

    DECLINE_RECOVERY_MULTIPLIER = 1.5      # 衰退恢复的IC乘数，[1.2, 2.0]
    FACTOR_TIERS = ["fast", "medium", "slow"]
    EXTERNAL_CALL_TIMEOUT_SEC = 1.0        # 外部调用超时（1秒，避免阻塞主流程）
    ACTIVE_WEIGHT_THRESHOLD = 0.001
    MAX_FACTOR_NAME_LENGTH = 128
    MAX_WORKER_THREADS = 8

    # 各时效层级的权重（用于混合IC时的加权平均）
    TIER_WEIGHTS = {
        "fast": 0.5,
        "medium": 0.3,
        "slow": 0.2,
    }

    def __init__(self):
        self._ic_history: Dict[str, Dict[str, deque]] = {}
        self._lifecycle_stage: Dict[str, FactorLifecycleStage] = {}
        self._last_update: Dict[str, float] = {}
        self._stage_start_time: Dict[str, float] = {}
        self._stage_history: Dict[str, deque] = {}
        self._decline_consecutive_count: Dict[str, int] = {}
        self._decline_recovery_count: Dict[str, int] = {}
        self._ic_adjuster = None
        self._behavioral_logger = None
        self._lock = threading.RLock()
        self._worker_semaphore = threading.BoundedSemaphore(self.MAX_WORKER_THREADS)
        self._total_ic_updates: int = 0
        self._total_stage_migrations: int = 0
        self._total_external_call_drops: int = 0
        self._decay_weights_cache: OrderedDict = OrderedDict()

        logger.info(
            "TemporalWeightManager 初始化完成，支持 %d 种因子时效层级",
            len(self.FACTOR_TIERS)
        )

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        ic_adjuster: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        if ic_adjuster is not None:
            if not hasattr(ic_adjuster, 'get_latest_ic'):
                logger.warning("ICPredictiveAdjuster 缺少 get_latest_ic 方法，IC数据源不可用")
                self._ic_adjuster = None
            else:
                self._ic_adjuster = ic_adjuster
                logger.info("ICPredictiveAdjuster 注入成功")
        else:
            logger.warning("ICPredictiveAdjuster 未注入，IC数据源降级为内部缓存")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，阶段变更日志降级为标准 WARNING logger")

    # ========== 公共接口 ==========
    def update_factor_ic(
        self, factor_name: str, ic_value: float, tier: str = "medium"
    ) -> Dict[str, Any]:
        # 参数校验
        if not factor_name or not isinstance(factor_name, str) or factor_name.strip() == "":
            logger.warning("无效因子名称: %s", factor_name)
            return self._error_response("invalid_factor_name", f"无效因子名称: '{factor_name}'")
        factor_name = factor_name.strip()
        if len(factor_name) > self.MAX_FACTOR_NAME_LENGTH:
            logger.warning("因子名称过长: %d > %d", len(factor_name), self.MAX_FACTOR_NAME_LENGTH)
            return self._error_response("factor_name_too_long", f"因子名称过长")
        if not re.match(r'^[a-zA-Z0-9_\.]+$', factor_name):
            logger.warning("因子名称包含非法字符: %s", factor_name)
            return self._error_response("invalid_characters", f"因子名称包含非法字符: {factor_name}")

        if ic_value is None or math.isnan(ic_value) or math.isinf(ic_value):
            logger.warning("因子 %s 收到无效IC值: %s，已丢弃", factor_name, ic_value)
            self._log_anomaly(factor_name, f"无效IC值: {ic_value}")
            return self._error_response("invalid_ic_value", f"无效IC值: {ic_value}")

        ic_value = max(-1.0, min(1.0, float(ic_value)))

        if tier not in self.FACTOR_TIERS:
            logger.warning("未知时效层级: %s，已拒绝写入。有效值: %s", tier, self.FACTOR_TIERS)
            return self._error_response("invalid_tier", f"未知时效层级: {tier}")

        now = time.time()
        stage_changed = False
        old_stage = None
        new_stage = None

        with self._lock:
            # 初始化因子数据结构（仅分配当前写入层级）
            if factor_name not in self._ic_history:
                self._ic_history[factor_name] = {
                    t: deque(maxlen=self.DEFAULT_IC_WINDOW_SIZE) for t in self.FACTOR_TIERS
                }
                self._lifecycle_stage[factor_name] = FactorLifecycleStage.GROWTH
                self._stage_start_time[factor_name] = now
                self._stage_history[factor_name] = deque(maxlen=self.MAX_STAGE_HISTORY)
                self._decline_consecutive_count[factor_name] = 0
                self._decline_recovery_count[factor_name] = 0
                logger.info("新因子注册: %s，进入成长期", factor_name)

            self._ic_history[factor_name][tier].append(ic_value)
            self._last_update[factor_name] = now
            self._total_ic_updates += 1

            if ic_value < self.DEFAULT_DECLINE_IC_THRESHOLD:
                self._decline_consecutive_count[factor_name] += 1
            else:
                self._decline_consecutive_count[factor_name] = 0

            if ic_value > self.DEFAULT_DECLINE_IC_THRESHOLD * self.DECLINE_RECOVERY_MULTIPLIER:
                self._decline_recovery_count[factor_name] += 1
            else:
                self._decline_recovery_count[factor_name] = 0

            old_stage = self._lifecycle_stage[factor_name]
            new_stage = self._evaluate_lifecycle_migration(factor_name, ic_value, now)
            if new_stage != old_stage:
                self._lifecycle_stage[factor_name] = new_stage
                self._stage_start_time[factor_name] = now
                self._stage_history[factor_name].append({
                    "timestamp": now,
                    "old_stage": old_stage.value,
                    "new_stage": new_stage.value,
                    "ic_value": ic_value,
                })
                self._decline_consecutive_count[factor_name] = 0
                self._decline_recovery_count[factor_name] = 0
                self._total_stage_migrations += 1
                stage_changed = True
                logger.info(
                    "因子 %s 生命周期迁移: %s -> %s (IC=%.4f)",
                    factor_name, old_stage.value, new_stage.value, ic_value
                )
                self._log_stage_change(factor_name, old_stage.value, new_stage.value, ic_value, now)

        return {
            "status": "ok",
            "reason": f"因子 {factor_name} IC已更新: {ic_value:.4f} (层级: {tier})",
            "data": {
                "factor_name": factor_name,
                "ic_value": round(ic_value, 4),
                "tier": tier,
                "lifecycle_stage": (new_stage or old_stage).value,
                "stage_changed": stage_changed,
                "timestamp": now,
            },
            "warnings": [],
        }

    def get_factor_weight(self, factor_name: str) -> Dict[str, Any]:
        with self._lock:
            if factor_name not in self._lifecycle_stage:
                logger.warning("因子 %s 未注册", factor_name)
                return self._error_response("unknown_factor", f"因子 {factor_name} 未注册")
            stage = self._lifecycle_stage[factor_name]

        if stage in (FactorLifecycleStage.DORMANT, FactorLifecycleStage.RETIRED):
            return {
                "status": "ok",
                "reason": f"因子 {factor_name} 处于 {stage.value} 期，权重为0",
                "data": {
                    "factor_name": factor_name,
                    "current_weight": 0.0,
                    "lifecycle_stage": stage.value,
                    "weight_cap": self.STAGE_WEIGHT_CAPS.get(stage, 0.0),
                },
                "warnings": [],
            }

        current_ic = self._get_latest_effective_ic(factor_name)

        with self._lock:
            # 再次确认阶段未变（可能已被迁移）
            stage = self._lifecycle_stage.get(factor_name, stage)
            if stage in (FactorLifecycleStage.DORMANT, FactorLifecycleStage.RETIRED):
                return {
                    "status": "ok",
                    "reason": f"因子 {factor_name} 刚进入 {stage.value} 期，权重为0",
                    "data": {"factor_name": factor_name, "current_weight": 0.0},
                    "warnings": [],
                }
            if current_ic is None or math.isnan(current_ic):
                current_ic = 0.0
            weight_cap = self.STAGE_WEIGHT_CAPS.get(stage, 0.5)
            decay_factor = self.STAGE_DECAY_FACTORS.get(stage, 1.0)
            raw_weight = max(0.0, current_ic * decay_factor)
            effective_weight = min(raw_weight, weight_cap)

        return {
            "status": "ok",
            "reason": f"因子 {factor_name} 权重: {effective_weight:.4f} (阶段: {stage.value})",
            "data": {
                "factor_name": factor_name,
                "current_weight": round(effective_weight, 4),
                "lifecycle_stage": stage.value,
                "current_ic": round(current_ic, 4),
            },
            "warnings": [],
        }

    def get_active_factors(self) -> Dict[str, Any]:
        active_weights = {}
        inactive_factors = []
        with self._lock:
            all_factors = list(self._lifecycle_stage.keys())
            stage_map = {f: self._lifecycle_stage[f] for f in all_factors}

        for factor_name in all_factors:
            stage = stage_map[factor_name]
            if stage in (FactorLifecycleStage.DORMANT, FactorLifecycleStage.RETIRED):
                inactive_factors.append(factor_name)
                continue
            current_ic = self._get_latest_effective_ic(factor_name)
            with self._lock:
                # 再次确认阶段和因子存在性
                if factor_name not in self._lifecycle_stage:
                    continue
                stage = self._lifecycle_stage[factor_name]
                if stage in (FactorLifecycleStage.DORMANT, FactorLifecycleStage.RETIRED):
                    inactive_factors.append(factor_name)
                    continue
                if current_ic is None or math.isnan(current_ic):
                    current_ic = 0.0
                weight_cap = self.STAGE_WEIGHT_CAPS.get(stage, 0.5)
                decay_factor = self.STAGE_DECAY_FACTORS.get(stage, 1.0)
                raw_weight = max(0.0, current_ic * decay_factor)
                effective_weight = min(raw_weight, weight_cap)
                if effective_weight > self.ACTIVE_WEIGHT_THRESHOLD:
                    active_weights[factor_name] = round(effective_weight, 4)

        sorted_active = OrderedDict(
            sorted(active_weights.items(), key=lambda x: x[1], reverse=True)
        )

        # 限制返回的非活跃因子数量
        inactive_preview = inactive_factors[:self.MAX_INACTIVE_FACTORS_IN_RESPONSE]

        return {
            "status": "ok",
            "reason": f"活跃因子数量: {len(sorted_active)}, 非活跃: {len(inactive_factors)}",
            "data": {
                "active_factors": sorted_active,
                "active_count": len(sorted_active),
                "inactive_count": len(inactive_factors),
                "inactive_factors_preview": inactive_preview,
            },
            "warnings": [],
        }

    def migrate_factor_stage(self, factor_name: str) -> Dict[str, Any]:
        with self._lock:
            if factor_name not in self._lifecycle_stage:
                return self._error_response("unknown_factor", f"因子 {factor_name} 未注册")
            old_stage = self._lifecycle_stage[factor_name]
            current_ic = self._get_latest_effective_ic_internal(factor_name)
            now = time.time()
            new_stage = self._evaluate_lifecycle_migration(factor_name, current_ic, now)
            if new_stage != old_stage:
                self._lifecycle_stage[factor_name] = new_stage
                self._stage_start_time[factor_name] = now
                self._decline_consecutive_count[factor_name] = 0
                self._decline_recovery_count[factor_name] = 0
                self._total_stage_migrations += 1
                self._stage_history[factor_name].append({
                    "timestamp": now,
                    "old_stage": old_stage.value,
                    "new_stage": new_stage.value,
                    "ic_value": current_ic,
                })

            return {
                "status": "ok",
                "reason": f"因子 {factor_name} 阶段迁移: {old_stage.value} -> {new_stage.value}",
                "data": {"factor_name": factor_name, "old_stage": old_stage.value, "new_stage": new_stage.value},
                "warnings": [],
            }

    def get_stage_distribution(self) -> Dict[str, Any]:
        with self._lock:
            distribution = {stage.value: 0 for stage in FactorLifecycleStage}
            for stage in self._lifecycle_stage.values():
                distribution[stage.value] += 1

        return {
            "status": "ok",
            "reason": f"因子阶段分布: {distribution}",
            "data": {"distribution": distribution, "total_factors": len(self._lifecycle_stage)},
            "warnings": [],
        }

    def get_metrics(self) -> Dict[str, Any]:
        with self._lock:
            distribution = {}
            for s in FactorLifecycleStage:
                distribution[s.value] = sum(1 for x in self._lifecycle_stage.values() if x == s)

        return {
            "status": "ok",
            "reason": "运维指标采集完成",
            "data": {
                "total_factors": len(self._lifecycle_stage),
                "total_ic_updates": self._total_ic_updates,
                "total_stage_migrations": self._total_stage_migrations,
                "total_external_call_drops": self._total_external_call_drops,
                "stage_distribution": distribution,
                "dependencies": {
                    "ic_adjuster_available": self._ic_adjuster is not None,
                    "behavioral_logger_available": self._behavioral_logger is not None,
                },
            },
            "warnings": [],
        }

    def health_check(self) -> Dict[str, Any]:
        try:
            with self._lock:
                total_factors = len(self._lifecycle_stage)
                distribution = {}
                for s in FactorLifecycleStage:
                    distribution[s.value] = sum(1 for x in self._lifecycle_stage.values() if x == s)
                ic_history_snapshot = list(self._ic_history.keys())
                total_ic_samples = 0
                for name in ic_history_snapshot:
                    for tier_data in self._ic_history[name].values():
                        total_ic_samples += len(tier_data)

            return {
                "status": "ok",
                "reason": (
                    f"TemporalWeightManager 正常，管理 {total_factors} 个因子，"
                    f"累计IC更新 {self._total_ic_updates} 次，阶段迁移 {self._total_stage_migrations} 次"
                ),
                "data": {
                    "total_factors": total_factors,
                    "stage_distribution": distribution,
                    "total_ic_samples": total_ic_samples,
                    "total_ic_updates": self._total_ic_updates,
                    "total_stage_migrations": self._total_stage_migrations,
                    "dependencies": {
                        "ic_adjuster": self._ic_adjuster is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error("健康检查失败: %s #RECOVERY: 检查锁状态和数据结构完整性", e)
            return self._error_response("health_check_failed", f"健康检查异常: {str(e)}")

    # ========== 私有方法 ==========
    def _error_response(self, error_code: str, reason: str) -> Dict[str, Any]:
        return {
            "status": "error",
            "reason": reason,
            "data": {"error_code": error_code},
            "warnings": [error_code],
        }

    def _log_anomaly(self, factor_name: str, message: str) -> None:
        if self._behavioral_logger:
            try:
                self._behavioral_logger.log_event(
                    event_type="factor_ic_anomaly",
                    details={"factor_name": factor_name, "message": message, "timestamp": time.time()},
                )
            except Exception:
                pass

    def _get_latest_effective_ic(self, factor_name: str) -> float:
        if self._ic_adjuster is not None:
            external_ic = self._call_external_with_timeout(factor_name)
            if external_ic is not None:
                return external_ic
        return self._get_latest_effective_ic_internal(factor_name)

    def _get_latest_effective_ic_internal(self, factor_name: str) -> float:
        """从内部滑动窗口计算加权IC，按层级加权平均"""
        weighted_sum = 0.0
        total_weight = 0.0
        if factor_name in self._ic_history:
            for tier in self.FACTOR_TIERS:
                tier_data = self._ic_history[factor_name].get(tier, deque())
                if not tier_data:
                    continue
                valid = [v for v in tier_data if v is not None and not math.isnan(v) and not math.isinf(v)]
                if not valid:
                    continue
                n = len(valid)
                decay_weights = self._get_decay_weights(n)
                tier_ic = float(np.dot(valid, decay_weights)) / float(np.sum(decay_weights))
                tier_w = self.TIER_WEIGHTS.get(tier, 0.33)
                weighted_sum += tier_ic * tier_w
                total_weight += tier_w
        if total_weight > 0:
            return max(-1.0, min(1.0, weighted_sum / total_weight))
        return 0.0

    def _call_external_with_timeout(self, factor_name: str) -> Optional[float]:
        if not self._worker_semaphore.acquire(blocking=False):
            self._total_external_call_drops += 1
            logger.debug("外部依赖调用线程池已满，跳过对 %s 的IC获取", factor_name)
            return None
        try:
            result_container: Dict[str, Any] = {"value": None, "error": None}

            def _call_target():
                try:
                    result_container["value"] = self._ic_adjuster.get_latest_ic(factor_name)
                except Exception as e:
                    result_container["error"] = e

            thread = threading.Thread(target=_call_target, daemon=True)
            thread.start()
            thread.join(timeout=self.EXTERNAL_CALL_TIMEOUT_SEC)

            if thread.is_alive():
                logger.warning("ICPredictiveAdjuster.get_latest_ic 超时，回退到滑动窗口")
                return None
            if result_container["error"] is not None:
                logger.debug("ICPredictiveAdjuster 调用异常: %s", result_container["error"])
                return None
            if result_container["value"] is not None:
                val = float(result_container["value"])
                if not math.isnan(val) and not math.isinf(val):
                    return max(-1.0, min(1.0, val))
        except Exception as e:
            logger.debug("ICPredictiveAdjuster 调用失败: %s", e)
        finally:
            self._worker_semaphore.release()
        return None

    def _get_decay_weights(self, n: int) -> np.ndarray:
        half_life = self.DEFAULT_IC_DECAY_HALFLIFE_DAYS
        cache_key = (n, half_life)
        if cache_key not in self._decay_weights_cache:
            if len(self._decay_weights_cache) >= self.MAX_DECAY_CACHE_SIZE:
                self._decay_weights_cache.popitem(last=False)
            weights = np.exp(-np.log(2) * np.arange(n - 1, -1, -1) / half_life)
            self._decay_weights_cache[cache_key] = weights
        return self._decay_weights_cache[cache_key]

    def _evaluate_lifecycle_migration(
        self, factor_name: str, current_ic: float, now: float
    ) -> FactorLifecycleStage:
        if current_ic is None or math.isnan(current_ic):
            current_ic = 0.0

        current_stage = self._lifecycle_stage.get(factor_name, FactorLifecycleStage.GROWTH)
        stage_start = self._stage_start_time.get(factor_name, now)
        duration_seconds = now - stage_start

        if current_stage == FactorLifecycleStage.GROWTH:
            if duration_seconds >= self.DEFAULT_GROWTH_MAX_SECONDS:
                if current_ic > self.DEFAULT_DECLINE_IC_THRESHOLD:
                    return FactorLifecycleStage.MATURE
                return FactorLifecycleStage.DECLINE

        elif current_stage == FactorLifecycleStage.MATURE:
            if current_ic < self.DEFAULT_DECLINE_IC_THRESHOLD:
                return FactorLifecycleStage.DECLINE

        elif current_stage == FactorLifecycleStage.DECLINE:
            if (self._decline_recovery_count.get(factor_name, 0) >=
                    self.DEFAULT_DECLINE_RECOVERY_CONSECUTIVE):
                return FactorLifecycleStage.MATURE
            if (self._decline_consecutive_count.get(factor_name, 0) >=
                    self.DEFAULT_DECLINE_CONSECUTIVE_COUNT):
                if current_ic < self.DEFAULT_DORMANT_IC_THRESHOLD:
                    return FactorLifecycleStage.DORMANT

        elif current_stage == FactorLifecycleStage.DORMANT:
            if duration_seconds >= self.DEFAULT_REVIVAL_TEST_INTERVAL_SEC:
                if current_ic > self.DEFAULT_DORMANT_IC_THRESHOLD:
                    return FactorLifecycleStage.REVIVAL
                # 延长检测间隔，避免刚重启就检测
                self._stage_start_time[factor_name] = now
            if duration_seconds >= self.DEFAULT_RETIRED_NO_REVIVAL_SEC:
                return FactorLifecycleStage.RETIRED

        elif current_stage == FactorLifecycleStage.REVIVAL:
            if current_ic > self.DEFAULT_DECLINE_IC_THRESHOLD:
                return FactorLifecycleStage.MATURE
            if duration_seconds >= self.DEFAULT_GROWTH_MAX_SECONDS:
                return FactorLifecycleStage.DECLINE

        return current_stage

    def _log_stage_change(
        self,
        factor_name: str,
        old_stage: str,
        new_stage: str,
        ic_value: float,
        timestamp: float,
    ) -> None:
        event_details = {
            "factor_name": factor_name,
            "old_stage": old_stage,
            "new_stage": new_stage,
            "ic_value": round(ic_value, 4),
            "timestamp": timestamp,
        }
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="factor_stage_migration",
                    details=event_details,
                )
            except Exception as e:
                logger.warning("行为日志记录失败: %s", e)
        else:
            logger.warning(
                "因子 %s 阶段变更: %s -> %s (IC=%.4f) [行为日志未注入]",
                factor_name, old_stage, new_stage, ic_value,
            )
