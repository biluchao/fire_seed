"""
火种系统 · 多通道并行下单路由器 (MultiVenueRouter)

核心职责：
1. 接收标准化订单请求，同时向多个交易所并行发送订单，通过竞速机制选择最优成交
2. 监控各交易所成交状态，基于实际执行质量（滑点、完成率）评估最优执行场所，自动取消其余订单

外部依赖（真实模块接口）：
- core.utils.api_client.rest_adapter.RestAdapter : 各交易所的 REST 接口封装，提供 place_order、get_order_status、cancel_order 方法
- core.perception.tactile_cortex.TactileCortex : 获取当前市场微观结构（流动性评级、预估滑点），用于预筛选交易所
- core.behavioral_logger.BehavioralLogger : 记录路由决策与执行结果，支持事后审计
- core.negotiation_bus.NegotiationBus : 当所有交易所均不可用时，发送紧急降级事件

接口契约：
- inject_adapters(adapters: Dict[str, RestAdapter]) -> None : 注入各交易所的 API 适配器，键为交易所标识（如 "binance"）
- route_order(order_request: Dict[str, Any]) -> Dict[str, Any] : 执行多通道下单与最优选择，返回最终成交结果
- health_check() -> Dict[str, Any] : 模块自检，验证至少一个适配器可用
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当注入的适配器数量为 0 时，route_order 直接返回错误，避免无效操作
- 当部分交易所下单失败时，仅记录警告，继续等待其他交易所的成交结果
- 当所有交易所均返回失败时，模块进入降级状态，通过 NegotiationBus 发送告警，并尝试使用第一个成功注入的适配器进行单通道下单（如果存在）
- 取消非最优订单后，必须验证订单已进入终态，否则触发孤儿订单紧急告警
- 所有网络异常或 API 错误均被捕获，绝不向上层抛出未处理异常

资源管理：
- 线程池在模块生命周期内复用，避免重复创建开销
- 交易所适配器由外部注入，模块本身不管理其连接
- 健康度缓存使用独立的读写锁，减少锁竞争
"""

import copy
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from typing import Dict, Any, List, Optional, Tuple
import numpy as np

logger = logging.getLogger(__name__)


class MultiVenueRouter:
    """多通道并行下单路由器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_TIMEOUT_SEC = 2.0              # 等待所有交易所成交的超时时间，秒，取值范围 [0.5, 5.0]
    DEFAULT_MIN_VENUE_COUNT = 1            # 最低可用交易所数量，低于此数降级，无量纲
    SLIPPAGE_TOLERANCE_BPS = 5.0           # 可接受的滑点容忍度（基点），超过则放弃该交易所，单位 bps
    PREFERRED_FILL_RATIO = 0.95            # 理想成交完成率，低于此值则扣分，无量纲 [0.5, 1.0]
    VENUE_HEALTH_CACHE_TTL = 30.0          # 交易所健康状态缓存有效期，秒
    ORPHAN_ORDER_TIMEOUT_MS = 500          # 验证取消订单的超时时间，毫秒
    VENUE_PROBE_INTERVAL_SEC = 300         # 低健康度交易所恢复探测间隔，秒
    VENUE_PROBE_MIN_RATE = 0.5             # 触发探测的最低健康度阈值
    EXECUTOR_POOL_SIZE = 8                 # 线程池核心大小

    def __init__(self):
        # 交易所适配器字典 { "binance": RestAdapter, "okx": RestAdapter, ... }
        self._adapters: Dict[str, Any] = {}
        # 交易所健康状态缓存 { venue: {"success_rate": float, "total": int, "last_check": float, "last_fail_time": float} }
        self._venue_health: Dict[str, Dict[str, float]] = {}

        # 外部依赖注入
        self._tactile_cortex = None
        self._behavioral_logger = None
        self._negotiation_bus = None

        # 线程池（模块生命周期内复用）
        self._executor = ThreadPoolExecutor(max_workers=self.EXECUTOR_POOL_SIZE)

        # 适配器锁（保护 _adapters 字典）
        self._adapter_lock = threading.Lock()
        # 健康度独立锁，减少与适配器操作的竞争
        self._health_lock = threading.Lock()

        logger.info("MultiVenueRouter 初始化完成，等待注入交易所适配器")

    # ========== 依赖注入 ==========
    def inject_adapters(self, adapters: Dict[str, Any]) -> None:
        """
        注入交易所 API 适配器

        Args:
            adapters: 交易所标识 -> RestAdapter 实例的字典，如 {"binance": adapter_obj}
        """
        with self._adapter_lock:
            if not adapters:
                logger.warning("注入的交易所适配器为空")
                return
            self._adapters = adapters.copy()
            logger.info(f"已注入 {len(self._adapters)} 个交易所适配器: {list(self._adapters.keys())}")

    def inject_dependencies(
        self,
        tactile_cortex: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
    ) -> None:
        """注入可选的外部依赖模块"""
        if tactile_cortex is not None:
            self._tactile_cortex = tactile_cortex
            logger.info("TactileCortex 注入成功")
        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        if negotiation_bus is not None:
            self._negotiation_bus = negotiation_bus
            logger.info("NegotiationBus 注入成功")

    # ========== 公共接口 ==========
    def route_order(self, order_request: Dict[str, Any]) -> Dict[str, Any]:
        """
        多通道并行下单，选择最优成交

        Args:
            order_request: 标准化订单请求字典，必须包含:
                symbol (str): 交易对，如 "BTCUSDT"
                side (str): "buy" 或 "sell"
                quantity (float): 数量
                order_type (str): "limit" 或 "market"，默认 "limit"
                price (float, 仅限价单): 挂单价格

        Returns:
            标准响应字典，data 中包含最终成交结果、使用的交易所、滑点等信息
        """
        # 参数校验
        required_fields = ["symbol", "side", "quantity"]
        missing = [f for f in required_fields if f not in order_request]
        if missing:
            return {
                "status": "error",
                "reason": f"订单请求缺少必要字段: {missing}",
                "data": {},
                "warnings": [f"missing_fields: {missing}"],
            }

        # 深拷贝，防止外部修改污染并行下单，并统一标准化限价单精度
        safe_request = copy.deepcopy(order_request)
        safe_request = self._normalize_order_precision(safe_request)

        symbol = safe_request["symbol"]
        side = safe_request["side"]

        # 根据当前流动性预筛选交易所
        venues = self._get_active_venues(symbol)
        if len(venues) < self.DEFAULT_MIN_VENUE_COUNT:
            reason = f"可用交易所数量不足: {len(venues)} < {self.DEFAULT_MIN_VENUE_COUNT}"
            logger.error(f"{reason} #RECOVERY: 检查交易所API连通性、网络或配置")
            self._notify_all_venues_dead(reason)
            return self._fallback_single_venue(safe_request)

        logger.info(
            f"开始多通道下单: symbol={symbol}, side={side}, qty={safe_request.get('quantity')}, "
            f"type={safe_request.get('order_type', 'limit')}, venues={venues}"
        )

        # 在下单前，为每个交易所获取当前 BBO 快照，用于精确计算执行滑点
        bbo_snapshots = self._capture_bbo_snapshots(venues, symbol)

        # 并行下单
        place_results = self._parallel_place_order(safe_request, venues, bbo_snapshots)
        # 等待成交结果并选择最优
        final_result = self._wait_and_select_best(safe_request, place_results, venues, bbo_snapshots)

        # 记录行为日志
        self._log_routing_result(final_result)

        return {
            "status": "ok" if final_result.get("success") else "error",
            "reason": final_result.get("reason", "多通道下单完成"),
            "data": final_result,
            "warnings": final_result.get("warnings", []),
        }

    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            with self._adapter_lock:
                venue_count = len(self._adapters)
            # 快速连通性测试
            available = 0
            for name, adapter in self._adapters.items():
                try:
                    if hasattr(adapter, 'get_server_time'):
                        _ = adapter.get_server_time()
                        available += 1
                except Exception:
                    logger.warning(f"交易所 {name} 连通性测试失败")

            return {
                "status": "ok" if available > 0 else "degraded",
                "reason": f"MultiVenueRouter 正常，已注入 {venue_count} 个适配器，当前可用 {available} 个",
                "data": {
                    "total_venues": venue_count,
                    "available_venues": available,
                    "dependencies": {
                        "tactile_cortex": self._tactile_cortex is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                    },
                },
                "warnings": [] if available > 0 else ["no_available_venues"],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查模块初始化状态")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _normalize_order_precision(self, order: Dict) -> Dict:
        """根据交易所精度标准化订单价格和数量，统一在此处完成"""
        # 具体实现依赖于每个交易所的规格，此处为基础示例
        return order

    def _capture_bbo_snapshots(self, venues: List[str], symbol: str) -> Dict[str, Dict]:
        """获取各交易所的最佳买卖价快照"""
        bbo = {}
        for venue in venues:
            adapter = self._adapters.get(venue)
            if adapter and hasattr(adapter, 'get_bbo'):
                try:
                    bbo[venue] = adapter.get_bbo(symbol)
                except Exception as e:
                    logger.warning(f"获取 {venue} BBO 失败: {e}")
                    bbo[venue] = {"bid": None, "ask": None}
            else:
                bbo[venue] = {"bid": None, "ask": None}
        return bbo

    def _get_active_venues(self, symbol: str) -> List[str]:
        """获取当前活跃且健康的交易所列表，综合考虑流动性预筛选与健康度探测恢复"""
        with self._adapter_lock:
            all_venues = list(self._adapters.keys())
        if not all_venues:
            return []

        active = all_venues.copy()
        if self._tactile_cortex is not None:
            try:
                if hasattr(self._tactile_cortex, 'get_liquidity_level'):
                    liquidity_level = self._tactile_cortex.get_liquidity_level(symbol)
                    if liquidity_level < 3:
                        active = self._filter_by_health(active)
            except Exception as e:
                logger.warning(f"流动性评级获取失败，使用全部交易所: {e}")

        # 对低健康度交易所进行探测恢复
        active = self._probe_stale_venues(active)
        return active

    def _filter_by_health(self, venues: List[str]) -> List[str]:
        """根据近期成功率过滤交易所"""
        filtered = []
        now = time.time()
        with self._health_lock:
            for v in venues:
                health = self._venue_health.get(v, {})
                last_check = health.get("last_check", 0)
                if now - last_check > self.VENUE_HEALTH_CACHE_TTL:
                    filtered.append(v)
                else:
                    success_rate = health.get("success_rate", 1.0)
                    if success_rate > self.VENUE_PROBE_MIN_RATE:
                        filtered.append(v)
                    else:
                        logger.info(f"排除低健康度交易所: {v} (成功率={success_rate:.2f})")
        return filtered

    def _probe_stale_venues(self, venues: List[str]) -> List[str]:
        """对长时间未检测的低健康度交易所进行探测，若恢复则重新加入"""
        now = time.time()
        with self._health_lock:
            for v in venues:
                health = self._venue_health.get(v, {})
                last_fail = health.get("last_fail_time", 0)
                if (now - last_fail > self.VENUE_PROBE_INTERVAL_SEC and
                        health.get("success_rate", 1.0) < self.VENUE_PROBE_MIN_RATE):
                    if self._probe_venue(v):
                        self._venue_health[v] = {
                            "success_rate": 0.6,
                            "total": 1,
                            "last_check": now,
                            "last_fail_time": last_fail
                        }
                        logger.info(f"交易所 {v} 探测恢复，重新加入路由")
        return venues  # 探测结果已体现在健康度缓存中，下次过滤时生效

    def _probe_venue(self, venue: str) -> bool:
        """探测交易所是否恢复（发送无风险请求，如查询服务器时间）"""
        adapter = self._adapters.get(venue)
        if not adapter:
            return False
        try:
            if hasattr(adapter, 'get_server_time'):
                _ = adapter.get_server_time()
                return True
        except Exception:
            pass
        return False

    def _parallel_place_order(
        self, order_request: Dict, venues: List[str], bbo_snapshots: Dict
    ) -> Dict[str, Any]:
        """使用线程池并行向多个交易所下单，返回下单结果字典"""
        results = {}
        futures = {}
        for venue in venues:
            future = self._executor.submit(
                self._place_order_safe, venue, order_request, bbo_snapshots.get(venue, {})
            )
            futures[future] = venue

        for future in as_completed(futures):
            venue = futures[future]
            try:
                result = future.result()
                results[venue] = result
            except Exception as e:
                logger.warning(f"交易所 {venue} 下单异常: {e}")
                results[venue] = {"success": False, "error": str(e)}
        return results

    def _place_order_safe(
        self, venue: str, order_request: Dict, bbo: Dict
    ) -> Dict[str, Any]:
        """安全下单，捕获所有异常，并附带 BBO 参考价格"""
        adapter = self._adapters.get(venue)
        if not adapter:
            return {"success": False, "error": "适配器不存在"}
        try:
            result = adapter.place_order(
                symbol=order_request["symbol"],
                side=order_request["side"],
                quantity=order_request["quantity"],
                order_type=order_request.get("order_type", "limit"),
                price=order_request.get("price"),
            )
            # 记录下单时的 BBO 用于精确滑点计算
            if isinstance(result, dict):
                side = order_request.get("side")
                ref_price = bbo.get("ask") if side == "buy" else bbo.get("bid")
                result["reference_price"] = ref_price
            return result
        except Exception as e:
            logger.error(f"交易所 {venue} 下单失败: {e}")
            return {"success": False, "error": str(e)}

    def _wait_and_select_best(
        self,
        order_request: Dict,
        place_results: Dict[str, Any],
        venues: List[str],
        bbo_snapshots: Dict,
    ) -> Dict[str, Any]:
        """
        使用事件驱动机制等待成交结果，选择最优成交
        """
        # 创建事件，当任何一个订单达到理想成交状态时触发
        fast_fill_event = threading.Event()
        final_states: Dict[str, Any] = {}
        state_lock = threading.Lock()

        # 启动状态监控线程
        monitor_futures = []
        for venue in venues:
            future = self._executor.submit(
                self._monitor_fill,
                venue,
                place_results.get(venue),
                fast_fill_event,
                final_states,
                state_lock,
                order_request,
                bbo_snapshots.get(venue, {}),
            )
            monitor_futures.append(future)

        # 等待快速成交信号或超时
        fast_fill_event.wait(timeout=self.DEFAULT_TIMEOUT_SEC)

        # 读取最终状态
        with state_lock:
            states_copy = final_states.copy()

        # 取消监控线程（让它们自行结束）
        best_venue = self._evaluate_best(states_copy)
        if best_venue:
            best_result = states_copy[best_venue]
            # 取消其他交易所的未成交订单，并验证取消
            self._cancel_remaining_orders(venues, best_venue, place_results)
            self._update_venue_health(best_venue, True)
            return {
                "success": True,
                "venue": best_venue,
                "order_id": best_result.get("order_id"),
                "fill_quantity": best_result.get("filled_quantity", 0),
                "average_price": best_result.get("average_price"),
                "slippage_bps": best_result.get("slippage_bps", 0),
                "reason": f"选择 {best_venue} 作为最优成交，滑点 {best_result.get('slippage_bps', 0):.1f} bps",
                "warnings": best_result.get("warnings", []),
            }
        else:
            for venue in venues:
                self._update_venue_health(venue, False)
            logger.error("所有交易所均未能成交 #RECOVERY: 检查市场流动性、订单参数或API状态")
            return {
                "success": False,
                "venue": None,
                "reason": "所有交易所均未能成交",
                "warnings": ["all_venues_failed"],
            }

    def _monitor_fill(
        self,
        venue: str,
        place_result: Optional[Dict],
        fast_event: threading.Event,
        final_states: Dict,
        lock: threading.Lock,
        order_request: Dict,
        bbo: Dict,
    ) -> None:
        """监控单个交易所的成交状态，当接近理想成交时触发快速信号"""
        deadline = time.time() + self.DEFAULT_TIMEOUT_SEC
        while time.time() < deadline:
            status = self._get_fill_status(venue, place_result, bbo, order_request)
            with lock:
                final_states[venue] = status
            # 若成交完成率高且滑点低，触发快速信号
            if (status.get("fill_ratio", 0) >= self.PREFERRED_FILL_RATIO and
                    status.get("slippage_bps", 0) < self.SLIPPAGE_TOLERANCE_BPS):
                fast_event.set()
                break
            # 间隔检查
            time.sleep(0.05)
        # 最终状态
        final_status = self._get_fill_status(venue, place_result, bbo, order_request)
        with lock:
            final_states[venue] = final_status
        # 如果还未触发，再次检查
        if (final_status.get("fill_ratio", 0) >= self.PREFERRED_FILL_RATIO and
                final_status.get("slippage_bps", 0) < self.SLIPPAGE_TOLERANCE_BPS):
            fast_event.set()

    def _get_fill_status(
        self, venue: str, place_result: Optional[Dict], bbo: Dict, order_request: Dict
    ) -> Dict[str, Any]:
        """查询订单成交状态，基于 BBO 计算精确滑点"""
        if not place_result or not place_result.get("success"):
            return {"venue": venue, "fill_ratio": 0, "success": False}
        order_id = place_result.get("order_id")
        if not order_id:
            return {"venue": venue, "fill_ratio": 0, "success": False}

        adapter = self._adapters.get(venue)
        if not adapter:
            return {"venue": venue, "fill_ratio": 0, "success": False}
        try:
            status = adapter.get_order_status(order_id)
            if not status:
                return {"venue": venue, "fill_ratio": 0, "success": False}
            filled_qty = status.get("filled_quantity", 0)
            total_qty = place_result.get("quantity", 1)
            fill_ratio = min(filled_qty / total_qty if total_qty else 0, 1.0)
            avg_price = status.get("average_price")
            # 使用下单时的 BBO 作为基准计算滑点
            side = order_request.get("side")
            ref_price = bbo.get("ask") if side == "buy" else bbo.get("bid")
            slippage_bps = 0.0
            if avg_price and ref_price:
                slippage_bps = abs(avg_price - ref_price) / ref_price * 10000
            return {
                "venue": venue,
                "order_id": order_id,
                "fill_ratio": fill_ratio,
                "filled_quantity": filled_qty,
                "average_price": avg_price,
                "slippage_bps": slippage_bps,
                "success": True,
            }
        except Exception as e:
            logger.warning(f"查询 {venue} 订单状态失败: {e}")
            return {"venue": venue, "fill_ratio": 0, "success": False, "error": str(e)}

    def _evaluate_best(self, final_states: Dict[str, Dict]) -> Optional[str]:
        """根据成交完成率、滑点和历史成功率综合评分，选择最优交易所"""
        best_score = -1.0
        best_venue = None
        for venue, state in final_states.items():
            if not state.get("success") or state.get("fill_ratio", 0) < 0.5:
                continue
            fill_score = state["fill_ratio"] * 100
            slippage_penalty = state.get("slippage_bps", 0) * 2
            health_penalty = 0.0
            with self._health_lock:
                health = self._venue_health.get(venue, {})
                if health:
                    health_penalty = (1 - health.get("success_rate", 1.0)) * 50
            total = fill_score - slippage_penalty - health_penalty
            if total > best_score:
                best_score = total
                best_venue = venue
        return best_venue

    def _cancel_remaining_orders(
        self, venues: List[str], best_venue: str, place_results: Dict[str, Any]
    ) -> None:
        """取消除最优交易所外的其他未成交订单，并验证取消状态"""
        for venue in venues:
            if venue == best_venue:
                continue
            result = place_results.get(venue)
            if not result or not result.get("success"):
                continue
            order_id = result.get("order_id")
            if not order_id:
                continue
            adapter = self._adapters.get(venue)
            if not adapter:
                continue
            try:
                adapter.cancel_order(order_id)
                logger.info(f"已发送 {venue} 的取消指令: {order_id}")
                # 验证取消状态
                if not self._verify_cancellation(venue, order_id):
                    self._trigger_orphan_order_alert(venue, order_id)
            except Exception as e:
                logger.error(f"取消 {venue} 订单 {order_id} 异常: {e}")
                self._trigger_orphan_order_alert(venue, order_id)

    def _verify_cancellation(self, venue: str, order_id: str, timeout_ms: int = None) -> bool:
        """验证订单是否已被成功取消"""
        if timeout_ms is None:
            timeout_ms = self.ORPHAN_ORDER_TIMEOUT_MS
        adapter = self._adapters.get(venue)
        if not adapter:
            return False
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            try:
                status = adapter.get_order_status(order_id)
                if status and status.get("state") in ("CANCELED", "CLOSED"):
                    logger.info(f"确认 {venue} 订单 {order_id} 已取消")
                    return True
            except Exception as e:
                logger.warning(f"查询取消状态异常: {e}")
            time.sleep(0.05)
        logger.error(
            f"CRITICAL: 无法确认 {venue} 订单 {order_id} 已取消，可能存在孤儿订单！"
            f"#RECOVERY: 立即调用交易所批量撤单接口，或手动核实持仓"
        )
        return False

    def _trigger_orphan_order_alert(self, venue: str, order_id: str) -> None:
        """触发孤儿订单紧急告警"""
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="execution",
                    level="critical",
                    message=f"可能存在的孤儿订单: {venue} {order_id}",
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.error(f"无法发送孤儿订单告警: {e}")

    def _update_venue_health(self, venue: str, success: bool) -> None:
        """更新交易所健康度缓存"""
        with self._health_lock:
            if venue not in self._venue_health:
                self._venue_health[venue] = {
                    "success_rate": 1.0,
                    "total": 0,
                    "last_check": time.time(),
                    "last_fail_time": 0,
                }
            health = self._venue_health[venue]
            total = health.get("total", 0)
            success_rate = health.get("success_rate", 1.0)
            new_total = total + 1
            new_rate = (success_rate * total + (1.0 if success else 0.0)) / new_total
            self._venue_health[venue] = {
                "success_rate": new_rate,
                "total": new_total,
                "last_check": time.time(),
                "last_fail_time": time.time() if not success else health.get("last_fail_time", 0),
            }

    def _fallback_single_venue(self, order_request: Dict) -> Dict[str, Any]:
        """降级：使用第一个可用交易所单通道下单"""
        with self._adapter_lock:
            venues = list(self._adapters.keys())
        if not venues:
            return {
                "status": "error",
                "reason": "无可用交易所，下单失败",
                "data": {},
                "warnings": ["no_available_venues"],
            }
        fallback_venue = venues[0]
        logger.warning(f"多通道不可用，降级为单通道下单: {fallback_venue}")
        result = self._place_order_safe(fallback_venue, order_request, {})
        if result.get("success"):
            return {
                "status": "ok",
                "reason": f"降级单通道成交于 {fallback_venue}",
                "data": {
                    "venue": fallback_venue,
                    "order_id": result.get("order_id"),
                    "fill_quantity": result.get("filled_quantity", order_request.get("quantity")),
                    "average_price": result.get("average_price"),
                },
                "warnings": ["fallback_single_venue"],
            }
        else:
            return {
                "status": "error",
                "reason": f"降级单通道下单失败: {result.get('error')}",
                "data": {},
                "warnings": ["fallback_failed"],
            }

    def _notify_all_venues_dead(self, reason: str) -> None:
        """通过协商总线发送所有交易所不可用的紧急告警"""
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="execution",
                    level="critical",
                    message=f"所有交易所不可用: {reason}",
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.error(f"无法发送紧急告警: {e}")

    def _log_routing_result(self, result: Dict) -> None:
        """记录路由结果到行为日志"""
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="multi_venue_routing",
                    details={
                        "venue": result.get("venue"),
                        "success": result.get("success"),
                        "fill_quantity": result.get("fill_quantity"),
                        "slippage_bps": result.get("slippage_bps"),
                    },
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

    # 资源清理（可选，由系统退出时调用）
    def shutdown(self):
        self._executor.shutdown(wait=False)
        logger.info("MultiVenueRouter 线程池已关闭")
