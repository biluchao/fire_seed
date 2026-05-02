#!/usr/bin/env python3
"""
火种系统 (FireSeed) 极端化奖励函数 单元测试
==============================================
验证 ExtremeRewardFunctions 的奖励映射完整性、公式合法性、
极端行为描述准确性，确保十二种世界观均被正确覆盖且互斥。
"""

import pytest
from agents.worldview import WorldView
from agents.extreme_rewards import ExtremeRewardFunctions


class TestExtremeRewardCompleteness:
    """测试奖励映射是否覆盖所有世界观且定义完整"""

    def test_all_worldviews_present(self):
        """
        确保 REWARD_MAP 中包含了全部 12 种世界观。
        缺失任何一种都可能导致智能体无法加载其奖励函数。
        """
        mapped = set(ExtremeRewardFunctions.REWARD_MAP.keys())
        expected = set(WorldView)
        missing = expected - mapped
        assert not missing, f"缺失世界观: {missing}"

    def test_no_extra_worldviews(self):
        """
        确保 REWARD_MAP 中不存在未定义的世界观。
        """
        mapped = set(ExtremeRewardFunctions.REWARD_MAP.keys())
        expected = set(WorldView)
        extra = mapped - expected
        assert not extra, f"多余世界观: {extra}"


class TestRewardDefinitionsIntegrity:
    """测试每个奖励定义的结构完整性"""

    @pytest.mark.parametrize("worldview", list(WorldView))
    def test_definition_has_required_fields(self, worldview):
        definition = ExtremeRewardFunctions.REWARD_MAP.get(worldview)
        assert definition is not None, f"{worldview} 缺少奖励定义"
        assert "formula" in definition, f"{worldview} 缺少 formula 字段"
        assert "extreme_behavior" in definition, f"{worldview} 缺少 extreme_behavior 字段"

    @pytest.mark.parametrize("worldview", list(WorldView))
    def test_formula_is_non_empty_string(self, worldview):
        definition = ExtremeRewardFunctions.REWARD_MAP[worldview]
        formula = definition["formula"]
        assert isinstance(formula, str) and len(formula.strip()) > 0, (
            f"{worldview} 的 formula 为空"
        )

    @pytest.mark.parametrize("worldview", list(WorldView))
    def test_extreme_behavior_is_non_empty_string(self, worldview):
        definition = ExtremeRewardFunctions.REWARD_MAP[worldview]
        behavior = definition["extreme_behavior"]
        assert isinstance(behavior, str) and len(behavior.strip()) > 0, (
            f"{worldview} 的 extreme_behavior 为空"
        )


class TestExtremeBehaviorSemantics:
    """验证极端行为描述是否与对应世界观的核心哲学一致"""

    # 世界观与描述中应包含的关键词（至少一个匹配）
    KEYWORD_MAP = {
        WorldView.MECHANICAL_MATERIALISM: ["误报", "漏报", "预警", "诊断"],
        WorldView.EVOLUTIONISM: ["新策略", "探索", "淘汰", "适应"],
        WorldView.EXISTENTIALISM: ["踏空", "风险", "减仓", "悲观"],
        WorldView.SKEPTICISM: ["证伪", "挑战", "错杀", "负面"],
        WorldView.INCOMPLETENESS: ["休眠", "错杀", "自省", "怀疑"],
        WorldView.PHYSICALISM: ["降频", "过热", "硬件", "稳定"],
        WorldView.OCCAMS_RAZOR: ["删除", "冗余", "简洁", "剃刀"],
        WorldView.BAYESIANISM: ["过拟合", "保守", "样本外", "差距"],
        WorldView.HERMENEUTICS: ["可读", "日报", "叙述", "理解"],
        WorldView.PLURALISM: ["混乱", "趋同", "多样性", "噪声"],
        WorldView.HISTORICISM: ["存储", "归档", "保存", "遗忘"],
        WorldView.HOLISM: ["同步", "降级", "整体", "断开"],
    }

    @pytest.mark.parametrize("worldview", list(WorldView))
    def test_behavior_contains_worldview_keywords(self, worldview):
        definition = ExtremeRewardFunctions.REWARD_MAP[worldview]
        behavior = definition["extreme_behavior"]
        keywords = self.KEYWORD_MAP.get(worldview, [])
        if not keywords:
            pytest.skip(f"世界观 {worldview} 未配置关键词")
        found = any(kw in behavior for kw in keywords)
        assert found, (
            f"{worldview} 的 extreme_behavior 不包含任何预期关键词: {keywords}\n"
            f"实际内容: {behavior}"
        )


class TestFormulaSemantics:
    """验证奖励公式在语义上是互斥的，且表达合理"""

    # 禁止出现两个世界观使用完全相同的公式（确保极端化）
    def test_formulas_are_unique(self):
        formulas = [
            info["formula"] for info in ExtremeRewardFunctions.REWARD_MAP.values()
        ]
        unique = set(formulas)
        # 允许少量重复（如某些世界观可能共享相同数学目标），但需记录
        if len(unique) < len(formulas):
            duplicated = [f for f in formulas if formulas.count(f) > 1]
            pytest.fail(
                f"发现重复的奖励公式: {set(duplicated)}。"
                "这暗示世界观之间缺乏极端化差异。"
            )

    @pytest.mark.parametrize("worldview", list(WorldView))
    def test_formula_contains_meaningful_expression(self, worldview):
        """
        公式至少应该看起来像一个数学表达式，例如包含 + - * / 或常见函数名。
        避免出现“无”或空占位符。
        """
        definition = ExtremeRewardFunctions.REWARD_MAP[worldview]
        formula = definition["formula"]
        # 简单启发式：至少包含一个运算符或常见函数
        has_math = any(
            op in formula
            for op in ["+", "-", "*", "/", "(", ")", "log", "exp", "max", "min", "F1", "sharpe"]
        )
        assert has_math, (
            f"{worldview} 的 formula 可能不是有效的数学表达式: '{formula}'"
        )


class TestExtremeRewardConsistency:
    """验证极端化奖励与世界观声明的优化目标一致"""

    # 世界观到其主要优化目标的映射（来自 WorldViewManifesto）
    TARGET_MAP = {
        WorldView.MECHANICAL_MATERIALISM: "F1",
        WorldView.EVOLUTIONISM: "novelty",
        WorldView.EXISTENTIALISM: "max_drawdown",
        WorldView.SKEPTICISM: "veto",
        WorldView.INCOMPLETENESS: "sleep",
        WorldView.PHYSICALISM: "uptime",
        WorldView.OCCAMS_RAZOR: "code_lines",
        WorldView.BAYESIANISM: "OOS_sharpe",
        WorldView.HERMENEUTICS: "read",
        WorldView.PLURALISM: "entropy",
        WorldView.HISTORICISM: "archive",
        WorldView.HOLISM: "sync",
    }

    @pytest.mark.parametrize("worldview", list(WorldView))
    def test_formula_references_primary_target(self, worldview):
        definition = ExtremeRewardFunctions.REWARD_MAP[worldview]
        formula = definition["formula"]
        expected_target = self.TARGET_MAP.get(worldview, "")
        if expected_target:
            assert expected_target in formula, (
                f"{worldview} 的公式中未包含其核心优化目标 '{expected_target}': {formula}"
          )
