#!/usr/bin/env python3
"""
火种系统 (FireSeed) 环境检察官智能体 (EnvInspector)
=====================================================
全天候监控服务器物理与操作系统层面的健康状态，包括：
- CPU 使用率、温度、频率调节
- 物理内存与 SWAP 使用
- 磁盘使用率与 I/O 延迟
- 网络链路质量（丢包率、重传率）
- 系统时钟同步（NTP 状态）
- 共享内存段状态
- 发现异常时生成分级告警并推送至消息渠道
"""

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import psutil

from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier

logger = logging.getLogger("fire_seed.env_inspector")


@dataclass
class EnvAlert:
    """环境告警条目"""
    timestamp: datetime = field(default_factory=datetime.now)
    level: EventLevel = EventLevel.INFO
    source: str = ""
    message: str = ""
    suggestion: str = ""


class EnvInspector:
    """
    环境检察官智能体。
    以固定间隔扫描操作系统与硬件指标，输出诊断报告与告警。
    """

    def __init__(self,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None,
                 check_interval_sec: int = 60):
        """
        :param behavior_log: 全系统行为日志实例
        :param notifier:     消息推送器
        :param check_interval_sec: 定时检查间隔（秒）
        """
        self.behavior_log = behavior_log
        self.notifier = notifier
        self.check_interval = check_interval_sec

        # 历史告警列表（内存）
        self._alerts: List[EnvAlert] = []
        # 上一次检查时间
        self._last_check = 0.0

        # 连续异常计数器（用于去重）
        self._consecutive_issues: Dict[str, int] = {}

        # 网络历史（用于判断趋势）
        self._net_drop_history: deque = deque(maxlen=60)
        self._net_retrans_history: deque = deque(maxlen=60)

    # ======================== 主诊断入口 ========================
    async def evaluate(self) -> Dict[str, Any]:
        """
        执行一次全面的硬件环境诊断。
        返回结构化诊断报告，推送异常告警。
        """
        now = time.time()
        if now - self._last_check < self.check_interval:
            return {"status": "throttled"}
        self._last_check = now

        alerts: List[EnvAlert] = []
        self._run_checks(alerts)

        # 推送告警
        for alert in alerts:
            self._emit_alert(alert)

        # 写入行为日志
        if self.behavior_log:
            self.behavior_log.log(
                EventType.SYSTEM, "EnvInspector",
                f"环境检查完成，告警: {len(alerts)}"
            )

        # 清理旧告警
        if len(self._alerts) > 200:
            self._alerts = self._alerts[-200:]

        return {
            "status": "WARNING" if alerts else "OK",
            "alerts": [self._alert_to_dict(a) for a in alerts[-10:]],
            "timestamp": datetime.now().isoformat()
        }

    # ======================== 具体检查项 ========================
    def _run_checks(self, alerts: List[EnvAlert]) -> None:
        self._check_cpu(alerts)
        self._check_memory(alerts)
        self._check_disk(alerts)
        self._check_network(alerts)
        self._check_clock(alerts)
        self._check_shm(alerts)

    def _check_cpu(self, alerts: List[EnvAlert]) -> None:
        """CPU 使用率、温度、降频"""
        # 使用率
        cpu_pct = psutil.cpu_percent(interval=0.1)
        if cpu_pct > 90:
            alerts.append(EnvAlert(
                level=EventLevel.WARN,
                source="CPU使用率",
                message=f"CPU 使用率 {cpu_pct:.1f}% 超过 90%",
                suggestion="检查是否有异常进程占用"
            ))

        # 温度（需 lm-sensors，psutil 可读取部分平台）
        try:
            temps = psutil.sensors_temperatures()
            for name, entries in temps.items():
                for entry in entries:
                    if entry.current and entry.current > 80:
                        alerts.append(EnvAlert(
                            level=EventLevel.WARN,
                            source="CPU温度",
                            message=f"{name} 温度 {entry.current:.0f}°C (高温)",
                            suggestion="检查散热与风扇状态"
                        ))
        except Exception:
            pass

        # 降频检查
        freq = psutil.cpu_freq()
        if freq and freq.max and freq.current < freq.max * 0.5:
            alerts.append(EnvAlert(
                level=EventLevel.WARN,
                source="CPU频率",
                message=f"CPU 降频: 当前 {freq.current}MHz, 最大 {freq.max}MHz",
                suggestion="检查电源模式与散热"
            ))

    def _check_memory(self, alerts: List[EnvAlert]) -> None:
        """内存与 SWAP 使用"""
        mem = psutil.virtual_memory()
        if mem.percent > 90:
            alerts.append(EnvAlert(
                level=EventLevel.WARN,
                source="内存使用率",
                message=f"内存使用 {mem.percent:.1f}% (剩余 {mem.available/(1024**3):.1f}GB)",
                suggestion="考虑增加内存或关闭非必要服务"
            ))

        swap = psutil.swap_memory()
        # SWAP 使用超过 100MB 视为异常
        if swap.used > 100 * 1024 * 1024:
            alerts.append(EnvAlert(
                level=EventLevel.WARN,
                source="SWAP使用",
                message=f"SWAP 已使用 {swap.used/(1024**2):.0f}MB，可能导致性能下降",
                suggestion="检查内存压力，避免频繁换页"
            ))

        # 大页检查（/proc/meminfo）
        try:
            with open('/proc/meminfo') as f:
                for line in f:
                    if 'HugePages_Free' in line:
                        free = int(line.split()[1])
                        if free == 0:
                            alerts.append(EnvAlert(
                                level=EventLevel.INFO,
                                source="大页内存",
                                message="大页内存已用尽",
                                suggestion="若依赖大页，需调整分配"
                            ))
                        break
        except Exception:
            pass

    def _check_disk(self, alerts: List[EnvAlert]) -> None:
        """磁盘使用率与 I/O 延迟"""
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                if usage.percent > 90:
                    alerts.append(EnvAlert(
                        level=EventLevel.WARN if usage.percent < 95 else EventLevel.CRITICAL,
                        source="磁盘使用率",
                        message=f"{part.mountpoint} 使用 {usage.percent:.1f}%",
                        suggestion="清理日志或扩容"
                    ))
            except Exception:
                pass

        # 磁盘 I/O 延迟（仅 Linux）
        try:
            io_counters = psutil.disk_io_counters()
            # 此处无法直接获取延迟，仅记录占位
        except Exception:
            pass

    def _check_network(self, alerts: List[EnvAlert]) -> None:
        """网络链路质量"""
        try:
            stats = psutil.net_io_counters(pernic=True)
            for iface, s in stats.items():
                if iface == 'lo':
                    continue
                # 丢包率
                drop_pct = (s.dropin + s.dropout) / max(s.packets_recv + s.packets_sent, 1) * 100
                self._net_drop_history.append(drop_pct)
                if drop_pct > 1.0:
                    alerts.append(EnvAlert(
                        level=EventLevel.WARN,
                        source="网络丢包",
                        message=f"网卡 {iface} 丢包率 {drop_pct:.2f}%",
                        suggestion="检查物理链路与交换机"
                    ))
                # 重传率（TCP 层面，psutil 未直接提供，可略）
        except Exception:
            pass

    def _check_clock(self, alerts: List[EnvAlert]) -> None:
        """系统时钟同步状态"""
        try:
            import subprocess
            result = subprocess.run(
                ['timedatectl', 'show', '-p', 'NTPSynchronized'],
                capture_output=True, text=True, timeout=2
            )
            if 'no' in result.stdout.lower():
                alerts.append(EnvAlert(
                    level=EventLevel.WARN,
                    source="时钟同步",
                    message="NTP 时间同步未激活",
                    suggestion="启用 NTP 同步以保持时间精度"
                ))
        except Exception:
            # 非 systemd 环境忽略
            pass

    def _check_shm(self, alerts: List[EnvAlert]) -> None:
        """共享内存使用情况"""
        try:
            import subprocess
            result = subprocess.run(
                ['ipcs', '-m', '-u'],
                capture_output=True, text=True, timeout=2
            )
            # 简单解析（不作复杂处理）
            if 'max total' in result.stdout.lower():
                pass  # 未来可更详细解析
        except Exception:
            pass

    # ======================== 告警处理 ========================
    def _emit_alert(self, alert: EnvAlert) -> None:
        """记录并推送告警"""
        self._alerts.append(alert)

        # 去重：同一来源连续出现时降低推送频率
        key = f"{alert.source}_{alert.level.value}"
        self._consecutive_issues[key] = self._consecutive_issues.get(key, 0) + 1
        if self._consecutive_issues[key] % 3 != 0:
            return  # 每3次重复才推送

        if self.notifier:
            asyncio.ensure_future(
                self.notifier.send_alert(
                    level=alert.level.value,
                    title=f"环境检察官告警 [{alert.source}]",
                    body=f"{alert.message}\n建议: {alert.suggestion}"
                )
            )

    @staticmethod
    def _alert_to_dict(alert: EnvAlert) -> Dict[str, Any]:
        return {
            "timestamp": alert.timestamp.isoformat(),
            "level": alert.level.value,
            "source": alert.source,
            "message": alert.message,
            "suggestion": alert.suggestion,
        }

    # ======================== 查询接口 ========================
    def get_recent_alerts(self, limit: int = 20) -> List[Dict]:
        return [self._alert_to_dict(a) for a in self._alerts[-limit:]]

    async def run_loop(self) -> None:
        """独立运行循环（可选）"""
        while True:
            await self.evaluate()
            await asyncio.sleep(self.check_interval)
