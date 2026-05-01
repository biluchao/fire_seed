#!/usr/bin/env python3
"""
火种系统 (FireSeed) 智能体议会协调者 (Council)
==================================================
负责：
- 协调12个智能体的信号输入
- 动态投票权重计算（基于近期表现）
- 加权融合决策，输出最终交易信号
- 记录议会投票历史
- 管理议会日程（轮岗、影子对决）
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from api.server import get_engine
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier

logger = logging.getLogger("fire_seed.council")


@dataclass
class AgentVote:
    """单个智能体的投票"""
    agent_id: str
    direction: int          # 1=多, -1=空, 0=中性
    confidence: float       # 0~1
    timestamp: float = field(default_factory=time.time)


@dataclass
class CouncilDecision:
    """议会最终决策"""
    direction: int
    confidence: float
    score: float            # 综合评分 0-100
    votes: List[AgentVote]
    weights: Dict[str, float]
    timestamp: datetime = field(default_factory=datetime.now)
    description: str = ""


class AgentCouncil:
    """
    智能体议会协调者。
    维护所有智能体的表现追踪，根据近期正确率动态调整投票权重。
    """

    def __init__(self,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None):
        """
        :param behavior_log: 系统行为日志
        :param notifier:     消息推送器
        """
        self.behavior_log = behavior_log
        self.notifier = notifier

        # 智能体注册表（可从配置文件加载）
        self.agents = {
            "sentinel": {"weight": 0.08, "performance": deque(maxlen=100)},
            "alchemist": {"weight": 0.08, "performance": deque(maxlen=100)},
            "guardian": {"weight": 0.10, "performance": deque(maxlen=100)},
            "devils_advocate": {"weight": 0.10, "performance": deque(maxlen=100)},
            "godel_watcher": {"weight": 0.05, "performance": deque(maxlen=100)},
            "env_inspector": {"weight": 0.05, "performance": deque(maxlen=100)},
            "redundancy_auditor": {"weight": 0.04, "performance": deque(maxlen=100)},
            "weight_calibrator": {"weight": 0.08, "performance": deque(maxlen=100)},
            "narrator": {"weight": 0.02, "performance": deque(maxlen=100)},
            "diversity_enforcer": {"weight": 0.06, "performance": deque(maxlen=100)},
            # 预留两个额外席位
            "reserve_1": {"weight": 0.02, "performance": deque(maxlen=100)},
            "reserve_2": {"weight": 0.02, "performance": deque(maxlen=100)},
        }

        # 单智能体最大权重上限
        self.max_single_weight = 0.35
        # 最低投票参与率
        self.min_participation = 0.6
        # 共识过载阈值（一致性超此值触发多样性预警）
        self.consensus_overload_threshold = 0.85

        # 投票历史
        self._vote_history: List[CouncilDecision] = []
        # 最近一次决策
        self._last_decision: Optional[CouncilDecision] = None

        # 权重更新步长
        self.weight_update_step = 0.01

    # ======================== 核心投票 ========================
    async def deliberate(self, context: Any = None) -> CouncilDecision:
        """
        议会审议：收集所有活跃智能体的意见，加权融合后输出决策。
        """
        # 收集各智能体的投票（实际应从各 Agent 实例获取，此处模拟）
        votes = await self._collect_votes(context)

        if not votes:
            # 无智能体投票，返回中性
            return CouncilDecision(
                direction=0, confidence=0.0, score=50.0,
                votes=[], weights={}
            )

        # 计算加权决策
        weights = self._get_current_weights()
        direction, confidence, score = self._weighted_fusion(votes, weights)

        decision = CouncilDecision(
            direction=direction,
            confidence=confidence,
            score=score,
            votes=votes,
            weights=weights,
            description=f"议会审议: {len(votes)} 投票, 方向={'多' if direction==1 else '空' if direction==-1 else '中性'}"
        )

        self._last_decision = decision
        self._vote_history.append(decision)
        if len(self._vote_history) > 200:
            self._vote_history = self._vote_history[-200:]

        # 检查共识过载
        self._check_consensus_overload(votes)

        # 记录行为日志
        if self.behavior_log:
            self.behavior_log.log(
                EventType.AGENT, "Council",
                f"议会决策: {decision.description}, 置信度={confidence:.2f}, 评分={score:.1f}",
                snapshot={"weights": {k: round(v, 3) for k, v in weights.items()}}
            )

        return decision

    async def _collect_votes(self, context: Any = None) -> List[AgentVote]:
        """
        从所有活跃智能体收集投票。
        实际实现应从各 Agent 实例获取最新信号。
        此处生成模拟投票用于框架演示。
        """
        votes = []
        for agent_id in self.agents:
            # 模拟：各智能体根据自己的感知生成方向
            direction = np.random.choice([1, -1, 0], p=[0.4, 0.35, 0.25])
            confidence = abs(direction) * (0.5 + 0.5 * np.random.random())
            votes.append(AgentVote(
                agent_id=agent_id,
                direction=direction,
                confidence=confidence
            ))
        return votes

    def _weighted_fusion(self, votes: List[AgentVote],
                         weights: Dict[str, float]) -> Tuple[int, float, float]:
        """
        加权融合投票：
        - 计算加权多空力量
        - 综合评分映射到 0-100
        """
        weighted_long = 0.0
        weighted_short = 0.0
        total_weight = sum(weights.get(v.agent_id, 0.02) for v in votes)

        if total_weight == 0:
            return 0, 0.0, 50.0

        for vote in votes:
            w = weights.get(vote.agent_id, 0.02) / total_weight
            if vote.direction == 1:
                weighted_long += w * vote.confidence
            elif vote.direction == -1:
                weighted_short += w * vote.confidence

        net = weighted_long - weighted_short

        # 方向
        if net > 0.15:
            direction = 1
        elif net < -0.15:
            direction = -1
        else:
            direction = 0

        # 置信度（归一化）和评分映射
        confidence = min(1.0, abs(net) * 2.0)
        score = 50.0 + net * 50.0  # 映射到 0-100

        return direction, confidence, max(0.0, min(100.0, score))

    # ======================== 权重管理 ========================
    def _get_current_weights(self) -> Dict[str, float]:
        """获取当前所有智能体的投票权重"""
        return {aid: min(self.max_single_weight, info["weight"])
                for aid, info in self.agents.items()}

    def update_weight(self, agent_id: str, performance_score: float) -> None:
        """
        根据近期表现调整智能体权重。
        performance_score: 0~1，越大越好。
        """
        if agent_id not in self.agents:
            return
        info = self.agents[agent_id]
        info["performance"].append(performance_score)
        # 取近期平均值
        recent = list(info["performance"])[-20:]
        if recent:
            avg_perf = np.mean(recent)
            # 表现越好权重越高，但不超过上限
            new_weight = min(self.max_single_weight, 0.02 + avg_perf * 0.15)
            info["weight"] = new_weight

    def _normalize_weights(self) -> None:
        """归一化所有权重，使其和为 1.0"""
        total = sum(info["weight"] for info in self.agents.values())
        if total > 0:
            for info in self.agents.values():
                info["weight"] /= total

    # ======================== 共识监控 ========================
    def _check_consensus_overload(self, votes: List[AgentVote]) -> None:
        """检查议会共识是否过高（群体极化风险）"""
        dirs = [v.direction for v in votes if v.direction != 0]
        if not dirs:
            return
        same = max(sum(1 for d in dirs if d == 1), sum(1 for d in dirs if d == -1))
        agreement = same / len(dirs)
        if agreement > self.consensus_overload_threshold:
            logger.warning(f"议会共识过载: {agreement*100:.0f}%，存在群体极化风险")
            if self.behavior_log:
                self.behavior_log.warn(
                    EventType.AGENT, "Council",
                    f"共识过载: {agreement*100:.0f}%"
                )

    # ======================== 查询与接口 ========================
    def get_last_decision(self) -> Optional[CouncilDecision]:
        """获取最近一次决策"""
        return self._last_decision

    def get_abstain_rate(self) -> float:
        """计算近期投票弃权率"""
        if not self._vote_history:
            return 0.0
        recent = self._vote_history[-10:]
        abstains = sum(1 for d in recent if d.direction == 0)
        return abstains / len(recent)

    def get_disagreement(self) -> float:
        """计算议会分歧度 (0-1)"""
        if not self._vote_history:
            return 0.0
        recent = self._vote_history[-10:]
        dirs = [d.direction for d in recent]
        if not dirs:
            return 0.0
        ones = sum(1 for d in dirs if d == 1)
        minus = sum(1 for d in dirs if d == -1)
        total = len(dirs)
        return 1.0 - min(ones, minus) / total if total > 0 else 1.0

    def get_status(self) -> Dict[str, Any]:
        """返回议会状态摘要"""
        return {
            "agents_count": len(self.agents),
            "last_decision": self._last_decision.description if self._last_decision else "无",
            "abstain_rate": round(self.get_abstain_rate(), 3),
            "disagreement": round(self.get_disagreement(), 3),
            "weights": {aid: round(info["weight"], 3) for aid, info in self.agents.items()},
        }

    async def generate_daily_report(self) -> str:
        """生成议会日报（叙事官辅助）"""
        status = self.get_status()
        last = self.get_last_decision()
        report = f"### 火种议会日报\n\n"
        report += f"- 投票智能体: {status['agents_count']}\n"
        report += f"- 最近决策: {last.description if last else '无'}\n"
        report += f"- 弃权率: {status['abstain_rate']*100:.1f}%\n"
        report += f"- 分歧度: {status['disagreement']*100:.1f}%\n"
        return report

    # ======================== 议会投票（特殊议题） ========================
    async def vote_for_ota(self) -> bool:
        """就OTA更新进行投票"""
        # 简化：权重求和，超过半数通过
        weights = self._get_current_weights()
        # 假设所有智能体对OTA投赞成/反对票，这里随机模拟
        votes = {aid: np.random.choice([True, False]) for aid in weights}
        total = sum(weights.values())
        approved = sum(w for aid, w in weights.items() if votes.get(aid, False))
        return approved / total > 0.6

    async def vote_for_action(self, action: str) -> bool:
        """就指定行动投票（重启、模式切换等）"""
        weights = self._get_current_weights()
        # 模拟2/3多数通过
        votes = {aid: np.random.choice([True, False], p=[0.7, 0.3]) for aid in weights}
        total = sum(weights.values())
        approved = sum(w for aid, w in weights.items() if votes.get(aid, False))
        return approved / total > 0.66
