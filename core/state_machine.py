#!/usr/bin/env python3
"""
火种系统 (FireSeed) 交易状态机
================================
根据感知层输出判断当前市场状态：
- 趋势 (trend) / 震荡 (oscillation) / 反转 (reversal) / 极端 (extreme)
- 提供 Choppiness Index 计算
- 提供多因子联合判定接口
"""

import logging
from typing import Dict, Optional, Tuple

import numpy as np

from config.loader import ConfigLoader
from core.context_isolator import IsolatedDataView

logger = logging.getLogger("fire_seed.state_machine")


class TradingStateMachine:
    """市场状态判定器，基于锁相环、粒子滤波、VIB 及价格序列。"""

    def __init__(self, config: ConfigLoader):
        self.config = config
        # 可配置阈值
        self.ci_oscillation_threshold = config.get("state_machine.ci_oscillation", 55.0)
        self.ci_trend_threshold = config.get("state_machine.ci_trend", 40.0)
        self.pll_freq_trend_min = config.get("state_machine.pll_freq_trend_min", 0.02)
        self.hurst_bf_trend_min = config.get("state_machine.hurst_bf_trend_min", 10.0)
        self.hurst_bf_oscillation_max = config.get("state_machine.hurst_bf_oscillation_max", 3.0)
        self.vib_reversal_risk_high = config.get("state_machine.vib_reversal_risk_high", 0.7)
        self.extreme_amplitude_pct = config.get("state_machine.extreme_amplitude_pct", 5.0)

        # 历史状态缓存
        self._last_regime = "unknown"

    # ======================== 主入口 ========================
    def determine_regime(
        self,
        pll_state,
        heston_state,
        vib_state,
        ctx: IsolatedDataView,
    ) -> str:
        """
        综合多源信息判断当前市场状态。
        返回: trend / oscillation / reversal / extreme / unknown
        """
        # 1. 计算 Choppiness Index
        ci = self.choppiness_index(ctx)

        # 2. 震荡判定
        is_osc = self._is_oscillation(pll_state, vib_state, ci, heston_state.hurst_bf)

        # 3. 极端判定 (基于振幅熔断)
        if self._is_extreme(ctx):
            self._last_regime = "extreme"
            return "extreme"

        # 4. 反转判定
        if self._is_reversal(pll_state, vib_state):
            self._last_regime = "reversal"
            return "reversal"

        # 5. 趋势判定
        if self._is_trending(pll_state, heston_state, ci):
            self._last_regime = "trend"
            return "trend"

        # 6. 震荡
        if is_osc:
            self._last_regime = "oscillation"
            return "oscillation"

        # 7. 未知（中性）
        self._last_regime = "unknown"
        return "unknown"

    # ======================== 子判定 ========================
    def _is_trending(self, pll_state, heston_state, ci: float) -> bool:
        """趋势判定条件"""
        # 锁相环锁定且频率足够高
        freq_ok = pll_state.locked and abs(pll_state.frequency) > self.pll_freq_trend_min
        # 赫斯特指数贝叶斯因子强持久性
        hurst_ok = heston_state.hurst_bf > self.hurst_bf_trend_min
        # Choppiness 较低
        ci_ok = ci < self.ci_trend_threshold
        return freq_ok and hurst_ok and ci_ok

    def _is_oscillation(self, pll_state, vib_state, ci: float, hurst_bf: float) -> bool:
        """震荡判定条件"""
        # PLL 未锁定
        pll_unlocked = not pll_state.locked
        # CI 高
        ci_high = ci > self.ci_oscillation_threshold
        # VIB 波动率区间偏高
        vib_range_ok = vib_state.volatility_regime > 0.5
        # 赫斯特贝叶斯因子接近 1 (反持久)
        hurst_ok = hurst_bf < self.hurst_bf_oscillation_max
        return pll_unlocked and ci_high and vib_range_ok and hurst_ok

    def _is_reversal(self, pll_state, vib_state) -> bool:
        """反转预警：锁相环频率过零 + VIB 反转风险高"""
        # 频率接近0且相位误差较大
        near_zero = abs(pll_state.frequency) < 0.005
        vib_reversal = vib_state.reversal_risk > self.vib_reversal_risk_high
        return near_zero and vib_reversal

    def _is_extreme(self, ctx: IsolatedDataView) -> bool:
        """极端行情判定：最近一根K线振幅超过阈值"""
        klines = ctx.get_klines(1)
        if not klines:
            return False
        k = klines[0]
        if k.high and k.low and k.open > 0:
            amplitude = (k.high - k.low) / k.open
            return amplitude > self.extreme_amplitude_pct / 100.0
        return False

    # ======================== Choppiness Index ========================
    def choppiness_index(self, ctx: IsolatedDataView, period: int = 14) -> float:
        """
        计算 Choppiness Index (0-100)。
        CI 越高，市场越震荡；越低越趋势。
        """
        klines = ctx.get_klines(period + 1)
        if len(klines) < period:
            return 50.0

        highs = [k.high for k in klines]
        lows = [k.low for k in klines]
        closes = [k.close for k in klines]

        # 真实波幅之和
        tr_sum = 0.0
        for i in range(1, len(klines)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            tr_sum += tr

        period_high = max(highs)
        period_low = min(lows)
        range_total = period_high - period_low

        if range_total <= 0:
            return 50.0

        ci = 100.0 * np.log10(tr_sum / range_total) / np.log10(period)
        return np.clip(ci, 0.0, 100.0)

    # ======================== 辅助查询 ========================
    @property
    def last_regime(self) -> str:
        return self._last_regime

    def is_oscillation(self, pll_state, vib_state, ci=None, hurst_bf=None) -> bool:
        """便捷查询：当前是否震荡"""
        if ci is None or hurst_bf is None:
            return self._last_regime == "oscillation"
        return self._is_oscillation(pll_state, vib_state, ci, hurst_bf)

    def is_trending(self) -> bool:
        return self._last_regime == "trend"
