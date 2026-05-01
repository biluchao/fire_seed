#!/usr/bin/env python3
"""
火种系统 (FireSeed) 对抗式议会 (AdversarialCouncil)
=========================================================
模拟英美法系庭审对抗制进行智能体决策：
- 提议者 (Proposer) 基于自身世界观提出决策建议
- 挑战者 (Challenger) 从对立世界观出发进行抗辩
- 若挑战成功，提议者进入冷却期，本轮决策被否决
- 若挑战未通过，陪审团 (Jury) 投票表决
- 反共识增强：当存在主义与进化论罕见一致时，可信度加倍
- 哥德尔监视者的怀疑指数作为全局修正因子
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from agents.worldview import WorldView, WorldViewAgent, WorldViewManifesto
from agents.extreme_rewards import ExtremeRewardFunctions

logger = logging.getLogger("fire_seed.adversarial_council")


@dataclass
class ParliamentRecord:
    """一轮议会审议的完整记录"""
    round_number: int
    timestamp: datetime = field(default_factory=datetime.now)
    proposer: str = ""
    proposer_worldview: str = ""
    proposal: Dict[str, Any] = field(default_factory=dict)
    challenger: str = ""
    challenger_worldview: str = ""
    challenge_result: Dict[str, Any] = field(default_factory=dict)
    vetoed: bool = False
    jury_votes: Dict[str, float] = field(default_factory=dict)
    verdict_direction: int = 0
    verdict_confidence: float = 0.0
    anti_consensus_triggered: bool = False
    godel_doubt_applied: float = 0.0


class AdversarialCouncil:
    """
    对抗式议会。

    决策流程：
    1. 从所有不在冷却期的智能体中随机选择提议者
    2. 选择世界观对立的智能体作为挑战者
    3. 挑战者提出抗辩，若抗辩成立（veto=True），提案被否决，提议者进入冷却
    4. 否则，剩余智能体组成陪审团进行投票
    5. 若存在主义与进化论罕见一致，触发反共识增强
    6. 应用哥德尔监视者的怀疑指数修正最终置信度
    """

    def __init__(self,
                 godel_watcher=None,
                 narrator=None,
                 behavior_log=None,
                 config: Optional[Dict[str, Any]] = None):
        """
        :param godel_watcher: 哥德尔监视者实例（用于获取怀疑指数）
        :param narrator:      叙事官实例（用于记录审议过程）
        :param behavior_log:  行为日志实例
        :param config:        配置参数
        """
        self.agents: Dict[str, WorldViewAgent] = {}
        self.godel_watcher = godel_watcher
        self.narrator = narrator
        self.behavior_log = behavior_log

        # 配置参数
        cfg = config or {}
        self.cooling_period_sec = cfg.get("adversarial_council.cooling_period_sec", 1800)  # 30分钟
        self.min_jury_size = cfg.get("adversarial_council.min_jury_size", 2)
        self.anti_consensus_multiplier = cfg.get("adversarial_council.anti_consensus_multiplier", 2.0)
        self.godel_doubt_threshold = cfg.get("adversarial_council.godel_doubt_threshold", 0.7)

        # 审议历史
        self.records: List[ParliamentRecord] = []
        self.round_count = 0

        logger.info("对抗式议会初始化完成")

    # ==================== 智能体注册 ====================
    def register_agent(self, name: str, agent: WorldViewAgent) -> None:
        """注册一个携带世界观的智能体"""
        self.agents[name] = agent
        logger.info(f"议会注册智能体: {name} ({agent.manifesto.worldview.value})")

    def unregister_agent(self, name: str) -> bool:
        """移除一个智能体"""
        if name in self.agents:
            del self.agents[name]
            return True
        return False

    # ==================== 核心审议 ====================
    async def deliberate(self, perception: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        执行一轮完整的对抗式审议。

        :param perception: 当前市场感知快照（可由引擎提供）
        :return: 最终决策字典，包含 direction, confidence, record
        """
        self.round_count += 1
        record = ParliamentRecord(round_number=self.round_count)

        # 1. 收集所有不在冷却期的智能体
        now = time.time()
        available = [
            a for a in self.agents.values()
            if now >= getattr(a, 'cooling_off_until', 0.0)
        ]
        if len(available) < 2:
            logger.debug(f"可用智能体不足 ({len(available)}<2)，返回中性决策")
            return {
                "direction": 0,
                "confidence": 0.0,
                "status": "insufficient_agents",
                "record": record
            }

        # 2. 随机选择提议者（排除魔鬼代言人作为提议者，因其天职是挑战）
        proposer_candidates = [a for a in available
                               if a.manifesto.worldview != WorldView.SKEPTICISM]
        if not proposer_candidates:
            proposer_candidates = available
        proposer = random.choice(proposer_candidates)

        # 3. 生成提案
        proposal = proposer.propose(perception)
        record.proposer = self._agent_name(proposer)
        record.proposer_worldview = proposer.manifesto.worldview.value
        record.proposal = proposal

        # 4. 选择世界观对立的挑战者
        remaining = [a for a in available if a != proposer]
        challenger = self._select_adversary(proposer, remaining)

        # 5. 挑战者提出抗辩
        challenge = challenger.challenge(proposal, challenger.manifesto.worldview)
        record.challenger = self._agent_name(challenger)
        record.challenger_worldview = challenger.manifesto.worldview.value
        record.challenge_result = challenge

        # 6. 若挑战成功（veto），否决提案
        if challenge.get("veto", False):
            record.vetoed = True
            self._apply_cooling(proposer)
            logger.info(f"提案被否决: {record.proposer} 被 {record.challenger} 成功挑战")
            self._log_event("PARLIAMENT_VETO", record)
            return {
                "direction": 0,
                "confidence": 0.0,
                "status": "vetoed",
                "reason": challenge.get("reason", "挑战成功"),
                "record": record
            }

        # 7. 挑战未通过，组成陪审团投票
        jury = [a for a in available if a not in (proposer, challenger)]
        if len(jury) < self.min_jury_size:
            direction = proposal.get("direction", 0)
            confidence = proposal.get("confidence", 0.3)
            record.verdict_direction = direction
            record.verdict_confidence = confidence
            return {
                "direction": direction,
                "confidence": confidence,
                "status": "proposer_fallback",
                "record": record
            }

        # 8. 陪审团投票
        votes: Dict[str, float] = {}
        for agent in jury:
            vote_conf = self._evaluate_proposal(agent, proposal, challenge, perception)
            agent_name = self._agent_name(agent)
            votes[agent_name] = vote_conf

        record.jury_votes = votes

        # 9. 反共识增强
        if self._is_anti_consensus(jury, proposal):
            record.anti_consensus_triggered = True
            votes = {k: v * self.anti_consensus_multiplier for k, v in votes.items()}
            logger.info("反共识增强触发：存在主义与进化论罕见一致")

        # 10. 计算裁决
        total = sum(votes.values())
        if abs(total) < 0.01:
            direction = 0
            confidence = 0.0
        else:
            direction = 1 if total > 0 else -1
            confidence = min(1.0, abs(total) / (len(votes) * 0.5))

        # 11. 应用哥德尔怀疑指数修正
        godel_doubt = self._get_godel_doubt()
        if godel_doubt > self.godel_doubt_threshold:
            confidence *= (1.0 - godel_doubt * 0.5)
            record.godel_doubt_applied = godel_doubt
            logger.info(f"哥德尔怀疑指数 {godel_doubt:.3f}，置信度修正为 {confidence:.3f}")

        record.verdict_direction = direction
        record.verdict_confidence = confidence
        self.records.append(record)

        self._log_event("PARLIAMENT_VERDICT", record)
        return {
            "direction": direction,
            "confidence": confidence,
            "status": "deliberated",
            "record": record
        }

    # ==================== 内部方法 ====================
    def _select_adversary(self, proposer: WorldViewAgent,
                          candidates: List[WorldViewAgent]) -> WorldViewAgent:
        """
        选择最对立的挑战者。
        优先选择 manifesto 中声明的 adversary_worldview。
        其次选择奖励函数方向相反的智能体。
        """
        adv_worldview = proposer.manifesto.adversary_worldview
        for agent in candidates:
            if agent.manifesto.worldview == adv_worldview:
                return agent

        # 备选：选择奖励信号方向最相反者（需各智能体预计算偏好符号）
        proposer_sign = self._get_preference_sign(proposer)
        best_agent = None
        best_opposition = -1.0
        for agent in candidates:
            agent_sign = self._get_preference_sign(agent)
            opposition = -proposer_sign * agent_sign  # 符号越相反，值越负
            # 我们想要最负的
            if opposition < best_opposition:
                best_opposition = opposition
                best_agent = agent

        return best_agent if best_agent else random.choice(candidates)

    def _evaluate_proposal(self, agent: WorldViewAgent,
                           proposal: Dict[str, Any],
                           challenge: Dict[str, Any],
                           perception: Optional[Dict] = None) -> float:
        """
        让一个陪审员评估提案。
        返回 -1.0 到 1.0 的评分，表示反对/支持程度。
        """
        # 尝试获取该智能体自身对该情形的判断
        own_proposal = agent.propose(perception) if hasattr(agent, 'propose') else {}
        own_dir = own_proposal.get("direction", 0)
        prop_dir = proposal.get("direction", 0)

        # 基础支持度基于方向一致性
        if own_dir == prop_dir and own_dir != 0:
            base = 0.4 + own_proposal.get("confidence", 0.3) * 0.4
        elif own_dir == 0:
            base = 0.1  # 中性
        else:
            base = -0.3 - own_proposal.get("confidence", 0.3) * 0.3

        # 引入世界观权重扰动（确保多样性）
        base += random.uniform(-0.05, 0.05)

        # 考虑挑战内容对评估的影响
        if challenge.get("severity", "low") == "high":
            base *= 0.8

        return max(-1.0, min(1.0, base))

    def _is_anti_consensus(self, jury: List[WorldViewAgent],
                           proposal: Dict[str, Any]) -> bool:
        """
        检测反共识：存在主义（守护者）与进化论（炼金术士）同时在场，
        且两者支持的方向相同且非零。
        """
        existentialist = None
        evolutionist = None
        for agent in jury:
            wv = agent.manifesto.worldview
            if wv == WorldView.EXISTENTIALISM:
                existentialist = agent
            elif wv == WorldView.EVOLUTIONISM:
                evolutionist = agent

        if existentialist is None or evolutionist is None:
            return False

        eval_exist = self._evaluate_proposal(existentialist, proposal, {}, None)
        eval_evol = self._evaluate_proposal(evolutionist, proposal, {}, None)

        # 两者方向一致且非零，视为罕见共识
        return (eval_exist * eval_evol > 0 and
                abs(eval_exist) > 0.2 and abs(eval_evol) > 0.2)

    def _get_preference_sign(self, agent: WorldViewAgent) -> float:
        """获取智能体的奖励偏好符号（+1 偏好多头，-1 偏好空头，0 中性）"""
        worldview = agent.manifesto.worldview
        # 守护者永远悲观 → -1, 炼金术士希望盈利 → +1, 魔鬼代言人→ 0
        sign_map = {
            WorldView.EXISTENTIALISM: -1,
            WorldView.EVOLUTIONISM: 1,
            WorldView.SKEPTICISM: 0,
            WorldView.INCOMPLETENESS: -0.5,
            WorldView.PHYSICALISM: 0,
            WorldView.OCCAMS_RAZOR: 0,
            WorldView.BAYESIANISM: 0,
            WorldView.HERMENEUTICS: 0,
            WorldView.PLURALISM: 0,
            WorldView.HISTORICISM: 0,
            WorldView.HOLISM: 0,
            WorldView.MECHANICAL_MATERIALISM: 0,
        }
        return sign_map.get(worldview, 0.0)

    def _apply_cooling(self, agent: WorldViewAgent) -> None:
        """对智能体施加冷却期"""
        setattr(agent, 'cooling_off_until', time.time() + self.cooling_period_sec)
        logger.info(f"智能体 {self._agent_name(agent)} 进入冷却 {self.cooling_period_sec}秒")

    def _get_godel_doubt(self) -> float:
        """获取哥德尔监视者的当前怀疑指数"""
        if self.godel_watcher and hasattr(self.godel_watcher, '_last_doubt'):
            return float(self.godel_watcher._last_doubt)
        return 0.0

    def _agent_name(self, agent: WorldViewAgent) -> str:
        """反向查找智能体的注册名称"""
        for name, a in self.agents.items():
            if a is agent:
                return name
        return str(id(agent))

    def _log_event(self, event_type: str, record: ParliamentRecord) -> None:
        """将议会事件写入行为日志"""
        if self.behavior_log is None:
            return
        try:
            summary = (
                f"第{record.round_number}轮: {record.proposer}({record.proposer_worldview}) 提案 "
                f"方向={record.proposal.get('direction')}, "
                f"挑战者={record.challenger}({record.challenger_worldview}), "
                f"否决={record.vetoed}, 裁决方向={record.verdict_direction}"
            )
            self.behavior_log.info(
                event_type="Parliament",
                source="AdversarialCouncil",
                content=summary,
                snapshot=record.__dict__
            )
        except Exception as e:
            logger.warning(f"记录议会日志失败: {e}")

    # ==================== 查询接口 ====================
    def get_status(self) -> Dict[str, Any]:
        """返回议会当前状态"""
        return {
            "total_agents": len(self.agents),
            "available_agents": sum(
                1 for a in self.agents.values()
                if time.time() >= getattr(a, 'cooling_off_until', 0.0)
            ),
            "total_rounds": self.round_count,
            "recent_records": [
                {
                    "round": r.round_number,
                    "proposer": r.proposer,
                    "challenger": r.challenger,
                    "vetoed": r.vetoed,
                    "verdict": r.verdict_direction,
                }
                for r in self.records[-5:]
            ],
        }

    def get_last_verdict(self) -> Optional[ParliamentRecord]:
        """获取最近一次裁决记录"""
        return self.records[-1] if self.records else None

    def clear_cooling(self) -> None:
        """清除所有智能体的冷却期（用于紧急情况）"""
        for agent in self.agents.values():
            setattr(agent, 'cooling_off_until', 0.0)
        logger.warning("已清除所有智能体的冷却期")
