#!/usr/bin/env python3
"""
火种系统 (FireSeed) 智能学习守卫模块
========================================
在每日指定的学习时段内自动切换到平稳策略，并实时监控市场异动。
当波动率或成交量出现异常时，立即唤醒系统恢复激进模式。
同时提供夜间风控收紧参数，保障学习期间的资金安全。

核心功能：
- 弹性学习时段判断（基于波动率与成交量自动识别低活跃期）
- 惊梦唤醒：波动率突增、跳空缺口、成交量极度萎缩时强制退出学习
- 夜间风控收紧：止损倍数降低、仓位限制、单品种最大占用减少
- 异常熔断：学习时段出现插针或流动性枯竭时直接进入防御状态
"""

import logging
import time
from collections import deque
from datetime import datetime, time as dt_time
from typing import Dict, Optional, Tuple

import numpy as np

from config.loader import ConfigLoader

logger = logging.getLogger("fire_seed.learning_guard")


class IntelligentLearningGuard:
    """
    智能学习守卫。

    执行流程：
    1. 主循环每 Tick 调用 check_and_handle()
    2. 若当前处于学习时段 (或弹性低活跃期)，确保策略已切换为平稳
    3. 持续监控波动率、成交量、跳空缺口
    4. 异常发生时立即唤醒 (惊梦)，通知引擎恢复激进策略并告警
    """

    def __init__(self, config: ConfigLoader, engine=None):
        self.config = config
        self.engine = engine  # 可选引用，用于直接控制策略模式

        # 固定学习时段（后备）
        learning_cfg = config.get("learning", {})
        self._fixed_start = learning_cfg.get("start_time", "01:30")
        self._fixed_end = learning_cfg.get("end_time", "04:30")

        # 惊梦阈值
        nightmare_cfg = learning_cfg.get("nightmare", {})
        self.vol_multiplier = nightmare_cfg.get("volatility_multiplier", 2.0)
        self.volume_shrink_ratio = nightmare_cfg.get("volume_shrink_ratio", 0.3)
        self.confirm_duration = nightmare_cfg.get("confirm_duration", 300)  # 秒
        self.gap_atr_mult = nightmare_cfg.get("gap_atr_mult", 3.0)

        # 是否启用弹性学习 (基于市场活跃度)
        self.elastic_learning = learning_cfg.get("elastic", True)

        # 内部状态
        self._learning_active = False
        self._nightmare_triggered = False
        self._nightmare_start_time: Optional[float] = None
        self._last_volatility = 0.0
        self._last_volume = 0.0
        self._current_atr = 0.0

        # 历史统计（用于弹性判断）
        self._volatility_history: deque = deque(maxlen=1440)  # 24小时分钟级波动率
        self._volume_history: deque = deque(maxlen=1440)

    # ======================== 主调用接口 ========================
    async def check_and_handle(self) -> None:
        """
        由引擎主循环调用（每分钟一次）。
        负责管理学习状态的进入、退出以及异常唤醒。
        """
        now = datetime.now()
        current_time = now.time()

        # 1. 判断是否应该进入学习模式
        in_fixed_window = self._is_in_fixed_window(current_time)
        in_elastic_low = self.elastic_learning and self._is_low_activity()

        should_learn = in_fixed_window or in_elastic_low

        if should_learn and not self._learning_active:
            await self._enter_learning_mode()
        elif not should_learn and self._learning_active:
            await self._exit_learning_mode(reason="学习时段结束")

        if not self._learning_active:
            return

        # 2. 学习期间持续监测异常
        if self._detect_nightmare():
            await self._trigger_nightmare()

    # ======================== 固定时段判断 ========================
    def _is_in_fixed_window(self, current_time: dt_time) -> bool:
        """判断当前时间是否在配置的固定学习时段内"""
        try:
            start_parts = list(map(int, self._fixed_start.split(":")))
            end_parts = list(map(int, self._fixed_end.split(":")))
            start = dt_time(start_parts[0], start_parts[1])
            end = dt_time(end_parts[0], end_parts[1])
            if start <= end:
                return start <= current_time <= end
            else:
                # 跨午夜的情况
                return current_time >= start or current_time <= end
        except Exception:
            return False

    # ======================== 弹性学习判断 ========================
    def _is_low_activity(self) -> bool:
        """基于历史波动率与成交量判断当前是否属于低活跃期"""
        if len(self._volatility_history) < 60:
            return False  # 数据不足，不启用弹性判断
        avg_vol = np.mean(list(self._volatility_history))
        avg_volume = np.mean(list(self._volume_history))
        current_vol = self._last_volatility
        current_vol_abs = self._last_volume
        return current_vol < avg_vol * 0.7 and current_vol_abs < avg_volume * 0.7

    # ======================== 数据更新 (由引擎在Tick时调用) ========================
    def update_market_state(self, volatility: float, volume: float, atr: float) -> None:
        """更新当前市场微观状态，用于弹性判断和惊梦检测"""
        self._last_volatility = volatility
        self._last_volume = volume
        self._current_atr = atr
        self._volatility_history.append(volatility)
        self._volume_history.append(volume)

    # ======================== 惊梦检测 ========================
    def _detect_nightmare(self) -> bool:
        """
        检测是否需要唤醒。
        条件：
        1. 当前波动率 > 过去24小时均值 * vol_multiplier
        2. 当前1分钟成交量 < 过去24小时均值 * volume_shrink_ratio
        3. 出现跳空缺口 (Tick级别涨幅 > gap_atr_mult * ATR)
        任意条件满足且持续 confirm_duration 秒后触发。
        """
        if len(self._volatility_history) < 60:
            return False

        avg_vol = np.mean(list(self._volatility_history))
        current_vol = self._last_volatility

        conditions_met = False

        # 条件1：波动率飙升
        if current_vol > avg_vol * self.vol_multiplier:
            conditions_met = True

        # 条件2：成交量极度萎缩（流动性真空）
        if len(self._volume_history) >= 20:
            avg_vol_abs = np.mean(list(self._volume_history))
            if self._last_volume < avg_vol_abs * self.volume_shrink_ratio:
                conditions_met = True

        # 条件3：跳空缺口（由外部检测后设置标志）
        if getattr(self, '_gap_detected', False):
            conditions_met = True

        if conditions_met:
            if self._nightmare_start_time is None:
                self._nightmare_start_time = time.time()
            elif time.time() - self._nightmare_start_time >= self.confirm_duration:
                return True
        else:
            self._nightmare_start_time = None

        return False

    def flag_gap(self) -> None:
        """外部调用：标记检测到跳空缺口"""
        self._gap_detected = True

    # ======================== 模式切换 ========================
    async def _enter_learning_mode(self) -> None:
        """进入学习模式"""
        self._learning_active = True
        self._nightmare_triggered = False
        logger.info("进入学习模式：策略降级为平稳，风控收紧")
        if self.engine and hasattr(self.engine, 'set_strategy_mode'):
            self.engine.set_strategy_mode("moderate")
        # 应用夜间风控参数
        if self.engine and hasattr(self.engine, 'risk_monitor'):
            risk = self.engine.risk_monitor
            risk.set_param("max_single_position_pct", 25)
            risk.set_param("max_entries", 0)
        # 记录行为日志
        if self.engine and hasattr(self.engine, 'behavior_log'):
            self.engine.behavior_log.log(EventType.SYSTEM, "LearningGuard", "进入学习模式")

    async def _exit_learning_mode(self, reason: str = "") -> None:
        """退出学习模式"""
        self._learning_active = False
        self._nightmare_triggered = False
        logger.info(f"退出学习模式: {reason}")
        if self.engine and hasattr(self.engine, 'set_strategy_mode'):
            self.engine.set_strategy_mode(self.config.get("system.strategy_mode", "moderate"))
        # 恢复正常风控
        if self.engine and hasattr(self.engine, 'risk_monitor'):
            risk = self.engine.risk_monitor
            risk.set_param("max_single_position_pct", self.config.get("risk.max_single_position_pct", 50))
            risk.set_param("max_entries", self.config.get("strategy.trend.max_add_layers", 3))
        if self.engine and hasattr(self.engine, 'behavior_log'):
            self.engine.behavior_log.log(EventType.SYSTEM, "LearningGuard", f"退出学习模式: {reason}")

    async def _trigger_nightmare(self) -> None:
        """惊梦：强制退出学习模式，恢复激进策略"""
        if self._nightmare_triggered:
            return
        self._nightmare_triggered = True
        logger.error("惊梦触发！市场异动，立即退出学习模式")
        await self._exit_learning_mode(reason="惊梦唤醒")
        # 通知
        if self.engine and hasattr(self.engine, 'notifier'):
            await self.engine.notifier.alert_nightmare()

    # ======================== 夜间风控参数 ========================
    def get_night_risk_params(self) -> Dict[str, float]:
        """返回学习时段适用的风控参数 (供外部使用)"""
        return {
            "atr_mult_stop_loss": 0.6,
            "max_single_position_pct": 25.0,
            "max_entries": 0,
            "cool_down_bars": 5,
        }

    # ======================== 状态查询 ========================
    @property
    def is_active(self) -> bool:
        return self._learning_active

    @property
    def is_nightmare(self) -> bool:
        return self._nightmare_triggered
