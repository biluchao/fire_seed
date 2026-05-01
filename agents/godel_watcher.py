#!/usr/bin/env python3
"""
火种系统 (FireSeed) 哥德尔监视者智能体 (Gödel Watcher)
=========================================================
世界观：不完备定理 (Incompleteness)
  - 任何足够强大的系统都无法完全理解自身。
  - 必须保持谦卑，时刻准备质疑自己的判断。
  - 当自我怀疑指数超过阈值时，强制系统进入休眠，防止自指涉陷阱。

核心职责：
  - 持续计算系统“自我怀疑”指数 (Self_Doubt, 0-1)
  - 当怀疑指数超过阈值时，触发系统休眠（暂停新开仓，仅保留风控）
  - 休眠期间持续监控，若怀疑指数回落则自动解除休眠
  - 在议会中提案：当前是否应进入休眠状态
  - 挑战其他智能体的提案，指出它们可能忽略的未知风险
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np

from agents.worldview import WorldView, WorldViewManifesto, WorldViewAgent
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier

logger = logging.getLogger("fire_seed.godel_watcher")

# ============================================================================
# 世界观宣言
# ============================================================================
GODEL_MANIFESTO = WorldViewManifesto(
    worldview=WorldView.INCOMPLETENESS,
    core_belief="自我指涉的系统永远无法自证其正确性",
    primary_optimization_target="minimize(missed_loss_during_sleep)",
    adversary_worldview=WorldView.MECHANICAL_MATERIALISM,
    forbidden_data_source={"MARKET_DATA", "TRADE_SIGNAL"},
    exclusive_data_source={"SYSTEM_META", "COUNCIL_VOTES", "PROCESS_TREE"},
    time_scale="60",
)


@dataclass
class DoubtSignal:
    """自我怀疑信号组件"""
    dimension: str          # 维度名称
    value: float            # 0-1 归一化值，越高越怀疑
    weight: float           # 该维度权重
    reason: str             # 人类可读的原因


class GodelWatcher(WorldViewAgent):
    """
    哥德尔监视者 —— 不完备定理的信徒。

    它不直接参与交易决策，而是持续反思：“我们是否正在犯一个系统性的错误？”
    当多个维度都指向‘我们可能错了’时，它会要求整个系统暂停思考。
    """

    def __init__(self,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None,
                 check_interval_sec: int = 60):
        """
        :param behavior_log:      系统行为日志
        :param notifier:          消息推送器
        :param check_interval_sec: 自我怀疑评估间隔（秒）
        """
        super().__init__(GODEL_MANIFESTO)

        self.behavior_log = behavior_log
        self.notifier = notifier
        self.check_interval = check_interval_sec

        # 怀疑阈值：高于此值触发休眠
        self.doubt_threshold = self._get_config("doubt_threshold", 0.8)
        # 最长休眠时间（秒），防止无限期停摆
        self.max_sleep_seconds = self._get_config("max_sleep_hours", 24) * 3600
        # 苏醒后至少保持清醒的时间
        self.min_awake_seconds = self._get_config("min_awake_hours", 6) * 3600

        # ---- 运行时状态 ----
        self._sleeping = False
        self._sleep_start: float = 0.0
        self._awake_start: float = time.time()
        self._last_doubt: float = 0.0
        self._doubt_history: deque = deque(maxlen=200)

        # ---- 维度权重定义 (可由配置文件覆盖) ----
        self._dimension_weights = {
            "performance_decay": 0.25,
            "council_abstain": 0.15,
            "evolution_churn": 0.15,
            "perception_anomaly": 0.20,
            "distribution_shift": 0.15,
            "hard_watcher_frequency": 0.10,
        }

        logger.info("哥德尔监视者初始化完成 | 世界观 = %s", self.manifesto.worldview.value)

    # ========================================================================
    # WorldViewAgent 接口实现
    # ========================================================================

    async def propose(self, perception: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        提案：当前是否应该休眠？
        返回包含 direction (1=建议休眠, -1=建议清醒, 0=中立) 和 confidence (0-1)。
        """
        system_state = self._collect_system_state()
        doubt = self._compute_self_doubt(system_state)
        self._last_doubt = doubt
        self._doubt_history.append(doubt)

        if self._sleeping:
            # 已经在休眠，提案是“继续休眠”还是“苏醒”
            sleep_duration = time.time() - self._sleep_start
            if sleep_duration > self.max_sleep_seconds:
                # 超过最大休眠时间，强制建议苏醒
                return {"direction": -1, "confidence": 0.9, "reason": "maximum sleep duration exceeded"}
            if doubt < self.doubt_threshold * 0.6:
                return {"direction": -1, "confidence": 0.8, "reason": f"doubt dropped to {doubt:.2f}"}
            # 仍应继续休眠
            return {"direction": 1, "confidence": doubt, "reason": f"ongoing doubt: {doubt:.2f}"}
        else:
            # 清醒状态，判断是否应进入休眠
            if doubt >= self.doubt_threshold:
                awake_duration = time.time() - self._awake_start
                if awake_duration > self.min_awake_seconds:
                    return {"direction": 1, "confidence": doubt, "reason": f"doubt threshold exceeded: {doubt:.2f}"}
            return {"direction": -1, "confidence": 0.5, "reason": "system appears healthy"}

    async def challenge(self, other_proposal: Dict[str, Any], my_worldview: WorldView) -> Dict[str, Any]:
        """
        挑战其他智能体的提案：所有决策都可能是错误的，
        因为我们永远无法完备地理解市场。
        """
        doubt = self._last_doubt
        challenges = []
        veto = False

        # 1. 如果自我怀疑指数已经很高，直接否决所有提案
        if doubt > self.doubt_threshold:
            challenges.append(f"自我怀疑指数 {doubt:.2f} 超过阈值，系统可能有未知盲区")
            veto = True

        # 2. 如果该提案的置信度极高（>0.9），哥德尔认为过于自信是危险的
        if other_proposal.get("confidence", 0) > 0.9:
            challenges.append("置信度过高（>0.9），这本身就是一种危险信号")
            if doubt > 0.5:
                veto = True

        # 3. 检查是否与历史休眠事件相关
        if len(self._doubt_history) > 50:
            avg_recent = sum(list(self._doubt_history)[-20:]) / 20
            if avg_recent > 0.5:
                challenges.append(f"近20次平均怀疑指数 {avg_recent:.2f}，系统可能处于慢性失效中")
                if doubt > 0.7:
                    veto = True

        # 4. 哥德尔特有的“元怀疑”：怀疑监视者自身的判断是否正确
        if random.random() < 0.05:  # 5% 的概率提出自我质疑
            challenges.append("哥德尔本身也可能出错——这份挑战本身的可靠性也需要质疑")

        return {
            "challenges": challenges,
            "veto": veto,
            "confidence_adjustment": -min(0.3, doubt * 0.5),
            "worldview_note": "任何系统都无法从内部证明自身的完备性"
        }

    # ========================================================================
    # 自我怀疑指数计算
    # ========================================================================

    def _compute_self_doubt(self, system_state: Dict[str, Any]) -> float:
        """
        基于多个独立维度综合计算自我怀疑指数 (0-1)。
        维度来源完全不依赖市场数据，符合世界观约束。
        """
        components: List[DoubtSignal] = []

        # ---- 维度 1: 策略绩效恶化 ----
        perf = system_state.get("performance", {})
        rolling_sharpe = perf.get("rolling_sharpe_24h", 1.0)
        consecutive_losses = perf.get("consecutive_losses", 0)
        if rolling_sharpe < 0:
            components.append(DoubtSignal(
                dimension="performance_decay",
                value=min(1.0, abs(rolling_sharpe) * 0.5),
                weight=self._dimension_weights["performance_decay"],
                reason=f"滚动夏普 {rolling_sharpe:.2f}"
            ))
        if consecutive_losses >= 5:
            components.append(DoubtSignal(
                dimension="performance_decay",
                value=min(1.0, consecutive_losses / 10),
                weight=self._dimension_weights["performance_decay"],
                reason=f"连续亏损 {consecutive_losses} 次"
            ))

        # ---- 维度 2: 议会弃权率 ----
        council = system_state.get("council", {})
        abstain_rate = council.get("abstain_rate", 0.0)
        if abstain_rate > 0.4:
            components.append(DoubtSignal(
                dimension="council_abstain",
                value=min(1.0, abstain_rate),
                weight=self._dimension_weights["council_abstain"],
                reason=f"议会弃权率 {abstain_rate:.2f}"
            ))

        # ---- 维度 3: 进化/模型更替频率 ----
        evolution = system_state.get("evolution", {})
        churn_rate = evolution.get("churn_rate", 0.0)
        if churn_rate > 2.0:
            components.append(DoubtSignal(
                dimension="evolution_churn",
                value=min(1.0, churn_rate / 7.0),
                weight=self._dimension_weights["evolution_churn"],
                reason=f"策略更替频率 {churn_rate:.1f}/周"
            ))

        # ---- 维度 4: 感知层异常 ----
        perception = system_state.get("perception", {})
        pll_snr = perception.get("pll_snr_db", 20.0)
        jump_detected = perception.get("jump_detected", False)
        if pll_snr < 6:
            components.append(DoubtSignal(
                dimension="perception_anomaly",
                value=0.6 + (6 - pll_snr) / 20,
                weight=self._dimension_weights["perception_anomaly"],
                reason=f"锁相环信噪比过低: {pll_snr:.1f}dB"
            ))
        if jump_detected:
            components.append(DoubtSignal(
                dimension="perception_anomaly",
                value=0.3,
                weight=self._dimension_weights["perception_anomaly"],
                reason="检测到价格跳跃"
            ))

        # ---- 维度 5: 分布偏移 (MMD) ----
        mmd_score = system_state.get("mmd_score", 0.0)
        if mmd_score > 0.05:
            components.append(DoubtSignal(
                dimension="distribution_shift",
                value=min(1.0, mmd_score * 10),
                weight=self._dimension_weights["distribution_shift"],
                reason=f"MMD 偏移分数 {mmd_score:.3f}"
            ))

        # ---- 维度 6: 第二重硬监视器触发频率 ----
        hard_watcher_count = system_state.get("hard_watcher_events_24h", 0)
        if hard_watcher_count > 3:
            components.append(DoubtSignal(
                dimension="hard_watcher_frequency",
                value=min(1.0, hard_watcher_count / 10),
                weight=self._dimension_weights["hard_watcher_frequency"],
                reason=f"硬监视器24h内触发 {hard_watcher_count} 次"
            ))

        if not components:
            return 0.0

        # 加权平均
        total_weight = sum(c.weight for c in components)
        if total_weight == 0:
            return 0.0
        weighted_sum = sum(c.value * c.weight for c in components)
        return min(1.0, weighted_sum / total_weight)

    # ========================================================================
    # 系统状态收集 (仅限非市场数据)
    # ========================================================================

    def _collect_system_state(self) -> Dict[str, Any]:
        """
        收集系统元数据。
        哥德尔禁止访问任何市场数据，只能从系统日志、进程树、议会投票记录中提取特征。
        """
        state: Dict[str, Any] = {
            "performance": {},
            "council": {},
            "evolution": {},
            "perception": {},
            "mmd_score": 0.0,
            "hard_watcher_events_24h": 0,
        }
        try:
            engine = _get_engine_safe()
            if engine is None:
                return state

            # 性能指标 (从行为日志而非市场数据)
            if self.behavior_log:
                state["performance"]["consecutive_losses"] = self._count_recent_event_type("consecutive_loss")
                # 滚动夏普可以从 risk_monitor 获取（因为这是系统级衍生指标，非原始市场数据）
                if hasattr(engine, 'risk_monitor'):
                    snap = await engine.risk_monitor.get_snapshot()
                    state["performance"]["rolling_sharpe_24h"] = getattr(snap, 'sharpe', 1.0)

            # 议会状态
            if hasattr(engine, 'agent_council'):
                council = engine.agent_council
                state["council"]["abstain_rate"] = council.get_abstain_rate()

            # 进化工厂
            if hasattr(engine, 'plugin_mgr'):
                state["evolution"]["churn_rate"] = engine.plugin_mgr.get_churn_rate()

            # 感知层元状态（只读标志，非原始数据）
            if hasattr(engine, 'perception'):
                state["perception"]["pll_snr_db"] = engine.perception._last_pll.snr_db
                state["perception"]["jump_detected"] = engine.perception.is_frozen

            # 分布偏移（系统级统计）
            if hasattr(engine, 'distribution_shift'):
                state["mmd_score"] = engine.distribution_shift.compute_mmd()

            # 硬监视器触发次数（从行为日志统计）
            state["hard_watcher_events_24h"] = self._count_recent_event_type("hard_watcher_trigger", hours=24)

        except Exception as e:
            logger.warning("收集系统状态时发生异常: %s", e)

        return state

    def _count_recent_event_type(self, event_type: str, hours: int = 24) -> int:
        """从行为日志中统计最近指定小时内的特定事件数量"""
        if not self.behavior_log:
            return 0
        # 简化：直接查询数据库或内存
        recent = self.behavior_log.get_recent(500)
        cutoff = time.time() - hours * 3600
        count = sum(1 for e in recent if e.ts > cutoff and event_type in e.content.lower())
        return count

    # ========================================================================
    # 休眠 / 唤醒执行
    # ========================================================================

    async def _go_to_sleep(self, doubt: float) -> None:
        """通知引擎进入休眠状态"""
        self._sleeping = True
        self._sleep_start = time.time()
        logger.warning("哥德尔触发休眠 | 怀疑指数 = %.3f", doubt)

        try:
            engine = _get_engine_safe()
            if engine:
                if hasattr(engine, 'freeze_new_entries'):
                    engine.freeze_new_entries(reason="GodelWatcher 休眠")
        except Exception as e:
            logger.error("执行休眠指令失败: %s", e)

        if self.behavior_log:
            self.behavior_log.log(
                EventType.AGENT, "GodelWatcher",
                f"强制休眠 | 怀疑指数 = {doubt:.3f}",
                snapshot={"doubt": doubt}
            )
        if self.notifier:
            await self.notifier.send_alert(
                "CRITICAL",
                "🧠 哥德尔休眠",
                f"自我怀疑指数 {doubt:.2f}，已进入休眠状态。\n"
                f"系统将暂停新开仓，仅保留现有风控。"
            )

    async def _wake_up(self, reason: str = "") -> None:
        """通知引擎解除休眠"""
        self._sleeping = False
        self._awake_start = time.time()
        logger.info("哥德尔苏醒 | 原因: %s", reason)

        try:
            engine = _get_engine_safe()
            if engine:
                if hasattr(engine, 'unfreeze_new_entries'):
                    engine.unfreeze_new_entries()
        except Exception as e:
            logger.error("执行苏醒指令失败: %s", e)

        if self.behavior_log:
            self.behavior_log.log(EventType.AGENT, "GodelWatcher", f"苏醒 | {reason}")
        if self.notifier:
            await self.notifier.send_alert(
                "INFO",
                "🧠 哥德尔苏醒",
                f"系统已解除休眠。原因: {reason}"
            )

    # ========================================================================
    # 公共接口 (供引擎/议会调用)
    # ========================================================================

    async def evaluate(self) -> Dict[str, Any]:
        """
        执行一次评估 (非议会路径，供日常调度使用)。
        返回包含自我怀疑指数、休眠状态等。
        """
        proposal = await self.propose()
        direction = proposal.get("direction", 0)

        if self._sleeping:
            if direction == -1:
                await self._wake_up(proposal.get("reason", "怀疑指数回落"))
            # 否则继续维持休眠
        else:
            if direction == 1:
                await self._go_to_sleep(proposal.get("confidence", self._last_doubt))

        return {
            "self_doubt": round(self._last_doubt, 4),
            "is_sleeping": self._sleeping,
            "sleep_remaining_sec": (
                max(0, self._sleep_start + self.max_sleep_seconds - time.time())
                if self._sleeping else 0
            ),
            "action": "sleep" if direction == 1 else ("wake" if direction == -1 else "none"),
            "timestamp": datetime.now().isoformat()
        }

    async def run_loop(self) -> None:
        """独立运行循环 (可选)"""
        while True:
            await self.evaluate()
            await asyncio.sleep(self.check_interval)

    def get_status(self) -> Dict[str, Any]:
        """返回当前监视者状态"""
        return {
            "self_doubt": round(self._last_doubt, 4),
            "is_sleeping": self._sleeping,
            "sleep_remaining_sec": (
                max(0, self._sleep_start + self.max_sleep_seconds - time.time())
                if self._sleeping else 0
            ),
            "awake_remaining_sec": max(0, self._awake_start + self.min_awake_seconds - time.time()),
            "doubt_history": list(self._doubt_history)[-50:],
        }

    def _get_config(self, key: str, default: Any) -> Any:
        """从引擎配置中获取参数，若不可用则返回默认值"""
        try:
            engine = _get_engine_safe()
            if engine and hasattr(engine, 'config'):
                return engine.config.get(f"godel_watcher.{key}", default)
        except Exception:
            pass
        return default


# ============================================================================
# 引擎安全获取函数 (避免循环导入)
# ============================================================================
def _get_engine_safe():
    """安全获取引擎实例，避免导入错误"""
    try:
        from api.server import get_engine
        return get_engine()
    except Exception:
        return None
