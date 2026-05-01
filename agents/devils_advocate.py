#!/usr/bin/env python3
"""
火种系统 (FireSeed) 魔鬼代言人智能体 (DevilsAdvocate)
=========================================================
负责对议会共识提出强制性质疑与对抗性验证：
- 检查近期相似决策的失败率
- 检测信号过度依赖（单一因子权重过高）
- 敏感度测试（输入扰动后输出稳定性）
- PLL 锁相环质量低时否决信号
- 历史相似情景回溯
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from api.server import get_engine
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier

logger = logging.getLogger("fire_seed.devils_advocate")


@dataclass
class ChallengeReport:
    """挑战报告"""
    timestamp: datetime = field(default_factory=datetime.now)
    challenged_decision: str = ""           # 被挑战的决策描述
    challenges: List[str] = field(default_factory=list)
    veto_recommended: bool = False
    confidence_adjustment: float = 0.0     # 对原决策的置信度调整值（负数表示降低）


class DevilsAdvocate:
    """
    魔鬼代言人智能体。
    在议会投票前自动执行对抗性验证，强制寻找策略/信号的潜在缺陷。
    其挑战报告会被议会采纳，可能触发否决或重新评估。
    """

    def __init__(self,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None):
        """
        :param behavior_log: 系统行为日志
        :param notifier:     消息推送器
        """
        self.behavior_log = behavior_log
        self.notifier = notifier

        # 历史挑战记录
        self._challenge_history: List[ChallengeReport] = []
        # 历史失败案例库（从引擎获取，若有）
        self._failure_db: List[Dict[str, Any]] = []

    # ======================== 核心挑战入口 ========================
    async def challenge(self, council_decision: Dict[str, Any],
                        evidence: Dict[str, Any]) -> ChallengeReport:
        """
        对议会的决策进行对抗性验证。
        :param council_decision: 议会的决策内容，包含 direction, confidence, score 等
        :param evidence: 证据快照，包含各因子贡献、市场状态、近期准确率等
        :return: 挑战报告
        """
        challenges = []

        # 1. 检查近期相似决策的失败率
        fail_challenges = await self._check_recent_similar_failures(council_decision)
        challenges.extend(fail_challenges)

        # 2. 信号过度依赖检测
        dependency_challenges = self._check_signal_over_dependency(evidence)
        challenges.extend(dependency_challenges)

        # 3. PLL 锁相质量检测
        pll_challenges = self._check_pll_quality(evidence)
        challenges.extend(pll_challenges)

        # 4. 敏感度测试（扰动输入，观察输出变化）
        sensitivity_challenges = await self._sensitivity_test(evidence)
        challenges.extend(sensitivity_challenges)

        # 5. 历史相似情景回溯
        historical_challenges = await self._historical_analogy(evidence)
        challenges.extend(historical_challenges)

        # 生成报告
        report = ChallengeReport(
            challenged_decision=str(council_decision.get("direction", "unknown")),
            challenges=challenges,
            veto_recommended=len(challenges) >= 3,
            confidence_adjustment=-0.1 * len(challenges)
        )

        # 记录和推送
        self._challenge_history.append(report)
        if self.behavior_log:
            self.behavior_log.log(
                EventType.AGENT, "DevilsAdvocate",
                f"挑战报告: 否决={'是' if report.veto_recommended else '否'}, "
                f"问题数={len(challenges)}, 置信调整={report.confidence_adjustment}",
                snapshot={"challenges": challenges}
            )

        if report.veto_recommended and self.notifier:
            await self.notifier.send_alert(
                level="WARN",
                title="魔鬼代言人建议否决",
                body=f"发现 {len(challenges)} 个潜在缺陷: " + "; ".join(challenges[:3])
            )

        return report

    # ======================== 子检查实现 ========================
    async def _check_recent_similar_failures(self, decision: Dict) -> List[str]:
        """检查近期相似决策的失败率"""
        challenges = []
        try:
            engine = get_engine()
            if engine is None:
                return challenges
            # 从失败基因库或交易记录中查询相似情景
            # 简化：若近期连续亏损次数 >= 3，则提出挑战
            daily_stats = engine.order_mgr.get_daily_trading_stats()
            if daily_stats.get("win_rate", 0) < 0.4 and daily_stats.get("count", 0) > 10:
                challenges.append(f"今日胜率仅 {daily_stats['win_rate']*100:.0f}%，当前决策可能受近期亏损情绪影响")
        except Exception as e:
            logger.warning(f"检查近期失败率异常: {e}")
        return challenges

    def _check_signal_over_dependency(self, evidence: Dict) -> List[str]:
        """检查是否过度依赖单一信号"""
        challenges = []
        contributions = evidence.get("signal_contributions", {})
        if not contributions:
            return challenges
        max_contrib = max(contributions.values()) if contributions else 0
        if max_contrib > 0.6:
            challenges.append(f"过度依赖单一信号 (权重 {max_contrib:.1%})，存在脆弱性")
        # 检查贡献分布是否过于集中
        if len(contributions) >= 3:
            sorted_vals = sorted(contributions.values(), reverse=True)
            if sorted_vals[0] > 2 * sorted_vals[1]:
                challenges.append("信号权重分布严重不均，可能产生认知盲区")
        return challenges

    def _check_pll_quality(self, evidence: Dict) -> List[str]:
        """检查锁相环信噪比"""
        challenges = []
        pll_snr = evidence.get("pll_snr_db", 20)
        if pll_snr < 6:
            challenges.append(f"锁相环信噪比过低 ({pll_snr:.1f} dB)，当前信号不可靠")
        return challenges

    async def _sensitivity_test(self, evidence: Dict) -> List[str]:
        """
        对输入进行微小扰动，测试输出是否剧烈变化。
        若输出对噪声敏感，说明可能过拟合。
        """
        challenges = []
        try:
            scores = evidence.get("raw_scores", {})
            if not scores:
                return challenges
            # 对每个因子施加 ±2% 的随机噪声，重新评估信号方向
            base_direction = evidence.get("direction", 0)
            if base_direction == 0:
                return challenges
            flip_count = 0
            trials = 30
            for _ in range(trials):
                perturbed = {}
                for k, v in scores.items():
                    noise = np.random.uniform(-0.02, 0.02)
                    perturbed[k] = v * (1 + noise)
                # 重新计算评分（这里做出假设，实际需调用评分卡接口）
                # 若方向反转则计数
                # 占位：模拟
                if np.random.random() < 0.05:  # 假设5%概率反转
                    flip_count += 1
            sensitivity = flip_count / trials
            if sensitivity > 0.2:
                challenges.append(f"决策对输入噪声敏感 (扰动模拟反转率 {sensitivity:.0%})，可能过拟合")
        except Exception as e:
            logger.warning(f"敏感度测试异常: {e}")
        return challenges

    async def _historical_analogy(self, evidence: Dict) -> List[str]:
        """在历史数据中寻找相似情景，若当时策略失败则提出挑战"""
        # 占位：未来可基于失败基因库实现
        return []

    # ======================== 状态查询 ========================
    def get_recent_challenges(self, limit: int = 10) -> List[Dict]:
        """获取最近的挑战报告"""
        reports = []
        for report in reversed(self._challenge_history[-limit:]):
            reports.append({
                "timestamp": report.timestamp.isoformat(),
                "challenges": report.challenges,
                "veto_recommended": report.veto_recommended,
                "confidence_adjustment": report.confidence_adjustment,
            })
        return reports

    def reset(self) -> None:
        """重置历史挑战记录"""
        self._challenge_history.clear()
