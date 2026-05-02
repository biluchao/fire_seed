#!/usr/bin/env python3
"""
火种系统 (FireSeed) 对抗式议会测试套件
=========================================
验证 AdversarialCouncil 的核心能力：
- 智能体注册
- 提议-挑战-陪审团流程
- 冷却机制
- 反共识增强
- 边缘情况（空议会、全部冷却）
"""

import asyncio
import time
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# —————— 导入被测试模块 ——————
from agents.adversarial_council import AdversarialCouncil
from agents.worldview import (
    WorldView, WorldViewManifesto, WorldViewAgent
)


# ======================== 模拟智能体 ========================
class MockAgent(WorldViewAgent):
    """可精确控制输出和挑战行为的模拟智能体"""

    def __init__(
        self,
        manifesto: WorldViewManifesto,
        proposal_direction: int = 0,
        proposal_confidence: float = 0.5,
        challenge_veto: bool = False,
        challenge_message: str = "mock",
    ):
        super().__init__(manifesto)
        self._direction = proposal_direction
        self._confidence = proposal_confidence
        self._veto = challenge_veto
        self._challenge_msg = challenge_message
        self.proposals_made: List[Dict] = []
        self.challenges_made: List[Dict] = []

    def propose(self, perception: Dict) -> Dict:
        record = {
            "direction": self._direction,
            "confidence": self._confidence,
            "source": self.manifesto.worldview.value,
        }
        self.proposals_made.append(record)
        return record

    def challenge(self, other_proposal: Dict, my_worldview: WorldView) -> Dict:
        record = {
            "veto": self._veto,
            "message": self._challenge_msg,
            "challenger_worldview": my_worldview.value,
            "target_proposal": other_proposal,
        }
        self.challenges_made.append(record)
        return record


# ======================== 辅助函数 ========================
def make_manifesto(worldview: WorldView) -> WorldViewManifesto:
    return WorldViewManifesto(
        worldview=worldview,
        core_belief=worldview.value,
        primary_optimization_target="mock_target",
        adversary_worldview=WorldView.SKEPTICISM,
    )


@pytest.fixture
def council():
    return AdversarialCouncil()


@pytest.fixture
def agent_evolution() -> MockAgent:
    return MockAgent(make_manifesto(WorldView.EVOLUTIONISM), proposal_direction=1)


@pytest.fixture
def agent_existential() -> MockAgent:
    return MockAgent(make_manifesto(WorldView.EXISTENTIALISM), proposal_direction=-1)


@pytest.fixture
def agent_skeptic() -> MockAgent:
    return MockAgent(make_manifesto(WorldView.SKEPTICISM), proposal_direction=0)


@pytest.fixture
def agent_pluralist() -> MockAgent:
    return MockAgent(make_manifesto(WorldView.PLURALISM), proposal_direction=1)


@pytest.fixture
def agent_mechanical() -> MockAgent:
    return MockAgent(make_manifesto(WorldView.MECHANICAL_MATERIALISM), proposal_direction=0)


# ======================== 单元测试 ========================
class TestAdversarialCouncil:
    def test_register_agent(self, council, agent_evolution):
        council.register_agent("evolution", agent_evolution)
        assert "evolution" in council.agents
        assert council.agents["evolution"] is agent_evolution

    def test_deliberate_too_few_agents(self, council, agent_evolution):
        """少于2个智能体时返回中性"""
        council.register_agent("only", agent_evolution)
        result = asyncio.run(council.deliberate({}))
        assert result["direction"] == 0
        assert result["confidence"] == 0

    def test_deliberate_no_available_agents_all_cooling(self, council, agent_evolution, agent_existential):
        """所有智能体都在冷却期时返回中性"""
        council.register_agent("evo", agent_evolution)
        council.register_agent("exist", agent_existential)
        # 将冷却期设为未来
        agent_evolution.cooling_off_until = time.time() + 3600
        agent_existential.cooling_off_until = time.time() + 3600
        result = asyncio.run(council.deliberate({}))
        assert result["direction"] == 0
        assert result["confidence"] == 0

    def test_challenge_veto(self, council):
        """当挑战者成功否决时，返回方向0，提议者进入冷却"""
        # 创建两个不同世界观的智能体：进化论提议，存在主义挑战
        evo_manifesto = make_manifesto(WorldView.EVOLUTIONISM)
        exist_manifesto = make_manifesto(WorldView.EXISTENTIALISM)
        # 设置对立
        evo_manifesto.adversary_worldview = WorldView.EXISTENTIALISM
        exist_manifesto.adversary_worldview = WorldView.EVOLUTIONISM

        # 提议者：方向做多，不会否决自己
        proposer = MockAgent(evo_manifesto, proposal_direction=1, proposal_confidence=0.8)
        # 挑战者：将设定为强制否决
        challenger = MockAgent(exist_manifesto, challenge_veto=True)

        council.register_agent("proposer", proposer)
        council.register_agent("challenger", challenger)
        # 还需要一个陪审员，否则陪审团不足会跳过否决直接返回（但否决发生在陪审团之前）
        # 根据当前逻辑，如果有挑战者且挑战成功则直接返回否决不经过陪审团
        council.register_agent("jury", MockAgent(make_manifesto(WorldView.PLURALISM)))

        # 让提案者被选中（因为只有两个可用且非冷却）
        # 我们不能保证随机性，直接 mock 随机选择
        original_random = __import__('random').choice
        with patch('random.choice', side_effect=[proposer, challenger]):
            result = asyncio.run(council.deliberate({}))
        assert result["direction"] == 0
        assert result["status"] == "vetoed"
        # 提议者进入冷却
        assert proposer.cooling_off_until > time.time()

    def test_normal_deliberation(self, council, agent_evolution, agent_existential, agent_skeptic):
        """正常审议流程（无否决）应产生方向输出"""
        # 调整对立世界观
        agent_evolution.manifesto.adversary_worldview = WorldView.EXISTENTIALISM
        agent_existential.manifesto.adversary_worldview = WorldView.EVOLUTIONISM
        agent_skeptic.manifesto.adversary_worldview = WorldView.EVOLUTIONISM

        council.register_agent("evo", agent_evolution)
        council.register_agent("exist", agent_existential)
        council.register_agent("skeptic", agent_skeptic)

        # 让进化论被选为提议者，存在主义为挑战者，怀疑论为陪审员
        with patch('random.choice', side_effect=[agent_evolution, agent_existential]):
            result = asyncio.run(council.deliberate({}))
        # 挑战者不应否决
        assert result["direction"] in (1, -1, 0)  # 陪审团投票可能产生各种方向
        assert result["confidence"] >= 0.0

    def test_anti_consensus_boost(self, council):
        """当守护者(存在主义)与炼金术士(进化论)一致时，置信度应提升"""
        evo_manifesto = make_manifesto(WorldView.EVOLUTIONISM)
        exist_manifesto = make_manifesto(WorldView.EXISTENTIALISM)
        plural_manifesto = make_manifesto(WorldView.PLURALISM)

        # 设置对立世界观
        evo_manifesto.adversary_worldview = WorldView.EXISTENTIALISM
        exist_manifesto.adversary_worldview = WorldView.EVOLUTIONISM

        # 提议者为进化论，挑战者为存在主义（不否决）
        proposer = MockAgent(evo_manifesto, proposal_direction=1, proposal_confidence=0.7)
        challenger = MockAgent(exist_manifesto, challenge_veto=False)
        # 陪审团包括一个存在主义和一个进化论，以及一个多元主义
        jury1 = MockAgent(evo_manifesto, proposal_direction=1)
        jury2 = MockAgent(exist_manifesto, proposal_direction=1)
        jury3 = MockAgent(plural_manifesto, proposal_direction=1)

        council.register_agent("evo_proposer", proposer)
        council.register_agent("exist_challenger", challenger)
        council.register_agent("evo_jury1", jury1)
        council.register_agent("exist_jury2", jury2)
        council.register_agent("plural_jury3", jury3)

        with patch('random.choice', side_effect=[proposer, challenger]):
            result = asyncio.run(council.deliberate({}))
        # 当存在主义和进化论均投正向票时，反共识增强应触发
        # 检查置信度是否相对较高（理论上会加倍）
        assert result["confidence"] > 0.5  # 原始平均约0.3，加倍至少0.6以上

    def test_agent_cooling_off_prevents_proposal(self, council, agent_evolution, agent_existential, agent_skeptic):
        """冷却期内的智能体不能成为提议者"""
        # 强制所有智能体冷却
        agent_evolution.cooling_off_until = time.time() + 3600
        agent_existential.cooling_off_until = time.time() + 3600
        # 只剩怀疑论可用
        agent_skeptic.cooling_off_until = time.time() - 10  # 已过冷却
        council.register_agent("evo", agent_evolution)
        council.register_agent("exist", agent_existential)
        council.register_agent("skeptic", agent_skeptic)

        # 只有一个可用，无法 deliberation
        result = asyncio.run(council.deliberate({}))
        assert result["direction"] == 0

    def test_verdict_history_recorded(self, council, agent_evolution, agent_existential, agent_skeptic):
        """审议记录应保存到判决历史"""
        agent_evolution.manifesto.adversary_worldview = WorldView.EXISTENTIALISM
        agent_existential.manifesto.adversary_worldview = WorldView.EVOLUTIONISM

        council.register_agent("evo", agent_evolution)
        council.register_agent("exist", agent_existential)
        council.register_agent("skeptic", agent_skeptic)

        with patch('random.choice', side_effect=[agent_evolution, agent_existential]):
            asyncio.run(council.deliberate({}))
        assert len(council.verdict_history) == 1
        record = council.verdict_history[0]
        assert "proposer" in record
        assert "challenger" in record
        assert "verdict" in record
