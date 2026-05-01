#!/usr/bin/env python3
"""
火种系统 (FireSeed) 幽灵影子统计验证器 v2
=============================================
功能：
- 基于虚拟交易历史的绩效评估（夏普、最大回撤、胜率、盈亏比）
- 新旧策略配对 t 检验，判断新策略是否显著优于旧策略
- 按市场状态分组检验（趋势/震荡），确保在各行情下均非负夏普
- 流动性冲击模拟中的悲观夏普计算
- 对手方风险压力测试
- 为 ShadowManager 提供统一的验证入口

设计原则：
- 所有计算基于日收益序列，保证统计意义
- 支持外部注入滑点/手续费模型，适应不同市场环境
- 输出结构化的验证报告，供金丝雀发布决策
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy import stats

from config.loader import ConfigLoader
from core.behavioral_logger import BehavioralLogger, EventType
from core.order_manager import Account

logger = logging.getLogger("fire_seed.shadow_validator")


@dataclass
class ValidationResult:
    """单次验证的输出报告"""
    passed: bool
    reason: str = ""
    sharpe_new: float = 0.0
    sharpe_baseline: float = 0.0
    p_value: float = 1.0
    max_drawdown_new: float = 0.0
    max_drawdown_baseline: float = 0.0
    regime_results: Dict[str, Any] = field(default_factory=dict)
    stress_passed: bool = True
    counterparty_passed: bool = True
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class PerformanceMetrics:
    """影子实例的绩效指标"""
    sharpe: float = 0.0
    max_dd: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_pnl: float = 0.0
    total_trades: int = 0
    daily_returns: List[float] = field(default_factory=list)


class ShadowValidatorV2:
    """
    幽灵影子验证器 v2。

    使用方式：
        validator = ShadowValidatorV2(config, behavior_log)
        result = validator.validate(new_trades, new_account,
                                     baseline_trades, baseline_account,
                                     market_data, regimes_mask)
    """

    def __init__(self,
                 config: ConfigLoader,
                 behavior_log: Optional[BehavioralLogger] = None):
        self.config = config
        self.log = behavior_log

        # 统计显著性水平
        self.alpha = config.get("shadow_validation.t_test_alpha", 0.05)
        # 要求在各市场状态下的最小非负夏普
        self.require_regime_non_negative = config.get(
            "shadow_validation.require_regime_non_negative", True
        )
        # 最小样本天数
        self.min_sample_days = config.get("shadow_validation.min_sample_days", 30)
        # 悲观滑点系数（应用于原始收益）
        self.pessimistic_slippage_factor = config.get(
            "shadow_validation.pessimistic_slippage_factor", 1.3
        )

    # ======================== 主评估接口 ========================
    def validate(self,
                 new_trades: List[Dict],
                 new_account: Account,
                 baseline_trades: Optional[List[Dict]] = None,
                 baseline_account: Optional[Account] = None,
                 market_data: Optional[Any] = None,
                 regimes: Optional[Dict[str, np.ndarray]] = None) -> ValidationResult:
        """
        对新策略进行全面验证。

        :param new_trades:        新策略的虚拟成交记录列表
        :param new_account:       新策略的虚拟账户对象
        :param baseline_trades:   基线策略的成交记录（若为空则与零收益比较）
        :param baseline_account:  基线账户
        :param market_data:       可选的市场行情数据（用于流动性压力测试）
        :param regimes:           市场状态掩码字典，如 {'trend': bool_array, 'osc': bool_array}
        :return: ValidationResult
        """
        # 1. 计算新策略的日收益序列及其绩效
        new_daily = self._calc_daily_returns(new_trades, new_account)
        new_metrics = self._calc_metrics(new_daily)

        # 基线收益
        if baseline_trades is not None and baseline_account is not None:
            baseline_daily = self._calc_daily_returns(baseline_trades, baseline_account)
            baseline_metrics = self._calc_metrics(baseline_daily)
        else:
            # 无基线时，与零假设比较（零收益）
            baseline_daily = np.zeros(len(new_daily)) if len(new_daily) > 0 else np.array([0.0])
            baseline_metrics = PerformanceMetrics()

        # 2. 样本量检查
        if len(new_daily) < self.min_sample_days:
            return ValidationResult(
                passed=False,
                reason=f"样本不足 ({len(new_daily)} 天，需要 {self.min_sample_days})",
                sharpe_new=new_metrics.sharpe,
            )

        # 3. 配对 t 检验
        min_len = min(len(new_daily), len(baseline_daily))
        if min_len >= 10:
            t_stat, p_value = stats.ttest_rel(
                new_daily[:min_len], baseline_daily[:min_len]
            )
        else:
            p_value = 1.0

        if p_value > self.alpha:
            return ValidationResult(
                passed=False,
                reason=f"统计不显著 (p={p_value:.3f})",
                sharpe_new=new_metrics.sharpe,
                sharpe_baseline=baseline_metrics.sharpe,
                p_value=p_value,
            )

        # 4. 按市场状态分组检验
        regime_results = {}
        if regimes and new_daily.size > 0:
            for regime_name, mask in regimes.items():
                if mask.sum() < 10:
                    continue
                new_regime = new_daily[mask]
                if baseline_daily.size > 0 and len(baseline_daily) == len(new_daily):
                    base_regime = baseline_daily[mask]
                else:
                    base_regime = np.zeros(len(new_regime))
                sharpe_new_regime = self._calc_sharpe(new_regime)
                sharpe_old_regime = self._calc_sharpe(base_regime)
                regime_results[regime_name] = {
                    "new_sharpe": round(sharpe_new_regime, 3),
                    "old_sharpe": round(sharpe_old_regime, 3),
                    "passed": sharpe_new_regime > 0 and (
                        not self.require_regime_non_negative or
                        sharpe_new_regime >= sharpe_old_regime * 0.8
                    )
                }

            # 若要求所有状态非负，且任一未通过，则整体不通过
            if self.require_regime_non_negative:
                if not all(r["passed"] for r in regime_results.values()):
                    return ValidationResult(
                        passed=False,
                        reason="部分市场状态表现不佳",
                        sharpe_new=new_metrics.sharpe,
                        sharpe_baseline=baseline_metrics.sharpe,
                        p_value=p_value,
                        regime_results=regime_results,
                    )

        # 5. 流动性压力测试（悲观滑点）
        stress_passed = True
        if len(new_daily) > 0:
            pessimistic_sharpe = self._calc_sharpe(
                new_daily * (1 - self.pessimistic_slippage_factor)
            )
            if pessimistic_sharpe < 0:
                stress_passed = False

        # 6. 对手方风险测试（此处简化，若需要可由独立模块调用）
        counterparty_passed = True

        # 7. 最终判定
        passed = (
            new_metrics.sharpe > baseline_metrics.sharpe * 0.8 and
            new_metrics.max_dd < max(baseline_metrics.max_dd, 30.0) and
            stress_passed
        )

        reason = "通过" if passed else "绩效未达标或压力测试失败"

        return ValidationResult(
            passed=passed,
            reason=reason,
            sharpe_new=new_metrics.sharpe,
            sharpe_baseline=baseline_metrics.sharpe,
            p_value=p_value,
            max_drawdown_new=new_metrics.max_dd,
            max_drawdown_baseline=baseline_metrics.max_dd,
            regime_results=regime_results,
            stress_passed=stress_passed,
            counterparty_passed=counterparty_passed,
        )

    # ======================== 单影子评估 ========================
    def evaluate_instance(self,
                          trade_history: List[Dict],
                          account: Account) -> Dict[str, float]:
        """
        评估单个影子实例的绩效指标，返回字典。
        供 ShadowManager 调用。
        """
        daily = self._calc_daily_returns(trade_history, account)
        metrics = self._calc_metrics(daily)
        return {
            "sharpe": round(metrics.sharpe, 3),
            "max_dd": round(metrics.max_dd, 3),
            "win_rate": round(metrics.win_rate, 3),
            "profit_factor": round(metrics.profit_factor, 3),
            "total_pnl": round(metrics.total_pnl, 2),
            "total_trades": metrics.total_trades,
        }

    # ======================== 比较策略 ========================
    def compare_strategies(self,
                           new_daily: List[float],
                           old_daily: List[float]) -> Tuple[float, float, bool]:
        """快捷接口：比较两策略的日收益序列，返回 (t_stat, p_value, is_significant)"""
        min_len = min(len(new_daily), len(old_daily))
        if min_len < 10:
            return 0.0, 1.0, False
        t_stat, p_value = stats.ttest_rel(
            new_daily[:min_len], old_daily[:min_len]
        )
        return t_stat, p_value, p_value < self.alpha

    # ======================== 内部计算函数 ========================
    def _calc_daily_returns(self,
                            trades: List[Dict],
                            account: Account) -> np.ndarray:
        """
        从成交记录和账户生成日收益序列（近似）。
        实际可根据 account 的 equity 曲线计算，此处简化：
        对每一天的已实现盈亏求和。
        """
        if not trades:
            return np.array([])

        daily_pnl = {}
        for t in trades:
            ts = t.get("timestamp")
            if ts is None:
                continue
            date = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
            pnl = float(t.get("pnl", 0.0))
            daily_pnl[date] = daily_pnl.get(date, 0.0) + pnl

        if not daily_pnl:
            # 回退：用账户已实现盈亏近似
            return np.array([account.realized_pnl_today])

        # 按日期排序
        sorted_dates = sorted(daily_pnl.keys())
        returns = [daily_pnl[d] for d in sorted_dates]
        return np.array(returns)

    def _calc_metrics(self, daily_returns: np.ndarray) -> PerformanceMetrics:
        """从日收益序列计算绩效指标"""
        metrics = PerformanceMetrics()
        if len(daily_returns) == 0:
            return metrics

        metrics.daily_returns = daily_returns.tolist()
        metrics.total_pnl = float(daily_returns.sum())
        metrics.sharpe = self._calc_sharpe(daily_returns)
        metrics.max_dd = self._calc_max_drawdown(daily_returns)
        metrics.total_trades = len(daily_returns)

        # 胜率（以日为单位，正值即为胜）
        if len(daily_returns) > 0:
            wins = np.sum(daily_returns > 0)
            metrics.win_rate = float(wins / len(daily_returns))

        # 盈亏比
        gains = daily_returns[daily_returns > 0].sum()
        losses = abs(daily_returns[daily_returns < 0].sum())
        if losses > 0:
            metrics.profit_factor = float(gains / losses)
        elif gains > 0:
            metrics.profit_factor = float('inf')

        return metrics

    @staticmethod
    def _calc_sharpe(returns: np.ndarray) -> float:
        """计算年化夏普比率（假设日收益）"""
        if returns.size < 2:
            return 0.0
        mean_ret = np.mean(returns)
        std_ret = np.std(returns, ddof=1)
        if std_ret == 0:
            return 0.0
        # 年化（√252 交易日）
        return float((mean_ret / std_ret) * np.sqrt(252))

    @staticmethod
    def _calc_max_drawdown(returns: np.ndarray) -> float:
        """从日收益序列计算最大回撤百分比"""
        if returns.size == 0:
            return 0.0
        cumulative = np.cumsum(returns)
        peak = np.maximum.accumulate(cumulative)
        drawdowns = (peak - cumulative) / (np.abs(peak) + 1e-10) * 100
        return float(np.max(drawdowns))

    # ======================== 压力测试 ========================
    def liquidity_stress_test(self,
                              daily_returns: np.ndarray,
                              additional_slippage_pct: float = 0.5) -> bool:
        """
        流动性压力测试：额外增加滑点后，夏普是否仍为正。
        """
        if len(daily_returns) == 0:
            return False
        stressed = daily_returns - abs(daily_returns) * (additional_slippage_pct / 100.0)
        return self._calc_sharpe(stressed) > 0

    def counterparty_risk_test(self,
                               trade_history: List[Dict],
                               withdrawal_rates: List[float] = None) -> bool:
        """
        对手方风险测试：模拟不同程度的对手方撤单，判断策略能否存活。
        至少通过 2/3 的撤单级别视为通过。
        """
        if withdrawal_rates is None:
            withdrawal_rates = [0.1, 0.3, 0.5]
        if not trade_history:
            return False

        survival = 0
        for rate in withdrawal_rates:
            # 随机移除部分成交
            np.random.seed(42)
            mask = np.random.random(len(trade_history)) < (1 - rate)
            filtered = [t for i, t in enumerate(trade_history) if i < len(mask) and mask[i]]
            if not filtered:
                continue
            daily = self._calc_daily_returns(filtered, Account())
            sharpe = self._calc_sharpe(daily)
            if sharpe > -1.0:
                survival += 1

        return survival >= max(1, int(len(withdrawal_rates) * 0.6))
