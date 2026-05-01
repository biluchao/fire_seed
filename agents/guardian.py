#!/usr/bin/env python3
"""
火种系统 (FireSeed) 守护者智能体 (Guardian) —— 存在主义
=========================================================
世界观：存在主义 (Existentialism)
核心信仰：市场的本质是无常，唯一能确定的是风险。
优化目标：-max_drawdown (宁可踏空也不承担风险)
天然对立：进化论 (Alchemist)

职责：
- 监控组合 VaR / CVaR，超出阈值时触发预警
- 压力测试：在历史黑天鹅场景中评估当前持仓
- 对手方风险与流动性危机感知
- 主动发出减仓/对冲建议
- 记录风险事件并推送告警
- 在对抗式议会中提出极端保守的提案，挑战任何冒险决策
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from config.loader import ConfigLoader
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier
from core.risk_monitor import RiskMonitor
from core.order_manager import OrderManager
from api.server import get_engine

# 世界观系统
from agents.worldview import (
    WorldView,
    WorldViewManifesto,
    WorldViewAgent,
)

logger = logging.getLogger("fire_seed.guardian")


# ---------- 守护者世界观宣言 ----------
GUARDIAN_MANIFESTO = WorldViewManifesto(
    worldview=WorldView.EXISTENTIALISM,
    core_belief="市场的本质是无常，唯一能确定的是风险。",
    primary_optimization_target="-max_drawdown",
    adversary_worldview=WorldView.EVOLUTIONISM,       # 与炼金术士的盈利追求天然对立
    forbidden_data_source={"ORDERBOOK", "SENTIMENT"}, # 不接触订单簿细节和情绪数据
    time_scale="300s",                                 # 5分钟重评估
)


@dataclass
class RiskAdvisory:
    """风险建议单"""
    timestamp: datetime = field(default_factory=datetime.now)
    severity: EventLevel = EventLevel.INFO
    category: str = "general"               # var, liquidity, counterparty, black_swan
    current_value: float = 0.0
    threshold: float = 0.0
    message: str = ""
    suggested_action: str = ""               # reduce_position, hedge, emergency_close, none
    acknowledged: bool = False


class GuardianAgent(WorldViewAgent):
    """
    守护者智能体 (存在主义)

    继承 WorldViewAgent，具备世界观驱动的 propose() 与 challenge() 能力。
    5 分钟检查一次风险状况，当检测到威胁时通过议会提出保守方案。
    """

    def __init__(self,
                 config: ConfigLoader,
                 risk_monitor: Optional[RiskMonitor] = None,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None,
                 check_interval_sec: int = 300):
        # 初始化世界观基类
        super().__init__(GUARDIAN_MANIFESTO)

        self.config = config
        self.behavior_log = behavior_log
        self.notifier = notifier
        self.check_interval = check_interval_sec

        # 风控模块（可从外部注入或运行时获取）
        self._risk_monitor = risk_monitor
        self._order_mgr: Optional[OrderManager] = None

        # 历史建议列表
        self._advisories: List[RiskAdvisory] = []

        # 阈值配置
        guardian_cfg = config.get("guardian", {})
        self.var_warn_mult = guardian_cfg.get("var_warn_mult", 30)
        self.cvar_warn_mult = guardian_cfg.get("cvar_warn_mult", 5)
        self.max_drawdown_action = guardian_cfg.get("max_drawdown_action", 15)
        self.liquidity_shrink_critical = guardian_cfg.get("liquidity_shrink_critical", 0.6)
        self.black_swan_scenes = guardian_cfg.get("black_swan_scenes", ["312", "519", "ftx_crash"])

        # 连续异常计数器
        self._consecutive_anomalies = 0

        logger.info("守护者·存在主义 初始化完成")

    # ============== WorldViewAgent 接口实现 ==============
    def propose(self, perception: Dict) -> Dict:
        """
        基于存在主义世界观提出决策建议。
        守护者永远倾向于降低风险，方向信号极度保守。
        """
        # 获取最新的风险快照
        risk_level = self._assess_overall_risk()
        # 存在主义倾向：除非风险极低，否则建议空仓或减仓
        if risk_level >= 0.7:
            return {
                "direction": 0,
                "confidence": 0.9,
                "reason": "存在主义认为当前风险过高，应空仓",
                "source": "Guardian",
            }
        elif risk_level >= 0.4:
            # 轻度风险，允许小仓位但附带减仓建议
            return {
                "direction": 1 if perception.get("trend_strength", 0) > 0.5 else -1,
                "confidence": 0.3,
                "reason": "存在主义谨慎看多/看空",
                "source": "Guardian",
            }
        else:
            return {
                "direction": perception.get("trend_direction", 0),
                "confidence": 0.5,
                "reason": "存在主义认为风险可控",
                "source": "Guardian",
            }

    def challenge(self, other_proposal: Dict, my_worldview: WorldView) -> Dict:
        """
        从存在主义视角挑战他人的冒险提案。
        若提案方向性很强且当前风险指标偏高，建议否决。
        """
        risk_level = self._assess_overall_risk()
        direction = other_proposal.get("direction", 0)
        confidence = other_proposal.get("confidence", 0)

        challenges = []
        veto = False

        if direction != 0 and risk_level >= 0.6:
            challenges.append(f"存在主义指出：当前风险等级 {risk_level:.2f}，不应持有方向性仓位")
            veto = True

        if confidence > 0.7 and risk_level >= 0.4:
            challenges.append("存在主义认为高置信度的方向性判断在市场无常面前是危险的")

        if self._consecutive_anomalies >= 3:
            challenges.append("连续风险异常，任何开仓提案都应推迟")
            veto = True

        return {
            "veto": veto,
            "challenges": challenges,
            "confidence_adjustment": -0.3 if challenges else 0.0,
            "source": "Guardian",
        }

    # ============== 内部风险评估 ==============
    def _assess_overall_risk(self) -> float:
        """综合多维度风险，返回 0~1 的风险等级"""
        components = []

        if self._risk_monitor:
            dd = self._risk_monitor.current_drawdown_pct
            if dd > self.max_drawdown_action:
                components.append(min(1.0, dd / (self.max_drawdown_action * 2)))

            liq = self._risk_monitor.get_liquidity_metrics()
            shrink = liq.get("depth_shrink_from_avg", 0)
            if shrink > 30:
                components.append(min(1.0, shrink / 80))

        if components:
            return np.mean(components)
        return 0.2  # 默认低风险

    # ============== 主守护循环 ==============
    async def run(self) -> None:
        """以独立协程方式持续运行守护检查。"""
        while True:
            await self.evaluate()
            await asyncio.sleep(self.check_interval)

    async def evaluate(self) -> Dict[str, Any]:
        """执行一次全维度风险守护评估，返回摘要。"""
        self._ensure_engine_components()

        advisories = []

        adv = await self._check_var_cvar()
        if adv: advisories.append(adv)

        adv = self._check_drawdown_and_concentration()
        if adv: advisories.append(adv)

        adv = await self._check_liquidity()
        if adv: advisories.append(adv)

        adv = await self._check_counterparty_risk()
        if adv: advisories.append(adv)

        adv = self._assess_black_swan()
        if adv: advisories.append(adv)

        for adv in advisories:
            self._emit_advisory(adv)

        if self.behavior_log:
            self.behavior_log.log(
                EventType.AGENT, "Guardian",
                f"守护评估完成，风险建议: {len(advisories)} 条"
            )

        self._consecutive_anomalies = (
            min(self._consecutive_anomalies + 1, 10) if advisories
            else max(0, self._consecutive_anomalies - 1)
        )

        return {
            "advisories_count": len(advisories),
            "consecutive_anomalies": self._consecutive_anomalies,
            "timestamp": datetime.now().isoformat(),
        }

    # ---------- 各项风险检查 ----------
    async def _check_var_cvar(self) -> Optional[RiskAdvisory]:
        if not self._risk_monitor: return None
        snapshot = await self._risk_monitor.get_snapshot()
        var_99 = snapshot.var_99
        cvar = snapshot.cvar
        daily_loss_limit = snapshot.daily_loss_limit_pct * snapshot.margin_ratio / 100.0

        if var_99 > daily_loss_limit * self.var_warn_mult:
            return RiskAdvisory(
                severity=EventLevel.WARN,
                category="var",
                current_value=var_99,
                threshold=daily_loss_limit * self.var_warn_mult,
                message=f"VaR(99)={var_99:.2f} 超过限 {daily_loss_limit:.2f} 的 {self.var_warn_mult} 倍",
                suggested_action="reduce_position",
            )
        if cvar > daily_loss_limit * self.cvar_warn_mult:
            return RiskAdvisory(
                severity=EventLevel.CRITICAL,
                category="cvar",
                current_value=cvar,
                threshold=daily_loss_limit * self.cvar_warn_mult,
                message=f"CVaR={cvar:.2f} 极高，尾部风险严重",
                suggested_action="emergency_close",
            )
        return None

    def _check_drawdown_and_concentration(self) -> Optional[RiskAdvisory]:
        if not self._risk_monitor: return None
        dd = self._risk_monitor.current_drawdown_pct
        if dd > self.max_drawdown_action:
            return RiskAdvisory(
                severity=EventLevel.WARN if dd < 20 else EventLevel.CRITICAL,
                category="drawdown",
                current_value=dd,
                threshold=self.max_drawdown_action,
                message=f"当前回撤 {dd:.1f}% 超过警戒线 {self.max_drawdown_action}%",
                suggested_action="reduce_position",
            )
        return None

    async def _check_liquidity(self) -> Optional[RiskAdvisory]:
        if not self._risk_monitor: return None
        liq = await self._risk_monitor.get_liquidity_metrics()
        shrink = liq.get("depth_shrink_from_avg", 0)
        if shrink > self.liquidity_shrink_critical * 100:
            return RiskAdvisory(
                severity=EventLevel.CRITICAL,
                category="liquidity",
                current_value=shrink,
                threshold=self.liquidity_shrink_critical * 100,
                message=f"订单簿深度骤降 {shrink:.1f}%，流动性几乎枯竭",
                suggested_action="emergency_close",
            )
        return None

    async def _check_counterparty_risk(self) -> Optional[RiskAdvisory]:
        # 占位
        return None

    def _assess_black_swan(self) -> Optional[RiskAdvisory]:
        # 占位
        return None

    # ---------- 建议处理 ----------
    def _emit_advisory(self, adv: RiskAdvisory) -> None:
        self._advisories.append(adv)
        if len(self._advisories) > 200:
            self._advisories = self._advisories[-200:]

        if self.behavior_log:
            self.behavior_log.log(
                EventType.RISK, "Guardian",
                f"{adv.severity.value} | {adv.category}: {adv.message}",
                snapshot={"suggested_action": adv.suggested_action},
            )

        if self.notifier:
            asyncio.ensure_future(
                self.notifier.send_alert(
                    level=adv.severity.value,
                    title=f"守护者风险告警 [{adv.category}]",
                    body=f"{adv.message}\n建议: {adv.suggested_action}",
                )
            )

    # ---------- 工具方法 ----------
    def _ensure_engine_components(self) -> None:
        try:
            engine = get_engine()
            if engine and self._risk_monitor is None:
                self._risk_monitor = engine.risk_monitor
            if engine and self._order_mgr is None:
                self._order_mgr = engine.order_mgr
        except Exception:
            pass

    def get_status(self) -> Dict[str, Any]:
        return {
            "worldview": self.manifesto.worldview.value,
            "consecutive_anomalies": self._consecutive_anomalies,
            "recent_advisories": [
                {
                    "timestamp": adv.timestamp.isoformat(),
                    "severity": adv.severity.value,
                    "category": adv.category,
                    "suggested_action": adv.suggested_action,
                }
                for adv in self._advisories[-5:]
            ],
            }
