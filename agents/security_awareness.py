#!/usr/bin/env python3
"""
火种系统 (FireSeed) 安全态势感知官智能体 (SecurityAwareness)
===============================================================
世界观：安全悲观主义 (Security Pessimism)
“假设系统一直在被攻击，只是尚未发现。”

核心职责：
- 实时监控登录失败频率与来源IP，识别暴力破解
- 检测API密钥在非交易时段的异常调用
- 监控核心配置文件的非授权修改（校验哈希/时间戳）
- 门限签名分片可用性检查
- JWT令牌异常刷新行为检测
- 生成安全态势评分并推送高危告警
"""

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from api.server import get_engine
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier

logger = logging.getLogger("fire_seed.security_awareness")


# ======================== 数据结构 ========================
@dataclass
class IPAlert:
    """IP异常告警记录"""
    ip: str
    fail_count: int
    first_seen: datetime
    last_seen: datetime
    blocked: bool = False


@dataclass
class SecurityAlert:
    """安全告警条目"""
    timestamp: datetime = field(default_factory=datetime.now)
    level: EventLevel = EventLevel.INFO
    category: str = ""
    message: str = ""
    evidence: str = ""
    suggested_action: str = ""


class SecurityAwareness:
    """
    安全态势感知官智能体。

    监控维度：
    - 认证安全：登录失败率、来源IP、暴力破解检测
    - API使用安全：异常时段调用、权限越界
    - 文件完整性：核心配置文件的非授权篡改
    - 门限签名分片：各分片可用性
    - JWT令牌：异常刷新频率检测
    """

    def __init__(self,
                 root_dir: str = ".",
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None,
                 check_interval_sec: int = 30):
        self.root = Path(root_dir)
        self.behavior_log = behavior_log
        self.notifier = notifier
        self.check_interval = check_interval_sec

        # 敏感配置文件路径及已知哈希（首次加载时计算，可定期刷新）
        self.sensitive_files: Dict[str, str] = {
            "config/settings.yaml": "",
            "config/risk_limits.yaml": "",
            "config/auth.yaml": "",
            "config/multi_account.yaml": "",
        }
        self._load_sensitive_file_hashes()

        # IP跟踪
        self.ip_alerts: Dict[str, IPAlert] = {}
        # 告警列表
        self._alerts: List[SecurityAlert] = []

        # 状态追踪
        self._last_check = 0.0
        self._consecutive_anomalies = 0

        logger.info("安全态势感知官初始化完成，世界观：安全悲观主义")

    def _load_sensitive_file_hashes(self) -> None:
        """计算敏感文件的初始哈希"""
        for rel_path in self.sensitive_files:
            full_path = self.root / rel_path
            if full_path.exists():
                self.sensitive_files[rel_path] = self._sha256_file(full_path)
            else:
                self.sensitive_files[rel_path] = "FILE_NOT_FOUND"

    @staticmethod
    def _sha256_file(filepath: Path) -> str:
        """计算文件的SHA-256哈希"""
        sha = hashlib.sha256()
        try:
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    sha.update(chunk)
            return sha.hexdigest()
        except Exception:
            return ""

    # ======================== 主入口 ========================
    async def evaluate(self) -> Dict[str, Any]:
        """执行完整的安全态势评估"""
        now = time.time()
        if now - self._last_check < self.check_interval:
            return {"status": "throttled"}
        self._last_check = now

        alerts: List[SecurityAlert] = []

        engine = get_engine()

        # 1. 登录安全性检查
        await self._check_login_security(engine, alerts)

        # 2. API使用异常检测
        await self._check_api_abuse(engine, alerts)

        # 3. 文件完整性检查
        self._check_file_integrity(alerts)

        # 4. 门限签名分片检查
        await self._check_threshold_shards(engine, alerts)

        # 5. JWT令牌异常检测
        await self._check_jwt_anomalies(engine, alerts)

        # 6. 综合态势评分
        score = self._compute_security_score()

        # 推送告警
        for alert in alerts:
            self._emit_alert(alert)

        if self.behavior_log:
            self.behavior_log.log(
                EventType.AGENT, "SecurityAwareness",
                f"安全态势评估完成，评分 {score:.0f}，告警 {len(alerts)}",
                snapshot={"score": score, "alerts": len(alerts)}
            )

        return {
            "security_score": score,
            "alert_count": len(alerts),
            "alerts": [self._alert_to_dict(a) for a in alerts[-5:]],
            "timestamp": datetime.now().isoformat()
        }

    # ======================== 登录安全 ========================
    async def _check_login_security(self, engine, alerts: List[SecurityAlert]) -> None:
        """监控登录失败频率与IP来源"""
        # 优先从行为日志获取，其次从auth模块
        if self.behavior_log:
            recent = self.behavior_log.query_db(
                start_time=datetime.now() - timedelta(minutes=10),
                event_type="认证操作",
                limit=500
            )
            failures = [e for e in recent if "失败" in e.get("content", "")]
            ip_counts: Dict[str, int] = {}
            for entry in failures:
                # 尝试提取IP（需要日志中包含IP信息）
                ip = entry.get("snapshot", {}).get("ip", "unknown")
                ip_counts[ip] = ip_counts.get(ip, 0) + 1

            # 检查IP异常
            for ip, count in ip_counts.items():
                if ip == "unknown":
                    continue
                if count >= 5:  # 10分钟内5次失败
                    if ip not in self.ip_alerts:
                        self.ip_alerts[ip] = IPAlert(
                            ip=ip, fail_count=count,
                            first_seen=datetime.now(), last_seen=datetime.now()
                        )
                    else:
                        self.ip_alerts[ip].fail_count += count
                        self.ip_alerts[ip].last_seen = datetime.now()

                    if self.ip_alerts[ip].fail_count >= 10:
                        alerts.append(SecurityAlert(
                            level=EventLevel.CRITICAL,
                            category="暴力破解",
                            message=f"IP {ip} 在10分钟内登录失败 {self.ip_alerts[ip].fail_count} 次",
                            evidence=f"首次出现: {self.ip_alerts[ip].first_seen.isoformat()}",
                            suggested_action="建议立即封禁该IP，检查防火墙与fail2ban规则"
                        ))
                    else:
                        alerts.append(SecurityAlert(
                            level=EventLevel.WARN,
                            category="登录异常",
                            message=f"IP {ip} 登录失败 {count} 次",
                            suggested_action="观察趋势，若继续增加则封禁"
                        ))

            # 全局失败率
            if len(failures) > 20:
                alerts.append(SecurityAlert(
                    level=EventLevel.WARN,
                    category="认证风暴",
                    message=f"近10分钟登录失败 {len(failures)} 次",
                    suggested_action="可能存在分布式暴力破解，启用全局IP限流"
                ))

        # 清理过期的IP记录（超过1小时未活动）
        now = datetime.now()
        expired_ips = [ip for ip, rec in self.ip_alerts.items()
                       if (now - rec.last_seen) > timedelta(hours=1)]
        for ip in expired_ips:
            del self.ip_alerts[ip]

    # ======================== API异常检测 ========================
    async def _check_api_abuse(self, engine, alerts: List[SecurityAlert]) -> None:
        """检测API密钥在非交易时段的异常调用"""
        # 获取当前时段类型（交易/非交易）
        current_hour = datetime.now().hour
        is_trading_hours = 8 <= current_hour <= 23

        # 从行为日志查询API调用记录
        if self.behavior_log:
            recent = self.behavior_log.query_db(
                start_time=datetime.now() - timedelta(hours=1),
                module="Execution",
                limit=200
            )
            if not is_trading_hours and len(recent) > 10:
                alerts.append(SecurityAlert(
                    level=EventLevel.WARN,
                    category="API异常调用",
                    message=f"非交易时段 ({current_hour}点) 出现 {len(recent)} 次API操作",
                    evidence="最近一次: " + (recent[0].get("content", "") if recent else "无"),
                    suggested_action="确认是否为策略自动运行导致，若非预期则立即冻结API密钥"
                ))

            # 检查是否有异常的账户操作（如修改密码、删除密钥等）
            sensitive_ops = [e for e in recent
                             if any(kw in e.get("content", "")
                                    for kw in ["修改密码", "删除密钥", "重置", "白名单"])]
            if sensitive_ops:
                alerts.append(SecurityAlert(
                    level=EventLevel.CRITICAL,
                    category="敏感操作",
                    message=f"检测到敏感操作: {len(sensitive_ops)} 次",
                    evidence=sensitive_ops[0].get("content", ""),
                    suggested_action="立即验证操作合法性，若非本人操作则冻结所有密钥"
                ))

    # ======================== 文件完整性 ========================
    def _check_file_integrity(self, alerts: List[SecurityAlert]) -> None:
        """校验核心配置文件是否被非授权修改"""
        for rel_path, old_hash in self.sensitive_files.items():
            full_path = self.root / rel_path
            if not full_path.exists():
                if old_hash != "FILE_NOT_FOUND":
                    alerts.append(SecurityAlert(
                        level=EventLevel.CRITICAL,
                        category="文件缺失",
                        message=f"核心配置文件消失: {rel_path}",
                        evidence=f"上次哈希: {old_hash[:16]}...",
                        suggested_action="检查文件系统，从备份恢复"
                    ))
                    self.sensitive_files[rel_path] = "FILE_NOT_FOUND"
                continue

            new_hash = self._sha256_file(full_path)
            if old_hash and old_hash != "FILE_NOT_FOUND" and new_hash != old_hash:
                # 检查修改时间是否在最近10分钟内
                mtime = datetime.fromtimestamp(full_path.stat().st_mtime)
                if datetime.now() - mtime < timedelta(minutes=10):
                    alerts.append(SecurityAlert(
                        level=EventLevel.CRITICAL,
                        category="配置篡改",
                        message=f"核心配置文件被修改: {rel_path}",
                        evidence=f"修改时间: {mtime.isoformat()}",
                        suggested_action="对比差异，若非授权操作请立即回滚"
                    ))
                # 更新哈希
                self.sensitive_files[rel_path] = new_hash
            elif not old_hash:
                self.sensitive_files[rel_path] = new_hash

    # ======================== 门限签名 ========================
    async def _check_threshold_shards(self, engine, alerts: List[SecurityAlert]) -> None:
        """检查门限签名各分片的可用性"""
        # 检查 auth.yaml 中的门限签名配置
        auth_config = self._load_auth_config()
        threshold_cfg = auth_config.get("threshold_signature", {})
        if not threshold_cfg.get("enabled", False):
            return  # 未启用

        shard_locations = threshold_cfg.get("shard_locations", [])
        for loc in shard_locations:
            if loc == "local_hsm" and not Path("/dev/hsm").exists():
                alerts.append(SecurityAlert(
                    level=EventLevel.WARN,
                    category="门限签名",
                    message=f"分片存储位置不可用: {loc}",
                    suggested_action="检查硬件安全模块连接"
                ))
            elif loc in ("aliyun_oss", "tencent_cos"):
                # 占位：实际应调用云存储API检查对象是否存在
                pass

    def _load_auth_config(self) -> Dict:
        """加载auth配置"""
        try:
            import yaml
            with open(self.root / "config" / "auth.yaml", "r") as f:
                return yaml.safe_load(f)
        except Exception:
            return {}

    # ======================== JWT异常 ========================
    async def _check_jwt_anomalies(self, engine, alerts: List[SecurityAlert]) -> None:
        """检测JWT令牌的异常刷新行为"""
        if self.behavior_log:
            recent = self.behavior_log.query_db(
                start_time=datetime.now() - timedelta(hours=1),
                module="Auth",
                limit=200
            )
            refreshes = [e for e in recent if "刷新" in e.get("content", "")]
            if len(refreshes) > 50:  # 一小时内超过50次刷新
                alerts.append(SecurityAlert(
                    level=EventLevel.WARN,
                    category="JWT异常",
                    message=f"JWT令牌刷新过于频繁 ({len(refreshes)}次/小时)",
                    suggested_action="检查前端自动刷新间隔，排查令牌泄漏"
                ))

    # ======================== 评分计算 ========================
    def _compute_security_score(self) -> float:
        """综合安全态势评分 (0-100，越高越安全)"""
        score = 100.0
        # 每个高危告警扣15分，中危扣5分
        for alert in self._alerts[-20:]:
            if alert.level == EventLevel.CRITICAL:
                score -= 15.0
            elif alert.level == EventLevel.WARN:
                score -= 5.0
        return max(0.0, min(100.0, score))

    # ======================== 告警处理 ========================
    def _emit_alert(self, alert: SecurityAlert) -> None:
        self._alerts.append(alert)
        if len(self._alerts) > 200:
            self._alerts = self._alerts[-200:]

        if self.notifier and alert.level in (EventLevel.WARN, EventLevel.CRITICAL):
            asyncio.ensure_future(
                self.notifier.send_alert(
                    level=alert.level.value,
                    title=f"安全态势 [{alert.category}]",
                    body=f"{alert.message}\n证据: {alert.evidence}\n建议: {alert.suggested_action}"
                )
            )

    @staticmethod
    def _alert_to_dict(alert: SecurityAlert) -> Dict[str, Any]:
        return {
            "timestamp": alert.timestamp.isoformat(),
            "level": alert.level.value,
            "category": alert.category,
            "message": alert.message,
        }

    def get_status(self) -> Dict[str, Any]:
        return {
            "security_score": self._compute_security_score(),
            "active_ip_alerts": len(self.ip_alerts),
            "recent_alerts": [self._alert_to_dict(a) for a in self._alerts[-5:]],
        }

    async def run_loop(self) -> None:
        while True:
            await self.evaluate()
            await asyncio.sleep(self.check_interval)
