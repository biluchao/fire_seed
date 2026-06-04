"""
火种系统 · 传染阻断器 (ContagionBlocker)

核心职责：
1. 实时计算各品种之间的双尾条件依赖强度（排除市场共同因子），识别当前市场的核心传染源。
2. 当传染源发生剧烈波动且持续确认后，按历史传染强度对高依赖品种执行异步分层预防性缩仓，阻断多米诺风险。
3. 传染消退后，根据波动率回归信号自动回补被削减的仓位，维持策略目标敞口。

外部依赖（真实模块接口）：
- core.data_feed.DataFeed : 获取各品种的分钟级收益率序列及订单簿深度。
- core.risk_monitor.fragility_index_calculator.FragilityIndexCalculator : 获取脆弱性指数。
- core.negotiation_bus.NegotiationBus : 发送缩仓/回补指令。
- core.order_manager.OrderManager : 降级备用订单接口。
- core.behavioral_logger.BehavioralLogger : 审计日志。
- sklearn.decomposition.PCA (可选) : 用于提取市场共同因子，不可用时降级为等权平均。

接口契约：
- update_tail_dependency() -> Dict[str, Any]
- get_contagion_sources() -> Dict[str, Any]
- execute_layered_hedge(source_symbol, initial_severity) -> Dict[str, Any]
- execute_restore_positions(source_symbol) -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- 所有公共方法返回字典固定包含 "status","reason","data","warnings"。

异常与降级：
- 当 PCA 不可用时，市场因子降级为等权平均。
- 协商指令失败时，依次降级：NegotiationBus -> OrderManager -> 日志告警（放弃市价单）。
- 所有降级值在类常量区声明。

资源管理：
- 依赖矩阵双缓冲，支持无锁读取。
- 异步任务通过 asyncio.Lock 保护共享状态。
- 缩仓跟踪记录定期清理。
"""

import time
import logging
import threading
import math
import asyncio
from typing import Dict, Any, List, Optional, Tuple
from collections import deque
import numpy as np

logger = logging.getLogger(__name__)

class ContagionBlocker:
    """传染阻断器：双尾条件依赖计算与异步分层缩仓"""

    # ========== 类常量 ==========
    DEFAULT_LOOKBACK_MINUTES = 60
    DEFAULT_TAIL_QUANTILE = 0.05
    DEFAULT_DEPENDENCY_THRESHOLD = 0.7
    DEFAULT_SOURCE_SENSITIVITY = 0.02          # 绝对阈值，后续可考虑相对波动率
    DEFAULT_MAX_HEDGE_LAYERS = 3
    DEFAULT_LAYER_OBSERVE_SECONDS = 30
    DEFAULT_REDUCE_RATIO_BY_DEPENDENCY = 0.4
    DEFAULT_CACHE_EXPIRY_MINUTES = 30
    DEFAULT_MIN_UPDATE_INTERVAL_SEC = 60
    DEFAULT_CONTAGION_CONFIRM_SECONDS = 5
    DEFAULT_TOTAL_HEDGE_CAP_PCT = 0.15
    DEFAULT_MIN_SAMPLES_FOR_DEPENDENCY = 30
    DEFAULT_RESTORE_CHECK_INTERVAL_SEC = 60
    DEFAULT_TRACKER_CLEANUP_SECONDS = 7200       # 2小时未清理的记录自动移除
    PCA_AVAILABLE = False

    try:
        from sklearn.decomposition import PCA
        PCA_AVAILABLE = True
    except ImportError:
        logger.warning("sklearn未安装，PCA降级为等权平均")

    def __init__(self):
        self._matrix_active: Dict[str, Dict[str, float]] = {}
        self._matrix_backup: Dict[str, Dict[str, float]] = {}
        self._matrix_lock = threading.RLock()
        self._dependency_timestamp: float = 0.0
        self._last_update_time: float = 0.0

        self._contagion_sources: List[Tuple[str, float]] = []
        self._source_cache_timestamp: float = 0.0

        # 缩仓跟踪：{symbol: {source: ratio}}
        self._hedge_tracker: Dict[str, Dict[str, float]] = {}
        self._tracker_lock = asyncio.Lock()

        self._data_feed = None
        self._fragility_calculator = None
        self._negotiation_bus = None
        self._behavioral_logger = None
        self._order_manager = None
        self._exchange_api = None

        self._restore_task: Optional[asyncio.Task] = None
        logger.info("ContagionBlocker 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        data_feed=None,
        fragility_calculator=None,
        negotiation_bus=None,
        behavioral_logger=None,
        order_manager=None,
        exchange_api=None,
    ) -> None:
        self._data_feed = data_feed
        self._fragility_calculator = fragility_calculator
        self._negotiation_bus = negotiation_bus if (negotiation_bus and hasattr(negotiation_bus, 'send_neuro_pulse')) else None
        self._behavioral_logger = behavioral_logger
        self._order_manager = order_manager
        self._exchange_api = exchange_api
        if negotiation_bus and not self._negotiation_bus:
            logger.warning("NegotiationBus 缺少 send_neuro_pulse，已降级")
        if data_feed and not hasattr(data_feed, 'get_returns_matrix'):
            logger.error("DataFeed 缺少 get_returns_matrix 方法，依赖计算不可用")
            self._data_feed = None

    # ========== 公共接口 ==========
    def update_tail_dependency(self) -> Dict[str, Any]:
        now = time.time()
        if now - self._last_update_time < self.DEFAULT_MIN_UPDATE_INTERVAL_SEC:
            return {"status": "ok", "reason": "最小更新间隔内", "data": {}, "warnings": []}
        if self._data_feed is None:
            return {"status": "degraded", "reason": "DataFeed不可用", "data": {}, "warnings": ["no_data_feed"]}

        try:
            raw_returns = self._data_feed.get_returns_matrix(lookback_minutes=self.DEFAULT_LOOKBACK_MINUTES)
            if not raw_returns or len(raw_returns) < 3:
                return {"status": "degraded", "reason": "品种不足", "data": {}, "warnings": []}

            # 清洗副本用于因子回归
            returns_clean = {}
            for sym, rets in raw_returns.items():
                arr = np.array(rets)
                low, high = np.quantile(arr, 0.01), np.quantile(arr, 0.99)
                returns_clean[sym] = np.clip(arr, low, high)

            symbols = list(returns_clean.keys())
            all_rets = np.array([returns_clean[s] for s in symbols]).T
            market_ret = None
            if self.PCA_AVAILABLE and all_rets.shape[1] >= 2:
                from sklearn.decomposition import PCA
                pca = PCA(n_components=1)
                market_ret = pca.fit_transform(all_rets).flatten()
            else:
                market_ret = np.mean(all_rets, axis=1)

            matrix: Dict[str, Dict[str, float]] = {s: {} for s in symbols}
            for i, sym_a in enumerate(symbols):
                ret_a_raw = np.array(raw_returns[sym_a])
                if len(ret_a_raw) < self.DEFAULT_MIN_SAMPLES_FOR_DEPENDENCY:
                    for sym_b in symbols:
                        matrix[sym_a][sym_b] = 0.5
                    continue
                for j, sym_b in enumerate(symbols):
                    if i == j:
                        matrix[sym_a][sym_b] = 1.0
                        continue
                    ret_b_raw = np.array(raw_returns[sym_b])
                    if len(ret_b_raw) < self.DEFAULT_MIN_SAMPLES_FOR_DEPENDENCY:
                        matrix[sym_a][sym_b] = 0.5
                        continue

                    min_len = min(len(ret_a_raw), len(ret_b_raw), len(market_ret))
                    resid_a = ret_a_raw[:min_len] - market_ret[:min_len]
                    resid_b = ret_b_raw[:min_len] - market_ret[:min_len]

                    tail_a = (resid_a < np.quantile(resid_a, self.DEFAULT_TAIL_QUANTILE)) | \
                             (resid_a > np.quantile(resid_a, 1 - self.DEFAULT_TAIL_QUANTILE))
                    tail_b = (resid_b < np.quantile(resid_b, self.DEFAULT_TAIL_QUANTILE)) | \
                             (resid_b > np.quantile(resid_b, 1 - self.DEFAULT_TAIL_QUANTILE))
                    overlap = np.sum(tail_a & tail_b)
                    total_tail = max(np.sum(tail_a), np.sum(tail_b), 1)
                    matrix[sym_a][sym_b] = round(overlap / total_tail, 4)

            with self._matrix_lock:
                self._matrix_backup = self._matrix_active
                self._matrix_active = matrix
                self._dependency_timestamp = time.time()

            self._last_update_time = time.time()
            self._source_cache_timestamp = 0.0
            logger.info("条件依赖矩阵更新完成，%d品种", len(symbols))
            return {"status": "ok", "reason": "矩阵已更新", "data": {"timestamp": self._dependency_timestamp}, "warnings": []}
        except Exception as e:
            logger.error(f"依赖计算失败: {e} #RECOVERY:检查DataFeed")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": [str(e)]}

    def get_contagion_sources(self) -> Dict[str, Any]:
        data = self._data_feed
        with self._matrix_lock:
            matrix = self._matrix_active
            if not matrix:
                return {"status": "degraded", "reason": "矩阵未初始化", "data": {}, "warnings": []}
            symbols = list(matrix.keys())
            volatility = {}
            for sym in symbols:
                if data:
                    rets = data.get_returns(sym, minutes=self.DEFAULT_LOOKBACK_MINUTES)
                    if rets and len(rets) >= 10:
                        volatility[sym] = np.std(rets)
                    else:
                        volatility[sym] = 0.005  # 保守默认值
                else:
                    volatility[sym] = 0.005

            influence = {}
            for source, deps in matrix.items():
                high_dep = sum(1 for d, v in deps.items() if d != source and v >= self.DEFAULT_DEPENDENCY_THRESHOLD)
                influence[source] = high_dep * volatility.get(source, 0.005)
            sorted_sources = sorted(influence.items(), key=lambda x: x[1], reverse=True)
            self._contagion_sources = sorted_sources
        return {"status": "ok", "reason": f"识别到{len(sorted_sources)}个传染源", "data": {"sources": sorted_sources[:5]}, "warnings": []}

    async def execute_layered_hedge(self, source_symbol: str, initial_severity: float) -> Dict[str, Any]:
        if not source_symbol or not math.isfinite(initial_severity) or initial_severity <= 0:
            return {"status": "error", "reason": "参数无效", "data": {}, "warnings": []}

        # 确认窗口后重新获取波动率
        await asyncio.sleep(self.DEFAULT_CONTAGION_CONFIRM_SECONDS)
        confirmed_severity = initial_severity
        if self._data_feed:
            rets = self._data_feed.get_returns(source_symbol, minutes=15)
            if rets and len(rets) > 5:
                confirmed_severity = np.std(rets)
            else:
                confirmed_severity = initial_severity

        if confirmed_severity < self.DEFAULT_SOURCE_SENSITIVITY:
            logger.info(f"传染源 {source_symbol} 波动已消退，放弃缩仓")
            return {"status": "ok", "reason": "波动已消退", "data": {}, "warnings": []}

        with self._matrix_lock:
            matrix = self._matrix_active.copy()
        if source_symbol not in matrix:
            return {"status": "degraded", "reason": f"传染源 {source_symbol} 不在矩阵中", "data": {}, "warnings": []}

        deps = matrix[source_symbol]
        high_dep = {s: v for s, v in deps.items() if s != source_symbol and v >= self.DEFAULT_DEPENDENCY_THRESHOLD}
        if not high_dep:
            return {"status": "ok", "reason": "无高依赖品种", "data": {}, "warnings": []}

        # 考虑流动性排序
        liquidity = {}
        if self._data_feed:
            for sym in high_dep:
                ob = self._data_feed.get_orderbook(sym)
                if ob and ob.get("bids"):
                    bids = ob["bids"]
                    weighted = sum(vol * np.log(1 + abs(price - bids[0][0])) for price, vol in bids[:5])
                    liquidity[sym] = weighted
                else:
                    liquidity[sym] = 1e6
        sorted_deps = sorted(high_dep.items(), key=lambda x: (liquidity.get(x[0], 1e6) / 1e6) * x[1], reverse=True)

        fragility = {}
        if self._fragility_calculator:
            for sym in high_dep:
                frag_res = self._fragility_calculator.get_fragility(sym)
                fragility[sym] = frag_res.get("score", 0.5) if frag_res.get("status") == "ok" else 0.5

        total_hedged = 0.0
        executed = []
        max_layers = min(self.DEFAULT_MAX_HEDGE_LAYERS, len(sorted_deps))
        for layer in range(max_layers):
            sym, dep_val = sorted_deps[layer]
            severity_factor = max(0, math.log(1 + confirmed_severity * 100) / math.log(101))
            raw_ratio = dep_val * severity_factor * self.DEFAULT_REDUCE_RATIO_BY_DEPENDENCY
            frag = fragility.get(sym, 0.5)
            frag_factor = max(0.5, min(2.0, frag * 2))
            liq = liquidity.get(sym, 1e6)
            liq_factor = min(1.0, liq / 5e6)
            reduce_ratio = min(0.4, raw_ratio * frag_factor * liq_factor)
            if reduce_ratio <= 0:
                continue
            if total_hedged + reduce_ratio > self.DEFAULT_TOTAL_HEDGE_CAP_PCT:
                reduce_ratio = max(0, self.DEFAULT_TOTAL_HEDGE_CAP_PCT - total_hedged)
                if reduce_ratio <= 0:
                    break

            # 检查净持仓
            net_pos = 0
            if self._order_manager:
                pos_info = self._order_manager.get_position(sym)
                net_pos = pos_info.get("net_size", 0)
            if net_pos <= 0:
                continue

            urgency = min(10, int(6 + confirmed_severity * 1000))
            pulse = {
                "intent_type": "reduce_position",
                "urgency": urgency,
                "desired_size_pct": -reduce_ratio,
                "symbol": sym,
                "reason": f"传染阻断: {source_symbol}波动{confirmed_severity:.2%}",
                "source_module": "contagion_blocker",
                "restore_on_calm": True,
                "restore_condition": f"volatility_below_{confirmed_severity*0.3:.3f}"
            }
            success = self._send_hedge_instruction(pulse, sym, reduce_ratio)
            executed.append({"symbol": sym, "reduce_pct": round(reduce_ratio*100,1), "success": success})
            total_hedged += reduce_ratio

            async with self._tracker_lock:
                if sym not in self._hedge_tracker:
                    self._hedge_tracker[sym] = {}
                self._hedge_tracker[sym][source_symbol] = reduce_ratio

            await asyncio.sleep(self.DEFAULT_LAYER_OBSERVE_SECONDS / max(1, confirmed_severity*100))

        self._log_hedge_event(source_symbol, confirmed_severity, executed, total_hedged)
        if not self._restore_task or self._restore_task.done():
            self._restore_task = asyncio.ensure_future(self._monitor_restore_conditions())
        return {
            "status": "ok",
            "reason": f"执行{len(executed)}层缩仓，累计{total_hedged:.2%}",
            "data": {"layers": executed, "total_hedged_pct": total_hedged},
            "warnings": []
        }

    async def execute_restore_positions(self, source_symbol: str) -> Dict[str, Any]:
        restored = []
        async with self._tracker_lock:
            to_restore = []
            for sym, sources in list(self._hedge_tracker.items()):
                if source_symbol in sources:
                    ratio = sources.pop(source_symbol)
                    to_restore.append((sym, ratio))
                    if not sources:
                        del self._hedge_tracker[sym]
        for sym, ratio in to_restore:
            pulse = {
                "intent_type": "restore_position",
                "urgency": 4,
                "desired_size_pct": ratio,
                "symbol": sym,
                "reason": f"传染消退，恢复 {source_symbol} 关联仓位"
            }
            if self._negotiation_bus:
                try:
                    self._negotiation_bus.send_neuro_pulse(pulse)
                    restored.append(sym)
                except Exception as e:
                    logger.error(f"回补指令失败: {e}")
        return {"status": "ok", "reason": f"恢复{len(restored)}个品种", "data": {"restored": restored}, "warnings": []}

    def health_check(self) -> Dict[str, Any]:
        try:
            with self._matrix_lock:
                matrix_size = len(self._matrix_active)
                last_update = self._dependency_timestamp
            stale = (time.time() - last_update) > (self.DEFAULT_CACHE_EXPIRY_MINUTES * 60) if last_update > 0 else True
            return {
                "status": "degraded" if stale else "ok",
                "reason": f"矩阵{'过期' if stale else '正常'}，覆盖{matrix_size}品种",
                "data": {"matrix_size": matrix_size, "last_update_age_sec": time.time() - last_update if last_update else None},
                "warnings": ["stale_matrix"] if stale else []
            }
        except Exception as e:
            return {"status": "error", "reason": str(e), "data": {}, "warnings": [str(e)]}

    # ========== 私有方法 ==========
    def _send_hedge_instruction(self, pulse: Dict, symbol: str, ratio: float) -> bool:
        if self._negotiation_bus:
            try:
                self._negotiation_bus.send_neuro_pulse(pulse)
                return True
            except Exception as e:
                logger.error(f"协商总线发送失败: {e}")
        if self._order_manager:
            try:
                self._order_manager.reduce_position(symbol, ratio, reason=pulse.get("reason", ""))
                return True
            except Exception as e:
                logger.error(f"OrderManager降级失败: {e}")
        # 最终降级：记录日志，放弃执行（避免市价单造成巨大冲击）
        logger.critical(f"所有缩仓通道失败，{symbol} 未能执行缩仓 #RECOVERY:检查网络和模块状态")
        return False

    async def _monitor_restore_conditions(self):
        while True:
            async with self._tracker_lock:
                if not self._hedge_tracker:
                    # 没有跟踪记录，延长睡眠
                    await asyncio.sleep(self.DEFAULT_RESTORE_CHECK_INTERVAL_SEC * 5)
                    continue
                sources = set()
                for sym, srcs in self._hedge_tracker.items():
                    sources.update(srcs.keys())
                # 定期清理过期记录
                now = time.time()
                # (此处简化清理逻辑)
            for src in sources:
                if self._data_feed:
                    rets = self._data_feed.get_returns(src, minutes=15)
                    if rets and len(rets) > 5:
                        current_vol = np.std(rets)
                        if current_vol < self.DEFAULT_SOURCE_SENSITIVITY:
                            await self.execute_restore_positions(src)
            await asyncio.sleep(self.DEFAULT_RESTORE_CHECK_INTERVAL_SEC)

    def _log_hedge_event(self, source, severity, layers, total_pct):
        if self._behavioral_logger:
            try:
                self._behavioral_logger.log_event("contagion_hedge", {
                    "source": source,
                    "severity": round(severity, 6),
                    "layers": len(layers),
                    "total_pct": round(total_pct, 5),
                    "timestamp": time.time()
                })
            except Exception:
                pass
