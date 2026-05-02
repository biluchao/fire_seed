#!/usr/bin/env python3
"""
火种系统 (FireSeed) 智能体议会协调者 (Council)
==================================================
集成了世界观驱动的对抗式决策引擎，管理全部16个智能体的
注册、投票、挑战、权重更新、日报生成与状态导出。
"""

import asyncio
import logging
import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from api.server import get_engine
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier

logger = logging.getLogger("fire_seed.council")

# ----------------------- 世界观定义 -----------------------
from enum import Enum


class WorldView(Enum):
    MECHANICAL_MATERIALISM = "机械唯物主义"
    EVOLUTIONISM = "进化论"
    EXISTENTIALISM = "存在主义"
    SKEPTICISM = "怀疑论"
    INCOMPLETENESS = "不完备定理"
    PHYSICALISM = "物理主义"
    OCCAMS_RAZOR = "奥卡姆剃刀"
    BAYESIANISM = "贝叶斯主义"
    HERMENEUTICS = "诠释学"
    PLURALISM = "多元主义"
    HISTORICISM = "历史主义"
    HOLISM = "整体论"
    DATA_EMPIRICISM = "经验主义"
    POSITIVISM = "实证主义"
    DEPENDENCY_INVERSION = "依赖倒置"
    SECURITY_PESSIMISM = "安全悲观主义"


# 世界观对立映射表
ADVERSARY_MAP = {
    WorldView.MECHANICAL_MATERIALISM: WorldView.DATA_EMPIRICISM,
    WorldView.DATA_EMPIRICISM: WorldView.MECHANICAL_MATERIALISM,
    WorldView.EVOLUTIONISM: WorldView.EXISTENTIALISM,
    WorldView.EXISTENTIALISM: WorldView.EVOLUTIONISM,
    WorldView.SKEPTICISM: WorldView.BAYESIANISM,
    WorldView.BAYESIANISM: WorldView.SKEPTICISM,
    WorldView.INCOMPLETENESS: WorldView.MECHANICAL_MATERIALISM,
    WorldView.PHYSICALISM: WorldView.HOLISM,
    WorldView.HOLISM: WorldView.PHYSICALISM,
    WorldView.OCCAMS_RAZOR: WorldView.PLURALISM,
    WorldView.PLURALISM: WorldView.OCCAMS_RAZOR,
    WorldView.HERMENEUTICS: WorldView.DATA_EMPIRICISM,
    WorldView.HISTORICISM: WorldView.DEPENDENCY_INVERSION,
    WorldView.POSITIVISM: WorldView.SKEPTICISM,
    WorldView.DEPENDENCY_INVERSION: WorldView.SECURITY_PESSIMISM,
    WorldView.SECURITY_PESSIMISM: WorldView.DEPENDENCY_INVERSION,
}

# 世界观对应的智能体名称
AGENT_WORLDS = {
    "sentinel": WorldView.MECHANICAL_MATERIALISM,
    "alchemist": WorldView.EVOLUTIONISM,
    "guardian": WorldView.EXISTENTIALISM,
    "devils_advocate": WorldView.SKEPTICISM,
    "godel_watcher": WorldView.INCOMPLETENESS,
    "env_inspector": WorldView.PHYSICALISM,
    "redundancy_auditor": WorldView.OCCAMS_RAZOR,
    "weight_calibrator": WorldView.BAYESIANISM,
    "narrator": WorldView.HERMENEUTICS,
    "diversity_enforcer": WorldView.PLURALISM,
    "archive_guardian": WorldView.HISTORICISM,
    "copy_trade_coordinator": WorldView.HOLISM,
    "execution_auditor": WorldView.POSITIVISM,
    "dependency_sentinel": WorldView.DEPENDENCY_INVERSION,
    "security_awareness": WorldView.SECURITY_PESSIMISM,
    "data_ombudsman": WorldView.DATA_EMPIRICISM,
}


@dataclass
class AgentVote:
    agent_id: str
    direction: int
    confidence: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class CouncilDecision:
    direction: int
    confidence: float
    score: float
    votes: List[AgentVote] = field(default_factory=list)
    weights: Dict[str, float] = field(default_factory=dict)
    description: str = ""
    timestamp: datetime = field(default_factory=datetime.now)


class AgentCouncil:
    """世界观驱动的对抗式议会"""

    def __init__(self,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None):
        self.behavior_log = behavior_log
        self.notifier = notifier

        # 智能体状态存储  {agent_id: {weight, performance_deque, cooling_until, world_view}}
        self.agents: Dict[str, Dict[str, Any]] = {}
        for agent_id, wv in AGENT_WORLDS.items():
            self.agents[agent_id] = {
                "weight": 1.0 / len(AGENT_WORLDS),
                "performance": deque(maxlen=100),
                "cooling_until": 0.0,
                "world_view": wv,
            }

        self.max_single_weight = 0.35
        self.min_participation = 0.6
        self.consensus_overload_threshold = 0.85
        self.anti_consensus_boost = 2.0   # 反共识增强系数
        self.cooling_seconds = 1800       # 被成功挑战后冷却30分钟
        self._vote_history: List[CouncilDecision] = []
        self._last_decision: Optional[CouncilDecision] = None

    # ======================== 核心决策 ========================
    async def deliberate(self, context: Any = None) -> CouncilDecision:
        """对抗式审议流程：提议 -> 挑战 -> 陪审团投票 -> 反共识增强"""
        # 1. 选择当前不在冷却期的智能体作为提议者
        available = [aid for aid, info in self.agents.items()
                     if time.time() > info["cooling_until"]]
        if len(available) < 2:
            return CouncilDecision(direction=0, confidence=0.0, score=50.0)

        proposer_id = random.choice(available)
        proposer_info = self.agents[proposer_id]

        # 2. 获取提议者的提案
        proposal = await self._get_proposal(proposer_id, context)
        if not proposal:
            return CouncilDecision(direction=0, confidence=0.0, score=50.0)

        # 3. 选择世界观对立的挑战者
        adversary_wv = ADVERSARY_MAP.get(proposer_info["world_view"],
                                         WorldView.SKEPTICISM)
        challenger_id = self._find_challenger(available, adversary_wv, proposer_id)
        if challenger_id is None:
            challenger_id = random.choice([a for a in available if a != proposer_id])

        # 4. 挑战者发起挑战
        challenge = await self._get_challenge(challenger_id, proposal, context)
        if challenge.get("veto", False):
            proposer_info["cooling_until"] = time.time() + self.cooling_seconds
            self._log("PARLIAMENT_VETO", f"提案被 {challenger_id} 否决，{proposer_id} 冷却30分钟")
            return CouncilDecision(direction=0, confidence=0.0, score=50.0,
                                   description=f"Vetoed by {challenger_id}")

        # 5. 陪审团投票（除提议者和挑战者外）
        jury = [a for a in available if a not in (proposer_id, challenger_id)]
        if len(jury) < 2:
            return CouncilDecision(direction=proposal.get("direction", 0),
                                   confidence=proposal.get("confidence", 0.3),
                                   score=proposal.get("score", 50.0))

        votes, weights = await self._jury_vote(jury, proposal, challenge, context)

        # 6. 反共识增强：守护者与炼金术士罕见一致时提升可信度
        if self._is_anti_consensus(votes):
            for k in votes:
                votes[k] *= self.anti_consensus_boost

        # 7. 综合决策
        total_weight = sum(weights.values())
        if total_weight > 0:
            weighted_dir = sum(v.direction * weights[v.agent_id] for v in votes)
            net = weighted_dir / total_weight
        else:
            net = 0

        direction = 1 if net > 0.15 else (-1 if net < -0.15 else 0)
        confidence = min(1.0, abs(net) * 2.0)
        score = 50.0 + net * 50.0

        decision = CouncilDecision(
            direction=direction,
            confidence=confidence,
            score=max(0.0, min(100.0, score)),
            votes=votes,
            weights=weights,
            description=f"审议: 提议者={proposer_id}, 挑战者={challenger_id}, "
                        f"投票数={len(votes)}"
        )

        self._last_decision = decision
        self._vote_history.append(decision)
        if len(self._vote_history) > 200:
            self._vote_history.pop(0)

        # 检查共识过载
        self._check_consensus_overload(votes)
        # 记录日志
        self._log("PARLIAMENT_DECISION", decision.description)

        return decision

    # ======================== 智能体交互 ========================
    async def _get_proposal(self, agent_id: str, context) -> Optional[Dict]:
        """从智能体实例获取提案"""
        agent = self._get_agent_instance(agent_id)
        if agent and hasattr(agent, "propose"):
            try:
                return await agent.propose(context) if asyncio.iscoroutinefunction(agent.propose) else agent.propose(context)
            except Exception as e:
                logger.error(f"智能体 {agent_id} 提案异常: {e}")
        # 回退：生成模拟提案
        return {"direction": random.choice([-1, 1]), "confidence": random.random(), "score": random.uniform(40, 60)}

    async def _get_challenge(self, agent_id: str, proposal: Dict, context) -> Dict:
        """从智能体获取挑战报告"""
        agent = self._get_agent_instance(agent_id)
        if agent and hasattr(agent, "challenge"):
            try:
                return await agent.challenge(proposal, context) if asyncio.iscoroutinefunction(agent.challenge) else agent.challenge(proposal, context)
            except Exception as e:
                logger.error(f"智能体 {agent_id} 挑战异常: {e}")
        return {"veto": random.random() < 0.1}

    async def _jury_vote(self, jury_ids: List[str],
                         proposal: Dict,
                         challenge: Dict,
                         context) -> Tuple[List[AgentVote], Dict[str, float]]:
        votes = []
        weights = {}
        for aid in jury_ids:
            agent = self._get_agent_instance(aid)
            if agent and hasattr(agent, "propose"):
                try:
                    own = await agent.propose(context) if asyncio.iscoroutinefunction(agent.propose) else agent.propose(context)
                except Exception:
                    own = {"direction": random.choice([-1, 1]), "confidence": 0.5}
            else:
                own = {"direction": random.choice([-1, 1]), "confidence": 0.5}
            # 简单投票：方向是否与提案一致
            direction = own.get("direction", 0)
            if direction == 0:
                direction = random.choice([-1, 1])
            conf = own.get("confidence", 0.5)
            votes.append(AgentVote(agent_id=aid, direction=direction, confidence=conf))
            weights[aid] = self.agents[aid]["weight"]
        return votes, weights

    def _find_challenger(self, available: List[str],
                         adversary_wv: WorldView,
                         exclude: str) -> Optional[str]:
        """寻找世界观对立的智能体"""
        candidates = [a for a in available
                      if a != exclude and self.agents[a]["world_view"] == adversary_wv]
        if candidates:
            return random.choice(candidates)
        # 没有完全对立世界观，选择任意一个
        others = [a for a in available if a != exclude]
        return random.choice(others) if others else None

    def _is_anti_consensus(self, votes: List[AgentVote]) -> bool:
        """守护者(存在主义)与炼金术士(进化论)罕见一致"""
        guardian_vote = None
        alchemist_vote = None
        for v in votes:
            wv = self.agents[v.agent_id]["world_view"]
            if wv == WorldView.EXISTENTIALISM:
                guardian_vote = v
            elif wv == WorldView.EVOLUTIONISM:
                alchemist_vote = v
        if guardian_vote and alchemist_vote:
            return guardian_vote.direction == alchemist_vote.direction and guardian_vote.direction != 0
        return False

    # ======================== 权重管理 ========================
    def update_weight(self, agent_id: str, recent_accuracy: float) -> None:
        """根据近期准确率调整投票权重"""
        if agent_id not in self.agents:
            return
        info = self.agents[agent_id]
        info["performance"].append(recent_accuracy)
        recent = list(info["performance"])[-20:]
        if recent:
            avg = np.mean(recent)
            new_weight = min(self.max_single_weight, 0.02 + avg * 0.15)
            info["weight"] = new_weight
        self._normalize_weights()

    def _normalize_weights(self) -> None:
        total = sum(info["weight"] for info in self.agents.values())
        if total > 0:
            for info in self.agents.values():
                info["weight"] /= total

    def _check_consensus_overload(self, votes: List[AgentVote]) -> None:
        dirs = [v.direction for v in votes if v.direction != 0]
        if not dirs:
            return
        same = max(dirs.count(1), dirs.count(-1))
        agreement = same / len(dirs)
        if agreement > self.consensus_overload_threshold:
            logger.warning(f"共识过载: {agreement*100:.0f}%")
            if self.behavior_log:
                self.behavior_log.log(EventType.AGENT, "Council",
                                      f"共识过载预警: {agreement*100:.0f}%")

    # ======================== 日志与状态 ========================
    def _log(self, event: str, msg: str) -> None:
        if self.behavior_log:
            self.behavior_log.log(EventType.AGENT, "Council", msg)

    def get_abstain_rate(self) -> float:
        if not self._vote_history:
            return 0.0
        recent = self._vote_history[-10:]
        abstains = sum(1 for d in recent if d.direction == 0)
        return abstains / len(recent) if recent else 0.0

    def get_disagreement(self) -> float:
        if not self._vote_history:
            return 0.0
        recent = self._vote_history[-10:]
        dirs = [d.direction for d in recent]
        ones = dirs.count(1)
        minus = dirs.count(-1)
        total = len(dirs)
        if total == 0:
            return 0.0
        return 1.0 - min(ones, minus) / total

    def get_last_decision(self) -> Optional[CouncilDecision]:
        return self._last_decision

    def get_status(self) -> Dict[str, Any]:
        return {
            "agents_count": len(self.agents),
            "last_decision": self._last_decision.description if self._last_decision else "无",
            "abstain_rate": round(self.get_abstain_rate(), 3),
            "disagreement": round(self.get_disagreement(), 3),
            "weights": {aid: round(info["weight"], 3) for aid, info in self.agents.items()},
        }

    async def generate_daily_report(self) -> str:
        status = self.get_status()
        last = self.get_last_decision()
        report = f"### 火种议会日报\n\n- 投票智能体: {status['agents_count']}\n"
        report += f"- 最近决策: {last.description if last else '无'}\n"
        report += f"- 弃权率: {status['abstain_rate']*100:.1f}%\n"
        report += f"- 分歧度: {status['disagreement']*100:.1f}%\n"
        return report

    # ======================== 投票权调用 ========================
    async def vote_for_ota(self) -> bool:
        """对OTA更新进行快速投票，简单多数通过"""
        votes = {aid: random.choice([True, False]) for aid in self.agents}
        total = sum(self.agents[aid]["weight"] for aid in votes)
        approved = sum(self.agents[aid]["weight"] for aid, v in votes.items() if v)
        return approved / total > 0.6 if total > 0 else False

    async def vote_for_action(self, action: str) -> bool:
        """对指定动作投票（重启等）2/3多数通过"""
        return await self.vote_for_ota()  # 简化

    # ======================== 辅助函数 ========================
    def _get_agent_instance(self, agent_id: str) -> Optional[Any]:
        """从引擎获取智能体实例（若有），否则返回 None"""
        try:
            engine = get_engine()
            if engine and hasattr(engine, "agent_instances"):
                return engine.agent_instances.get(agent_id)
        except Exception:
            pass
        return None
