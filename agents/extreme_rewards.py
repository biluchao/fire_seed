#!/ Helicoidal Strategy Reinforcement Functions
"""
火种系统 (FireSeed) 极端化奖励函数定义模块
=============================================
本模块为16个智能体各自的世界观提供互斥的优化目标，
确保智能体之间不存在趋同动机，从而维持根本的认知多样性。

每个奖励条目包含：
- formula: 奖励函数的数学描述（字符串）
- extreme_behavior: 该世界观在极端情况下的行为倾向
- primary_metric: 主要优化指标名
- anti_metric: 天然冲突的指标（用于对抗性校验）
"""

from typing import Any, Dict, Optional
from enum import Enum

# 注意：WorldView 枚举被设计为与 worldview.py 保持同步，
# 实际部署时将从 worldview 模块导入。
# 此处重新声明以避免循环导入，生产环境中会合并到 worldview.py。
class WorldView(Enum):
    MECHANICAL_MATERIALISM = "机械唯物主义"
    EVOLUTIONISM = "进化论"
    EXISTENTIALISM = "存在主义"
    SKEPTICISM = "怀疑论"
    INCOMPLETENESS = "不完备定理"
    PHYSICALISM = "物理主义"
    OCCAMS_RAZOR = "奥卡姆剃刀"
    BAYESIANISM = "贝叶斯主义"
    HERMENEUTICS = "诠释学"
    PLURALISM = "多元主义"
    HISTORICISM = "历史主义"
    HOLISM = "整体论"
    EMPIRICISM = "经验主义"               # 数据监察员
    POSITIVISM = "实证主义"               # 执行质量审计官
    DEPENDENCY_INVERSION = "依赖倒置"     # 外部依赖哨兵
    SECURITY_PESSIMISM = "安全悲观主义"   # 安全态势感知官


class ExtremeRewardFunctions:
    """
    极端化奖励函数注册表。

    每个世界观被赋予一个单一、极端的优化方向，与其它世界观形成不可调和的矛盾。
    这种设计保证了议会投票时永远存在不同声音，无法被单一利益捕获。
    """

    # ---- 奖励定义 ----
    REWARD_MAP: Dict[WorldView, Dict[str, Any]] = {
        WorldView.MECHANICAL_MATERIALISM: {
            "formula": "-log(F1_score)",            # 异常检测F1的对数损失
            "extreme_behavior": "宁可误报不可漏报，频繁发出预警",
            "primary_metric": "anomaly_detection_f1",
            "anti_metric": "false_alarm_rate",      # 与低误报目标冲突
        },
        WorldView.EVOLUTIONISM: {
            "formula": "sharpe × novelty",           # 夏普乘以新颖度
            "extreme_behavior": "不断生成新策略，即使短期亏损也不停止探索",
            "primary_metric": "novelty_weighted_sharpe",
            "anti_metric": "max_drawdown",           # 与保守目标冲突
        },
        WorldView.EXISTENTIALISM: {
            "formula": "-max_drawdown",              # 最小化最大回撤
            "extreme_behavior": "市场一有波动就建议减仓，永远悲观",
            "primary_metric": "max_drawdown",
            "anti_metric": "annual_return",          # 与收益目标冲突
        },
        WorldView.SKEPTICISM: {
            "formula": "F1(correct_veto)",           # 成功证伪的F1
            "extreme_behavior": "对每一个提案都找出至少一个致命缺陷",
            "primary_metric": "veto_f1",
            "anti_metric": "decision_approval_rate", # 与效率目标冲突
        },
        WorldView.INCOMPLETENESS: {
            "formula": "missed_loss_during_sleep",   # 休眠期间错过的亏损
            "extreme_behavior": "有任何风吹草动就让系统休眠",
            "primary_metric": "sleep_triggered_risk_coverage",
            "anti_metric": "lost_profit_opportunity",# 与盈利目标冲突
        },
        WorldView.PHYSICALISM: {
            "formula": "uptime / error_count",       # 运行时间除以错误次数
            "extreme_behavior": "CPU一高就建议降频，性能永远让位于稳定",
            "primary_metric": "system_uptime",
            "anti_metric": "cpu_utilization",        # 与算力需求冲突
        },
        WorldView.OCCAMS_RAZOR: {
            "formula": "-code_lines / 1000",         # 代码行数越少越好
            "extreme_behavior": "扫描到未用函数就要求删除，不管未来可能有用",
            "primary_metric": "dead_code_ratio",
            "anti_metric": "feature_completeness",   # 与功能完整性冲突
        },
        WorldView.BAYESIANISM: {
            "formula": "OOS_sharpe - IS_sharpe",     # 样本外夏普 − 样本内夏普
            "extreme_behavior": "任何因子只要样本内外差距大就建议冻结",
            "primary_metric": "sharpe_oos_is_gap",
            "anti_metric": "in_sample_sharpe",       # 与拟合目标冲突
        },
        WorldView.HERMENEUTICS: {
            "formula": "human_reading_completion_rate", # 人类阅读完成率
            "extreme_behavior": "日报写得像小说，即使牺牲信息密度也要保证可读性",
            "primary_metric": "report_readability_score",
            "anti_metric": "report_conciseness",     # 与简洁目标冲突
        },
        WorldView.PLURALISM: {
            "formula": "H(voting_distribution)",      # 投票分布的信息熵
            "extreme_behavior": "投票一致性过高时主动注入噪声，追求混沌",
            "primary_metric": "voting_entropy",
            "anti_metric": "decision_consistency",    # 与一致性目标冲突
        },
        WorldView.HISTORICISM: {
            "formula": "archived_data / total_data",  # 归档覆盖率
            "extreme_behavior": "永远觉得存储不够，不断要求扩容",
            "primary_metric": "archive_coverage_ratio",
            "anti_metric": "storage_cost",            # 与成本目标冲突
        },
        WorldView.HOLISM: {
            "formula": "sync_success_rate",           # 跟单同步成功率
            "extreme_behavior": "同步延迟一高就暂停跟单，不管错过行情",
            "primary_metric": "copy_sync_success_rate",
            "anti_metric": "sync_throughput",         # 与吞吐目标冲突
        },
        WorldView.EMPIRICISM: {
            "formula": "data_health_score × detection_timeliness",
            "extreme_behavior": "数据源出现微小异常即触发降级，永不相信数据的完美",
            "primary_metric": "data_quality_score",
            "anti_metric": "system_availability",     # 与可用性目标冲突
        },
        WorldView.POSITIVISM: {
            "formula": "execution_quality_score / slippage_cost",
            "extreme_behavior": "滑点超过0.01%就发出审计警告，追求零损耗执行",
            "primary_metric": "execution_quality",
            "anti_metric": "execution_speed",         # 与速度目标冲突
        },
        WorldView.DEPENDENCY_INVERSION: {
            "formula": "sum(1/api_latency)",
            "extreme_behavior": "任何外部依赖的响应时间超过阈值就标记为不可信",
            "primary_metric": "api_health_score",
            "anti_metric": "internal_efficiency",     # 与内部优化目标冲突
        },
        WorldView.SECURITY_PESSIMISM: {
            "formula": "-unauthorized_access_attempts",
            "extreme_behavior": "假设系统一直在被攻击，每一个异常请求都是潜在的入侵",
            "primary_metric": "security_incident_count",
            "anti_metric": "user_experience",         # 与便利性目标冲突
        },
    }

    @classmethod
    def get_reward_config(cls, worldview: WorldView) -> Dict[str, Any]:
        """
        返回指定世界观的奖励配置。
        若未找到则返回默认的中性配置。
        """
        return cls.REWARD_MAP.get(worldview, {
            "formula": "0.0",
            "extreme_behavior": "无特定极端行为",
            "primary_metric": "unknown",
            "anti_metric": "unknown",
        })

    @classmethod
    def get_conflicting_pair(cls, worldview: WorldView) -> Optional[WorldView]:
        """
        根据当前世界观的 anti_metric 寻找与之冲突的世界观。
        若没有明显冲突，则返回怀疑论（默认挑战者）。
        """
        config = cls.get_reward_config(worldview)
        anti_metric = config.get("anti_metric")
        if not anti_metric:
            return WorldView.SKEPTICISM

        # 寻找 primary_metric 与 anti_metric 匹配的世界观
        for wv, cfg in cls.REWARD_MAP.items():
            if wv == worldview:
                continue
            if cfg.get("primary_metric") == anti_metric:
                return wv
        return WorldView.SKEPTICISM

    @classmethod
    def get_all_metrics(cls) -> Dict[str, WorldView]:
        """返回所有主要指标到世界观的映射"""
        metric_map = {}
        for wv, cfg in cls.REWARD_MAP.items():
            primary = cfg.get("primary_metric")
            if primary:
                metric_map[primary] = wv
        return metric_map
