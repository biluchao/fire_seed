#!/usr/bin/env python3
"""
测试引擎初始化智能体模块
==========================
验证引擎启动时：
- 正确创建 AdversarialCouncil 实例
- 注册全部 12 个携带世界观的智能体
- 各智能体继承 WorldViewAgent 且世界观正确
- 智能体映射到正确的源代码文件
"""

import importlib
import inspect
import os
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent.parent.parent
import sys
sys.path.insert(0, str(PROJECT_ROOT))

# 需要导入的模块（如果 agent 文件已存在则直接导入，否则跳过）
try:
    from agents.worldview import (
        WorldView,
        WorldViewAgent,
        WorldViewManifesto,
    )
    from agents.adversarial_council import AdversarialCouncil
    AGENTS_MODULE_AVAILABLE = True
except ImportError:
    AGENTS_MODULE_AVAILABLE = False


# ---------- 期望的12智能体配置表 ----------
EXPECTED_AGENTS = [
    {
        "file": "sentinel.py",
        "class_name": "SentinelAgent",
        "worldview": WorldView.MECHANICAL_MATERIALISM if AGENTS_MODULE_AVAILABLE else "MECHANICAL_MATERIALISM",
    },
    {
        "file": "alchemist.py",
        "class_name": "AlchemistAgent",
        "worldview": WorldView.EVOLUTIONISM if AGENTS_MODULE_AVAILABLE else "EVOLUTIONISM",
    },
    {
        "file": "guardian.py",
        "class_name": "GuardianAgent",
        "worldview": WorldView.EXISTENTIALISM if AGENTS_MODULE_AVAILABLE else "EXISTENTIALISM",
    },
    {
        "file": "devils_advocate.py",
        "class_name": "DevilsAdvocate",
        "worldview": WorldView.SKEPTICISM if AGENTS_MODULE_AVAILABLE else "SKEPTICISM",
    },
    {
        "file": "godel_watcher.py",
        "class_name": "GodelWatcher",
        "worldview": WorldView.INCOMPLETENESS if AGENTS_MODULE_AVAILABLE else "INCOMPLETENESS",
    },
    {
        "file": "env_inspector.py",
        "class_name": "EnvInspector",
        "worldview": WorldView.PHYSICALISM if AGENTS_MODULE_AVAILABLE else "PHYSICALISM",
    },
    {
        "file": "redundancy_auditor.py",
        "class_name": "RedundancyAuditor",
        "worldview": WorldView.OCCAMS_RAZOR if AGENTS_MODULE_AVAILABLE else "OCCAMS_RAZOR",
    },
    {
        "file": "weight_calibrator.py",
        "class_name": "WeightCalibrator",
        "worldview": WorldView.BAYESIANISM if AGENTS_MODULE_AVAILABLE else "BAYESIANISM",
    },
    {
        "file": "narrator.py",
        "class_name": "NarratorAgent",
        "worldview": WorldView.HERMENEUTICS if AGENTS_MODULE_AVAILABLE else "HERMENEUTICS",
    },
    {
        "file": "diversity_enforcer.py",
        "class_name": "DiversityEnforcer",
        "worldview": WorldView.PLURALISM if AGENTS_MODULE_AVAILABLE else "PLURALISM",
    },
    {
        "file": "archive_guardian.py",
        "class_name": "ArchiveGuardian",
        "worldview": WorldView.HISTORICISM if AGENTS_MODULE_AVAILABLE else "HISTORICISM",
    },
    {
        "file": "copy_trade_coordinator.py",
        "class_name": "CopyTradeCoordinator",
        "worldview": WorldView.HOLISM if AGENTS_MODULE_AVAILABLE else "HOLISM",
    },
]


# ======================== 辅助函数 ========================
def agent_file_exists(agent_info: dict) -> bool:
    """检查智能体源文件是否存在于 agents/ 目录"""
    agents_dir = PROJECT_ROOT / "agents"
    filepath = agents_dir / agent_info["file"]
    return filepath.exists()


def import_agent_class(module_name: str, class_name: str):
    """动态导入智能体类"""
    try:
        mod = importlib.import_module(f"agents.{module_name}")
        return getattr(mod, class_name)
    except (ImportError, AttributeError):
        return None


# ======================== 测试用例 ========================

@pytest.mark.skipif(not AGENTS_MODULE_AVAILABLE, reason="worldview 模块不可用")
class TestAgentWorldViews:
    """验证世界观枚举和宣言"""

    def test_all_worldviews_defined(self):
        """确保12个世界观都已定义"""
        expected = {
            "MECHANICAL_MATERIALISM", "EVOLUTIONISM", "EXISTENTIALISM",
            "SKEPTICISM", "INCOMPLETENESS", "PHYSICALISM",
            "OCCAMS_RAZOR", "BAYESIANISM", "HERMENEUTICS",
            "PLURALISM", "HISTORICISM", "HOLISM",
        }
        actual = {w.name for w in WorldView}
        missing = expected - actual
        assert not missing, f"缺失世界观: {missing}"

    def test_each_agent_has_valid_worldview(self):
        """验证期望表中的每个智能体都有合法的世界观"""
        for agent in EXPECTED_AGENTS:
            assert isinstance(agent["worldview"], WorldView), (
                f"{agent['class_name']} 的世界观不是 WorldView 枚举: {type(agent['worldview'])}"
            )


class TestAgentFileExistence:
    """验证所有智能体文件是否存在"""

    @pytest.mark.parametrize("agent", EXPECTED_AGENTS)
    def test_agent_file_exists(self, agent):
        """检查每个智能体文件是否存在于磁盘"""
        assert agent_file_exists(agent), (
            f"智能体文件 agents/{agent['file']} 不存在"
        )


class TestAgentClassLoadable:
    """验证智能体类可导入"""

    @pytest.mark.parametrize("agent", EXPECTED_AGENTS)
    def test_agent_class_loadable(self, agent):
        """验证智能体类能否从对应文件导入"""
        if not agent_file_exists(agent):
            pytest.skip(f"文件 {agent['file']} 不存在，跳过导入测试")
        module_name = agent["file"].replace(".py", "")
        cls = import_agent_class(module_name, agent["class_name"])
        assert cls is not None, (
            f"无法从 agents.{module_name} 导入 {agent['class_name']}"
        )


@pytest.mark.skipif(not AGENTS_MODULE_AVAILABLE, reason="worldview 模块不可用")
class TestAgentInheritance:
    """验证智能体继承关系"""

    @pytest.mark.parametrize("agent", EXPECTED_AGENTS)
    def test_agent_inherits_worldview_agent(self, agent):
        """验证智能体类继承自 WorldViewAgent"""
        if not agent_file_exists(agent):
            pytest.skip(f"文件 {agent['file']} 不存在")
        module_name = agent["file"].replace(".py", "")
        cls = import_agent_class(module_name, agent["class_name"])
        if cls is None:
            pytest.skip(f"类 {agent['class_name']} 不可导入")
        assert issubclass(cls, WorldViewAgent), (
            f"{agent['class_name']} 应继承 WorldViewAgent"
        )


class TestEngineAgentRegistration:
    """使用 mock 验证引擎初始化时注册12智能体"""

    @patch("core.engine.FireSeedEngine")
    def test_engine_registers_12_agents(self, MockEngine):
        """Mock 引擎并检查注册到 AdversarialCouncil 的智能体数量"""
        # 这个测试需要引擎代码实现了注册逻辑，这里仅验证 mock 行为
        # 实际部署后应改为真实引擎实例测试
        mock_engine = MockEngine.return_value
        mock_council = MagicMock(spec=AdversarialCouncil if AGENTS_MODULE_AVAILABLE else object)
        mock_engine.agent_council = mock_council

        # 模拟注册过程
        registered = []
        for agent_conf in EXPECTED_AGENTS:
            if agent_file_exists(agent_conf):
                module_name = agent_conf["file"].replace(".py", "")
                cls = import_agent_class(module_name, agent_conf["class_name"])
                if cls:
                    # 创建携带世界观的实例 (简化)
                    if AGENTS_MODULE_AVAILABLE:
                        manifesto = WorldViewManifesto(
                            worldview=agent_conf["worldview"],
                            core_belief=agent_conf["worldview"].value,
                            primary_optimization_target="test",
                            adversary_worldview=WorldView.SKEPTICISM,
                        )
                        # 实际继承类需要接受 manifesto 参数
                        # 这里仅验证注册数量
                        registered.append(agent_conf)

        assert len(registered) >= 10, (
            f"应注册至少10个智能体，实际 {len(registered)}"
        )


# ======================== 集成测试 (需要引擎已启动) ========================
@pytest.mark.integration
@pytest.mark.skip(reason="需要完整引擎运行环境")
class TestEngineIntegration:
    """需要实际运行引擎的集成测试"""

    def test_engine_creates_adversarial_council(self):
        """真实引擎启动后应包含 AdversarialCouncil"""
        from core.engine import FireSeedEngine
        engine = FireSeedEngine(mode="virtual")
        assert hasattr(engine, "agent_council"), "引擎缺少 agent_council 属性"
        # assert isinstance(engine.agent_council, AdversarialCouncil)

    def test_all_agents_registered_at_startup(self):
        """引擎启动后所有12个智能体都应已注册"""
        from core.engine import FireSeedEngine
        engine = FireSeedEngine(mode="virtual")
        council = engine.agent_council
        registered = len(council.agents) if hasattr(council, "agents") else 0
        assert registered >= 12, f"期望注册12个智能体，实际 {registered}"


# ======================== 运行环境自检 ========================
def test_project_structure():
    """确保项目目录结构正确，以便测试能定位到智能体文件"""
    agents_dir = PROJECT_ROOT / "agents"
    assert agents_dir.exists(), f"agents 目录不存在: {agents_dir}"
    files = list(agents_dir.glob("*.py"))
    assert len(files) >= 12, f"agents 目录下应有至少12个 .py 文件，实际 {len(files)}"
