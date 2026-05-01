#!/usr/bin/env python3
"""
火种系统 (FireSeed) 多周期仲裁器 v2 (认知升级版)
================================================
负责：
- 收集各周期的独立策略信号 (1m / 3m / 5m / 15m)
- 共振有效性过滤：防止假共振放大仓位
- 大周期否决权：动态分位数阈值 + 冷却期 + 预判式否决
- 边际否决：盈利持仓仅收紧止损，亏损持仓才执行降仓
- 统计显著性：共振需满足历史盈利条件
- 自适应否决阈值：基于波动率调整大周期置信度要求
"""

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("fire_seed.arbiter")


@dataclass
class TFSignal:
    """单个周期的决策信号"""
    timeframe: str
    direction: int                # 1: 做多, -1: 做空, 0: 中性
    confidence: float             # 0.0 ~ 1.0
    score: float                  # 0 ~ 100 (评分卡输出)
    timestamp: float = field(default_factory=time.time)
    # 以下为可选元数据，便于统计
    median_score: float = 50.0    # 该周期近期评分中位数
    locked: bool = True           # 锁相环是否锁定


@dataclass
class ArbiterDecision:
    """仲裁器的最终输出"""
    direction: int                 # 1 / -1 / 0
    score: float                   # 综合评分
    resonance_mult: float          # 共振乘数
    veto_active: bool = False
    veto_reason: str = ""
    action: str = "normal"         # normal / tighten_stop / reduce_position / reject
    position_advice: Optional[Dict] = None


class MultiTFArbiter:
    """
    多周期仲裁器 (认知升级版)

    核心特性：
    - 共振有效性：各周期评分需高于自身历史中位数，且历史共振盈利比率 > 1.3
    - 大周期否决权：15分钟置信度超过历史95分位数时激活否决，带30分钟冷却
    - 边际否决：盈利时仅收紧止损，亏损时降仓
    - 预判式否决：利用15分钟K线部分数据提前判断方向
    - 自适应阈值：根据波动率动态调整否决置信度要求
    """

    def __init__(self, config: Dict = None):
        self.config = config or {}

        # 收集的各周期最新信号
        self.signals: Dict[str, TFSignal] = {}

        # 历史信号记录 (用于计算分位数和共振有效性)
        self._confidence_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=200))
        self._score_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=200))

        # 大周期否决权历史 (用于事后追责)
        self._veto_history: deque = deque(maxlen=100)          # 存储每次否决的快照
        self._veto_correct_count: int = 0                      # 否决后小周期正确的次数
        self._last_veto_time: float = 0.0
        self._veto_cooldown_sec: int = self.config.get("arbiter.veto.cooldown_seconds", 1800)  # 30分钟

        # 共振有效性历史
        self._resonance_history: deque = deque(maxlen=200)     # 存储 (共振方向, 实际盈亏)

        # 配置参数
        self._veto_confidence_percentile = self.config.get("arbiter.veto.confidence_percentile", 95)
        self._min_resonance_effectiveness = self.config.get("arbiter.resonance.min_effectiveness_ratio", 1.3)
        self._max_resonance_mult = self.config.get("arbiter.resonance.max_multiplier", 1.6)
        self._pre_veto_enabled = self.config.get("arbiter.pre_veto.enable", True)
        self._pre_veto_completion_weight = self.config.get("arbiter.pre_veto.completion_weight_factor", 0.7)

        # 波动率自适应参数
        self._base_confidence_threshold = self.config.get("arbiter.veto.base_confidence", 0.85)
        self._current_volatility_percentile: float = 50.0

        logger.info("多周期仲裁器 v2 初始化完成")

    # ======================== 信号收集 ========================
    def collect_signal(self, timeframe: str, signal: TFSignal) -> None:
        """接收一个周期的决策信号"""
        self.signals[timeframe] = signal

        # 更新历史记录
        self._confidence_history[timeframe].append(signal.confidence)
        self._score_history[timeframe].append(signal.score)

        # 更新信号的中位数评分字段 (方便共振判断)
        scores = list(self._score_history[timeframe])
        if scores:
            signal.median_score = np.median(scores)

    # ======================== 主评估逻辑 ========================
    def evaluate(self, position_state: Optional[Dict] = None) -> ArbiterDecision:
        """
        根据所有已收集的信号，输出最终仲裁决策。
        :param position_state: 当前持仓状态字典，包含 unrealized_pnl, stop_locked_profit 等
        """
        decision = ArbiterDecision(direction=0, score=50.0, resonance_mult=1.0)

        # 0. 若无信号，返回中性
        if not self.signals:
            return decision

        # 1. 检查大周期否决权
        s15 = self.signals.get("15m")
        veto_active, veto_confidence = self._check_veto(s15, position_state)

        # 2. 收集小周期信号
        small_tfs = ["1m", "3m", "5m"]
        small_signals = {tf: self.signals[tf] for tf in small_tfs if tf in self.signals}
        if not small_signals:
            # 只有大周期信号，直接以其为主（但此时系统一般不会交易）
            if s15:
                decision.direction = s15.direction
                decision.score = s15.score
            return decision

        # 3. 共振有效性检测与加权
        resonance_mult, consensus_dir = self._calc_resonance(small_signals)

        # 4. 选取小周期中置信度最高的信号为基础
        best_signal = max(small_signals.values(), key=lambda s: s.confidence)
        base_score = best_signal.score

        # 5. 综合评分计算
        final_score = base_score * resonance_mult

        # 6. 大周期否决处理
        if veto_active:
            final_score, decision = self._apply_veto(
                final_score, decision, veto_confidence, position_state, s15, best_signal
            )
            if decision.action == "reject":
                return decision

        # 7. 确定最终方向
        if final_score >= 65:
            decision.direction = 1
        elif final_score <= 35:
            decision.direction = -1
        else:
            decision.direction = 0

        decision.score = min(100, max(0, final_score))
        decision.resonance_mult = resonance_mult
        decision.veto_active = veto_active
        if veto_active:
            decision.veto_reason = f"15m 否决 (置信度 {veto_confidence:.2f})"

        return decision

    # ======================== 共振计算 ========================
    def _calc_resonance(self, small_signals: Dict[str, TFSignal]) -> Tuple[float, int]:
        """
        计算共振加成系数。
        返回 (乘数, 共识方向)
        """
        if len(small_signals) < 2:
            return 1.0, list(small_signals.values())[0].direction

        # 共识方向：多数投票
        dirs = [s.direction for s in small_signals.values() if s.direction != 0]
        if not dirs:
            return 1.0, 0
        # 多数方向
        long_count = sum(1 for d in dirs if d == 1)
        short_count = sum(1 for d in dirs if d == -1)
        consensus = 1 if long_count > short_count else (-1 if short_count > long_count else 0)
        if consensus == 0:
            return 1.0, 0

        # 共振有效性条件：
        # a) 各周期评分均高于自身历史中位数
        all_above_median = all(
            s.score >= s.median_score for s in small_signals.values() if s.direction == consensus
        )
        if not all_above_median:
            return 1.0, consensus

        # b) 历史共振有效性比率 > 阈值
        recent_resonance = [
            (d, pnl) for (d, pnl) in list(self._resonance_history)[-30:]
            if d == consensus
        ]
        if recent_resonance:
            avg_profit = np.mean([pnl for _, pnl in recent_resonance if pnl > 0]) if any(pnl > 0 for _, pnl in recent_resonance) else 0.0
            avg_loss = abs(np.mean([pnl for _, pnl in recent_resonance if pnl < 0])) if any(pnl < 0 for _, pnl in recent_resonance) else 1.0
            effectiveness = avg_profit / (avg_loss + 1e-10)
            if effectiveness < self._min_resonance_effectiveness:
                return 1.0, consensus

        # 计算乘数：同向周期越多，乘数越高
        same_dir_count = sum(1 for s in small_signals.values() if s.direction == consensus)
        mult = 1.0 + 0.15 * (same_dir_count - 1)
        mult = min(mult, self._max_resonance_mult)
        return mult, consensus

    # ======================== 大周期否决权 ========================
    def _check_veto(self, s15: Optional[TFSignal],
                    position_state: Optional[Dict] = None) -> Tuple[bool, float]:
        """
        检查是否应触发否决权。
        返回 (是否否决, 大周期置信度)
        """
        if s15 is None or s15.direction == 0:
            return False, 0.0

        # 获取历史置信度分位数
        confidences = list(self._confidence_history.get("15m", []))
        if len(confidences) < 30:
            # 历史数据不足，使用固定阈值
            threshold = self._base_confidence_threshold
        else:
            percentile = self._veto_confidence_percentile
            threshold = np.percentile(confidences, percentile)
            # 根据波动率调整：高波动时适当提高阈值(更难否决)
            if hasattr(self, '_current_volatility_percentile'):
                if self._current_volatility_percentile > 70:
                    threshold *= 1.1
                elif self._current_volatility_percentile < 30:
                    threshold *= 0.95

        # 确保阈值在合理范围内
        threshold = max(0.7, min(0.95, threshold))

        if s15.confidence < threshold:
            return False, s15.confidence

        # 冷却期检查
        if time.time() - self._last_veto_time < self._veto_cooldown_sec:
            return False, s15.confidence

        # 通过所有检查，激活否决
        self._last_veto_time = time.time()
        return True, s15.confidence

    def _apply_veto(self, score: float, decision: ArbiterDecision,
                    veto_confidence: float, position_state: Optional[Dict],
                    s15: TFSignal, small_signal: TFSignal) -> Tuple[float, ArbiterDecision]:
        """
        应用否决效果。若持仓盈利则仅收紧止损，亏损则降仓或拒绝。
        """
        # 若无持仓，直接降低评分
        if not position_state or position_state.get('size', 0) == 0:
            decision.action = "reject"
            return score * 0.3, decision

        unrealized_pnl = position_state.get('unrealized_pnl', 0)
        stop_locked = position_state.get('stop_locked_profit', False)

        if unrealized_pnl > 0 and stop_locked:
            # 盈利且止损已锁定利润 → 收紧止损，而不是降仓
            decision.action = "tighten_stop"
            decision.position_advice = {
                "action": "tighten_stop",
                "new_stop_offset_pct": 0.1  # 0.1%
            }
            logger.info("大周期否决但持仓盈利，仅收紧止损")
            return score * 0.6, decision  # 轻微降分
        else:
            # 亏损或无利润锁 → 执行降仓
            decision.action = "reduce_position"
            decision.position_advice = {
                "action": "reduce",
                "reduce_pct": 0.5  # 降仓50%
            }
            logger.info("大周期否决，执行降仓")
            return score * 0.2, decision

    # ======================== 预判式否决 (基于部分K线) ========================
    def predict_15m_direction(self, partial_kline: Dict) -> Tuple[Optional[int], float]:
        """
        基于15分钟K线未闭合时的部分数据，预判最终方向。
        partial_kline: {high, low, close, open, volume, time_elapsed_seconds}
        返回 (预测方向, 置信度)
        """
        if not self._pre_veto_enabled:
            return None, 0.0

        elapsed = partial_kline.get('time_elapsed_seconds', 0)
        if elapsed < 120:  # 至少需要2分钟数据
            return None, 0.0

        completion_ratio = elapsed / 900.0  # 15分钟 = 900秒

        # 简易预判逻辑：基于当前价格在K线内的位置和成交量
        open_price = partial_kline['open']
        current = partial_kline['close']
        high = partial_kline['high']
        low = partial_kline['low']
        vol = partial_kline.get('volume', 0)
        avg_vol_per_sec = vol / elapsed if elapsed else 0

        # 价格位置得分 (0:接近低点, 1:接近高点)
        range_val = high - low
        if range_val <= 0:
            return None, 0.0
        position_ratio = (current - low) / range_val

        # 方向预判：高于开盘价偏多，低于偏空
        if current > open_price:
            direction = 1
        elif current < open_price:
            direction = -1
        else:
            direction = 0

        # 置信度 = 完成度 * 位置极端性 * 成交量因子
        confidence = completion_ratio * abs(position_ratio - 0.5) * 2  # 中间位置不给高置信度
        # 成交量异常放大可增加置信度
        if avg_vol_per_sec > 0.5:
            confidence = min(0.85, confidence * 1.3)

        return direction, confidence

    # ======================== 反馈与学习 ========================
    def record_outcome(self, resonance_direction: int, pnl: float) -> None:
        """记录共振信号的实际盈亏，用于有效性统计"""
        self._resonance_history.append((resonance_direction, pnl))

    def record_veto_outcome(self, small_signal_direction: int, market_direction_after: int) -> None:
        """
        记录否决后的市场实际走向，用于事后追责。
        :param small_signal_direction: 被否决的小周期方向
        :param market_direction_after: 后续市场实际方向
        """
        self._veto_history.append({
            "time": time.time(),
            "small_dir": small_signal_direction,
            "market_dir": market_direction_after,
            "correct": small_signal_direction == market_direction_after
        })
        if small_signal_direction == market_direction_after:
            self._veto_correct_count += 1

    @property
    def veto_accuracy(self) -> float:
        """否决后小周期正确的比例 (越低越好，说明否决是有益的)"""
        total = len(self._veto_history)
        if total == 0:
            return 0.0
        return self._veto_correct_count / total

    def should_freeze_veto(self) -> bool:
        """若否决后小周期正确率超过40%，应临时冻结否决权"""
        if len(self._veto_history) >= 10 and self.veto_accuracy > 0.4:
            return True
        return False

    def set_volatility_percentile(self, pct: float) -> None:
        """更新当前波动率分位数，用于自适应否决阈值"""
        self._current_volatility_percentile = pct

    def reset_veto_history(self) -> None:
        """重置否决历史 (如市场结构突变后)"""
        self._veto_history.clear()
        self._veto_correct_count = 0

    def clear_signals(self) -> None:
        """清空本轮信号，为下一分钟评估做准备"""
        self.signals.clear()
