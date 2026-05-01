#!/usr/bin/env python3
"""
火种系统 (FireSeed) 魔鬼代言人智能体 (DevilsAdvocate) — 怀疑论世界观
=========================================================================
继承 WorldViewAgent，以怀疑论哲学为指导，在议会中担任专职挑战者。
它不相信任何单一提案，致力于寻找缺陷、矛盾与历史失败模式。
当挑战成立时，可触发对原提案的否决或置信度惩罚。
"""

import asyncio
import logging
import random
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# 世界观核心模块
from agents.worldview import WorldViewAgent, WorldView, WorldViewManifesto

# 系统基础设施
from api.server import get_engine
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier


logger = logging.getLogger("fire_seed.devils_advocate")


class DevilsAdvocate(WorldViewAgent):
    """
    魔鬼代言人·怀疑论者

    世界观：怀疑论 — 任何真理都可能是临时的，需要不断证伪。
    在议会中的作用：
    - propose：从不提出具体的交易方向，而是根据当前内部状态建议系统“继续观望”或“主动收缩”。
    - challenge：对任意提案进行严苛审查，综合近期相似决策失败率、
      信号过度依赖度、锁相环质量、输入扰动敏感性、历史情景回溯等因素。
    """

    def __init__(self,
                 manifesto: Optional[WorldViewManifesto] = None,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None):
        """
        初始化魔鬼代言人智能体。
        若未提供 manifesto，则自动构建怀疑论世界观宣言。
        """
        if manifesto is None:
            manifesto = WorldViewManifesto(
                worldview=WorldView.SKEPTICISM,
                core_belief="任何决策都蕴含未被发现的致命缺陷",
                primary_optimization_target="F1(correct_veto)",   # 最大化成功证伪次数
                adversary_worldview=WorldView.BAYESIANISM,        # 天然反对过度依赖统计的贝叶斯主义
                forbidden_data_source={"LIVE_PRICE"},              # 禁止接触实时价格，以免被噪声误导
                time_scale="0",                                   # 事件驱动（每当有提案时触发）
            )
        super().__init__(manifesto)

        self.behavior_log = behavior_log
        self.notifier = notifier

        # 挑战历史记录
        self._challenge_records: List[Dict[str, Any]] = []
        # 近期挑战成功率（用于自适应调整挑战强度）
        self._recent_veto_results: List[bool] = []

        logger.info("魔鬼代言人·怀疑论者已就位")

    # ================== 世界观接口实现 ==================
    def propose(self, perception: Dict[str, Any]) -> Dict[str, Any]:
        """
        怀疑论者不主动提出交易方向，而是提出“谨慎”或“撤退”建议。
        返回的提案将被视为对当前任何交易意图的挑战。
        """
        # 永远不提供方向，只提供一种防御性姿态
        return {
            "direction": 0,                     # 中性
            "confidence": 0.0,
            "score": 50.0,
            "description": "怀疑论者主张：所有信号均不可靠，建议观望",
            "recommendation": "hold_or_reduce",
            "reason": "市场存在未建模风险，当前信心不足以开仓",
        }

    def challenge(self, proposal: Dict[str, Any],
                  my_worldview: WorldView) -> Dict[str, Any]:
        """
        对另一个智能体的提案进行严苛的对抗性验证。
        返回挑战结果，可能包含否决建议和置信度调整。
        """
        challenges = []
        evidence = proposal.get("evidence", {})  # 提案附带的证据快照

        # 1. 历史相似情景失败率
        challenges.extend(self._check_recent_failures(proposal))

        # 2. 信号过度依赖
        challenges.extend(self._check_over_reliance(evidence))

        # 3. PLL 锁相质量
        challenges.extend(self._check_pll_quality(evidence))

        # 4. 输入扰动敏感性
        challenges.extend(self._sensitivity_check(evidence))

        # 5. 历史相似情景回溯
        challenges.extend(self._historical_analogy(evidence))

        # 综合判断
        veto = len(challenges) >= 3  # 三个及以上严重缺陷 → 建议否决
        confidence_adjustment = -0.1 * len(challenges)  # 每个缺陷降低 10% 置信度

        result = {
            "veto": veto,
            "challenges": challenges,
            "confidence_adjustment": max(-0.5, confidence_adjustment),  # 最多降低 50%
            "reason": "；".join(challenges) if challenges else "未发现明显缺陷",
        }

        # 记录挑战历史
        self._challenge_records.append({
            "timestamp": datetime.now().isoformat(),
            "veto": veto,
            "challenges": challenges,
        })

        # 记录行为日志
        if self.behavior_log:
            level = EventLevel.WARN if veto else EventLevel.INFO
            self.behavior_log.log(
                EventType.AGENT, "DevilsAdvocate",
                f"挑战结果: {'否决' if veto else '通过'}, "
                f"缺陷数: {len(challenges)}, "
                f"置信调整: {confidence_adjustment:.2f}",
                snapshot={"veto": veto, "challenges": challenges}
            )

        # 若否决，推送告警
        if veto and self.notifier:
            asyncio.ensure_future(
                self.notifier.send_alert(
                    level="WARN",
                    title="魔鬼代言人建议否决",
                    body=f"发现 {len(challenges)} 个潜在缺陷: "
                         + "；".join(challenges[:3])
                )
            )

        # 更新近期成功率（用于内部自适应）
        self._recent_veto_results.append(veto)
        if len(self._recent_veto_results) > 50:
            self._recent_veto_results.pop(0)

        return result

    # ================== 挑战逻辑实现 ==================
    def _check_recent_failures(self, proposal: Dict[str, Any]) -> List[str]:
        """检查近期相似决策的失败率"""
        challenges = []
        try:
            engine = get_engine()
            if engine is None:
                return challenges
            # 从订单管理器获取近期统计
            daily_stats = engine.order_mgr.get_daily_trading_stats()
            win_rate = daily_stats.get("win_rate", 0.0)
            total_trades = daily_stats.get("count", 0)

            if total_trades > 10 and win_rate < 0.35:
                challenges.append(
                    f"近期胜率仅 {win_rate*100:.0f}%（共{total_trades}笔），"
                    "当前决策环境可能不利"
                )

            # 检查连续亏损天数
            if hasattr(engine.risk_monitor, 'consecutive_lossing_days'):
                cons_days = engine.risk_monitor.consecutive_lossing_days
                if cons_days >= 3:
                    challenges.append(f"已连续亏损 {cons_days} 天，策略可能正在失效")
        except Exception as e:
            logger.debug(f"检查近期失败率出错: {e}")

        return challenges

    def _check_over_reliance(self, evidence: Dict[str, Any]) -> List[str]:
        """检查信号是否过度依赖单一因子"""
        challenges = []
        contributions = evidence.get("signal_contributions", {})
        if not contributions:
            return challenges

        max_contrib = max(contributions.values()) if contributions else 0
        if max_contrib > 0.6:
            challenges.append(
                f"过度依赖单一信号（权重 {max_contrib:.1%}），决策脆弱"
            )

        # 检查信号集中度
        if len(contributions) >= 3:
            sorted_vals = sorted(contributions.values(), reverse=True)
            if sorted_vals[0] > 2.0 * sorted_vals[1]:
                challenges.append("信号权重分布极度不均，存在认知盲区")

        return challenges

    def _check_pll_quality(self, evidence: Dict[str, Any]) -> List[str]:
        """检查锁相环是否可靠"""
        challenges = []
        pll_snr = evidence.get("pll_snr_db", 20.0)
        if pll_snr < 6.0:
            challenges.append(f"锁相环信噪比仅 {pll_snr:.1f} dB，方向判断极易出错")
        return challenges

    def _sensitivity_check(self, evidence: Dict[str, Any]) -> List[str]:
        """
        对输入施加微小扰动，测试输出是否剧烈变化。
        若方向因噪声而反转，则说明决策边界不稳定，存在过拟合。
        """
        challenges = []
        scores = evidence.get("raw_scores", {})
        base_direction = evidence.get("direction", 0)

        if not scores or base_direction == 0:
            return challenges

        flip_count = 0
        trials = min(30, max(10, len(scores) * 2))
        for _ in range(trials):
            perturbed = {}
            for k, v in scores.items():
                # ±5% 随机噪声
                noise = np.random.uniform(-0.05, 0.05)
                perturbed[k] = v * (1.0 + noise)

            # 假设评分方向与因子值呈线性关系，计算扰动后的方向
            # 此处为简化模型，实际可调用评分卡模拟
            perturbed_dir = self._estimate_direction(perturbed)
            if perturbed_dir != base_direction:
                flip_count += 1

        sensitivity = flip_count / trials
        if sensitivity > 0.25:
            challenges.append(
                f"决策对输入噪声高度敏感（扰动反转率 {sensitivity:.0%}），可能过拟合"
            )

        return challenges

    def _historical_analogy(self, evidence: Dict[str, Any]) -> List[str]:
        """在历史数据中寻找相似情景，若当时策略失败则提出挑战"""
        # 占位：实际可基于失败基因库进行最近邻搜索
        return []

    @staticmethod
    def _estimate_direction(scores: Dict[str, float]) -> int:
        """基于因子得分大致估算信号方向（简化的评分模型）"""
        # 假设因子得分 > 0 偏多，< 0 偏空
        net = sum(scores.values())
        if net > 0.2:
            return 1
        elif net < -0.2:
            return -1
        return 0

    # ================== 自适应调整与状态查询 ==================
    @property
    def recent_veto_accuracy(self) -> float:
        """近期否决的正确率（需要外部反馈，这里仅返回挑战次数）"""
        if not self._recent_veto_results:
            return 0.0
        return sum(self._recent_veto_results) / len(self._recent_veto_results)

    def get_status(self) -> Dict[str, Any]:
        """返回魔鬼代言人的当前状态摘要"""
        return {
            "worldview": self.manifesto.worldview.value,
            "total_challenges": len(self._challenge_records),
            "recent_veto_rate": self.recent_veto_accuracy,
            "last_challenges": self._challenge_records[-5:],
        }
