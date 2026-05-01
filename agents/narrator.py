#!/usr/bin/env python3
"""
火种系统 (FireSeed) 叙事官智能体 (Narrator)
===============================================
世界观：诠释学 (Hermeneutics)
核心理念：意义存在于叙述之中，真相是故事的函数。

每日自动生成并向消息渠道推送结构化的议会日报，同时：
- 在议会审议时，基于自身世界观解读提案的“叙事合理性”
- 对不符合连贯叙事的提案提出温和质疑（挑战）
- 为决策增添可解释性，使人类运维者能直观理解系统状态
- 审核其他智能体输出的“可读性”，推动信息透明化
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import psutil

from agents.worldview import (
    WorldViewAgent,
    WorldViewManifesto,
    WorldView,
)
from api.server import get_engine
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier

logger = logging.getLogger("fire_seed.narrator")


class NarratorAgent(WorldViewAgent):
    """
    叙事官智能体（诠释学）。

    在对抗式议会中，叙事官的角色类似“记录官+温和质疑者”：
    - 它不会提出激进的买卖提案，而是为其他智能体的提案增添“故事背景”
    - 挑战时，它不攻击逻辑，而是攻击“叙事的不一致性”（如前后矛盾、与历史故事冲突）
    - 负责每日生成议会日报，将所有决策编织为连贯的故事线
    """

    def __init__(
        self,
        behavior_log: Optional[BehavioralLogger] = None,
        notifier: Optional[SystemNotifier] = None,
    ):
        # 定义诠释学宣言
        manifesto = WorldViewManifesto(
            worldview=WorldView.HERMENEUTICS,
            core_belief="意义存在于叙述之中，真相是故事的函数",
            primary_optimization_target="human_read_completion_rate",
            adversary_worldview=WorldView.MECHANICAL_MATERIALISM,  # 与纯机械分解对立
            forbidden_data_source={"RAW_ORDER_FLOW"},  # 叙事官不需要原始订单流
            exclusive_data_source={"COUNCIL_VOTES", "SYSTEM_LOGS", "PERFORMANCE_HISTORY"},
            time_scale="1d",
        )
        super().__init__(manifesto)

        self.behavior_log = behavior_log
        self.notifier = notifier

        # 日报生成历史（内存）
        self._report_history: List[Dict[str, Any]] = []

        # 故事线记忆（用于检测叙事不一致）
        self._story_memory: List[Dict[str, Any]] = []

    # ======================== 世界观接口实现 ========================
    def propose(self, perception: Dict[str, Any]) -> Dict[str, Any]:
        """
        叙事官不提出具体的买卖方向，而是基于当前感知生成“叙事提案”。
        提案内容为：当前市场故事的主题、市场情绪、以及建议的谨慎程度。
        """
        # 收集当前状态
        system_state = self._collect_system_state_sync()
        strategy_state = self._collect_strategy_state_sync()
        council_state = self._collect_council_state_sync()
        risk_state = self._collect_risk_state_sync()

        # 构建叙事主题
        narrative = self._compose_narrative(
            system_state, strategy_state, council_state, risk_state
        )

        # 叙事官提案为中性方向（其“提案”本质上是关于叙事的建议，而非买卖）
        return {
            "direction": 0,  # 中性
            "confidence": 0.0,
            "proposal_type": "narrative",
            "content": narrative,
            "readability_score": self._estimate_readability(narrative),
            "timestamp": datetime.now().isoformat(),
        }

    def challenge(self, other_proposal: Dict[str, Any], my_worldview: WorldView) -> Dict[str, Any]:
        """
        叙事官的挑战方式：检测提案背后的“叙事是否一贯”。
        如果发现提案与近期系统行为或历史故事有明显矛盾，则提出质疑。
        """
        challenges = []
        content = other_proposal.get("content", "")

        # 1. 检查是否与历史故事线矛盾
        if self._story_memory:
            last_story = self._story_memory[-1].get("theme", "")
            current_theme = other_proposal.get("market_theme", "")
            if last_story and current_theme and last_story != current_theme:
                # 主题突变，需要解释
                challenges.append(
                    f"叙事断裂：上次主题为「{last_story}」，但此次提案暗示「{current_theme}」，"
                    f"需提供衔接性解释。"
                )

        # 2. 检查提案的可读性
        if isinstance(content, str) and len(content) > 1000:
            challenges.append("提案叙述过于冗长，可能降低人类理解度。")

        # 3. 温和质疑：要求补充背景故事
        challenges.append("建议为提案附加一个简短的「故事摘要」，说明该决策在市场时间线中的位置。")

        veto = len(challenges) >= 2  # 叙事官的挑战一般不直接否决，除非严重断裂

        return {
            "veto": veto,
            "challenges": challenges,
            "confidence_adjustment": -0.05 * len(challenges),
            "worldview_note": "诠释学视⻆：关注叙事的连贯性与可读性。",
        }

    # ======================== 日报生成主接口 ========================
    async def generate_daily_report(self) -> str:
        """
        生成并推送每日议会日报（异步版本）。
        返回生成的Markdown文本。
        """
        try:
            system_state = await self._collect_system_state()
            strategy_state = await self._collect_strategy_state()
            council_state = await self._collect_council_state()
            evolution_state = await self._collect_evolution_state()
            risk_state = await self._collect_risk_state()
            environment_state = await self._collect_environment_state()
            recent_events = await self._collect_recent_events()

            report_md = self._compose_report(
                system_state, strategy_state, council_state,
                evolution_state, risk_state, environment_state, recent_events,
            )

            # 将日报内容也作为故事线存证
            self._story_memory.append({
                "timestamp": datetime.now().isoformat(),
                "theme": self._extract_theme(report_md),
                "report": report_md,
            })
            if len(self._story_memory) > 50:
                self._story_memory.pop(0)

            self._report_history.append({
                "timestamp": datetime.now().isoformat(),
                "report": report_md,
            })
            if len(self._report_history) > 30:
                self._report_history.pop(0)

            if self.notifier:
                await self.notifier.send_daily_report(report_md)

            if self.behavior_log:
                self.behavior_log.info(
                    EventType.AGENT, "Narrator",
                    "日报已生成并推送（诠释学视⻆）",
                )

            return report_md

        except Exception as e:
            logger.error(f"生成日报失败: {e}", exc_info=True)
            error_report = f"### 火种议会日报\n\n> ⚠️ 日报生成失败: {str(e)}\n"
            if self.notifier:
                await self.notifier.send_message("日报异常", error_report, level="ERROR")
            return error_report

    # ======================== 数据收集（异步） ========================
    async def _collect_system_state(self) -> Dict[str, Any]:
        state = {"health_score": "N/A", "mode": "unknown", "version": "unknown"}
        try:
            engine = get_engine()
            if engine:
                state["mode"] = getattr(engine, "mode", "N/A")
                state["version"] = getattr(engine, "current_version", self._get_version())
                if hasattr(engine, "self_check"):
                    report = engine.self_check.run()
                    state["health_score"] = report.score
        except Exception:
            pass
        return state

    async def _collect_strategy_state(self) -> Dict[str, Any]:
        state = {"daily_pnl": 0.0, "win_rate": 0.0, "total_trades": 0}
        try:
            engine = get_engine()
            if engine and hasattr(engine, "order_mgr"):
                stats = engine.order_mgr.get_daily_trading_stats()
                state["daily_pnl"] = stats.get("realized_pnl", 0.0)
                state["win_rate"] = stats.get("win_rate", 0.0) * 100
                state["total_trades"] = stats.get("count", 0)
                if hasattr(engine, "risk_monitor"):
                    snap = await engine.risk_monitor.get_snapshot()
                    state["current_drawdown"] = snap.drawdown_pct
        except Exception:
            pass
        return state

    async def _collect_council_state(self) -> Dict[str, Any]:
        state = {"dominant_agent": "N/A", "last_decision": "无", "abstain_rate": 0.0}
        try:
            engine = get_engine()
            if engine and hasattr(engine, "agent_council"):
                council = engine.agent_council
                last = council.get_last_decision()
                state["last_decision"] = last.description if last else "无"
                state["abstain_rate"] = council.get_abstain_rate() * 100
                weights = council._get_current_weights()
                if weights:
                    dominant = max(weights, key=weights.get)
                    state["dominant_agent"] = dominant
        except Exception:
            pass
        return state

    async def _collect_evolution_state(self) -> Dict[str, Any]:
        return {"new_factors": 0, "shadow_count": 0}

    async def _collect_risk_state(self) -> Dict[str, Any]:
        state = {"cb_level": 0, "var_99": 0.0}
        try:
            engine = get_engine()
            if engine and hasattr(engine, "risk_monitor"):
                risk = engine.risk_monitor
                cb = risk.circuit_breaker
                state["cb_level"] = cb.level
                snap = await risk.get_snapshot()
                state["var_99"] = snap.var_99
                state["current_drawdown"] = snap.drawdown_pct
        except Exception:
            pass
        return state

    async def _collect_environment_state(self) -> Dict[str, Any]:
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return {
            "cpu_pct": cpu,
            "mem_pct": mem.percent,
            "disk_pct": disk.percent,
        }

    async def _collect_recent_events(self) -> List[str]:
        events: List[str] = []
        try:
            if self.behavior_log:
                entries = self.behavior_log.get_recent(10, level="WARN")
                for e in entries:
                    events.append(f"[{e.ts_str}] {e.content}")
        except Exception:
            pass
        return events

    # ======================== 同步备用（供同步世界观接口使用） ========================
    def _collect_system_state_sync(self) -> Dict[str, Any]:
        state = {"health_score": "N/A", "mode": "unknown", "version": "unknown"}
        try:
            engine = get_engine()
            if engine:
                state["mode"] = getattr(engine, "mode", "N/A")
                state["version"] = getattr(engine, "current_version", self._get_version())
                if hasattr(engine, "self_check"):
                    report = engine.self_check.run()
                    state["health_score"] = report.score
        except Exception:
            pass
        return state

    def _collect_strategy_state_sync(self) -> Dict[str, Any]:
        return {"daily_pnl": 0.0, "win_rate": 0.0, "total_trades": 0}

    def _collect_council_state_sync(self) -> Dict[str, Any]:
        return {"dominant_agent": "N/A", "last_decision": "无", "abstain_rate": 0.0}

    def _collect_risk_state_sync(self) -> Dict[str, Any]:
        return {"cb_level": 0, "var_99": 0.0}

    # ======================== 报告排版 ========================
    def _compose_narrative(self, system, strategy, council, risk) -> str:
        """构建当前市场叙事文本"""
        mood = "积极"
        if strategy.get("current_drawdown", 0) > 5 or risk.get("cb_level", 0) > 0:
            mood = "谨慎"
        return (
            f"当前市场叙事：火种处于{mood}模式。"
            f"主导角色{council.get('dominant_agent', '未知')}认为……"
        )

    def _compose_report(self, system, strategy, council, evolution, risk, env, events) -> str:
        now = datetime.now()
        md = (
            f"## 火种议会日报\n"
            f"📅 {now.strftime('%Y年%m月%d日')}\n\n"
            f"### 🏥 系统健康\n"
            f"- 健康评分: {system.get('health_score', 'N/A')}\n"
            f"- 运行模式: {system.get('mode', 'N/A')}\n"
            f"- 版本: {system.get('version', 'N/A')}\n\n"
            f"### 💰 策略绩效\n"
            f"- 今日盈亏: ${strategy.get('daily_pnl', 0):,.2f}\n"
            f"- 胜率: {strategy.get('win_rate', 0):.1f}%\n"
            f"- 交易笔数: {strategy.get('total_trades', 0)}\n"
            f"- 当前回撤: {strategy.get('current_drawdown', 0):.1f}%\n\n"
            f"### 🏛️ 议会动态\n"
            f"- 主导角色: {council.get('dominant_agent', 'N/A')}\n"
            f"- 最近决策: {council.get('last_decision', '无')}\n"
            f"- 弃权率: {council.get('abstain_rate', 0):.1f}%\n\n"
            f"### 🧬 进化工厂\n"
            f"- 新因子: {evolution.get('new_factors', 0)} 个\n"
            f"- 影子候选: {evolution.get('shadow_count', 0)} 个\n\n"
            f"### 🛡️ 风险控制\n"
            f"- 熔断级别: {risk.get('cb_level', 0)} (0=正常)\n"
            f"- VaR(99): ${risk.get('var_99', 0):,.0f}\n"
            f"- 当前回撤: {risk.get('current_drawdown', 0):.1f}%\n\n"
            f"### 🌡️ 环境资源\n"
            f"- CPU: {env.get('cpu_pct', 0):.0f}%\n"
            f"- 内存: {env.get('mem_pct', 0):.0f}%\n"
            f"- 磁盘: {env.get('disk_pct', 0):.0f}%\n\n"
        )
        if events:
            md += f"### 📋 重要事件\n"
            for ev in events[:5]:
                md += f"- {ev}\n"
        md += "\n> ⚖️ 本日报由诠释学叙事官生成 —— 相信数字背后的故事。\n"
        return md

    # ======================== 辅助函数 ========================
    def _estimate_readability(self, text: str) -> float:
        """估计文本的可读性（0-1，越高越好）"""
        if not text:
            return 0.0
        sentences = [s for s in text.replace("!", ".").replace("?", ".").split(".") if s.strip()]
        if not sentences:
            return 0.5
        avg_words = sum(len(s.split()) for s in sentences) / len(sentences)
        # 简单的可读性模型：15-20词为理想
        score = 1.0 - abs(avg_words - 17.5) / 17.5
        return max(0.0, min(1.0, score))

    def _extract_theme(self, report: str) -> str:
        """从日报中提取主题词"""
        if "熔断" in report:
            return "风险控制"
        if "议会" in report:
            return "智能体协作"
        return "正常交易"

    def _get_version(self) -> str:
        try:
            with open("config/version.txt", "r") as f:
                return f.read().strip()
        except Exception:
            return "unknown"

    # ======================== 查询接口 ========================
    def get_recent_reports(self, limit: int = 7) -> List[Dict]:
        return self._report_history[-limit:]

    def get_status(self) -> Dict[str, Any]:
        return {
            "worldview": self.manifesto.worldview.value,
            "reports_generated": len(self._report_history),
            "story_memory_size": len(self._story_memory),
        }
