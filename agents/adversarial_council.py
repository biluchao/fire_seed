#!/usr/bin/env python3
"""
火种系统 (FireSeed) 对抗式议会 (AdversarialCouncil)
========================================================
基于英美法系庭审对抗的超级决策引擎。工作流程：
1. 随机选择一名不在冷却期的智能体作为「提议者」
2. 选择一名世界观与提议者对立的智能体作为「挑战者」
3. 若挑战成功（veto），提议者进入冷却期
4. 若挑战未通过，由其他智能体组成「陪审团」投票
5. 检测反共识（守护者 vs 炼金术士）并增强信号
6. 输出最终方向与置信度

特性：
- 16 智能体各自携带不可调和的哲学世界观
- 信息源隔离、时间尺度分裂、奖励函数极端化
- 持续演化反共识规则
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Set

import numpy as np

from agents.worldview import (
    WorldView,
    WorldViewAgent,
    WorldViewManifesto,
)

logger = logging.getLogger("fire_seed.adversarial_council")


@dataclass
class CouncilRecord:
    """单轮审议的完整记录"""
    round_number: int
    timestamp: datetime
    proposer: str
    proposer_worldview: str
    proposal: Dict[str, Any]
    challenger: str
    challenger_worldview: str
    challenge_result: str          # "vetoed" / "overruled"
    jury_votes: Dict[str, float]   # 每个陪审员的名字 -> 投票值
    final_direction: int
    final_confidence: float
    anti_consensus_triggered: bool = False
    notes: str = ""


@dataclass
class AgentRegistryEntry:
    """智能体注册项"""
    name: str
    agent: WorldViewAgent
    manifesto: WorldViewManifesto
    cool_down_until: float = 0.0
    recent_proposals: List[Dict] = field(default_factory=list)
    challenge_wins: int = 0
    challenge_losses: int = 0
    proposal_accepted: int = 0
    proposal_rejected: int = 0


class AdversarialCouncil:
    """
    对抗式议会。
    每个智能体带着自己的世界观参与审议，通过抗辩制碰撞出最优决策。
    """

    def __init__(self, behavior_log=None, config: Dict = None):
        self.behavior_log = behavior_log
        self.config = config or {}

        # 已注册的智能体
        self.agents: Dict[str, AgentRegistryEntry] = {}

        # 审议历史
        self.history: List[CouncilRecord] = []
        self.round_counter = 0

        # 配置参数
        self.cooling_off_seconds = self.config.get(
            "adversarial_council.cooling_off_seconds", 1800
        )  # 默认 30 分钟
        self.min_jury_participation = self.config.get(
            "adversarial_council.min_jury_participation", 0.6
        )
        self.anti_consensus_multiplier = self.config.get(
            "adversarial_council.anti_consensus_multiplier", 2.0
        )

        logger.info("对抗式议会初始化完成")

    # ======================== 智能体管理 ========================
    def register_agent(self, name: str, agent: WorldViewAgent) -> None:
        """注册一个携带世界观的智能体"""
        if name in self.agents:
            logger.warning(f"智能体 {name} 已注册，将被覆盖")

        manifesto = agent.manifesto if hasattr(agent, 'manifesto') else WorldViewManifesto(
            worldview=WorldView.MECHANICAL_MATERIALISM,
            core_belief="默认",
            primary_optimization_target="未知",
            adversary_worldview=WorldView.SKEPTICISM,
        )

        self.agents[name] = AgentRegistryEntry(
            name=name,
            agent=agent,
            manifesto=manifesto,
        )
        logger.info(f"智能体已注册: {name} ({manifesto.worldview.value})")

    def unregister_agent(self, name: str) -> bool:
        """注销智能体"""
        if name in self.agents:
            del self.agents[name]
            return True
        return False

    def get_active_agents(self) -> List[Tuple[str, AgentRegistryEntry]]:
        """获取所有不在冷却期的智能体"""
        now = time.time()
        active = []
        for name, entry in self.agents.items():
            if now >= entry.cool_down_until:
                active.append((name, entry))
        return active

    # ======================== 核心审议流程 ========================
    async def deliberate(self, context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        一轮完整的对抗式议会审议。
        返回最终决策：{direction, confidence, record, ...}
        """
        self.round_counter += 1
        context = context or {}
        record = CouncilRecord(
            round_number=self.round_counter,
            timestamp=datetime.now(),
            proposer="",
            proposer_worldview="",
            proposal={},
            challenger="",
            challenger_worldview="",
            challenge_result="pending",
            jury_votes={},
            final_direction=0,
            final_confidence=0.0,
        )

        # 1. 确定参与者
        active = self.get_active_agents()
        if len(active) < 3:
            logger.warning(f"活跃智能体不足 ({len(active)} < 3)，审议终止")
            record.notes = "活跃智能体不足"
            self.history.append(record)
            return {
                "direction": 0,
                "confidence": 0.0,
                "status": "insufficient_agents",
                "record": self._record_to_dict(record),
            }

        # 2. 选择提议者（随机）
        proposer_name, proposer_entry = random.choice(active)
        record.proposer = proposer_name
        record.proposer_worldview = proposer_entry.manifesto.worldview.value

        # 3. 提议者生成提案
        try:
            proposal = proposer_entry.agent.propose(context)
        except Exception as e:
            logger.error(f"提议者 {proposer_name} 提案失败: {e}")
            proposal = {"direction": 0, "confidence": 0.0}
        record.proposal = proposal

        # 4. 选择挑战者（世界观对立的智能体）
        challenger_name, challenger_entry = self._select_adversary(
            proposer_name, proposer_entry, active
        )
        record.challenger = challenger_name
        record.challenger_worldview = challenger_entry.manifesto.worldview.value

        # 5. 挑战者发起挑战
        try:
            challenge = challenger_entry.agent.challenge(
                proposal,
                challenger_entry.manifesto.worldview,
            )
        except Exception as e:
            logger.error(f"挑战者 {challenger_name} 挑战失败: {e}")
            challenge = {"veto": False, "reason": "挑战过程异常"}

        # 6. 是否被否决
        if challenge.get("veto", False):
            # 否决成功：提议者进入冷却期
            proposer_entry.cool_down_until = time.time() + self.cooling_off_seconds
            proposer_entry.challenge_losses += 1
            proposer_entry.proposal_rejected += 1
            challenger_entry.challenge_wins += 1

            record.challenge_result = "vetoed"
            record.notes = f"否决理由: {challenge.get('reason', '未提供')}"
            self.history.append(record)

            # 记录行为日志
            if self.behavior_log:
                self.behavior_log.log(
                    EventType.AGENT, "AdversarialCouncil",
                    f"提案被否决: {proposer_name}({record.proposer_worldview}) "
                    f"by {challenger_name}({record.challenger_worldview})",
                    snapshot={"reason": challenge.get("reason", "")}
                )

            return {
                "direction": 0,
                "confidence": 0.0,
                "status": "vetoed",
                "record": self._record_to_dict(record),
            }

        # 7. 陪审团投票
        record.challenge_result = "overruled"
        challenger_entry.challenge_losses += 1

        jury = [
            (name, entry)
            for name, entry in active
            if name not in (proposer_name, challenger_name)
        ]

        if len(jury) < 2:
            logger.warning("陪审团人数不足")
            record.notes = "陪审团不足"
            self.history.append(record)
            return {
                "direction": proposal.get("direction", 0),
                "confidence": 0.3,
                "status": "tiny_jury",
                "record": self._record_to_dict(record),
            }

        # 8. 陪审员评估
        for juror_name, juror_entry in jury:
            try:
                vote = juror_entry.agent.evaluate_proposal(proposal, challenge)
                record.jury_votes[juror_name] = vote
            except Exception as e:
                logger.error(f"陪审员 {juror_name} 评估失败: {e}")
                record.jury_votes[juror_name] = 0.0

        # 9. 反共识增强
        anti_consensus = self._check_anti_consensus(record.jury_votes)
        record.anti_consensus_triggered = anti_consensus

        if anti_consensus:
            for name in record.jury_votes:
                record.jury_votes[name] *= self.anti_consensus_multiplier

        # 10. 综合决策
        total_votes = sum(record.jury_votes.values())
        confidence = min(1.0, abs(total_votes) / max(len(record.jury_votes), 1))
        direction = 1 if total_votes > 0 else (-1 if total_votes < 0 else 0)

        record.final_direction = direction
        record.final_confidence = confidence

        # 更新提议者统计
        proposer_entry.proposal_accepted += 1

        self.history.append(record)

        if self.behavior_log:
            self.behavior_log.log(
                EventType.AGENT, "AdversarialCouncil",
                f"审议通过: 方向={'多' if direction==1 else '空' if direction==-1 else '中性'}, "
                f"置信度={confidence:.2f}, 反共识={anti_consensus}",
                snapshot={"jury_votes": record.jury_votes, "anti_consensus": anti_consensus}
            )

        return {
            "direction": direction,
            "confidence": confidence,
            "status": "deliberated",
            "anti_consensus": anti_consensus,
            "record": self._record_to_dict(record),
        }

    # ======================== 挑战者选择 ========================
    def _select_adversary(
        self,
        proposer_name: str,
        proposer_entry: AgentRegistryEntry,
        active: List[Tuple[str, AgentRegistryEntry]],
    ) -> Tuple[str, AgentRegistryEntry]:
        """根据世界观对立原则选择挑战者"""

        # 提议者声明的天然对立世界观
        adversary_worldview = proposer_entry.manifesto.adversary_worldview

        # 第一步：寻找世界观精确匹配的挑战者
        candidates = [
            (name, entry)
            for name, entry in active
            if name != proposer_name
            and entry.manifesto.worldview == adversary_worldview
        ]

        if candidates:
            return random.choice(candidates)

        # 第二步：若无精确匹配，选择世界观距离最远的
        remaining = [
            (name, entry)
            for name, entry in active
            if name != proposer_name
        ]

        if not remaining:
            # 极端情况：所有活跃智能体只有提议者自己
            return proposer_name, proposer_entry

        # 计算世界观距离（基于枚举值的哈希差值）
        proposer_wv = proposer_entry.manifesto.worldview
        farthest = max(
            remaining,
            key=lambda x: abs(
                hash(x[1].manifesto.worldview) - hash(proposer_wv)
            ),
        )
        return farthest

    # ======================== 反共识检测 ========================
    def _check_anti_consensus(self, jury_votes: Dict[str, float]) -> bool:
        """
        检测守护者（存在主义）与炼金术士（进化论）是否罕见地达成一致。
        这是一对几乎永远对立的智能体，当它们同时支持同一方向时，
        往往意味着信号极其可靠（或极其危险）。
        """
        guardian_found = False
        alchemist_found = False

        for name, vote in jury_votes.items():
            entry = self.agents.get(name)
            if entry is None:
                continue
            worldview = entry.manifesto.worldview
            if worldview == WorldView.EXISTENTIALISM and vote > 0:
                guardian_found = True
            if worldview == WorldView.EVOLUTIONISM and vote > 0:
                alchemist_found = True

        return guardian_found and alchemist_found

    # ======================== 状态查询 ========================
    def get_status(self) -> Dict[str, Any]:
        """返回议会当前状态"""
        active_count = len(self.get_active_agents())
        return {
            "total_agents": len(self.agents),
            "active_agents": active_count,
            "cooling_off_agents": len(self.agents) - active_count,
            "rounds_completed": self.round_counter,
            "last_verdict": self._record_to_dict(self.history[-1]) if self.history else None,
            "agent_stats": {
                name: {
                    "worldview": entry.manifesto.worldview.value,
                    "cooling_off": max(0, entry.cool_down_until - time.time()),
                    "proposals_accepted": entry.proposal_accepted,
                    "proposals_rejected": entry.proposal_rejected,
                    "challenge_wins": entry.challenge_wins,
                    "challenge_losses": entry.challenge_losses,
                }
                for name, entry in self.agents.items()
            },
        }

    def get_recent_history(self, limit: int = 20) -> List[Dict]:
        """获取最近审议记录"""
        return [self._record_to_dict(r) for r in self.history[-limit:]]

    @staticmethod
    def _record_to_dict(record: CouncilRecord) -> Dict[str, Any]:
        if record is None:
            return {}
        return {
            "round": record.round_number,
            "timestamp": record.timestamp.isoformat(),
            "proposer": record.proposer,
            "proposer_worldview": record.proposer_worldview,
            "challenger": record.challenger,
            "challenger_worldview": record.challenger_worldview,
            "challenge_result": record.challenge_result,
            "jury_votes": record.jury_votes,
            "final_direction": record.final_direction,
            "final_confidence": record.final_confidence,
            "anti_consensus": record.anti_consensus_triggered,
            "notes": record.notes,
        }

    # ======================== 周期运行 ========================
    async def run_loop(self, interval_sec: int = 300) -> None:
        """独立运行循环（由引擎驱动）"""
        while True:
            await self.deliberate()
            await asyncio.sleep(interval_sec)
