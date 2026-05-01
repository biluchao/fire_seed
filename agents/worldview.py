#!/usr/bin/env python3
"""
火种系统 (FireSeed) 智能体世界观模块
========================================
定义智能体议会的哲学根基：
- 12 种不可调和的世界观 (WorldView)
- 每个智能体的宣言 (WorldViewManifesto)：核心信仰、优化目标、对立世界观、信息源隔离、时间尺度
- 智能体基类 (WorldViewAgent)：要求子类实现 propose() 和 challenge() 方法
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class WorldView(Enum):
    """12 种互斥的世界观"""
    MECHANICAL_MATERIALISM = "机械唯物主义"    # 监察者
    EVOLUTIONISM          = "进化论"           # 炼金术士
    EXISTENTIALISM        = "存在主义"         # 守护者
    SKEPTICISM            = "怀疑论"           # 魔鬼代言人
    INCOMPLETENESS        = "不完备定理"       # 哥德尔监视者
    PHYSICALISM           = "物理主义"         # 环境检察官
    OCCAMS_RAZOR          = "奥卡姆剃刀"       # 冗余审计官
    BAYESIANISM           = "贝叶斯主义"       # 权重校准师
    HERMENEUTICS          = "诠释学"           # 叙事官
    PLURALISM             = "多元主义"         # 多样性强制者
    HISTORICISM           = "历史主义"         # 归档审计官
    HOLISM                = "整体论"           # 跟单协调官


@dataclass(frozen=True)
class WorldViewManifesto:
    """世界观宣言：定义智能体的根本立场与约束"""
    worldview: WorldView
    core_belief: str                            # 核心信仰（一句哲学陈述）
    primary_objective: str                      # 优化的主要目标（公式描述）
    adversary_worldview: WorldView              # 天然对立的世界观
    forbidden_data_sources: Set[str] = field(default_factory=set)   # 禁止接触的数据类型
    exclusive_data_sources: Set[str] = field(default_factory=set)    # 专属数据源
    time_scale_seconds: float = 60.0            # 运作的时间尺度（秒）
    max_voting_weight: float = 0.35             # 最大投票权重上限
    independence_coefficient: float = 1.0       # 异质独立性系数（非主流范式加权用）


class WorldViewAgent(ABC):
    """携带世界观的智能体基类"""

    def __init__(self, manifesto: WorldViewManifesto):
        self.manifesto = manifesto
        self._cooling_off_until: float = 0.0    # 被成功挑战后的冷却时刻 (monotonic time)
        self.recent_proposals: List[Dict[str, Any]] = []
        self.challenge_history: List[Dict[str, Any]] = []

    @abstractmethod
    def propose(self, perception: Dict[str, Any]) -> Dict[str, Any]:
        """
        基于自身世界观提出决策建议。
        返回建议字典，至少包含:
          - direction: int   (1=多, -1=空, 0=中性)
          - confidence: float (0~1)
          - worldview: str   (自身世界观枚举值)
        """
        ...

    @abstractmethod
    def challenge(self, other_proposal: Dict[str, Any],
                  my_worldview: WorldView) -> Dict[str, Any]:
        """
        从自身世界观出发，挑战另一个智能体的提议。
        返回挑战报告，至少包含:
          - challenges: List[str]  发现的问题列表
          - veto: bool             是否要求否决
          - confidence_adjustment: float  建议的置信度调整量 (负数表示降低)
        """
        ...

    def enter_cooling_off(self, duration_seconds: float = 1800.0) -> None:
        """进入冷却期 (默认 30 分钟)"""
        import time
        self._cooling_off_until = time.time() + duration_seconds

    @property
    def is_in_cooling_off(self) -> bool:
        import time
        return time.time() < self._cooling_off_until

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}({self.manifesto.worldview.value})>"


# ======================== 预设宣言工厂 ========================
def build_manifesto(worldview: WorldView) -> WorldViewManifesto:
    """根据世界观枚举创建对应的宣言"""
    configs = {
        WorldView.MECHANICAL_MATERIALISM: {
            "core_belief": "系统是可分解为独立组件的钟表，故障可定位",
            "primary_objective": "-log(F1_score)",  # 宁可误报不可漏报
            "adversary": WorldView.HOLISM,
            "forbidden": {"KLINE", "ORDERBOOK", "POSITION"},
            "exclusive": {"SYSTEM_METRICS", "PROCESS_TABLE", "NETWORK_IO"},
            "time_scale": 15.0,
            "independence_coefficient": 1.0,
        },
        WorldView.EVOLUTIONISM: {
            "core_belief": "策略是适应环境的生命体，适者生存",
            "primary_objective": "sharpe × novelty",
            "adversary": WorldView.EXISTENTIALISM,
            "forbidden": {"SYSTEM_METRICS", "REAL_TIME_PNL"},
            "exclusive": {"ACADEMIC_PAPERS", "FACTOR_EXPRESSION_TEXT"},
            "time_scale": 86400.0,
            "independence_coefficient": 1.3,
        },
        WorldView.EXISTENTIALISM: {
            "core_belief": "市场的本质是无常，唯一能确定的是风险",
            "primary_objective": "-max_drawdown",
            "adversary": WorldView.EVOLUTIONISM,
            "forbidden": {"ORDERBOOK", "SENTIMENT", "SIGNAL_SCORE"},
            "exclusive": {"FULL_RETURN_HISTORY", "TAIL_RISK_METRICS"},
            "time_scale": 300.0,
            "independence_coefficient": 1.2,
        },
        WorldView.SKEPTICISM: {
            "core_belief": "任何真理都可能是临时的，需要不断证伪",
            "primary_objective": "F1(correct_veto)",
            "adversary": WorldView.BAYESIANISM,
            "forbidden": {"LIVE_PRICE", "REAL_TIME_ANYTHING"},
            "exclusive": {"ADVERSARIAL_SAMPLE_DB", "FAILURE_GENE_DB"},
            "time_scale": 0.0,  # 事件驱动
            "independence_coefficient": 1.5,
        },
        WorldView.INCOMPLETENESS: {
            "core_belief": "系统永远无法完全理解自身，必须保持谦卑",
            "primary_objective": "missed_loss_during_sleep",
            "adversary": WorldView.MECHANICAL_MATERIALISM,
            "forbidden": {"MARKET_DATA", "TRADE_SIGNAL", "ORDER_FLOW"},
            "exclusive": {"SYSTEM_META_METRICS", "LOG_LENGTHS", "PROCESS_AGE"},
            "time_scale": 60.0,
            "independence_coefficient": 2.0,  # 最独立的范式
        },
        WorldView.PHYSICALISM: {
            "core_belief": "一切问题终将表现为物理参数异常",
            "primary_objective": "uptime / error_count",
            "adversary": WorldView.EVOLUTIONISM,
            "forbidden": {"KLINE", "POSITION", "ORDERBOOK"},
            "exclusive": {"CPU_TEMP", "FAN_SPEED", "SMART_DATA", "VOLTAGE"},
            "time_scale": 60.0,
            "independence_coefficient": 1.1,
        },
        WorldView.OCCAMS_RAZOR: {
            "core_belief": "简洁是真理的标志，冗余是熵的征兆",
            "primary_objective": "-code_lines",
            "adversary": WorldView.PLURALISM,
            "forbidden": {"ALL_MARKET_DATA"},
            "exclusive": {"PYTHON_AST", "CONFIG_YAML", "IMPORT_GRAPH"},
            "time_scale": 86400.0,
            "independence_coefficient": 1.4,
        },
        WorldView.BAYESIANISM: {
            "core_belief": "概率是信念的量化，需要不断用证据更新",
            "primary_objective": "OOS_sharpe - IS_sharpe",
            "adversary": WorldView.SKEPTICISM,
            "forbidden": {"RAW_PRICE", "SENTIMENT"},
            "exclusive": {"FACTOR_IC_SEQUENCE", "WEIGHT_POSTERIOR"},
            "time_scale": 86400.0,
            "independence_coefficient": 1.0,
        },
        WorldView.HERMENEUTICS: {
            "core_belief": "意义存在于叙述之中，真相是故事的函数",
            "primary_objective": "human_read_completion_rate",
            "adversary": WorldView.OCCAMS_RAZOR,
            "forbidden": {"RAW_ORDER_FLOW", "FACTOR_EXPRESSION"},
            "exclusive": {"VOTING_RECORDS", "BEHAVIOR_LOG_SUMMARY"},
            "time_scale": 86400.0,
            "independence_coefficient": 1.2,
        },
        WorldView.PLURALISM: {
            "core_belief": "正确的决策需要多样化的视角，没有单一真理",
            "primary_objective": "H(voting_distribution)",
            "adversary": WorldView.OCCAMS_RAZOR,
            "forbidden": {"TRADE_HISTORY"},
            "exclusive": {"VOTE_DIRECTION_SEQUENCE", "AGENT_WEIGHT_HISTORY"},
            "time_scale": 300.0,
            "independence_coefficient": 1.3,
        },
        WorldView.HISTORICISM: {
            "core_belief": "一切当前状态都可以从历史痕迹中理解",
            "primary_objective": "archived_data / total_data",
            "adversary": WorldView.EVOLUTIONISM,
            "forbidden": {"REAL_TIME_ANYTHING"},
            "exclusive": {"FILE_TIMESTAMPS", "DB_PAGE_COUNTS", "INODE_TABLES"},
            "time_scale": 3600.0,
            "independence_coefficient": 1.1,
        },
        WorldView.HOLISM: {
            "core_belief": "部分异常反映整体失调，断裂即风险",
            "primary_objective": "sync_success_rate",
            "adversary": WorldView.MECHANICAL_MATERIALISM,
            "forbidden": {"INDIVIDUAL_ACCOUNT_DETAIL"},
            "exclusive": {"SUB_ACCOUNT_STATUS", "API_RESPONSE_CODES"},
            "time_scale": 30.0,
            "independence_coefficient": 1.0,
        },
    }

    cfg = configs[worldview]
    return WorldViewManifesto(
        worldview=worldview,
        core_belief=cfg["core_belief"],
        primary_objective=cfg["primary_objective"],
        adversary_worldview=cfg["adversary"],
        forbidden_data_sources=cfg["forbidden"],
        exclusive_data_sources=cfg["exclusive"],
        time_scale_seconds=cfg["time_scale"],
        independence_coefficient=cfg.get("independence_coefficient", 1.0),
    )
