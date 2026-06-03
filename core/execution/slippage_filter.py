"""
火种系统 · 滑点过滤器 (SlippageFilter)

核心职责：
1. 基于实时订单簿深度模拟订单簿穿透过程，预估市价单和限价单的预期滑点，并据此对订单规模进行限制或拒绝
2. 持续追踪各品种在滚动时间窗口内的累积滑点（含方向性统计与机会成本记录），超标时自动限制该品种的下单类型

外部依赖（真实模块接口）：
- core.perception.tactile_cortex.TactileCortex : 获取当前流动性评级、订单簿深度快照与买卖价差
- core.risk_monitor.order_risk_gateway.OrderRiskGateway : 查询单笔订单最大允许规模与品种日内累计限额
- core.account_ledger.AccountLedger : 获取当前账户权益用于规模比例换算
- core.behavioral_logger.BehavioralLogger : 记录滑点拒绝事件与累积滑点告警

接口契约：
- estimate_slippage(order_size: float, side: int, symbol: str, event_risk_factor: float = 1.0) -> Dict[str, Any]
- validate_order(order_size: float, side: int, symbol: str) -> Dict[str, Any]
- get_cumulative_slippage(symbol: str, window_seconds: Optional[float] = None) -> Dict[str, Any]
- record_fill(exec_price: float, mid_price: float, size: float, symbol: str, side: int) -> Dict[str, Any]
- record_fills_batch(fills: List[Dict[str, Any]]) -> Dict[str, Any]
- record_missed_opportunity(order_size: float, symbol: str, side: int, missed_price: float, mid_price: float) -> Dict[str, Any]
- get_rejection_attribution(symbol: Optional[str] = None) -> Dict[str, Any]
- health_check() -> Dict[str, Any]

异常与降级：
- 当 TactileCortex 不可用时，使用本地缓存深度快照；缓存连续过期则标记 stale_orderbook 告警
- 当 OrderRiskGateway 不可用时，使用类常量中预定义的最大订单量保守值作为安全上限
- 当 AccountLedger 不可用时，使用最近一次有效的权益快照计算规模比例
- 检测到订单簿空盘口时，返回 liquidity_vacuum 标记并强制仅限价单

资源管理：
- 维护每个品种的累积滑点滑动窗口（按时间与笔数），定期分批清理过期数据
- 不持有任何需要手动释放的外部资源
- 锁获取顺序固定：先 _cumulative_slippage_lock，再 _depth_cache_lock，防止死锁
- 锁内仅执行轻量操作，复杂计算移出锁外

统计稳定性：
- 历史滑点均值采用时间衰减加权，半衰期与品种波动率分位挂钩
- 关键操作（record_fill）使用更长锁超时，避免统计失真
- 所有数据队列均设置独立的过期清理
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple
from collections import deque
import numpy as np

logger = logging.getLogger(__name__)


class SlippageFilter:
    """滑点过滤器：预估滑点、限制订单规模、累积滑点监控"""

    # ========== 类常量（默认配置） ==========
    DEFAULT_MAX_SLIPPAGE_BPS = 5.0             # 单笔订单最大可接受滑点，基点，取值范围 [1.0, 20.0]
    DEFAULT_MAX_ORDER_SIZE_PCT_EQUITY = 0.5    # 单笔订单最大规模占权益比例，无量纲，[0.1, 2.0]
    DEFAULT_CUMULATIVE_SLIPPAGE_WINDOW_COUNT = 100  # 累积滑点监控窗口（交易笔数），无量纲，[20, 500]
    DEFAULT_CUMULATIVE_SLIPPAGE_WINDOW_SEC = 300     # 累积滑点监控时间窗口，秒，[60, 1800]
    DEFAULT_CUMULATIVE_SLIPPAGE_THRESHOLD_BPS = 30.0 # 窗口内累积滑点告警阈值，基点，[10.0, 100.0]
    DEFAULT_SLIPPAGE_DATA_MAX_AGE_SEC = 3600    # 滑点数据最大保留时间，秒，[600, 7200]
    DEFAULT_CLEANUP_INTERVAL_SEC = 300          # 过期数据清理间隔，秒，[60, 900]
    DEFAULT_CLEANUP_BATCH_SIZE = 500            # 每次清理最多处理的记录数，无量纲，[100, 2000]
    DEFAULT_DEPTH_SNAPSHOT_TTL_SEC = 2.0        # 深度快照缓存有效期，秒，[0.5, 5.0]
    DEFAULT_MIN_PRICE_PRECISION = 1e-8          # 最小有效价格精度，防除零

    # 深度穿透相关
    DEFAULT_FILL_PROBABILITY_BASE = 0.92        # 第一档成交概率系数，每档衰减至此值的幂，[0.8, 1.0]
    DEFAULT_OVERFLOW_PREMIUM_MULTIPLIER = 1.01  # 超出盘口深度时溢價倍数，[1.0, 1.1]
    DEFAULT_MAX_REDUCTION_ATTEMPTS = 20         # 最大缩减尝试次数

    # 告警与样本相关
    DEFAULT_MIN_SAMPLES_IN_WINDOW = 10          # 时间窗口内最小样本数，无量纲
    DEFAULT_STALE_CACHE_ALERT_THRESHOLD = 10    # 缓存连续过期次数告警阈值
    DEFAULT_SUGGESTED_LIMIT_OFFSET_BPS = 2.0    # 建议限价偏移量，基点
    DEFAULT_LIQUIDITY_VACUUM_OFFSET_BPS = 200   # 流动性真空时强制的限价偏移量，基点

    # 本地波动率估计参数
    DEFAULT_LOCAL_VOL_WINDOW_SEC = 300          # 本地波动率估计的时间窗口，秒
    DEFAULT_LOCAL_VOL_MIN_SAMPLES = 5           # 本地波动率估计最小样本数
    DEFAULT_LOCAL_VOL_MAXLEN = 5000             # 本地波动率队列最大长度（避免高频时被溢出清理）

    # 锁超时策略（区分关键/非关键操作）
    DEFAULT_LOCK_TIMEOUT_CRITICAL_SEC = 0.05    # 关键操作锁超时（record_fill等），秒
    DEFAULT_LOCK_TIMEOUT_NORMAL_SEC = 0.005     # 普通操作锁超时，秒

    # 历史均值半衰期（与波动率挂钩）
    DEFAULT_HALFLIFE_HIGH_VOL_SEC = 21600       # 高波动时半衰期，秒（6小时）
    DEFAULT_HALFLIFE_NORMAL_VOL_SEC = 86400     # 正常波动半衰期，秒（24小时）
    DEFAULT_HALFLIFE_LOW_VOL_SEC = 172800       # 低波动时半衰期，秒（48小时）

    # 拒绝归因记录参数
    DEFAULT_REJECTION_ATTRIBUTION_MAXLEN = 200  # 拒绝归因队列最大长度

    # 趋势分析参数
    DEFAULT_TREND_ANALYSIS_RECENT_COUNT = 50    # 趋势分析仅取最近N条记录

    def __init__(self):
        # 累积滑点统计（按品种）
        self._cumulative_slippage: Dict[str, deque] = {}
        self._opportunity_cost: Dict[str, deque] = {}
        self._cumulative_slippage_lock = threading.Lock()

        # 深度快照缓存
        self._depth_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._depth_cache_lock = threading.Lock()
        self._stale_cache_counters: Dict[str, int] = {}

        # 本地滑点波动率估计（独立于累积滑点窗口，避免高频时被过早溢出）
        self._local_slippage_vol: Dict[str, deque] = {}

        # 外部依赖注入
        self._tactile_cortex = None
        self._order_risk_gateway = None
        self._account_ledger = None
        self._behavioral_logger = None

        # 清理进度记录
        self._cleanup_cursors: Dict[str, int] = {}
        self._last_cleanup = time.time()

        # 锁等待统计
        self._lock_wait_times: deque = deque(maxlen=100)
        self._lock_contention_count = 0
        self._lock_contention_lock = threading.Lock()

        # 拒绝归因记录
        self._rejection_records: deque = deque(maxlen=self.DEFAULT_REJECTION_ATTRIBUTION_MAXLEN)
        self._rejection_lock = threading.Lock()

        # 趋势分析缓存（避免锁内重复计算）
        self._trend_cache: Dict[str, str] = {}
        self._trend_cache_ts: float = 0.0

        logger.info("SlippageFilter 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        tactile_cortex: Optional[Any] = None,
        order_risk_gateway: Optional[Any] = None,
        account_ledger: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        if tactile_cortex is not None:
            self._tactile_cortex = tactile_cortex
            logger.info("TactileCortex 注入成功")
        else:
            logger.warning("TactileCortex 未注入，滑点预估功能降级")

        if order_risk_gateway is not None:
            self._order_risk_gateway = order_risk_gateway
            logger.info("OrderRiskGateway 注入成功")
        else:
            logger.warning("OrderRiskGateway 未注入，使用保守的默认限额")

        if account_ledger is not None:
            self._account_ledger = account_ledger
            logger.info("AccountLedger 注入成功")
        else:
            logger.warning("AccountLedger 未注入，规模计算降级")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

    # ========== 公共接口 ==========
    def estimate_slippage(self, order_size: float, side: int, symbol: str, event_risk_factor: float = 1.0) -> Dict[str, Any]:
        if order_size <= 0:
            return {
                "status": "error", "reason": f"订单量必须为正数，当前值: {order_size}",
                "data": {}, "warnings": ["invalid_order_size"],
            }
        if side not in (1, -1):
            return {
                "status": "error", "reason": f"无效的方向: {side}，有效值为 1(买入) 或 -1(卖出)",
                "data": {}, "warnings": ["invalid_side"],
            }

        orderbook = self._get_orderbook_snapshot(symbol)
        if orderbook is None:
            return {
                "status": "degraded", "reason": f"无法获取 {symbol} 的订单簿深度快照",
                "data": {"estimated_slippage_bps": self.DEFAULT_MAX_SLIPPAGE_BPS},
                "warnings": ["orderbook_unavailable"],
            }

        if side == 1:
            levels = orderbook.get("asks", [])
        else:
            levels = orderbook.get("bids", [])

        if not levels:
            logger.error(f"{symbol} 订单簿深度为空，标记 liquidity_vacuum")
            return {
                "status": "degraded",
                "reason": f"{symbol} 流动性真空",
                "data": {
                    "estimated_slippage_bps": self.DEFAULT_MAX_SLIPPAGE_BPS * 10.0,
                    "liquidity_state": "liquidity_vacuum",
                    "force_limit_only": True,
                    "suggested_limit_offset_bps": self.DEFAULT_LIQUIDITY_VACUUM_OFFSET_BPS,
                },
                "warnings": ["liquidity_vacuum", "empty_orderbook_side"],
            }

        base_price = levels[0][0]
        if base_price < self.DEFAULT_MIN_PRICE_PRECISION:
            logger.error(f"{symbol} 盘口基准价格过低({base_price})，标记为 extreme_liquidity")
            return {
                "status": "degraded",
                "reason": f"{symbol} 盘口基准价格异常({base_price})",
                "data": {
                    "estimated_slippage_bps": self.DEFAULT_MAX_SLIPPAGE_BPS * 3.0,
                    "liquidity_state": "extreme_liquidity",
                },
                "warnings": ["extreme_liquidity", "zero_base_price"],
            }

        vol_percentile = self._get_volatility_percentile(symbol)
        fill_prob = self.DEFAULT_FILL_PROBABILITY_BASE
        if vol_percentile > 80:
            fill_prob = 0.82
        elif vol_percentile < 30:
            fill_prob = 0.95

        if event_risk_factor is not None and event_risk_factor > 0:
            fill_prob *= event_risk_factor
            logger.debug(f"应用事件风险系数 {event_risk_factor}，调整后 fill_prob={fill_prob:.4f}")

        remaining = order_size
        total_cost = 0.0
        for i, (price, volume) in enumerate(levels):
            if remaining <= 0:
                break
            effective_volume = volume * (fill_prob ** i)
            filled = min(remaining, effective_volume)
            total_cost += filled * price
            remaining -= filled

        if remaining > 0:
            last_price = levels[-1][0] if levels else base_price
            overflow_mult = self._get_overflow_multiplier(symbol)
            total_cost += remaining * last_price * overflow_mult

        avg_price = total_cost / order_size if order_size > 0 else base_price
        slippage_abs = avg_price - base_price if side == 1 else base_price - avg_price
        slippage_bps = (slippage_abs / base_price) * 10000

        logger.debug(
            f"滑点预估: {symbol} side={side} size={order_size:.4f} -> "
            f"avg={avg_price:.2f} base={base_price:.2f} slip={slippage_bps:.2f}bps"
        )

        return {
            "status": "ok",
            "reason": f"预估滑点 {slippage_bps:.2f} bps (波动率分位 {vol_percentile})",
            "data": {
                "estimated_slippage_bps": round(slippage_bps, 2),
                "base_price": base_price,
                "avg_price": round(avg_price, 2),
                "depth_consumed_pct": round((order_size - remaining) / order_size * 100, 1) if order_size > 0 else 100,
                "fill_probability_applied": fill_prob,
            },
            "warnings": ["insufficient_depth"] if remaining > 0 else [],
        }

    def validate_order(self, order_size: float, side: int, symbol: str) -> Dict[str, Any]:
        warnings = []
        max_allowed = self._get_max_order_size(symbol)
        if order_size > max_allowed:
            logger.warning(f"订单规模 {order_size:.4f} 超过最大允许值 {max_allowed:.4f}，自动缩减")
            order_size = max_allowed
            warnings.append("order_size_reduced")

        cumulative = self.get_cumulative_slippage(symbol, self.DEFAULT_CUMULATIVE_SLIPPAGE_WINDOW_SEC)
        cum_data = cumulative.get("data", {})

        snapshot = self._get_orderbook_snapshot(symbol)
        snapshot_ts = time.time()

        for attempt in range(self.DEFAULT_MAX_REDUCTION_ATTEMPTS):
            if time.time() - snapshot_ts > self.DEFAULT_DEPTH_SNAPSHOT_TTL_SEC:
                snapshot = self._get_orderbook_snapshot(symbol)
                snapshot_ts = time.time()

            est = self.estimate_slippage(order_size, side, symbol)
            if est["status"] == "error":
                self._record_rejection(symbol, side, order_size, est.get("reason", ""), est["data"].get("estimated_slippage_bps", 0))
                return {
                    "status": "error", "reason": est["reason"],
                    "data": {"allowed": False, "adjusted_size": 0.0},
                    "warnings": est.get("warnings", []),
                }

            if est["data"].get("liquidity_state") == "liquidity_vacuum":
                return {
                    "status": "ok",
                    "reason": "流动性真空，强制限价单",
                    "data": {
                        "allowed": True,
                        "force_limit_order": True,
                        "adjusted_size": round(order_size * 0.3, 8),
                        "estimated_slippage_bps": est["data"]["estimated_slippage_bps"],
                        "suggested_limit_offset_bps": self.DEFAULT_LIQUIDITY_VACUUM_OFFSET_BPS,
                    },
                    "warnings": warnings + ["liquidity_vacuum_forced_limit"],
                }

            est_slippage = est["data"]["estimated_slippage_bps"]
            cumulative_buy = cum_data.get("total_buy_slippage_bps", 0.0)
            cumulative_sell = cum_data.get("total_sell_slippage_bps", 0.0)
            cumulative_total = cumulative_buy + cumulative_sell

            if cumulative_total > self.DEFAULT_CUMULATIVE_SLIPPAGE_THRESHOLD_BPS:
                logger.warning(f"{symbol} 累积滑点 {cumulative_total:.1f}bps 超标(时间窗口)，强制限价单")
                return {
                    "status": "ok",
                    "reason": f"累积滑点超标({cumulative_total:.1f}bps)，强制限价单",
                    "data": {
                        "allowed": True,
                        "force_limit_order": True,
                        "adjusted_size": round(order_size * 0.5, 8),
                        "estimated_slippage_bps": round(est_slippage, 2),
                        "cumulative_buy_bps": round(cumulative_buy, 2),
                        "cumulative_sell_bps": round(cumulative_sell, 2),
                        "suggested_limit_offset_bps": self._get_suggested_offset(symbol, side),
                    },
                    "warnings": warnings + ["cumulative_slippage_exceeded"],
                }

            if est_slippage <= self.DEFAULT_MAX_SLIPPAGE_BPS:
                return {
                    "status": "ok",
                    "reason": f"订单校验通过，预估滑点 {est_slippage:.1f}bps",
                    "data": {
                        "allowed": True,
                        "adjusted_size": round(order_size, 8),
                        "estimated_slippage_bps": round(est_slippage, 2),
                        "cumulative_buy_bps": round(cumulative_buy, 2),
                        "cumulative_sell_bps": round(cumulative_sell, 2),
                    },
                    "warnings": warnings,
                }

            reduced_size = order_size * 0.5
            if reduced_size < self._get_min_order_size(symbol):
                logger.warning(f"滑点超标且无法继续缩减，拒绝订单: {symbol} size={order_size:.4f} after {attempt} attempts")
                mid_price = self._get_mid_price(symbol)
                if mid_price > 0:
                    self.record_missed_opportunity(order_size, symbol, side, mid_price, mid_price)
                self._record_rejection(symbol, side, order_size, f"slippage_exceeded_after_{attempt}_reductions", est_slippage)
                return {
                    "status": "ok",
                    "reason": f"滑点超标({est_slippage:.1f}bps)且经{attempt}次缩减仍无法满足",
                    "data": {"allowed": False, "adjusted_size": 0.0, "estimated_slippage_bps": round(est_slippage, 2)},
                    "warnings": warnings + ["slippage_exceeded", "order_rejected"],
                }
            logger.info(f"滑点超标({est_slippage:.1f}bps)，第{attempt+1}次缩减至 {reduced_size:.4f}")
            order_size = reduced_size

        logger.error(f"达到最大缩减次数({self.DEFAULT_MAX_REDUCTION_ATTEMPTS})，拒绝订单: {symbol}")
        self._record_rejection(symbol, side, order_size, "max_reduction_attempts_reached", est_slippage if 'est_slippage' in locals() else 0)
        return {
            "status": "ok",
            "reason": f"滑点超标，经{self.DEFAULT_MAX_REDUCTION_ATTEMPTS}次缩减仍无法满足",
            "data": {"allowed": False, "adjusted_size": 0.0},
            "warnings": warnings + ["max_reduction_attempts_reached"],
        }

    def get_cumulative_slippage(self, symbol: str, window_seconds: Optional[float] = None) -> Dict[str, Any]:
        self._try_cleanup()
        acquired = self._acquire_lock_with_timeout(self._cumulative_slippage_lock, critical=False)
        if not acquired:
            logger.warning(f"无法获取累积滑点锁，返回降级值 for {symbol}")
            return {
                "status": "degraded",
                "reason": "锁超时，无法获取累积滑点数据",
                "data": {"total_buy_slippage_bps": 0.0, "total_sell_slippage_bps": 0.0, "trade_count": 0},
                "warnings": ["lock_timeout"],
            }
        try:
            if symbol not in self._cumulative_slippage:
                return {
                    "status": "ok", "reason": f"{symbol} 暂无滑点记录",
                    "data": {"total_buy_slippage_bps": 0.0, "total_sell_slippage_bps": 0.0, "trade_count": 0},
                    "warnings": [],
                }

            records = self._cumulative_slippage[symbol]
            if not records:
                return {
                    "status": "ok", "reason": f"{symbol} 滑点记录为空",
                    "data": {"total_buy_slippage_bps": 0.0, "total_sell_slippage_bps": 0.0, "trade_count": 0},
                    "warnings": [],
                }

            if window_seconds is not None:
                cutoff = time.time() - window_seconds
                filtered = [r for r in records if r[0] >= cutoff]
                if len(filtered) < self.DEFAULT_MIN_SAMPLES_IN_WINDOW:
                    expanded_cutoff = time.time() - (window_seconds * 2)
                    filtered = [r for r in records if r[0] >= expanded_cutoff]
                    if len(filtered) < self.DEFAULT_MIN_SAMPLES_IN_WINDOW:
                        hist_avg_buy, hist_avg_sell = self._get_historical_avg_slippage(symbol)
                        return {
                            "status": "degraded",
                            "reason": f"{symbol} 时间窗口内样本不足，使用时间衰减均值",
                            "data": {
                                "total_buy_slippage_bps": round(hist_avg_buy, 2),
                                "total_sell_slippage_bps": round(hist_avg_sell, 2),
                                "trade_count": len(filtered),
                                "window_seconds": window_seconds,
                                "insufficient_samples": True,
                                "data_age_seconds": round(time.time() - records[-1][0], 1) if records else None,
                            },
                            "warnings": ["insufficient_samples_in_window"],
                        }
            else:
                filtered = list(records)

            buy_bps = sum(r[1] for r in filtered if r[2] == 1)
            sell_bps = sum(r[1] for r in filtered if r[2] == -1)
            trade_count = len(filtered)

            return {
                "status": "ok",
                "reason": f"{symbol} 窗口内买滑点{buy_bps:.1f}bps 卖滑点{sell_bps:.1f}bps",
                "data": {
                    "total_buy_slippage_bps": round(buy_bps, 2),
                    "total_sell_slippage_bps": round(sell_bps, 2),
                    "trade_count": trade_count,
                    "window_seconds": window_seconds,
                },
                "warnings": [],
            }
        finally:
            self._cumulative_slippage_lock.release()

    def record_fill(self, exec_price: float, mid_price: float, size: float, symbol: str, side: int) -> Dict[str, Any]:
        if mid_price <= 0 or size <= 0 or side not in (1, -1):
            return {
                "status": "error", "reason": f"无效参数",
                "data": {}, "warnings": ["invalid_parameters"],
            }

        slippage_bps = abs(exec_price - mid_price) / mid_price * 10000
        now = time.time()

        # 关键操作使用更长的锁超时，避免滑点统计失真
        acquired = self._acquire_lock_with_timeout(self._cumulative_slippage_lock, critical=True)
        if not acquired:
            logger.warning(f"无法获取锁记录滑点 for {symbol}")
            return {
                "status": "degraded", "reason": "锁超时，滑点记录未保存",
                "data": {}, "warnings": ["lock_timeout"],
            }
        try:
            if symbol not in self._cumulative_slippage:
                self._cumulative_slippage[symbol] = deque(maxlen=self.DEFAULT_CUMULATIVE_SLIPPAGE_WINDOW_COUNT)
            self._cumulative_slippage[symbol].append((now, slippage_bps, side))

            if symbol not in self._local_slippage_vol:
                self._local_slippage_vol[symbol] = deque(maxlen=self.DEFAULT_LOCAL_VOL_MAXLEN)
            self._local_slippage_vol[symbol].append((now, slippage_bps))
        finally:
            self._cumulative_slippage_lock.release()

        logger.debug(f"记录滑点: {symbol} exec={exec_price:.2f} mid={mid_price:.2f} side={side} -> {slippage_bps:.2f}bps")

        return {
            "status": "ok",
            "reason": f"已记录 {symbol} 滑点 {slippage_bps:.2f}bps",
            "data": {"slippage_bps": round(slippage_bps, 2), "symbol": symbol, "side": side},
            "warnings": [],
        }

    def record_fills_batch(self, fills: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not fills:
            return {"status": "error", "reason": "fills 列表为空", "data": {}, "warnings": []}

        success_count = 0
        error_count = 0
        now = time.time()

        # 批量记录也使用关键操作锁超时
        acquired = self._acquire_lock_with_timeout(self._cumulative_slippage_lock, critical=True)
        if not acquired:
            logger.warning("无法获取锁进行批量滑点记录")
            return {
                "status": "degraded", "reason": "锁超时，批量记录未保存",
                "data": {}, "warnings": ["lock_timeout"],
            }
        try:
            for fill in fills:
                try:
                    symbol = fill["symbol"]
                    side = fill["side"]
                    exec_price = fill["exec_price"]
                    mid_price = fill["mid_price"]
                    size = fill["size"]

                    if mid_price <= 0 or size <= 0 or side not in (1, -1):
                        error_count += 1
                        continue

                    slippage_bps = abs(exec_price - mid_price) / mid_price * 10000
                    if symbol not in self._cumulative_slippage:
                        self._cumulative_slippage[symbol] = deque(maxlen=self.DEFAULT_CUMULATIVE_SLIPPAGE_WINDOW_COUNT)
                    self._cumulative_slippage[symbol].append((now, slippage_bps, side))

                    if symbol not in self._local_slippage_vol:
                        self._local_slippage_vol[symbol] = deque(maxlen=self.DEFAULT_LOCAL_VOL_MAXLEN)
                    self._local_slippage_vol[symbol].append((now, slippage_bps))
                    success_count += 1
                except Exception as e:
                    logger.warning(f"批量记录滑点单条失败: {e}")
                    error_count += 1
        finally:
            self._cumulative_slippage_lock.release()

        logger.info(f"批量记录滑点: 成功 {success_count}, 失败 {error_count}")
        return {
            "status": "ok",
            "reason": f"批量记录完成，成功 {success_count} 条，失败 {error_count} 条",
            "data": {"success_count": success_count, "error_count": error_count},
            "warnings": [],
        }

    def record_missed_opportunity(
        self, order_size: float, symbol: str, side: int, missed_price: float, mid_price: float
    ) -> Dict[str, Any]:
        if mid_price <= 0:
            return {
                "status": "error", "reason": f"无效的中间价: {mid_price}",
                "data": {}, "warnings": ["invalid_mid_price"],
            }

        est = self.estimate_slippage(order_size, side, symbol)
        if est["status"] != "ok":
            return est

        opportunity_bps = est["data"]["estimated_slippage_bps"]
        now = time.time()

        acquired = self._acquire_lock_with_timeout(self._cumulative_slippage_lock, critical=False)
        if not acquired:
            logger.warning(f"无法获取锁记录机会成本 for {symbol}")
            return {"status": "degraded", "reason": "锁超时", "data": {}, "warnings": ["lock_timeout"]}
        try:
            if symbol not in self._opportunity_cost:
                self._opportunity_cost[symbol] = deque(maxlen=self.DEFAULT_CUMULATIVE_SLIPPAGE_WINDOW_COUNT)
            self._opportunity_cost[symbol].append((now, opportunity_bps))
        finally:
            self._cumulative_slippage_lock.release()

        logger.info(
            f"记录机会成本: {symbol} side={side} size={order_size:.4f} "
            f"missed_price={missed_price:.2f} opportunity={opportunity_bps:.2f}bps"
        )

        return {
            "status": "ok",
            "reason": f"机会成本 {opportunity_bps:.2f}bps 已记录",
            "data": {
                "opportunity_cost_bps": round(opportunity_bps, 2),
                "symbol": symbol, "side": side, "order_size": order_size,
            },
            "warnings": [],
        }

    def get_rejection_attribution(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """获取滑点拒绝的归因分析"""
        with self._rejection_lock:
            records = list(self._rejection_records)
            if symbol:
                records = [r for r in records if r.get("symbol") == symbol]

        if not records:
            return {
                "status": "ok",
                "reason": "暂无拒绝记录",
                "data": {"rejection_count": 0, "attribution": {}},
                "warnings": [],
            }

        attribution = {}
        for r in records:
            key = f"{r['symbol']}:{r['reason']}"
            if key not in attribution:
                attribution[key] = {"count": 0, "total_size": 0.0, "avg_slippage_bps": 0.0}
            attribution[key]["count"] += 1
            attribution[key]["total_size"] += r["order_size"]
            attribution[key]["avg_slippage_bps"] += r["estimated_slippage_bps"]

        for k in attribution:
            attribution[k]["avg_slippage_bps"] = round(
                attribution[k]["avg_slippage_bps"] / attribution[k]["count"], 2
            )

        return {
            "status": "ok",
            "reason": f"共 {len(records)} 条拒绝记录",
            "data": {
                "rejection_count": len(records),
                "attribution": attribution,
            },
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        try:
            if not hasattr(self, '_cumulative_slippage'):
                return {
                    "status": "degraded", "reason": "数据结构未初始化",
                    "data": {}, "warnings": ["not_initialized"],
                }

            # 锁外获取基本统计（使用轻量采样）
            monitored_symbols = 0
            total_records = 0
            total_opportunity = 0
            trends_data: Dict[str, List[float]] = {}
            opportunity_depths: Dict[str, int] = {}

            acquired = self._acquire_lock_with_timeout(self._cumulative_slippage_lock, critical=False)
            if acquired:
                try:
                    monitored_symbols = len(self._cumulative_slippage)
                    for symbol in self._cumulative_slippage:
                        records = self._cumulative_slippage[symbol]
                        total_records += len(records)
                        # 仅提取最近N条用于趋势分析（轻量操作）
                        slice_size = min(self.DEFAULT_TREND_ANALYSIS_RECENT_COUNT, len(records))
                        recent_slippages = [records[i][1] for i in range(-slice_size, 0)] if slice_size > 0 else []
                        trends_data[symbol] = recent_slippages
                    total_opportunity = sum(len(v) for v in self._opportunity_cost.values())
                    for symbol in self._opportunity_cost:
                        opportunity_depths[symbol] = len(self._opportunity_cost[symbol])
                finally:
                    self._cumulative_slippage_lock.release()
            else:
                logger.warning("health_check 无法获取锁，使用部分数据")

            # 锁外执行趋势分析（np.mean不持锁）
            trends = {}
            for symbol, slippages in trends_data.items():
                if len(slippages) < 20:
                    trends[symbol] = "insufficient_data"
                    continue
                half = len(slippages) // 2
                first_half_avg = np.mean(slippages[:half])
                second_half_avg = np.mean(slippages[half:])
                if second_half_avg > first_half_avg * 1.15:
                    trends[symbol] = "rising"
                elif second_half_avg < first_half_avg * 0.85:
                    trends[symbol] = "falling"
                else:
                    trends[symbol] = "stable"

            # 锁外读取缓存过期计数
            stale_alerts = {
                sym: count
                for sym, count in self._stale_cache_counters.items()
                if count >= self.DEFAULT_STALE_CACHE_ALERT_THRESHOLD
            }

            # 锁外读取锁统计
            with self._lock_contention_lock:
                avg_wait = np.mean(list(self._lock_wait_times)) * 1000 if self._lock_wait_times else 0.0
                max_wait = max(self._lock_wait_times) * 1000 if self._lock_wait_times else 0.0
                contention_count = self._lock_contention_count

            # 机会成本最大深度告警
            max_opp_depth = max(opportunity_depths.values()) if opportunity_depths else 0

            warnings = []
            if stale_alerts:
                for sym, count in stale_alerts.items():
                    msg = f"{sym} 深度缓存连续 {count} 次过期"
                    warnings.append(msg)

            if avg_wait > 5.0:
                warnings.append(f"锁平均等待时间 {avg_wait:.1f}ms 过高")

            if max_opp_depth > self.DEFAULT_CUMULATIVE_SLIPPAGE_WINDOW_COUNT * 0.9:
                warnings.append(f"机会成本队列深度 {max_opp_depth} 接近上限")

            return {
                "status": "degraded" if warnings else "ok",
                "reason": f"SlippageFilter 正常，监控 {monitored_symbols} 品种",
                "data": {
                   
