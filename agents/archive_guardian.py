#!/usr/bin/env python3
"""
火种系统 (FireSeed) 归档审计官智能体 (ArchiveGuardian) — 历史主义
=====================================================================
全天候监控冷存储与数据完整性，确保关键数据安全。
世界观：历史主义 — 一切当前状态都可以从历史痕迹中理解，遗忘是系统最大的敌人。
"""

import asyncio
import hashlib
import logging
import os
import sqlite3
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from api.server import get_engine
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier
from agents.worldview import WorldView, WorldViewAgent, WorldViewManifesto

logger = logging.getLogger("fire_seed.archive_guardian")


# ------------------------------------------------------------
# 历史主义世界观宣言
# ------------------------------------------------------------
ARCHIVE_GUARDIAN_MANIFESTO = WorldViewManifesto(
    worldview=WorldView.HISTORICISM,
    core_belief="一切当前状态都可以从历史痕迹中理解，遗忘是系统最大的敌人",
    primary_optimization_target="archived_data / total_data",
    adversary_worldview=WorldView.MECHANICAL_MATERIALISM,
    forbidden_data_source={"REAL_TIME_ANYTHING"},
    exclusive_data_source={"FILE_SYSTEM", "DATABASE_METADATA", "COLD_STORAGE_LOGS"},
    time_scale="1h",
)


@dataclass
class ArchiveAlert:
    """归档告警条目"""
    timestamp: datetime = field(default_factory=datetime.now)
    level: EventLevel = EventLevel.INFO
    category: str = ""
    message: str = ""
    suggestion: str = ""


class ArchiveGuardian(WorldViewAgent):
    """
    归档审计官智能体 — 历史主义。

    监控维度：
    - 数据库完整性（行为日志、ELO排名、订单记录）
    - 冷存储上传成功率
    - 日志轮转与磁盘空间
    - 关键知识库备份状态
    - 长期趋势异常检测
    """

    def __init__(self,
                 root_dir: str = ".",
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None,
                 check_interval_sec: int = 3600):  # 每小时检查一次
        super().__init__(ARCHIVE_GUARDIAN_MANIFESTO)
        self.root = Path(root_dir)
        self.behavior_log = behavior_log
        self.notifier = notifier
        self.check_interval = check_interval_sec

        # 关键数据库文件列表（相对于根目录）
        self.critical_databases = [
            "data/logs/behavior_*.db",
            "data/fire_seed.db",
            "data/elo_rankings.db",
            "data/archive_index.db",
            "data/human_feedback.db",
        ]

        # 关键知识库文件
        self.critical_knowledge = [
            "config/weights.yaml",
            "config/human_constraints.yaml",
            "brain/evolution/pareto_frontier.db",
        ]

        # 冷存储配置
        self.cold_storage_config_path = self.root / "config" / "cloud_storage.yaml"

        # 告警历史
        self._alerts: List[ArchiveAlert] = []
        # 数据库大小历史（检测膨胀）
        self._db_size_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=30))
        # 上次检查时间
        self._last_check = 0.0
        # 连续异常计数器
        self._consecutive_issues: Dict[str, int] = {}

    # ======================== 世界观接口实现 ========================
    def propose(self, perception: Dict) -> Dict:
        """基于历史主义视角提出建议：检查哪些数据有被遗忘的风险"""
        # 探测最近未更新的文件
        stale_files = self._detect_stale_files()
        lost_data_risk = 1.0 if stale_files else 0.0

        return {
            "direction": -1 if lost_data_risk > 0.3 else 0,
            "confidence": min(1.0, lost_data_risk),
            "reason": f"发现 {len(stale_files)} 个可能被遗忘的数据文件" if stale_files else "数据记忆完好",
            "stale_files": stale_files,
        }

    def challenge(self, other_proposal: Dict, my_worldview: WorldView) -> Dict:
        """从历史主义角度挑战其他提案：该决策是否忽略了历史教训？"""
        # 以最近归档失败为例，指出任何忽略持久化的行为
        recent_failures = [a for a in self._alerts if a.level == EventLevel.CRITICAL]
        veto = len(recent_failures) > 0 and other_proposal.get("direction", 0) != 0

        return {
            "veto": veto,
            "reason": "历史数据显示关键数据丢失风险，不宜执行新操作" if veto else "历史记录正常，不反对",
            "confidence": len(recent_failures) / 10.0,
        }

    def _detect_stale_files(self) -> List[str]:
        """检测长时间未修改的关键文件"""
        stale = []
        threshold = datetime.now() - timedelta(days=7)
        for pattern in self.critical_databases:
            for db_file in self.root.glob(pattern):
                if db_file.exists():
                    mtime = datetime.fromtimestamp(db_file.stat().st_mtime)
                    if mtime < threshold:
                        stale.append(str(db_file.relative_to(self.root)))
        return stale

    # ======================== 主入口 ========================
    async def evaluate(self) -> Dict[str, Any]:
        """执行完整的归档健康审计"""
        now = time.time()
        if now - self._last_check < self.check_interval:
            return {"status": "throttled"}
        self._last_check = now

        alerts: List[ArchiveAlert] = []

        # 1. 数据库完整性检查
        await self._check_database_integrity(alerts)

        # 2. 冷存储状态检查
        await self._check_cold_storage(alerts)

        # 3. 日志轮转与磁盘空间
        self._check_log_rotation_and_disk(alerts)

        # 4. 关键知识库备份验证
        self._check_knowledge_backups(alerts)

        # 5. 数据库膨胀检测
        self._check_database_bloat(alerts)

        # 推送告警
        for alert in alerts:
            self._emit_alert(alert)

        # 记录行为日志
        if self.behavior_log:
            self.behavior_log.log(
                EventType.AGENT, "ArchiveGuardian",
                f"归档审计完成，告警: {len(alerts)}",
                snapshot={"alert_count": len(alerts)}
            )

        return {
            "status": "WARNING" if alerts else "OK",
            "alert_count": len(alerts),
            "alerts": [self._alert_to_dict(a) for a in alerts[-10:]],
            "timestamp": datetime.now().isoformat()
        }

    # ======================== 数据库完整性 ========================
    async def _check_database_integrity(self, alerts: List[ArchiveAlert]) -> None:
        """检查关键数据库文件是否存在且可正常读写"""
        for pattern in self.critical_databases:
            for db_file in self.root.glob(pattern):
                if not db_file.exists():
                    continue
                try:
                    conn = sqlite3.connect(str(db_file))
                    cursor = conn.execute("PRAGMA quick_check")
                    result = cursor.fetchone()
                    conn.close()

                    if result and result[0] != "ok":
                        alerts.append(ArchiveAlert(
                            level=EventLevel.CRITICAL,
                            category="数据库完整性",
                            message=f"{db_file.name} 完整性检查失败: {result[0]}",
                            suggestion="立即从备份恢复"
                        ))
                    else:
                        # 记录数据库大小
                        size_bytes = db_file.stat().st_size
                        self._db_size_history[db_file.name].append(size_bytes)
                        # 记录最近修改时间
                        mtime = datetime.fromtimestamp(db_file.stat().st_mtime)
                        age_hours = (datetime.now() - mtime).total_seconds() / 3600
                        if age_hours > 24 and "behavior" in db_file.name:
                            alerts.append(ArchiveAlert(
                                level=EventLevel.WARN,
                                category="数据库时效性",
                                message=f"{db_file.name} 超过 {age_hours:.0f} 小时未更新",
                                suggestion="检查行为日志写入是否正常"
                            ))
                except sqlite3.OperationalError as e:
                    alerts.append(ArchiveAlert(
                        level=EventLevel.CRITICAL,
                        category="数据库连接",
                        message=f"无法打开 {db_file.name}: {e}",
                        suggestion="数据库可能已损坏"
                    ))
                except Exception as e:
                    logger.warning(f"检查数据库 {db_file} 时出错: {e}")

    # ======================== 冷存储检查 ========================
    async def _check_cold_storage(self, alerts: List[ArchiveAlert]) -> None:
        """检查冷存储是否正常工作"""
        if not self.cold_storage_config_path.exists():
            return

        try:
            with open(self.cold_storage_config_path, 'r') as f:
                cfg = yaml.safe_load(f)

            provider = cfg.get("provider", "")
            if not provider:
                alerts.append(ArchiveAlert(
                    level=EventLevel.WARN,
                    category="冷存储配置",
                    message="未配置冷存储提供商",
                    suggestion="在 config/cloud_storage.yaml 中启用冷存储"
                ))
                return

            # 检查归档索引数据库
            index_db = self.root / "data" / "archive_index.db"
            if index_db.exists():
                conn = sqlite3.connect(str(index_db))
                cursor = conn.execute(
                    "SELECT COUNT(*), MAX(uploaded_at) FROM archive_index"
                )
                count, last_upload = cursor.fetchone()
                conn.close()

                if count == 0:
                    alerts.append(ArchiveAlert(
                        level=EventLevel.INFO,
                        category="冷存储",
                        message="归档索引为空，尚无文件上传",
                        suggestion="等待首次归档完成"
                    ))
                elif last_upload:
                    last_time = datetime.fromtimestamp(float(last_upload))
                    hours_since = (datetime.now() - last_time).total_seconds() / 3600
                    if hours_since > 48:
                        alerts.append(ArchiveAlert(
                            level=EventLevel.WARN,
                            category="冷存储时效",
                            message=f"最近一次归档上传在 {hours_since:.0f} 小时前",
                            suggestion="检查冷存储服务的网络连接与凭证"
                        ))
        except Exception as e:
            logger.warning(f"冷存储检查失败: {e}")

    # ======================== 日志轮转与磁盘 ========================
    def _check_log_rotation_and_disk(self, alerts: List[ArchiveAlert]) -> None:
        """检查日志目录的磁盘占用与轮转状态"""
        log_dir = self.root / "logs"
        if not log_dir.exists():
            return

        total_size = sum(f.stat().st_size for f in log_dir.rglob("*") if f.is_file())
        total_mb = total_size / (1024 * 1024)

        if total_mb > 5000:  # 超过 5GB
            alerts.append(ArchiveAlert(
                level=EventLevel.WARN,
                category="日志膨胀",
                message=f"日志目录占用 {total_mb:.0f}MB，可能影响磁盘性能",
                suggestion="检查 logrotate 配置或手动清理旧日志"
            ))

        # 检查是否有超过30天未轮转的日志文件
        thirty_days_ago = datetime.now() - timedelta(days=30)
        old_files = [
            f for f in log_dir.rglob("*")
            if f.is_file() and datetime.fromtimestamp(f.stat().st_mtime) < thirty_days_ago
        ]
        if len(old_files) > 10:
            alerts.append(ArchiveAlert(
                level=EventLevel.INFO,
                category="日志轮转",
                message=f"发现 {len(old_files)} 个超过30天未动的日志文件",
                suggestion="可考虑手动归档或删除"
            ))

    # ======================== 知识库备份验证 ========================
    def _check_knowledge_backups(self, alerts: List[ArchiveAlert]) -> None:
        """检查关键知识库文件是否有近期的备份"""
        for rel_path in self.critical_knowledge:
            full_path = self.root / rel_path
            if not full_path.exists():
                continue

            # 检查是否存在备份（假设备份在 versions/ 或对象存储）
            backup_dir = self.root / "versions"
            if backup_dir.exists():
                found = list(backup_dir.glob(f"{full_path.name}*"))
                if not found:
                    alerts.append(ArchiveAlert(
                        level=EventLevel.WARN,
                        category="知识库备份",
                        message=f"{full_path.name} 未找到备份副本",
                        suggestion="立即执行手动备份"
                    ))
            else:
                alerts.append(ArchiveAlert(
                    level=EventLevel.WARN,
                    category="备份目录",
                    message="versions/ 备份目录不存在",
                    suggestion="创建备份目录并配置定期备份"
                ))

    # ======================== 数据库膨胀检测 ========================
    def _check_database_bloat(self, alerts: List[ArchiveAlert]) -> None:
        """检测数据库是否存在异常膨胀趋势"""
        for db_name, size_history in self._db_size_history.items():
            if len(size_history) < 7:  # 至少7个数据点
                continue
            # 计算最近的增长速率
            recent = list(size_history)[-7:]
            growth_rate = (recent[-1] - recent[0]) / (recent[0] + 1)
            if growth_rate > 0.5:  # 7天内增长超过50%
                alerts.append(ArchiveAlert(
                    level=EventLevel.WARN,
                    category="数据库膨胀",
                    message=f"{db_name} 在7天内增长了 {growth_rate*100:.0f}%",
                    suggestion="检查是否积累了异常数据，考虑执行 VACUUM"
                ))

    # ======================== 告警处理 ========================
    def _emit_alert(self, alert: ArchiveAlert) -> None:
        self._alerts.append(alert)
        if len(self._alerts) > 200:
            self._alerts = self._alerts[-200:]

        key = f"{alert.category}"
        self._consecutive_issues[key] = self._consecutive_issues.get(key, 0) + 1
        if self._consecutive_issues[key] % 3 != 0:
            return

        if self.notifier:
            asyncio.ensure_future(
                self.notifier.send_alert(
                    level=alert.level.value,
                    title=f"归档审计 [{alert.category}]",
                    body=f"{alert.message}\n建议: {alert.suggestion}"
                )
            )

    @staticmethod
    def _alert_to_dict(alert: ArchiveAlert) -> Dict[str, Any]:
        return {
            "timestamp": alert.timestamp.isoformat(),
            "level": alert.level.value,
            "category": alert.category,
            "message": alert.message,
        }

    def get_status(self) -> Dict[str, Any]:
        return {
            "last_check": datetime.fromtimestamp(self._last_check).isoformat() if self._last_check else None,
            "alerts_count": len(self._alerts),
            "recent_alerts": [self._alert_to_dict(a) for a in self._alerts[-5:]],
        }

    async def run_loop(self) -> None:
        while True:
            await self.evaluate()
            await asyncio.sleep(self.check_interval)
