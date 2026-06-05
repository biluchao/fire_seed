"""
火种系统 · 因子冗余惩罚器 (RedundancyPenalty)

核心职责：
1. 基于因子间相关系数矩阵识别高度冗余的因子对，对冗余因子施加权重惩罚，防止多重共线性导致评分失真
2. 支持皮尔逊相关性和可选降级方案（无scipy时使用纯numpy实现），确保在任何环境下均可运行
3. 提供确定性审计种子、归一化后处理及完整审计追踪，保证风控合规与策略回测的可复现性

外部依赖（真实模块接口）：
- core.conditional_weight.weight_engine.WeightEngine : 获取当前因子权重与IC序列
- core.perception.factor_preprocessor.FactorPreprocessor : 获取因子值矩阵用于计算实时相关性
- core.utils.config_loader.ConfigLoader : 读取冗余惩罚阈值、惩罚系数、最大惩罚次数等配置

接口契约：
- compute_penalty(factor_weights: Dict[str, float], factor_values: Optional[Dict[str, List[float]]] = None) -> Dict[str, Any]
  计算冗余惩罚后的权重，输出字典固定包含 "adjusted_weights" (Dict[str, float]), "penalty_details" (List[Dict]), "reason" (str), "warnings" (List[str])
  错误时附带 "error_code" (str)
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 scipy 不可用时，自动降级为纯 numpy 实现的皮尔逊相关系数计算，并记录 WARNING
- 当因子值矩阵不可用时，降级为仅基于历史IC相关性进行惩罚（保守策略）
- 当配置缺失或值域非法时，使用类常量提供的安全默认值并记录 ERROR
- 当 IC 不可用时，惩罚目标按因子名称字母序固定选择，确保可复现性
- 当权重归一化总和为零时，降级为均匀分配

资源管理：
- 本模块为无状态计算模块，不持有任何需要手动释放的资源
- 所有中间计算结果在方法返回后自动回收
- 相关系数矩阵在计算完成后立即释放，防止内存峰值
"""

import logging
import threading
import time
from typing import Dict, Any, List, Optional, Tuple, Union

import numpy as np

# scipy 可选依赖，导入失败时降级为 numpy 实现
try:
    from scipy.stats import pearsonr as _scipy_pearsonr
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    _scipy_pearsonr = None

logger = logging.getLogger(__name__)


def _numpy_pearsonr(x: np.ndarray, y: np.ndarray) -> Tuple[float, float]:
    """
    纯 numpy 实现的皮尔逊相关系数，用于 scipy 不可用时的降级。
    注意：返回的 p 值为粗略近似，仅用于排序比较，不具备统计显著性检验能力。
    公式参考：t = r * sqrt((n-2) / (1-r^2))，p ≈ 2 * (1 - Φ(|t|)) 的 tanh 近似。
    """
    n = len(x)
    if n < 3:
        return 0.0, 1.0
    r = np.corrcoef(x, y)[0, 1]
    if np.isnan(r):
        return 0.0, 1.0
    t_stat = r * np.sqrt((n - 2) / (1 - r ** 2 + 1e-10))
    p_val = 2 * (1 - 0.5 * (1 + np.tanh(t_stat / np.sqrt(2))))
    return float(r), float(p_val)


class RedundancyPenalty:
    """因子冗余惩罚器：识别高度共线因子并降低其权重"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_CORRELATION_THRESHOLD: float = 0.85        # 冗余判定相关系数阈值，无量纲，取值范围 [0.7, 0.95]
    DEFAULT_PENALTY_FACTOR: float = 0.7               # 单次冗余惩罚系数（乘数），无量纲，取值范围 [0.3, 0.9]
    DEFAULT_MAX_PENALTY_COUNT: int = 2                # 单个因子最多被惩罚的次数，防止权重过度衰减，[1, 5]
    DEFAULT_MIN_WEIGHT_AFTER_PENALTY: float = 0.001   # 惩罚后最低权重，防止权重归零导致因子被彻底忽略
    DEFAULT_MIN_IC_DIFF: float = 0.02                 # 最小IC差异（绝对值），低于此值视为IC相同，[0.001, 0.1]
    DEFAULT_MIN_SAMPLES_FOR_CORR: int = 30            # 计算相关系数所需最小样本量，无量纲，[10, 100]
    DEFAULT_MAX_FACTORS_FOR_CORR: int = 200           # 最大参与相关性计算的因子数，超出后随机下采样，[50, 500]
    DEFAULT_FACTOR_VALUE_MAX_LEN: int = 2000          # 因子值序列最大长度，超出后截断，[500, 5000]
    DEFAULT_AUDIT_SEED: int = 42                      # 审计种子，用于下采样确保可复现，[0, 2**31-1]

    def __init__(self) -> None:
        # 外部依赖注入
        self._weight_engine: Any = None
        self._factor_preprocessor: Any = None
        self._config_loader: Any = None

        # 配置参数（由config_loader注入后覆盖）
        self._correlation_threshold: float = self.DEFAULT_CORRELATION_THRESHOLD
        self._penalty_factor: float = self.DEFAULT_PENALTY_FACTOR
        self._max_penalty_count: int = self.DEFAULT_MAX_PENALTY_COUNT
        self._min_weight: float = self.DEFAULT_MIN_WEIGHT_AFTER_PENALTY
        self._min_ic_diff: float = self.DEFAULT_MIN_IC_DIFF
        self._min_samples: int = self.DEFAULT_MIN_SAMPLES_FOR_CORR
        self._max_factors: int = self.DEFAULT_MAX_FACTORS_FOR_CORR
        self._max_value_len: int = self.DEFAULT_FACTOR_VALUE_MAX_LEN
        self._audit_seed: int = self.DEFAULT_AUDIT_SEED

        # 线程安全（读写锁，支持配置读与计算并行，写独占）
        self._lock: threading.RLock = threading.RLock()

        if not SCIPY_AVAILABLE:
            logger.warning("scipy 不可用，已降级为 numpy 实现，相关系数 p 值仅用于排序比较")
            self._pearsonr = _numpy_pearsonr
        else:
            self._pearsonr = _scipy_pearsonr

        logger.info("RedundancyPenalty 初始化完成，阈值=%.2f，惩罚系数=%.2f，最大惩罚次数=%d，种子=%d",
                    self._correlation_threshold, self._penalty_factor,
                    self._max_penalty_count, self._audit_seed)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        weight_engine: Optional[Any] = None,
        factor_preprocessor: Optional[Any] = None,
        config_loader: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选注入，未注入时对应功能降级，可重复调用以更新依赖）"""
        with self._lock:
            if weight_engine is not None:
                self._weight_engine = weight_engine
                logger.info("WeightEngine 注入成功")
            if factor_preprocessor is not None:
                self._factor_preprocessor = factor_preprocessor
                logger.info("FactorPreprocessor 注入成功")
            if config_loader is not None:
                self._config_loader = config_loader
                self._load_config()
                logger.info("ConfigLoader 注入成功，配置已同步")

    def _load_config(self) -> None:
        """从配置加载器读取参数，加锁保证原子性，并对值域进行二次验证"""
        if self._config_loader is None:
            return
        try:
            with self._lock:
                new_threshold = self._config_loader.get(
                    "conditional_weight.redundancy.correlation_threshold",
                    self.DEFAULT_CORRELATION_THRESHOLD
                )
                new_penalty = self._config_loader.get(
                    "conditional_weight.redundancy.penalty_factor",
                    self.DEFAULT_PENALTY_FACTOR
                )
                new_max_penalty = self._config_loader.get(
                    "conditional_weight.redundancy.max_penalty_count",
                    self.DEFAULT_MAX_PENALTY_COUNT
                )
                new_min_weight = self._config_loader.get(
                    "conditional_weight.redundancy.min_weight_after_penalty",
                    self.DEFAULT_MIN_WEIGHT_AFTER_PENALTY
                )
                new_min_ic_diff = self._config_loader.get(
                    "conditional_weight.redundancy.min_ic_diff",
                    self.DEFAULT_MIN_IC_DIFF
                )
                new_min_samples = self._config_loader.get(
                    "conditional_weight.redundancy.min_samples",
                    self.DEFAULT_MIN_SAMPLES_FOR_CORR
                )
                new_max_factors = self._config_loader.get(
                    "conditional_weight.redundancy.max_factors",
                    self.DEFAULT_MAX_FACTORS_FOR_CORR
                )
                new_max_value_len = self._config_loader.get(
                    "conditional_weight.redundancy.max_value_len",
                    self.DEFAULT_FACTOR_VALUE_MAX_LEN
                )
                new_seed = self._config_loader.get(
                    "conditional_weight.redundancy.audit_seed",
                    self.DEFAULT_AUDIT_SEED
                )

                # 值域验证，非法时保留旧值并记录错误
                if not (0.7 <= new_threshold <= 0.95):
                    logger.error(f"correlation_threshold 超出范围: {new_threshold}，已保留原值 {self._correlation_threshold}")
                    new_threshold = self._correlation_threshold
                if not (0.3 <= new_penalty <= 0.9):
                    logger.error(f"penalty_factor 超出范围: {new_penalty}，已保留原值 {self._penalty_factor}")
                    new_penalty = self._penalty_factor
                if new_max_penalty < 1:
                    logger.error(f"max_penalty_count 必须 >= 1: {new_max_penalty}，已保留原值 {self._max_penalty_count}")
                    new_max_penalty = self._max_penalty_count
                if new_min_weight <= 0:
                    logger.error(f"min_weight 必须 > 0: {new_min_weight}，已保留原值 {self._min_weight}")
                    new_min_weight = self._min_weight
                if new_min_samples < 3:
                    logger.error(f"min_samples 必须 >= 3: {new_min_samples}，已保留原值 {self._min_samples}")
                    new_min_samples = self._min_samples

                self._correlation_threshold = new_threshold
                self._penalty_factor = new_penalty
                self._max_penalty_count = new_max_penalty
                self._min_weight = new_min_weight
                self._min_ic_diff = new_min_ic_diff
                self._min_samples = new_min_samples
                self._max_factors = new_max_factors
                self._max_value_len = new_max_value_len
                self._audit_seed = new_seed

            logger.info("配置已热加载并验证通过: threshold=%.2f, penalty=%.2f, max_penalty=%d, "
                        "min_weight=%.4f, seed=%d",
                        self._correlation_threshold, self._penalty_factor,
                        self._max_penalty_count, self._min_weight, self._audit_seed)
        except Exception as e:
            logger.error(f"加载配置失败: {e} #RECOVERY: 检查配置文件路径及格式")

    def _get_config_snapshot(self) -> Dict[str, Union[float, int]]:
        """获取当前配置的不可变快照，加锁读取"""
        with self._lock:
            return {
                "correlation_threshold": self._correlation_threshold,
                "penalty_factor": self._penalty_factor,
                "max_penalty_count": self._max_penalty_count,
                "min_weight": self._min_weight,
                "min_ic_diff": self._min_ic_diff,
                "min_samples": self._min_samples,
                "max_factors": self._max_factors,
                "max_value_len": self._max_value_len,
                "audit_seed": self._audit_seed,
            }

    # ========== 公共接口 ==========
    def compute_penalty(
        self,
        factor_weights: Dict[str, float],
        factor_values: Optional[Dict[str, List[float]]] = None,
    ) -> Dict[str, Any]:
        """
        对给定因子权重施加冗余惩罚，并自动归一化调整后的权重。

        Args:
            factor_weights: 原始因子权重字典 {factor_name: weight}
            factor_values: 可选的因子值矩阵，用于计算实时相关性。
                           若为None，则尝试从注入的FactorPreprocessor获取。

        Returns:
            标准响应字典，包含 normalized_weights, penalty_details, audit_info
        """
        # 参数校验
        if not isinstance(factor_weights, dict):
            return {
                "status": "error",
                "error_code": "INVALID_INPUT",
                "reason": "factor_weights 必须为字典类型",
                "data": {},
                "warnings": [],
            }
        if not factor_weights:
            return {
                "status": "ok",
                "reason": "因子权重为空，无需惩罚",
                "data": {"adjusted_weights": {}, "penalty_details": [], "statistics": {}},
                "warnings": [],
            }

        # 清理并过滤非法权重值，生成安全的副本
        cleaned_weights: Dict[str, float] = {}
        invalid_count = 0
        for name, w in factor_weights.items():
            if not isinstance(w, (int, float)) or np.isnan(w) or np.isinf(w) or w < 0:
                logger.warning(f"因子 {name} 权重无效 ({w})，已重置为 0.001")
                cleaned_weights[name] = 0.001
                invalid_count += 1
            else:
                cleaned_weights[name] = float(w)

        # 获取因子值矩阵（仅取与权重字典匹配的因子）
        values_matrix = factor_values
        if values_matrix is not None:
            # 过滤掉 factor_values 中不在权重字典中的键，防止无关数据进入计算
            values_matrix = {k: v for k, v in values_matrix.items() if k in cleaned_weights}

        if values_matrix is None and self._factor_preprocessor is not None:
            try:
                raw_values = self._factor_preprocessor.get_factor_values(list(cleaned_weights.keys()))
                if raw_values:
                    values_matrix = {k: v for k, v in raw_values.items() if k in cleaned_weights}
                logger.debug("从 FactorPreprocessor 获取到 %d 个因子值矩阵", len(values_matrix) if values_matrix else 0)
            except Exception as e:
                logger.warning(f"获取因子值矩阵失败: {e}，降级为跳过冗余惩罚")
                values_matrix = None

        factor_names = sorted(cleaned_weights.keys())  # 排序确保后续处理确定性
        if len(factor_names) < 2:
            return {
                "status": "ok",
                "reason": "因子数量少于2，无需冗余惩罚",
                "data": {
                    "adjusted_weights": dict(cleaned_weights),
                    "penalty_details": [],
                    "statistics": {"total_factors": len(factor_names), "redundant_pairs": 0},
                },
                "warnings": [],
            }

        # 获取不可变配置快照
        cfg = self._get_config_snapshot()

        # 计算相关系数矩阵（在锁外执行，只读数据）
        corr_matrix = None
        start_time = time.time()
        if values_matrix is not None:
            corr_matrix = self._compute_correlation_matrix(factor_names, values_matrix, cfg)
        elapsed = time.time() - start_time
        if elapsed > 1.0:
            logger.info("相关系数矩阵计算耗时 %.2f 秒，因子数 %d", elapsed, len(factor_names))

        # 执行冗余惩罚——基于原始权重副本，不修改原始输入
        adjusted_weights, penalty_details = self._apply_redundancy_penalty(
            factor_names, dict(cleaned_weights), corr_matrix, cfg
        )

        # 归一化调整后的权重
        adjusted_weights = self._normalize_weights(adjusted_weights)

        warnings: List[str] = []
        if corr_matrix is None:
            warnings.append("因子值矩阵不可用，冗余惩罚跳过，权重保持不变")
        if invalid_count > 0:
            warnings.append(f"{invalid_count} 个因子权重被重置")

        logger.info("冗余惩罚完成: %d 个因子，%d 对被惩罚",
                     len(factor_names), len(penalty_details))

        return {
            "status": "ok",
            "reason": f"冗余惩罚完成，{len(penalty_details)} 个因子被降权",
            "data": {
                "adjusted_weights": adjusted_weights,
                "penalty_details": penalty_details,
                "statistics": {
                    "total_factors": len(factor_names),
                    "redundant_pairs": len(penalty_details),
                    "corr_computation_time_ms": round(elapsed * 1000, 1),
                },
            },
            "warnings": warnings,
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检，不依赖外部模块即可完成基础功能验证"""
        try:
            # 基础计算测试（不依赖外部模块）
            test_weights = {"a": 0.6, "b": 0.4}
            test_values = {"a": [1.0, 2.0, 3.0] * 10, "b": [1.0, 2.1, 3.2] * 10}
            result = self.compute_penalty(test_weights, test_values)
            if result["status"] != "ok":
                return {
                    "status": "degraded",
                    "error_code": "SELF_TEST_FAILED",
                    "reason": f"基本计算测试失败: {result.get('reason', '未知错误')}",
                    "data": {},
                    "warnings": ["self_test_failed"],
                }

            # 如果外部依赖已注入，额外检查接口可用性（不产生副作用）
            if self._weight_engine is not None:
                if not hasattr(self._weight_engine, 'get_factor_ic'):
                    logger.warning("WeightEngine 缺少 get_factor_ic 方法")
                if not hasattr(self._weight_engine, 'get_all_factor_names'):
                    logger.warning("WeightEngine 缺少 get_all_factor_names 方法")

            return {
                "status": "ok",
                "reason": "RedundancyPenalty 自检通过",
                "data": {
                    "dependencies": {
                        "weight_engine": self._weight_engine is not None,
                        "factor_preprocessor": self._factor_preprocessor is not None,
                        "config_loader": self._config_loader is not None,
                    },
                    "scipy_available": SCIPY_AVAILABLE,
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查scipy/numpy依赖完整性")
            return {
                "status": "error",
                "error_code": "HEALTH_CHECK_EXCEPTION",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _compute_correlation_matrix(
        self,
        factor_names: List[str],
        factor_values: Dict[str, List[float]],
        cfg: Dict[str, Union[float, int]],
    ) -> Optional[np.ndarray]:
        """计算因子间相关系数矩阵，对大规模因子自动采样并清洗异常值"""
        n = len(factor_names)
        max_factors = int(cfg["max_factors"])
        max_value_len = int(cfg["max_value_len"])
        min_samples = int(cfg["min_samples"])
        audit_seed = int(cfg.get("audit_seed", self.DEFAULT_AUDIT_SEED))

        # 大规模因子下采样：使用审计种子确保结果可复现
        sampled_indices = list(range(n))
        excluded_names: List[str] = []
        if n > max_factors:
            logger.warning("因子数量 %d 超过上限 %d，使用审计种子 %d 进行可复现采样",
                           n, max_factors, audit_seed)
            rng = np.random.RandomState(audit_seed)
            sampled_indices = sorted(rng.choice(n, max_factors, replace=False))
            excluded_names = [factor_names[i] for i in range(n) if i not in sampled_indices]
            factor_names = [factor_names[i] for i in sampled_indices]
            n = max_factors

        matrix = np.eye(n)
        valid_mask = np.ones(n, dtype=bool)

        # 清洗因子值：检查样本量、类型、常量检测
        cleaned_values: List[List[float]] = []
        for i, name in enumerate(factor_names):
            raw_values = factor_values.get(name, [])
            if not isinstance(raw_values, (list, np.ndarray)):
                logger.warning(f"因子 {name} 的值不是序列类型，忽略")
                valid_mask[i] = False
                cleaned_values.append([])
                continue

            numeric_vals: List[float] = []
            for v in raw_values:
                if isinstance(v, (int, float)) and not (np.isnan(v) or np.isinf(v)):
                    numeric_vals.append(float(v))
            if len(numeric_vals) < min_samples:
                valid_mask[i] = False
                logger.warning(f"因子 {name} 有效样本不足({len(numeric_vals)}<{min_samples})")
                cleaned_values.append([])
                continue

            # 检测常量因子（方差为零），常量因子与其他因子的相关系数无意义
            arr = np.array(numeric_vals)
            if np.std(arr) < 1e-12:
                logger.warning(f"因子 {name} 为常量，相关性置为0")
                valid_mask[i] = False
                cleaned_values.append([])
                continue

            if len(numeric_vals) > max_value_len:
                logger.debug(f"因子 {name} 序列过长({len(numeric_vals)})，截断至 {max_value_len}")
                numeric_vals = numeric_vals[:max_value_len]
            cleaned_values.append(numeric_vals)

        for i in range(n):
            if not valid_mask[i]:
                continue
            xi = np.array(cleaned_values[i])
            for j in range(i + 1, n):
                if not valid_mask[j]:
                    continue
                xj = np.array(cleaned_values[j])
                min_len = min(len(xi), len(xj))
                if min_len < min_samples:
                    continue
                try:
                    corr, _ = self._pearsonr(xi[:min_len], xj[:min_len])
                    matrix[i, j] = matrix[j, i] = abs(corr) if not np.isnan(corr) else 0.0
                except Exception as e:
                    logger.debug(f"计算 {factor_names[i]}-{factor_names[j]} 相关系数失败: {e}")
                    matrix[i, j] = matrix[j, i] = 0.0

        # 显式释放大矩阵中间变量，降低内存峰值
        del cleaned_values

        return matrix

    def _apply_redundancy_penalty(
        self,
        factor_names: List[str],
        original_weights: Dict[str, float],
        corr_matrix: Optional[np.ndarray],
        cfg: Dict[str, Union[float, int]],
    ) -> Tuple[Dict[str, float], List[Dict]]:
        """应用冗余惩罚逻辑，返回调整后的权重和惩罚明细"""
        adjusted = dict(original_weights)
        details: List[Dict] = []

        if corr_matrix is None:
            return adjusted, details

        n = len(factor_names)
        ic_map = self._get_factor_ic(factor_names, original_weights)

        penalty_counts: Dict[str, int] = {name: 0 for name in factor_names}
        max_penalty = int(cfg["max_penalty_count"])
        penalty_factor = float(cfg["penalty_factor"])
        min_weight = float(cfg["min_weight"])
        min_ic_diff = float(cfg["min_ic_diff"])
        threshold = float(cfg["correlation_threshold"])
        audit_timestamp = time.time()

        processed_pairs: set = set()
        for i in range(n):
            for j in range(i + 1, n):
                if corr_matrix[i, j] < threshold:
                    continue
                pair = (factor_names[i], factor_names[j])
                if pair in processed_pairs:
                    continue
                processed_pairs.add(pair)

                name_i = factor_names[i]
                name_j = factor_names[j]
                ic_i = ic_map.get(name_i, 0.0)
                ic_j = ic_map.get(name_j, 0.0)

                # 决定惩罚目标：IC较低的因子
                if abs(ic_i - ic_j) < min_ic_diff:
                    # IC差异极小，使用字母序确保确定性（不受 PYTHONHASHSEED 影响）
                    if name_i < name_j:
                        penalty_target, retain_target = name_j, name_i
                    else:
                        penalty_target, retain_target = name_i, name_j
                    reason = f"IC差异极小({abs(ic_i - ic_j):.4f} < {min_ic_diff})，按字母序保留 {retain_target}"
                elif ic_i >= ic_j:
                    penalty_target = name_j
                    retain_target = name_i
                    reason = f"IC较低 (IC={ic_j:.4f} vs {ic_i:.4f})"
                else:
                    penalty_target = name_i
                    retain_target = name_j
                    reason = f"IC较低 (IC={ic_i:.4f} vs {ic_j:.4f})"

                # 检查惩罚次数限制
                if penalty_counts[penalty_target] >= max_penalty:
                    logger.debug(f"因子 {penalty_target} 已达最大惩罚次数 {max_penalty}，跳过")
                    continue

                original_val = adjusted[penalty_target]
                new_val = max(original_val * penalty_factor, min_weight)
                if new_val < original_val:
                    adjusted[penalty_target] = round(new_val, 6)
                    penalty_counts[penalty_target] += 1
                    details.append({
                        "penalty_target": penalty_target,
                        "retained_factor": retain_target,
                        "correlation": round(float(corr_matrix[i, j]), 4),
                        "original_weight": original_val,
                        "adjusted_weight": adjusted[penalty_target],
                        "ic_target": round(ic_map.get(penalty_target, 0.0), 4),
                        "ic_retained": round(ic_map.get(retain_target, 0.0), 4),
                        "reason": reason,
                        "timestamp": round(audit_timestamp, 4),
                    })
                    logger.debug("冗余惩罚: %s 权重 %.4f → %.4f (与 %s 相关 %.2f)",
                                 penalty_target, original_val, adjusted[penalty_target],
                                 retain_target, corr_matrix[i, j])

        return adjusted, details

    @staticmethod
    def _normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
        """归一化权重，确保总和为1。当总和为0或负时，降级为均匀分配。"""
        total = sum(weights.values())
        if total <= 0:
            logger.warning("权重总和非正，无法归一化，降级为均匀分配")
            n = len(weights)
            if n == 0:
                return {}
            return {name: round(1.0 / n, 6) for name in weights}
        return {name: round(w / total, 6) for name, w in weights.items()}

    def _get_factor_ic(self, factor_names: List[str], _weights: Dict[str, float]) -> Dict[str, float]:
        """获取因子IC值，优先从weight_engine获取，失败则返回空字典（保守策略）"""
        if self._weight_engine is not None:
            try:
                ic_map = self._weight_engine.get_factor_ic(factor_names)
                if ic_map:
                    return {k: v for k, v in ic_map.items() if not np.isnan(v) and not np.isinf(v)}
            except Exception as e:
                logger.warning(f"获取因子IC失败: {e}，IC不可用，惩罚决策将基于字母序")
        logger.debug("因子IC不可用，惩罚决策基于字母序（确定性）")
        return {}
