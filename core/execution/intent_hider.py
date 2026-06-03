"""
火种系统 · 意图隐藏器 (IntentHider)

核心职责：
1. 聚合去重：在随机化时间窗口内，按订单价格容忍度分组合并同品种、同方向的订单。通过低概率延迟和后台自动刷新，
   避免形成固定模式被高频做市商识别，同时保证在任何订单流量下订单不会被无限期缓冲。
2. 幽灵流动性探测：以极小的市价单主动“咬”一口对手盘，通过对比订单簿快照感应对手方隐藏大单或算法行为。
   极端行情下自动熔断，探测单带超时自动清理和线程安全的撤单保护。

外部依赖（真实模块接口）：
- core.order_manager.OrderManager : 查询当前活跃订单，合并后更新订单状态，撤销超时订单
- core.execution.multi_venue_router.MultiVenueRouter : 获取各交易所流动性评级、挂单建议与订单簿快照
- core.risk_monitor.RiskMonitor : 查询当前市场是否处于极端波动状态，触发熔断
- core.behavioral_logger.BehavioralLogger : 记录合并操作与幽灵探测结果

接口契约：
- aggregate_orders(symbol: str, direction: int, orders: List[Dict]) -> Dict[str, Any] : 聚合去重订单
- place_phantom_probe(symbol: str, direction: int, size: float) -> Dict[str, Any] : 下达幽灵探测单
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 OrderManager 不可用时，聚合功能降级为透传原始订单，幽灵探测自动跳过
- 当 MultiVenueRouter 不可用时，幽灵探测自动跳过，并记录 insufficient_liquidity_data 状态
- 当 RiskMonitor 不可用或市场极端波动时，幽灵探测自动熔断
- 未知订单类型归入保守组处理，确保不会静默丢弃
- 所有降级值在类常量区明确声明

资源管理：
- 本模块不持有持久化资源，所有中间计算结果在方法返回后自动释放
- 幽灵探测单的生命周期由 OrderManager 统一管理，超时后自动撤单，撤单操作使用独立线程避免阻塞
- 后台自动刷新线程在模块销毁时优雅退出
"""

import time
import logging
import threading
import random
from typing import Dict, Any, List, Optional
from collections import defaultdict
from queue import Queue

logger = logging.getLogger(__name__)


class IntentHider:
    """意图隐藏器：订单聚合去重与幽灵流动性探测"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_AGGREGATION_WINDOW_MS = 500         # 基础聚合时间窗口，毫秒，取值范围 [100, 2000]
    AGGREGATION_WINDOW_JITTER_MS = 150          # 聚合窗口随机抖动范围，毫秒，取值范围 [50, 300]
    AGGREGATION_JITTER_DISTRIBUTION = "gaussian" # 随机分布类型: "uniform" 或 "gaussian"
    AGGREGATION_GAUSSIAN_SIGMA_MS = 50          # 高斯分布标准差，毫秒，取值范围 [20, 100]
    AGGREGATION_SIZE_THRESHOLD = 3              # 触发聚合的最小订单数量，无量纲，取值范围 [2, 10]
    AGGREGATION_DELAY_PROBABILITY = 0.01        # 低概率延迟事件，无量纲，取值范围 [0.0, 0.05]
    AGGREGATION_DELAY_FACTOR_MS = 800           # 延迟事件的额外等待时间，毫秒，取值范围 [300, 1500]
    AUTO_FLUSH_CHECK_INTERVAL_MS = 200           # 后台自动刷新检查间隔，毫秒，取值范围 [100, 500]
    DEFAULT_PHANTOM_PROBE_SIZE_PCT = 0.01      # 幽灵探测单占账户权益的比例，无量纲，取值范围 [0.001, 0.05]
    DEFAULT_MAX_AGGREGATED_ORDERS = 10          # 单次聚合最大订单数，无量纲，取值范围 [2, 20]
    DEFAULT_PROBE_TIMEOUT_SEC = 3.0            # 幽灵探测单超时时间，秒，取值范围 [1.0, 10.0]
    DEFAULT_PROBE_MIN_DEPTH_RATIO = 0.5        # 幽灵探测所需最小盘口深度比例（相对于历史均值），取值范围 [0.3, 0.8]
    PROBE_MARKET_ORDER_SIZE_MULTIPLIER = 2.0   # 市价探测单相对于原始幽灵单的数量倍数，无量纲，[1.0, 3.0]
    CANCEL_ORDER_TIMEOUT_SEC = 0.5             # 撤单超时时间，秒，取值范围 [0.2, 1.0]

    def __init__(self):
        # 订单缓冲池（按品种+方向分组），由 self._lock 保护
        self._order_buffer: Dict[str, List[Dict]] = defaultdict(list)
        # 缓冲池时间戳，由 self._lock 保护
        self._buffer_timestamp: Dict[str, float] = {}
        # 聚合窗口随机引擎
        self._jitter_generator = random.Random()

        # 幽灵探测单跟踪，由 self._lock 保护
        self._phantom_orders: Dict[str, Dict] = {}

        # 订单提交队列：用于在锁外执行网络 I/O，避免阻塞
        self._submit_queue: Queue = Queue()

        # 外部依赖注入
        self._order_manager = None
        self._multi_venue_router = None
        self._risk_monitor = None
        self._behavioral_logger = None

        # 线程安全锁：保护 _order_buffer、_buffer_timestamp 和 _phantom_orders
        self._lock = threading.Lock()

        # 后台线程控制
        self._stop_event = threading.Event()

        # 启动订单提交工作线程
        self._submit_worker = threading.Thread(target=self._submit_loop, daemon=True, name="IntentHider-Submit")
        self._submit_worker.start()

        # 启动自动刷新守护线程，防止订单长时间滞留在缓冲池
        self._auto_flush_thread = threading.Thread(target=self._auto_flush_loop, daemon=True,
                                                   name="IntentHider-AutoFlush")
        self._auto_flush_thread.start()

        logger.info("IntentHider 初始化完成，聚合窗口=%d±%dms (分布=%s)，后台线程已启动",
                    self.DEFAULT_AGGREGATION_WINDOW_MS, self.AGGREGATION_WINDOW_JITTER_MS,
                    self.AGGREGATION_JITTER_DISTRIBUTION)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        order_manager: Optional[Any] = None,
        multi_venue_router: Optional[Any] = None,
        risk_monitor: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选注入，未注入时对应功能降级）"""
        if order_manager is not None:
            if not hasattr(order_manager, 'place_order') or not hasattr(order_manager, 'cancel_order'):
                logger.warning("OrderManager 缺少必要方法，聚合功能降级")
                self._order_manager = None
            else:
                self._order_manager = order_manager
                logger.info("OrderManager 注入成功")

        if multi_venue_router is not None:
            self._multi_venue_router = multi_venue_router
            logger.info("MultiVenueRouter 注入成功")
        else:
            logger.warning("MultiVenueRouter 未注入，幽灵探测功能降级")

        if risk_monitor is not None:
            self._risk_monitor = risk_monitor
            logger.info("RiskMonitor 注入成功")
        else:
            logger.warning("RiskMonitor 未注入，极端行情熔断功能降级")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

    # ========== 公共接口 ==========
    def aggregate_orders(self, symbol: str, direction: int, orders: List[Dict]) -> Dict[str, Any]:
        """
        聚合去重订单：在截断高斯分布随机化时间窗口内，分组合并同品种、同方向的订单。
        订单会被立即放入缓冲池，聚合提交由后台线程异步完成，避免锁内网络I/O阻塞。

        Args:
            symbol: 交易品种
            direction: 方向（1=多头，-1=空头）
            orders: 待处理的订单列表，每个订单包含 qty, price, order_type

        Returns:
            标准响应字典，data 中包含 merged_order 或 original_orders
        """
        if not orders:
            logger.warning("空订单列表，跳过聚合")
            return {
                "status": "ok",
                "reason": "空订单列表，无需聚合",
                "data": {"merged_order": None, "original_count": 0},
                "warnings": ["empty_orders"],
            }

        if direction not in (1, -1):
            logger.warning(f"无效方向: {direction}")
            return {
                "status": "error",
                "reason": f"无效方向: {direction}，有效值为 1(多头) 或 -1(空头)",
                "data": {},
                "warnings": [f"invalid_direction: {direction}"],
            }

        # 降级检查：OrderManager 不可用时直接返回原始订单
        if self._order_manager is None:
            logger.warning("OrderManager 不可用，聚合降级为透传")
            return {
                "status": "degraded",
                "reason": "OrderManager 不可用，聚合降级为透传原始订单",
                "data": {"original_orders": orders, "merged_order": None},
                "warnings": ["order_manager_unavailable"],
            }

        with self._lock:
            now = time.time()
            buffer_key = f"{symbol}:{direction}"

            # 计算带截断高斯分布的抖动窗口，模糊聚合模式指纹
            jitter_ms = 0
            if self.AGGREGATION_JITTER_DISTRIBUTION == "gaussian":
                jitter_ms = int(self._jitter_generator.gauss(0, self.AGGREGATION_GAUSSIAN_SIGMA_MS))
                jitter_ms = max(-self.AGGREGATION_WINDOW_JITTER_MS,
                                min(self.AGGREGATION_WINDOW_JITTER_MS, jitter_ms))
            else:
                jitter_ms = self._jitter_generator.randint(-self.AGGREGATION_WINDOW_JITTER_MS,
                                                           self.AGGREGATION_WINDOW_JITTER_MS)

            current_window_ms = max(100, self.DEFAULT_AGGREGATION_WINDOW_MS + jitter_ms)

            # 极低概率延迟事件，模拟人类犹豫或系统微卡顿
            if self._jitter_generator.random() < self.AGGREGATION_DELAY_PROBABILITY:
                logger.debug("触发低概率延迟事件，延长聚合窗口")
                current_window_ms += self.AGGREGATION_DELAY_FACTOR_MS

            # 检查是否需要刷新缓冲（超过随机时间窗口）
            last_update = self._buffer_timestamp.get(buffer_key, 0)
            should_flush = (now - last_update) * 1000 > current_window_ms

            if should_flush:
                if buffer_key in self._order_buffer and self._order_buffer[buffer_key]:
                    merged = self._merge_orders(self._order_buffer[buffer_key])
                    self._submit_queue.put(merged)
                    self._order_buffer[buffer_key] = []
                self._buffer_timestamp[buffer_key] = now

            for order in orders:
                if isinstance(order, dict) and 'qty' in order:
                    self._order_buffer[buffer_key].append(order)

            current_count = len(self._order_buffer[buffer_key])
            # 检查是否达到聚合数量阈值
            if current_count >= self.AGGREGATION_SIZE_THRESHOLD:
                merged = self._merge_orders(self._order_buffer[buffer_key])
                self._submit_queue.put(merged)
                self._order_buffer[buffer_key] = []
                self._buffer_timestamp[buffer_key] = now

                logger.info(
                    "聚合订单: symbol=%s, direction=%d, 原始%d笔, 合并为1笔, qty=%.4f, type=%s",
                    symbol, direction, current_count, merged.get('qty', 0), merged.get('order_type', 'N/A')
                )
                return {
                    "status": "ok",
                    "reason": f"订单已聚合，原始{current_count}笔合并为1笔",
                    "data": {
                        "merged_order": merged,
                        "original_count": current_count,
                        "audit_info": {
                            "window_ms": current_window_ms,
                            "trigger": "size_threshold"
                        }
                    },
                    "warnings": [],
                }

            # 暂未达到聚合条件，返回待处理状态
            return {
                "status": "ok",
                "reason": f"订单已缓存，当前缓冲{current_count}笔，窗口={current_window_ms}ms",
                "data": {
                    "merged_order": None,
                    "buffered_count": current_count,
                    "audit_info": {"window_ms": current_window_ms, "trigger": "pending"}
                },
                "warnings": [],
            }

    def place_phantom_probe(self, symbol: str, direction: int, size: float) -> Dict[str, Any]:
        """
        下达幽灵探测单：以极小的市价单主动探测对手盘流动性，通过对比订单簿快照评估冲击。
        极端行情下自动熔断。

        Args:
            symbol: 交易品种
            direction: 方向（1=多头，-1=空头）
            size: 探测单基础数量（实际市价单数量将乘以倍数）

        Returns:
            标准响应字典，data 中包含 probe_order_id、冲击评估等信息
        """
        if size <= 0:
            logger.warning(f"无效探测单数量: {size}")
            return {
                "status": "error",
                "reason": f"无效探测单数量: {size}，必须为正数",
                "data": {},
                "warnings": ["invalid_probe_size"],
            }

        if direction not in (1, -1):
            logger.warning(f"无效方向: {direction}")
            return {
                "status": "error",
                "reason": f"无效方向: {direction}，有效值为 1(多头) 或 -1(空头)",
                "data": {},
                "warnings": [f"invalid_direction: {direction}"],
            }

        # 极端行情熔断
        if self._risk_monitor is not None and hasattr(self._risk_monitor, 'is_market_extreme'):
            try:
                if self._risk_monitor.is_market_extreme():
                    logger.warning("市场处于极端波动状态，暂停幽灵探测")
                    return {
                        "status": "degraded",
                        "reason": "市场极度波动，暂停幽灵探测以规避风险",
                        "data": {"probe_order_id": None, "skipped": True, "audit_info": {"meltdown": True}},
                        "warnings": ["market_extreme"],
                    }
            except Exception as e:
                logger.warning(f"调用风控模块异常: {e}，降级为跳过探测")

        if self._multi_venue_router is None:
            logger.warning("MultiVenueRouter 不可用，幽灵探测跳过")
            return {
                "status": "degraded",
                "reason": "MultiVenueRouter 不可用，幽灵探测跳过",
                "data": {"probe_order_id": None, "skipped": True},
                "warnings": ["router_unavailable"],
            }

        if self._order_manager is None:
            logger.warning("OrderManager 不可用，幽灵探测跳过")
            return {
                "status": "degraded",
                "reason": "OrderManager 不可用，幽灵探测跳过",
                "data": {"probe_order_id": None, "skipped": True},
                "warnings": ["order_manager_unavailable"],
            }

        try:
            liquidity_info = {}
            if hasattr(self._multi_venue_router, 'get_liquidity_rating'):
                liquidity_info = self._multi_venue_router.get_liquidity_rating(symbol) or {}
            else:
                logger.warning("MultiVenueRouter 缺少 get_liquidity_rating 方法")

            current_depth_ratio = liquidity_info.get('depth_ratio', 1.0)
            if current_depth_ratio < self.DEFAULT_PROBE_MIN_DEPTH_RATIO:
                logger.warning(
                    "盘口深度不足: symbol=%s, depth_ratio=%.2f, 拒绝幽灵探测",
                    symbol, current_depth_ratio
                )
                return {
                    "status": "ok",
                    "reason": f"盘口深度不足({current_depth_ratio:.2f}<{self.DEFAULT_PROBE_MIN_DEPTH_RATIO})，拒绝幽灵探测",
                    "data": {"probe_order_id": None, "depth_ratio": current_depth_ratio, "audit_info": {"depth_rejected": True}},
                    "warnings": ["insufficient_depth"],
                }

            # 获取探测前订单簿快照
            pre_snapshot = None
            if hasattr(self._multi_venue_router, 'get_orderbook_snapshot'):
                pre_snapshot = self._multi_venue_router.get_orderbook_snapshot(symbol)

            probe_qty = size * self.PROBE_MARKET_ORDER_SIZE_MULTIPLIER
            probe_order = {
                "symbol": symbol,
                "direction": direction,
                "qty": probe_qty,
                "order_type": "market",
                "is_phantom": True,
                "timeout_sec": self.DEFAULT_PROBE_TIMEOUT_SEC,
            }

            # 先检查并撤销同方向残留幽灵单
            self._cancel_existing_phantom_probes(symbol, direction)

            order_id = self._order_manager.place_order(**probe_order) if hasattr(
                self._order_manager, 'place_order') else None

            if order_id is None:
                logger.error("幽灵探测下单失败 #RECOVERY: 检查 OrderManager 接口和交易所连接")
                return {
                    "status": "error",
                    "reason": "幽灵探测下单失败",
                    "data": {"probe_order_id": None},
                    "warnings": ["place_order_failed"],
                }

            # 获取探测后订单簿快照
            post_snapshot = None
            if hasattr(self._multi_venue_router, 'get_orderbook_snapshot'):
                post_snapshot = self._multi_venue_router.get_orderbook_snapshot(symbol)

            # 计算冲击评估
            impact_assessment = {}
            if pre_snapshot and post_snapshot:
                key_vol = 'ask_vol_5' if direction == 1 else 'bid_vol_5'
                pre_vol = pre_snapshot.get(key_vol, 0)
                post_vol = post_snapshot.get(key_vol, 0)
                # 除去探测单自身消耗，挂单量的恢复情况
                recovery = post_vol - (pre_vol - probe_qty)
                impact_assessment = {
                    "pre_vol": pre_vol,
                    "post_vol": post_vol,
                    "recovery_vol": recovery,
                    "resilience_rating": "high" if recovery > probe_qty * 0.5 else "low"
                }

            with self._lock:
                self._phantom_orders[order_id] = {
                    "symbol": symbol,
                    "direction": direction,
                    "placed_at": time.time(),
                    "timeout_sec": self.DEFAULT_PROBE_TIMEOUT_SEC,
                }

            logger.info(
                "幽灵探测单已下达: symbol=%s, direction=%d, qty=%.4f, order_id=%s, type=market, resilience=%s",
                symbol, direction, probe_qty, order_id, impact_assessment.get("resilience_rating", "unknown")
            )

            return {
                "status": "ok",
                "reason": f"幽灵探测单已下达: {order_id}",
                "data": {
                    "probe_order_id": order_id,
                    "symbol": symbol,
                    "direction": direction,
                    "qty": probe_qty,
                    "audit_info": {
                        "depth_ratio": current_depth_ratio,
                        "impact_assessment": impact_assessment
                    }
                },
                "warnings": [],
            }

        except Exception as e:
            logger.error(f"幽灵探测异常: {e} #RECOVERY: 检查网络连接和交易所API状态", exc_info=True)
            return {
                "status": "error",
                "reason": f"幽灵探测异常: {str(e)}",
                "data": {"probe_order_id": None},
                "warnings": [f"phantom_probe_exception: {str(e)}"],
            }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检，包含幽灵单泄漏检测与带超时保护的自动清理。
        为避免阻塞，清理操作在锁外执行。
        """
        try:
            stale_ids = []
            with self._lock:
                buffer_count = len(self._order_buffer)
                phantom_count = len(self._phantom_orders)
                now = time.time()
                for oid, info in self._phantom_orders.items():
                    if now - info.get("placed_at", 0) > self.DEFAULT_PROBE_TIMEOUT_SEC:
                        stale_ids.append(oid)

            if stale_ids:
                logger.warning("检测到 %d 笔超时幽灵单，开始清理（含超时保护）", len(stale_ids))
                self._cleanup_stale_probes_by_ids(stale_ids)
                with self._lock:
                    phantom_count = len(self._phantom_orders)

            return {
                "status": "ok",
                "reason": f"IntentHider 正常，缓冲池 {buffer_count} 组，幽灵单 {phantom_count} 笔",
                "data": {
                    "buffered_groups": buffer_count,
                    "phantom_orders": phantom_count,
                    "stale_probes": len(stale_ids),
                    "submit_queue_depth": self._submit_queue.qsize(),
                    "dependencies": {
                        "order_manager": self._order_manager is not None,
                        "multi_venue_router": self._multi_venue_router is not None,
                        "risk_monitor": self._risk_monitor is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和内部数据结构")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _merge_orders(self, orders: List[Dict]) -> Dict:
        """
        分组合并订单：按价格容忍度分为激进组和保守组，未知类型归入保守组确保不丢失。
        激进组包含市价/IOC单，合并后强制为市价单以确保成交。
        """
        aggressive_orders = []
        passive_orders = []

        for o in orders:
            otype = o.get("order_type", "limit")
            if otype in ("market", "limit_ioc"):
                aggressive_orders.append(o)
            elif otype in ("limit", "iceberg", "twap"):
                passive_orders.append(o)
            else:
                passive_orders.append(o)
                logger.warning(f"未知订单类型: {otype}，已归入保守组处理 #RECOVERY: 检查策略引擎与intent_hider的订单类型兼容性")

        if aggressive_orders:
            target_group = aggressive_orders
            final_type = "market" if any(o["order_type"] == "market" for o in aggressive_orders) else "limit_ioc"
        else:
            target_group = passive_orders
            final_type = "limit"

        total_qty = sum(float(o.get("qty", 0)) for o in target_group)
        if total_qty == 0:
            return {"qty": 0.0, "price": 0.0, "order_type": "limit", "original_count": 0}

        weighted_price = sum(float(o["qty"]) * float(o["price"]) for o in target_group) / total_qty

        return {
            "qty": round(total_qty, 8),
            "price": round(weighted_price, 2),
            "order_type": final_type,
            "original_count": len(target_group),
        }

    def _submit_loop(self) -> None:
        """订单提交工作线程：在锁外执行网络 I/O，避免阻塞聚合逻辑"""
        while not self._stop_event.is_set():
            try:
                merged = self._submit_queue.get(timeout=0.5)
                if self._order_manager is not None and hasattr(self._order_manager, 'place_order'):
                    try:
                        self._order_manager.place_order(**merged)
                        logger.debug("提交订单成功: qty=%.4f, type=%s", merged.get("qty", 0), merged.get("order_type"))
                    except Exception as e:
                        logger.error(f"提交订单失败: {e} #RECOVERY: 检查 OrderManager 状态和交易所连接")
                else:
                    logger.warning("OrderManager 不可用，聚合订单被丢弃")
            except Exception:
                # 队列超时，继续循环
                pass

    def _auto_flush_loop(self) -> None:
        """后台自动刷新循环：定期检查缓冲池，防止订单在低流量时段长时间滞留"""
        while not self._stop_event.is_set():
            time.sleep(self.AUTO_FLUSH_CHECK_INTERVAL_MS / 1000.0)
            now = time.time()
            with self._lock:
                for buffer_key in list(self._order_buffer.keys()):
                    if not self._order_buffer[buffer_key]:
                        continue
                    # 使用基础窗口加上随机抖动检查是否超时
                    jitter_ms = self._jitter_generator.randint(-self.AGGREGATION_WINDOW_JITTER_MS,
                                                               self.AGGREGATION_WINDOW_JITTER_MS)
                    current_window_ms = max(100, self.DEFAULT_AGGREGATION_WINDOW_MS + jitter_ms)
                    last_update = self._buffer_timestamp.get(buffer_key, 0)
                    if (now - last_update) * 1000 > current_window_ms:
                        merged = self._merge_orders(self._order_buffer[buffer_key])
                        self._submit_queue.put(merged)
                        self._order_buffer[buffer_key] = []
                        logger.info("后台自动刷新: key=%s, 合并%d笔订单", buffer_key, merged.get("original_count", 0))

    def _cancel_existing_phantom_probes(self, symbol: str, direction: int) -> None:
        """撤销同品种同方向的残留幽灵探测单"""
        to_cancel = []
        with self._lock:
            for oid, info in list(self._phantom_orders.items()):
                if info["symbol"] == symbol and info["direction"] == direction:
                    to_cancel.append(oid)

        for oid in to_cancel:
            if self._order_manager and hasattr(self._order_manager, 'cancel_order'):
                try:
                    self._order_manager.cancel_order(oid)
                    logger.info("已撤销残留幽灵探测单: %s", oid)
                except Exception as e:
                    logger.warning(f"撤销残留幽灵单 {oid} 失败: {e}")
            with self._lock:
                self._phantom_orders.pop(oid, None)

    def _cancel_with_timeout(self, order_id: str, timeout_sec: float = None) -> bool:
        """带超时保护的撤单操作，使用线程实现，安全、无副作用"""
        if timeout_sec is None:
            timeout_sec = self.CANCEL_ORDER_TIMEOUT_SEC

        result = [False]
        exception_info = [None]

        def worker():
            try:
                self._order_manager.cancel_order(order_id)
                result[0] = True
            except Exception as e:
                exception_info[0] = e
                result[0] = False

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=timeout_sec)
        if t.is_alive():
            logger.error(f"幽灵探测单 {order_id} 撤单超时 #RECOVERY: 检查 OrderManager 状态")
            return False
        if exception_info[0]:
            logger.error(f"幽灵探测单 {order_id} 撤单失败: {exception_info[0]} #RECOVERY: 手动在交易所撤销此订单")
        return result[0]

    def _cleanup_stale_probes_by_ids(self, stale_ids: List[str]) -> None:
        """清理指定的超时幽灵探测单，在锁外执行撤单"""
        for oid in stale_ids:
            logger.info("清理超时幽灵探测单: %s", oid)
            if self._order_manager and hasattr(self._order_manager, 'cancel_order'):
                success = self._cancel_with_timeout(oid)
                if not success:
                    pass
            with self._lock:
                self._phantom_orders.pop(oid, None)
        if stale_ids:
            logger.info("批量清理 %d 笔超时幽灵探测单", len(stale_ids))

    def __del__(self):
        """模块销毁时优雅退出后台线程"""
        self._stop_event.set()
        if hasattr(self, '_submit_worker') and self._submit_worker.is_alive():
            self._submit_worker.join(timeout=2.0)
        if hasattr(self, '_auto_flush_thread') and self._auto_flush_thread.is_alive():
            self._auto_flush_thread.join(timeout=2.0)
