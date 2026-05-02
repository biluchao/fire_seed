#!/usr/bin/env python3
"""
火种系统 (FireSeed) 执行质量审计官智能体 (ExecutionAuditor)
===============================================================
世界观：实证主义 (Empiricism)
“执行质量只能用统计数据证明，不接受理论最优。”

核心职责：
- 监控订单执行全链路效率：TWAP/VWAP完成率、冰山订单效果
- 跟踪滑点趋势，检测滑点是否逐渐扩大
- 统计撤单率与订单重试频率，识别执行异常
- 评估多交易所路由质量（实际成交均价与最优价的偏离）
- 生成执行质量综合评分并推送异常告警
- 与监察者形成对抗性校验：执行质量 vs 系统健康

边界：
- 禁止访问：策略信号逻辑、因子权重、因子计算细节
- 专属数据：订单委托与成交记录、价格快照、交易所响应时间
- 时间尺度：60秒（平衡实时性与计算开销）
"""

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy.stats import linregress

from api.server import get_engine
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier

logger = logging.getLogger("fire_seed.execution_auditor")


# ======================== 数据结构 ========================
@dataclass
class ExecutionMetrics:
    """单次评估的执行指标快照"""
    twap_completion_pct: float = 100.0     # TWAP 完成率（实际执行量/计划量）
    vwap_deviation_bps: float = 0.0        # VWAP 偏离（基点）
    avg_slippage_bps: float = 0.0          # 平均滑点（基点）
    max_slippage_bps: float = 0.0          # 最大滑点（基点）
    cancel_rate_pct: float = 0.0           # 撤单率（撤单数/总下单数）
    retry_rate_pct: float = 0.0            # 重试率（重试订单/总下单数）
    iceberg_efficiency: float = 1.0        # 冰山订单效率（成交均价/挂单均价）
    router_quality_score: float = 100.0    # 多交易所路由质量评分（0-100）
    total_orders: int = 0                  # 总订单数
    evaluation_timestamp: str = ""


@dataclass
class ExecutionAlert:
    """执行质量告警"""
    timestamp: datetime = field(default_factory=datetime.now)
    level: EventLevel = EventLevel.INFO
    category: str = ""
    metric: str = ""
    current_value: float = 0.0
    threshold: float = 0.0
    message: str = ""
    suggested_action: str = ""


# ======================== 执行质量审计官主类 ========================
class ExecutionAuditor:
    """
    执行质量审计官智能体。
    以60秒为周期运行，综合评估订单执行质量。
    """

    def __init__(self,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None,
                 check_interval_sec: int = 60):
        self.behavior_log = behavior_log
        self.notifier = notifier
        self.check_interval = check_interval_sec

        # 历史指标（用于趋势检测）
        self._slippage_history: deque = deque(maxlen=1440)    # 最近24小时每分钟一条
        self._cancel_rate_history: deque = deque(maxlen=1440)

        # 告警历史
        self._alerts: List[ExecutionAlert] = []

        # 状态追踪
        self._last_check = 0.0
        self._global_quality_score: float = 100.0

        # 连续异常计数器
        self._consecutive_anomalies: Dict[str, int] = {}

        logger.info("执行质量审计官初始化完成，世界观：实证主义")

    # ======================== 主入口 ========================
    async def evaluate(self) -> Dict[str, Any]:
        """执行一次完整的执行质量审计"""
        now = time.time()
        if now - self._last_check < self.check_interval:
            return {"status": "throttled"}
        self._last_check = now

        alerts: List[ExecutionAlert] = []
        metrics = ExecutionMetrics()

        engine = get_engine()
        if engine is None:
            return {"status": "engine_not_available"}

        # 1. TWAP/VWAP 完成率
        await self._check_twap_vwap(engine, metrics, alerts)

        # 2. 滑点趋势
        await self._check_slippage(engine, metrics, alerts)

        # 3. 撤单/重试率
        await self._check_cancel_retry(engine, metrics, alerts)

        # 4. 冰山订单效率
        await self._check_iceberg_efficiency(engine, metrics, alerts)

        # 5. 多交易所路由质量
        await self._check_router_quality(engine, metrics, alerts)

        # 6. 综合评分
        self._compute_global_quality(metrics)

        # 推送告警
        for alert in alerts:
            self._emit_alert(alert)

        # 记录行为日志
        if self.behavior_log:
            self.behavior_log.log(
                EventType.AGENT, "ExecutionAuditor",
                f"执行质量检查完成: 综合评分 {self._global_quality_score:.0f}, 告警 {len(alerts)}",
                snapshot={
                    "global_quality": self._global_quality_score,
                    "alerts": len(alerts),
                    "metrics": metrics.__dict__
                }
            )

        return {
            "global_quality_score": self._global_quality_score,
            "metrics": metrics.__dict__,
            "alert_count": len(alerts),
            "alerts": [self._alert_to_dict(a) for a in alerts[-5:]],
            "timestamp": datetime.now().isoformat()
        }

    # ======================== TWAP/VWAP检查 ========================
    async def _check_twap_vwap(self, engine, metrics: ExecutionMetrics,
                               alerts: List[ExecutionAlert]) -> None:
        """
        检查最近完成的TWAP/VWAP订单是否在预定时间内足额完成。
        """
        if not hasattr(engine, 'order_mgr') or not hasattr(engine, 'execution'):
            return

        try:
            # 获取最近使用TWAP/VWAP的已完成订单
            orders = engine.order_mgr.get_recent_orders(100)
            twap_orders = [o for o in orders if hasattr(o, 'order_type') and
                          o.order_type in ('TWAP', 'VWAP') and
                          o.status == 'FILLED']

            if not twap_orders:
                metrics.twap_completion_pct = 100.0
                metrics.vwap_deviation_bps = 0.0
                return

            # 计算平均完成率
            completion_rates = []
            vwap_deviations = []
            for order in twap_orders:
                # 假设有 filled_qty / planned_qty 的记录
                planned = getattr(order, 'planned_quantity', order.quantity)
                actual = getattr(order, 'filled_quantity', order.quantity)
                if planned > 0:
                    completion_rates.append(min(100.0, (actual / planned) * 100))
                # VWAP偏离
                if hasattr(order, 'avg_fill_price') and hasattr(order, 'vwap_benchmark'):
                    if order.vwap_benchmark > 0:
                        dev_bps = abs(order.avg_fill_price - order.vwap_benchmark) / order.vwap_benchmark * 10000
                        vwap_deviations.append(dev_bps)

            metrics.twap_completion_pct = np.mean(completion_rates) if completion_rates else 100.0
            metrics.vwap_deviation_bps = np.mean(vwap_deviations) if vwap_deviations else 0.0

            if metrics.twap_completion_pct < 90.0:
                alerts.append(ExecutionAlert(
                    level=EventLevel.WARN,
                    category="TWAP/VWAP",
                    metric="完成率",
                    current_value=metrics.twap_completion_pct,
                    threshold=90.0,
                    message=f"TWAP/VWAP 平均完成率仅 {metrics.twap_completion_pct:.1f}%",
                    suggested_action="检查市场流动性或调整执行算法参数"
                ))
            if metrics.vwap_deviation_bps > 10.0:
                alerts.append(ExecutionAlert(
                    level=EventLevel.WARN,
                    category="VWAP偏离",
                    metric="偏离(bps)",
                    current_value=metrics.vwap_deviation_bps,
                    threshold=10.0,
                    message=f"VWAP 成交偏离基准 {metrics.vwap_deviation_bps:.1f} bps",
                    suggested_action="检查订单拆分策略，考虑降低单笔子订单规模"
                ))
        except Exception as e:
            logger.warning(f"TWAP/VWAP检查异常: {e}")

    # ======================== 滑点趋势检查 ========================
    async def _check_slippage(self, engine, metrics: ExecutionMetrics,
                              alerts: List[ExecutionAlert]) -> None:
        """
        统计最近一段时间的成交滑点，检查是否有恶化趋势。
        """
        try:
            orders = engine.order_mgr.get_recent_orders(500)
            slippages = []
            for o in orders:
                if hasattr(o, 'slippage_bps') and o.slippage_bps is not None:
                    slippages.append(o.slippage_bps)

            if not slippages:
                return

            metrics.avg_slippage_bps = np.mean(slippages)
            metrics.max_slippage_bps = np.max(slippages)

            # 加入历史并检测趋势
            self._slippage_history.append(metrics.avg_slippage_bps)

            # 趋势检验（最近30分钟 vs 前30分钟）
            if len(self._slippage_history) >= 60:
                recent = list(self._slippage_history)[-30:]
                earlier = list(self._slippage_history)[-60:-30]
                recent_avg = np.mean(recent)
                earlier_avg = np.mean(earlier)
                if earlier_avg > 0 and recent_avg > earlier_avg * 1.5:
                    alerts.append(ExecutionAlert(
                        level=EventLevel.WARN,
                        category="滑点趋势",
                        metric="平均滑点",
                        current_value=recent_avg,
                        threshold=earlier_avg * 1.5,
                        message=f"滑点恶化了 {(recent_avg/earlier_avg-1)*100:.0f}%",
                        suggested_action="检查做市商行为变化或降低订单激进程度"
                    ))

            # 单笔极高滑点告警
            if metrics.max_slippage_bps > 50.0:
                alerts.append(ExecutionAlert(
                    level=EventLevel.WARN,
                    category="极端滑点",
                    metric="最大滑点",
                    current_value=metrics.max_slippage_bps,
                    threshold=50.0,
                    message=f"检测到单笔极端滑点 {metrics.max_slippage_bps:.1f} bps",
                    suggested_action="检查该笔订单的市场环境，是否在流动性真空期下单"
                ))
        except Exception as e:
            logger.warning(f"滑点检查异常: {e}")

    # ======================== 撤单/重试率检查 ========================
    async def _check_cancel_retry(self, engine, metrics: ExecutionMetrics,
                                  alerts: List[ExecutionAlert]) -> None:
        """
        统计撤单比例与重试率。异常高的撤单率可能意味着策略频繁改变决策。
        """
        try:
            orders = engine.order_mgr.get_recent_orders(200)
            total = len(orders)
            if total == 0:
                return

            cancelled = sum(1 for o in orders if getattr(o, 'status', '') == 'CANCELLED')
            retried = sum(1 for o in orders if getattr(o, 'is_retry', False))

            metrics.total_orders = total
            metrics.cancel_rate_pct = (cancelled / total) * 100 if total > 0 else 0.0
            metrics.retry_rate_pct = (retried / total) * 100 if total > 0 else 0.0

            self._cancel_rate_history.append(metrics.cancel_rate_pct)

            if metrics.cancel_rate_pct > 30.0:
                alerts.append(ExecutionAlert(
                    level=EventLevel.WARN,
                    category="撤单率",
                    metric="撤单比例",
                    current_value=metrics.cancel_rate_pct,
                    threshold=30.0,
                    message=f"撤单率高达 {metrics.cancel_rate_pct:.1f}%",
                    suggested_action="检查策略信号是否过于频繁变动，或订单参数设置是否合理"
                ))
            if metrics.retry_rate_pct > 10.0:
                alerts.append(ExecutionAlert(
                    level=EventLevel.INFO,
                    category="重试率",
                    metric="重试比例",
                    current_value=metrics.retry_rate_pct,
                    threshold=10.0,
                    message=f"订单重试率 {metrics.retry_rate_pct:.1f}%，可能存在交易所限频或网络波动",
                    suggested_action="检查API限频状态与网络延迟"
                ))
        except Exception as e:
            logger.warning(f"撤单/重试率检查异常: {e}")

    # ======================== 冰山订单效率检查 ========================
    async def _check_iceberg_efficiency(self, engine, metrics: ExecutionMetrics,
                                        alerts: List[ExecutionAlert]) -> None:
        """
        评估冰山订单的执行效率：成交均价与初始挂单价之间的差异。
        """
        try:
            orders = engine.order_mgr.get_recent_orders(100)
            iceberg_orders = [o for o in orders if getattr(o, 'order_type', '') == 'ICEBERG']

            if not iceberg_orders:
                metrics.iceberg_efficiency = 1.0
                return

            ratios = []
            for o in iceberg_orders:
                if hasattr(o, 'avg_fill_price') and hasattr(o, 'limit_price') and o.limit_price > 0:
                    ratio = o.avg_fill_price / o.limit_price
                    ratios.append(ratio if o.side == 'buy' else 1/ratio)

            metrics.iceberg_efficiency = np.mean(ratios) if ratios else 1.0

            if metrics.iceberg_efficiency > 1.005:  # 超过0.5%的额外成交成本
                alerts.append(ExecutionAlert(
                    level=EventLevel.WARN,
                    category="冰山效率",
                    metric="成交均价/挂单价",
                    current_value=metrics.iceberg_efficiency,
                    threshold=1.005,
                    message=f"冰山订单成交成本高出 {((metrics.iceberg_efficiency-1)*100):.2f}%",
                    suggested_action="检查冰山订单显单量是否被做市商探测并针对性调整"
                ))
        except Exception as e:
            logger.warning(f"冰山订单效率检查异常: {e}")

    # ======================== 多交易所路由质量检查 ========================
    async def _check_router_quality(self, engine, metrics: ExecutionMetrics,
                                    alerts: List[ExecutionAlert]) -> None:
        """
        评估多交易所智能路由的质量：实际成交均价是否接近最优价格。
        """
        try:
            # 这里简化：如果data_feed提供跨交易所价格，则计算最优价与实际成交价差异
            if not hasattr(engine, 'data_feed'):
                metrics.router_quality_score = 100.0
                return

            # 实际成交记录
            orders = engine.order_mgr.get_recent_orders(50)
            executed = [o for o in orders if getattr(o, 'status', '') == 'FILLED']

            if not executed:
                metrics.router_quality_score = 100.0
                return

            # 对每个成交订单，比较成交价与当时所有交易所的最优价
            quality_scores = []
            for o in executed:
                # 假设有 best_possible_price 字段（由执行网关记录）
                if hasattr(o, 'best_possible_price') and o.best_possible_price > 0:
                    if o.side == 'buy':
                        score = max(0, 100 - (o.avg_fill_price / o.best_possible_price - 1) * 10000)
                    else:
                        score = max(0, 100 - (1 - o.avg_fill_price / o.best_possible_price) * 10000)
                    quality_scores.append(score)

            metrics.router_quality_score = np.mean(quality_scores) if quality_scores else 100.0

            if metrics.router_quality_score < 80.0:
                alerts.append(ExecutionAlert(
                    level=EventLevel.WARN,
                    category="路由质量",
                    metric="路由评分",
                    current_value=metrics.router_quality_score,
                    threshold=80.0,
                    message=f"多交易所路由质量评分 {metrics.router_quality_score:.0f}，偏离最优价严重",
                    suggested_action="检查各交易所延时与深度，调整路由权重"
                ))
        except Exception as e:
            logger.warning(f"路由质量检查异常: {e}")

    # ======================== 综合评分 ========================
    def _compute_global_quality(self, metrics: ExecutionMetrics) -> None:
        """综合各项指标计算执行质量评分"""
        components = []
        # TWAP完成率（满分30）
        components.append(min(30, metrics.twap_completion_pct * 0.3))
        # 滑点（满分25，基准0滑点=25，10bps=15）
        components.append(max(0, 25 - metrics.avg_slippage_bps * 1.5))
        # 撤单率（满分15）
        components.append(max(0, 15 - metrics.cancel_rate_pct * 0.5))
        # 冰山效率（满分15）
        components.append(max(0, 15 - (metrics.iceberg_efficiency - 1) * 1000))
        # 路由质量（满分15）
        components.append(min(15, metrics.router_quality_score * 0.15))
        self._global_quality_score = max(0.0, min(100.0, sum(components)))

    # ======================== 告警推送 ========================
    def _emit_alert(self, alert: ExecutionAlert) -> None:
        self._alerts.append(alert)
        if len(self._alerts) > 200:
            self._alerts = self._alerts[-200:]

        if self.notifier:
            asyncio.ensure_future(
                self.notifier.send_alert(
                    level=alert.level.value,
                    title=f"执行质量审计 [{alert.category}]",
                    body=f"{alert.message}\n建议: {alert.suggested_action}"
                )
            )

    @staticmethod
    def _alert_to_dict(alert: ExecutionAlert) -> Dict[str, Any]:
        return {
            "timestamp": alert.timestamp.isoformat(),
            "level": alert.level.value,
            "category": alert.category,
            "message": alert.message,
        }

    def get_status(self) -> Dict[str, Any]:
        return {
            "global_quality_score": self._global_quality_score,
            "recent_alerts": [self._alert_to_dict(a) for a in self._alerts[-5:]],
        }

    async def run_loop(self) -> None:
        while True:
            await self.evaluate()
            await asyncio.sleep(self.check_interval)
