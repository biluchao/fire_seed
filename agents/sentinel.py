#!/usr/bin/env python3
"""
火种系统 (FireSeed) 监察者智能体 (Sentinel)
=============================================
全天候监控系统运行状态，包含：
- 关键进程存活 (引擎、C++守护进程、Redis、Docker)
- 系统资源 (CPU、内存、磁盘、SWAP)
- 网络延迟与丢包率
- API 限频与错误率
- 数据库连接与日志写入
- 熔断、回撤、策略异常等交易指标

发现异常时生成分级告警并推送至消息渠道。
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from api.server import get_engine, get_config
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.self_check import SystemSelfCheck
from core.notifier import SystemNotifier

logger = logging.getLogger("fire_seed.sentinel")


@dataclass
class HealthAlert:
    """健康告警条目"""
    timestamp: datetime = field(default_factory=datetime.now)
    level: EventLevel = EventLevel.INFO
    source: str = ""
    message: str = ""
    suggestion: str = ""
    acknowledged: bool = False


class SentinelAgent:
    """
    监察者智能体。
    以固定间隔扫描系统各维度，生成综合健康报告。
    可在引擎后台线程中运行，也可按需调用。
    """

    def __init__(self,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None,
                 check_interval_sec: int = 15):
        """
        :param behavior_log: 全系统行为日志实例
        :param notifier:     消息推送器
        :param check_interval_sec: 定时检查间隔（秒）
        """
        self.behavior_log = behavior_log
        self.notifier = notifier
        self.check_interval = check_interval_sec

        # 历史告警列表（内存）
        self._alerts: List[HealthAlert] = []
        # 上一次检查时间
        self._last_check = 0.0
        # 自检模块
        self._self_check = SystemSelfCheck()

        # 连续异常计数器（用于避免重复告警）
        self._consecutive_issues: Dict[str, int] = {}

    # ======================== 主入口 ========================
    async def evaluate(self) -> Dict[str, Any]:
        """
        执行一次全面健康评估。
        返回结构化健康报告，并推送异常告警。
        """
        now = time.time()
        # 频率控制（避免高频重复检查）
        if now - self._last_check < self.check_interval:
            return {"status": "throttled"}

        self._last_check = now
        report = self._self_check.run()  # 核心系统自检

        alerts = []
        # 遍历自检报告中的异常项
        for check in report.checks:
            if check.status == "ERROR":
                alert = HealthAlert(
                    level=EventLevel.CRITICAL,
                    source=check.name,
                    message=check.message,
                    suggestion="请立即检查相关模块"
                )
                alerts.append(alert)
            elif check.status == "WARNING":
                alert = HealthAlert(
                    level=EventLevel.WARN,
                    source=check.name,
                    message=check.message,
                    suggestion="关注趋势，必要时手动介入"
                )
                alerts.append(alert)

        # 额外维度：策略与风险指标
        await self._check_strategy_health(alerts)

        # 合并告警并推送
        for alert in alerts:
            self._emit_alert(alert)

        # 写入行为日志
        if self.behavior_log:
            self.behavior_log.log(
                EventType.SYSTEM,
                "Sentinel",
                f"健康检查完成，评分={report.score}，告警={len(alerts)}",
                snapshot={"score": report.score, "alerts": len(alerts)}
            )

        # 清理过时告警（保留最近200条）
        if len(self._alerts) > 200:
            self._alerts = self._alerts[-200:]

        return {
            "health_score": report.score,
            "status": report.status,
            "alerts": [self._alert_to_dict(a) for a in alerts[-10:]],
            "timestamp": report.timestamp
        }

    # ======================== 策略与风险检测 ========================
    async def _check_strategy_health(self, alerts: List[HealthAlert]) -> None:
        """检查策略层面的健康度（需从引擎获取数据）"""
        try:
            engine = get_engine()
            if engine is None:
                return

            # 检查日亏损是否接近熔断
            risk = engine.risk_monitor
            cb = risk.circuit_breaker
            if cb.level >= 1:
                alerts.append(HealthAlert(
                    level=EventLevel.WARN if cb.level == 1 else EventLevel.CRITICAL,
                    source="熔断监控",
                    message=f"当前熔断级别 {cb.level}: {cb.reason}",
                    suggestion="检查仓位与风险敞口"
                ))

            # 检查是否连续亏损
            daily_stats = engine.order_mgr.get_daily_trading_stats()
            if daily_stats.get("realized_pnl", 0) < -500:
                alerts.append(HealthAlert(
                    level=EventLevel.WARN,
                    source="策略绩效",
                    message=f"今日亏损 {daily_stats['realized_pnl']:.0f} USDT",
                    suggestion="考虑暂停或缩减仓位"
                ))

            # 检查API错误率
            api_err_rate = getattr(engine.execution, 'error_rate', 0.0)
            if api_err_rate > 0.02:
                alerts.append(HealthAlert(
                    level=EventLevel.WARN,
                    source="API错误率",
                    message=f"错误率 {api_err_rate*100:.1f}%",
                    suggestion="检查交易所连接与限频状态"
                ))

        except Exception as e:
            logger.warning(f"策略健康检查异常: {e}")

    # ======================== 告警推送 ========================
    def _emit_alert(self, alert: HealthAlert) -> None:
        """记录告警并推送至消息渠道"""
        # 连续相同告警去重：如果30秒内已经发送过相同来源的告警，跳过
        recent_key = f"{alert.source}_{alert.level.value}"
        if recent_key in self._consecutive_issues:
            self._consecutive_issues[recent_key] += 1
            if self._consecutive_issues[recent_key] % 5 != 0:  # 每5次重复才再次通知
                return
        else:
            self._consecutive_issues[recent_key] = 1

        self._alerts.append(alert)

        # 推送至消息渠道
        if self.notifier:
            asyncio.ensure_future(
                self.notifier.send_alert(
                    level=alert.level.value,
                    title=f"监察者告警 [{alert.source}]",
                    body=f"{alert.message}\n建议: {alert.suggestion}"
                )
            )

    @staticmethod
    def _alert_to_dict(alert: HealthAlert) -> Dict[str, Any]:
        return {
            "timestamp": alert.timestamp.isoformat(),
            "level": alert.level.value,
            "source": alert.source,
            "message": alert.message,
            "suggestion": alert.suggestion,
        }

    # ======================== 告警查询 ========================
    def get_recent_alerts(self, limit: int = 20, level: Optional[str] = None) -> List[Dict]:
        """获取最近的健康告警"""
        result = []
        for alert in reversed(self._alerts):
            if level and alert.level.value != level:
                continue
            result.append(self._alert_to_dict(alert))
            if len(result) >= limit:
                break
        return result

    def acknowledge(self, index: int) -> bool:
        """确认一条告警为已知"""
        if 0 <= index < len(self._alerts):
            self._alerts[index].acknowledged = True
            return True
        return False

    # ======================== 定时循环 (可由外部协程驱动) ========================
    async def run_loop(self):
        """如果需要独立运行循环，可使用此方法"""
        while True:
            await self.evaluate()
            await asyncio.sleep(self.check_interval)
