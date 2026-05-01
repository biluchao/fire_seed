#!/usr/bin/env python3
"""
火种系统 (FireSeed) 叙事官智能体 (Narrator)
===============================================
每日自动生成并向消息渠道推送结构化的议会日报。
日报内容涵盖：
- 系统运行状态与健康评分
- 策略绩效（当日盈亏、胜率、夏普）
- 智能体议会动态（主导角色、投票结果、近期决策）
- 进化工厂产出（新因子、影子验证进度）
- 风险控制状态（熔断级别、回撤、VaR）
- 环境健康（CPU、内存、磁盘）
- 行为摘要（最后几笔交易、重要事件）

日报格式为Markdown，支持直接发送至Telegram/钉钉。
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from api.server import get_engine
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier

logger = logging.getLogger("fire_seed.narrator")


class NarratorAgent:
    """
    叙事官智能体。
    以每日定时任务方式运行（通常在每日任务调度中触发），
    收集系统各部分状态，生成人类可读的日报并通过消息渠道推送。
    """

    def __init__(self,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None):
        """
        :param behavior_log: 全系统行为日志实例
        :param notifier:     消息推送器（用于将日报发送至外部渠道）
        """
        self.behavior_log = behavior_log
        self.notifier = notifier

        # 日报生成历史（内存）
        self._report_history: List[Dict[str, Any]] = []

    # ======================== 日报生成主接口 ========================
    async def generate_daily_report(self) -> str:
        """
        生成并推送每日议会日报。
        返回生成的Markdown文本。
        """
        try:
            # 收集各部分数据
            system_state = await self._collect_system_state()
            strategy_state = await self._collect_strategy_state()
            council_state = await self._collect_council_state()
            evolution_state = await self._collect_evolution_state()
            risk_state = await self._collect_risk_state()
            environment_state = await self._collect_environment_state()
            recent_events = await self._collect_recent_events()

            # 组合日报
            report_md = self._compose_report(
                system_state, strategy_state, council_state,
                evolution_state, risk_state, environment_state, recent_events
            )

            # 存历史
            self._report_history.append({
                "timestamp": datetime.now().isoformat(),
                "report": report_md,
            })
            if len(self._report_history) > 30:
                self._report_history.pop(0)

            # 推送
            if self.notifier:
                await self.notifier.send_daily_report(report_md)

            # 记录行为日志
            if self.behavior_log:
                self.behavior_log.info(
                    EventType.AGENT, "Narrator",
                    "日报已生成并推送"
                )

            return report_md

        except Exception as e:
            logger.error(f"生成日报失败: {e}", exc_info=True)
            # 尝试生成简化错误报告
            error_report = f"### 火种议会日报\n\n> ⚠️ 日报生成失败: {str(e)}\n"
            if self.notifier:
                await self.notifier.send_message("日报异常", error_report, level="ERROR")
            return error_report

    # ======================== 数据收集 ========================
    async def _collect_system_state(self) -> Dict[str, Any]:
        """收集系统基础状态"""
        state = {"health_score": "N/A", "mode": "unknown", "version": "unknown"}
        try:
            engine = get_engine()
            if engine:
                state["mode"] = getattr(engine, 'mode', 'N/A')
                state["version"] = getattr(engine, 'current_version', self._get_version())
                # 自检健康评分
                if hasattr(engine, 'self_check'):
                    report = engine.self_check.run()
                    state["health_score"] = report.score
        except Exception:
            pass
        return state

    async def _collect_strategy_state(self) -> Dict[str, Any]:
        """收集策略绩效"""
        state = {"daily_pnl": 0.0, "win_rate": 0.0, "total_trades": 0}
        try:
            engine = get_engine()
            if engine and hasattr(engine, 'order_mgr'):
                stats = engine.order_mgr.get_daily_trading_stats()
                state["daily_pnl"] = stats.get("realized_pnl", 0.0)
                state["win_rate"] = stats.get("win_rate", 0.0) * 100
                state["total_trades"] = stats.get("count", 0)
                # 获取夏普（从风险监控器）
                if hasattr(engine, 'risk_monitor'):
                    snap = await engine.risk_monitor.get_snapshot()
                    state["current_drawdown"] = snap.drawdown_pct
        except Exception:
            pass
        return state

    async def _collect_council_state(self) -> Dict[str, Any]:
        """收集议会动态"""
        state = {"dominant_agent": "N/A", "last_decision": "无", "abstain_rate": 0.0}
        try:
            engine = get_engine()
            if engine and hasattr(engine, 'agent_council'):
                council = engine.agent_council
                last = council.get_last_decision()
                state["last_decision"] = last.description if last else "无"
                state["abstain_rate"] = council.get_abstain_rate() * 100
                # 主导角色（权重最高者）
                weights = council._get_current_weights()
                if weights:
                    dominant = max(weights, key=weights.get)
                    state["dominant_agent"] = dominant
        except Exception:
            pass
        return state

    async def _collect_evolution_state(self) -> Dict[str, Any]:
        """收集进化工厂状态"""
        state = {"new_factors": 0, "shadow_count": 0}
        try:
            engine = get_engine()
            if engine and hasattr(engine, 'plugin_mgr'):
                # 占位：实际从进化工厂获取
                pass
        except Exception:
            pass
        return state

    async def _collect_risk_state(self) -> Dict[str, Any]:
        """收集风险控制状态"""
        state = {"cb_level": 0, "var_99": 0.0}
        try:
            engine = get_engine()
            if engine and hasattr(engine, 'risk_monitor'):
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
        """收集环境资源状态"""
        import psutil
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        return {
            "cpu_pct": cpu,
            "mem_pct": mem.percent,
            "disk_pct": disk.percent,
        }

    async def _collect_recent_events(self) -> List[Dict]:
        """收集最近的重要行为日志事件"""
        events = []
        try:
            if self.behavior_log:
                entries = self.behavior_log.get_recent(10, level="WARN")  # 只取重要事件
                for e in entries:
                    events.append(f"[{e.ts_str}] {e.content}")
        except Exception:
            pass
        return events

    # ======================== 报告排版 ========================
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
        md += "\n> 愿火种永燃 🔥"
        return md

    # ======================== 辅助 ========================
    def _get_version(self) -> str:
        try:
            with open("config/version.txt", "r") as f:
                return f.read().strip()
        except Exception:
            return "unknown"

    def get_recent_reports(self, limit: int = 7) -> List[Dict]:
        return self._report_history[-limit:]
