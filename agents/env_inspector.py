#!/usr/bin/env python3
"""
火种系统 (FireSeed) 环境检察官智能体 (EnvInspector)
=====================================================
全天候监控服务器物理与操作系统层面的健康状态。
世界观：物理主义 — 一切问题终将表现为物理参数异常。
在议会中主要充当挑战者角色，当环境指标恶化时强制否决交易提案。
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

# ---- 世界观框架 ----
try:
    from agents.worldview import (
        WorldViewAgent, WorldViewManifesto, WorldView
    )
except ImportError:
    # 轻量回退：允许在不依赖完整 worldview 模块时仍可独立测试
    class WorldViewAgent:
        def __init__(self, manifesto=None): self.manifesto = manifesto
        def propose(self, perception): raise NotImplementedError
        def challenge(self, other_proposal, my_worldview): raise NotImplementedError

    class WorldViewManifesto:
        def __init__(self, worldview=None, core_belief="", primary_optimization_target="",
                     adversary_worldview=None, forbidden_data_source=None, time_scale="1m"):
            self.worldview = worldview
            self.core_belief = core_belief
            self.primary_optimization_target = primary_optimization_target
            self.adversary_worldview = adversary_worldview
            self.forbidden_data_source = forbidden_data_source or set()
            self.time_scale = time_scale

    class WorldView:
        PHYSICALISM = "物理主义"
        MECHANICAL_MATERIALISM = "机械唯物主义"
        EVOLUTIONISM = "进化论"
        EXISTENTIALISM = "存在主义"
        SKEPTICISM = "怀疑论"
        INCOMPLETENESS = "不完备定理"
        OCCAMS_RAZOR = "奥卡姆剃刀"
        BAYESIANISM = "贝叶斯主义"
        HERMENEUTICS = "诠释学"
        PLURALISM = "多元主义"
        HISTORICISM = "历史主义"
        HOLISM = "整体论"


logger = logging.getLogger("fire_seed.env_inspector")


@dataclass
class EnvAlert:
    """环境告警条目"""
    timestamp: datetime = field(default_factory=datetime.now)
    level: EventLevel = EventLevel.INFO
    source: str = ""
    message: str = ""
    suggestion: str = ""


class EnvInspector(WorldViewAgent):
    """
    环境检察官智能体。
    世界观：物理主义 — 硬件状态决定一切。
    守护服务器物理健康，极端情况下可以否决议会提案。
    """

    def __init__(self,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None,
                 check_interval_sec: int = 60):
        # 构建物理主义宣言
        manifesto = WorldViewManifesto(
            worldview=WorldView.PHYSICALISM,
            core_belief="一切系统问题最终都会表现为CPU、内存、磁盘或网络的物理异常",
            primary_optimization_target="uptime / error_count",
            adversary_worldview=WorldView.MECHANICAL_MATERIALISM,
            forbidden_data_source={"KLINE", "ORDERBOOK", "POSITION"},
            time_scale="1m"
        )
        super().__init__(manifesto)

        self.behavior_log = behavior_log
        self.notifier = notifier
        self.check_interval = check_interval_sec

        self._alerts: List[EnvAlert] = []
        self._last_check = 0.0
        self._consecutive_issues: Dict[str, int] = {}
        self._net_drop_history: deque = deque(maxlen=60)
        self._net_retrans_history: deque = deque(maxlen=60)

        # 上一次环境综合风险评分 (0-1)
        self._current_risk_score = 0.0

    # ======================== 世界观接口：提议 ========================
    def propose(self, perception: Dict = None) -> Dict:
        """
        物理主义者提案：基于当前环境健康度，给出环境风险评分。
        该提案本身不直接产生交易信号，但风险评分会被议会参考。
        """
        self.evaluate()  # 刷新环境数据
        risk = self._current_risk_score
        return {
            "direction": 0,                     # 中性无方向
            "confidence": risk,                 # 风险越高越不适宜交易
            "type": "environmental_risk",
            "source": "EnvInspector",
            "detail": f"环境风险评分 {risk:.2f}",
        }

    # ======================== 世界观接口：挑战 ========================
    def challenge(self, other_proposal: Dict, my_worldview=None) -> Dict:
        """
        从物理主义角度挑战其他智能体的提案。
        当前环境恶化时，直接否决高风险交易方向。
        :return: {'veto': bool, 'reason': str}
        """
        # 刷新最新环境状态
        self.evaluate()
        risk = self._current_risk_score

        # 环境健康时通过
        if risk < 0.4:
            return {"veto": False, "reason": "环境物理指标正常"}

        # 环境轻度恶化：仅否决高杠杆或大仓位建议
        if risk < 0.7:
            proposal_dir = other_proposal.get("direction", 0)
            if proposal_dir != 0:
                return {
                    "veto": True,
                    "reason": f"环境风险 {risk:.2f}，物理世界不支持新开仓位",
                }
            return {"veto": False, "reason": "风险可控"}

        # 环境严重恶化：无条件否决
        return {
            "veto": True,
            "reason": f"物理环境严重恶化 (风险 {risk:.2f})，所有交易提案必须暂停",
        }

    # ======================== 主诊断评估 ========================
    async def evaluate_async(self) -> Dict[str, Any]:
        """异步执行环境检查（供外部调用）"""
        now = time.time()
        if now - self._last_check < self.check_interval:
            return {"status": "throttled"}
        self._last_check = now
        return self.evaluate()

    def evaluate(self) -> Dict[str, Any]:
        """执行一次全面的硬件环境诊断（同步，供内部使用）"""
        now = time.time()
        if now - self._last_check < self.check_interval:
            return {"status": "throttled"}
        self._last_check = now

        alerts: List[EnvAlert] = []
        self._run_checks(alerts)

        # 计算综合风险评分
        self._current_risk_score = self._calc_risk_score(alerts)

        # 推送告警
        for alert in alerts:
            self._emit_alert(alert)

        if self.behavior_log:
            self.behavior_log.log(
                EventType.SYSTEM, "EnvInspector",
                f"环境检查完成，告警: {len(alerts)}, 风险评分: {self._current_risk_score:.2f}"
            )

        if len(self._alerts) > 200:
            self._alerts = self._alerts[-200:]

        return {
            "status": "WARNING" if alerts else "OK",
            "alerts": [self._alert_to_dict(a) for a in alerts[-10:]],
            "risk_score": self._current_risk_score,
            "timestamp": datetime.now().isoformat(),
        }

    def _calc_risk_score(self, alerts: List[EnvAlert]) -> float:
        """从告警等级计算综合环境风险得分"""
        if not alerts:
            return 0.0
        weights = {EventLevel.CRITICAL: 0.4, EventLevel.WARN: 0.2, EventLevel.INFO: 0.05}
        score = sum(weights.get(a.level, 0.05) for a in alerts)
        return min(1.0, score)

    # ======================== 具体检查项 ========================
    def _run_checks(self, alerts: List[EnvAlert]) -> None:
        self._check_cpu(alerts)
        self._check_memory(alerts)
        self._check_disk(alerts)
        self._check_network(alerts)
        self._check_clock(alerts)
        self._check_shm(alerts)

    def _check_cpu(self, alerts: List[EnvAlert]) -> None:
        cpu_pct = psutil.cpu_percent(interval=0.1)
        if cpu_pct > 90:
            alerts.append(EnvAlert(
                level=EventLevel.WARN,
                source="CPU使用率",
                message=f"CPU 使用率 {cpu_pct:.1f}% 超过 90%",
                suggestion="检查是否有异常进程占用"
            ))
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
        freq = psutil.cpu_freq()
        if freq and freq.max and freq.current < freq.max * 0.5:
            alerts.append(EnvAlert(
                level=EventLevel.WARN,
                source="CPU频率",
                message=f"CPU 降频: 当前 {freq.current}MHz, 最大 {freq.max}MHz",
                suggestion="检查电源模式与散热"
            ))

    def _check_memory(self, alerts: List[EnvAlert]) -> None:
        mem = psutil.virtual_memory()
        if mem.percent > 90:
            alerts.append(EnvAlert(
                level=EventLevel.WARN,
                source="内存使用率",
                message=f"内存使用 {mem.percent:.1f}% (剩余 {mem.available/(1024**3):.1f}GB)",
                suggestion="考虑增加内存或关闭非必要服务"
            ))
        swap = psutil.swap_memory()
        if swap.used > 100 * 1024 * 1024:
            alerts.append(EnvAlert(
                level=EventLevel.WARN,
                source="SWAP使用",
                message=f"SWAP 已使用 {swap.used/(1024**2):.0f}MB，可能导致性能下降",
                suggestion="检查内存压力，避免频繁换页"
            ))
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

    def _check_network(self, alerts: List[EnvAlert]) -> None:
        try:
            stats = psutil.net_io_counters(pernic=True)
            for iface, s in stats.items():
                if iface == 'lo':
                    continue
                drop_pct = (s.dropin + s.dropout) / max(s.packets_recv + s.packets_sent, 1) * 100
                self._net_drop_history.append(drop_pct)
                if drop_pct > 1.0:
                    alerts.append(EnvAlert(
                        level=EventLevel.WARN,
                        source="网络丢包",
                        message=f"网卡 {iface} 丢包率 {drop_pct:.2f}%",
                        suggestion="检查物理链路与交换机"
                    ))
        except Exception:
            pass

    def _check_clock(self, alerts: List[EnvAlert]) -> None:
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
            pass

    def _check_shm(self, alerts: List[EnvAlert]) -> None:
        try:
            import subprocess
            subprocess.run(['ipcs', '-m', '-u'], capture_output=True, text=True, timeout=2)
            # 解析占位，生产可进一步分析
        except Exception:
            pass

    # ======================== 告警处理 ========================
    def _emit_alert(self, alert: EnvAlert) -> None:
        self._alerts.append(alert)
        key = f"{alert.source}_{alert.level.value}"
        self._consecutive_issues[key] = self._consecutive_issues.get(key, 0) + 1
        if self._consecutive_issues[key] % 3 != 0:
            return
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

    def get_recent_alerts(self, limit: int = 20) -> List[Dict]:
        return [self._alert_to_dict(a) for a in self._alerts[-limit:]]

    # ======================== 独立运行循环 (兼容旧调用) ========================
    async def run_loop(self) -> None:
        while True:
            await self.evaluate_async()
            await asyncio.sleep(self.check_interval)
