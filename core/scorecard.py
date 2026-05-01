#!/usr/bin/env python3
"""
火种系统 (FireSeed) 动态评分卡模块
====================================
将因子得分 (0-1) 与动态权重结合，输出 0-100 综合评分。
核心职责：
- 加载权重配置（支持热重载）
- 计算加权得分并映射到 0-100 区间
- 根据当前模式（激进/稳健）提供入场阈值
- 缓存最近评分供惰性求值器使用
"""

import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import yaml

from config.loader import ConfigLoader

logger = logging.getLogger("fire_seed.scorecard")


class DynamicScoreCard:
    """动态加权评分引擎。

    采用层叠架构：
    1. 核心因子 (权重约60%) —— 趋势强度、VIB压力、市场状态适配
    2. 辅助因子 (权重约40%) —— 微观结构、行为信号、环境因子
    """

    def __init__(self, config: ConfigLoader):
        self.config = config
        # 权重配置路径（可被条件权重引擎更新）
        self._weights_path = Path(config.get("scorecard.weights_file", "config/weights.yaml"))
        self._weights: Dict[str, float] = {}
        self._core_factors: Dict[str, float] = {}
        self._aux_factors: Dict[str, float] = {}

        # 入场阈值
        self._load_mode_thresholds()

        # 最近一次评分缓存
        self._last_score: float = 50.0
        self._last_contributions: Dict[str, float] = {}

        # 初始加载权重
        self.reload_weights()

    # ---- 权重加载与热重载 ----
    def reload_weights(self) -> None:
        """重新加载权重配置文件（支持文件系统监听触发热重载）"""
        if not self._weights_path.exists():
            logger.warning(f"权重文件不存在: {self._weights_path}，使用默认等权。")
            self._set_default_weights()
            return

        try:
            with open(self._weights_path, "r") as f:
                data = yaml.safe_load(f)
            self._core_factors = data.get("core_factors", {})
            self._aux_factors = data.get("auxiliary_factors", {})
            self._weights = {**self._core_factors, **self._aux_factors}
            logger.info(f"权重已加载，核心因子: {len(self._core_factors)}，辅助因子: {len(self._aux_factors)}")
            # 验证权重和是否为 1.0
            total = sum(self._weights.values())
            if abs(total - 1.0) > 0.01:
                logger.warning(f"权重和 {total:.3f} 偏离 1.0，将自动归一化。")
                self._normalize_weights()
        except Exception as e:
            logger.error(f"权重加载失败: {e}")
            self._set_default_weights()

    def _set_default_weights(self) -> None:
        """后备默认权重（简单等权）"""
        self._core_factors = {}
        self._aux_factors = {}
        self._weights = {}

    def _normalize_weights(self) -> None:
        if not self._weights:
            return
        total = sum(self._weights.values())
        if total == 0:
            return
        factor = 1.0 / total
        for k in self._weights:
            self._weights[k] *= factor
        # 分离核心与辅助
        self._core_factors = {k: v for k, v in self._weights.items() if k in self._core_factors}
        self._aux_factors = {k: v for k, v in self._weights.items() if k in self._aux_factors}

    # ---- 模式阈值 ----
    def _load_mode_thresholds(self) -> None:
        mode = self.config.get("system.strategy_mode", "moderate")
        if mode == "aggressive":
            self.threshold_long = self.config.get("strategy.aggressive.entry.threshold", 58)
            self.threshold_short = self.config.get("strategy.aggressive.entry.short_threshold", 42)
        else:
            self.threshold_long = self.config.get("strategy.moderate.entry.threshold", 65)
            self.threshold_short = self.config.get("strategy.moderate.entry.short_threshold", 35)
        logger.info(f"评分卡阈值: 做多>{self.threshold_long}, 做空<{self.threshold_short}")

    # ---- 核心计算 ----
    def compute(self, factor_scores: Dict[str, float],
                external_weights: Optional[Dict[str, float]] = None) -> float:
        """
        计算综合评分。
        :param factor_scores: 因子名 -> 得分 (0-1 或标准化后的 Z 值)
        :param external_weights: 外部传入的权重（如条件权重引擎实时输出），不传则使用文件权重。
        :return: 0-100 综合评分（50 为中性）
        """
        weights = external_weights if external_weights is not None else self._weights

        if not weights or not factor_scores:
            self._last_score = 50.0
            self._last_contributions = {}
            return 50.0

        score = 0.0
        contributions = {}
        used_weight_sum = 0.0

        for name, raw_value in factor_scores.items():
            # 将原始得分映射到 -1..1 (假设 raw 已经接近标准化，但做安全裁剪)
            clamped = max(-1.0, min(1.0, float(raw_value)))
            w = weights.get(name, 0.0)
            if w == 0.0:
                continue
            score += w * clamped
            contributions[name] = w * clamped
            used_weight_sum += abs(w)

        # 归一化到 50 分中性基准，满分 100
        if used_weight_sum > 0:
            normalized = score / used_weight_sum
        else:
            normalized = 0.0

        # 映射：normalized ∈ [-1,1] → score ∈ [0,100]
        final = 50.0 + normalized * 50.0
        final = max(0.0, min(100.0, final))

        self._last_score = final
        self._last_contributions = contributions
        return final

    @property
    def last_score(self) -> float:
        return self._last_score

    @property
    def last_contributions(self) -> Dict[str, float]:
        return self._last_contributions

    # ---- 信号判断 ----
    def is_long_signal(self, score: Optional[float] = None) -> bool:
        s = score if score is not None else self._last_score
        return s >= self.threshold_long

    def is_short_signal(self, score: Optional[float] = None) -> bool:
        s = score if score is not None else self._last_score
        return s <= self.threshold_short

    def get_direction(self, score: Optional[float] = None) -> str:
        s = score if score is not None else self._last_score
        if self.is_long_signal(s):
            return "LONG"
        if self.is_short_signal(s):
            return "SHORT"
        return "NEUTRAL"
