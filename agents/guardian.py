#!/usr/bin/env python3
"""
火种系统 (FireSeed) 守护者智能体 (Guardian)
=============================================
专门负责极端风险识别与多维风险抗争：
- 监控组合 VaR / CVaR，超出阈值时触发预警
- 压力测试：在历史黑天鹅场景中评估当前持仓
- 对手方风险与流动性危机感知
- 主动发出减仓/对冲建议
- 记录风险事件并推送告警
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

logger = logging.getLogger("fire_seed.guardian")


@dataclass
class RiskAdvisory:
    """风险建议单"""
    timestamp: datetime = field(default_factory=datetime.now)
    severity: EventLevel = EventLevel.INFO
    category: str = "general"               # 风险类别: var, liquidity, counterparty, black_swan
    current_value: float = 0.0
    threshold: float = 0.0
    message: str = ""
    suggested_action: str = ""               # 建议动作: reduce_position, hedge, emergency_close, none
    acknowledged: bool = False


class GuardianAgent:
    """
    守护者智能体。

    以独立协程或定时回调方式运行，对以下风险维度进行持续监控：
    - 组合 VaR / CVaR 是否超过安全边界
    - 当前回撤与持仓集中度
    - 流动性骤降（盘口深度变化）
    - 对手方风险（交易所储备金、保险基金）
    - 尾部分布异常（基于极值理论）

    当检测到风险事件时，会立即向 SystemNotifier 发布告警，
    并将建议写入行为日志，供议会投票参考。
    """

    def __init__(self,
                 config: ConfigLoader,
                 risk_monitor: Optional[RiskMonitor] = None,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None,
                 check_interval_sec: int = 30):
        """
        :param config: 全局配置
        :param risk_monitor: 风控模块实例（若未提供，则从引擎获取）
        :param behavior_log: 行为日志
        :param notifier: 消息推送器
        :param check_interval_sec: 守护检查间隔（秒）
        """
        self.config = config
        self.behavior_log = behavior_log
        self.notifier = notifier
        self.check_interval = check_interval_sec

        # 风控模块（可从外部注入或运行时获取）
        self._risk_monitor = risk_monitor
        # 订单管理器
        self._order_mgr: Optional[OrderManager] = None

        # 历史建议列表
        self._advisories: List[RiskAdvisory] = []

        # 阈值配置
        guardian_cfg = config.get("guardian", {})
        self.var_warn_mult = guardian_cfg.get("var_warn_mult", 30)          # VaR 超过日损失限额的倍数告警
        self.cvar_warn_mult = guardian_cfg.get("cvar_warn_mult", 5)
        self.max_drawdown_action = guardian_cfg.get("max_drawdown_action", 15)  # 回撤超此值建议减仓
        self.liquidity_shrink_critical = guardian_cfg.get("liquidity_shrink_critical", 0.6)  # 深度骤降超60%告警
        self.black_swan_scenes = guardian_cfg.get("black_swan_scenes", ["312", "519", "ftx_crash"])

        # 连续异常计数器
        self._consecutive_anomalies = 0

        logger.info("守护者智能体初始化完成")

    # ======================== 主守护循环 ========================
    async def run(self) -> None:
        """以独立协程方式持续运行守护检查。"""
        while True:
            await self.evaluate()
            await asyncio.sleep(self.check_interval)

    async def evaluate(self) -> Dict[str, Any]:
        """
        执行一次全维度风险守护评估。
        返回本次评估摘要，并推送异常告警。
        """
        # 确保获取最新的引擎组件
        self._ensure_engine_components()

        advisories = []

        # 1. VaR / CVaR 检查
        adv = await self._check_var_cvar()
        if adv:
            advisories.append(adv)

        # 2. 回撤与持仓集中度
        adv = self._check_drawdown_and_concentration()
        if adv:
            advisories.append(adv)

        # 3. 流动性危机
        adv = await self._check_liquidity()
        if adv:
            advisories.append(adv)

        # 4. 对手方风险（需要外部数据，若不可用则跳过）
        adv = await self._check_counterparty_risk()
        if adv:
            advisories.append(adv)

        # 5. 黑天鹅预演（压力测试）
        adv = self._assess_black_swan()
        if adv:
            advisories.append(adv)

        # 处理建议
        for adv in advisories:
            self._emit_advisory(adv)

        # 记录守护日志
        if self.behavior_log:
            self.behavior_log.log(
                EventType.AGENT, "Guardian",
                f"守护评估完成，风险建议: {len(advisories)} 条"
            )

        # 更新连续异常计数
        if advisories:
            self._consecutive_anomalies = min(self._consecutive_anomalies + 1, 10)
        else:
            self._consecutive_anomalies = max(0, self._consecutive_anomalies - 1)

        return {
            "advisories_count": len(advisories),
            "consecutive_anomalies": self._consecutive_anomalies,
            "timestamp": datetime.now().isoformat()
        }

    # ======================== 风险检查子项 ========================
    async def _check_var_cvar(self) -> Optional[RiskAdvisory]:
        """VaR / CVaR 指标检测"""
        if not self._risk_monitor:
            return None
        snapshot = await self._risk_monitor.get_snapshot()
        var_99 = snapshot.var_99
        cvar = snapshot.cvar
        daily_loss_limit = snapshot.daily_loss_limit_pct * snapshot.margin_ratio  # 简陋转换，实际需取权益

        if var_99 > daily_loss_limit * self.var_warn_mult:
            return RiskAdvisory(
                severity=EventLevel.WARN,
                category="var",
                current_value=var_99,
                threshold=daily_loss_limit * self.var_warn_mult,
                message=f"VaR(99)={var_99:.2f} 超过日损失上限 {daily_loss_limit:.2f} 的 {self.var_warn_mult} 倍",
                suggested_action="reduce_position"
            )
        if cvar > daily_loss_limit * self.cvar_warn_mult:
            return RiskAdvisory(
                severity=EventLevel.CRITICAL,
                category="cvar",
                current_value=cvar,
                threshold=daily_loss_limit * self.cvar_warn_mult,
                message=f"CVaR={cvar:.2f} 极高，尾部风险严重",
                suggested_action="emergency_close"
            )
        return None

    def _check_drawdown_and_concentration(self) -> Optional[RiskAdvisory]:
        """回撤与持仓集中度检测"""
        if not self._risk_monitor:
            return None
        dd = self._risk_monitor.current_drawdown_pct
        if dd > self.max_drawdown_action:
            return RiskAdvisory(
                severity=EventLevel.WARN if dd < 20 else EventLevel.CRITICAL,
                category="drawdown",
                current_value=dd,
                threshold=self.max_drawdown_action,
                message=f"当前回撤 {dd:.1f}% 超过警戒线 {self.max_drawdown_action}%",
                suggested_action="reduce_position"
            )
        return None

    async def _check_liquidity(self) -> Optional[RiskAdvisory]:
        """流动性检测"""
        if not self._risk_monitor:
            return None
        liq = await self._risk_monitor.get_liquidity_metrics()
        shrink = liq.get("depth_shrink_from_avg", 0)
        if shrink > self.liquidity_shrink_critical * 100:
            return RiskAdvisory(
                severity=EventLevel.CRITICAL,
                category="liquidity",
                current_value=shrink,
                threshold=self.liquidity_shrink_critical * 100,
                message=f"订单簿深度骤降 {shrink:.1f}%，流动性几乎枯竭",
                suggested_action="emergency_close"
            )
        return None

    async def _check_counterparty_risk(self) -> Optional[RiskAdvisory]:
        """对手方风险检测（占位，实际可能需读取交易所储备证明数据）"""
        # 此处可扩展：监控交易所保险基金、提币状态等
        return None

    def _assess_black_swan(self) -> Optional[RiskAdvisory]:
        """在黑天鹅压力情景下评估当前持仓（占位）"""
        # 实际实现：选取历史极端日期，计算当前持仓在该日期的模拟损失
        # 若损失超过权益 20%，则生成提示
        return None

    # ======================== 建议处理 ========================
    def _emit_advisory(self, adv: RiskAdvisory) -> None:
        """记录并推送风险建议"""
        self._advisories.append(adv)
        # 保持列表长度
        if len(self._advisories) > 200:
            self._advisories = self._advisories[-200:]

        if self.behavior_log:
            self.behavior_log.log(
                EventType.RISK, "Guardian",
                f"{adv.severity.value} | {adv.category}: {adv.message}",
                snapshot={"suggested_action": adv.suggested_action}
            )

        if self.notifier:
            asyncio.ensure_future(
                self.notifier.send_alert(
                    level=adv.severity.value,
                    title=f"守护者风险告警 [{adv.category}]",
                    body=f"{adv.message}\n建议: {adv.suggested_action}"
                )
            )

    # ======================== 工具方法 ========================
    def _ensure_engine_components(self) -> None:
        """确保持有实时的引擎组件引用"""
        try:
            engine = get_engine()
            if engine and self._risk_monitor is None:
                self._risk_monitor = engine.risk_monitor
            if engine and self._order_mgr is None:
                self._order_mgr = engine.order_mgr
        except Exception:
            pass  # 未启动时忽略

    def get_status(self) -> Dict[str, Any]:
        """返回守护者当前状态摘要"""
        return {
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
            "last_check": datetime.now().isoformat(),
                                              }
