#!/usr/bin/env python3
"""
火种系统 (FireSeed) 跟单协调官智能体 (CopyTradeCoordinator)
===============================================================
世界观：整体论 (Holism)
核心信仰：系统是各部分的协同体，部分异常反映整体失调
天然对立：机械唯物主义 (Sentinel)
专属数据源：子账户 API 响应状态码、延迟
禁止接触：单个账户的详细持仓
时间尺度：30 秒
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from api.server import get_engine
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier
from agents.worldview import WorldViewAgent, WorldViewManifesto, WorldView

logger = logging.getLogger("fire_seed.copy_trade_coordinator")


@dataclass
class SubAccountHealth:
    """子账户健康状态"""
    name: str
    online: bool = False
    last_sync: Optional[datetime] = None
    sync_delay_sec: float = 0.0
    daily_error_count: int = 0
    max_position_pct: float = 0.0
    status_summary: str = "unknown"


@dataclass
class CopyTradeAlert:
    """跟单告警"""
    timestamp: datetime = field(default_factory=datetime.now)
    level: EventLevel = EventLevel.INFO
    account: str = ""
    message: str = ""
    suggestion: str = ""


class CopyTradeCoordinator(WorldViewAgent):
    """
    跟单协调官智能体（整体论）。
    监控多账户跟单系统的整体协同状态，当子系统异常时推断系统整体失调。
    """

    def __init__(self,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None,
                 check_interval_sec: int = 30):
        # 构建整体论世界观宣言
        manifesto = WorldViewManifesto(
            worldview=WorldView.HOLISM,
            core_belief="系统是各部分的协同体，部分异常反映整体失调",
            primary_optimization_target="sync_success_rate",
            adversary_worldview=WorldView.MECHANICAL_MATERIALISM,
            forbidden_data_source={"INDIVIDUAL_ACCOUNT_DETAIL"},
            exclusive_data_source={"SUB_ACCOUNT_API_RESPONSE", "SYNC_DELAY"},
            time_scale="30",
        )
        super().__init__(manifesto)

        self.behavior_log = behavior_log
        self.notifier = notifier
        self.check_interval = check_interval_sec

        self._health_states: Dict[str, SubAccountHealth] = {}
        self._alerts: List[CopyTradeAlert] = []
        self._last_check = 0.0
        self._consecutive_anomalies: Dict[str, int] = {}
        self._recovery_attempts: Dict[str, int] = {}

    # ====================== 世界观接口实现 ======================
    def propose(self, perception: Dict = None) -> Dict:
        """
        基于整体论提出交易建议。
        监控所有子账户状态，若有异常则建议减仓或暂停跟单。
        """
        # 从引擎获取跟单状态
        engine = get_engine()
        if engine is None or not hasattr(engine, 'copy_trading'):
            return {
                "direction": 0,
                "confidence": 0.0,
                "description": "跟单引擎未就绪",
                "worldview": self.manifesto.worldview.value,
            }

        trader = engine.copy_trading
        subs = trader.list_sub_accounts()
        if not subs:
            return {
                "direction": 0,
                "confidence": 0.0,
                "description": "无子账户",
                "worldview": self.manifesto.worldview.value,
            }

        offline_count = sum(1 for s in subs if s.get('status') != 'online')
        total_count = len(subs)
        sync_health = offline_count / total_count if total_count > 0 else 0

        if sync_health > 0.3:
            # 超过30%子账户离线，整体失调，建议减仓
            return {
                "direction": -1,
                "confidence": min(0.9, sync_health),
                "description": f"多账户跟单系统整体失调：{offline_count}/{total_count} 离线",
                "worldview": self.manifesto.worldview.value,
            }
        elif sync_health > 0.1:
            return {
                "direction": 0,
                "confidence": 0.5,
                "description": f"少数子账户异常：{offline_count}/{total_count} 离线，建议暂停加仓",
                "worldview": self.manifesto.worldview.value,
            }
        else:
            return {
                "direction": 1,
                "confidence": 0.6,
                "description": "多账户跟单系统协同良好",
                "worldview": self.manifesto.worldview.value,
            }

    def challenge(self, other_proposal: Dict, my_worldview: WorldView) -> Dict:
        """
        从整体论视角挑战其他提案。
        若提案未考虑多账户跟单的连锁影响，则提出否决。
        """
        challenges = []

        # 检查提案是否完全忽略了多账户系统的存在
        engine = get_engine()
        if engine and hasattr(engine, 'copy_trading') and engine.copy_trading.enabled:
            subs = engine.copy_trading.list_sub_accounts()
            if subs:
                offline = [s for s in subs if s.get('status') != 'online']
                if offline:
                    challenges.append(
                        f"当前有 {len(offline)} 个子账户离线，提案未考虑整体协同风险"
                    )

                # 检查是否有子账户处于高延迟状态
                for name, health in self._health_states.items():
                    if health.sync_delay_sec > 120:
                        challenges.append(
                            f"子账户 {name} 延迟 {health.sync_delay_sec:.0f}s，提案风险被低估"
                        )

        veto = len(challenges) >= 2
        return {
            "veto": veto,
            "challenges": challenges,
            "suggestion": "建议在解决跟单系统异常后再执行此提案",
            "worldview": my_worldview.value,
        }

    # ====================== 主监控入口 ======================
    async def evaluate(self) -> Dict[str, Any]:
        """执行一次完整的跟单健康检查"""
        now = time.time()
        if now - self._last_check < self.check_interval:
            return {"status": "throttled"}
        self._last_check = now

        alerts: List[CopyTradeAlert] = []
        engine = get_engine()
        if engine is None or not hasattr(engine, 'copy_trading'):
            if self.behavior_log:
                self.behavior_log.log(EventType.AGENT, "CopyTradeCoordinator", "跟单引擎未就绪")
            return {"status": "engine_not_available"}

        trader = engine.copy_trading
        if not trader.enabled:
            return {"status": "disabled"}

        await self._check_sub_account_connectivity(trader, alerts)
        await self._check_sync_delay(trader, alerts)
        await self._check_success_rate(trader, alerts)
        await self._check_position_deviation(trader, alerts)
        await self._attempt_recovery(trader, alerts)

        for alert in alerts:
            self._emit_alert(alert)

        online_count = sum(1 for h in self._health_states.values() if h.online)
        total_count = len(self._health_states)
        if self.behavior_log:
            self.behavior_log.log(
                EventType.AGENT, "CopyTradeCoordinator",
                f"跟单检查完成: {online_count}/{total_count} 在线, 告警 {len(alerts)}",
                snapshot={"online": online_count, "total": total_count, "alerts": len(alerts)}
            )

        return {
            "online_count": online_count,
            "total_accounts": total_count,
            "alert_count": len(alerts),
            "health_states": {k: v.status_summary for k, v in self._health_states.items()},
            "timestamp": datetime.now().isoformat(),
        }

    async def _check_sub_account_connectivity(self, trader, alerts):
        subs = trader.list_sub_accounts()
        for sub in subs:
            name = sub.get("name", "unknown")
            status = sub.get("status", "offline")
            last_sync = sub.get("last_sync")

            if name not in self._health_states:
                self._health_states[name] = SubAccountHealth(name=name)

            health = self._health_states[name]
            health.status_summary = status

            if status == "offline":
                health.online = False
                alerts.append(CopyTradeAlert(
                    level=EventLevel.WARN,
                    account=name,
                    message=f"子账户 {name} 处于离线状态",
                    suggestion="检查API密钥有效性与交易所连接"
                ))
                self._consecutive_anomalies[name] = self._consecutive_anomalies.get(name, 0) + 1
            elif status == "disabled":
                health.online = False
            else:
                health.online = True
                if last_sync:
                    sync_time = datetime.fromisoformat(last_sync) if isinstance(last_sync, str) else last_sync
                    delay = (datetime.now() - sync_time).total_seconds()
                    health.last_sync = sync_time
                    health.sync_delay_sec = delay
                    if delay > 300:
                        alerts.append(CopyTradeAlert(
                            level=EventLevel.WARN,
                            account=name,
                            message=f"子账户 {name} 超过 {delay:.0f}秒 未同步",
                            suggestion="检查网络连接或交易所状态"
                        ))
                        self._consecutive_anomalies[name] = self._consecutive_anomalies.get(name, 0) + 1
                    else:
                        self._consecutive_anomalies[name] = 0

                error_count = sub.get("error_count", 0)
                health.daily_error_count = error_count
                if error_count > 10:
                    alerts.append(CopyTradeAlert(
                        level=EventLevel.WARN,
                        account=name,
                        message=f"子账户 {name} 今日跟单错误 {error_count} 次",
                        suggestion="检查日志定位错误原因"
                    ))

    async def _check_sync_delay(self, trader, alerts):
        for name, health in self._health_states.items():
            if health.online and health.sync_delay_sec > 120:
                alerts.append(CopyTradeAlert(
                    level=EventLevel.WARN if health.sync_delay_sec < 300 else EventLevel.CRITICAL,
                    account=name,
                    message=f"子账户 {name} 跟单延迟 {health.sync_delay_sec:.0f}秒",
                    suggestion="网络延迟过高，考虑检查子账户交易所的物理区域"
                ))

    async def _check_success_rate(self, trader, alerts):
        if not self.behavior_log:
            return
        recent = self.behavior_log.query_db(
            start_time=datetime.now() - timedelta(hours=1),
            module="CopyTrading",
            limit=500
        )
        failures = [e for e in recent if "失败" in e.get("content", "")]
        if len(failures) > 5:
            fail_per_account = {}
            for f in failures:
                for name in self._health_states:
                    if name in f.get("content", ""):
                        fail_per_account[name] = fail_per_account.get(name, 0) + 1
            for name, count in fail_per_account.items():
                if count > 3:
                    alerts.append(CopyTradeAlert(
                        level=EventLevel.WARN,
                        account=name,
                        message=f"子账户 {name} 近1小时跟单失败 {count} 次",
                        suggestion="考虑暂停该账户跟单并人工排查"
                    ))

    async def _check_position_deviation(self, trader, alerts):
        try:
            engine = get_engine()
            if engine is None:
                return
            master_pos = engine.order_mgr.get_position_summary()
            master_size = master_pos.size if hasattr(master_pos, 'size') else 0.0
            # 实际偏差检查需对比每个子账户的持仓，此处占位
        except Exception as e:
            logger.warning(f"仓位偏差检查失败: {e}")

    async def _attempt_recovery(self, trader, alerts):
        for name, count in self._consecutive_anomalies.items():
            if count >= 10:
                alerts.append(CopyTradeAlert(
                    level=EventLevel.CRITICAL,
                    account=name,
                    message=f"子账户 {name} 连续异常 {count} 次，已超出自动恢复次数",
                    suggestion="需要人工介入排查"
                ))
            elif count >= 3:
                attempts = self._recovery_attempts.get(name, 0)
                if attempts < 3:
                    success = trader.enable_account(name)
                    self._recovery_attempts[name] = attempts + 1
                    if success:
                        self._consecutive_anomalies[name] = 0
                        logger.info(f"子账户 {name} 自动恢复成功")
                        alerts.append(CopyTradeAlert(
                            level=EventLevel.INFO,
                            account=name,
                            message=f"子账户 {name} 已自动恢复上线",
                            suggestion=""
                        ))
                    else:
                        logger.warning(f"子账户 {name} 自动恢复失败 ({attempts+1}/3)")

    def _emit_alert(self, alert: CopyTradeAlert):
        self._alerts.append(alert)
        if len(self._alerts) > 200:
            self._alerts = self._alerts[-200:]
        if self.notifier:
            asyncio.ensure_future(
                self.notifier.send_alert(
                    level=alert.level.value,
                    title=f"跟单告警 [{alert.account}]",
                    body=f"{alert.message}\n建议: {alert.suggestion}"
                )
            )

    def get_status(self) -> Dict[str, Any]:
        return {
            "accounts": {k: v.status_summary for k, v in self._health_states.items()},
            "alerts_count": len(self._alerts),
            "recent_alerts": [
                {"timestamp": a.timestamp.isoformat(), "account": a.account, "message": a.message}
                for a in self._alerts[-5:]
            ],
        }

    async def run_loop(self) -> None:
        while True:
            await self.evaluate()
            await asyncio.sleep(self.check_interval)
