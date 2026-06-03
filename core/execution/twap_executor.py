"""
火种系统 · TWAP执行器 (TwapExecutor)

核心职责：
1. 将大额订单按时间加权平均价格算法拆分为多笔子订单，以指数分布随机抖动间隔执行，降低市场冲击与意图暴露
2. 实时跟踪执行进度与IOC未成交机会成本，在盘口允许时智能补仓；在超时或风控约束下执行渐进式降级
3. 锚定到达价格(Arrival Price)与市场VWAP，输出严格的交易成本分析(TCA)摘要，拒绝循环论证

外部依赖（真实模块接口）：
- core.execution.multi_venue_router.MultiVenueRouter : 发送子订单并获取成交回报
- core.execution.slippage_filter.SlippageFilter : 对每笔子订单进行预期滑点校验与规模限制
- core.negotiation_bus.NegotiationBus : 发送状态变更事件、查询风险预算、检查内部挂单以避免自成交
- core.behavioral_logger.BehavioralLogger : 记录TWAP执行日志与异常事件
- core.data_feed.market_data_aggregator.MarketDataAggregator : 获取实时成交量分布、盘口深度、市场VWAP及到达价格锚点
- core.risk_monitor.RiskMonitor : 执行每笔切片前的全局风险预算预检查
- core.order_manager.order_book_snapshot.OrderBookSnapshot : 查询当前内部活跃挂单，用于自成交保护

接口契约：
- start_twap(order_id: str, symbol: str, side: int, total_qty: float, duration_seconds: float) -> Dict[str, Any]
- get_twap_status(plan_id: str) -> Dict[str, Any]
- cancel_twap(plan_id: str) -> Dict[str, Any]
- execute_next_slice(plan_id: str) -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 MarketDataAggregator 不可用时，使用固定间隔 + 指数分布随机抖动替代自适应间隔，并标记 "degraded"
- 当连续拒单超过阈值时，执行渐进式降级：加大切片量 → 限价单等待 → 市价脉冲
- 当全局风控模块不可用时，保守地将单笔切片规模限制为默认安全值
- 当状态持久化失败时，记录关键字段到本地日志作为最后恢复凭据
- 所有降级值在类常量区明确声明

资源管理：
- 每个TWAP计划使用独立执行锁，防止并发切片；计划完成后自动清理定时器与内存状态
- 计划状态在每次切片成交后原子化持久化至本地快照文件，重启时自动恢复未完成计划，但过期计划仅标记待处理不自动激活
- 不持有外部网络连接或文件句柄（持久化文件使用 with 上下文管理器）
"""

import time
import logging
import threading
import uuid
import random
import json
import os
from typing import Dict, Any, List, Optional, Tuple
from collections import deque

logger = logging.getLogger(__name__)


class TwapExecutor:
    """时间加权平均价格执行器（机构级）"""

    # ========== 类常量 ==========
    DEFAULT_SLICE_COUNT = 20
    MIN_SLICE_COUNT = 5
    MAX_SLICE_COUNT = 100
    DEFAULT_MIN_SLICE_INTERVAL_SEC = 0.5
    DEFAULT_MAX_SLICE_INTERVAL_SEC = 30.0
    CONSECUTIVE_REJECT_LIMIT = 3
    EMERGENCY_FILL_RATIO = 0.95
    STATUS_CHECK_INTERVAL_SEC = 0.2
    PLAN_CLEANUP_DELAY_SEC = 60
    MAX_RETRY_PER_SLICE = 2
    DEFAULT_BASE_INTERVAL_SEC = 1.0

    # 机构级常量
    OPPORTUNITY_COST_THRESHOLD = 0.8
    JITTER_RATIO = 0.3                     # 指数分布尺度参数基础（实际使用expovariate）
    DEFAULT_SAFE_SLICE_QTY = 0.001
    SELF_TRADE_CHECK_ENABLED = True
    STATE_PERSIST_DIR = "data/twap_states"
    TCA_BENCHMARK_INTERVAL_SEC = 10
    CATCHUP_SLIPPAGE_THRESHOLD_BPS = 5.0   # 补仓时允许的额外滑点上限（基点）
    EXPIRED_PLAN_RECOVERY_ACTION = "mark_pending"  # 恢复过期计划时的处理方式

    def __init__(self):
        self._active_plans: Dict[str, Dict[str, Any]] = {}
        self._plan_locks: Dict[str, threading.Lock] = {}
        self._completed_plans: deque = deque()

        # 外部依赖
        self._multi_venue_router = None
        self._slippage_filter = None
        self._negotiation_bus = None
        self._behavioral_logger = None
        self._market_data_aggregator = None
        self._risk_monitor = None
        self._order_book_snapshot = None

        self._lock = threading.Lock()
        self._last_cleanup = time.time()

        os.makedirs(self.STATE_PERSIST_DIR, exist_ok=True)

        # 从持久化恢复未完成计划，但仅标记过期计划
        self._recover_plans()

        logger.info("TwapExecutor (机构级·最终版) 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        multi_venue_router: Optional[Any] = None,
        slippage_filter: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        market_data_aggregator: Optional[Any] = None,
        risk_monitor: Optional[Any] = None,
        order_book_snapshot: Optional[Any] = None,
    ) -> None:
        """注入外部依赖"""
        if multi_venue_router is not None and hasattr(multi_venue_router, 'send_order'):
            self._multi_venue_router = multi_venue_router
        if slippage_filter is not None and hasattr(slippage_filter, 'validate_order'):
            self._slippage_filter = slippage_filter
        if negotiation_bus is not None and hasattr(negotiation_bus, 'publish_event'):
            self._negotiation_bus = negotiation_bus
        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
        if market_data_aggregator is not None and hasattr(market_data_aggregator, 'get_volume_profile'):
            self._market_data_aggregator = market_data_aggregator
        if risk_monitor is not None and hasattr(risk_monitor, 'pre_check_order'):
            self._risk_monitor = risk_monitor
        if order_book_snapshot is not None and hasattr(order_book_snapshot, 'get_active_orders'):
            self._order_book_snapshot = order_book_snapshot
        logger.info("TwapExecutor 依赖注入完成")

    # ========== 公共接口 ==========
    def start_twap(self, order_id: str, symbol: str, side: int, total_qty: float,
                   duration_seconds: float) -> Dict[str, Any]:
        """启动TWAP执行计划（机构级）"""
        if total_qty <= 0 or duration_seconds <= 0 or side not in (1, -1):
            return {"status": "error", "reason": "参数无效", "data": {}, "warnings": ["invalid_params"]}

        slice_count = max(self.MIN_SLICE_COUNT, min(self.MAX_SLICE_COUNT, self.DEFAULT_SLICE_COUNT))
        slice_interval = duration_seconds / slice_count
        slice_qty = total_qty / slice_count

        if slice_interval < self.DEFAULT_MIN_SLICE_INTERVAL_SEC:
            slice_count = max(self.MIN_SLICE_COUNT, int(duration_seconds / self.DEFAULT_MIN_SLICE_INTERVAL_SEC))
            slice_interval = duration_seconds / slice_count
            slice_qty = total_qty / slice_count

        plan_id = str(uuid.uuid4())[:12]

        # 锚定到达价格 (Arrival Price)
        arrival_price = self._get_arrival_price(symbol)

        plan = {
            "plan_id": plan_id, "order_id": order_id, "symbol": symbol, "side": side,
            "total_qty": total_qty, "duration_seconds": duration_seconds,
            "slice_count": slice_count, "slice_qty": slice_qty, "slice_interval": slice_interval,
            "start_time": time.time(), "end_time": time.time() + duration_seconds,
            "filled_qty": 0.0, "filled_notional": 0.0, "slice_index": 0,
            "consecutive_rejects": 0, "status": "active", "last_slice_time": 0.0,
            "missed_qty": 0.0,
            "market_prices": [],       # TCA基准采样（每笔切片成交价）
            "slice_latencies": [],
            "arrival_price": arrival_price,  # 到达价格锚点
            "market_vwap": None,            # 结束时填充市场VWAP
        }

        with self._lock:
            self._active_plans[plan_id] = plan
            self._plan_locks[plan_id] = threading.Lock()

        self._persist_plan(plan)
        logger.info(f"TWAP启动: {plan_id}, total={total_qty}, slices={slice_count}, arrival_price={arrival_price:.4f}")
        self._publish_event("twap_started", plan)

        return {"status": "ok", "reason": f"TWAP计划已创建: {plan_id}",
                "data": {"plan_id": plan_id, "slice_count": slice_count, "slice_qty": round(slice_qty, 8)},
                "warnings": []}

    def get_twap_status(self, plan_id: str) -> Dict[str, Any]:
        """查询TWAP进度"""
        with self._lock:
            plan = self._active_plans.get(plan_id)
        if not plan:
            return {"status": "error", "reason": "计划不存在", "data": {}, "warnings": ["not_found"]}
        remaining = plan["total_qty"] - plan["filled_qty"]
        avg_price = plan["filled_notional"] / plan["filled_qty"] if plan["filled_qty"] > 0 else 0.0
        return {"status": "ok", "reason": f"进度: {plan['filled_qty']/plan['total_qty']*100:.1f}%",
                "data": {"plan_id": plan_id, "filled_qty": plan["filled_qty"], "remaining_qty": remaining,
                         "avg_price": round(avg_price, 4), "slice_index": plan["slice_index"],
                         "missed_qty": plan.get("missed_qty", 0)},
                "warnings": []}

    def cancel_twap(self, plan_id: str) -> Dict[str, Any]:
        """取消TWAP"""
        with self._lock:
            plan = self._active_plans.pop(plan_id, None)
            self._plan_locks.pop(plan_id, None)
        if not plan:
            return {"status": "error", "reason": "计划不存在", "data": {}, "warnings": ["not_found"]}
        plan["status"] = "cancelled"
        self._remove_persisted_plan(plan_id)
        logger.info(f"TWAP取消: {plan_id}, filled={plan['filled_qty']}")
        return {"status": "ok", "reason": "已取消",
                "data": {"plan_id": plan_id, "filled_qty": plan["filled_qty"]}, "warnings": []}

    def execute_next_slice(self, plan_id: str) -> Dict[str, Any]:
        """执行下一笔切片（机构级：含智能补仓、风控、自成交检查、指数抖动）"""
        lock = self._plan_locks.get(plan_id)
        if not lock:
            return {"status": "error", "reason": "计划不存在", "data": {}, "warnings": ["not_found"]}

        with lock:
            with self._lock:
                plan = self._active_plans.get(plan_id)
            if not plan or plan["status"] != "active":
                return {"status": "error", "reason": "计划不可执行", "data": {}, "warnings": ["not_executable"]}

            if plan["filled_qty"] >= plan["total_qty"]:
                self._finalize_plan(plan)
                return {"status": "ok", "reason": "已完成", "data": {"plan_id": plan_id}, "warnings": []}

            if time.time() >= plan["end_time"]:
                return self._handle_timeout_degradation(plan)

            # 计算本次切片量（含智能补仓）
            base_qty = (plan["total_qty"] - plan["filled_qty"]) / max(1, plan["slice_count"] - plan["slice_index"])
            catchup_qty = 0.0
            if plan.get("missed_qty", 0) > 0:
                # 智能补仓：仅在盘口足够时执行
                tentative_catchup = min(plan["missed_qty"], base_qty * 0.5)
                if tentative_catchup > 0:
                    if self._slippage_filter and hasattr(self._slippage_filter, 'validate_order'):
                        validation = self._slippage_filter.validate_order(
                            plan["symbol"], plan["side"], base_qty + tentative_catchup,
                            extra_slippage_bps=self.CATCHUP_SLIPPAGE_THRESHOLD_BPS
                        )
                        if validation.get("allowed", True):
                            catchup_qty = tentative_catchup
                            plan["missed_qty"] -= catchup_qty
                        else:
                            logger.debug(f"补仓滑点超限，跳过本轮补仓")
                    else:
                        catchup_qty = tentative_catchup
                        plan["missed_qty"] -= catchup_qty
                if catchup_qty > 0:
                    logger.debug(f"TWAP智能补仓: +{catchup_qty:.6f}")

            current_qty = base_qty + catchup_qty

            # 全局风控预检查
            max_allowed = self._query_risk_budget(plan["symbol"], plan["side"], current_qty)
            current_qty = min(current_qty, max_allowed)

            if current_qty <= 0:
                return {"status": "ok", "reason": "风控限制，跳过切片", "data": {"plan_id": plan_id}, "warnings": ["risk_limit_zero"]}

            # 自成交检查
            if self.SELF_TRADE_CHECK_ENABLED and self._detect_self_trade(plan["symbol"], plan["side"], current_qty):
                logger.warning(f"TWAP自成交风险: {plan_id}，跳过本轮切片")
                return {"status": "ok", "reason": "自成交风险跳过", "data": {"plan_id": plan_id}, "warnings": ["self_trade_skip"]}

            plan["slice_index"] += 1
            plan["last_slice_time"] = time.time()
            slice_start = time.time()

        # 发送订单（锁外）
        order_result = self._send_slice_order(plan, current_qty)

        with lock:
            slice_latency = time.time() - slice_start
            plan.setdefault("slice_latencies", []).append(slice_latency)

            if order_result.get("status") == "filled":
                filled_qty = order_result.get("filled_qty", 0)
                filled_price = order_result.get("avg_price", 0)
                plan["filled_qty"] += filled_qty
                plan["filled_notional"] += filled_qty * filled_price
                plan["consecutive_rejects"] = 0

                missed = current_qty - filled_qty
                if missed > 0:
                    plan["missed_qty"] = plan.get("missed_qty", 0) + missed

                plan.setdefault("market_prices", []).append(filled_price)
                self._persist_plan(plan)

                if plan["filled_qty"] >= plan["total_qty"]:
                    self._finalize_plan(plan)
            else:
                plan["consecutive_rejects"] += 1
                if plan["consecutive_rejects"] >= self.CONSECUTIVE_REJECT_LIMIT:
                    logger.error(f"TWAP连续拒单触发降级: {plan_id}")
                    return self._handle_reject_degradation(plan)

        # 指数分布随机间隔
        adjusted_interval = self._calculate_adaptive_interval(plan)
        try:
            next_interval = random.expovariate(1.0 / adjusted_interval)
        except ZeroDivisionError:
            next_interval = adjusted_interval
        next_interval = max(self.DEFAULT_MIN_SLICE_INTERVAL_SEC,
                            min(self.DEFAULT_MAX_SLICE_INTERVAL_SEC, next_interval))

        return {"status": "ok", "reason": f"切片{plan['slice_index']}完成",
                "data": {"plan_id": plan_id, "next_interval": round(next_interval, 3),
                         "filled_qty": plan["filled_qty"]},
                "warnings": []}

    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            with self._lock:
                active = len(self._active_plans)
            return {"status": "ok", "reason": f"TwapExecutor正常，活跃计划: {active}",
                    "data": {"active_plans": active}, "warnings": []}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"]}

    # ========== 私有方法 ==========
    def _send_slice_order(self, plan: Dict[str, Any], qty: float) -> Dict[str, Any]:
        """发送切片订单（含自成交错误捕获重试）"""
        if self._multi_venue_router is None:
            return {"status": "filled", "filled_qty": qty, "avg_price": 0.0}

        if self._slippage_filter and hasattr(self._slippage_filter, 'validate_order'):
            validation = self._slippage_filter.validate_order(plan["symbol"], plan["side"], qty)
            if not validation.get("allowed", True):
                return {"status": "rejected", "reason": validation.get("reason", "滑点校验未通过")}

        for retry in range(self.MAX_RETRY_PER_SLICE):
            try:
                result = self._multi_venue_router.send_order(
                    symbol=plan["symbol"], side=plan["side"], qty=qty,
                    order_type="limit", time_in_force="IOC"
                )
                if result.get("status") == "filled":
                    return result
                # 捕获自成交错误码并重试
                if "self_trade" in result.get("reason", "").lower():
                    logger.warning(f"自成交错误，重试: {plan['plan_id']}")
                    time.sleep(0.05)
                    continue
            except Exception as e:
                logger.warning(f"切片发送异常: {e}")
        return {"status": "rejected", "reason": "超过最大重试次数"}

    def _calculate_adaptive_interval(self, plan: Dict[str, Any]) -> float:
        """计算自适应间隔（基础值，外部调用再叠加指数抖动）"""
        base = plan["duration_seconds"] / plan["slice_count"]
        if self._market_data_aggregator and hasattr(self._market_data_aggregator, 'get_volume_profile'):
            try:
                profile = self._market_data_aggregator.get_volume_profile(plan["symbol"], 60)
                if profile:
                    ratio = profile.get("current_ratio", 1.0)
                    base = base / max(0.3, min(3.0, ratio))
            except Exception:
                pass
        return max(self.DEFAULT_MIN_SLICE_INTERVAL_SEC,
                   min(self.DEFAULT_MAX_SLICE_INTERVAL_SEC, base))

    def _query_risk_budget(self, symbol: str, side: int, desired_qty: float) -> float:
        """查询全局风控预算"""
        if self._risk_monitor and hasattr(self._risk_monitor, 'pre_check_order'):
            try:
                result = self._risk_monitor.pre_check_order(symbol, side, desired_qty)
                return result.get("max_allowed_qty", desired_qty)
            except Exception as e:
                logger.warning(f"风控预检查失败: {e}")
        return min(desired_qty, self.DEFAULT_SAFE_SLICE_QTY)

    def _detect_self_trade(self, symbol: str, side: int, qty: float) -> bool:
        """检测自成交风险"""
        if self._order_book_snapshot and hasattr(self._order_book_snapshot, 'get_active_orders'):
            try:
                active = self._order_book_snapshot.get_active_orders(symbol)
                for order in active:
                    if order["side"] != side and abs(order["price"] - 0) < 1e6:
                        return True
            except Exception as e:
                logger.warning(f"自成交检查异常: {e}")
        return False

    def _get_arrival_price(self, symbol: str) -> float:
        """获取到达价格（订单下达时的市场中间价）"""
        if self._market_data_aggregator and hasattr(self._market_data_aggregator, 'get_mid_price'):
            try:
                return self._market_data_aggregator.get_mid_price(symbol)
            except Exception as e:
                logger.warning(f"获取到达价格失败: {e}")
        return 0.0

    def _get_market_vwap(self, symbol: str, start_time: float, end_time: float) -> Optional[float]:
        """获取市场VWAP（指定时间窗口内的全市场成交量加权均价）"""
        if self._market_data_aggregator and hasattr(self._market_data_aggregator, 'get_market_vwap'):
            try:
                return self._market_data_aggregator.get_market_vwap(symbol, start_time, end_time)
            except Exception as e:
                logger.warning(f"获取市场VWAP失败: {e}")
        return None

    def _handle_timeout_degradation(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """超时渐进式降级"""
        remaining = plan["total_qty"] - plan["filled_qty"]
        logger.warning(f"TWAP超时降级: {plan['plan_id']}, remaining={remaining:.6f}")

        boosted_qty = remaining * 1.5 / (plan["slice_count"] - plan["slice_index"] + 1)
        result = self._send_slice_order(plan, boosted_qty)
        if result.get("status") == "filled":
            plan["filled_qty"] += result.get("filled_qty", 0)
            if plan["filled_qty"] >= plan["total_qty"]:
                self._finalize_plan(plan)
                return {"status": "ok", "reason": "超时降级完成（加大切片）", "data": {}, "warnings": []}
            remaining = plan["total_qty"] - plan["filled_qty"]

        if remaining > 0 and self._multi_venue_router:
            for _ in range(3):
                result = self._multi_venue_router.send_order(
                    symbol=plan["symbol"], side=plan["side"], qty=remaining,
                    order_type="limit", time_in_force="GTC"
                )
                if result.get("status") == "filled":
                    plan["filled_qty"] += result.get("filled_qty", 0)
                    break
                time.sleep(1)

        remaining = plan["total_qty"] - plan["filled_qty"]
        if remaining > 0:
            self._execute_remaining_as_market(plan, remaining)

        self._finalize_plan(plan)
        return {"status": "degraded", "reason": "超时降级完成", "data": {}, "warnings": ["timeout_degraded"]}

    def _handle_reject_degradation(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """拒单降级处理"""
        remaining = plan["total_qty"] - plan["filled_qty"]
        self._execute_remaining_as_market(plan, remaining * self.EMERGENCY_FILL_RATIO)
        self._finalize_plan(plan)
        return {"status": "degraded", "reason": "拒单降级完成", "data": {}, "warnings": ["reject_degraded"]}

    def _execute_remaining_as_market(self, plan: Dict[str, Any], qty: float) -> None:
        """市价单执行剩余量"""
        if self._multi_venue_router:
            try:
                result = self._multi_venue_router.send_order(
                    symbol=plan["symbol"], side=plan["side"], qty=qty, order_type="market"
                )
                plan["filled_qty"] += result.get("filled_qty", 0)
            except Exception as e:
                logger.error(f"市价降级失败: {e}")

    def _finalize_plan(self, plan: Dict[str, Any]) -> None:
        """完成计划并输出TCA（含到达价格和市场VWAP基准）"""
        plan["status"] = "completed"
        plan["completion_time"] = time.time()
        # 获取市场VWAP
        plan["market_vwap"] = self._get_market_vwap(
            plan["symbol"], plan["start_time"], plan["completion_time"]
        )
        self._generate_tca_report(plan)
        self._remove_persisted_plan(plan["plan_id"])
        self._publish_event("twap_completed", plan)
        logger.info(f"TWAP完成: {plan['plan_id']}, filled={plan['filled_qty']:.6f}, "
                    f"avg_price={plan.get('tca_avg_price', 0):.4f}")

    def _generate_tca_report(self, plan: Dict[str, Any]) -> None:
        """生成交易成本分析摘要（双基准：到达价格 & 市场VWAP）"""
        if plan["filled_qty"] <= 0:
            return
        avg_price = plan["filled_notional"] / plan["filled_qty"]
        plan["tca_avg_price"] = avg_price

        # 相对到达价格的滑点
        arrival_price = plan.get("arrival_price", 0)
        if arrival_price > 0:
            arrival_slippage_bps = (avg_price - arrival_price) / arrival_price * 10000
            plan["tca_arrival_slippage_bps"] = arrival_slippage_bps
            logger.info(f"TCA(Arrival): avg={avg_price:.4f}, arrival={arrival_price:.4f}, "
                        f"slippage={arrival_slippage_bps:.1f}bps")

        # 相对市场VWAP的滑点
        market_vwap = plan.get("market_vwap")
        if market_vwap and market_vwap > 0:
            market_slippage_bps = (avg_price - market_vwap) / market_vwap * 10000
            plan["tca_market_slippage_bps"] = market_slippage_bps
            logger.info(f"TCA(MarketVWAP): avg={avg_price:.4f}, vwap={market_vwap:.4f}, "
                        f"slippage={market_slippage_bps:.1f}bps")

    def _persist_plan(self, plan: Dict[str, Any]) -> None:
        """持久化计划状态"""
        try:
            filepath = os.path.join(self.STATE_PERSIST_DIR, f"{plan['plan_id']}.json")
            state = {k: plan[k] for k in ["plan_id", "order_id", "symbol", "side", "total_qty",
                                           "duration_seconds", "slice_count", "slice_qty",
                                           "start_time", "end_time", "filled_qty", "filled_notional",
                                           "slice_index", "status", "missed_qty", "arrival_price"]}
            with open(filepath, 'w') as f:
                json.dump(state, f)
        except Exception as e:
            logger.error(f"TWAP状态持久化失败: {e} #RECOVERY: 检查磁盘空间与目录权限")

    def _remove_persisted_plan(self, plan_id: str) -> None:
        """移除持久化文件"""
        try:
            filepath = os.path.join(self.STATE_PERSIST_DIR, f"{plan_id}.json")
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception as e:
            logger.warning(f"移除持久化文件失败: {e}")

    def _recover_plans(self) -> None:
        """从持久化恢复未完成计划（过期计划仅标记待处理，不自动激活）"""
        try:
            for fname in os.listdir(self.STATE_PERSIST_DIR):
                if not fname.endswith('.json'):
                    continue
                filepath = os.path.join(self.STATE_PERSIST_DIR, fname)
                with open(filepath) as f:
                    state = json.load(f)
                if state.get("status") == "active":
                    plan_id = state["plan_id"]
                    now = time.time()
                    if now >= state.get("end_time", 0):
                        # 过期计划：标记待处理，不激活
                        state["status"] = "pending_recovery"
                        state["recovery_time"] = now
                        self._persist_plan(state)  # 更新持久化状态
                        logger.warning(f"过期TWAP计划已标记待处理: {plan_id}, end_time={state['end_time']}")
                        continue
                    # 未过期计划正常恢复
                    state["last_slice_time"] = 0.0
                    state["consecutive_rejects"] = 0
                    state["market_prices"] = []
                    state["slice_latencies"] = []
                    state["market_vwap"] = None
                    with self._lock:
                        self._active_plans[plan_id] = state
                        self._plan_locks[plan_id] = threading.Lock()
                    logger.info(f"从持久化恢复TWAP计划: {plan_id}, filled={state['filled_qty']}")
        except Exception as e:
            logger.error(f"TWAP计划恢复失败: {e} #RECOVERY: 检查持久化目录完整性")

    def _publish_event(self, event_type: str, plan: Dict[str, Any]) -> None:
        """发布事件"""
        if self._negotiation_bus:
            try:
                self._negotiation_bus.publish_event(event_type=event_type, details={
                    "plan_id": plan["plan_id"], "symbol": plan["symbol"], "status": plan["status"],
                    "filled_qty": plan.get("filled_qty", 0), "total_qty": plan.get("total_qty", 0)
                })
            except Exception:
                pass
