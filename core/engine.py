#!/usr/bin/env python3
"""
火种核心交易引擎
- 异步事件驱动
- 管理多周期策略实例
- 热插拔支持
- 学习时段自动降级
"""
import asyncio
import signal
import sys
import argparse
from pathlib import Path
from datetime import datetime

# 本地模块
from core.data_feed import DataFeed
from core.order_manager import OrderManager
from core.execution import ExecutionGateway
from core.risk_monitor import RiskMonitor
from core.scorecard import FusionScoreCard
from core.multi_tf_arbiter_v2 import CognitivelyEnhancedArbiter
from core.self_check import run_self_check
from core.daily_tasks import schedule_daily_tasks
from core.intelligent_learning_guard import LearningGuard
from core.plugin_manager import PluginManager
from config.settings import load_settings

class FireSeedEngine:
    def __init__(self, config_path: str):
        self.settings = load_settings(config_path)
        self.running = True
        self.data_feed = DataFeed(self.settings)
        self.order_manager = OrderManager()
        self.executor = ExecutionGateway(self.order_manager, self.data_feed)
        self.risk_monitor = RiskMonitor()
        self.scorecard = FusionScoreCard()
        self.arbiter = CognitivelyEnhancedArbiter()
        self.learning_guard = LearningGuard()
        self.plugin_manager = PluginManager()
        self._shutdown_event = asyncio.Event()

    async def initialize(self):
        """异步初始化各个模块"""
        await self.data_feed.start()
        self.plugin_manager.discover_and_load()
        self.risk_monitor.initialize()
        # 启动每日任务调度
        asyncio.create_task(schedule_daily_tasks(self))
        # 启动每小时自检
        asyncio.create_task(self._periodic_self_check())

    async def _periodic_self_check(self):
        while self.running:
            await asyncio.sleep(3600)
            report = run_self_check(self)
            if not report['healthy']:
                self.risk_monitor.trigger_alert("自检失败", report)

    async def on_tick(self, tick):
        """行情更新入口"""
        if not self.running:
            return

        # 学习时段自动降级
        if self.learning_guard.is_night_time:
            self.learning_guard.apply_night_mode(self)
        elif self.learning_guard.check_nightmare(tick.volatility, self.data_feed.avg_volatility):
            self.learning_guard.wake_up(self)

        # 多周期计算与仲裁
        signals = {}
        for tf in self.active_timeframes:
            signal = self.scorecard.evaluate(tick, tf)
            if signal:
                self.arbiter.collect_signal(tf, signal)

        final_signal = self.arbiter.evaluate(position_state=self.order_manager.position)
        if final_signal:
            await self.executor.execute(final_signal)

    async def run(self):
        """主循环"""
        await self.initialize()
        self.running = True
        while self.running:
            try:
                tick = await self.data_feed.get_next_tick()
                await self.on_tick(tick)
            except Exception as e:
                self.risk_monitor.log_error(e)
                await asyncio.sleep(0.1)

    async def shutdown(self):
        """优雅退出"""
        self.running = False
        await self.data_feed.stop()
        self.order_manager.cancel_all()
        self.plugin_manager.unload_all()
        self._shutdown_event.set()


def parse_args():
    parser = argparse.ArgumentParser(description='火种核心引擎')
    parser.add_argument('--config', type=str, default='config/settings.yaml',
                        help='配置文件路径')
    return parser.parse_args()


async def main():
    args = parse_args()
    engine = FireSeedEngine(args.config)

    # 注册信号处理
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(engine.shutdown()))

    await engine.run()


if __name__ == "__main__":
    asyncio.run(main())
