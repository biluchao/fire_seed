#!/usr/bin/env python3
"""
火种系统 (FireSeed) 幽灵影子管理器
=====================================
负责管理虚拟交易影子实例，在真实市场数据驱动下验证新策略或
新参数组合的表现，而不产生真实订单。

核心职责：
- 创建/销毁影子实例（每个实例包含独立策略与虚拟券商）
- 接收 Tick 数据并分发给所有活跃影子
- 定期评估影子表现（夏普、回撤、胜率等）
- 自动淘汰表现不佳的影子
- 将合格的影子晋升为候选金丝雀
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.context_isolator import IsolatedDataView, ContextFactory
from core.order_manager import OrderManager, Order
from core.execution import ExecutionGateway
from core.behavioral_logger import BehavioralLogger, EventType
from ghost.virtual_broker import VirtualBroker
from ghost.shadow_validator_v2 import ShadowValidatorV2

logger = logging.getLogger("fire_seed.shadow_manager")


@dataclass
class ShadowInstance:
    """单个影子实例的状态与资源"""
    id: str                                 # 唯一标识
    strategy: Any                           # IStrategy 实现
    order_mgr: OrderManager                 # 虚拟订单管理器
    broker: VirtualBroker                   # 虚拟券商，负责撮合
    ctx: IsolatedDataView                   # 隔离的数据视图
    created_at: float                       # 创建时间戳
    performance: Dict[str, float] = field(default_factory=lambda: {
        "sharpe": 0.0, "max_dd": 0.0, "win_rate": 0.0, "total_pnl": 0.0
    })
    trade_history: List[Dict] = field(default_factory=list)
    status: str = "running"                 # running / paused / completed / eliminated


class ShadowManager:
    """
    幽灵影子实例管理器。
    在主引擎的每一Tick中，将行情数据推送给所有影子实例，
    并定期由 ShadowValidatorV2 执行统计检验。
    """

    def __init__(self,
                 config: dict,
                 master_order_mgr: OrderManager,
                 master_execution: ExecutionGateway,
                 behavior_log: Optional[BehavioralLogger] = None,
                 data_feed=None):
        self.config = config
        self.master_order_mgr = master_order_mgr
        self.master_execution = master_execution
        self.log = behavior_log

        # 影子验证器（执行显著性检验、压力测试）
        self.validator = ShadowValidatorV2(config, behavior_log)

        # 所有活跃的影子实例
        self._active_shadows: Dict[str, ShadowInstance] = {}

        # 上下文工厂（用于为每个影子创建隔离视图）
        self.ctx_factory = ContextFactory(config)

        # 上一次评估时间
        self._last_eval_time = 0.0
        self.eval_interval_sec = config.get("shadow.eval_interval_sec", 300)  # 默认5分钟

        # 最大同时运行影子数
        self.max_shadows = config.get("shadow.max_instances", 6)

        # 数据源引用（用于虚拟券商撮合）
        self.data_feed = data_feed

        logger.info("幽灵影子管理器初始化完成")

    # ======================== 影子生命周期管理 ========================
    async def deploy_shadow(self, strategy: Any, strategy_name: str = "") -> str:
        """
        部署一个新的影子实例。
        :param strategy: 实现了 IStrategy 接口的策略对象
        :param strategy_name: 策略名称（用于日志）
        :return: 影子实例 ID
        """
        # 检查容量
        if len(self._active_shadows) >= self.max_shadows:
            # 淘汰表现最差的影子
            await self._eliminate_weakest()

        shadow_id = f"shadow_{strategy_name}_{uuid.uuid4().hex[:8]}"

        # 创建独立订单管理器和虚拟券商
        shadow_om = OrderManager(self.config)
        shadow_broker = VirtualBroker(self.config, shadow_om, self.data_feed, self.log)

        # 创建隔离的数据上下文（使用1分钟视图）
        ctx = self.ctx_factory.create("1m")

        shadow = ShadowInstance(
            id=shadow_id,
            strategy=strategy,
            order_mgr=shadow_om,
            broker=shadow_broker,
            ctx=ctx,
            created_at=time.time()
        )

        self._active_shadows[shadow_id] = shadow

        if self.log:
            self.log.log(EventType.SYSTEM, "ShadowManager",
                         f"创建影子实例 {shadow_id}", {"strategy": strategy_name})

        logger.info(f"影子实例 {shadow_id} 已部署")
        return shadow_id

    async def remove_shadow(self, shadow_id: str) -> bool:
        """移除指定影子实例"""
        shadow = self._active_shadows.pop(shadow_id, None)
        if shadow is None:
            return False

        # 调用策略的清理接口
        try:
            if hasattr(shadow.strategy, "terminate"):
                shadow.strategy.terminate()
        except Exception as e:
            logger.warning(f"影子 {shadow_id} 清理异常: {e}")

        if self.log:
            self.log.log(EventType.SYSTEM, "ShadowManager",
                         f"移除影子实例 {shadow_id}")

        return True

    async def _eliminate_weakest(self) -> None:
        """淘汰当前表现最差的影子实例（按夏普排序）"""
        if not self._active_shadows:
            return

        # 按夏普升序
        worst_id = min(
            self._active_shadows.keys(),
            key=lambda sid: self._active_shadows[sid].performance.get("sharpe", -999)
        )
        logger.info(f"淘汰表现最差的影子: {worst_id}")
        await self.remove_shadow(worst_id)

    # ======================== Tick 驱动 ========================
    async def tick(self, market_tick) -> None:
        """
        将主引擎收到的 Tick 数据分发给所有活跃影子。
        由引擎主循环调用。
        """
        if not self._active_shadows:
            return

        # 获取当前时间
        now = time.time()

        for shadow in self._active_shadows.values():
            try:
                # 更新隔离数据视图
                shadow.ctx.add_kline(market_tick.ohlc) if hasattr(market_tick, 'ohlc') else None
                if hasattr(market_tick, 'orderbook'):
                    shadow.ctx.add_orderbook(market_tick.orderbook)

                # 调用策略的信号生成
                orders = shadow.strategy.on_tick(market_tick)

                # 通过虚拟券商撮合订单
                for order_signal in orders:
                    await shadow.broker.execute(order_signal)

            except Exception as e:
                logger.error(f"影子 {shadow.id} 处理异常: {e}")
                # 连续异常可标记为暂停

        # 定期评估
        if now - self._last_eval_time >= self.eval_interval_sec:
            await self._evaluate_all()
            self._last_eval_time = now

    # ======================== 性能评估 ========================
    async def _evaluate_all(self) -> None:
        """对所有活跃影子进行性能评估"""
        for shadow in list(self._active_shadows.values()):
            try:
                metrics = self.validator.evaluate_instance(
                    shadow.trade_history,
                    shadow.order_mgr.get_account()
                )
                shadow.performance.update(metrics)

                # 检查是否应晋升
                if self._should_promote(shadow):
                    await self._promote_to_canary(shadow)

                # 检查是否应淘汰
                if self._should_eliminate(shadow):
                    await self.remove_shadow(shadow.id)

            except Exception as e:
                logger.error(f"评估影子 {shadow.id} 失败: {e}")

    def _should_promote(self, shadow: ShadowInstance) -> bool:
        """判断影子是否应晋升为金丝雀候选"""
        perf = shadow.performance
        return (perf.get("sharpe", 0) > 1.5 and
                perf.get("max_dd", 100) < 10.0 and
                len(shadow.trade_history) >= 30)

    def _should_eliminate(self, shadow: ShadowInstance) -> bool:
        """判断影子是否应被淘汰"""
        perf = shadow.performance
        # 运行超过1小时且夏普极低
        if time.time() - shadow.created_at > 3600 and perf.get("sharpe", 0) < -1.0:
            return True
        # 最大回撤过大
        if perf.get("max_dd", 0) > 25.0:
            return True
        return False

    async def _promote_to_canary(self, shadow: ShadowInstance) -> None:
        """将影子策略晋升为金丝雀候选，进入下一步发布流程"""
        if self.log:
            self.log.log(EventType.EVOLUTION, "ShadowManager",
                         f"影子 {shadow.id} 晋升为金丝雀候选",
                         {"sharpe": shadow.performance.get("sharpe")})

        # 存储策略的基因信息到金丝雀队列（由 canary.deployer 处理）
        # 此处仅记录日志，实际需要传递给金丝雀发布模块
        logger.info(f"影子 {shadow.id} 已标记为金丝雀候选")

    # ======================== 查询接口 ========================
    def get_active_shadows(self) -> List[Dict]:
        """返回所有活跃影子的状态摘要"""
        result = []
        for sid, shadow in self._active_shadows.items():
            result.append({
                "id": sid,
                "status": shadow.status,
                "uptime_sec": time.time() - shadow.created_at,
                "trades_count": len(shadow.trade_history),
                "performance": shadow.performance.copy(),
            })
        return result

    def get_shadow_detail(self, shadow_id: str) -> Optional[Dict]:
        """获取指定影子的详细信息"""
        shadow = self._active_shadows.get(shadow_id)
        if not shadow:
            return None
        return {
            "id": shadow.id,
            "performance": shadow.performance,
            "trade_history": shadow.trade_history[-50:],
            "position": shadow.order_mgr.get_position_summary(),
        }

    # ======================== 生命周期 ========================
    async def shutdown(self) -> None:
        """关闭所有影子实例"""
        for sid in list(self._active_shadows.keys()):
            await self.remove_shadow(sid)
        logger.info("幽灵影子管理器已关闭")
