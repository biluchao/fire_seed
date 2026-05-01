#!/usr/bin/env python3
"""
火种系统 (FireSeed) 炼金术士智能体 (Alchemist) · 进化论世界观
===============================================================
携带世界观：进化论
核心信仰：市场策略是不断适应环境的生命体，适者生存。
决策哲学：探索优于利用。宁可生成低夏普的新策略，也不固守旧策略。
优化目标：策略夏普比率 × 策略新颖度（与现有策略库的余弦距离）。

数据隔离：
- 允许访问：学术论文摘要、历史K线数据（用于回测，非实时）、因子表达式文本
- 禁止访问：实时行情数据、当前持仓、实时盈亏、订单簿深度

在抗辩式议会中的角色：
- 作为提议者时：基于因子探索或参数演化生成新的交易策略提案。
- 作为挑战者时：质疑对手的策略是否已经“过度适应当前环境”（即过拟合），
  主张市场环境的微小变化就可能导致该策略失效。
"""

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import yaml

from config.loader import ConfigLoader
from core.behavioral_logger import BehavioralLogger, EventType
from core.notifier import SystemNotifier
from brain.evolution.gene_factory import GeneFactory
from brain.evolution.sandbox_compiler import SandboxCompiler
from brain.evolution.economic_filter import EconomicFilter
from ghost.shadow_manager import ShadowManager
from ghost.shadow_validator_v2 import ShadowValidatorV2

from agents.worldview import (
    WorldView, WorldViewManifesto, WorldViewAgent,
)

logger = logging.getLogger("fire_seed.alchemist")


class Alchemist(WorldViewAgent):
    """
    炼金术士 · 进化论。
    它认为策略是生物，需要不断变异、杂交、优胜劣汰。
    它对“新颖性”的渴望超过对“稳健性”的追求，因此常与守护者（存在主义）爆发冲突。
    """

    def __init__(
        self,
        config: ConfigLoader,
        behavior_log: Optional[BehavioralLogger] = None,
        notifier: Optional[SystemNotifier] = None,
    ):
        # 构建进化论世界观宣言
        manifesto = WorldViewManifesto(
            worldview=WorldView.EVOLUTIONISM,
            core_belief="策略是适应环境的生命体，适者生存",
            primary_optimization_target="sharpe * novelty",
            adversary_worldview=WorldView.EXISTENTIALISM,   # 天生对立
            forbidden_data_source={"LIVE_PRICE", "REAL_TIME_PNL", "ORDERBOOK_SNAPSHOT"},
            exclusive_data_source={"HISTORICAL_KLINE", "PAPER_ABSTRACTS", "FACTOR_EXPRESSIONS"},
            time_scale="86400",  # 每日探索
        )
        super().__init__(manifesto)

        self.config = config
        self.behavior_log = behavior_log
        self.notifier = notifier

        # 进化工厂子模块
        self.gene_factory = GeneFactory(config)
        self.sandbox = SandboxCompiler(config)
        self.economic_filter = EconomicFilter(config)

        # 影子验证（通过引擎引用获取）
        self.shadow_mgr: Optional[ShadowManager] = None
        self.shadow_validator = ShadowValidatorV2(config, behavior_log)

        # LLM 配置
        llm_cfg = config.get("llm", {})
        self.llm_provider = llm_cfg.get("provider", "deepseek")
        self.llm_enabled = llm_cfg.get("enabled", False)

        # 内部状态
        self._last_explore_time = 0.0
        self._proposal_history: List[Dict] = []

        logger.info("炼金术士（进化论）初始化完成")

    # ==================== 核心接口实现 ====================
    def propose(self, perception: Dict) -> Dict:
        """
        基于进化论世界观提出交易策略建议。
        perception 仅包含历史数据、论文摘要等本世界观允许的数据。
        """
        # 从感知字典中提取允许的数据（实际应通过 DataSourceRegistry 校验）
        historical_klines = perception.get("historical_klines", [])
        paper_abstracts = perception.get("paper_abstracts", [])
        current_strategies = perception.get("current_strategies", [])

        # 生成新因子创意
        ideas = self._generate_factor_ideas(paper_abstracts, historical_klines)

        if not ideas:
            # 没有新想法时，对现有策略进行微变异（参数调优）
            return self._create_param_mutation_proposal(current_strategies)

        # 选择第一个通过经济滤网的因子，构建提案
        for idea in ideas[:3]:
            gene = self._idea_to_gene(idea)
            if gene is None:
                continue

            # 沙箱编译
            compiled, code = self.sandbox.compile(gene)
            if not compiled:
                continue

            # 经济滤网
            if not self.economic_filter.validate(code):
                continue

            # 构建提案：建议将新因子部署至影子验证
            proposal = {
                "direction": 0,                # 因子本身不产生交易方向，方向由组合决定
                "confidence": 0.6,             # 进化论者对新因子天生乐观
                "score": 50.0,                # 中性
                "action": "DEPLOY_SHADOW",
                "factor_name": gene.name,
                "factor_code": code,
                "novelty_score": self._calc_novelty(gene, current_strategies),
                "rationale": f"新因子 '{gene.name}' 可增加策略多样性，建议立即进入影子验证。",
                "worldview": self.manifesto.worldview.value,
            }
            self._log_proposal(proposal)
            return proposal

        # 无可用因子时返回中性
        return {
            "direction": 0,
            "confidence": 0.0,
            "action": "HOLD",
            "rationale": "当前未发现值得探索的新因子。",
        }

    def challenge(self, other_proposal: Dict, my_worldview: WorldView) -> Dict:
        """
        从进化论角度挑战另一个智能体的提案。
        主要攻击点：
        1. 提案是否过度依赖当前市场环境（过拟合风险）
        2. 提案是否缺乏新颖性（与现有策略高度相似）
        3. 提案是否忽略了市场可能的快速演化
        """
        challenges = []

        # 检查提案的“适应度狭窄度”：如果提案基于的因子在过去三个月内只在一个市场状态下有效，
        # 那么进化论者认为它无法适应多变环境。
        factor_name = other_proposal.get("factor_name", "")
        if factor_name:
            historical_robustness = self._check_cross_regime_robustness(factor_name)
            if not historical_robustness.get("robust", True):
                challenges.append(
                    f"因子 '{factor_name}' 在不同市场状态下表现不一致，"
                    "可能只是当前环境的临时适应者。"
                )

        # 检查新颖度：若与现有策略余弦相似度 > 0.9，视为近亲繁殖
        novelty = other_proposal.get("novelty_score", 1.0)
        if novelty < 0.1:
            challenges.append(
                "该提案与现有策略高度雷同，缺乏进化价值。"
            )

        # 检查时间衰减：如果一个策略长时间未更新，进化论者认为它可能已老化
        last_mutation = other_proposal.get("last_mutation_time", 0)
        if time.time() - last_mutation > 7 * 86400:
            challenges.append(
                f"现有策略已超过7天未变异，可能无法适应近期市场变化。"
            )

        veto_recommended = len(challenges) >= 2
        return {
            "veto": veto_recommended,
            "challenges": challenges,
            "worldview": self.manifesto.worldview.value,
            "alternative": "建议立即进行因子变异或引入外来基因。",
        }

    # ==================== 内部方法 ====================
    def _generate_factor_ideas(
        self, paper_abstracts: List[str], historical_klines: List
    ) -> List[str]:
        """
        从学术论文和LLM中获取新因子创意。
        注意：严禁使用实时行情数据。
        """
        ideas = []
        # 1. 从论文摘要中提取关键词启发
        if paper_abstracts:
            for abstract in paper_abstracts[:3]:
                # 简单启发：提取“波动率”、“流动性”、“动量”等关键词生成候选表达式
                if "volatility" in abstract.lower():
                    ideas.append("VOL_PREDICT: rolling_stddev(close, 20) / sma(volume, 20)")
                if "liquidity" in abstract.lower():
                    ideas.append("LIQ_DEPTH: (bid_vol - ask_vol) / (bid_vol + ask_vol) * spread")
                if "momentum" in abstract.lower():
                    ideas.append("CROSS_MOMENTUM: correlation(close, ma(close,5), 10)")

        # 2. 如果LLM可用，请求更多创意（但必须使用历史数据作为上下文）
        if self.llm_enabled and len(ideas) < 3:
            try:
                # 假设引擎提供了异步LLM接口
                from api.server import get_engine
                engine = get_engine()
                if engine and hasattr(engine, "llm_gateway"):
                    prompt = self._build_llm_prompt(historical_klines)
                    response = engine.llm_gateway.chat_sync(prompt)  # 同步调用
                    llm_ideas = self._parse_llm_response(response)
                    ideas.extend(llm_ideas)
            except Exception as e:
                logger.error(f"LLM因子生成失败: {e}")

        # 3. 回退：从历史优秀因子库中随机变异
        if not ideas:
            ideas = self._fallback_ideas()

        return ideas

    def _idea_to_gene(self, idea: str) -> Optional[Any]:
        """将因子创意文本转换为基因对象"""
        try:
            # 通过 GeneFactory 将文本解析为基因
            return self.gene_factory.generate_from_text(idea)
        except Exception as e:
            logger.warning(f"因子创意解析失败: {e}")
            return None

    def _calc_novelty(self, gene, current_strategies: List) -> float:
        """计算因子的新颖度（与现有策略库的1-余弦相似度）"""
        if not current_strategies:
            return 1.0
        # 简单实现：基于因子名称和表达式的编辑距离估算
        # 实际应基于因子值的相关性矩阵
        name_set = {s.get("name", "") for s in current_strategies}
        if gene.name in name_set:
            return 0.0
        # 粗糙估计：陌生因子得0.5，后续可通过幽灵验证结果动态调整
        return 0.5

    def _check_cross_regime_robustness(self, factor_name: str) -> Dict:
        """检查因子在不同市场状态下的稳健性（占位）"""
        # 实际应从因子库查询该因子在各状态下的IC
        # 这里返回占位值
        return {"robust": True, "regime_ics": {"trend": 0.03, "oscillation": -0.01}}

    def _create_param_mutation_proposal(self, current_strategies: List) -> Dict:
        """对现有策略进行参数微变异"""
        if not current_strategies:
            return {"direction": 0, "confidence": 0.0, "action": "HOLD"}
        target = current_strategies[0]
        # 随机扰动参数
        mutated_params = target.get("params", {}).copy()
        for key in mutated_params:
            mutated_params[key] *= 1 + np.random.uniform(-0.1, 0.1)
        return {
            "direction": 0,
            "confidence": 0.5,
            "action": "MUTATE_PARAMS",
            "strategy_name": target.get("name"),
            "new_params": mutated_params,
            "rationale": "对现有策略进行微小变异以测试环境适应性。",
        }

    def _build_llm_prompt(self, historical_klines: List) -> str:
        """基于历史K线构建LLM请求（禁止包含实时价格）"""
        return """
基于提供的加密货币历史K线数据（最近30日），生成一个全新的1分钟级别技术因子表达式。
因子应使用成交量、价格波动、订单簿不平衡等微观结构信息，但不可直接引用当前价格。
输出格式：一行 'FACTOR_NAME: expression'
"""

    @staticmethod
    def _parse_llm_response(response: str) -> List[str]:
        ideas = []
        for line in response.strip().split("\n"):
            if ":" in line and line.strip()[:3].isalpha():
                ideas.append(line.strip())
        return ideas

    def _fallback_ideas(self) -> List[str]:
        return [
            "VOL_SURGE_V2: (volume / ma(volume,20)) * (high - low) / close",
            "SPREAD_PRESSURE_V2: (bid_vol / ask_vol) * (ask - bid) / mid",
        ]

    def _log_proposal(self, proposal: Dict) -> None:
        """记录提案到行为日志"""
        if self.behavior_log:
            self.behavior_log.log(
                EventType.AGENT, "Alchemist",
                f"进化论提案: {proposal.get('action')} - {proposal.get('rationale')}",
            )

    # ======================== 对外状态接口 ========================
    def get_status(self) -> Dict:
        return {
            "worldview": self.manifesto.worldview.value,
            "proposal_count": len(self._proposal_history),
            }
