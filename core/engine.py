#!/usr/bin/env python3
"""
火种系统 (FireSeed) 核心交易引擎
==================================
主事件循环负责：
- 行情接收与多周期分发
- 感知层调用 (粒子滤波、锁相环、VIB)
- 策略决策 (多因子评分、多周期仲裁、抗辩式议会)
- 风险管理与硬实时风控协同
- 订单执行 (网关、TWAP、冰山)
- 幽灵影子管理
- 行为日志记录
- 每日任务调度与学习守卫
- OTA 更新与冷数据归档
- 智能体议会 (12世界观异构智能体 + 对抗式决策)
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
from core.context_isolator import ContextFactory, Kline
from core.continuous_position import ContinuousPositionController
from core.plugin_manager import PluginManager
from core.self_check import SystemSelfCheck
from core.daily_tasks import DailyTaskScheduler
from core.intelligent_learning_guard import LearningGuard
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.lazy_evaluator import LazyFactorEvaluator
from core.compute_scheduler import ComputeScheduler
from core.copy_trading import CopyTradingEngine
from core.notifier import SystemNotifier

# ---------- 幽灵 & OTA ----------
from ghost.shadow_manager import ShadowManager
from ota.updater import OTAUpdater
from ota.health_check import OTAHealthCheck
from cold_storage.archiver import ColdArchiver

# ---------- 助手 ----------
from assistant.llm_gateway import LLMGateway
from integrations.messenger_hub import MessengerHub

# ---------- 智能体议会 (深入异构) ----------
from agents.worldview import WorldView, WorldViewManifesto, WorldViewAgent
from agents.extreme_rewards import ExtremeRewardFunctions
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

# ---------- C++ 高性能模块 ----------
try:
    from cpp.bindings import (
        RealtimeGuard, JumpDetector, IncrementalCovariance,
        RingQueue, FlatMsg, AdaptiveTwap, IcebergSlicer,
        FtrlLearner, VIBInference, EvtRiskEstimator,
        HardWatcher,
    )
    CPP_AVAILABLE = True
except ImportError:
    CPP_AVAILABLE = False
    RealtimeGuard = None
    JumpDetector = None
    IncrementalCovariance = None
    RingQueue = None
    FlatMsg = None
    AdaptiveTwap = None
    IcebergSlicer = None
    FtrlLearner = None
    VIBInference = None
    EvtRiskEstimator = None
    HardWatcher = None

# ---------- 日志 ----------
from utils.logger import setup_logging


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

        # 将行为日志器提前初始化，供后续模块使用
        self.behavior_log = BehavioralLogger(max_memory=500)
        # 消息推送与通知
        self.messenger = MessengerHub(self.config)
        self.notifier = SystemNotifier(self.messenger, self.behavior_log)

        self.scorecard = DynamicScoreCard(self.config)
        self.perception = PerceptionFusion(self.config)
        self.state_machine = TradingStateMachine(self.config)
        self.weight_engine = ConditionalWeightEngine(self.config)
        self.arbiter = MultiTFArbiter(self.config)
        self.ctx_factory = ContextFactory(self.config)
        self.position_ctrl = ContinuousPositionController(self.config)
        self.plugin_mgr = PluginManager(self.config)
        self.self_check = SystemSelfCheck(self.config)
        self.daily_tasks = DailyTaskScheduler(self.config, engine=self)
        self.learning_guard = LearningGuard(self.config, engine=self)
        self.lazy_evaluator = LazyFactorEvaluator(self.config)
        self.compute_scheduler = ComputeScheduler(self.config)
        self.copy_trading = CopyTradingEngine(
            self.config,
            self.order_mgr,
            self.execution,
            self.behavior_log,
        )

        # ------------------------- 幽灵与进化 -------------------------
        self.shadow_mgr = ShadowManager(
            self.config,
            self.order_mgr,
            self.execution,
            self.behavior_log,
            self.data_feed,
        )
        self.ota_updater = OTAUpdater(self.config)
        self.ota_health_check = OTAHealthCheck(self.config)
        self.cold_archiver = ColdArchiver(self.config)
        self.llm_gateway = LLMGateway(self.config, self.behavior_log)

        # ------------------------- 智能体议会(对抗式) -------------------------
        self.adversarial_council = AdversarialCouncil()
        self._init_agents()

        # ------------------------- C++ 模块 (可选) -------------------------
        self.cpp_guard: Optional[RealtimeGuard] = None
        self.cpp_jump: Optional[JumpDetector] = None
        self.cpp_covar: Optional[IncrementalCovariance] = None
        self.cpp_hard_watcher: Optional[HardWatcher] = None
        if CPP_AVAILABLE:
            if RealtimeGuard:
                self.cpp_guard = RealtimeGuard()
            if JumpDetector:
                self.cpp_jump = JumpDetector()
            if IncrementalCovariance:
                self.cpp_covar = IncrementalCovariance(20)
            if HardWatcher:
                self.cpp_hard_watcher = HardWatcher()
            self.log.info("C++ 高性能模块加载成功")

        # ------------------------- 运行模式 -------------------------
        self.mode = self.config.get("system.mode", "virtual")
        # 注册信号处理
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    # ======================== 智能体注册 ========================
    def _init_agents(self):
        """创建12个携带世界观的智能体并注册到对抗议会"""
        manifestos = {
            "sentinel": WorldViewManifesto(
                worldview=WorldView.MECHANICAL_MATERIALISM,
                core_belief="系统是可分解的钟表",
                primary_optimization_target="-log(F1_score)",
                adversary_worldview=WorldView.HOLISM,
                forbidden_data_source={"KLINE", "ORDERBOOK"},
                exclusive_data_source={"SYSTEM_METRICS"},
                time_scale="15s",
            ),
            "alchemist": WorldViewManifesto(
                worldview=WorldView.EVOLUTIONISM,
                core_belief="策略是适应环境的生命体",
                primary_optimization_target="sharpe * novelty",
                adversary_worldview=WorldView.EXISTENTIALISM,
                forbidden_data_source={"SYSTEM_METRICS", "REAL_TIME_PNL"},
                exclusive_data_source={"RESEARCH_PAPER", "FORUM_RSS"},
                time_scale="1d",
            ),
            "guardian": WorldViewManifesto(
                worldview=WorldView.EXISTENTIALISM,
                core_belief="市场的本质是无常",
                primary_optimization_target="-max_drawdown",
                adversary_worldview=WorldView.EVOLUTIONISM,
                forbidden_data_source={"ORDERBOOK", "SENTIMENT"},
                exclusive_data_source={"RETURNS_SERIES", "EVT"},
                time_scale="5m",
            ),
            "devils_advocate": WorldViewManifesto(
                worldview=WorldView.SKEPTICISM,
                core_belief="任何真理都需不断证伪",
                primary_optimization_target="F1(correct_veto)",
                adversary_worldview=WorldView.BAYESIANISM,
                forbidden_data_source={"LIVE_PRICE"},
                exclusive_data_source={"FAILURE_CASES", "ADVERSARIAL_SAMPLES"},
                time_scale="event",
            ),
            "godel_watcher": WorldViewManifesto(
                worldview=WorldView.INCOMPLETENESS,
                core_belief="系统永远无法完全理解自身",
                primary_optimization_target="missed_loss_during_sleep",
                adversary_worldview=WorldView.MECHANICAL_MATERIALISM,
                forbidden_data_source={"MARKET_DATA", "TRADE_SIGNAL"},
                exclusive_data_source={"META_META_DATA"},
                time_scale="1m",
            ),
            "env_inspector": WorldViewManifesto(
                worldview=WorldView.PHYSICALISM,
                core_belief="一切问题终将表现为物理参数异常",
                primary_optimization_target="uptime / error_count",
                adversary_worldview=WorldView.EVOLUTIONISM,
                forbidden_data_source={"KLINE", "POSITION"},
                exclusive_data_source={"CPU", "MEM", "DISK", "NETWORK"},
                time_scale="1m",
            ),
            "redundancy_auditor": WorldViewManifesto(
                worldview=WorldView.OCCAMS_RAZOR,
                core_belief="简洁是真理的标志",
                primary_optimization_target="-code_lines",
                adversary_worldview=WorldView.PLURALISM,
                forbidden_data_source={"ALL_MARKET_DATA"},
                exclusive_data_source={"PYTHON_AST"},
                time_scale="1d",
            ),
            "weight_calibrator": WorldViewManifesto(
                worldview=WorldView.BAYESIANISM,
                core_belief="概率是信念的量化",
                primary_optimization_target="OOS_sharpe - IS_sharpe",
                adversary_worldview=WorldView.SKEPTICISM,
                forbidden_data_source={"RAW_PRICE"},
                exclusive_data_source={"FACTOR_IC_SERIES"},
                time_scale="1d",
            ),
            "narrator": WorldViewManifesto(
                worldview=WorldView.HERMENEUTICS,
                core_belief="意义存在于叙述之中",
                primary_optimization_target="human_read_completion_rate",
                adversary_worldview=WorldView.OCCAMS_RAZOR,
                forbidden_data_source={"RAW_ORDER_FLOW"},
                exclusive_data_source={"COUNCIL_LOGS", "STRATEGY_STATS"},
                time_scale="1d",
            ),
            "diversity_enforcer": WorldViewManifesto(
                worldview=WorldView.PLURALISM,
                core_belief="正确的决策需要多样化的视角",
                primary_optimization_target="H(voting_distribution)",
                adversary_worldview=WorldView.OCCAMS_RAZOR,
                forbidden_data_source={"TRADE_HISTORY"},
                exclusive_data_source={"VOTE_CONSISTENCY"},
                time_scale="5m",
            ),
            "archive_guardian": WorldViewManifesto(
                worldview=WorldView.HISTORICISM,
                core_belief="一切当前状态都可从历史痕迹中理解",
                primary_optimization_target="archived_data / total_data",
                adversary_worldview=WorldView.HOLISM,
                forbidden_data_source={"REAL_TIME_ANYTHING"},
                exclusive_data_source={"FILE_SYSTEM", "DB_INTEGRITY"},
                time_scale="1h",
            ),
            "copy_trade_coordinator": WorldViewManifesto(
                worldview=WorldView.HOLISM,
                core_belief="部分异常反映整体失调",
                primary_optimization_target="sync_success_rate",
                adversary_worldview=WorldView.MECHANICAL_MATERIALISM,
                forbidden_data_source={"INDIVIDUAL_ACCOUNT_DETAIL"},
                exclusive_data_source={"SUB_ACCOUNT_API_STATUS"},
                time_scale="30s",
            ),
        }

        # 创建智能体实例
        agents = {
            "sentinel": SentinelAgent(self.behavior_log, self.notifier),
            "alchemist": AlchemistAgent(self.config, self.behavior_log, self.notifier),
            "guardian": GuardianAgent(self.config, self.risk_monitor, self.behavior_log, self.notifier),
            "devils_advocate": DevilsAdvocate(self.behavior_log, self.notifier),
            "godel_watcher": GodelWatcher(behavior_log=self.behavior_log, notifier=self.notifier),
            "env_inspector": EnvInspector(self.behavior_log, self.notifier),
            "redundancy_auditor": RedundancyAuditor(root=".", behavior_log=self.behavior_log),
            "weight_calibrator": WeightCalibrator(behavior_log=self.behavior_log, notifier=self.notifier),
            "narrator": NarratorAgent(self.behavior_log, self.notifier),
            "diversity_enforcer": DiversityEnforcer(self.behavior_log, self.notifier),
            "archive_guardian": ArchiveGuardian(behavior_log=self.behavior_log, notifier=self.notifier),
            "copy_trade_coordinator": CopyTradeCoordinator(self.behavior_log, self.notifier),
        }

        # 注入世界观并注册
        for name, agent in agents.items():
            manifesto = manifestos[name]
            # 轻量注入：将世界观直接赋值给智能体实例（各智能体类需预留 manifesto 属性）
            if hasattr(agent, "manifesto"):
                agent.manifesto = manifesto
            # 强制实现 propose / challenge 方法（由各智能体类负责）
            self.adversarial_council.register_agent(name, agent)

        self.log.info("12世界观异构智能体已注册到对抗式议会")

    # ======================== 主事件循环 ========================
    async def run(self):
        """主引擎循环"""
        self.log.info(f"火种引擎启动于 {self.mode} 模式")

        # 启动数据源
        await self.data_feed.start()
        # 启动计算资源调度
        self.compute_scheduler.start()

        # 可选 C++ 硬实时保护线程
        if self.cpp_guard:
            self.cpp_guard.start()

        # 启动后台任务
        asyncio.create_task(self._background_tasks())
        asyncio.create_task(self._ota_check_loop())
        asyncio.create_task(self._cold_archive_loop())

        # 最新一分钟跟踪
        last_minute = None

        while self._running:
            try:
                tick = await self.data_feed.get_next_tick(timeout=0.5)
                if tick is None:
                    continue

                now = tick.timestamp.replace(second=0, microsecond=0)
                # 新一分钟处理
                if now != last_minute:
                    last_minute = now
                    await self._on_new_minute(tick)
                else:
                    await self._on_tick(tick)

                # 更新 C++ 监视器心跳
                if self.cpp_guard:
                    self.cpp_guard.heartbeat()
                if self.cpp_hard_watcher:
                    self.cpp_hard_watcher.update_metrics(
                        rollback_count=self.plugin_mgr.rollback_count(),
                        strategy_churn=self.plugin_mgr.churn_rate(),
                        avg_sharpe=self.risk_monitor.avg_sharpe(),
                        self_doubt_hours=0,
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log.error(f"主循环异常: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(0.1)

        await self._shutdown()

    # ======================== 新分钟闭合 ========================
    async def _on_new_minute(self, tick):
        try:
            tf = "1m"
            ctx = self.ctx_factory.get_or_create(tf)
            kline = Kline(
                timestamp=tick.timestamp,
                open=tick.open_price,
                high=tick.high_price,
                low=tick.low_price,
                close=tick.last_price,
                volume=tick.volume,
            )
            ctx.add_kline(kline)
            ctx.add_orderbook(tick.orderbook)

            # 学习守卫更新
            self.learning_guard.update_market_state(
                volatility=self.scorecard.last_score,  # 简化
                volume=tick.volume,
                atr=self.perception._last_pf.atr,
            )
            await self.learning_guard.check_and_handle()

            # 感知
            pll_state = self.perception.update_pll(tick.last_price)
            pf_state = self.perception.update_particle_filter({
                "close": tick.last_price,
                "high": tick.high_price,
                "low": tick.low_price,
            })
            vib_state = self.perception.update_vib(self._extract_orderbook_vector(tick.orderbook))

            # 市场状态
            regime = self.state_machine.determine_regime(pll_state, pf_state, vib_state, ctx)

            # 因子计算 (惰性)
            if self.lazy_evaluator.should_full_evaluate(regime, ctx):
                factor_scores = self.lazy_evaluator.evaluate_all(ctx, pll_state, pf_state, vib_state)
                weights = self.weight_engine.get_weights(regime)
                score = self.scorecard.compute(factor_scores, weights)
            else:
                score = self.scorecard.last_score

            # 多周期仲裁 (收集各TF信号)
            for period in ("1m", "3m", "5m", "15m"):
                if period in self.ctx_factory._contexts:
                    ctx_tf = self.ctx_factory.get(period)
                    if ctx_tf:
                        pll_tf = self.perception.update_pll(kline.close) if period == "1m" else pll_state
                        pf_tf = pf_state
                        vib_tf = vib_state
                        signal = self._generate_tf_signal(period, ctx_tf, pll_tf, pf_tf, vib_tf, score if period == "1m" else None)
                        self.arbiter.collect_signal(period, signal)
            final_arb = self.arbiter.evaluate()

            # 构建议会感知数据包
            parliament_perception = {
                "regime": regime,
                "score": score,
                "pll_snr": pll_state.snr_db,
                "jump_detected": self.perception.is_frozen,
                "position_size": self.order_mgr.get_position_summary().size,
                "daily_pnl": self.order_mgr.get_daily_trading_stats().get("realized_pnl", 0),
                "circuit_breaker_level": self.risk_monitor.circuit_breaker.level,
            }

            # 对抗式议会审议
            council_decision = await self.adversarial_council.deliberate(parliament_perception)

            # 决策融合
            direction = council_decision.get("direction", 0)
            confidence = council_decision.get("confidence", 0.0)
            if direction == 0 and final_arb.direction != 0:
                direction = final_arb.direction
                confidence = max(confidence, final_arb.confidence * 0.8)

            # 风控审批
            if direction != 0 and self.risk_monitor.approve(direction, confidence):
                await self._execute_signal(direction, confidence, tick, ctx)

            # 影子更新
            await self.shadow_mgr.tick(tick)

            # 行为日志
            self.behavior_log.log(
                EventType.SYSTEM, "Engine",
                f"新分钟处理完成, 评分={score:.1f}, 议会方向={direction}",
                snapshot={"regime": regime, "council": council_decision}
            )

        except Exception as e:
            self.log.error(f"每分钟处理异常: {e}", exc_info=True)

    def _generate_tf_signal(self, tf, ctx, pll, pf, vib, base_score):
        from core.multi_tf_arbiter_v2 import TFSignal
        score = self.scorecard.compute(self.lazy_evaluator.last_scores) if base_score is None else base_score
        direction = 1 if score >= self.scorecard.threshold_long else (-1 if score <= self.scorecard.threshold_short else 0)
        return TFSignal(
            timeframe=tf,
            direction=direction,
            confidence=0.5 + abs(score - 50) / 100,
            score=score,
            timestamp=time.time(),
        )

    async def _on_tick(self, tick):
        # 快速风控与止损检查
        if self.cpp_guard:
            self.cpp_guard.feed_price(tick.last_price, tick.bid, tick.ask)
        if self.order_mgr.has_position():
            self.risk_monitor.update_trailing_stop(tick.last_price)

    async def _execute_signal(self, direction, confidence, tick, ctx):
        try:
            size = self.position_ctrl.calc_size(direction, confidence, self.order_mgr.get_equity())
            order = await self.execution.create_order(
                symbol=self.config.get("trading.symbol", "BTCUSDT"),
                side="buy" if direction == 1 else "sell",
                order_type="LIMIT",
                price=tick.last_price,
                quantity=size,
            )
            if order:
                self.behavior_log.log(EventType.ORDER, "Engine", f"下单: {direction} {size}")
                await self.copy_trading.replicate(order)
        except Exception as e:
            self.log.error(f"下单失败: {e}")

    # ======================== 后台任务 ========================
    async def _background_tasks(self):
        while self._running:
            now = datetime.now()
            if now.hour == 3 and now.minute == 0:
                await self.daily_tasks.run()
                await asyncio.sleep(60)
            await asyncio.sleep(30)

    async def _ota_check_loop(self):
        while self._running:
            await self.ota_updater.check_and_update()
            await asyncio.sleep(3600)

    async def _cold_archive_loop(self):
        while self._running:
            await self.cold_archiver.archive_expired()
            await asyncio.sleep(3600)

    # ======================== 辅助工具 ========================
    def _extract_orderbook_vector(self, ob) -> list:
        if not ob:
            return [0.0] * 44
        vec = []
        for level in (ob.get("bids", []) + ob.get("asks", [])):
            vec.extend(level)
        return vec[:44] + [0.0] * max(0, 44 - len(vec))

    def _signal_handler(self, signum, frame):
        self.log.warning(f"接收到信号 {signum}，开始优雅退出...")
        self._running = False

    async def _shutdown(self):
        self.log.info("正在关闭引擎...")
        self._shutting_down = True
        if self.mode == "live" and self.config.get("system.flat_on_exit", True):
            await self.execution.close_all_positions()
        await self.data_feed.stop()
        self.weight_engine.save()
        self.behavior_log.flush_to_db()
        self.log.info("引擎已安全退出")


# ================== 入口 ==================
async def main():
    import argparse
    parser = argparse.ArgumentParser(description="火种量化引擎")
    parser.add_argument("--mode", type=str, default="virtual", choices=["live", "virtual", "ghost"])
    parser.add_argument("--config", type=str, default="config/settings.yaml")
    args = parser.parse_args()

    engine = FireSeedEngine(config_path=args.config)
    engine.mode = args.mode
    await engine.run()


if __name__ == "__main__":
    asyncio.run(main())
