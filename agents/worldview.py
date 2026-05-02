#!/usr/bin/env python3
"""
火种系统 (FireSeed) 世界观与智能体基类
=============================================
定义16种互斥的智能体世界观、数据源隔离规则、以及
携带世界观的智能体基类 WorldViewAgent。
"""

from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from abc import ABC, abstractmethod
import time


# ======================== 世界观枚举 ========================
class WorldView(Enum):
    """16 种互斥的底层认知范式"""
    MECHANICAL_MATERIALISM = "机械唯物主义"   # 监察者
    EVOLUTIONISM           = "进化论"          # 炼金术士
    EXISTENTIALISM         = "存在主义"        # 守护者
    SKEPTICISM             = "怀疑论"          # 魔鬼代言人
    INCOMPLETENESS         = "不完备定理"       # 哥德尔监视者
    PHYSICALISM            = "物理主义"         # 环境检察官
    OCCAMS_RAZOR           = "奥卡姆剃刀"       # 冗余审计官
    BAYESIANISM            = "贝叶斯主义"       # 权重校准师
    HERMENEUTICS           = "诠释学"           # 叙事官
    PLURALISM              = "多元主义"         # 多样性强制者
    HISTORICISM            = "历史主义"         # 归档审计官
    HOLISM                 = "整体论"           # 跟单协调官
    POSITIVISM             = "实证主义"         # 执行质量审计官
    DEPENDENCY_INVERSION   = "依赖倒置"         # 外部依赖哨兵
    SECURITY_PESSIMISM     = "安全悲观主义"      # 安全态势感知官
    DATA_EMPIRICISM        = "经验主义"          # 数据监察员


# ======================== 世界观宣言 ========================
@dataclass
class WorldViewManifesto:
    """描述一个智能体不可动摇的核心信仰与约束"""
    worldview: WorldView
    core_belief: str                             # 核心信仰
    primary_optimization_target: str             # 主要优化目标
    adversary_worldview: WorldView                # 天然对立的世界观
    forbidden_data_sources: Set[str] = field(default_factory=set)   # 禁止访问的数据源类型
    exclusive_data_sources: Set[str] = field(default_factory=set)   # 专属数据源
    time_scale_seconds: float = 60.0             # 运行时间尺度（秒）


# ======================== 预设世界观清单 ========================
# 使用方法: WORLDVIEW_PRESETS[WorldView.XXX] 获取宣言
WORLDVIEW_PRESETS: Dict[WorldView, WorldViewManifesto] = {
    WorldView.MECHANICAL_MATERIALISM: WorldViewManifesto(
        worldview=WorldView.MECHANICAL_MATERIALISM,
        core_belief="系统是可分解为独立组件的钟表，故障可定位",
        primary_optimization_target="故障检测覆盖率 × 响应速度",
        adversary_worldview=WorldView.DATA_EMPIRICISM,
        forbidden_data_sources={"kline", "orderbook", "trading_signal"},
        exclusive_data_sources={"system_metrics", "process_status", "network_stats"},
        time_scale_seconds=15,
    ),
    WorldView.EVOLUTIONISM: WorldViewManifesto(
        worldview=WorldView.EVOLUTIONISM,
        core_belief="策略是不断适应环境的生命体，适者生存",
        primary_optimization_target="策略夏普 × 策略新颖度",
        adversary_worldview=WorldView.EXISTENTIALISM,
        forbidden_data_sources={"system_metrics", "real_time_pnl"},
        exclusive_data_sources={"historical_ohlcv", "academic_papers", "forum_feeds"},
        time_scale_seconds=86400,
    ),
    WorldView.EXISTENTIALISM: WorldViewManifesto(
        worldview=WorldView.EXISTENTIALISM,
        core_belief="市场的本质是无常，唯一能确定的是风险",
        primary_optimization_target="-max_drawdown",
        adversary_worldview=WorldView.EVOLUTIONISM,
        forbidden_data_sources={"orderbook", "sentiment"},
        exclusive_data_sources={"full_return_series", "stress_test_scenarios"},
        time_scale_seconds=300,
    ),
    WorldView.SKEPTICISM: WorldViewManifesto(
        worldview=WorldView.SKEPTICISM,
        core_belief="任何真理都可能是临时的，需要不断证伪",
        primary_optimization_target="成功证伪次数 × 证伪准确率",
        adversary_worldview=WorldView.BAYESIANISM,
        forbidden_data_sources={"live_price", "real_time_pnl"},
        exclusive_data_sources={"adversarial_samples", "failure_gene_db"},
        time_scale_seconds=0,  # 事件驱动
    ),
    WorldView.INCOMPLETENESS: WorldViewManifesto(
        worldview=WorldView.INCOMPLETENESS,
        core_belief="系统永远无法完全理解自身，必须保持谦卑",
        primary_optimization_target="休眠后的市场真实亏损最小化",
        adversary_worldview=WorldView.MECHANICAL_MATERIALISM,
        forbidden_data_sources={"market_data", "trade_signal"},
        exclusive_data_sources={"system_metadata", "log_length", "process_age"},
        time_scale_seconds=60,
    ),
    WorldView.PHYSICALISM: WorldViewManifesto(
        worldview=WorldView.PHYSICALISM,
        core_belief="一切问题终将表现为物理参数异常",
        primary_optimization_target="系统运行时间 / 故障次数",
        adversary_worldview=WorldView.EVOLUTIONISM,
        forbidden_data_sources={"kline", "position"},
        exclusive_data_sources={"/proc", "/sys", "sensors", "smartctl"},
        time_scale_seconds=60,
    ),
    WorldView.OCCAMS_RAZOR: WorldViewManifesto(
        worldview=WorldView.OCCAMS_RAZOR,
        core_belief="简洁是真理的标志，冗余是熵的征兆",
        primary_optimization_target="-有效代码行数",
        adversary_worldview=WorldView.PLURALISM,
        forbidden_data_sources={"all_market_data"},
        exclusive_data_sources={"python_source", "config_files", "ast_tree"},
        time_scale_seconds=86400,
    ),
    WorldView.BAYESIANISM: WorldViewManifesto(
        worldview=WorldView.BAYESIANISM,
        core_belief="概率是信念的量化，需不断用证据更新",
        primary_optimization_target="样本外夏普 - 样本内夏普",
        adversary_worldview=WorldView.SKEPTICISM,
        forbidden_data_sources={"raw_price"},
        exclusive_data_sources={"factor_ic_series", "weight_variance"},
        time_scale_seconds=86400,
    ),
    WorldView.HERMENEUTICS: WorldViewManifesto(
        worldview=WorldView.HERMENEUTICS,
        core_belief="意义存在于叙述之中，真相是故事的函数",
        primary_optimization_target="人类运维者日报阅读完成率",
        adversary_worldview=WorldView.OCCAMS_RAZOR,
        forbidden_data_sources={"raw_order_flow"},
        exclusive_data_sources={"parliament_votes", "behavior_logs", "performance_summary"},
        time_scale_seconds=86400,
    ),
    WorldView.PLURALISM: WorldViewManifesto(
        worldview=WorldView.PLURALISM,
        core_belief="正确的决策需要多样化的视角，没有单一真理",
        primary_optimization_target="投票分布的信息熵",
        adversary_worldview=WorldView.OCCAMS_RAZOR,
        forbidden_data_sources={"trade_history"},
        exclusive_data_sources={"agent_voting_directions", "consensus_history"},
        time_scale_seconds=300,
    ),
    WorldView.HISTORICISM: WorldViewManifesto(
        worldview=WorldView.HISTORICISM,
        core_belief="一切当前状态都可以从历史痕迹中理解",
        primary_optimization_target="归档覆盖率 × 恢复验证率",
        adversary_worldview=WorldView.HOLISM,
        forbidden_data_sources={"real_time_anything"},
        exclusive_data_sources={"file_timestamps", "archive_index", "storage_bills"},
        time_scale_seconds=3600,
    ),
    WorldView.HOLISM: WorldViewManifesto(
        worldview=WorldView.HOLISM,
        core_belief="系统是各部分的协同体，部分异常反映整体失调",
        primary_optimization_target="子账户同步成功率",
        adversary_worldview=WorldView.MECHANICAL_MATERIALISM,
        forbidden_data_sources={"individual_account_detail"},
        exclusive_data_sources={"subaccount_status", "sync_delay", "copy_error_log"},
        time_scale_seconds=30,
    ),
    WorldView.POSITIVISM: WorldViewManifesto(
        worldview=WorldView.POSITIVISM,
        core_belief="执行质量只能用统计数据证明，不接受理论最优",
        primary_optimization_target="订单执行质量综合评分",
        adversary_worldview=WorldView.MECHANICAL_MATERIALISM,
        forbidden_data_sources={"trading_signal", "position"},
        exclusive_data_sources={"fill_records", "slippage_stats", "route_metrics"},
        time_scale_seconds=60,
    ),
    WorldView.DEPENDENCY_INVERSION: WorldViewManifesto(
        worldview=WorldView.DEPENDENCY_INVERSION,
        core_belief="高层策略不应依赖底层实现，外部依赖是潜在的断裂点",
        primary_optimization_target="外部服务可用性 × 故障恢复速度",
        adversary_worldview=WorldView.MECHANICAL_MATERIALISM,
        forbidden_data_sources={"kline", "trading_signal"},
        exclusive_data_sources={"api_health_pings", "cloud_storage_status", "llm_response_times"},
        time_scale_seconds=30,
    ),
    WorldView.SECURITY_PESSIMISM: WorldViewManifesto(
        worldview=WorldView.SECURITY_PESSIMISM,
        core_belief="假设系统一直在被攻击，只是尚未发现",
        primary_optimization_target="安全事件发现率 × 误报容忍度",
        adversary_worldview=WorldView.PLURALISM,
        forbidden_data_sources={"profit_data"},
        exclusive_data_sources={"login_logs", "api_usage_patterns", "config_changes"},
        time_scale_seconds=30,
    ),
    WorldView.DATA_EMPIRICISM: WorldViewManifesto(
        worldview=WorldView.DATA_EMPIRICISM,
        core_belief="无数据，不决策；脏数据，必误判",
        primary_optimization_target="数据异常检测覆盖率 × 报告及时性",
        adversary_worldview=WorldView.MECHANICAL_MATERIALISM,
        forbidden_data_sources={"trading_signal", "position"},
        exclusive_data_sources={"raw_ticks", "ws_heartbeat", "ntp_offset", "backtest_trades"},
        time_scale_seconds=10,
    ),
}


# ======================== 数据源注册表 ========================
class DataSourceRegistry:
    """
    强制执行数据隔离的全局注册表。
    每个智能体通过此注册表检查是否允许访问特定数据源。
    """

    @staticmethod
    def is_data_allowed(worldview: WorldView, data_type: str) -> bool:
        manifesto = WORLDVIEW_PRESETS.get(worldview)
        if not manifesto:
            return True  # 未知世界观默认允许
        # 检查禁止列表
        if data_type.lower() in {item.lower() for item in manifesto.forbidden_data_sources}:
            return False
        return True

    @staticmethod
    def get_exclusive_sources(worldview: WorldView) -> Set[str]:
        manifesto = WORLDVIEW_PRESETS.get(worldview)
        return manifesto.exclusive_data_sources if manifesto else set()

    @staticmethod
    def get_time_scale(worldview: WorldView) -> float:
        manifesto = WORLDVIEW_PRESETS.get(worldview)
        return manifesto.time_scale_seconds if manifesto else 60.0


# ======================== 智能体基类 ========================
class WorldViewAgent(ABC):
    """
    携带世界观的智能体抽象基类。
    所有智能体必须继承此类，注入自己的世界观宣言。
    """

    def __init__(self, manifesto: WorldViewManifesto):
        self.manifesto = manifesto
        self.cooling_off_until: float = 0.0          # 被成功挑战后的冷却期结束时间戳
        self.recent_proposals: List[Dict[str, Any]] = []
        self.challenge_history: List[Dict[str, Any]] = []

    @abstractmethod
    def propose(self, perception: Dict[str, Any]) -> Dict[str, Any]:
        """
        基于自身世界观产生决策建议。
        :param perception: 当前感知数据（已由数据注册表过滤）
        :return: 建议字典，至少包含 direction (int), confidence (float)
        """
        ...

    @abstractmethod
    def challenge(self, other_proposal: Dict[str, Any], my_worldview: WorldView) -> Dict[str, Any]:
        """
        从自身世界观出发挑战另一个智能体的提案。
        :param other_proposal: 被挑战的提案
        :param my_worldview: 自身的世界观（用于上下文）
        :return: 挑战结果，至少包含 veto (bool), reason (str)
        """
        ...

    def apply_cooling_off(self, duration_sec: float = 1800.0) -> None:
        """进入冷却期（挑战失败后被惩罚）"""
        self.cooling_off_until = time.time() + duration_sec

    @property
    def is_cooling_off(self) -> bool:
        return time.time() < self.cooling_off_until

    def filter_perception(self, raw_perception: Dict[str, Any]) -> Dict[str, Any]:
        """
        过滤感知数据，仅保留本智能体被允许访问的部分。
        """
        filtered = {}
        for key, value in raw_perception.items():
            if DataSourceRegistry.is_data_allowed(self.manifesto.worldview, key):
                filtered[key] = value
        return filtered
