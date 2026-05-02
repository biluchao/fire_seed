#!/usr/bin/env python3
"""
火种系统 (FireSeed) 外部依赖哨兵智能体 (DependencySentinel)
===============================================================
世界观：依赖倒置原则 (Dependency Inversion)
“高层策略不应依赖于底层实现细节。任何外部依赖都是潜在的断裂点。”

持续监控所有被火种依赖的外部服务健康度：
- 交易所 API (REST + WebSocket 连通性)
- 云存储 API (OSS/COS/S3 可读写性)
- LLM API (DeepSeek / OpenAI 等)
- 消息推送 API (Telegram / 钉钉 / 企业微信)
- 基础服务 (Redis / Docker)
- 第三方数据源 (可选)

发现异常时生成分级告警并推送；严重时触发自动化降级策略。
"""

import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import psutil
import redis
import yaml

from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier

logger = logging.getLogger("fire_seed.dependency_sentinel")


# ======================== 数据结构 ========================
@dataclass
class EndpointConfig:
    """一个外部服务端点的配置"""
    name: str                      # 内部名称，如 "binance_rest"
    category: str                  # 分类：exchange / storage / llm / messaging / infrastructure
    url: str                       # 监控 URL 或连接字符串
    method: str = "GET"            # HTTP 方法
    headers: Dict[str, str] = field(default_factory=dict)
    expected_status: int = 200     # 预期 HTTP 状态码
    timeout_sec: float = 5.0       # 超时时间
    check_interval_sec: int = 30   # 单点检查间隔
    retry_on_fail: int = 2         # 失败重试次数
    health_options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ServiceHealth:
    """外部服务的健康快照"""
    name: str
    category: str
    status: str = "unknown"            # healthy / degraded / offline
    latency_ms: float = 0.0
    error_count_today: int = 0
    last_success: Optional[datetime] = None
    last_failure: Optional[datetime] = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    remarks: str = ""


@dataclass
class DependencyAlert:
    """外部依赖告警"""
    timestamp: datetime = field(default_factory=datetime.now)
    level: EventLevel = EventLevel.INFO
    service: str = ""
    message: str = ""
    suggestion: str = ""


# ======================== 外部依赖哨兵 ========================
class DependencySentinel:
    """
    外部依赖哨兵智能体。
    世界观：依赖倒置原则——系统不应信任任何外部依赖，必须持续验证。
    """

    def __init__(self,
                 config_path: str = "config/settings.yaml",
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None,
                 default_check_interval_sec: int = 30):
        self.behavior_log = behavior_log
        self.notifier = notifier
        self.default_check_interval = default_check_interval_sec

        # 加载外部依赖配置
        self.endpoints: List[EndpointConfig] = []
        self._load_endpoints(config_path)

        # 维护每个端点的健康状态
        self.health: Dict[str, ServiceHealth] = {}
        self._init_health_states()

        # 告警历史
        self._alerts: List[DependencyAlert] = []
        # 连续异常计数器
        self._consecutive_anomalies: Dict[str, int] = defaultdict(int)
        # 上次检查时间
        self._last_check: Dict[str, float] = {}

        logger.info(f"外部依赖哨兵初始化完成，监控 {len(self.endpoints)} 个端点")

    # ======================== 配置加载 ========================
    def _load_endpoints(self, config_path: str) -> None:
        """从主配置文件加载外部服务端点列表"""
        try:
            with open(config_path, "r") as f:
                cfg = yaml.safe_load(f)

            deps = cfg.get("dependency_sentinel", {}).get("endpoints", [])
            for endpoint_dict in deps:
                self.endpoints.append(EndpointConfig(
                    name=endpoint_dict.get("name", "unknown"),
                    category=endpoint_dict.get("category", "external"),
                    url=endpoint_dict.get("url", ""),
                    method=endpoint_dict.get("method", "GET"),
                    headers=endpoint_dict.get("headers", {}),
                    expected_status=endpoint_dict.get("expected_status", 200),
                    timeout_sec=endpoint_dict.get("timeout_sec", 5),
                    check_interval_sec=endpoint_dict.get("check_interval_sec", 30),
                    retry_on_fail=endpoint_dict.get("retry_on_fail", 2),
                    health_options=endpoint_dict.get("health_options", {})
                ))
        except FileNotFoundError:
            logger.warning(f"配置文件 {config_path} 不存在，使用默认监控列表")
            self._set_default_endpoints()
        except Exception as e:
            logger.error(f"加载外部依赖配置失败: {e}")
            self._set_default_endpoints()

    def _set_default_endpoints(self) -> None:
        """当配置文件缺失时，使用内置默认的关键依赖列表"""
        self.endpoints = [
            EndpointConfig("binance_rest", "exchange", "https://api.binance.com/api/v3/ping"),
            EndpointConfig("binance_fapi", "exchange", "https://fapi.binance.com/fapi/v1/ping"),
            EndpointConfig("bybit_rest", "exchange", "https://api.bybit.com/v5/market/time"),
            EndpointConfig("telegram_api", "messaging", "https://api.telegram.org"),
            EndpointConfig("dingtalk_api", "messaging", "https://oapi.dingtalk.com"),
        ]
        logger.info("已使用默认外部依赖端点列表")

    def _init_health_states(self) -> None:
        for ep in self.endpoints:
            self.health[ep.name] = ServiceHealth(
                name=ep.name,
                category=ep.category,
                status="unknown"
            )

    # ======================== 主入口 ========================
    async def evaluate(self) -> Dict[str, Any]:
        """
        执行一次所有端点的健康扫描。
        """
        now = time.time()
        alerts: List[DependencyAlert] = []

        # 按间隔调度检查
        for ep in self.endpoints:
            last = self._last_check.get(ep.name, 0)
            if now - last < ep.check_interval_sec:
                continue
            self._last_check[ep.name] = now

            health = self.health.get(ep.name)
            if not health:
                continue

            # 针对不同类别执行检测
            if ep.category in ("exchange", "messaging", "llm", "storage"):
                await self._check_http_endpoint(ep, health, alerts)
            elif ep.category == "infrastructure":
                if "redis" in ep.name:
                    self._check_redis(ep, health, alerts)
                elif "docker" in ep.name:
                    self._check_docker(ep, health, alerts)

        # 推送告警
        for alert in alerts:
            self._emit_alert(alert)

        # 记录行为日志
        if self.behavior_log:
            offline_count = sum(1 for h in self.health.values() if h.status == "offline")
            self.behavior_log.log(
                EventType.AGENT, "DependencySentinel",
                f"外部依赖检查完成，{offline_count} 个服务离线",
                snapshot={"offline": offline_count}
            )

        return {
            "total_services": len(self.endpoints),
            "offline_services": [h.name for h in self.health.values() if h.status == "offline"],
            "alerts": len(alerts),
            "timestamp": datetime.now().isoformat()
        }

    # ======================== HTTP 端点检查 ========================
    async def _check_http_endpoint(self, ep: EndpointConfig,
                                   health: ServiceHealth,
                                   alerts: List[DependencyAlert]) -> None:
        """检查基于 HTTP 的外部服务"""
        for attempt in range(1 + ep.retry_on_fail):
            try:
                timeout = aiohttp.ClientTimeout(total=ep.timeout_sec)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    start = time.time()
                    async with session.request(
                        method=ep.method,
                        url=ep.url,
                        headers=ep.headers
                    ) as resp:
                        latency = (time.time() - start) * 1000
                        if resp.status == ep.expected_status:
                            # 成功
                            health.latency_ms = latency
                            health.status = "healthy"
                            health.last_success = datetime.now()
                            health.consecutive_failures = 0
                            health.consecutive_successes += 1
                            health.remarks = f"OK ({resp.status})"
                            return
                        else:
                            raise aiohttp.ClientResponseError(
                                status=resp.status,
                                message=f"Unexpected status {resp.status}"
                            )
            except Exception as e:
                if attempt == ep.retry_on_fail:
                    # 最终失败
                    health.consecutive_failures += 1
                    health.consecutive_successes = 0
                    health.error_count_today += 1
                    health.last_failure = datetime.now()

                    if health.consecutive_failures >= 3:
                        health.status = "offline"
                    else:
                        health.status = "degraded"

                    health.remarks = str(e)[:100]

                    level = EventLevel.CRITICAL if health.consecutive_failures >= 5 else EventLevel.WARN
                    alerts.append(DependencyAlert(
                        level=level,
                        service=ep.name,
                        message=f"服务 {ep.name} 不可达 (连续失败 {health.consecutive_failures} 次): {e}",
                        suggestion="检查网络或对应的 API 密钥有效性"
                    ))
                    self._consecutive_anomalies[ep.name] += 1
                else:
                    await asyncio.sleep(0.5)

    # ======================== Redis 检查 ========================
    def _check_redis(self, ep: EndpointConfig,
                     health: ServiceHealth,
                     alerts: List[DependencyAlert]) -> None:
        """检查 Redis 服务是否存活"""
        redis_url = ep.url or "redis://localhost:6379"
        try:
            client = redis.from_url(redis_url, socket_connect_timeout=ep.timeout_sec)
            start = time.time()
            client.ping()
            latency = (time.time() - start) * 1000
            client.close()
            health.latency_ms = latency
            health.status = "healthy"
            health.last_success = datetime.now()
            health.consecutive_failures = 0
            health.consecutive_successes += 1
            health.remarks = "PONG"
        except Exception as e:
            health.consecutive_failures += 1
            health.error_count_today += 1
            health.last_failure = datetime.now()
            health.status = "offline"
            health.remarks = str(e)[:80]
            self._consecutive_anomalies[ep.name] += 1
            alerts.append(DependencyAlert(
                level=EventLevel.CRITICAL,
                service=ep.name,
                message=f"Redis 不可用: {e}",
                suggestion="立即检查 Redis 进程是否运行"
            ))

    # ======================== Docker 检查 ========================
    def _check_docker(self, ep: EndpointConfig,
                      health: ServiceHealth,
                      alerts: List[DependencyAlert]) -> None:
        """检查 Docker 服务是否存活"""
        try:
            # 使用 docker info 或 psutil 检查 dockerd 进程
            for proc in psutil.process_iter(['name']):
                if 'dockerd' in proc.info['name']:
                    health.status = "healthy"
                    health.remarks = "dockerd running"
                    health.last_success = datetime.now()
                    health.consecutive_failures = 0
                    return
            # 未找到 dockerd 进程
            raise RuntimeError("dockerd process not found")
        except Exception as e:
            health.consecutive_failures += 1
            health.last_failure = datetime.now()
            health.status = "offline"
            health.remarks = str(e)[:80]
            alerts.append(DependencyAlert(
                level=EventLevel.CRITICAL,
                service=ep.name,
                message=f"Docker 服务异常: {e}",
                suggestion="Docker 可能未安装或未运行"
            ))

    # ======================== 告警处理 ========================
    def _emit_alert(self, alert: DependencyAlert) -> None:
        self._alerts.append(alert)
        if len(self._alerts) > 200:
            self._alerts = self._alerts[-200:]

        if self.notifier and alert.level in (EventLevel.WARN, EventLevel.CRITICAL):
            asyncio.ensure_future(
                self.notifier.send_alert(
                    level=alert.level.value,
                    title=f"外部依赖告警 [{alert.service}]",
                    body=f"{alert.message}\n建议: {alert.suggestion}"
                )
            )

    # ======================== 状态查询 ========================
    def get_status(self) -> Dict[str, Any]:
        return {
            "services": {
                name: {
                    "status": h.status,
                    "latency_ms": round(h.latency_ms, 1),
                    "consecutive_failures": h.consecutive_failures,
                }
                for name, h in self.health.items()
            },
            "recent_alerts": [
                {"service": a.service, "level": a.level.value, "message": a.message}
                for a in self._alerts[-5:]
            ],
        }

    async def run_loop(self) -> None:
        """独立运行循环（可由引擎协程管理）"""
        while True:
            await self.evaluate()
            await asyncio.sleep(self.default_check_interval)
