#!/usr/bin/env python3
"""
火种系统 (FireSeed) 智能体议会总成
=====================================
统一导出所有智能体实例、世界观基础设施以及议会核心类。
外部代码可通过 `from agents import Council` 等方式直接获取。
"""

# ---------- 世界观基础设施 ----------
from agents.worldview import (
    WorldView,
    WorldViewManifesto,
    WorldViewAgent,
)

# ---------- 极端化奖励函数 ----------
from agents.extreme_rewards import ExtremeRewardFunctions

# ---------- 对抗式议会 ----------
from agents.adversarial_council import AdversarialCouncil

# ---------- 议会协调者 (已集成对抗式内核) ----------
from agents.council import AgentCouncil as Council

# ---------- 12 智能体 ----------
from agents.sentinel import SentinelAgent as Sentinel
from agents.alchemist import AlchemistAgent as Alchemist
from agents.guardian import GuardianAgent as Guardian
from agents.devils_advocate import DevilsAdvocate
from agents.godel_watcher import GodelWatcher
from agents.env_inspector import EnvInspector
from agents.redundancy_auditor import RedundancyAuditor
from agents.weight_calibrator import WeightCalibrator
from agents.narrator import NarratorAgent as Narrator
from agents.diversity_enforcer import DiversityEnforcer
from agents.archive_guardian import ArchiveGuardian
from agents.copy_trade_coordinator import CopyTradeCoordinator


__all__ = [
    # 世界观
    "WorldView",
    "WorldViewManifesto",
    "WorldViewAgent",
    # 奖励
    "ExtremeRewardFunctions",
    # 议会
    "AdversarialCouncil",
    "Council",
    # 智能体
    "Sentinel",
    "Alchemist",
    "Guardian",
    "DevilsAdvocate",
    "GodelWatcher",
    "EnvInspector",
    "RedundancyAuditor",
    "WeightCalibrator",
    "Narrator",
    "DiversityEnforcer",
    "ArchiveGuardian",
    "CopyTradeCoordinator",
]
