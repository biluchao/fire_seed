#!/usr/bin/env python3
"""
火种系统 (FireSeed) 哥德尔监视者智能体 (Gödel Watcher)
=========================================================
职责：
- 持续评估系统整体的“自我怀疑”指数 (Self_Doubt, 0-1)
- 当怀疑指数超过阈值时，强制系统进入休眠状态（暂停新开仓，仅保留风控）
- 休眠期间持续监控系统健康，若恢复则自动解除休眠
- 防止因自指涉递归或集体盲区导致系统失效
"""

import asyncio
import logging
import time
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional

from api.server import get_engine
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier

logger = logging.getLogger("fire_seed.godel_watcher")


class GodelWatcher:
    """
    哥德尔监视者。
    以独立协程或定时回调方式运行，定期检查系统状态并计算怀疑指数。
    怀疑指数超过阈值时，通知引擎执行休眠；指数回落时解除休眠。
    """

    def __init__(self,
                 doubt_threshold: float = 0.8,
                 sleep_hours: float = 24.0,
                 min_awake_hours: float = 6.0,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None,
                 check_interval_sec: int = 60):
        """
        :param doubt_threshold:   自我怀疑阈值 (0-1)，超过则休眠
        :param sleep_hours:       最长休眠小时数（达到后强制苏醒）
        :param min_awake_hours:   苏醒后至少保持清醒的最小时间
        :param behavior_log:      行为日志实例
        :param notifier:          消息推送器
        :param check_interval_sec: 检查间隔（秒）
        """
        self.doubt_threshold = doubt_threshold
        self.sleep_duration_sec = sleep_hours * 3600.0
        self.min_awake_duration_sec = min_awake_hours * 3600.0

        self.behavior_log = behavior_log
        self.notifier = notifier
        self.check_interval = check_interval_sec

        # 休眠状态
        self._sleeping = False
        self._sleep_start: float = 0.0
        self._awake_start: float = time.time()

        # 历史怀疑指数
        self._doubt_history: deque = deque(maxlen=200)
        self._last_doubt = 0.0

        logger.info("哥德尔监视者初始化完成")

    # ======================== 主循环 ========================
    async def run(self) -> None:
        """独立运行循环，定期评估系统怀疑指数"""
        while True:
            await self.evaluate()
            await asyncio.sleep(self.check_interval)

    async def evaluate(self) -> Dict[str, Any]:
        """
        执行一次自我怀疑评估。
        返回评估结果，若需要休眠则通知引擎。
        """
        now = time.time()

        # 计算怀疑指数
        system_state = self._collect_system_state()
        doubt = self._compute_self_doubt(system_state)
        self._last_doubt = doubt
        self._doubt_history.append(doubt)

        # 判断是否需要进入/退出休眠
        action = "none"
        if self._sleeping:
            # 当前正在休眠，检查是否应该苏醒
            if now - self._sleep_start > self.sleep_duration_sec:
                # 超过最大休眠时间，强制苏醒
                await self._wake_up(reason="休眠时间已达上限")
                action = "wake_up"
            elif doubt < self.doubt_threshold * 0.7:
                # 怀疑指数显著下降，自动苏醒
                await self._wake_up(reason=f"怀疑指数回落至 {doubt:.2f}")
                action = "wake_up"
        else:
            # 当前清醒，检查是否需要休眠
            if doubt >= self.doubt_threshold and (now - self._awake_start > self.min_awake_duration_sec):
                await self._go_to_sleep(doubt)
                action = "sleep"

        # 记录行为日志
        if self.behavior_log:
            self.behavior_log.log(
                EventType.AGENT, "GodelWatcher",
                f"评估: 怀疑指数={doubt:.3f}, 休眠={'是' if self._sleeping else '否'}, 动作={action}",
                snapshot={"doubt": doubt, "sleeping": self._sleeping, "action": action}
            )

        return {
            "self_doubt": round(doubt, 4),
            "is_sleeping": self._sleeping,
            "action": action,
            "timestamp": datetime.now().isoformat()
        }

    # ======================== 怀疑指数计算 ========================
    def _compute_self_doubt(self, system_state: Dict[str, Any]) -> float:
        """
        基于多个维度综合计算自我怀疑指数 (0-1)。
        维度包括：
        - 策略绩效恶化（连续亏损、夏普下降）
        - 智能体议会弃权率
        - 模型更替频率（进化过频繁）
        - 感知层异常（PLL 信噪比低、跳跃检测频繁）
        - 分布偏移（MMD 分数高）
        """
        components = []

        # 1. 策略绩效
        perf = system_state.get("performance", {})
        recent_sharpe = perf.get("rolling_sharpe_24h", 1.0)
        consecutive_losses = perf.get("consecutive_losses", 0)
        if recent_sharpe < 0:
            components.append(min(1.0, abs(recent_sharpe) * 0.5))
        if consecutive_losses >= 5:
            components.append(min(1.0, consecutive_losses / 10.0))

        # 2. 智能体议会弃权率
        agent_metrics = system_state.get("agent_metrics", {})
        abstain_rate = agent_metrics.get("abstain_rate", 0.0)
        if abstain_rate > 0.5:
            components.append(abstain_rate)

        # 3. 进化/模型更替
        evolution = system_state.get("evolution", {})
        churn_rate = evolution.get("churn_rate", 0.0)
        if churn_rate > 3.0:  # 每周超过3次更替
            components.append(min(1.0, churn_rate / 7.0))

        # 4. 感知层异常
        perception = system_state.get("perception", {})
        pll_snr = perception.get("pll_snr_db", 20.0)
        jump_detected = perception.get("jump_detected", False)
        if pll_snr < 6:
            components.append(0.7 + (6 - pll_snr) / 20)
        if jump_detected:
            components.append(0.3)

        # 5. 分布偏移（MMD）
        mmd_score = system_state.get("mmd_score", 0.0)
        if mmd_score > 0.05:
            components.append(min(1.0, mmd_score * 10))

        # 综合计算
        if components:
            return min(1.0, np.mean(components))
        return 0.0

    def _collect_system_state(self) -> Dict[str, Any]:
        """
        从引擎收集当前系统状态快照。
        """
        state = {
            "performance": {},
            "agent_metrics": {},
            "evolution": {},
            "perception": {},
            "mmd_score": 0.0,
        }
        try:
            engine = get_engine()
            if engine is None:
                return state

            # 策略绩效
            if hasattr(engine, 'order_mgr'):
                stats = engine.order_mgr.get_daily_trading_stats()
                state["performance"]["consecutive_losses"] = stats.get("consecutive_losses", 0)
                # 获取滚动夏普（可从风险监控器获取）
                if hasattr(engine, 'risk_monitor'):
                    snap = engine.risk_monitor.get_snapshot()
                    # 简单转换：这里需要实际获取24h夏普，占位
                    state["performance"]["rolling_sharpe_24h"] = snap.sharpe if hasattr(snap, 'sharpe') else 1.0

            # 智能体弃权率（从议会获取）
            if hasattr(engine, 'agent_council'):
                state["agent_metrics"]["abstain_rate"] = engine.agent_council.get_abstain_rate()

            # 进化更替率
            if hasattr(engine, 'plugin_mgr'):
                state["evolution"]["churn_rate"] = engine.plugin_mgr.get_churn_rate()

            # 感知层异常
            if hasattr(engine, 'perception'):
                pll_state = engine.perception._last_pll
                state["perception"]["pll_snr_db"] = pll_state.snr_db
                state["perception"]["jump_detected"] = engine.perception.is_frozen

            # MMD 分数
            if hasattr(engine, 'distribution_shift'):
                state["mmd_score"] = engine.distribution_shift.compute_mmd()

        except Exception as e:
            logger.warning(f"收集系统状态失败: {e}")

        return state

    # ======================== 休眠管理 ========================
    async def _go_to_sleep(self, doubt: float) -> None:
        """进入休眠状态"""
        self._sleeping = True
        self._sleep_start = time.time()
        logger.warning(f"哥德尔监视者触发休眠，怀疑指数={doubt:.3f}")

        # 通知引擎冻结新开仓
        try:
            engine = get_engine()
            if engine:
                # 假设引擎有 freeze_new_entries 方法
                if hasattr(engine, 'freeze_new_entries'):
                    engine.freeze_new_entries(reason="GodelWatcher 休眠")
                # 或将策略模式强制设为 moderate 并限制仓位
                if hasattr(engine, 'set_strategy_mode'):
                    engine.set_strategy_mode("moderate")
        except Exception as e:
            logger.error(f"休眠指令执行失败: {e}")

        # 推送告警
        if self.notifier:
            await self.notifier.send_alert(
                level="CRITICAL",
                title="🧠 哥德尔休眠",
                body=f"系统自我怀疑指数 {doubt:.2f}，已进入休眠状态。\n"
                     f"休眠将持续至怀疑指数回落或达到最大时限 {self.sleep_duration_sec/3600:.1f} 小时。"
            )

    async def _wake_up(self, reason: str = "") -> None:
        """退出休眠状态"""
        self._sleeping = False
        self._awake_start = time.time()
        logger.info(f"哥德尔监视者苏醒: {reason}")

        # 恢复引擎正常交易
        try:
            engine = get_engine()
            if engine:
                if hasattr(engine, 'unfreeze_new_entries'):
                    engine.unfreeze_new_entries()
        except Exception as e:
            logger.error(f"苏醒指令执行失败: {e}")

        # 推送通知
        if self.notifier:
            await self.notifier.send_alert(
                level="INFO",
                title="🧠 哥德尔苏醒",
                body=f"系统已恢复正常交易。原因: {reason}"
            )

    # ======================== 状态查询 ========================
    def get_status(self) -> Dict[str, Any]:
        """返回当前监视者状态"""
        return {
            "self_doubt": round(self._last_doubt, 4),
            "is_sleeping": self._sleeping,
            "sleep_remaining_sec": (
                max(0, self._sleep_start + self.sleep_duration_sec - time.time())
                if self._sleeping else 0
            ),
            "doubt_history": list(self._doubt_history)[-50:],
        }

    @property
    def is_sleeping(self) -> bool:
        return self._sleeping


# 为方便计算，导入 numpy（若不可用则回退）
try:
    import numpy as np
except ImportError:
    np = None  # type: ignore
    logger.warning("numpy 未安装，哥德尔监视者的怀疑指数计算将退化为简单平均值")
    # 提供一个简单的 mean 退路
    class _FakeNumpy:
        @staticmethod
        def mean(lst):
            return sum(lst) / len(lst) if lst else 0.0
    np = _FakeNumpy()
