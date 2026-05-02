#!/usr/bin/env python3
"""
火种系统 (FireSeed) 监察者智能体 (Sentinel) —— 机械唯物主义
=============================================================
全天候监控系统运行状态，基于可量化的物理指标判断系统健康。
世界观：机械唯物主义——系统是可分解为独立组件的钟表，故障可定位。
核心职责：
- CPU、内存、磁盘、网络、时钟等硬件/OS指标实时采集
- 关键进程（引擎、C++守护、Redis、Docker）存活检测
- 数据库、共享内存、配置文件等基础设施完整性扫描
- 与数据监察员（经验主义）进行对抗性交叉验证
- 当物理指标异常时，即使数据监察员未报警，亦独立触发告警
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

from agents.worldview import WorldViewAgent, WorldViewManifesto, WorldView
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier

logger = logging.getLogger("fire_seed.sentinel")

@dataclass
class SystemAlert:
    """系统级告警条目"""
    timestamp: datetime = field(default_factory=datetime.now)
    level: EventLevel = EventLevel.INFO
    source: str = ""
    message: str = ""
    suggestion: str = ""

class SentinelAgent(WorldViewAgent):
    """
    监察者智能体。基于机械唯物主义世界观：将所有系统状态视为可测量的物理量。
    """

    def __init__(self,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None,
                 check_interval_sec: int = 15):
        manifesto = WorldViewManifesto(
            worldview=WorldView.MECHANICAL_MATERIALISM,
            core_belief="系统是可分解为独立组件的钟表，故障可定位",
            primary_optimization_target="异常检测F1最大化",
            adversary_worldview=WorldView.DATA_EMPIRICISM,  # 对立：数据监察员
            time_scale="15s",
            forbidden_data_source={"RAW_MARKET_DATA", "STRATEGY_SIGNAL"},
        )
        super().__init__(manifesto)

        self.behavior_log = behavior_log
        self.notifier = notifier
        self.check_interval = check_interval_sec

        self._alerts: List[SystemAlert] = []
        self._last_check = 0.0
        self._consecutive_issues: Dict[str, int] = {}

        # 历史数据（用于趋势分析）
        self._cpu_history: deque = deque(maxlen=60)
        self._mem_history: deque = deque(maxlen=60)

        logger.info("监察者（机械唯物主义）初始化完成")

    # ======================== 世界观接口实现 ========================
    def propose(self, perception: Optional[Dict] = None) -> Dict:
        """
        基于机械唯物主义世界观提出系统健康状态评估。
        返回方向：1=健康状况良好可继续交易，-1=建议暂停，0=中性
        """
        report = self._run_checks()
        # 如果存在 ERROR 或 WARNING，则建议暂停或谨慎
        error_count = sum(1 for a in report["alerts"] if a["level"] == "ERROR")
        warn_count = sum(1 for a in report["alerts"] if a["level"] == "WARNING")
        direction = -1 if error_count > 0 else 1 if warn_count == 0 else 0
        return {
            "direction": direction,
            "confidence": 1.0 if error_count > 0 else 0.7,
            "detail": report,
            "worldview": self.manifesto.worldview.value,
        }

    def challenge(self, other_proposal: Dict, my_worldview: WorldView) -> Dict:
        """
        挑战其他智能体的提案。基于物理指标质疑任何忽略硬件风险的决策。
        """
        my_state = self._run_checks()
        # 如果系统有物理异常，则必须挑战忽略该异常的提案
        has_physical_error = any(a["level"] in ("ERROR", "CRITICAL") for a in my_state["alerts"])
        veto = has_physical_error and other_proposal.get("direction", 0) != 0
        return {
            "veto": veto,
            "reason": "物理指标异常，禁止高风险操作" if veto else "物理状态正常",
            "my_state": my_state["summary"],
            "worldview": my_worldview.value,
        }

    # ======================== 主检查入口 ========================
    async def evaluate(self) -> Dict:
        """
        执行一次完整系统体检，并写入行为日志，进行对抗性交叉校验。
        """
        now = time.time()
        if now - self._last_check < self.check_interval:
            return {"status": "throttled"}
        self._last_check = now

        report = self._run_checks()
        # 写入行为日志
        if self.behavior_log:
            self.behavior_log.log(EventType.SYSTEM, "Sentinel",
                                  f"系统体检完成，评分={report['score']}，告警={len(report['alerts'])}")
        # 推送告警
        for alert in report["alerts"]:
            self._emit_alert(SystemAlert(
                level=EventLevel[alert["level"]],
                source=alert["source"],
                message=alert["message"],
                suggestion=alert.get("suggestion", "")
            ))
        # 与数据监察员进行对抗性交叉验证
        await self._cross_verify_with_data_ombudsman(report)
        return report

    def _run_checks(self) -> Dict:
        """执行所有物理检查并返回报告"""
        alerts: List[Dict] = []
        self._check_cpu(alerts)
        self._check_memory(alerts)
        self._check_disk(alerts)
        self._check_network(alerts)
        self._check_processes(alerts)
        self._check_filesystem(alerts)
        self._check_database(alerts)
        self._check_shm(alerts)
        self._check_clock(alerts)

        error_count = sum(1 for a in alerts if a["level"] in ("ERROR", "CRITICAL"))
        warn_count = sum(1 for a in alerts if a["level"] == "WARNING")
        score = max(0, 100 - error_count * 20 - warn_count * 5)
        return {
            "score": score,
            "summary": f"物理状态: {score}分, 错误{error_count}, 警告{warn_count}",
            "alerts": alerts,
            "timestamp": datetime.now().isoformat(),
        }

    # --------------------- 各检查项 ---------------------
    def _check_cpu(self, alerts: List[Dict]) -> None:
        pct = psutil.cpu_percent(interval=0.1)
        self._cpu_history.append(pct)
        if pct > 90:
            alerts.append({"level": "WARNING", "source": "CPU", "message": f"使用率 {pct:.1f}%",
                           "suggestion": "检查异常进程"})
        freq = psutil.cpu_freq()
        if freq and freq.max and freq.current < freq.max * 0.5:
            alerts.append({"level": "WARNING", "source": "CPU降频",
                           "message": f"当前 {freq.current}MHz, 最大 {freq.max}MHz",
                           "suggestion": "检查散热与电源策略"})

    def _check_memory(self, alerts: List[Dict]) -> None:
        mem = psutil.virtual_memory()
        self._mem_history.append(mem.percent)
        if mem.percent > 90:
            alerts.append({"level": "WARNING", "source": "内存", "message": f"使用率 {mem.percent:.1f}%",
                           "suggestion": "释放或增加内存"})
        swap = psutil.swap_memory()
        if swap.used > 100 * 1024 * 1024:
            alerts.append({"level": "WARNING", "source": "SWAP使用",
                           "message": f"已使用 {swap.used//1024//1024}MB",
                           "suggestion": "内存压力过大"})

    def _check_disk(self, alerts: List[Dict]) -> None:
        for part in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(part.mountpoint)
                if usage.percent > 85:
                    level = "CRITICAL" if usage.percent > 95 else "WARNING"
                    alerts.append({"level": level, "source": f"磁盘 {part.mountpoint}",
                                   "message": f"使用率 {usage.percent:.1f}%",
                                   "suggestion": "清理或扩容"})
            except Exception:
                pass

    def _check_network(self, alerts: List[Dict]) -> None:
        try:
            stats = psutil.net_io_counters(pernic=True)
            for iface, s in stats.items():
                if iface == 'lo':
                    continue
                drop_rate = (s.dropin + s.dropout) / max(s.packets_recv + s.packets_sent, 1) * 100
                if drop_rate > 1.0:
                    alerts.append({"level": "WARNING", "source": f"网络 {iface}",
                                   "message": f"丢包率 {drop_rate:.2f}%",
                                   "suggestion": "检查物理链路"})
        except Exception:
            pass

    def _check_processes(self, alerts: List[Dict]) -> None:
        # 检查 redis-server
        redis_ok = any('redis-server' in p.info['name'] for p in psutil.process_iter(['name']))
        if not redis_ok:
            alerts.append({"level": "ERROR", "source": "Redis", "message": "进程未运行",
                           "suggestion": "启动 redis-server"})
        # 检查 docker
        docker_ok = any('dockerd' in p.info['name'] for p in psutil.process_iter(['name']))
        if not docker_ok:
            alerts.append({"level": "WARNING", "source": "Docker", "message": "进程未运行",
                           "suggestion": "沙箱编译需要 Docker"})

    def _check_filesystem(self, alerts: List[Dict]) -> None:
        critical_files = ["config/settings.yaml", "config/risk_limits.yaml"]
        for cf in critical_files:
            if not os.path.exists(cf):
                alerts.append({"level": "ERROR", "source": "配置文件", "message": f"{cf} 缺失"})

    def _check_database(self, alerts: List[Dict]) -> None:
        try:
            import sqlite3
            conn = sqlite3.connect("data/fire_seed.db")
            conn.execute("SELECT 1")
            conn.close()
        except Exception as e:
            alerts.append({"level": "ERROR", "source": "数据库", "message": f"连接失败: {e}",
                           "suggestion": "检查数据库文件权限"})

    def _check_shm(self, alerts: List[Dict]) -> None:
        shm_path = "/dev/shm/fire_seed_queue"
        if os.path.exists(shm_path):
            # 可选：检查大小、读写权限等
            pass
        # 不报警，仅记录

    def _check_clock(self, alerts: List[Dict]) -> None:
        try:
            import subprocess
            result = subprocess.run(['timedatectl', 'show', '-p', 'NTPSynchronized'],
                                    capture_output=True, text=True, timeout=2)
            if 'no' in result.stdout.lower():
                alerts.append({"level": "WARNING", "source": "时钟同步",
                               "message": "NTP 未同步",
                               "suggestion": "启用 NTP 保证时间精度"})
        except Exception:
            pass

    # ======================== 告警推送 ========================
    def _emit_alert(self, alert: SystemAlert) -> None:
        self._alerts.append(alert)
        if len(self._alerts) > 200:
            self._alerts.pop(0)
        key = f"{alert.source}_{alert.level.value}"
        self._consecutive_issues[key] = self._consecutive_issues.get(key, 0) + 1
        if self._consecutive_issues[key] % 3 != 0:
            return
        if self.notifier:
            asyncio.ensure_future(self.notifier.send_alert(
                level=alert.level.value,
                title=f"监察者 [{alert.source}]",
                body=f"{alert.message}\n建议: {alert.suggestion}"
            ))

    # ======================== 对抗性校验 ========================
    async def _cross_verify_with_data_ombudsman(self, my_report: Dict) -> None:
        """
        与数据监察员进行对抗性校验。
        当系统物理指标正常但数据质量异常时，本智能体不松口；反之亦然。
        """
        try:
            from api.server import get_engine
            engine = get_engine()
            if engine and hasattr(engine, 'agent_registry'):
                data_ombudsman = engine.agent_registry.get("data_ombudsman")
                if data_ombudsman:
                    data_status = data_ombudsman.get_status()
                    data_health = data_status.get("global_health_score", 100)
                    sys_errors = sum(1 for a in my_report["alerts"] if a["level"] in ("ERROR", "CRITICAL"))
                    # 物理正常但数据异常
                    if sys_errors == 0 and data_health < 60:
                        logger.warning("对抗性校验：物理正常但数据监察员报告异常，可能存在隐蔽的数据源问题")
                    # 物理异常但数据正常
                    if sys_errors > 0 and data_health > 90:
                        logger.warning("对抗性校验：数据源正常但物理硬件异常，需优先处理硬件问题")
        except Exception as e:
            logger.debug(f"对抗性校验暂不可用: {e}")

    # ======================== 查询接口 ========================
    def get_status(self) -> Dict:
        """返回监察者当前状态"""
        return {
            "alerts_count": len(self._alerts),
            "last_check": datetime.fromtimestamp(self._last_check).isoformat() if self._last_check else None,
            "recent_alerts": [{"time": a.timestamp.isoformat(), "source": a.source, "msg": a.message}
                              for a in self._alerts[-5:]],
        }

    async def run_loop(self) -> None:
        while True:
            await self.evaluate()
            await asyncio.sleep(self.check_interval)
