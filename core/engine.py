#!/usr/bin/env python3
"""
火种系统 (FireSeed) 核心交易引擎
==================================
主事件循环负责：
- 行情接收与分发 (多交易所、多周期)
- 感知层调用 (粒子滤波、锁相环、VIB)
- 策略决策 (多因子评分、仲裁、状态机)
- 风险校验 (硬实时C++模块 + Python风控)
- 订单执行 (网关、TWAP、冰山)
- 幽灵影子管理
- 行为日志记录
- 每日任务调度
- 16 智能体抗辩式议会 (世界观分裂)
- 对抗性数据质量校验

运行方式:
    python engine.py --mode live    # 实盘
    python engine.py --mode virtual # 虚拟盘
    python engine.py --mode ghost   # 幽灵回放
"""

import asyncio
import os
import signal
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

# ---------- 火种核心模块 ----------
from config.loader import ConfigLoader
from core.data_feed import MarketDataFeed
from core.order_manager import OrderManager
from core.execution import ExecutionGateway
from core.risk_monitor import RiskMonitor
from core.scorecard import DynamicScoreCard
from core.perception import PerceptionFusion
from core.state_machine import TradingStateMachine
from core.conditional_weight import ConditionalWeightEngine
from core.multi_tf_arbiter_v2 import MultiTFArbiter
from core.context_isolator import ContextFactory
from core.continuous_position import ContinuousPositionController
from core.plugin_manager import PluginManager
from core.self_check import SystemSelfCheck
from core.daily_tasks import DailyTaskScheduler
from core.intelligent_learning_guard import IntelligentLearningGuard
from core.behavioral_logger import BehavioralLogger, EventType
from core.lazy_evaluator import LazyFactorEvaluator
from core.compute_scheduler import ComputeScheduler
from core.copy_trading import CopyTradingEngine

# ---------- 智能体议会 ----------
from agents.adversarial_council import AdversarialCouncil
from agents.sentinel import SentinelAgent
from agents.alchemist import AlchemistAgent
from agents.guardian import GuardianAgent
from agents.devils_advocate import DevilsAdvocate
from agents.godel_watcher import GodelWatcher
from agents.env_inspector import EnvInspector
from agents.redundancy_auditor import RedundancyAuditor
from agents.weight_calibrator import WeightCalibrator
from agents.narrator import NarratorAgent
from agents.diversity_enforcer import DiversityEnforcer
from agents.archive_guardian import ArchiveGuardian
from agents.copy_trade_coordinator import CopyTradeCoordinator
from agents.execution_auditor import ExecutionAuditor
from agents.dependency_sentinel import DependencySentinel
from agents.security_awareness import SecurityAwareness
from agents.data_ombudsman import DataOmbudsman

# ---------- 进化与幽灵 ----------
from ghost.shadow_manager import ShadowManager
from ota.updater import OTAUpdater
from cold_storage.archiver import ColdArchiver
from assistant.llm_gateway import LLMGateway
from integrations.messenger_hub import MessengerHub
from core.notifier import SystemNotifier

# 日志
from utils.logger import setup_logging

# 尝试加载 C++ 高性能模块
try:
    from cpp.bindings import RealtimeGuard, JumpDetector, IncrementalCovariance, HardWatcher
    CPP_AVAILABLE = True
except ImportError:
    CPP_AVAILABLE = False
    RealtimeGuard = None
    JumpDetector = None
    IncrementalCovariance = None
    HardWatcher = None


class FireSeedEngine:
    """火种交易引擎单例"""

    def __init__(self, config_path: str = "config/settings.yaml"):
        self.config = ConfigLoader(config_path)
        self.log = setup_logging("engine", self.config.get("system.log_level", "INFO"))
        self._running = True
        self._shutting_down = False

        # ------------------------- 核心组件 -------------------------
        self.data_feed = MarketDataFeed(self.config)
        self.order_mgr = OrderManager(self.config)
        self.execution = ExecutionGateway(self.config, self.order_mgr)
        self.risk_monitor = RiskMonitor(self.config, self.order_mgr)
        self.scorecard = DynamicScoreCard(self.config)
        self.perception = PerceptionFusion(self.config)
        self.state_machine = TradingStateMachine(self.config)
        self.weight_engine = ConditionalWeightEngine(self.config)
        self.arbiter = MultiTFArbiter(self.config)
        self.ctx_factory = ContextFactory(self.config)
        self.position_ctrl = ContinuousPositionController(self.config)
        self.plugin_mgr = PluginManager(self.config)
        self.self_check = SystemSelfCheck(self.config)
        self.daily_tasks = DailyTaskScheduler(self.config)
        self.learning_guard = IntelligentLearningGuard(self.config)
        self.behavior_log = BehavioralLogger()
        self.lazy_evaluator = LazyFactorEvaluator(self.config)
        self.compute_scheduler = ComputeScheduler(self.config)
        self.copy_trading = CopyTradingEngine(self.config, self.order_mgr, self.execution, self.behavior_log)

        # ------------------------- C++ 模块 (可选) -------------------------
        self.cpp_guard: Optional[RealtimeGuard] = None
        self.cpp_jump: Optional[JumpDetector] = None
        self.cpp_covar: Optional[IncrementalCovariance] = None
        self.cpp_hard_watcher: Optional[HardWatcher] = None
        if CPP_AVAILABLE:
            self.cpp_guard = RealtimeGuard()
            self.cpp_jump = JumpDetector()
            self.cpp_covar = IncrementalCovariance(20)
            self.cpp_hard_watcher = HardWatcher()
            self.log.info("C++ 高性能模块加载成功")

        # ------------------------- 智能体议会 (16位) -------------------------
        self.adversarial_council = AdversarialCouncil()
        self._initialize_agents()

        # ------------------------- 幽灵影子与进化 -------------------------
        self.shadow_mgr = ShadowManager(self.config, self.order_mgr, self.execution, self.behavior_log, self.data_feed)
        self.ota_updater = OTAUpdater(self.config)
        self.cold_archiver = ColdArchiver(self.config)

        # ------------------------- 助手与通知 -------------------------
        self.llm_gateway = LLMGateway(self.config)
        self.messenger = MessengerHub(self.config)
        self.notifier = SystemNotifier(self.messenger)

        # 运行模式
        self.mode = self.config.get("system.mode", "virtual")
        # 注册信号处理
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        self.log.info(f"火种引擎启动于 {self.mode} 模式，已注册 16 位智能体")

    def _initialize_agents(self):
        """创建并注册全部 16 个携带世界观的智能体"""
        # 核心智能体（1-12）
        self.adversarial_council.register_agent("sentinel",
            SentinelAgent(self.behavior_log, self.notifier))
        self.adversarial_council.register_agent("alchemist",
            AlchemistAgent(self.config, self.behavior_log, self.notifier))
        self.adversarial_council.register_agent("guardian",
            GuardianAgent(self.config, self.risk_monitor, self.behavior_log, self.notifier))
        self.adversarial_council.register_agent("devils_advocate",
            DevilsAdvocate(self.behavior_log, self.notifier))
        self.adversarial_council.register_agent("godel_watcher",
            GodelWatcher(self.behavior_log, self.notifier))
        self.adversarial_council.register_agent("env_inspector",
            EnvInspector(self.behavior_log, self.notifier))
        self.adversarial_council.register_agent("redundancy_auditor",
            RedundancyAuditor(root=".", behavior_log=self.behavior_log))
        self.adversarial_council.register_agent("weight_calibrator",
            WeightCalibrator(config_path="config", behavior_log=self.behavior_log, notifier=self.notifier))
        self.adversarial_council.register_agent("narrator",
            NarratorAgent(self.behavior_log, self.notifier))
        self.adversarial_council.register_agent("diversity_enforcer",
            DiversityEnforcer(self.behavior_log, self.notifier))
        self.adversarial_council.register_agent("archive_guardian",
            ArchiveGuardian(self.behavior_log, self.notifier))
        self.adversarial_council.register_agent("copy_trade_coordinator",
            CopyTradeCoordinator(self.behavior_log, self.notifier))

        # 新增智能体（13-16）
        self.adversarial_council.register_agent("execution_auditor",
            ExecutionAuditor(self.execution, self.behavior_log, self.notifier))
        self.adversarial_council.register_agent("dependency_sentinel",
            DependencySentinel(self.behavior_log, self.notifier))
        self.adversarial_council.register_agent("security_awareness",
            SecurityAwareness(self.behavior_log, self.notifier))
        self.adversarial_council.register_agent("data_ombudsman",
            DataOmbudsman(self.behavior_log, self.notifier))

        self.log.info("16 位智能体已注册到抗辩式议会")

    def _signal_handler(self, signum, frame):
        self.log.warning(f"接收到信号 {signum}，开始优雅退出...")
        self._running = False

    async def run(self):
        """主事件循环"""
        self.log.info(f"火种引擎启动于 {self.mode} 模式")

        # 启动数据源
        await self.data_feed.start()
        # 启动 C++ 实时保护线程
        if self.cpp_guard:
            self.cpp_guard.start()
        # 启动后台任务
        asyncio.create_task(self._background_tasks())
        # 启动 OTA 检查
        asyncio.create_task(self._ota_check_loop())
        # 启动冷归档
        asyncio.create_task(self._cold_archive_loop())
        # 启动智能体守护循环（可并行运行多个）
        for agent_name in self.adversarial_council.agents:
            agent = self.adversarial_council.agents[agent_name]
            if hasattr(agent, 'run_loop'):
                asyncio.create_task(agent.run_loop())

        # 最后记录时间戳
        last_minute = None

        while self._running:
            try:
                # 获取当前 Tick
                tick = await self.data_feed.get_next_tick(timeout=0.5)
                if tick is None:
                    continue

                now = tick.timestamp
                current_minute = now.replace(second=0, microsecond=0)

                # 每分钟执行一次 (新K线闭合)
                if current_minute != last_minute:
                    last_minute = current_minute
                    await self._on_new_minute(tick)
                else:
                    # 同1分钟内，做高频轻量处理 (仅更新部分指标)
                    await self._on_tick(tick)

                # 心跳写入 C++ 监视器
                if self.cpp_guard:
                    self.cpp_guard.heartbeat()

                # 行为日志推送 (可选，避免每Tick推送)
                if self.behavior_log.should_flush():
                    await self.behavior_log.flush_to_frontend()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log.error(f"主循环异常: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(0.1)

        await self._shutdown()

    async def _on_new_minute(self, tick):
        """每分钟K线闭合时执行的主要逻辑"""
        try:
            tf = "1m"
            ctx = self.ctx_factory.get(tf)
            ctx.add_kline(tick.ohlc)

            # 1. 系统自检
            health = self.self_check.run()
            if health.status != "OK":
                self.behavior_log.log(EventType.SYSTEM, "SelfCheck", f"异常: {health.status}")
                await self.notifier.alert_health(health)

            # 2. 跳跃检测 (防止插针干扰)
            if self.cpp_jump and self.cpp_jump.detect(tick.returns[-10:]):
                self.behavior_log.log(EventType.RISK, "JumpDetect", "检测到异常跳跃，冻结感知层")
                self.perception.freeze()

            # 3. 感知层推理
            pll_state = self.perception.update_pll(ctx)
            heston_state = self.perception.update_particle_filter(ctx)
            vib_state = self.perception.update_vib(ctx)

            # 4. 市场状态判定
            regime = self.state_machine.determine_regime(pll_state, heston_state, vib_state, ctx)

            # 5. 震荡过滤器
            ci = self.state_machine.choppiness_index(ctx)
            is_oscillation = self.state_machine.is_oscillation(ci, pll_state, hurst_bf=heston_state.hurst_bf)

            # 6. 因子评分 (惰性求值)
            if self.lazy_evaluator.should_full_evaluate(regime, ctx):
                factor_scores = self.lazy_evaluator.evaluate_all(ctx, pll_state, heston_state, vib_state)
                weights = self.weight_engine.get_weights(regime)
                score = self.scorecard.compute(factor_scores, weights)
            else:
                score = self.scorecard.last_score

            # 7. 策略决策
            signal = None
            if not is_oscillation and score > self.scorecard.threshold_long:
                signal = {"direction": "LONG", "score": score, "confidence": self._calc_confidence(pll_state)}
            elif not is_oscillation and score < self.scorecard.threshold_short:
                signal = {"direction": "SHORT", "score": score, "confidence": self._calc_confidence(pll_state)}

            # 8. 多周期仲裁
            self.arbiter.collect_signal(tf, signal)
            final_signal = self.arbiter.evaluate()

            # 9. 风险校验
            if final_signal and not self.risk_monitor.approve(final_signal):
                self.behavior_log.log(EventType.RISK, "RiskReject", f"评分{final_signal.get('score')}被风控否决")
                final_signal = None

            # 10. 执行订单
            if final_signal:
                await self._execute_signal(final_signal, tick, ctx)

            # 11. 幽灵影子更新
            await self.shadow_mgr.tick(tick)

            # 12. 抗辩式议会决策（替换原加权投票）
            parliament_decision = await self.adversarial_council.deliberate(ctx)
            if parliament_decision:
                self.behavior_log.log(
                    EventType.SYSTEM, "Parliament",
                    f"议会决策: 方向={parliament_decision.get('direction')}, 置信度={parliament_decision.get('confidence')}"
                )

            # 13. 学习时段检查
            await self.learning_guard.check_and_handle()

            # 14. 行为日志记录
            self.behavior_log.log(EventType.SIGNAL, "Engine",
                                  f"分钟闭合处理完成, 评分={score:.1f}, 信号={'有' if final_signal else '无'}")

        except Exception as e:
            self.log.error(f"每分钟处理异常: {e}")

    async def _on_tick(self, tick):
        """Tick级别快速处理 (无损实时性)"""
        # 仅更新 C++ 风控模块、移动止损等
        if self.cpp_guard:
            self.cpp_guard.feed_price(tick.last_price, tick.bid, tick.ask)
        # 更新止损 (若持仓)
        if self.order_mgr.has_position():
            self.risk_monitor.update_trailing_stop(tick.last_price)

    async def _execute_signal(self, signal: dict, tick, ctx):
        """执行交易指令"""
        try:
            size = self.position_ctrl.calc_size(signal, self.order_mgr.get_equity())
            order = self.execution.create_order(
                symbol=self.config.get("trading.symbol"),
                direction=signal["direction"],
                size=size,
                price=tick.last_price,
                order_type="LIMIT" if self.mode == "virtual" else "SMART"
            )
            if order:
                self.behavior_log.log(EventType.ORDER, "Execution",
                                      f"下单: {signal['direction']} {size} @ {tick.last_price}")
                # 多账户跟单
                await self.copy_trading.replicate(order)
        except Exception as e:
            self.log.error(f"下单执行失败: {e}")

    def _calc_confidence(self, pll_state) -> float:
        """基于锁相环锁相质量计算置信度"""
        if pll_state.snr_db > 10:
            return 0.9
        elif pll_state.snr_db > 6:
            return 0.7
        return 0.3

    async def _background_tasks(self):
        """后台定时任务"""
        while self._running:
            now = datetime.now()
            # 每日凌晨3点执行
            if now.hour == 3 and now.minute == 0:
                self.daily_tasks.run()
                await asyncio.sleep(60)
            await asyncio.sleep(30)

    async def _ota_check_loop(self):
        """OTA更新检查循环"""
        while self._running:
            await self.ota_updater.check_and_update()
            await asyncio.sleep(3600)  # 每小时检查一次

    async def _cold_archive_loop(self):
        """冷数据归档循环"""
        while self._running:
            await self.cold_archiver.archive_expired()
            await asyncio.sleep(3600)

    async def _shutdown(self):
        """优雅关闭"""
        self.log.info("正在关闭引擎...")
        self._shutting_down = True
        # 平掉所有仓位 (如果配置要求)
        if self.config.get("system.flat_on_exit", True) and self.mode == "live":
            await self.execution.close_all()
        # 停止数据源
        await self.data_feed.stop()
        # 保存状态
        self.weight_engine.save()
        self.behavior_log.flush_to_db()
        self.log.info("引擎已安全退出")


# ================== 入口 ==================
async def main():
    import argparse
    parser = argparse.ArgumentParser(description="火种量化引擎")
    parser.add_argument("--mode", type=str, default="virtual", choices=["live", "virtual", "ghost"], help="运行模式")
    parser.add_argument("--config", type=str, default="config/settings.yaml", help="配置文件路径")
    args = parser.parse_args()

    engine = FireSeedEngine(config_path=args.config)
    engine.mode = args.mode
    await engine.run()


if __name__ == "__main__":
    asyncio.run(main())
