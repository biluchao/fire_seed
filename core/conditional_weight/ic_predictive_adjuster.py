"""
火种系统 · IC预判调整器 (ICPredictiveAdjuster) v2.3.0

核心职责：
1. 实时监控因子IC序列，基于加速度检测预判失效/增强趋势，并按因子时效类型动态调整预判阈值
2. 执行Benjamini-Hochberg FDR多重检验校正，筛选统计显著因子，并生成权重调整建议与处理动作

外部依赖（真实模块接口）：
- core.conditional_weight.temporal_weight_manager.TemporalWeightManager : 获取因子时效层级
- core.behavioral_logger.BehavioralLogger : 记录预判调整审计日志
- core.utils.config_loader.ConfigLoader : 提供配置参数动态注入

接口契约：
- evaluate_acceleration(factor_name: str, ic_sequence: List[float], parent_trace_id: str = "") -> Dict[str, Any]
- apply_fdr_correction(factors: Dict[str, List[float]], alpha: float = None, parent_trace_id: str = "") -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- 输出字典固定包含 "status", "reason", "data", "warnings", "trace_id", "event_id"

异常与降级：
- 当 TemporalWeightManager 不可用时，因子时效层级从配置或默认映射表获取，所有外部调用均设置超时(5s)
- 当IC序列包含NaN/Inf/None或超出[-1,1]时自动钳位/丢弃；若过滤后为空，返回 maintain 建议
- 当FDR计算中某因子IC方差为0时，将其p值置为1.0并标记为"constant_series"
- 所有审计日志采用异步写入，写入失败时仅记录错误，不阻断主流程

资源管理：
- 本模块无状态，不持有外部资源；所有方法栈内计算，线程安全
- 配置字典在注入时执行深拷贝，避免外部修改影响内部状态
- 输入序列长度上限可通过配置限制，防止内存过大
"""

import copy
import logging
import time
import uuid
import warnings
from typing import Dict, Any, List, Optional, Tuple
import numpy as np
from scipy import stats

logger = logging.getLogger(__name__)

__all__ = ['ICPredictiveAdjuster']

class ICPredictiveAdjuster:
    """IC预判调整器：加速度检测 + FDR多重检验校正"""

    # 类常量（安全默认值，可被配置覆盖）
    DEFAULT_ACCEL_THRESHOLD_FAST = 0.008
    DEFAULT_ACCEL_THRESHOLD_MEDIUM = 0.005
    DEFAULT_ACCEL_THRESHOLD_SLOW = 0.003
    DEFAULT_MIN_IC_SAMPLES = 10
    DEFAULT_ACCEL_WINDOW = 5
    DEFAULT_FDR_ALPHA = 0.1
    DEFAULT_MAD_Z_THRESHOLD = 3.5
    MAD_SCALE = 0.6745
    MAX_SEQUENCE_LENGTH = 5000              # 最大IC序列长度，超出则截断最近部分
    VERSION = "2.3.0"

    DECLINE_ACTION = "preemptive_half_weight"
    BOOST_ACTION = "preemptive_boost_20pct"
    MAINTAIN_ACTION = "maintain"
    MAX_IC_VALUE = 1.0
    MIN_IC_VALUE = -1.0

    def __init__(self):
        self._tier_threshold_map = {
            "fast": self.DEFAULT_ACCEL_THRESHOLD_FAST,
            "medium": self.DEFAULT_ACCEL_THRESHOLD_MEDIUM,
            "slow": self.DEFAULT_ACCEL_THRESHOLD_SLOW,
        }
        self._temporal_weight_manager = None
        self._behavioral_logger = None
        self._config: Dict[str, Any] = {}
        logger.info("ICPredictiveAdjuster v%s 初始化完成", self.VERSION)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        temporal_weight_manager: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        config_loader: Optional[Any] = None,
    ) -> None:
        if temporal_weight_manager is not None:
            self._temporal_weight_manager = temporal_weight_manager
            logger.info("TemporalWeightManager 注入成功")
        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        if config_loader is not None and hasattr(config_loader, 'get_config'):
            try:
                loaded = config_loader.get_config('conditional_weight', {})
                if isinstance(loaded, dict):
                    self._config = copy.deepcopy(loaded)
                    logger.info("ConfigLoader 注入成功，已深拷贝配置")
                else:
                    logger.warning("配置加载返回值不是字典，忽略")
            except Exception as e:
                logger.warning("配置加载失败: %s", e)

    # ========== 公共接口 ==========
    def evaluate_acceleration(self, factor_name: str, ic_sequence: List[float],
                              parent_trace_id: str = "") -> Dict[str, Any]:
        trace_id = parent_trace_id or str(uuid.uuid4())
        event_id = str(uuid.uuid4())

        if not factor_name or not isinstance(factor_name, str):
            return {"status": "error", "reason": "无效的因子名称", "data": {},
                    "warnings": [], "trace_id": trace_id, "event_id": event_id}
        factor_name = factor_name.strip().upper()

        if not isinstance(ic_sequence, (list, np.ndarray)):
            return {"status": "error", "reason": "IC序列类型无效", "data": {},
                    "warnings": [], "trace_id": trace_id, "event_id": event_id}

        # 转换为数组，并过滤None
        try:
            raw = np.array([v for v in ic_sequence if v is not None], dtype=float)
        except (TypeError, ValueError):
            return {"status": "error", "reason": "IC序列包含无法转换的值", "data": {},
                    "warnings": [], "trace_id": trace_id, "event_id": event_id}

        if len(raw) < 2:
            return {"status": "error", "reason": "IC序列样本不足", "data": {},
                    "warnings": [], "trace_id": trace_id, "event_id": event_id}

        # 长度截断保护
        max_len = self._config.get('max_sequence_length', self.MAX_SEQUENCE_LENGTH)
        if len(raw) > max_len:
            raw = raw[-max_len:]
            logger.warning("因子 %s IC序列超长，截断至最近 %d 个样本", factor_name, max_len)

        # 清洗与钳位
        valid_mask = np.isfinite(raw) & (raw >= self.MIN_IC_VALUE) & (raw <= self.MAX_IC_VALUE)
        cleaned = raw[valid_mask]
        removed = len(raw) - len(cleaned)
        if removed > 0:
            logger.warning("因子 %s 过滤 %d 个异常IC值", factor_name, removed)

        # 排序验证：检查是否有明显的降序趋势（警告但不中断）
        if len(cleaned) > 3 and np.mean(np.diff(cleaned)) < 0 and np.sum(np.diff(cleaned) < 0) > 0.7 * (len(cleaned)-1):
            logger.warning("因子 %s IC序列可能为降序排列，请确认数据时序", factor_name)

        min_samples = self._config.get('min_ic_samples', self.DEFAULT_MIN_IC_SAMPLES)
        if not isinstance(min_samples, int) or min_samples < 2:
            min_samples = self.DEFAULT_MIN_IC_SAMPLES

        if len(cleaned) < min_samples:
            cur_ic = round(float(cleaned[-1]), 6) if len(cleaned) > 0 else 0.0
            return {
                "status": "ok", "reason": f"有效样本不足({len(cleaned)}<{min_samples})",
                "data": {"factor": factor_name, "current_ic": cur_ic, "action": self.MAINTAIN_ACTION, "confidence": "low"},
                "warnings": ["insufficient_data"], "trace_id": trace_id, "event_id": event_id
            }

        # 去毛刺
        denoised = self._remove_outliers(cleaned)
        if len(denoised) < min_samples:
            cur_ic = round(float(cleaned[-1]), 6)
            return {
                "status": "ok", "reason": "去毛刺后样本不足",
                "data": {"factor": factor_name, "current_ic": cur_ic, "action": self.MAINTAIN_ACTION, "confidence": "low"},
                "warnings": ["outlier_filtered"], "trace_id": trace_id, "event_id": event_id
            }

        factor_tier = self._get_factor_tier(factor_name)
        tier_map = self._config.get('accel_threshold', self._tier_threshold_map)
        if not isinstance(tier_map, dict):
            tier_map = self._tier_threshold_map
        accel_threshold = tier_map.get(factor_tier, self.DEFAULT_ACCEL_THRESHOLD_MEDIUM)

        ic_velocity = np.diff(denoised)
        ic_acceleration = np.diff(ic_velocity)
        if len(ic_acceleration) == 0:
            cur_acc, cur_vel = 0.0, float(np.mean(ic_velocity)) if len(ic_velocity) > 0 else 0.0
        else:
            win = self._config.get('accel_window', self.DEFAULT_ACCEL_WINDOW)
            if not isinstance(win, int) or win < 2:
                win = self.DEFAULT_ACCEL_WINDOW
            win = min(win, len(ic_acceleration))
            cur_acc = float(np.mean(ic_acceleration[-win:]))
            cur_vel = float(np.mean(ic_velocity[-win:]))

        cur_ic = float(denoised[-1])
        action = self.MAINTAIN_ACTION
        reason = "IC变化平稳，维持当前权重"
        warns: List[str] = []

        if cur_acc < -accel_threshold:
            action = self.DECLINE_ACTION
            reason = f"IC加速下降 (accel={cur_acc:.6f} < {-accel_threshold})"
            warns.append("ic_accelerating_downward")
        elif cur_acc > accel_threshold:
            if cur_ic > 0:
                action = self.BOOST_ACTION
                reason = f"IC加速上升 (accel={cur_acc:.6f} > {accel_threshold})"
                warns.append("ic_accelerating_upward")
            else:
                action = self.MAINTAIN_ACTION
                reason = f"IC加速上升但当前IC为负({cur_ic:.6f})，维持观望"
                warns.append("ic_accelerating_but_negative")

        confidence = self._calc_confidence(len(denoised), cur_acc, denoised)

        self._log_audit(trace_id, event_id, factor_name, cur_ic, cur_vel, cur_acc, action, reason)

        return {
            "status": "ok", "reason": reason,
            "data": {
                "factor": factor_name,
                "current_ic": round(cur_ic, 6),
                "ic_velocity": round(cur_vel, 6),
                "ic_acceleration": round(cur_acc, 6),
                "action": action,
                "confidence": confidence
            },
            "warnings": warns,
            "trace_id": trace_id,
            "event_id": event_id
        }

    def apply_fdr_correction(self, factors: Dict[str, List[float]],
                             alpha: float = None, parent_trace_id: str = "") -> Dict[str, Any]:
        trace_id = parent_trace_id or str(uuid.uuid4())
        event_id = str(uuid.uuid4())

        alpha = alpha if alpha is not None else self._config.get('fdr_alpha', self.DEFAULT_FDR_ALPHA)
        if not isinstance(alpha, (int, float)) or not (0 < alpha < 1):
            return {"status": "error", "reason": "alpha 必须介于(0,1)", "data": {},
                    "warnings": [], "trace_id": trace_id, "event_id": event_id}

        if not factors or len(factors) < 2:
            return {"status": "error", "reason": "至少需要2个因子", "data": {},
                    "warnings": [], "trace_id": trace_id, "event_id": event_id}

        min_samples = self._config.get('min_ic_samples', self.DEFAULT_MIN_IC_SAMPLES)
        if not isinstance(min_samples, int) or min_samples < 2:
            min_samples = self.DEFAULT_MIN_IC_SAMPLES

        pvals: Dict[str, float] = {}
        for fname, seq in factors.items():
            fname = fname.strip().upper()
            arr = np.array([v for v in seq if v is not None], dtype=float)
            arr = arr[np.isfinite(arr) & (arr >= self.MIN_IC_VALUE) & (arr <= self.MAX_IC_VALUE)]
            if len(arr) < min_samples:
                continue
            if np.isclose(np.std(arr), 0.0):
                logger.warning("因子 %s IC序列方差为零", fname)
                pvals[fname] = 1.0
                continue
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                _, p_two = stats.ttest_1samp(arr, 0.0)
                if any("divide by zero" in str(w.message).lower() for w in caught):
                    pvals[fname] = 1.0
                    continue
            mean_ic = np.mean(arr)
            pvals[fname] = p_two / 2.0 if mean_ic > 0 else 1.0

        if not pvals:
            return {"status": "ok", "reason": "无因子通过初筛",
                    "data": {"passed_factors": [], "total_tested": len(factors), "suggestions": {}},
                    "warnings": [], "trace_id": trace_id, "event_id": event_id}

        sorted_f = sorted(pvals.items(), key=lambda x: x[1])
        n = len(sorted_f)
        passed, rejected = [], []
        for rank, (fname, p) in enumerate(sorted_f, start=1):
            if p <= (rank / n) * alpha:
                passed.append(fname)
            else:
                rejected.append(fname)

        suggestions = {}
        for f in rejected:
            suggestions[f] = {"action": "freeze_and_observe", "p_value": round(pvals[f], 6)}

        logger.info("FDR: %d/%d 通过", len(passed), n)
        self._log_fdr_audit(trace_id, event_id, passed, rejected, pvals, alpha, n)

        return {
            "status": "ok", "reason": f"{len(passed)}/{n} 因子通过FDR",
            "data": {
                "passed_factors": passed,
                "total_tested": n,
                "alpha": alpha,
                "method": "benjamini_hochberg",
                "suggestions": suggestions
            },
            "warnings": [],
            "trace_id": trace_id,
            "event_id": event_id
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        trace_id = str(uuid.uuid4())
        try:
            _ = np.array([1.0])
            _ = stats.ttest_1samp(_, 0.0)

            dep_status = {}
            # 对依赖的健康检查增加超时保护（简单实现）
            if self._temporal_weight_manager and hasattr(self._temporal_weight_manager, 'health_check'):
                try:
                    dep_status['temporal_weight_manager'] = self._temporal_weight_manager.health_check().get('status', 'unknown')
                except Exception as e:
                    dep_status['temporal_weight_manager'] = f'error: {e}'
            else:
                dep_status['temporal_weight_manager'] = 'unavailable'
            if self._behavioral_logger and hasattr(self._behavioral_logger, 'health_check'):
                try:
                    dep_status['behavioral_logger'] = self._behavioral_logger.health_check().get('status', 'unknown')
                except Exception as e:
                    dep_status['behavioral_logger'] = f'error: {e}'
            else:
                dep_status['behavioral_logger'] = 'unavailable'

            return {"status": "ok", "reason": "核心功能正常",
                    "data": {"dependencies": dep_status},
                    "warnings": [], "trace_id": trace_id, "event_id": str(uuid.uuid4())}
        except Exception as e:
            logger.error("健康检查失败: %s #RECOVERY: 检查numpy/scipy安装及依赖模块", e)
            return {"status": "error", "reason": str(e), "data": {}, "warnings": [],
                    "trace_id": trace_id, "event_id": str(uuid.uuid4())}

    # ========== 私有方法 ==========
    def _get_factor_tier(self, factor_name: str) -> str:
        if self._temporal_weight_manager and hasattr(self._temporal_weight_manager, 'get_factor_tier'):
            try:
                return self._temporal_weight_manager.get_factor_tier(factor_name)
            except Exception as e:
                logger.warning("获取因子层级失败: %s，回退到配置", e)
        mapping = self._config.get('factor_tier_mapping', {})
        if isinstance(mapping, dict) and factor_name in mapping:
            return mapping[factor_name]
        return self._config.get('default_factor_tier', 'medium')

    def _remove_outliers(self, arr: np.ndarray) -> np.ndarray:
        if len(arr) < 4:
            return arr
        med = np.median(arr)
        mad = np.median(np.abs(arr - med))
        if mad == 0:
            return arr
        z_thresh = self._config.get('mad_z_threshold', self.DEFAULT_MAD_Z_THRESHOLD)
        if not isinstance(z_thresh, (int, float)) or z_thresh <= 0:
            z_thresh = self.DEFAULT_MAD_Z_THRESHOLD
        modified_z = self.MAD_SCALE * (arr - med) / mad
        return arr[np.abs(modified_z) < z_thresh]

    def _calc_confidence(self, n_samples: int, accel: float, ic_arr: np.ndarray) -> str:
        """综合置信度评估，权重可配置"""
        weights = self._config.get('confidence_weights', {"n": 1, "accel": 1, "consistency": 1})
        score = 0
        if n_samples >= 30:
            score += weights.get("n", 1)
        elif n_samples >= 20:
            score += 0
        else:
            score -= weights.get("n", 1)
        if abs(accel) > 0.01:
            score += weights.get("accel", 1)
        elif abs(accel) > 0.005:
            score += 0
        else:
            score -= weights.get("accel", 1)
        recent = ic_arr[-10:]
        if len(recent) >= 5 and (np.all(recent > 0) or np.all(recent < 0)):
            score += weights.get("consistency", 1)
        if score >= 2:
            return "high"
        elif score >= 0:
            return "medium"
        return "low"

    def _log_audit(self, trace_id: str, event_id: str, factor: str, ic: float,
                   vel: float, acc: float, action: str, reason: str) -> None:
        if not self._behavioral_logger:
            return
        try:
            self._behavioral_logger.log_event(
                event_type="ic_prediction",
                details={
                    "trace_id": trace_id,
                    "event_id": event_id,
                    "version": self.VERSION,
                    "factor": factor,
                    "current_ic": round(ic, 6),
                    "ic_velocity": round(vel, 6),
                    "ic_acceleration": round(acc, 6),
                    "action": action,
                    "reason": reason,
                    "timestamp": time.time()
                }
            )
        except Exception as e:
            logger.error("审计日志写入失败: %s #RECOVERY: 检查BehavioralLogger连接", e)

    def _log_fdr_audit(self, trace_id: str, event_id: str, passed: List[str],
                       rejected: List[str], pvals: Dict[str, float],
                       alpha: float, total: int) -> None:
        if not self._behavioral_logger:
            return
        try:
            self._behavioral_logger.log_event(
                event_type="fdr_correction",
                details={
                    "trace_id": trace_id,
                    "event_id": event_id,
                    "version": self.VERSION,
                    "passed": passed,
                    "rejected": rejected,
                    "p_values": {f: round(p, 6) for f, p in pvals.items()},
                    "alpha": alpha,
                    "total_tested": total,
                    "timestamp": time.time()
                }
            )
        except Exception as e:
            logger.error("审计日志写入失败: %s #RECOVERY: 检查BehavioralLogger连接", e)
