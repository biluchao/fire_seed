#!/usr/bin/env python3
"""
火种系统 (FireSeed) 多样性强制者智能体 (DiversityEnforcer)
===============================================================
世界观：多元主义 —— “正确的决策需要多样化的视角，没有单一真理。
        趋同是最大的威胁，宁可混乱不可枯萎。”

核心奖励函数：H(投票分布) —— 最大化议会投票的信息熵。
极端行为：当一致性 > 85% 时主动注入噪声、随机重置低活跃度智能体、
        通过随机扰动打破共识。

在对抗式议会中：
- propose() 不生成确定性交易信号，而是输出“熵增信号”——
  一个随机的方向，附带极低的置信度，其唯一目的是增加投票多样性。
- challenge() 从多元主义角度质疑任何包含高度一致性的提案，
  标记为“共识过载风险”，并建议采纳挑战以降低单一方向权重。
"""

import asyncio
import logging
import random
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np

from agents.worldview import WorldView, WorldViewManifesto, WorldViewAgent
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier

logger = logging.getLogger("fire_seed.diversity_enforcer")


class DiversityEnforcer(WorldViewAgent):
    """
    多样性强制者。

    继承 WorldViewAgent，持有多元主义世界观。
    在议会中作为“专业搅局者”，当共识过载时执行干预。
    """

    def __init__(self,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None,
                 check_interval_sec: int = 300,
                 consensus_threshold: float = 0.85,
                 noise_injection_prob: float = 0.1,
                 reset_prob: float = 0.05):
        # 定义世界观宣言
        manifesto = WorldViewManifesto(
            worldview=WorldView.PLURALISM,
            core_belief="正确的决策需要多样化的视角，没有单一真理。趋同是最大的威胁。",
            primary_optimization_target="H(voting_distribution)",
            adversary_worldview=WorldView.OCCAMS_RAZOR,
            forbidden_data_source={"TRADE_HISTORY", "REAL_TIME_PNL"},
            exclusive_data_source={"VOTING_RECORDS", "AGENT_PERFORMANCE", "DIVERSITY_INDEX"},
            time_scale="5m"
        )
        super().__init__(manifesto)

        self.behavior_log = behavior_log
        self.notifier = notifier
        self.check_interval = check_interval_sec
        self.consensus_threshold = consensus_threshold
        self.noise_prob = noise_injection_prob
        self.reset_prob = reset_prob

        # 干预历史
        self._intervention_history: List[Dict[str, Any]] = []
        self._consecutive_overload = 0

        # 上一次生成“熵增信号”的时间
        self._last_entropy_signal = 0.0

        logger.info("多样性强制者（多元主义）初始化完成")

    # ======================== 世界观接口实现 ========================

    def propose(self, perception: Dict[str, Any]) -> Dict[str, Any]:
        """
        从多元主义世界观提出决策建议。
        我们不做方向性预测，而是生成“熵增信号”——
        一个随机方向，附带极低的置信度，目的是增加投票多样性。
        如果当前共识已高度一致，我们会故意投出反对票来打破共识。
        """
        now = time.time()

        # 检查是否需要故意打破共识
        try:
            from api.server import get_engine
            engine = get_engine()
            if engine and hasattr(engine, 'agent_council'):
                council = engine.agent_council
                vote_history = council._vote_history[-20:]
                if len(vote_history) >= 5:
                    dirs = [d.direction for d in vote_history if d.direction != 0]
                    if dirs:
                        most_common = max(set(dirs), key=dirs.count)
                        agreement = dirs.count(most_common) / len(dirs)
                        if agreement > self.consensus_threshold:
                            # 共识过载！故意投反对票
                            anti_direction = -most_common
                            self._last_entropy_signal = now
                            return {
                                "direction": anti_direction,
                                "confidence": 0.15,  # 极低置信度
                                "reason": "多元主义干预：打破共识过载",
                                "worldview": self.manifesto.worldview.value
                            }
        except Exception as e:
            logger.warning(f"多样性强制者 propose 异常: {e}")

        # 正常情况：随机方向，极低置信度
        if now - self._last_entropy_signal > 120:
            direction = random.choice([-1, 1])
            self._last_entropy_signal = now
            return {
                "direction": direction,
                "confidence": 0.05,
                "reason": "多元主义视角：随机探索以维持多样性",
                "worldview": self.manifesto.worldview.value
            }

        # 未到触发间隔，弃权
        return {
            "direction": 0,
            "confidence": 0.0,
            "reason": "多元主义：当前无需干预",
            "worldview": self.manifesto.worldview.value
        }

    def challenge(self, other_proposal: Dict[str, Any],
                  my_worldview: WorldView) -> Dict[str, Any]:
        """
        从多元主义世界观挑战另一个提案。
        重点关注：
        - 提案是否由高度一致的共识产生
        - 近期投票分布熵是否过低
        - 是否存在“多数人暴政”风险
        """
        challenges = []

        # 检查提案中的一致性指标
        if other_proposal.get("consensus_level", 0) > self.consensus_threshold:
            challenges.append({
                "type": "consensus_overload",
                "severity": "high",
                "detail": f"提案共识度 {other_proposal['consensus_level']:.1%} 超过阈值"
            })

        # 检查近期投票熵
        entropy = self._calculate_recent_voting_entropy()
        if entropy < 1.0:  # 低熵 = 高度一致
            challenges.append({
                "type": "low_entropy",
                "severity": "medium",
                "detail": f"近期投票熵 {entropy:.2f}，亟需增加多样性"
            })

        veto = False
        if challenges:
            # 如果提案共识度过高且我们已有连续干预记录，建议否决
            if self._consecutive_overload >= 3:
                veto = True
                challenges.append({
                    "type": "veto_escalation",
                    "severity": "critical",
                    "detail": f"连续共识过载 {self._consecutive_overload} 次，建议否决"
                })

        return {
            "veto": veto,
            "challenges": challenges,
            "counter_proposal": {
                "direction": -other_proposal.get("direction", 0),
                "confidence": 0.1,
                "reason": "多元主义反证：增加决策空间维度",
                "worldview": self.manifesto.worldview.value
            },
            "worldview": self.manifesto.worldview.value
        }

    # ======================== 主动干预 ========================

    async def evaluate(self) -> Dict[str, Any]:
        """
        执行一次多样性检查与必要干预（供外部定时调用）。
        """
        interventions = []

        # 1. 共识过载检测与干预
        consensus = await self._check_consensus_overload()
        if consensus.get("overloaded"):
            interventions.append(consensus)
            await self._aggressive_intervention()

        # 2. 随机噪声注入
        noise_count = await self._inject_noise_to_agents()
        if noise_count > 0:
            interventions.append({"action": "inject_noise", "count": noise_count})

        # 3. 低活跃智能体参数重置
        reset_count = await self._reset_low_activity_agents()
        if reset_count > 0:
            interventions.append({"action": "reset_agents", "count": reset_count})

        # 4. 计算当前投票熵并检查模型多样性
        entropy = self._calculate_recent_voting_entropy()
        model_check = self._check_model_diversity()

        if self.behavior_log and interventions:
            self.behavior_log.log(
                EventType.AGENT, "DiversityEnforcer",
                f"多样性干预: {len(interventions)} 项，投票熵: {entropy:.2f}",
                snapshot={"interventions": interventions, "entropy": entropy}
            )

        self._intervention_history.extend(interventions)
        if len(self._intervention_history) > 200:
            self._intervention_history = self._intervention_history[-200:]

        return {
            "interventions": interventions,
            "voting_entropy": entropy,
            "consecutive_overload": self._consecutive_overload,
            "timestamp": datetime.now().isoformat()
        }

    # ======================== 共识过载检测 ========================

    async def _check_consensus_overload(self) -> Dict[str, Any]:
        """检测议会投票是否过于一致"""
        try:
            from api.server import get_engine
            engine = get_engine()
            if engine is None or not hasattr(engine, 'agent_council'):
                return {"overloaded": False}

            council = engine.agent_council
            vote_history = council._vote_history[-20:]

            if len(vote_history) < 5:
                return {"overloaded": False}

            dirs = [d.direction for d in vote_history if d.direction != 0]
            if not dirs:
                return {"overloaded": False}

            most_common = max(set(dirs), key=dirs.count)
            agreement = dirs.count(most_common) / len(dirs)
            overloaded = agreement > self.consensus_threshold

            if overloaded:
                self._consecutive_overload += 1
                logger.warning(
                    f"认知多样性预警: 投票一致率 {agreement*100:.1f}%，"
                    f"连续过载 {self._consecutive_overload} 次"
                )
                if self.notifier and self._consecutive_overload % 3 == 0:
                    await self.notifier.send_alert(
                        level="WARN",
                        title="认知多样性预警",
                        body=f"议会投票一致率 {agreement*100:.1f}%，"
                             f"已实施强制干预（第 {self._consecutive_overload} 次）。"
                    )
            else:
                self._consecutive_overload = 0

            return {
                "overloaded": overloaded,
                "agreement": round(agreement, 3),
                "consecutive_overload": self._consecutive_overload,
            }
        except Exception as e:
            logger.error(f"共识过载检测异常: {e}")
            return {"overloaded": False}

    async def _aggressive_intervention(self) -> None:
        """激进干预：随机重置2个智能体的参数"""
        try:
            from api.server import get_engine
            engine = get_engine()
            if not engine or not hasattr(engine, 'agent_council'):
                return

            council = engine.agent_council
            agents = list(council.agents.keys())
            if len(agents) >= 2:
                targets = random.sample(agents, min(2, len(agents)))
                for agent_id in targets:
                    council.agents[agent_id]["performance"].clear()
                    council.agents[agent_id]["weight"] = 0.02
                    logger.info(f"强制干预: 已重置智能体 {agent_id} 的权重")

            # 同时向锁相环注入短暂扰动
            if hasattr(engine, 'perception'):
                engine.perception.freeze(reason="DiversityEnforcer 强制扰动")
                await asyncio.sleep(0.5)
                engine.perception.unfreeze()

        except Exception as e:
            logger.error(f"激进干预失败: {e}")

    # ======================== 噪声注入 ========================

    async def _inject_noise_to_agents(self) -> int:
        """随机向部分智能体的锁相环注入微小扰动"""
        try:
            from api.server import get_engine
            engine = get_engine()
            if not engine or not hasattr(engine, 'perception'):
                return 0

            injected = 0
            for _ in range(3):  # 尝试注入最多3次
                if random.random() < self.noise_prob:
                    pll_state = engine.perception._last_pll
                    original_freq = pll_state.frequency
                    pll_state.frequency *= (1 + random.uniform(-0.05, 0.05))
                    injected += 1
                    logger.debug(f"向锁相环注入噪声: 频率 {original_freq:.4f} → {pll_state.frequency:.4f}")

            if injected > 0:
                logger.info(f"已向智能体感知层注入 {injected} 次随机噪声")
            return injected
        except Exception as e:
            logger.warning(f"噪声注入异常: {e}")
            return 0

    # ======================== 随机重置 ========================

    async def _reset_low_activity_agents(self) -> int:
        """随机重置表现平淡的智能体参数"""
        try:
            from api.server import get_engine
            engine = get_engine()
            if not engine or not hasattr(engine, 'agent_council'):
                return 0

            council = engine.agent_council
            reset_count = 0
            for agent_id, info in council.agents.items():
                if random.random() < self.reset_prob:
                    info["performance"].clear()
                    info["weight"] = max(0.01, random.random() * 0.15)
                    reset_count += 1
                    logger.info(f"已重置智能体 {agent_id} 的累积表现与权重")

            return reset_count
        except Exception as e:
            logger.warning(f"重置智能体异常: {e}")
            return 0

    # ======================== 投票熵计算 ========================

    def _calculate_recent_voting_entropy(self) -> float:
        """计算近期议会投票分布的信息熵"""
        try:
            from api.server import get_engine
            engine = get_engine()
            if not engine or not hasattr(engine, 'agent_council'):
                return 0.0

            council = engine.agent_council
            votes = council._vote_history[-20:]
            if not votes:
                return 0.0

            dirs = [v.direction for v in votes]
            counts = {d: dirs.count(d) for d in set(dirs)}
            total = len(dirs)
            entropy = -sum((c / total) * np.log2(c / total + 1e-10) for c in counts.values())
            return round(entropy, 4)
        except Exception:
            return 0.0

    def _check_model_diversity(self) -> Dict[str, Any]:
        """检查当前智能体模型的多样性是否充足"""
        # 此处可接入实际模型类型统计，目前返回占位结果
        return {"sufficient": True, "detail": "异构模型数量满足要求"}

    # ======================== 查询接口 ========================

    def get_status(self) -> Dict[str, Any]:
        return {
            "worldview": self.manifesto.worldview.value,
            "consensus_threshold": self.consensus_threshold,
            "consecutive_overload": self._consecutive_overload,
            "voting_entropy": self._calculate_recent_voting_entropy(),
            "last_interventions": self._intervention_history[-5:],
        }
