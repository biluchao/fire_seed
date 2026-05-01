#!/usr/bin/env python3
"""
火种系统 (FireSeed) 极端化奖励函数定义
=========================================
为每个智能体世界观提供互斥的、极端化的目标函数，
确保议会中不存在中庸的"共同利益"，从底层保障认知多样性。
"""

from enum import Enum
from typing import Any, Dict, Optional

from agents.worldview import WorldView


class ExtremeRewardFunctions:
    """
    极端化奖励函数注册表。
    每个世界观对应一个不可调和的优化目标，服务于单一"信仰"。
    """

    # ======================== 奖励定义 ========================
    REWARD_MAP: Dict[WorldView, Dict[str, Any]] = {
        WorldView.MECHANICAL_MATERIALISM: {
            "formula": "-log(anomaly_detection_F1 + 1e-6)",
            "description": "宁可误报，不可漏报。追求系统异常的完美检测率，接受较高的假阳性。",
            "extreme_behavior": "即使系统正常运行也会频繁发出预警，永远不信任表面平静。",
            "primary_metric": "F1_score",
            "direction": "minimize",
            "primary_metric_weight": 1.0,
            "secondary_metric": "false_positive_rate",
            "secondary_weight": 0.3,
            "target_range": [0.95, 1.0],          # 追求极高的召回率
            "natural_adversary": WorldView.SKEPTICISM,
        },

        WorldView.EVOLUTIONISM: {
            "formula": "sharpe_ratio × strategy_novelty",
            "description": "宁可要一个低夏普的新策略，也不要高夏普但与其他策略雷同的老策略。",
            "extreme_behavior": "不断生成和淘汰策略，不关心近期盈亏，只关心基因多样性。",
            "primary_metric": "weighted_sharpe_novelty",
            "direction": "maximize",
            "primary_metric_weight": 1.0,
            "secondary_metric": "generation_rate",
            "secondary_weight": 0.2,
            "natural_adversary": WorldView.EXISTENTIALISM,
        },

        WorldView.EXISTENTIALISM: {
            "formula": "-max_drawdown",
            "description": "宁可踏空整段行情，也不能承受一次巨大回撤。生存是第一要义。",
            "extreme_behavior": "市场一波动就建议减仓，永远悲观，永远准备最坏情况。",
            "primary_metric": "max_drawdown_pct",
            "direction": "minimize",
            "primary_metric_weight": 1.0,
            "secondary_metric": "cvar_99",
            "secondary_weight": 0.5,
            "natural_adversary": WorldView.EVOLUTIONISM,
        },

        WorldView.SKEPTICISM: {
            "formula": "F1_score(correct_veto)",
            "description": "宁可错杀(否决)一千个正确提案，也不放过一个可能出错的提案。",
            "extreme_behavior": "对每个提案都找出负面理由并发动挑战，永远不信任任何信号。",
            "primary_metric": "veto_precision_recall_F1",
            "direction": "maximize",
            "primary_metric_weight": 1.0,
            "secondary_metric": "challenge_count",
            "secondary_weight": 0.2,
            "natural_adversary": WorldView.BAYESIANISM,
        },

        WorldView.INCOMPLETENESS: {
            "formula": "missed_loss_during_awake - 0.5 * profit_lost_during_sleep",
            "description": "宁可让系统频繁休眠错过利润，也要避免清醒时遭受巨大亏损。",
            "extreme_behavior": "任何风吹草动就让系统休眠，怀疑指数的阈值设得极低。",
            "primary_metric": "loss_during_awake_state",
            "direction": "minimize",
            "primary_metric_weight": 1.0,
            "secondary_metric": "sleep_frequency",
            "secondary_weight": 0.3,
            "natural_adversary": WorldView.MECHANICAL_MATERIALISM,
        },

        WorldView.PHYSICALISM: {
            "formula": "uptime_seconds / (error_count + 1) - 0.5 * cpu_frequency_ghz",
            "description": "宁可降频运行，也不能过热宕机。硬件稳定高于一切性能。",
            "extreme_behavior": "CPU一高就建议降频，性能永远让位于稳定，永远在担心散热。",
            "primary_metric": "weighted_stability",
            "direction": "maximize",
            "primary_metric_weight": 1.0,
            "secondary_metric": "temperature_variance",
            "secondary_weight": 0.4,
            "natural_adversary": WorldView.EVOLUTIONISM,
        },

        WorldView.OCCAMS_RAZOR: {
            "formula": "-total_code_lines - 10 * unused_functions",
            "description": "宁可删除可能在未来有用的代码，也要保持当前的极致简洁。",
            "extreme_behavior": "扫描到任何未用函数就要求立刻删除，不管注释怎么说。",
            "primary_metric": "dead_code_ratio",
            "direction": "minimize",
            "primary_metric_weight": 1.0,
            "secondary_metric": "cyclomatic_complexity",
            "secondary_weight": 0.3,
            "natural_adversary": WorldView.PLURALISM,
        },

        WorldView.BAYESIANISM: {
            "formula": "out_of_sample_sharpe - in_sample_sharpe",
            "description": "宁可保守到错过行情，也不能让样本内外的差距超过容忍范围。",
            "extreme_behavior": "任何因子只要样本内外差异超过阈值就建议冻结，永不信任过拟合。",
            "primary_metric": "sharpe_oos_minus_is",
            "direction": "maximize",          # 越接近0越好（差距越小）
            "primary_metric_weight": 1.0,
            "secondary_metric": "ic_decay_rate",
            "secondary_weight": 0.4,
            "natural_adversary": WorldView.SKEPTICISM,
        },

        WorldView.HERMENEUTICS: {
            "formula": "human_read_completion_rate × 100 - report_generation_seconds / 60",
            "description": "宁可日报啰嗦如小说，也要确保人类运维者完整阅读并理解。",
            "extreme_behavior": "日报写得像故事，即使信息密度低也坚持用叙事代替数据表。",
            "primary_metric": "read_completion_estimate",
            "direction": "maximize",
            "primary_metric_weight": 1.0,
            "secondary_metric": "generation_speed",
            "secondary_weight": 0.3,
            "natural_adversary": WorldView.MECHANICAL_MATERIALISM,
        },

        WorldView.PLURALISM: {
            "formula": "entropy(voting_distribution)",
            "description": "宁可议会陷入混乱的争吵，也不要看到所有智能体面带微笑地同意彼此。",
            "extreme_behavior": "投票一致性过高时主动注入噪声，永远怀疑共识。",
            "primary_metric": "voting_entropy",
            "direction": "maximize",
            "primary_metric_weight": 1.0,
            "secondary_metric": "consensus_override_count",
            "secondary_weight": 0.2,
            "natural_adversary": WorldView.OCCAMS_RAZOR,
        },

        WorldView.HISTORICISM: {
            "formula": "archived_bytes / (total_critical_data_bytes + 1)",
            "description": "宁可存储空间耗尽，也不能丢失任何一段历史记录。",
            "extreme_behavior": "永远觉得存储不够，不断要求扩容，从不建议删除任何东西。",
            "primary_metric": "archive_coverage_ratio",
            "direction": "maximize",
            "primary_metric_weight": 1.0,
            "secondary_metric": "storage_efficiency",
            "secondary_weight": 0.1,
            "natural_adversary": WorldView.OCCAMS_RAZOR,
        },

        WorldView.HOLISM: {
            "formula": "sync_success_rate - 0.5 * (1 - sync_latency_normalized)",
            "description": "宁可降级甚至暂停跟单，也不能让任何一个子账户处于不同步状态。",
            "extreme_behavior": "同步延迟一高就暂停跟单，不管错过行情，永远追求整体一致。",
            "primary_metric": "sync_success_rate",
            "direction": "maximize",
            "primary_metric_weight": 1.0,
            "secondary_metric": "max_sync_latency_ms",
            "secondary_weight": 0.3,
            "natural_adversary": WorldView.MECHANICAL_MATERIALISM,
        },
    }

    # ======================== 查询接口 ========================
    @classmethod
    def get_reward_info(cls, worldview: WorldView) -> Dict[str, Any]:
        """
        获取指定世界观的奖励函数完整信息。
        若世界观未注册，返回空的默认值。
        """
        return cls.REWARD_MAP.get(worldview, {
            "formula": "unknown",
            "description": "未定义",
            "extreme_behavior": "无",
            "primary_metric": "unknown",
            "direction": "neutral",
            "primary_metric_weight": 0.0,
            "secondary_metric": "unknown",
            "secondary_weight": 0.0,
            "target_range": [],
            "natural_adversary": WorldView.SKEPTICISM,
        })

    @classmethod
    def get_formula(cls, worldview: WorldView) -> str:
        return cls.get_reward_info(worldview)["formula"]

    @classmethod
    def get_primary_metric(cls, worldview: WorldView) -> str:
        return cls.get_reward_info(worldview)["primary_metric"]

    @classmethod
    def get_optimization_direction(cls, worldview: WorldView) -> str:
        """返回 'maximize' 或 'minimize'"""
        return cls.get_reward_info(worldview)["direction"]

    @classmethod
    def get_adversary_worldview(cls, worldview: WorldView) -> WorldView:
        """返回该世界观的自然对立世界观"""
        return cls.get_reward_info(worldview)["natural_adversary"]

    @classmethod
    def list_all_rewards(cls) -> Dict[str, Dict[str, Any]]:
        """以世界观名称字符串为键返回所有奖励信息摘要"""
        result = {}
        for wv, info in cls.REWARD_MAP.items():
            result[wv.value] = {
                "formula": info["formula"],
                "description": info["description"],
                "primary_metric": info["primary_metric"],
                "natural_adversary": info["natural_adversary"].value,
            }
        return result
