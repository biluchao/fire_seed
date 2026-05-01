#!/usr/bin/env python3
"""
火种系统 (FireSeed) 连续仓位控制器
====================================
实现评分的平滑仓位映射，支持：
- 评分的 Sigmoid 映射到 [-1, 1] 目标仓位
- 磁滞回线：小偏离不调整，避免震荡磨损
- 成本敏感：预期收益低于 1.5 倍预估成本则放弃调整
- 合并定时批次执行，减少碎片化订单
- 支持动态灵敏度与最大仓位调整
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np

from config.loader import ConfigLoader

logger = logging.getLogger("fire_seed.continuous_position")


@dataclass
class PositionAdjustment:
    action: str           # 'buy' / 'sell' / 'hold'
    quantity: float       # 需交易的绝对值
    reason: str           # 调整原因描述


class ContinuousPositionController:
    """
    连续仓位控制器。

    将 0-100 评分平滑转化为 [-1, 1] 目标仓位，并输出分批调整指令。
    通过磁滞回线与成本过滤避免过度交易。
    """

    def __init__(self, config: ConfigLoader):
        self.config = config
        # 最大仓位（正股本位），默认 1.0 表示满仓
        self.max_position = config.get("position.max_size", 1.0)
        # 敏感度：越高曲线越陡峭，在阈值附近切换更快
        self.sensitivity = config.get("position.sensitivity", 0.1)

        # 磁滞阈值：偏离超过该比例才调整
        self.hysteresis = config.get("position.hysteresis", 0.02)  # 2%

        # 成本阈值：预期收益 / 预估成本 < 此值则放弃调整
        self.cost_ratio_threshold = config.get("position.cost_ratio_threshold", 1.5)

        # 批量执行间隔（秒）
        self.batch_interval = config.get("position.batch_interval", 30)

        # 内部状态
        self._current_target: float = 0.0
        self._last_adjust_time: float = 0.0
        self._pending_queue: list = []   # 待批量执行的调整

    def score_to_target(self, score: float) -> float:
        """
        将 0-100 评分映射到 [-1, 1] 仓位。
        50 分为中性（仓位 0），100 分满仓多，0 分满仓空。
        """
        normalized = (score - 50.0) / 10.0   # 50→0, 100→5, 0→-5
        sigmoid = 1.0 / (1.0 + np.exp(-normalized * self.sensitivity))
        target = (sigmoid - 0.5) * 2.0 * self.max_position
        return np.clip(target, -self.max_position, self.max_position)

    def calc_adjustment(self, score: float, current_position: float,
                        estimated_pnl: float = 0.0,
                        estimated_cost: float = 0.0) -> PositionAdjustment:
        """
        计算仓位调整指令。
        :param score: 当前评分 (0-100)
        :param current_position: 当前实际仓位 (-max_position ~ max_position)
        :param estimated_pnl: 本次调整的预期收益（可选用）
        :param estimated_cost: 预估交易成本（滑点+手续费）
        :return: 调整指令
        """
        target = self.score_to_target(score)
        delta = target - current_position

        # 磁滞回线：偏离太小不调整
        if abs(delta) < self.hysteresis * self.max_position:
            return PositionAdjustment(action="hold", quantity=0.0,
                                      reason=f"偏差 {delta:.3f} 小于磁滞阈值 {self.hysteresis:.3f}")

        # 成本敏感：若成本过高则放弃
        if estimated_cost > 0 and estimated_pnl > 0:
            if estimated_pnl / estimated_cost < self.cost_ratio_threshold:
                return PositionAdjustment(action="hold", quantity=0.0,
                                          reason=f"预期收益/成本 {estimated_pnl/estimated_cost:.2f} < {self.cost_ratio_threshold}")

        action = "buy" if delta > 0 else "sell"
        quantity = abs(delta)
        reason = f"目标 {target:.3f} 当前 {current_position:.3f} 偏差 {delta:.3f}"

        self._current_target = target
        return PositionAdjustment(action=action, quantity=quantity, reason=reason)

    def should_batch_execute(self) -> bool:
        """判断是否应触发批量执行"""
        now = time.time()
        if now - self._last_adjust_time >= self.batch_interval:
            self._last_adjust_time = now
            return True
        return False

    def add_to_batch(self, adjustment: PositionAdjustment) -> None:
        """将调整加入待执行批次"""
        self._pending_queue.append(adjustment)

    def get_batch(self) -> list:
        """获取并清空当前批次的所有调整"""
        batch = self._pending_queue.copy()
        self._pending_queue.clear()
        return batch

    @property
    def current_target(self) -> float:
        return self._current_target
