"""
火种系统 · 因子冗余惩罚器 (RedundancyPenalty)

核心职责：
1. 计算因子两两之间的非线性冗余度（基于互信息或距离相关系数），识别高度共线的因子对。
2. 根据冗余度矩阵，对每个因子生成一个独立的惩罚系数（0~1），用于条件权重引擎的权重调节。
3. 维护冗余状态的历史快照，支持异常回退和审计追踪。

外部依赖（真实模块接口）：
- 无外部模块依赖。所有计算基于传入的标准化因子数值序列。

接口契约：
- compute_penalty(factor_values: Dict[str, np.ndarray]) -> Dict[str, Any]
  输出字典固定包含 "penalty_map" (Dict[str, float]), "redundancy_matrix" (Dict[str, Dict[str, float]]), "reason" (str), "warnings" (List[str])
- get_last_penalty_map() -> Dict[str, float]
  输出最近一次计算的惩罚系数快照，用于降级恢复。
- health_check() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "message" (str)

异常与降级：
- 当 factor_values 中因子数量 < 2 时，返回全 1.0 的惩罚系数（即无惩罚），并记录 WARNING 日志。
- 当互信息计算因数据量不足或数值异常而失败时，对该因子对之间的惩罚系数降级为 1.0（无惩罚），并记录具体错误。
- 若输入数据包含 NaN/Inf，自动清洗并记录 WARNING；若某因子有效样本低于最低阈值，该因子不参与冗余计算，惩罚系数设为 1.0。
- 若所有数据均无效，返回全 1.0 惩罚，并记录 CRITICAL 日志。

资源管理：
- 本模块不持有任何需要手动释放的资源。
- 内部状态使用轻量级锁保护，确保多线程查询安全。
"""

import logging
import time
import threading
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
from sklearn.feature_selection import mutual_info_regression

logger = logging.getLogger(__name__)


class RedundancyPenalty:
    """因子冗余惩罚器：基于互信息检测因子共线性并施加惩罚"""

    # 类常量（默认配置，附带单位与取值范围注释）
    DEFAULT_REDUNDANCY_THRESHOLD = 0.65  # 冗余判定阈值，无量纲，取值范围 [0.5, 0.95]
    DEFAULT_PENALTY_STRENGTH = 0.7       # 冗余惩罚强度，无量纲，取值范围 [0.3, 1.0]
    MIN_SAMPLES_FOR_MI = 50              # 互信息计算所需最小样本数，个，取值范围 [20, 500]
    MI_NEIGHBORS = 3                     # 互信息估计的邻居数，个，取值范围 [1, 10]
    HEALTH_SAMPLE_FACTORS = 5            # 健康检查生成的模拟因子数，个，取值范围 [2, 10]
    HEALTH_SAMPLE_SIZE = 200             # 健康检查生成的模拟样本数，个，取值范围 [100, 1000]
    MAX_NAN_RATIO = 0.3                  # 序列中允许的最大缺失值比例，无量纲，取值范围 [0.0, 0.5]
    MIN_VALID_FACTOR_SAMPLES = 30        # 因子参与计算的最小有效样本数，个，取值范围 [20, 200]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        初始化冗余惩罚器。
        所有参数优先从配置字典读取，缺失时使用类常量作为安全默认值。
        对传入配置执行严格边界校验，非法值自动回退至默认值。
        """
        cfg = config or {}
        self._redundancy_threshold = self._validate_float(
            cfg.get("redundancy_threshold", self.DEFAULT_REDUNDANCY_THRESHOLD),
            0.5, 0.95, "redundancy_threshold"
        )
        self._penalty_strength = self._validate_float(
            cfg.get("penalty_strength", self.DEFAULT_PENALTY_STRENGTH),
            0.3, 1.0, "penalty_strength"
        )
        self._min_samples = self._validate_int(
            cfg.get("min_samples_for_mi", self.MIN_SAMPLES_FOR_MI),
            20, 500, "min_samples_for_mi"
        )
        self._mi_neighbors = self._validate_int(
            cfg.get("mi_neighbors", self.MI_NEIGHBORS),
            1, 10, "mi_neighbors"
        )
        self._max_nan_ratio = self._validate_float(
            cfg.get("max_nan_ratio", self.MAX_NAN_RATIO),
            0.0, 0.5, "max_nan_ratio"
        )
        self._min_valid_samples = self._validate_int(
            cfg.get("min_valid_factor_samples", self.MIN_VALID_FACTOR_SAMPLES),
            20, 200, "min_valid_factor_samples"
        )

        # 内部状态与锁
        self._lock = threading.Lock()
        self._last_penalty_map: Dict[str, float] = {}
        self._last_redundancy_matrix: Dict[str, Dict[str, float]] = {}
        self._last_update_time: float = 0.0
        self._metrics: Dict[str, Any] = {
            "total_calculations": 0,
            "avg_penalty": 0.0,
            "avg_redundant_pairs": 0.0
        }

        logger.info(
            f"RedundancyPenalty 初始化完成: "
            f"threshold={self._redundancy_threshold:.2f}, "
            f"strength={self._penalty_strength:.2f}, "
            f"min_samples={self._min_samples}, "
            f"neighbors={self._mi_neighbors}"
        )

    # ────────────────────────── 配置校验 ──────────────────────────
    @staticmethod
    def _validate_int(value: Any, low: int, high: int, name: str) -> int:
        """校验整数配置在边界内，否则返回默认值并警告"""
        try:
            v = int(value)
            if low <= v <= high:
                return v
            logger.warning(f"配置 {name}={value} 超出范围 [{low},{high}]，使用默认值")
        except (ValueError, TypeError):
            logger.warning(f"配置 {name}={value} 无效，使用默认值")
        return getattr(RedundancyPenalty, name.upper(), None) or low

    @staticmethod
    def _validate_float(value: Any, low: float, high: float, name: str) -> float:
        """校验浮点配置在边界内，否则返回默认值并警告"""
        try:
            v = float(value)
            if low <= v <= high:
                return v
            logger.warning(f"配置 {name}={value} 超出范围 [{low},{high}]，使用默认值")
        except (ValueError, TypeError):
            logger.warning(f"配置 {name}={value} 无效，使用默认值")
        return getattr(RedundancyPenalty, name.upper(), None) or low

    # ────────────────────────── 公共接口 ──────────────────────────
    def compute_penalty(self, factor_values: Dict[str, np.ndarray]) -> Dict[str, Any]:
        """
        计算因子冗余惩罚系数。
        
        参数:
            factor_values: 键为因子名，值为该因子的标准化数值序列（一维 numpy 数组）。
        
        返回:
            标准化字典，包含惩罚映射、冗余矩阵、决策原因和警告。
        """
        warnings: List[str] = []

        # 1. 数据清洗
        clean_values, clean_warnings = self._clean_factor_data(factor_values)
        warnings.extend(clean_warnings)

        factor_names = list(clean_values.keys())
        n_factors = len(factor_names)

        # 2. 边界校验：因子数量不足
        if n_factors < 2:
            reason = f"因子数量不足 ({n_factors}<2)，跳过冗余计算，返回无惩罚"
            logger.debug(reason)
            penalty_map = {name: 1.0 for name in factor_names}
            self._update_state(penalty_map, {})
            return {
                "penalty_map": penalty_map,
                "redundancy_matrix": {},
                "reason": reason,
                "warnings": warnings
            }

        # 3. 样本量校验
        sample_sizes = {name: len(arr) for name, arr in clean_values.items()}
        min_samples = min(sample_sizes.values())
        if min_samples < self._min_valid_samples:
            warn_msg = (
                f"最小样本量 ({min_samples}) 低于要求 ({self._min_valid_samples})，"
                f"冗余惩罚可靠性下降"
            )
            logger.warning(warn_msg)
            warnings.append(warn_msg)

        # 4. 计算互信息矩阵
        mi_matrix, mi_warnings = self._compute_mutual_information_matrix(clean_values, factor_names)
        warnings.extend(mi_warnings)

        # 5. 根据冗余度生成惩罚系数
        penalty_map = self._derive_penalty_map(mi_matrix, factor_names)

        # 6. 构建冗余展示矩阵（仅超阈值的因子对）
        redundancy_display = self._build_redundancy_display(mi_matrix, factor_names)

        # 7. 更新内部状态
        self._update_state(penalty_map, redundancy_display)

        reason = (
            f"冗余惩罚计算完成，{n_factors} 个因子参与评估，"
            f"阈值={self._redundancy_threshold:.2f}，"
            f"强度={self._penalty_strength:.2f}"
        )
        logger.info(reason)

        return {
            "penalty_map": penalty_map,
            "redundancy_matrix": redundancy_display,
            "reason": reason,
            "warnings": warnings
        }

    def get_last_penalty_map(self) -> Dict[str, float]:
        """获取最近一次计算的惩罚系数快照，线程安全"""
        with self._lock:
            return self._last_penalty_map.copy()

    def get_last_redundancy_matrix(self) -> Dict[str, Dict[str, float]]:
        """获取最近一次计算的冗余矩阵快照，线程安全"""
        with self._lock:
            return {k: dict(v) for k, v in self._last_redundancy_matrix.items()}

    def get_metrics(self) -> Dict[str, Any]:
        """获取累积监控指标"""
        with self._lock:
            return dict(self._metrics)

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检：使用模拟数据运行完整计算流程，验证边界处理。"""
        try:
            rng = np.random.RandomState(42)
            factor_names = [f"health_factor_{i}" for i in range(cls.HEALTH_SAMPLE_FACTORS)]
            factor_values = {
                name: rng.randn(cls.HEALTH_SAMPLE_SIZE) for name in factor_names
            }
            # 构造一对高度相关的因子
            factor_values[factor_names[0]] = (
                factor_values[factor_names[1]] + rng.randn(cls.HEALTH_SAMPLE_SIZE) * 0.1
            )
            # 添加含 NaN 的因子
            dirty = rng.randn(cls.HEALTH_SAMPLE_SIZE)
            dirty[0] = np.nan
            factor_values["health_nan"] = dirty

            instance = cls()
            result = instance.compute_penalty(factor_values)

            # 验证返回字典完整性
            for key in ("penalty_map", "redundancy_matrix", "reason", "warnings"):
                if key not in result:
                    return {"status": "error", "message": f"缺少键: {key}"}

            # 验证惩罚系数在有效范围内
            for name, penalty in result["penalty_map"].items():
                if not 0.0 <= penalty <= 1.0:
                    return {"status": "error", "message": f"因子 {name} 惩罚系数 {penalty} 越界"}

            # 验证高度相关的两个因子被惩罚
            penalty_0 = result["penalty_map"].get(factor_names[0], 1.0)
            penalty_1 = result["penalty_map"].get(factor_names[1], 1.0)
            if penalty_0 >= 1.0 and penalty_1 >= 1.0:
                return {"status": "error", "message": "高度相关因子未被正确惩罚"}

            # 测试空输入
            empty_result = instance.compute_penalty({})
            if empty_result.get("penalty_map") is None:
                return {"status": "error", "message": "空输入处理失败"}

            # 测试单因子输入
            single_result = instance.compute_penalty({"single": rng.randn(100)})
            if single_result["penalty_map"].get("single") != 1.0:
                return {"status": "error", "message": "单因子应返回无惩罚"}

            return {"status": "ok", "message": "健康检查通过"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _clean_factor_data(self, factor_values: Dict[str, np.ndarray]) -> Tuple[Dict[str, np.ndarray], List[str]]:
        """
        清洗因子数据：移除 NaN/Inf，过滤样本过少的因子。
        返回有效数据字典和警告列表。
        """
        clean = {}
        warnings = []
        for name, arr in factor_values.items():
            mask = np.isfinite(arr)
            valid_ratio = mask.sum() / max(len(arr), 1)
            if valid_ratio < 1.0 - self._max_nan_ratio:
                warn = f"因子 {name} 缺失值过多 ({1-valid_ratio:.1%})，跳过该因子"
                logger.warning(warn)
                warnings.append(warn)
                continue
            if not np.all(mask):
                arr = arr[mask]
                warnings.append(f"因子 {name} 存在缺失值，已剔除")
            if len(arr) < self._min_valid_samples:
                warn = f"因子 {name} 有效样本 ({len(arr)}) 低于最低阈值 ({self._min_valid_samples})，跳过"
                logger.warning(warn)
                warnings.append(warn)
                continue
            clean[name] = arr
        return clean, warnings

    def _compute_mutual_information_matrix(
        self,
        factor_values: Dict[str, np.ndarray],
        factor_names: List[str]
    ) -> Tuple[Dict[str, Dict[str, float]], List[str]]:
        """
        计算因子间的互信息矩阵。
        
        返回:
            mi_matrix: 嵌套字典，mi_matrix[name_a][name_b] 为因子 a 对因子 b 的归一化互信息。
            warnings: 计算过程中产生的警告列表。
        """
        n = len(factor_names)
        mi_matrix: Dict[str, Dict[str, float]] = {name: {} for name in factor_names}
        warnings = []

        # 检查样本量
        min_len = min(len(arr) for arr in factor_values.values())
        if min_len < self._min_samples:
            warn_msg = (
                f"实际样本量 ({min_len}) 低于互信息计算所需最小样本量 ({self._min_samples})，"
                f"计算结果可能不稳定"
            )
            logger.warning(warn_msg)
            warnings.append(warn_msg)

        # 构建二维数组 (样本数 x 因子数)，对齐到最短长度
        min_len = min(len(arr) for arr in factor_values.values())
        data = np.column_stack([factor_values[name][:min_len] for name in factor_names])

        for i in range(n):
            for j in range(i + 1, n):
                name_a, name_b = factor_names[i], factor_names[j]
                try:
                    # 计算互信息
                    mi = mutual_info_regression(
                        data[:, i].reshape(-1, 1),
                        data[:, j],
                        n_neighbors=self._mi_neighbors,
                        random_state=42
                    )[0]
                except Exception as e:
                    warn = f"因子对 ({name_a}, {name_b}) 互信息计算失败: {e}，降级为无冗余"
                    logger.warning(warn)
                    warnings.append(warn)
                    mi = 0.0

                # 归一化到 [0, 1]，用 tanh 压制极端值，并确保对称
                normalized_mi = float(np.tanh(abs(mi)))
                mi_matrix[name_a][name_b] = normalized_mi
                mi_matrix[name_b][name_a] = normalized_mi

            # 对角线为 1.0
            mi_matrix[factor_names[i]][factor_names[i]] = 1.0

        return mi_matrix, warnings

    def _derive_penalty_map(
        self,
        mi_matrix: Dict[str, Dict[str, float]],
        factor_names: List[str]
    ) -> Dict[str, float]:
        """
        根据互信息矩阵生成每个因子的惩罚系数。
        惩罚逻辑：对每个因子，找出与它最冗余的另一个因子，若冗余度 > 阈值，则施加惩罚。
        惩罚系数 = 1.0 - penalty_strength * (冗余度 - 阈值) / (1.0 - 阈值)。
        """
        penalty_map: Dict[str, float] = {}
        for name in factor_names:
            max_redundancy = max(
                (mi_matrix[name][other] for other in factor_names if other != name),
                default=0.0
            )
            if max_redundancy > self._redundancy_threshold:
                excess = max_redundancy - self._redundancy_threshold
                penalty = 1.0 - self._penalty_strength * excess / (1.0 - self._redundancy_threshold)
                penalty = max(0.0, min(1.0, penalty))
            else:
                penalty = 1.0
            penalty_map[name] = penalty
        return penalty_map

    def _build_redundancy_display(
        self,
        mi_matrix: Dict[str, Dict[str, float]],
        factor_names: List[str]
    ) -> Dict[str, Dict[str, float]]:
        """
        构建仅包含超阈值冗余关系的展示矩阵，供前端热力图使用。
        """
        display = {}
        for i, name_a in enumerate(factor_names):
            row = {}
            for j, name_b in enumerate(factor_names):
                if i < j:
                    mi = mi_matrix[name_a][name_b]
                    if mi >= self._redundancy_threshold:
                        row[name_b] = round(mi, 4)
            if row:
                display[name_a] = row
        return display

    def _update_state(self, penalty_map: Dict[str, float], redundancy_matrix: Dict[str, Dict[str, float]]) -> None:
        """更新内部状态和监控指标，线程安全"""
        with self._lock:
            self._last_penalty_map = penalty_map.copy()
            self._last_redundancy_matrix = {k: dict(v) for k, v in redundancy_matrix.items()}
            self._last_update_time = time.time()

            # 更新监控指标
            avg_penalty = sum(penalty_map.values()) / max(len(penalty_map), 1)
            redundant_pairs = sum(len(v) for v in redundancy_matrix.values())
            n = self._metrics["total_calculations"]
            old_avg_pen = self._metrics["avg_penalty"]
            old_avg_pairs = self._metrics["avg_redundant_pairs"]
            self._metrics["total_calculations"] += 1
            self._metrics["avg_penalty"] = old_avg_pen + (avg_penalty - old_avg_pen) / (n + 1)
            self._metrics["avg_redundant_pairs"] = (
                old_avg_pairs + (redundant_pairs - old_avg_pairs) / (n + 1)
      )
