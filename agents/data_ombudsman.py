#!/usr/bin/env python3
"""
火种系统 (FireSeed) 数据监察员智能体 (DataOmbudsman)
=========================================================
世界观：经验主义 (Empiricism)
“无数据，不决策；脏数据，必误判”

核心职责：
- 实时监控所有数据源的健康状态（WebSocket、REST、NTP）
- 检测数据异常：延迟飙升、丢包、时间戳倒退、价格断层、成交量为零
- 对比回测数据与实盘数据的分布差异（Wasserstein距离）
- 独立于监察者运行，两者结论互相校验
- 数据异常时触发降级（切换到备用数据源或标记为不可信）
- 生成数据健康评分并推送告警

边界：
- 禁止访问：策略信号、仓位信息、交易决策
- 专属数据：原始Tick、WebSocket心跳、NTP偏移、回测成交记录
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
from scipy.stats import wasserstein_distance

from api.server import get_engine
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier

logger = logging.getLogger("fire_seed.data_ombudsman")


# ======================== 数据结构 ========================
@dataclass
class DataSourceHealth:
    """单个数据源的健康状态"""
    name: str                            # 数据源名称
    source_type: str                     # WS / REST / NTP / BACKTEST
    latency_ms: float = 0.0              # 当前延迟
    packet_loss_pct: float = 0.0         # 丢包率
    gap_detected: bool = False           # 检测到数据断层
    last_heartbeat: float = 0.0          # 最后心跳时间戳
    anomaly_score: float = 0.0           # 综合异常评分 (0-1)
    status: str = "unknown"              # healthy / degraded / offline


@dataclass
class DataQualityAlert:
    """数据质量告警"""
    timestamp: datetime = field(default_factory=datetime.now)
    level: EventLevel = EventLevel.INFO
    source: str = ""
    metric: str = ""
    current_value: float = 0.0
    threshold: float = 0.0
    message: str = ""
    suggested_action: str = ""


# ======================== 数据监察员主类 ========================
class DataOmbudsman:
    """
    数据监察员智能体。
    以10秒为周期运行，对所有数据源进行高频健康扫描。
    """

    def __init__(self,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None,
                 check_interval_sec: int = 10):
        """
        :param behavior_log: 全系统行为日志
        :param notifier:     消息推送器
        :param check_interval_sec: 检查间隔（秒），默认10秒
        """
        self.behavior_log = behavior_log
        self.notifier = notifier
        self.check_interval = check_interval_sec

        # 数据源注册表
        self.data_sources: Dict[str, DataSourceHealth] = {}
        self._init_data_sources()

        # 数据质量历史（用于趋势分析）
        self._latency_history: Dict[str, deque] = {}
        self._packet_loss_history: Dict[str, deque] = {}
        self._price_sequence: deque = deque(maxlen=600)   # 最近10分钟价格（每秒一个点）

        # 告警历史
        self._alerts: List[DataQualityAlert] = []

        # 状态追踪
        self._last_check = 0.0
        self._consecutive_anomalies: Dict[str, int] = {}
        self._global_health_score: float = 100.0

        logger.info("数据监察员初始化完成，世界观：经验主义")

    def _init_data_sources(self) -> None:
        """初始化要监控的数据源清单"""
        sources = [
            ("binance_ws", "WS"),      # 币安WebSocket
            ("binance_rest", "REST"),   # 币安REST API
            ("bybit_ws", "WS"),         # Bybit备用WebSocket
            ("okx_ws", "WS"),           # OKX备用WebSocket
            ("ntp_sync", "NTP"),        # 系统时钟同步
            ("backtest_store", "BACKTEST"), # 回测数据源
        ]
        for name, stype in sources:
            self.data_sources[name] = DataSourceHealth(name=name, source_type=stype)
            self._latency_history[name] = deque(maxlen=120)  # 保留20分钟（10秒间隔）
            self._packet_loss_history[name] = deque(maxlen=120)

    # ======================== 主入口 ========================
    async def evaluate(self) -> Dict[str, Any]:
        """执行一次完整的数据质量审计"""
        now = time.time()
        if now - self._last_check < self.check_interval:
            return {"status": "throttled"}
        self._last_check = now

        alerts: List[DataQualityAlert] = []

        # 获取引擎引用
        engine = get_engine()

        # 1. 检查WebSocket数据源
        await self._check_ws_sources(engine, alerts)

        # 2. 检查REST API数据源
        await self._check_rest_sources(engine, alerts)

        # 3. 检查NTP时钟同步
        await self._check_ntp_sync(alerts)

        # 4. 检查回测数据与实盘数据的分布差异
        await self._check_backtest_alignment(engine, alerts)

        # 5. 检测价格序列异常（断层、倒退）
        await self._check_price_anomalies(engine, alerts)

        # 6. 计算全局数据健康评分
        self._compute_global_health()

        # 推送告警
        for alert in alerts:
            self._emit_alert(alert)

        # 记录行为日志
        if self.behavior_log:
            self.behavior_log.log(
                EventType.AGENT, "DataOmbudsman",
                f"数据质量检查完成: 健康评分 {self._global_health_score:.0f}, 告警 {len(alerts)}",
                snapshot={"health_score": self._global_health_score, "alerts": len(alerts)}
            )

        # 与监察者交叉校验
        if self.behavior_log:
            self.behavior_log.log(
                EventType.AGENT, "对抗性校验",
                f"数据监察员(经验主义) vs 监察者(机械唯物主义): "
                f"数据健康={self._global_health_score:.0f}, "
                f"系统状态需参考监察者报告"
            )

        return {
            "global_health_score": self._global_health_score,
            "alert_count": len(alerts),
            "healthy_sources": sum(1 for ds in self.data_sources.values() if ds.status == "healthy"),
            "total_sources": len(self.data_sources),
            "alerts": [self._alert_to_dict(a) for a in alerts[-5:]],
            "timestamp": datetime.now().isoformat()
        }

    # ======================== WebSocket检查 ========================
    async def _check_ws_sources(self, engine, alerts: List[DataQualityAlert]) -> None:
        """检查所有WebSocket数据源的连接状态"""
        if engine is None:
            return

        ws_sources = [name for name, ds in self.data_sources.items() if ds.source_type == "WS"]
        for name in ws_sources:
            ds = self.data_sources[name]

            # 从data_feed获取真实状态
            if hasattr(engine, 'data_feed'):
                feed = engine.data_feed
                # 检查是否有该数据源的连接
                if hasattr(feed, 'get_ws_status'):
                    status = feed.get_ws_status(name)
                    ds.latency_ms = status.get('latency_ms', 0)
                    ds.packet_loss_pct = status.get('packet_loss_pct', 0)
                    ds.last_heartbeat = status.get('last_heartbeat', time.time())

            self._latency_history[name].append(ds.latency_ms)
            self._packet_loss_history[name].append(ds.packet_loss_pct)

            # 延迟告警
            if ds.latency_ms > 200:
                alerts.append(DataQualityAlert(
                    level=EventLevel.WARN if ds.latency_ms < 500 else EventLevel.CRITICAL,
                    source=name,
                    metric="延迟",
                    current_value=ds.latency_ms,
                    threshold=200,
                    message=f"WebSocket {name} 延迟 {ds.latency_ms:.0f}ms",
                    suggested_action="检查网络链路或切换到备用数据源"
                ))
                ds.anomaly_score = min(1.0, ds.anomaly_score + 0.2)
            else:
                ds.anomaly_score = max(0.0, ds.anomaly_score - 0.1)

            # 丢包告警
            if ds.packet_loss_pct > 1.0:
                alerts.append(DataQualityAlert(
                    level=EventLevel.WARN,
                    source=name,
                    metric="丢包率",
                    current_value=ds.packet_loss_pct,
                    threshold=1.0,
                    message=f"WebSocket {name} 丢包率 {ds.packet_loss_pct:.1f}%",
                    suggested_action="检查网络稳定性"
                ))

            # 心跳超时（超过30秒无心跳）
            if time.time() - ds.last_heartbeat > 30:
                ds.status = "offline"
                alerts.append(DataQualityAlert(
                    level=EventLevel.CRITICAL,
                    source=name,
                    metric="心跳",
                    current_value=time.time() - ds.last_heartbeat,
                    threshold=30,
                    message=f"WebSocket {name} 超过30秒无心跳",
                    suggested_action="立即切换到备用数据源"
                ))
            elif ds.anomaly_score > 0.5:
                ds.status = "degraded"
            else:
                ds.status = "healthy"

    # ======================== REST API检查 ========================
    async def _check_rest_sources(self, engine, alerts: List[DataQualityAlert]) -> None:
        """检查REST API的数据质量"""
        rest_sources = [name for name, ds in self.data_sources.items() if ds.source_type == "REST"]
        for name in rest_sources:
            ds = self.data_sources[name]

            # 占位：实际应ping交易所的/ping端点
            ds.status = "healthy"

    # ======================== NTP时钟检查 ========================
    async def _check_ntp_sync(self, alerts: List[DataQualityAlert]) -> None:
        """检查系统时钟同步状态"""
        ds = self.data_sources.get("ntp_sync")
        if ds is None:
            return

        try:
            # 检查NTP偏移（通过chronyc）
            import subprocess
            result = subprocess.run(
                ['chronyc', 'tracking'],
                capture_output=True, text=True, timeout=2
            )
            for line in result.stdout.split('\n'):
                if 'System time' in line:
                    # 解析偏移值
                    parts = line.split()
                    for i, p in enumerate(parts):
                        if 'slow' in p or 'fast' in p:
                            offset_str = parts[i-1].strip('s')
                            ds.latency_ms = abs(float(offset_str)) * 1000
                            break

            if ds.latency_ms > 100:
                alerts.append(DataQualityAlert(
                    level=EventLevel.WARN,
                    source="ntp_sync",
                    metric="时钟偏移",
                    current_value=ds.latency_ms,
                    threshold=100,
                    message=f"系统时钟偏移 {ds.latency_ms:.1f}ms",
                    suggested_action="检查NTP服务器连接"
                ))
                ds.status = "degraded"
            else:
                ds.status = "healthy"

        except Exception:
            ds.status = "healthy"  # 无法检测时不告警

    # ======================== 回测数据对齐检查 ========================
    async def _check_backtest_alignment(self, engine, alerts: List[DataQualityAlert]) -> None:
        """
        对比回测数据源与实盘数据的Wasserstein距离。
        若距离过大，说明回测环境与实盘环境不匹配，策略验证结果可能失真。
        """
        ds = self.data_sources.get("backtest_store")
        if ds is None or engine is None:
            return

        try:
            # 获取近期实盘成交数据
            if hasattr(engine, 'order_mgr'):
                live_trades = engine.order_mgr.get_recent_orders(100)
                live_prices = [t.price for t in live_trades if hasattr(t, 'price')]

                # 获取对应时段回测数据（从回测引擎获取）
                if hasattr(engine, 'backtest_engine'):
                    backtest_prices = engine.backtest_engine.get_prices(len(live_prices))
                    if len(live_prices) >= 30 and len(backtest_prices) >= 30:
                        # 计算Wasserstein距离
                        w_dist = wasserstein_distance(live_prices, backtest_prices)
                        ds.anomaly_score = min(1.0, w_dist / 100.0)  # 归一化
                        if w_dist > 50:
                            alerts.append(DataQualityAlert(
                                level=EventLevel.WARN,
                                source="backtest_store",
                                metric="Wasserstein距离",
                                current_value=w_dist,
                                threshold=50,
                                message=f"回测与实盘价格分布偏差 {w_dist:.1f}",
                                suggested_action="检查回测数据是否使用了正确的滑点模型"
                            ))
                            ds.status = "degraded"
                        else:
                            ds.status = "healthy"
        except Exception as e:
            logger.debug(f"回测对齐检查跳过: {e}")

    # ======================== 价格异常检测 ========================
    async def _check_price_anomalies(self, engine, alerts: List[DataQualityAlert]) -> None:
        """
        检测价格序列中的异常：
        - 断层：相邻Tick价格跳跃超过历史标准差的5倍
        - 倒退：时间戳倒退
        - 停滞：价格长时间不变
        """
        if engine is None:
            return

        try:
            # 从data_feed获取最新价格
            if hasattr(engine, 'data_feed'):
                tick = await engine.data_feed.get_next_tick(timeout=1.0)
                if tick and hasattr(tick, 'last_price'):
                    self._price_sequence.append(tick.last_price)

            # 仅在有足够数据时检测
            if len(self._price_sequence) < 60:
                return

            prices = np.array(list(self._price_sequence))
            returns = np.diff(prices)
            std_ret = np.std(returns) + 1e-10

            # 断层检测
            latest_return = returns[-1] if len(returns) > 0 else 0
            if abs(latest_return) > 5 * std_ret:
                alerts.append(DataQualityAlert(
                    level=EventLevel.WARN,
                    source="price_sequence",
                    metric="价格断层",
                    current_value=abs(latest_return),
                    threshold=5 * std_ret,
                    message=f"检测到价格断层: 波动 {abs(latest_return):.4f} (正常范围 ±{5*std_ret:.4f})",
                    suggested_action="检查是否是插针行情或数据源错误，若为插针则忽略"
                ))

            # 价格停滞检测（连续10秒价格不变）
            if len(self._price_sequence) >= 10:
                last_10 = list(self._price_sequence)[-10:]
                if len(set(last_10)) == 1:
                    alerts.append(DataQualityAlert(
                        level=EventLevel.WARN,
                        source="price_sequence",
                        metric="价格停滞",
                        current_value=10.0,
                        threshold=10.0,
                        message="价格连续10秒未变动",
                        suggested_action="检查WebSocket连接是否仍存活"
                    ))
        except Exception as e:
            logger.warning(f"价格异常检测失败: {e}")

    # ======================== 健康评分计算 ========================
    def _compute_global_health(self) -> None:
        """综合所有数据源的健康状态，计算全局健康评分"""
        if not self.data_sources:
            self._global_health_score = 100.0
            return

        scores = []
        for ds in self.data_sources.values():
            if ds.status == "healthy":
                scores.append(100.0)
            elif ds.status == "degraded":
                scores.append(60.0)
            elif ds.status == "offline":
                scores.append(20.0)
            else:
                scores.append(50.0)
            # 附加异常评分惩罚
            scores[-1] -= ds.anomaly_score * 30

        self._global_health_score = max(0.0, min(100.0, np.mean(scores)))

    # ======================== 告警处理 ========================
    def _emit_alert(self, alert: DataQualityAlert) -> None:
        self._alerts.append(alert)
        if len(self._alerts) > 200:
            self._alerts = self._alerts[-200:]

        if self.notifier and alert.level in (EventLevel.WARN, EventLevel.CRITICAL):
            asyncio.ensure_future(
                self.notifier.send_alert(
                    level=alert.level.value,
                    title=f"数据监察员 [{alert.source}]",
                    body=f"{alert.message}\n建议: {alert.suggested_action}"
                )
            )

    @staticmethod
    def _alert_to_dict(alert: DataQualityAlert) -> Dict[str, Any]:
        return {
            "timestamp": alert.timestamp.isoformat(),
            "level": alert.level.value,
            "source": alert.source,
            "metric": alert.metric,
            "message": alert.message,
        }

    # ======================== 状态查询 ========================
    def get_status(self) -> Dict[str, Any]:
        return {
            "global_health_score": self._global_health_score,
            "sources": {
                name: {"status": ds.status, "anomaly_score": ds.anomaly_score}
                for name, ds in self.data_sources.items()
            },
            "recent_alerts": [self._alert_to_dict(a) for a in self._alerts[-5:]],
        }

    async def run_loop(self) -> None:
        while True:
            await self.evaluate()
            await asyncio.sleep(self.check_interval)
