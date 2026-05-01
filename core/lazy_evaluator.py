#!/usr/bin/env python3
"""
火种系统 (FireSeed) 惰性求值器
================================
负责：
- 控制因子计算的触发时机，仅在可能出现交易信号时才全量评估所有因子
- 在非触发状态下仅维护少量“基态因子”（OBI, CVD, 流动性等）
- 大幅减少 CPU 负载，尤其是闲置时段
- 支持基于市场状态、评分趋势、时间间隔的多条件判断
- 缓存最近的全量评分结果，供评分卡快速读取
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from config.loader import ConfigLoader
from core.context_isolator import IsolatedDataView

@dataclass
class FactorCache:
    """单次全量评估的缓存"""
    scores: Dict[str, float] = field(default_factory=dict)
    timestamp: float = 0.0
    regime: str = "unknown"


class LazyFactorEvaluator:
    """
    惰性因子求值器。

    工作模式：
    - 每 Tick 都会更新基态因子（OBI, CVD, 价差）
    - 仅在满足“唤醒”条件时计算全部因子并更新评分
    - 唤醒条件：市场状态变化、基态因子显著异常、距上次评估超过最大间隔
    """

    def __init__(self, config: ConfigLoader):
        self.config = config
        # 最大静默间隔（秒），超过此时间即使无信号也强制全量评估
        self.max_silence_sec = config.get("performance.max_eval_interval", 30)
        # 基态因子变化阈值（归一化后的变化量），超过则唤醒
        self.base_state_change_threshold = config.get("performance.base_state_threshold", 0.3)

        # 基态因子当前值（实时更新）
        self.base_factors: Dict[str, float] = {
            "obi": 0.0,
            "cvd_slope": 0.0,
            "spread": 0.0,
            "volume_ratio": 1.0,
        }

        # 上次全量评估的缓存
        self._last_full_eval: FactorCache = FactorCache()
        # 上次全量评估的时间
        self._last_full_time: float = 0.0
        # 上次市场状态
        self._last_regime: str = "unknown"

        # 因子计算器引用（由外部注入，或通过插件管理器获取）
        self._factor_calculator = None

    def set_factor_calculator(self, calculator):
        """注入因子计算器实例，需提供 evaluate_all(ctx, pll, heston, vib) -> Dict[str, float] 方法"""
        self._factor_calculator = calculator

    def update_base_state(self, ctx: IsolatedDataView) -> None:
        """
        更新基态因子（轻量级，每 Tick 调用）。
        """
        ob = ctx.get_orderbook()
        if ob:
            bids = ob.bids
            asks = ob.asks
            if bids and asks:
                bid_vol = sum(v for _, v in bids[:5])
                ask_vol = sum(v for _, v in asks[:5])
                total = bid_vol + ask_vol
                self.base_factors["obi"] = (bid_vol - ask_vol) / total if total > 0 else 0.0
                self.base_factors["spread"] = (asks[0][0] - bids[0][0]) / asks[0][0] if asks[0][0] > 0 else 0.0

        # 成交量比值
        klines = ctx.get_klines(20)
        if len(klines) >= 20:
            avg_vol = np.mean([k.volume for k in klines[:-1]])
            if avg_vol > 0:
                self.base_factors["volume_ratio"] = klines[-1].volume / avg_vol

        # CVD 斜率（简化）
        if len(klines) >= 5:
            # 假设有 CVD 历史（无则置 0）
            pass

    def should_full_evaluate(self, regime: str, ctx: IsolatedDataView) -> bool:
        """
        判断是否应触发全量因子评估。
        :param regime: 当前市场状态字符串
        :param ctx: 隔离数据视图
        :return: True 表示需要全量计算
        """
        now = time.time()

        # 1. 市场状态发生变化
        if regime != self._last_regime:
            self._last_regime = regime
            return True

        # 2. 距上次全量评估超过最大静默间隔
        if now - self._last_full_time > self.max_silence_sec:
            return True

        # 3. 基态因子显著偏离上次全量评估时的水平
        if self._last_full_eval.scores:
            deviation = self._calc_base_deviation()
            if deviation > self.base_state_change_threshold:
                return True

        # 4. 特殊情况：锁相环解锁或频率突变（由外部直接调用 evaluate_all 也可）
        return False

    def _calc_base_deviation(self) -> float:
        """计算当前基态因子与上次全量评估时基态的快照之间的偏差"""
        # 这里简化：比较 OBI 的绝对变化
        last_obi = self._last_full_eval.scores.get("obi_raw", 0.0)
        current_obi = self.base_factors.get("obi", 0.0)
        return abs(current_obi - last_obi)

    def evaluate_all(self, ctx: IsolatedDataView, pll_state,
                     heston_state, vib_state) -> Dict[str, float]:
        """
        执行全量因子计算，返回 {factor_name: score} 字典。
        若因子计算器未注入，则返回空字典。
        """
        if self._factor_calculator is None:
            return {}

        scores = self._factor_calculator.evaluate_all(ctx, pll_state, heston_state, vib_state)
        # 更新缓存
        self._last_full_eval = FactorCache(
            scores=scores.copy(),
            timestamp=time.time(),
            regime=self._last_regime,
        )
        # 同步更新基态快照（记录评估时的 OBI 等）
        scores["obi_raw"] = self.base_factors.get("obi", 0.0)
        self._last_full_time = time.time()
        return scores

    @property
    def last_scores(self) -> Dict[str, float]:
        """返回上一次全量评估的因子得分（供评分卡直接使用）"""
        return self._last_full_eval.scores.copy()

    @property
    def last_regime(self) -> str:
        return self._last_regime

    @property
    def last_full_time(self) -> float:
        return self._last_full_time
