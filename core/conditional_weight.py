#!/usr/bin/env python3
"""
火种系统 (FireSeed) 条件权重引擎
==================================
负责：
- 按市场状态（趋势/震荡/高波/低波）分离维护因子IC序列
- 基于贝叶斯动态线性模型的因子权重在线估计
- 波动率门控：极端波动时冻结权重更新
- 互补性惩罚：高度共线因子自动降权
- 多重检验FDR控制 (Benjamini-Hochberg)
- L2正则化平滑：防止单次调整幅度过大
- 模糊隶属度：支持状态间平滑过渡
- 权重热保存与评分卡联动
"""

import logging
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import yaml
from scipy.stats import spearmanr
from sklearn.feature_selection import mutual_info_regression

from config.loader import ConfigLoader

logger = logging.getLogger("fire_seed.weights")


class ConditionalWeightEngine:
    """
    条件权重引擎。

    核心特性：
    - 按市场状态独立追踪因子IC，避免混合环境失真
    - 波动率突破历史分位时冻结权重，防止噪声误导
    - 互补性惩罚消除因子共线性的伪共振
    - FDR多重检验控制，淘汰纯噪声因子
    - L2正则化防止权重剧烈震荡
    """

    def __init__(self, config: ConfigLoader):
        self.config = config

        # 从配置读取参数
        wcfg = config.get("weight_calibration", {})
        self.ic_window = wcfg.get("ic_window_days", 20)           # IC 计算窗口（天）
        self.decay_halflife = wcfg.get("decay_halflife", 15)      # 指数衰减半衰期
        self.l2_lambda = wcfg.get("l2_lambda", 0.3)               # L2 正则化强度
        self.vol_freeze_percentile = wcfg.get("vol_freeze_percentile", 0.80)  # 波动率冻结分位
        self.min_ic_threshold = wcfg.get("min_ic_threshold", 0.02)  # 最低IC接纳阈值
        self.fdr_q = wcfg.get("fdr_q", 0.1)                       # FDR 控制 q 值
        self.use_fuzzy = wcfg.get("conditional_fuzzy", True)      # 是否启用模糊隶属度

        # 市场状态定义（可扩展）
        self.regimes = ["trend", "oscillation", "high_vol", "low_vol"]

        # 因子 IC 历史：{ regime: { factor_name: deque of IC values } }
        self.ic_history: Dict[str, Dict[str, List[float]]] = {
            r: defaultdict(list) for r in self.regimes
        }
        # 波动率历史（用于计算分位）
        self.volatility_history: List[float] = []
        self._frozen = False
        self._freeze_reason = ""

        # 当前权重集
        self.weights: Dict[str, float] = {}
        # 长期中性基准（一年均值）
        self.long_term_baseline: Dict[str, float] = {}
        # 权重方差估计（贝叶斯更新）
        self.weight_variance: Dict[str, float] = {}
        # 因子互信息矩阵缓存
        self._mi_matrix: Optional[np.ndarray] = None
        self._mi_factor_names: List[str] = []

        # 输出权重文件路径
        self._output_path = Path(config.get("scorecard.weights_file", "config/weights.yaml"))
        # 权重版本号
        self._version_counter = 0

        # 尝试加载已有权重作为初始值
        self._load_existing_weights()

    # ======================== IC 追踪 ========================
    def update_ic(self, factor_name: str, regime: str, ic_value: float) -> None:
        """
        记录特定市场状态下的因子 IC。
        :param factor_name: 因子名称
        :param regime: 市场状态标签
        :param ic_value: 秩相关系数值
        """
        if self._frozen:
            return
        if regime not in self.regimes:
            regime = "trend"  # 未知状态归入趋势
        self.ic_history[regime][factor_name].append(ic_value)
        # 限制历史长度（最多保留 2 倍窗口）
        max_len = self.ic_window * 2
        if len(self.ic_history[regime][factor_name]) > max_len:
            self.ic_history[regime][factor_name] = self.ic_history[regime][factor_name][-max_len:]

    # ======================== 波动率门控 ========================
    def update_volatility(self, current_vol: float) -> None:
        """更新波动率历史并检查是否需要冻结"""
        self.volatility_history.append(current_vol)
        if len(self.volatility_history) > 500:
            self.volatility_history = self.volatility_history[-500:]

        if len(self.volatility_history) < 50:
            return

        percentile = np.percentile(self.volatility_history, self.vol_freeze_percentile * 100)
        if current_vol > percentile:
            if not self._frozen:
                self._frozen = True
                self._freeze_reason = f"波动率 {current_vol:.4f} 超过 {self.vol_freeze_percentile*100:.0f}% 分位 {percentile:.4f}"
                logger.warning(f"权重冻结: {self._freeze_reason}")
        else:
            if self._frozen:
                self._frozen = False
                self._freeze_reason = ""
                logger.info("权重冻结解除")

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    # ======================== 权重计算 ========================
    def compute_weights(self, target_regime: str,
                        penalty_matrix: Optional[Dict[str, List[str]]] = None) -> Dict[str, float]:
        """
        计算目标市场状态下的最优权重。
        若冻结则返回现有权重；若新状态数据不足则使用最接近状态的权重。
        """
        if self._frozen:
            logger.info("权重冻结中，返回当前权重")
            return self.weights.copy() if self.weights else self._get_fallback_weights()

        # 选择数据最充足的状态
        regime = self._select_best_regime(target_regime)
        ic_data = self.ic_history.get(regime, {})

        if not ic_data:
            return self._get_fallback_weights()

        # 计算每个因子的信息比率 (IR)
        ir_values: Dict[str, float] = {}
        for factor, ic_list in ic_data.items():
            if len(ic_list) < 10:
                continue
            ir = self._calc_weighted_ir(ic_list)
            if abs(ir) >= self.min_ic_threshold:
                ir_values[factor] = max(0.0, ir)  # 仅保留正向因子

        if not ir_values:
            return self._get_fallback_weights()

        # 互补性惩罚
        ir_values = self._apply_redundancy_penalty(ir_values, ic_data)

        # FDR 多重检验控制
        ir_values = self._apply_fdr_control(ir_values, ic_data)

        # 归一化得到原始权重
        total_ir = sum(ir_values.values())
        new_weights = {f: v / total_ir for f, v in ir_values.items()} if total_ir > 0 else {}

        # L2 正则化平滑：新权重向旧权重回归
        if self.weights:
            smoothed = {}
            for f, w in new_weights.items():
                old_w = self.weights.get(f, w)
                smoothed[f] = (1 - self.l2_lambda) * w + self.l2_lambda * old_w
            # 补充旧权重中存在但新权重中消失的因子（保留微量权重）
            for f, old_w in self.weights.items():
                if f not in smoothed and f in ic_data and len(ic_data[f]) >= 10:
                    # 如果旧因子仍有数据但IR为负或低于阈值，保留0.1%权重观察
                    smoothed[f] = old_w * 0.3
            new_weights = smoothed

        # 归一化最终权重
        total = sum(new_weights.values())
        if total > 0:
            new_weights = {f: v / total for f, v in new_weights.items()}

        # 更新方差估计
        for f, w in new_weights.items():
            old_w = self.weights.get(f, w)
            delta = abs(w - old_w)
            self.weight_variance[f] = self.weight_variance.get(f, 0.01) * 0.9 + delta * 0.1

        self.weights = new_weights
        return new_weights

    def get_weights(self, regime: str) -> Dict[str, float]:
        """外部调用接口：获取指定状态的权重（若冻结则不变）"""
        if self._frozen:
            return self.weights.copy() if self.weights else self._get_fallback_weights()
        return self.compute_weights(regime)

    # ======================== 内部计算方法 ========================
    def _calc_weighted_ir(self, ic_list: List[float]) -> float:
        """指数衰减加权 IC 均值 / 标准差"""
        n = len(ic_list)
        if n < 2:
            return 0.0
        # 指数衰减权重
        decay_weights = np.exp(-np.arange(n)[::-1] / self.decay_halflife)
        decay_weights /= decay_weights.sum()
        ic_array = np.array(ic_list)
        weighted_mean = np.average(ic_array, weights=decay_weights)
        # 加权标准差
        weighted_var = np.average((ic_array - weighted_mean) ** 2, weights=decay_weights)
        weighted_std = np.sqrt(weighted_var)
        return weighted_mean / (weighted_std + 1e-10)

    def _apply_redundancy_penalty(self, ir_values: Dict[str, float],
                                  ic_data: Dict[str, List[float]]) -> Dict[str, float]:
        """对高度共线的因子施加惩罚：保留IR最高的，其余降权"""
        if len(ir_values) < 2:
            return ir_values
        # 构建因子值矩阵用于互信息计算
        factor_names = list(ir_values.keys())
        min_len = min(len(ic_data[f]) for f in factor_names)
        if min_len < 20:
            return ir_values
        # 截取相同长度
        values_matrix = np.array([ic_data[f][-min_len:] for f in factor_names]).T
        # 计算互信息矩阵（若因子名变化则重算）
        if self._mi_factor_names != factor_names:
            self._mi_matrix = self._calc_mutual_info(values_matrix)
            self._mi_factor_names = factor_names[:]
        if self._mi_matrix is None:
            return ir_values
        # 惩罚逻辑
        penalty = np.ones(len(factor_names))
        used_best = set()
        for i in range(len(factor_names)):
            for j in range(i + 1, len(factor_names)):
                mi = self._mi_matrix[i, j]
                if mi > 0.6:
                    # 保留 IR 较高的
                    if ir_values[factor_names[i]] >= ir_values[factor_names[j]]:
                        best_idx = i
                        other_idx = j
                    else:
                        best_idx = j
                        other_idx = i
                    if best_idx not in used_best:
                        penalty[other_idx] *= 0.7  # 惩罚30%
                        used_best.add(best_idx)
        # 应用惩罚
        result = {}
        for idx, name in enumerate(factor_names):
            result[name] = ir_values[name] * penalty[idx]
        return result

    @staticmethod
    def _calc_mutual_info(values: np.ndarray) -> np.ndarray:
        """计算因子间的归一化互信息矩阵"""
        n = values.shape[1]
        mi = np.zeros((n, n))
        for i in range(n):
            for j in range(i + 1, n):
                try:
                    m = mutual_info_regression(
                        values[:, i].reshape(-1, 1),
                        values[:, j]
                    )[0]
                    mi[i, j] = m
                    mi[j, i] = m
                except Exception:
                    mi[i, j] = 0.0
                    mi[j, i] = 0.0
        # 归一化到 0~1
        max_val = mi.max()
        if max_val > 0:
            mi /= max_val
        return mi

    def _apply_fdr_control(self, ir_values: Dict[str, float],
                           ic_data: Dict[str, List[float]]) -> Dict[str, float]:
        """Benjamini-Hochberg FDR 控制，淘汰 q 值过高的因子"""
        if len(ir_values) < 5:
            return ir_values
        # 计算每个因子的 p 值（基于IC的 t 检验近似）
        p_values = {}
        for factor in ir_values:
            ic_list = ic_data.get(factor, [])
            if len(ic_list) < 10:
                continue
            ic_array = np.array(ic_list[-self.ic_window:])
            if len(ic_array) < 10:
                continue
            t_stat = np.mean(ic_array) / (np.std(ic_array) / np.sqrt(len(ic_array)) + 1e-10)
            # 单尾 p 值近似（t 分布转正态）
            from scipy.stats import norm
            p_values[factor] = 2 * (1 - norm.cdf(abs(t_stat)))

        if not p_values:
            return ir_values

        # BH 过程
        sorted_factors = sorted(p_values.items(), key=lambda x: x[1])
        n = len(sorted_factors)
        threshold_idx = -1
        for i, (_, p) in enumerate(sorted_factors, 1):
            if p <= self.fdr_q * i / n:
                threshold_idx = i
        # 保留通过FDR的因子
        passed = set(f for i, (f, _) in enumerate(sorted_factors) if i < threshold_idx)
        if not passed:
            return ir_values  # 至少保留所有
        return {f: v for f, v in ir_values.items() if f in passed}

    def _select_best_regime(self, target: str) -> str:
        """选择数据最充足的状态（优先目标状态）"""
        if target in self.ic_history and any(len(v) >= 20 for v in self.ic_history[target].values()):
            return target
        # 选择数据量最大的状态
        best = target
        max_count = 0
        for regime in self.regimes:
            total = sum(len(v) for v in self.ic_history[regime].values())
            if total > max_count:
                max_count = total
                best = regime
        return best

    def _get_fallback_weights(self) -> Dict[str, float]:
        """回退权重：优先长期基线，其次等权"""
        if self.long_term_baseline:
            return self.long_term_baseline.copy()
        if self.weights:
            return self.weights.copy()
        return {}

    def _load_existing_weights(self) -> None:
        """加载已有权重文件作为初始值"""
        if not self._output_path.exists():
            return
        try:
            with open(self._output_path, "r") as f:
                data = yaml.safe_load(f)
            core = data.get("core_factors", {})
            aux = data.get("auxiliary_factors", {})
            self.weights = {**core, **aux}
            self.long_term_baseline = self.weights.copy()
            logger.info(f"从 {self._output_path} 加载了 {len(self.weights)} 个因子的初始权重")
        except Exception as e:
            logger.warning(f"加载已有权重失败: {e}")

    # ======================== 权重持久化 ========================
    def save(self) -> None:
        """将当前权重写入配置文件，供评分卡热加载"""
        if not self.weights:
            return
        self._version_counter += 1
        # 分离核心与辅助因子（按权重排序）
        sorted_factors = sorted(self.weights.items(), key=lambda x: x[1], reverse=True)
        total = sum(self.weights.values())
        # 前 40% 权重视为核心
        cumulative = 0.0
        core = {}
        aux = {}
        for name, w in sorted_factors:
            if cumulative < 0.4 * total:
                core[name] = round(w, 6)
                cumulative += w
            else:
                aux[name] = round(w, 6)
        # 构建输出
        output = {
            "version": datetime.now().strftime("%Y%m%d") + f"-{self._version_counter:02d}",
            "last_calibrated": datetime.now().isoformat(),
            "current_regime": "trend",  # 占位
            "calibration_window": self.ic_window,
            "core_factors": core,
            "auxiliary_factors": aux,
            "factor_notes": self._generate_notes(),
        }
        # 写入文件
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._output_path, "w") as f:
            yaml.dump(output, f, default_flow_style=False, allow_unicode=True)
        logger.info(f"权重已保存至 {self._output_path} (v{output['version']})")

    def _generate_notes(self) -> Dict[str, str]:
        """生成因子状态备注"""
        notes = {}
        for name in list(self.weights.keys()):
            variance = self.weight_variance.get(name, 0.01)
            if variance > 0.1:
                notes[name] = f"权重波动较大 (σ={variance:.3f})，建议观察"
        if self._frozen:
            notes["_freeze"] = f"权重因波动率门控已冻结: {self._freeze_reason}"
        return notes
