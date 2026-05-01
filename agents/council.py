#!/usr/bin/env python3
"""
火种系统 (FireSeed) 智能体议会协调者 (Council) ── 对抗式版本
================================================================
职责：
- 内部集成对抗式议会 (AdversarialCouncil)
- 保留原有状态查询接口 (弃权率、分歧度、日报生成)
- 将决策结果转换为统一的 CouncilDecision 格式
- 为 OTA 等特殊议题提供独立投票通道
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from agents.worldview import WorldViewAgent, WorldView
from agents.adversarial_council import AdversarialCouncil
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier

logger = logging.getLogger("fire_seed.council")


@dataclass
class AgentVote:
    """单个智能体的原始投票（内部使用，不再作为主要决策依据）"""
    agent_id: str
    direction: int
    confidence: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class CouncilDecision:
    """议会最终决策（保持向外一致的接口）"""
    direction: int
    confidence: float
    score: float
    votes: List[AgentVote] = field(default_factory=list)
    weights: Dict[str, float] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    description: str = ""
    # 对抗式议会的内部记录
    adversarial_record: Optional[Dict[str, Any]] = None


class AgentCouncil:
    """
    智能体议会协调者（对抗式版本）。

    内部使用 AdversarialCouncil 进行基于世界观的庭审式决策，
    对外仍保持传统的查询接口。
    """

    def __init__(self,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None):
        self.behavior_log = behavior_log
        self.notifier = notifier

        # 对抗式议会核心
        self._adversarial = AdversarialCouncil()

        # 历史决策记录（旧格式用于统计）
        self._decision_history: List[CouncilDecision] = []
        self._last_decision: Optional[CouncilDecision] = None

        # 初始化智能体并注入世界观（会在首次运行前调用）
        self._agents_initialized = False

        logger.info("对抗式议会协调者初始化完成")

    # ────────────────── 智能体注册 ──────────────────
    def _ensure_agents_initialized(self) -> None:
        """延迟初始化所有携带世界观的智能体（避免循环导入）"""
        if self._agents_initialized:
            return

        # 从 agents 模块动态导入各智能体的具体类
        try:
            from agents.sentinel import SentinelAgent
            from agents.alchemist import AlchemistAgent
            from agents.guardian import GuardianAgent
            from agents.devils_advocate import DevilsAdvocate
            from agents.godel_watcher import GodelWatcher
            from agents.env_inspector import EnvInspector
            from agents.redundancy_auditor import RedundancyAuditor
            from agents.weight_calibrator import WeightCalibrator
            from agents.narrator import NarratorAgent
            from agents.diversity_enforcer import DiversityEnforcer
            from agents.archive_guardian import ArchiveGuardian
            from agents.copy_trade_coordinator import CopyTradeCoordinator
        except ImportError as e:
            logger.warning(f"无法导入智能体模块，议会将使用模拟逻辑: {e}")
            self._agents_initialized = True
            return

        # 注册顺序不影响决策，但需确保12个全部到位
        self._adversarial.register_agent("sentinel", SentinelAgent(
            behavior_log=self.behavior_log, notifier=self.notifier))
        self._adversarial.register_agent("alchemist", AlchemistAgent(
            behavior_log=self.behavior_log, notifier=self.notifier))
        self._adversarial.register_agent("guardian", GuardianAgent(
            behavior_log=self.behavior_log, notifier=self.notifier))
        self._adversarial.register_agent("devils_advocate", DevilsAdvocate(
            behavior_log=self.behavior_log, notifier=self.notifier))
        self._adversarial.register_agent("godel_watcher", GodelWatcher(
            behavior_log=self.behavior_log, notifier=self.notifier))
        self._adversarial.register_agent("env_inspector", EnvInspector(
            behavior_log=self.behavior_log, notifier=self.notifier))
        self._adversarial.register_agent("redundancy_auditor", RedundancyAuditor(
            behavior_log=self.behavior_log, notifier=self.notifier))
        self._adversarial.register_agent("weight_calibrator", WeightCalibrator(
            behavior_log=self.behavior_log, notifier=self.notifier))
        self._adversarial.register_agent("narrator", NarratorAgent(
            behavior_log=self.behavior_log, notifier=self.notifier))
        self._adversarial.register_agent("diversity_enforcer", DiversityEnforcer(
            behavior_log=self.behavior_log, notifier=self.notifier))
        self._adversarial.register_agent("archive_guardian", ArchiveGuardian(
            behavior_log=self.behavior_log, notifier=self.notifier))
        self._adversarial.register_agent("copy_trade_coordinator", CopyTradeCoordinator(
            behavior_log=self.behavior_log, notifier=self.notifier))

        self._agents_initialized = True
        logger.info("12 个世界观智能体已注册到对抗式议会")

    # ────────────────── 核心决策 ──────────────────
    async def deliberate(self, perception: Optional[Dict[str, Any]] = None) -> CouncilDecision:
        """
        对抗式议会审议：启动“提议-挑战-陪审”流程，生成最终决策。
        """
        self._ensure_agents_initialized()

        # 调用 AdversarialCouncil 的异步审议
        result = await self._adversarial.deliberate(perception or {})

        # 提取方向、置信度、评分
        direction = result.get("direction", 0)
        confidence = result.get("confidence", 0.0)
        # 评分映射：50 + direction * confidence * 50
        score = 50.0 + direction * confidence * 50.0
        score = max(0.0, min(100.0, score))

        # 构建描述文本
        record = result.get("record", {})
        proposer = record.get("proposer", "?")
        challenger = record.get("challenger", "?")
        if result.get("status") == "vetoed":
            description = f"挑战成功 ({challenger})，否决 ({proposer})"
        else:
            description = f"对抗式决策: 提案({proposer}) 挑战({challenger}) 方向={'多' if direction == 1 else '空' if direction == -1 else '中性'}"

        decision = CouncilDecision(
            direction=direction,
            confidence=confidence,
            score=score,
            description=description,
            adversarial_record=result,
        )

        self._last_decision = decision
        self._decision_history.append(decision)
        if len(self._decision_history) > 200:
            self._decision_history = self._decision_history[-200:]

        # 记录行为日志
        if self.behavior_log:
            self.behavior_log.log(
                EventType.AGENT, "Council",
                f"对抗式议会决策: {description}",
                snapshot={
                    "direction": direction,
                    "confidence": confidence,
                    "score": score,
                }
            )

        return decision

    # ────────────────── 历史统计接口 (保持向前兼容) ──────────────────
    def get_last_decision(self) -> Optional[CouncilDecision]:
        """获取最近一次决策"""
        return self._last_decision

    def get_abstain_rate(self) -> float:
        """计算近期决策中的弃权比例（判决为中性视为弃权）"""
        if not self._decision_history:
            return 0.0
        recent = self._decision_history[-10:]
        neutral_count = sum(1 for d in recent if d.direction == 0)
        return neutral_count / len(recent)

    def get_disagreement(self) -> float:
        """
        计算议会分歧度：近期决策中，被否决的比例。
        （因为对抗式议会天然包含对抗，这里用否决率表示分歧）
        """
        if not self._decision_history:
            return 0.0
        recent = self._decision_history[-10:]
        vetoed = sum(
            1 for d in recent
            if d.adversarial_record and d.adversarial_record.get("status") == "vetoed"
        )
        return vetoed / len(recent)

    def get_status(self) -> Dict[str, Any]:
        """返回议会状态摘要"""
        return {
            "type": "adversarial_council",
            "agents_count": len(self._adversarial.agents),
            "last_decision": self._last_decision.description if self._last_decision else "无",
            "abstain_rate": round(self.get_abstain_rate(), 3),
            "disagreement": round(self.get_disagreement(), 3),
            "verdict_history_len": len(self._adversarial.verdict_history),
        }

    # ────────────────── 特殊议题投票 ──────────────────
    async def vote_for_ota(self) -> bool:
        """就OTA更新进行特别投票（简化：使用对抗式议会的陪审团机制）"""
        self._ensure_agents_initialized()
        # 构造一个模拟的感知上下文，让议会决定是否批准更新
        perception = {"action": "ota_update", "context": "request_permission"}
        decision = await self.deliberate(perception)
        # 如果方向非零且置信度 > 0.5，视为通过
        return decision.direction != 0 and decision.confidence > 0.5

    async def vote_for_action(self, action: str) -> bool:
        """就指定行动（重启、模式切换等）投票"""
        perception = {"action": action, "context": "request_permission"}
        decision = await self.deliberate(perception)
        return decision.direction != 0 and decision.confidence > 0.5

    # ────────────────── 日报生成 ──────────────────
    async def generate_daily_report(self) -> str:
        """生成议会日报（叙事官辅助）"""
        status = self.get_status()
        last = self.get_last_decision()
        report = "### 火种对抗式议会日报\n\n"
        report += f"- 参与智能体: {status['agents_count']}\n"
        report += f"- 最近决策: {last.description if last else '无'}\n"
        report += f"- 弃权率: {status['abstain_rate']*100:.1f}%\n"
        report += f"- 分歧度(否决率): {status['disagreement']*100:.1f}%\n"
        report += f"- 历史判决数: {status['verdict_history_len']}\n"
        return report

    # ────────────────── 内部辅助 ──────────────────
    def _log(self, message: str, snapshot: Optional[Dict] = None) -> None:
        if self.behavior_log:
            self.behavior_log.info(EventType.AGENT, "Council", message, snapshot)
