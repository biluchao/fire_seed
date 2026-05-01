#!/usr/bin/env python3
"""
火种系统 (FireSeed) 系统通知器
================================
将内部告警、日报、状态变更等消息通过 MessengerHub
分发至已配置的通讯渠道（Telegram、钉钉、企业微信等）。
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.behavioral_logger import BehavioralLogger, EventType, EventLevel

logger = logging.getLogger("fire_seed.notifier")


class SystemNotifier:
    """
    系统通知服务。
    封装所有对外消息推送逻辑，根据告警级别选择推送渠道与格式。
    """

    def __init__(self, messenger_hub=None):
        """
        :param messenger_hub: MessengerHub 实例，若为 None 则仅写入行为日志
        """
        self.hub = messenger_hub
        self._recent_alerts: List[Dict[str, Any]] = []  # 内存中的告警历史

    # ======================== 通用告警推送 ========================
    async def send_alert(self,
                         level: str,
                         title: str,
                         body: str,
                         push_console: bool = True) -> None:
        """
        发送一条分级告警。
        :param level: CRITICAL / HIGH / WARN / INFO
        :param title: 告警标题
        :param body: 告警正文
        :param push_console: 是否同步打印到控制台日志
        """
        # 记录到内部告警历史
        self._recent_alerts.append({
            "timestamp": datetime.now(),
            "level": level,
            "title": title,
            "message": body,
            "acknowledged": False,
        })
        # 限制告警历史长度
        if len(self._recent_alerts) > 200:
            self._recent_alerts = self._recent_alerts[-200:]

        # 日志输出
        if push_console:
            if level in ("CRITICAL", "HIGH"):
                logger.error(f"[{level}] {title}: {body}")
            elif level == "WARN":
                logger.warning(f"[{level}] {title}: {body}")
            else:
                logger.info(f"[{level}] {title}: {body}")

        # 通过消息枢纽广播
        if self.hub:
            try:
                await self.hub.broadcast_alert(level, title, body)
            except Exception as e:
                logger.error(f"告警推送失败: {e}")

    # ======================== 特定告警场景 ========================
    async def alert_circuit_breaker(self, reason: str) -> None:
        """熔断触发告警"""
        await self.send_alert(
            level="CRITICAL",
            title="🔥 熔断触发",
            body=f"原因: {reason}\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

    async def alert_margin_low(self, margin_ratio: float, symbol: str = "") -> None:
        """保证金率过低告警"""
        level = "CRITICAL" if margin_ratio < 120 else "HIGH"
        await self.send_alert(
            level=level,
            title="⚠️ 保证金率过低",
            body=f"品种: {symbol or '全局'}\n当前保证金率: {margin_ratio:.2f}%\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

    async def alert_health(self, health_report) -> None:
        """系统健康检查异常告警"""
        if health_report.status == "OK":
            return
        level = "CRITICAL" if health_report.status == "CRITICAL" else "WARN"
        issues = "\n".join(
            [f"- {c.name}: {c.message}" for c in health_report.checks if c.status != "OK"]
        )
        await self.send_alert(
            level=level,
            title="🩺 系统健康异常",
            body=f"评分: {health_report.score}/100\n异常项:\n{issues}"
        )

    async def alert_nightmare(self) -> None:
        """惊梦唤醒告警"""
        await self.send_alert(
            level="HIGH",
            title="🌩️ 惊梦触发",
            body="市场异动，学习模式已强制退出，恢复激进策略。"
        )

    async def alert_liquidity_crisis(self, depth_shrink_pct: float) -> None:
        """流动性危机告警"""
        await self.send_alert(
            level="HIGH",
            title="📉 流动性危机",
            body=f"订单簿深度萎缩 {depth_shrink_pct:.0f}%，已暂停开仓。"
        )

    async def alert_ota_rollback(self, old_version: str, new_version: str, reason: str) -> None:
        """OTA回滚告警"""
        await self.send_alert(
            level="WARN",
            title="🔄 OTA 已回滚",
            body=f"版本: {new_version} → {old_version}\n原因: {reason}"
        )

    # ======================== 日报与定时推送 ========================
    async def send_daily_report(self, markdown_content: str) -> None:
        """推送每日议会日报（使用Markdown格式）"""
        if self.hub:
            try:
                await self.hub.send_daily_report(markdown_content)
            except Exception as e:
                logger.error(f"日报推送失败: {e}")
        # 同时写入行为日志
        logger.info("日报已生成")

    async def send_message(self, title: str, body: str, level: str = "INFO") -> None:
        """通用消息发送，仅通过消息枢纽推送"""
        if self.hub:
            try:
                await self.hub.send_markdown(title, body)
            except Exception as e:
                logger.error(f"消息推送失败: {e}")

    # ======================== 告警历史查询 ========================
    def get_recent_alerts(self, limit: int = 20) -> List[Dict[str, Any]]:
        """获取最近的告警通知列表"""
        return self._recent_alerts[-limit:]

    def acknowledge_alert(self, index: int) -> bool:
        """标记某条告警为已确认"""
        if 0 <= index < len(self._recent_alerts):
            self._recent_alerts[index]["acknowledged"] = True
            return True
        return False
