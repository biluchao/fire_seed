#!/usr/bin/env python3
"""
火种系统 (FireSeed) 炼金术士智能体 (Alchemist)
=================================================
负责策略衍生、因子探索、参数自动调优。
- 通过 DeepSeek / OpenAI 等 LLM 生成新因子表达式
- 调用进化工厂将因子编译为可执行代码
- 自动注入幽灵影子验证
- 监控影子表现，将优胜因子提交金丝雀发布
- 周期性搜索参数空间以改进现有策略
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from config.loader import ConfigLoader
from core.behavioral_logger import BehavioralLogger, EventType
from core.notifier import SystemNotifier
from brain.evolution.gene_factory import GeneFactory
from brain.evolution.sandbox_compiler import SandboxCompiler
from brain.evolution.economic_filter import EconomicFilter
from ghost.shadow_manager import ShadowManager
from ghost.shadow_validator_v2 import ShadowValidatorV2
from api.server import get_engine  # 用于获取引擎实例

logger = logging.getLogger("fire_seed.alchemist")


class AlchemistAgent:
    """
    炼金术士智能体。

    两种核心工作模式：
    1. 因子探索模式 (默认)：周期性地向 LLM 请求新因子创意，
       通过进化工厂生成代码，提交影子验证，监控并通过优胜者。
    2. 参数调优模式 (按需)：使用贝叶斯优化或网格搜索寻找现有策略的最优参数。
    """

    def __init__(self,
                 config: ConfigLoader,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None):
        self.config = config
        self.behavior_log = behavior_log
        self.notifier = notifier

        # 进化工厂子模块
        self.gene_factory = GeneFactory(config)
        self.sandbox = SandboxCompiler(config)
        self.economic_filter = EconomicFilter(config)

        # 幽灵验证 (通过引擎引用获取，若不存在则留空)
        self.shadow_mgr: Optional[ShadowManager] = None
        self.shadow_validator = ShadowValidatorV2(config, behavior_log)

        # LLM 配置
        llm_cfg = config.get("llm", {})
        self.llm_provider = llm_cfg.get("provider", "deepseek")
        self.llm_enabled = llm_cfg.get("enabled", False)

        # 调度参数
        self.explore_interval_hours = config.get("alchemist.explore_interval_hours", 12)
        self.max_concurrent_shadows = config.get("alchemist.max_concurrent_shadows", 5)
        self.max_daily_proposals = config.get("alchemist.max_daily_proposals", 10)

        # 内部计数
        self._daily_proposals_sent = 0
        self._last_explore_time = 0.0

        # 提案历史
        self._proposal_history: List[Dict[str, Any]] = []

        logger.info("炼金术士初始化完成")

    # ======================== 主工作入口 ========================
    async def work(self) -> Optional[Dict[str, Any]]:
        """
        执行一次炼金循环：决定是探索新因子还是调优现有策略。
        由每日任务调度器或引擎在指定时段调用。
        """
        # 检查是否需要探索
        now = time.time()
        if self._should_explore(now):
            return await self.explore_factors()
        else:
            # 参数调优
            return await self.tune_parameters()

    # ======================== 因子探索 ========================
    async def explore_factors(self) -> Dict[str, Any]:
        """
        生成新因子创意 → 基因编译 → 经济滤网 → 幽灵影子验证。
        返回本次探索的摘要。
        """
        self._last_explore_time = time.time()

        proposals = []
        # 1. 从 LLM 获取因子创意
        if self.llm_enabled:
            ideas = await self._request_llm_ideas()
            for idea in ideas[:3]:  # 每次最多探索3个
                gene = self.gene_factory.generate_from_text(idea)
                if gene is None:
                    continue
                # 2. 沙箱编译
                compiled, code = self.sandbox.compile(gene)
                if not compiled:
                    self._log("因子编译失败", {"idea": idea})
                    continue

                # 3. 经济滤网
                if not self.economic_filter.validate(code):
                    self._log("因子未通过经济滤网", {"idea": idea})
                    continue

                # 4. 注入影子
                if self.shadow_mgr:
                    shadow_id = await self.shadow_mgr.deploy_shadow_from_code(code, gene.name)
                    proposals.append({
                        "name": gene.name,
                        "shadow_id": shadow_id,
                        "status": "shadow_running",
                        "timestamp": datetime.now().isoformat()
                    })
                    self._daily_proposals_sent += 1
                    self._log(f"新因子进入影子验证: {gene.name}", {"shadow_id": shadow_id})
                else:
                    self._log("影子管理器不可用，跳过部署")

        # 修剪提案历史
        self._proposal_history.extend(proposals)
        self._proposal_history = self._proposal_history[-100:]

        return {
            "proposals_count": len(proposals),
            "status": "completed",
            "timestamp": datetime.now().isoformat()
        }

    # ======================== 参数调优 ========================
    async def tune_parameters(self, strategy_name: str = "trend_1m") -> Dict[str, Any]:
        """
        使用网格搜索或贝叶斯优化调整指定策略的参数。
        在幽灵影子中并行运行多组参数，选择最优者。
        """
        engine = get_engine()
        if engine is None:
            return {"error": "引擎未启动"}

        # 获取当前策略参数
        current_params = self.config.get(f"strategy.{strategy_name}.params", {})
        if not current_params:
            return {"error": f"策略 {strategy_name} 无配置参数"}

        # 生成候选参数组合（简单网格）
        candidates = self._generate_param_grid(current_params)
        shadows_deployed = []

        for candidate in candidates[:self.max_concurrent_shadows]:
            # 创建参数快照策略（通过影子管理器加载）
            if self.shadow_mgr:
                shadow_id = await self.shadow_mgr.deploy_shadow_with_params(
                    strategy_name, candidate
                )
                shadows_deployed.append({
                    "params": candidate,
                    "shadow_id": shadow_id,
                    "status": "shadow_running"
                })

        self._log(f"参数调优: {len(shadows_deployed)} 组候选参数已提交")
        return {
            "tuned_strategy": strategy_name,
            "candidates": len(shadows_deployed),
            "status": "running"
        }

    # ======================== 影子监控与晋升 ========================
    async def monitor_and_promote(self) -> List[Dict[str, Any]]:
        """
        检查所有活跃影子的表现，将满足晋升条件的提交金丝雀。
        通常由每日任务调度器调用。
        """
        if not self.shadow_mgr:
            return []

        promoted = []
        active_shadows = self.shadow_mgr.get_active_shadows()
        for shadow in active_shadows:
            perf = shadow.get("performance", {})
            if self._shadow_ready_for_promotion(shadow):
                # 标记为金丝雀候选
                self._log(f"影子晋升: {shadow['id']}", {"perf": perf})
                promoted.append(shadow["id"])

        return promoted

    # ======================== 内部辅助 ========================
    def _should_explore(self, now: float) -> bool:
        """判断当前是否应执行因子探索（避免过于频繁）"""
        if self._daily_proposals_sent >= self.max_daily_proposals:
            return False
        if now - self._last_explore_time < self.explore_interval_hours * 3600:
            return False
        return True

    async def _request_llm_ideas(self) -> List[str]:
        """
        调用 LLM 请求新的因子创意。
        返回文本描述列表。
        """
        # 构建 Prompt
        prompt = self._build_explore_prompt()
        try:
            # 通过引擎的 LLM 网关发送请求
            engine = get_engine()
            if engine and hasattr(engine, 'llm_gateway'):
                response = await engine.llm_gateway.chat(
                    message=prompt,
                    provider=self.llm_provider
                )
                # 解析 LLM 返回的因子创意（假设以 JSON 格式或行分隔）
                ideas = self._parse_ideas_from_response(response)
                return ideas
        except Exception as e:
            logger.error(f"LLM 请求因子创意失败: {e}", exc_info=True)

        # 回退：使用历史优秀因子变异
        return self._fallback_ideas()

    def _build_explore_prompt(self) -> str:
        """构建因子探索的 Prompt"""
        return """
你是一位量化交易专家。请为加密货币永续合约1分钟级别交易生成3个新的技术因子表达式。
每个因子应满足：
- 使用订单簿不平衡(OBI)、CVD、成交量分布、微观结构数据。
- 表达式应可直接翻译为Python代码。
- 输出格式：每行一个因子，格式为 "因子名: 表达式"
"""

    @staticmethod
    def _parse_ideas_from_response(response: str) -> List[str]:
        """从 LLM 响应中提取因子创意"""
        ideas = []
        for line in response.strip().split('\n'):
            line = line.strip()
            if ':' in line and line[0].isalpha():
                ideas.append(line)
            elif len(line) > 20:
                ideas.append(f"LLM_FACTOR: {line}")
        return ideas[:5]

    def _fallback_ideas(self) -> List[str]:
        """LLM 不可用时的回退因子创意"""
        return [
            "VOLUME_SURGE: (current_volume / avg_volume(20)) * (high - low) / close",
            "SPREAD_PRESSURE: (bid_vol / ask_vol) * (ask_price - bid_price) / bid_price",
            "ORDERBOOK_SLOPE: linear_regression_slope(orderbook_imbalance, 10)"
        ]

    def _generate_param_grid(self, base_params: Dict) -> List[Dict]:
        """在基准参数周围生成网格搜索点"""
        candidates = [base_params.copy()]
        # 对数值型参数施加 ±20% 扰动
        for key, value in base_params.items():
            if isinstance(value, (int, float)):
                for factor in [0.8, 1.0, 1.2]:
                    new_params = base_params.copy()
                    new_params[key] = round(value * factor, 6)
                    if new_params not in candidates:
                        candidates.append(new_params)
        return candidates[:self.max_concurrent_shadows]

    def _shadow_ready_for_promotion(self, shadow: Dict) -> bool:
        """判断影子是否满足晋升条件"""
        perf = shadow.get("performance", {})
        if perf.get("sharpe", 0) < 1.2:
            return False
        if perf.get("max_dd", 100) > 15.0:
            return False
        if shadow.get("uptime_sec", 0) < 86400:  # 至少运行24小时
            return False
        return True

    def _log(self, message: str, snapshot: Optional[Dict] = None) -> None:
        """记录炼金术士的行为日志"""
        if self.behavior_log:
            self.behavior_log.info(EventType.AGENT, "Alchemist", message, snapshot)
        logger.info(f"[Alchemist] {message}")

    # ======================== 对外查询接口 ========================
    def get_status(self) -> Dict[str, Any]:
        """返回炼金术士当前状态"""
        return {
            "daily_proposals_sent": self._daily_proposals_sent,
            "last_explore_time": datetime.fromtimestamp(self._last_explore_time).isoformat() if self._last_explore_time else None,
            "active_shadows": len(self.shadow_mgr.get_active_shadows()) if self.shadow_mgr else 0,
            "proposal_history": self._proposal_history[-5:],
            "llm_enabled": self.llm_enabled,
      }
