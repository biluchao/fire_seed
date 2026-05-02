#!/usr/bin/env python3
"""
火种系统 (FireSeed) 世界观体系单元测试
=========================================
测试覆盖：
- 世界观枚举完整性
- WorldViewManifesto 结构与默认值
- WorldViewAgent 基类行为
- 数据源注册表 (DataSourceRegistry) 隔离规则
- 极端奖励函数 (ExtremeRewardFunctions) 定义完整性
- 敌对世界观映射
"""

import asyncio
import time
from typing import Any, Dict, List, Optional, Set
from unittest.mock import MagicMock, patch

import pytest

from agents.worldview import (
    WorldView,
    WorldViewManifesto,
    WorldViewAgent,
    DataSourceRegistry,
)
from agents.extreme_rewards import ExtremeRewardFunctions


# ======================== Fixtures ========================
@pytest.fixture
def manifesto_trend() -> WorldViewManifesto:
    return WorldViewManifesto(
        worldview=WorldView.EVOLUTIONISM,
        core_belief="市场策略是不断适应环境的生命体",
        primary_optimization_target="sharpe × novelty",
        adversary_worldview=WorldView.EXISTENTIALISM,
        forbidden_data_source={"SYSTEM_METRICS", "REAL_TIME_PNL"},
        exclusive_data_source={"ACADEMIC_PAPERS", "FACTOR_EXPRESSIONS"},
        time_scale="1d",
    )


@pytest.fixture
def agent_trend(manifesto_trend) -> WorldViewAgent:
    class TrendAgent(WorldViewAgent):
        def propose(self, perception: Dict) -> Dict:
            return {"direction": 1, "confidence": 0.8, "worldview": self.manifesto.worldview.value}

        def challenge(self, other_proposal: Dict, my_worldview: WorldView) -> Dict:
            return {"veto": False, "reason": "不构成反对"}

    return TrendAgent(manifesto=manifesto_trend)


@pytest.fixture
def manifesto_guard() -> WorldViewManifesto:
    return WorldViewManifesto(
        worldview=WorldView.EXISTENTIALISM,
        core_belief="市场的本质是无常，唯一能确定的是风险",
        primary_optimization_target="-max_drawdown",
        adversary_worldview=WorldView.EVOLUTIONISM,
        forbidden_data_source={"ORDERBOOK", "SENTIMENT"},
        exclusive_data_source={"RETURNS_SERIES", "CVAR"},
        time_scale="5m",
    )


@pytest.fixture
def agent_guard(manifesto_guard) -> WorldViewAgent:
    class GuardAgent(WorldViewAgent):
        def propose(self, perception: Dict) -> Dict:
            return {"direction": -1, "confidence": 0.9, "worldview": self.manifesto.worldview.value}

        def challenge(self, other_proposal: Dict, my_worldview: WorldView) -> Dict:
            return {"veto": True, "reason": "极度危险"}

    return GuardAgent(manifesto=manifesto_guard)


# ======================== 世界观枚举测试 ========================
class TestWorldViewEnum:
    def test_enum_values(self):
        """确保12个世界观全部存在"""
        expected = {
            WorldView.MECHANICAL_MATERIALISM,
            WorldView.EVOLUTIONISM,
            WorldView.EXISTENTIALISM,
            WorldView.SKEPTICISM,
            WorldView.INCOMPLETENESS,
            WorldView.PHYSICALISM,
            WorldView.OCCAMS_RAZOR,
            WorldView.BAYESIANISM,
            WorldView.HERMENEUTICS,
            WorldView.PLURALISM,
            WorldView.HISTORICISM,
            WorldView.HOLISM,
        }
        assert set(WorldView) == expected

    def test_enum_names(self):
        """检查中文标签"""
        assert WorldView.MECHANICAL_MATERIALISM.value == "机械唯物主义"
        assert WorldView.EVOLUTIONISM.value == "进化论"
        assert WorldView.EXISTENTIALISM.value == "存在主义"
        assert WorldView.SKEPTICISM.value == "怀疑论"
        assert WorldView.INCOMPLETENESS.value == "不完备定理"
        assert WorldView.PHYSICALISM.value == "物理主义"
        assert WorldView.OCCAMS_RAZOR.value == "奥卡姆剃刀"
        assert WorldView.BAYESIANISM.value == "贝叶斯主义"
        assert WorldView.HERMENEUTICS.value == "诠释学"
        assert WorldView.PLURALISM.value == "多元主义"
        assert WorldView.HISTORICISM.value == "历史主义"
        assert WorldView.HOLISM.value == "整体论"


# ======================== WorldViewManifesto 测试 ========================
class TestWorldViewManifesto:
    def test_create_manifesto(self, manifesto_trend):
        assert manifesto_trend.worldview == WorldView.EVOLUTIONISM
        assert manifesto_trend.adversary_worldview == WorldView.EXISTENTIALISM
        assert manifesto_trend.time_scale == "1d"
        assert "SYSTEM_METRICS" in manifesto_trend.forbidden_data_source
        assert "ACADEMIC_PAPERS" in manifesto_trend.exclusive_data_source

    def test_default_forbidden_empty(self):
        manifesto = WorldViewManifesto(
            worldview=WorldView.HOLISM,
            core_belief="整体",
            primary_optimization_target="sync",
            adversary_worldview=WorldView.MECHANICAL_MATERIALISM,
        )
        assert manifesto.forbidden_data_source == set()
        assert manifesto.exclusive_data_source == set()
        assert manifesto.time_scale == "1m"


# ======================== WorldViewAgent 测试 ========================
class TestWorldViewAgent:
    def test_abstract_propose(self, manifesto_trend):
        """未实现 propose 时应抛出异常"""
        class IncompleteAgent(WorldViewAgent):
            def challenge(self, other, my_wv): return {"veto": False}

        agent = IncompleteAgent(manifesto=manifesto_trend)
        with pytest.raises(NotImplementedError):
            agent.propose({})

    def test_abstract_challenge(self, manifesto_trend):
        """未实现 challenge 时应抛出异常"""
        class IncompleteAgent(WorldViewAgent):
            def propose(self, p): return {}

        agent = IncompleteAgent(manifesto=manifesto_trend)
        with pytest.raises(NotImplementedError):
            agent.challenge({}, WorldView.SKEPTICISM)

    def test_cooling_off(self, agent_trend):
        """冷却机制：成功挑战后进入冷却"""
        assert agent_trend.cooling_off_until == 0.0
        # 模拟被否决
        agent_trend.cooling_off_until = time.time() + 1800
        assert agent_trend.cooling_off_until > time.time()

    def test_challenge_history(self, agent_trend):
        """挑战历史记录"""
        agent_trend.challenge_history.append({"round": 1, "result": "lost"})
        assert len(agent_trend.challenge_history) == 1

    def test_recent_proposals(self, agent_trend):
        agent_trend.recent_proposals.append({"direction": 1})
        assert len(agent_trend.recent_proposals) == 1


# ======================== DataSourceRegistry 测试 ========================
class TestDataSourceRegistry:
    def test_forbidden_mapping_completeness(self):
        """每个世界观都有禁止数据源映射"""
        for wv in WorldView:
            assert wv in DataSourceRegistry.FORBIDDEN_MAP

    def test_time_scale_mapping(self):
        for wv in WorldView:
            assert wv in DataSourceRegistry.TIME_SCALES

    def test_is_data_allowed(self):
        # 机械唯物主义禁止 KLINE
        assert not DataSourceRegistry.is_data_allowed(
            WorldView.MECHANICAL_MATERIALISM, "KLINE"
        )
        # 但允许 SYSTEM_METRICS
        assert DataSourceRegistry.is_data_allowed(
            WorldView.MECHANICAL_MATERIALISM, "SYSTEM_METRICS"
        )
        # 进化论不允许 SYSTEM_METRICS
        assert not DataSourceRegistry.is_data_allowed(
            WorldView.EVOLUTIONISM, "SYSTEM_METRICS"
        )

    def test_unknown_data_type_allowed(self):
        """未知数据类型默认允许"""
        assert DataSourceRegistry.is_data_allowed(
            WorldView.BAYESIANISM, "UNKNOWN_TYPE"
        )


# ======================== 极端奖励函数测试 ========================
class TestExtremeRewardFunctions:
    def test_all_worldviews_have_reward(self):
        """确保每个世界观都有极端奖励定义"""
        for wv in WorldView:
            assert wv in ExtremeRewardFunctions.REWARD_MAP

    def test_formula_structure(self):
        for wv, info in ExtremeRewardFunctions.REWARD_MAP.items():
            assert "formula" in info
            assert "extreme_behavior" in info
            assert isinstance(info["formula"], str)
            assert isinstance(info["extreme_behavior"], str)

    def test_extreme_behaviors_are_distinct(self):
        """极端行为描述不应同质化"""
        behaviors = set()
        for info in ExtremeRewardFunctions.REWARD_MAP.values():
            behaviors.add(info["extreme_behavior"])
        assert len(behaviors) == len(WorldView), "极端行为描述存在重复"


# ======================== 集成测试 ========================
class TestWorldViewIntegration:
    """世界观交互集成测试"""
    def test_conflicting_agents_propose_and_challenge(
        self, agent_trend, agent_guard
    ):
        """趋势（进化论）与守护（存在主义）的天然冲突"""
        perception = {"volatility": 0.02}
        trend_proposal = agent_trend.propose(perception)
        assert trend_proposal["direction"] == 1

        guard_challenge = agent_guard.challenge(
            trend_proposal, WorldView.EXISTENTIALISM
        )
        # 守护者极端保守，应该否决
        assert guard_challenge["veto"] is True

    def test_cooling_off_prevents_proposal(self, agent_trend):
        """冷却期过后才能重新提议"""
        agent_trend.cooling_off_until = time.time() - 1  # 已过期
        # 此刻应可提议
        can_propose = time.time() > agent_trend.cooling_off_until
        assert can_propose

        # 设置未来冷却
        agent_trend.cooling_off_until = time.time() + 100
        assert not (time.time() > agent_trend.cooling_off_until)
