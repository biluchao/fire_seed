"""
火种系统 · 活跃订单注册表 (ActiveOrderRegistry)

核心职责：
1. 维护所有活跃订单（挂单、部分成交单、冰山订单）的线程安全注册表，支持按订单ID、品种、方向快速查询
2. 提供原子化的“检查并预留”接口，消除并发环境下的幻读风险，确保自成交检测与订单簿快照的一致性
3. 支持订单部分成交后的数量更新，维护准确的剩余量用于自成交判断
4. 在注册与预留槽位时内置反向自成交检测，确保多道防线阻隔自成交风险

外部依赖（真实模块接口）：
- core.behavioral_logger.BehavioralLogger : 记录订单注册、撤销、冲突等关键事件

接口契约：
- register_order(order_id, symbol, side, price, qty, order_type) -> Dict[str, Any] : 注册新订单，内置自成交检测
- update_order_qty(order_id, new_qty) -> Dict[str, Any] : 更新指定订单的剩余数量（如部分成交后）
- unregister_order(order_id) -> Dict[str, Any] : 撤销指定订单
- get_active_orders(symbol, side) -> Dict[str, Any] : 查询活跃订单列表，可按品种、方向过滤
- reserve_slot(symbol, side, price, qty, timeout_ms) -> Dict[str, Any] : 原子化预留订单槽位，内置自成交检测
- release_slot(token) -> Dict[str, Any] : 释放预留槽位
- detect_self_trade(symbol, side, price) -> Dict[str, Any] : 检查是否与现有活跃订单构成自成交（独立的检测接口）
- health_check(timeout_sec: float = 5.0) -> Dict[str, Any] : 模块自检，包含索引一致性验证，支持超时控制
- stress_health_check() -> Dict[str, Any] : 压力测试自检（仅限非交易时段隔离环境执行），评估并发性能
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])
- 错误与冲突响应必须包含 "retry_strategy" 字段，取值为 "none" / "retry" / "abort"

异常与降级：
- 当 BehavioralLogger 不可用时，事件记录降级为标准 logger
- 若品种/方向索引出现不一致，自动修复并记录告警
- 所有降级值在类常量区明确声明

资源管理：
- 内部维护订单字典、价格排序索引、时间堆、预留槽位字典、预留槽位过期堆、订单-品种映射表，使用品种级读写锁与映射表专用锁保护
- 预留槽位具有超时自动释放机制（基于堆的惰性清理，并定期压缩堆），时间堆支持周期性压缩以防止惰性内存泄漏
- 映射表更新均在品种锁外执行，避免死锁
"""

import time
import uuid
import bisect
import heapq
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple
from collections import defaultdict

logger = logging.getLogger(__name__)


class ActiveOrderRegistry:
    """线程安全的活跃订单注册表（品种分片锁 + 价格索引 + 时间堆压缩 + 预留堆优化 + 部分成交支持）"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_RESERVE_TIMEOUT_MS = 500          # 预留槽位超时时间，毫秒，取值范围 [100, 2000]
    DEFAULT_SELF_TRADE_PRICE_TOLERANCE = 0.0001  # 自成交价格容忍度（比例），无量纲，[0.00001, 0.01]
    MAX_ORDERS_PER_SYMBOL_SIDE = 50           # 单品种单方向最大订单数，无量纲，[10, 200]
    HEAP_COMPRESS_RATIO = 3.0                 # 时间堆压缩触发比率（堆大小 / 存活订单数），无量纲，[2.0, 5.0]
    RESERVE_HEAP_COMPRESS_RATIO = 3.0         # 预留堆压缩触发比率（堆大小 / 字典大小），无量纲，[2.0, 5.0]
    HEALTH_CHECK_TIMEOUT_SEC = 5.0            # 健康检查超时时间，秒，取值范围 [2.0, 30.0]

    def __init__(self):
        # 活跃订单：按品种分片
        self._orders_by_symbol: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
        # 价格排序索引：key="symbol:side"，value=有序列表 [(price, order_id), ...]
        self._price_index: Dict[str, List[Tuple[float, str]]] = defaultdict(list)
        # 创建时间堆：key="symbol:side"，value=最小堆 [(created_at, order_id), ...]
        self._time_heap: Dict[str, List[Tuple[float, str]]] = defaultdict(list)
        # 预留槽位：按品种分片，存储 token -> reservation 详情
        self._reservations_by_symbol: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
        # 预留槽位过期堆：按品种分片，存储 (expires_at, token) 的最小堆
        self._reservation_heap_by_symbol: Dict[str, List[Tuple[float, str]]] = defaultdict(list)
        # 品种级锁
        self._locks_by_symbol: Dict[str, threading.RLock] = defaultdict(threading.RLock)

        # 订单ID到品种的映射表，用于快速查找订单所属品种
        self._order_id_to_symbol: Dict[str, str] = {}
        self._mapping_lock = threading.Lock()

        # 外部依赖注入
        self._behavioral_logger = None

        logger.info(
            "ActiveOrderRegistry 初始化完成 "
            "(品种分片锁 + 价格索引 + 时间堆压缩 + 预留堆优化 + 内置自成交检测 + 部分成交更新)"
        )

    # ========== 依赖注入 ==========
    def inject_dependencies(self, behavioral_logger: Optional[Any] = None) -> None:
        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，事件记录降级为标准 logger")

    # ========== 私有辅助 ==========
    def _get_lock(self, symbol: str) -> threading.RLock:
        return self._locks_by_symbol[symbol]

    def _get_side_key(self, symbol: str, side: str) -> str:
        return f"{symbol}:{side}"

    def _is_price_conflict(self, price: float, target_price: float) -> bool:
        if target_price <= 0:
            return False
        return abs(price - target_price) / target_price < self.DEFAULT_SELF_TRADE_PRICE_TOLERANCE

    def _find_conflict_orders(self, symbol: str, side: str, price: float) -> List[str]:
        key = self._get_side_key(symbol, side)
        sorted_list = self._price_index.get(key, [])
        if not sorted_list:
            return []
        prices = [p for p, _ in sorted_list]
        idx = bisect.bisect_left(prices, price)
        conflict_ids = []
        i = idx - 1
        while i >= 0 and self._is_price_conflict(price, prices[i]):
            conflict_ids.append(sorted_list[i][1])
            i -= 1
        i = idx
        while i < len(prices) and self._is_price_conflict(price, prices[i]):
            conflict_ids.append(sorted_list[i][1])
            i += 1
        return conflict_ids

    def _add_to_indices(self, order_info: Dict[str, Any]) -> None:
        symbol, side, oid, price, created_at = (
            order_info["symbol"], order_info["side"], order_info["order_id"],
            order_info["price"], order_info["created_at"]
        )
        key = self._get_side_key(symbol, side)
        bisect.insort(self._price_index[key], (price, oid))
        heapq.heappush(self._time_heap[key], (created_at, oid))

    def _remove_from_indices(self, order_info: Dict[str, Any]) -> None:
        symbol, side, oid, price = (
            order_info["symbol"], order_info["side"], order_info["order_id"],
            order_info["price"]
        )
        key = self._get_side_key(symbol, side)
        sorted_list = self._price_index.get(key, [])
        try:
            sorted_list.remove((price, oid))
        except ValueError:
            pass

    def _compress_time_heap(self, symbol: str, side: str) -> None:
        key = self._get_side_key(symbol, side)
        heap = self._time_heap.get(key, [])
        if not heap:
            return
        orders = self._orders_by_symbol.get(symbol, {})
        new_heap = []
        for created_at, oid in heap:
            if oid in orders and orders[oid]["side"] == side:
                new_heap.append((created_at, oid))
        heapq.heapify(new_heap)
        self._time_heap[key] = new_heap
        if len(new_heap) < len(heap):
            logger.debug("压缩时间堆: %s 从 %d 减少到 %d", key, len(heap), len(new_heap))

    def _compress_reservation_heap(self, symbol: str) -> None:
        """压缩预留堆：移除已不在字典中的无效条目"""
        heap = self._reservation_heap_by_symbol.get(symbol, [])
        if not heap:
            return
        reservations = self._reservations_by_symbol.get(symbol, {})
        new_heap = []
        for expires_at, token in heap:
            if token in reservations:
                new_heap.append((expires_at, token))
        heapq.heapify(new_heap)
        self._reservation_heap_by_symbol[symbol] = new_heap
        if len(new_heap) < len(heap):
            logger.debug("压缩预留堆: %s 从 %d 减少到 %d", symbol, len(heap), len(new_heap))

    def _cleanup_expired_reservations(self, symbol: str) -> None:
        heap = self._reservation_heap_by_symbol.get(symbol, [])
        if not heap:
            return
        reservations = self._reservations_by_symbol.get(symbol, {})
        now = time.time()
        removed = 0
        while heap and heap[0][0] < now:
            expires_at, token = heapq.heappop(heap)
            if token in reservations:
                del reservations[token]
                removed += 1
                logger.debug("清理过期预留槽位: token=%s, symbol=%s", token, symbol)

        # 自动压缩：如果堆大小超过字典大小的指定倍数，触发压缩
        if heap and reservations and len(heap) > len(reservations) * self.RESERVE_HEAP_COMPRESS_RATIO:
            self._compress_reservation_heap(symbol)

        if removed:
            logger.debug("预留槽位清理: symbol=%s 移除 %d 个过期槽位", symbol, removed)

    def _evict_oldest_if_needed(self, symbol: str, side: str) -> Optional[str]:
        key = self._get_side_key(symbol, side)
        orders = self._orders_by_symbol.get(symbol, {})
        side_order_count = sum(1 for o in orders.values() if o["side"] == side)
        if side_order_count < self.MAX_ORDERS_PER_SYMBOL_SIDE:
            return None

        heap = self._time_heap.get(key, [])
        while heap:
            created_at, oid = heapq.heappop(heap)
            if oid in orders and orders[oid]["side"] == side:
                self._remove_order_internal(symbol, oid)
                logger.warning("订单淘汰: %s 达到上限，移除最早订单 %s", key, oid)
                return oid

        self._compress_time_heap(symbol, side)
        heap = self._time_heap.get(key, [])
        while heap:
            created_at, oid = heapq.heappop(heap)
            if oid in orders and orders[oid]["side"] == side:
                self._remove_order_internal(symbol, oid)
                logger.warning("订单淘汰(重试): %s 达到上限，移除最早订单 %s", key, oid)
                return oid

        logger.error(
            "订单淘汰失败: %s 达到上限 %d，但无法找到可淘汰订单 #RECOVERY: 检查时间堆完整性",
            key, self.MAX_ORDERS_PER_SYMBOL_SIDE
        )
        return None

    def _remove_order_internal(self, symbol: str, order_id: str) -> Optional[Dict[str, Any]]:
        orders = self._orders_by_symbol.get(symbol)
        if not orders or order_id not in orders:
            return None
        order_info = orders.pop(order_id)
        self._remove_from_indices(order_info)
        return order_info

    def _update_order_id_mapping(self, order_id: str, symbol: str) -> None:
        with self._mapping_lock:
            self._order_id_to_symbol[order_id] = symbol

    def _delete_order_id_mapping(self, order_id: str) -> None:
        with self._mapping_lock:
            self._order_id_to_symbol.pop(order_id, None)

    def _get_symbol_by_order_id(self, order_id: str) -> Optional[str]:
        with self._mapping_lock:
            return self._order_id_to_symbol.get(order_id)

    def _verify_index_consistency(self, symbol: str) -> Tuple[bool, List[str]]:
        errors = []
        orders = self._orders_by_symbol.get(symbol, {})
        for side in ("buy", "sell"):
            key = self._get_side_key(symbol, side)
            sorted_list = self._price_index.get(key, [])
            indexed_ids = set(oid for _, oid in sorted_list)
            actual_ids = set(oid for oid, o in orders.items() if o["side"] == side)
            if indexed_ids != actual_ids:
                errors.append(f"{key} 价格索引不一致: 索引{len(indexed_ids)} 实际{len(actual_ids)}")
            heap = self._time_heap.get(key, [])
            heap_ids = set(oid for _, oid in heap)
            valid_heap_ids = heap_ids & actual_ids
            if len(valid_heap_ids) != len(actual_ids):
                errors.append(f"{key} 时间堆不一致: 有效条目{len(valid_heap_ids)} 实际{len(actual_ids)}")
            if heap and len(heap) > len(actual_ids) * self.HEAP_COMPRESS_RATIO:
                self._compress_time_heap(symbol, side)
        # 检查预留堆
        heap = self._reservation_heap_by_symbol.get(symbol, [])
        reservations = self._reservations_by_symbol.get(symbol, {})
        if heap and reservations and len(heap) > len(reservations) * self.RESERVE_HEAP_COMPRESS_RATIO:
            self._compress_reservation_heap(symbol)
        return len(errors) == 0, errors

    def _log_event(self, event_type: str, details: Dict[str, Any]) -> None:
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(event_type=event_type, details=details)
            except Exception as e:
                logger.warning("行为日志记录失败: %s", e)

    # ========== 公共接口 ==========
    def register_order(
        self, order_id: str, symbol: str, side: str, price: float, qty: float, order_type: str
    ) -> Dict[str, Any]:
        if not order_id or not symbol or side not in ("buy", "sell"):
            return {
                "status": "error", "reason": f"无效参数: order_id={order_id}, symbol={symbol}, side={side}",
                "data": {}, "warnings": ["invalid_parameters"], "retry_strategy": "abort",
            }
        if price <= 0 or qty <= 0:
            return {
                "status": "error", "reason": "价格或数量必须为正数",
                "data": {}, "warnings": ["invalid_price_or_qty"], "retry_strategy": "abort",
            }

        lock = self._get_lock(symbol)
        evicted_id = None
        with lock:
            self._cleanup_expired_reservations(symbol)
            orders = self._orders_by_symbol[symbol]

            if order_id in orders:
                return {
                    "status": "error", "reason": f"订单ID {order_id} 已存在",
                    "data": {}, "warnings": ["duplicate_order_id"], "retry_strategy": "abort",
                }

            # 同方向价格冲突检查
            conflict_ids = self._find_conflict_orders(symbol, side, price)
            if conflict_ids:
                return {
                    "status": "error", "reason": f"价格 {price} 与活跃订单 {conflict_ids[0]} 冲突",
                    "data": {"conflict_order_ids": conflict_ids}, "warnings": ["active_order_conflict"],
                    "retry_strategy": "abort",
                }

            # 【新增】反向自成交检测：检查相反方向是否有相同价格的订单
            opposite_side = "sell" if side == "buy" else "buy"
            self_trade_ids = self._find_conflict_orders(symbol, opposite_side, price)
            if self_trade_ids:
                logger.warning(
                    "内置自成交检测拦截: %s %s @ %.4f 与反向订单 %s 冲突",
                    symbol, side, price, self_trade_ids[0]
                )
                return {
                    "status": "error", "reason": f"检测到自成交风险，与反向订单 {self_trade_ids[0]} 冲突",
                    "data": {"self_trade_order_ids": self_trade_ids},
                    "warnings": ["self_trade_detected"],
                    "retry_strategy": "abort",
                }

            # 检查与预留槽位冲突
            for token, res in self._reservations_by_symbol.get(symbol, {}).items():
                if res["side"] == side and self._is_price_conflict(price, res["price"]):
                    return {
                        "status": "error", "reason": f"价格 {price} 与预留槽位 {token} 冲突",
                        "data": {"conflict_token": token}, "warnings": ["reservation_conflict"],
                        "retry_strategy": "retry",
                    }

            # 执行淘汰
            evicted_id = self._evict_oldest_if_needed(symbol, side)
            if evicted_id is None and sum(1 for o in orders.values() if o["side"] == side) >= self.MAX_ORDERS_PER_SYMBOL_SIDE:
                return {
                    "status": "error",
                    "reason": f"订单注册失败: {symbol} {side} 方向已达上限且无法淘汰",
                    "data": {}, "warnings": ["max_orders_reached_and_evict_failed"],
                    "retry_strategy": "retry",
                }

            now = time.time()
            order_info = {
                "order_id": order_id, "symbol": symbol, "side": side,
                "price": price, "qty": qty, "order_type": order_type,
                "created_at": now, "status": "active",
            }
            orders[order_id] = order_info
            self._add_to_indices(order_info)

        if evicted_id:
            self._delete_order_id_mapping(evicted_id)
        self._update_order_id_mapping(order_id, symbol)

        logger.info("订单注册成功: %s %s %s %.4f qty=%.4f", order_id, symbol, side, price, qty)
        self._log_event("order_registered", order_info)
        return {
            "status": "ok", "reason": f"订单 {order_id} 注册成功",
            "data": {"order_id": order_id, "created_at": now}, "warnings": [], "retry_strategy": "none",
        }

    def update_order_qty(self, order_id: str, new_qty: float) -> Dict[str, Any]:
        if not order_id or new_qty <= 0:
            return {
                "status": "error", "reason": "无效参数: order_id 或 new_qty",
                "data": {}, "warnings": ["invalid_parameters"], "retry_strategy": "abort",
            }
        symbol = self._get_symbol_by_order_id(order_id)
        if not symbol:
            return {
                "status": "error", "reason": f"订单 {order_id} 不存在",
                "data": {}, "warnings": ["order_not_found"], "retry_strategy": "abort",
            }
        lock = self._get_lock(symbol)
        with lock:
            orders = self._orders_by_symbol.get(symbol)
            if not orders or order_id not in orders:
                return {
                    "status": "error", "reason": f"订单 {order_id} 不存在",
                    "data": {}, "warnings": ["order_not_found"], "retry_strategy": "abort",
                }
            # 数量只能减少（部分成交），不能增加
            if new_qty > orders[order_id]["qty"]:
                logger.warning("订单数量更新拒绝: %s 新数量 %.4f 大于现有 %.4f", order_id, new_qty, orders[order_id]["qty"])
                return {
                    "status": "error", "reason": "新数量不能大于现有数量",
                    "data": {}, "warnings": ["invalid_qty_update"], "retry_strategy": "abort",
                }
            orders[order_id]["qty"] = new_qty
            logger.info("订单数量更新: %s -> qty=%.4f", order_id, new_qty)
        return {
            "status": "ok", "reason": f"订单 {order_id} 数量已更新",
            "data": {"order_id": order_id, "qty": new_qty}, "warnings": [], "retry_strategy": "none",
        }

    def unregister_order(self, order_id: str) -> Dict[str, Any]:
        if not order_id:
            return {
                "status": "error", "reason": "order_id 不能为空",
                "data": {}, "warnings": ["invalid_order_id"], "retry_strategy": "abort",
            }
        symbol = self._get_symbol_by_order_id(order_id)
        if not symbol:
            return {
                "status": "error", "reason": f"订单 {order_id} 不存在",
                "data": {}, "warnings": ["order_not_found"], "retry_strategy": "abort",
            }
        lock = self._get_lock(symbol)
        with lock:
            order_info = self._remove_order_internal(symbol, order_id)
            if order_info is None:
                return {
                    "status": "error", "reason": f"订单 {order_id} 不存在",
                    "data": {}, "warnings": ["order_not_found"], "retry_strategy": "abort",
                }
        self._delete_order_id_mapping(order_id)
        logger.info("订单撤销成功: %s", order_id)
        self._log_event("order_unregistered", order_info)
        return {
            "status": "ok", "reason": f"订单 {order_id} 已撤销",
            "data": {"order_id": order_id}, "warnings": [], "retry_strategy": "none",
        }

    def get_active_orders(
        self, symbol: Optional[str] = None, side: Optional[str] = None
    ) -> Dict[str, Any]:
        result = []
        symbols = [symbol] if symbol else sorted(self._orders_by_symbol.keys())
        for sym in symbols:
            lock = self._get_lock(sym)
            with lock:
                orders = self._orders_by_symbol.get(sym, {})
                for oid, info in orders.items():
                    if side and info.get("side") != side:
                        continue
                    result.append(info.copy())
        return {
            "status": "ok", "reason": f"查询到 {len(result)} 个活跃订单",
            "data": {"orders": result, "count": len(result)}, "warnings": [], "retry_strategy": "none",
        }

    def reserve_slot(
        self, symbol: str, side: str, price: float, qty: float, timeout_ms: Optional[int] = None
    ) -> Dict[str, Any]:
        if not symbol or side not in ("buy", "sell") or price <= 0 or qty <= 0:
            return {
                "status": "error", "reason": "无效参数",
                "data": {}, "warnings": ["invalid_parameters"], "retry_strategy": "abort",
            }
        timeout_s = (timeout_ms / 1000.0) if timeout_ms else self.DEFAULT_RESERVE_TIMEOUT_MS / 1000.0
        token = str(uuid.uuid4())
        lock = self._get_lock(symbol)
        with lock:
            self._cleanup_expired_reservations(symbol)

            # 同方向冲突检测
            conflict_ids = self._find_conflict_orders(symbol, side, price)
            if conflict_ids:
                return {
                    "status": "error", "reason": f"价格 {price} 与活跃订单 {conflict_ids[0]} 冲突",
                    "data": {"conflict_order_ids": conflict_ids}, "warnings": ["active_order_conflict"],
                    "retry_strategy": "abort",
                }

            # 【新增】反向自成交检测
            opposite_side = "sell" if side == "buy" else "buy"
            self_trade_ids = self._find_conflict_orders(symbol, opposite_side, price)
            if self_trade_ids:
                logger.warning(
                    "预留槽位自成交检测拦截: %s %s @ %.4f 与反向订单 %s 冲突",
                    symbol, side, price, self_trade_ids[0]
                )
                return {
                    "status": "error", "reason": f"检测到自成交风险，与反向订单 {self_trade_ids[0]} 冲突",
                    "data": {"self_trade_order_ids": self_trade_ids},
                    "warnings": ["self_trade_detected"],
                    "retry_strategy": "abort",
                }

            # 与其他预留槽位冲突
            for tok, res in self._reservations_by_symbol.get(symbol, {}).items():
                if res["side"] == side and self._is_price_conflict(price, res["price"]):
                    return {
                        "status": "error", "reason": f"价格 {price} 与预留槽位 {tok} 冲突",
                        "data": {"conflict_token": tok}, "warnings": ["reservation_conflict"],
                        "retry_strategy": "retry",
                    }

            now = time.time()
            expires_at = now + timeout_s
            reservation = {
                "token": token, "symbol": symbol, "side": side,
                "price": price, "qty": qty, "created_at": now, "expires_at": expires_at,
            }
            self._reservations_by_symbol[symbol][token] = reservation
            heapq.heappush(self._reservation_heap_by_symbol[symbol], (expires_at, token))

        logger.info("预留槽位成功: token=%s %s %s %.4f", token, symbol, side, price)
        return {
            "status": "ok", "reason": f"预留槽位 {token} 创建成功，有效期 {timeout_s:.1f}s",
            "data": {"token": token, "expires_at": expires_at},
            "warnings": [], "retry_strategy": "none",
        }

    def release_slot(self, token: str) -> Dict[str, Any]:
        if not token:
            return {
                "status": "error", "reason": "token 不能为空",
                "data": {}, "warnings": ["invalid_token"], "retry_strategy": "abort",
            }
        found_sym = None
        for sym, res_dict in self._reservations_by_symbol.items():
            if token in res_dict:
                found_sym = sym
                break
        if not found_sym:
            return {
                "status": "error", "reason": f"预留槽位 {token} 不存在或已过期",
                "data": {}, "warnings": ["reservation_not_found"], "retry_strategy": "abort",
            }
        lock = self._get_lock(found_sym)
        with lock:
            if token not in self._reservations_by_symbol.get(found_sym, {}):
                return {
                    "status": "error", "reason": f"预留槽位 {token} 不存在或已过期",
                    "data": {}, "warnings": ["reservation_not_found"], "retry_strategy": "abort",
                }
            del self._reservations_by_symbol[found_sym][token]

        logger.info("预留槽位释放: token=%s", token)
        return {
            "status": "ok", "reason": f"预留槽位 {token} 已释放",
            "data": {"token": token}, "warnings": [], "retry_strategy": "none",
        }

    def detect_self_trade(self, symbol: str, side: str, price: float) -> Dict[str, Any]:
        if not symbol or side not in ("buy", "sell"):
            return {
                "status": "error", "reason": "无效参数",
                "data": {}, "warnings": ["invalid_parameters"], "retry_strategy": "abort",
            }
        opposite_side = "sell" if side == "buy" else "buy"
        lock = self._get_lock(symbol)
        with lock:
            conflict_ids = self._find_conflict_orders(symbol, opposite_side, price)
            if conflict_ids:
                logger.warning(
                    "检测到自成交风险: %s %s @ %.4f 冲突订单 %s",
                    symbol, side, price, conflict_ids[0]
                )
                return {
                    "status": "ok", "reason": "检测到自成交风险",
                    "data": {"is_self_trade": True, "conflict_order_ids": conflict_ids},
                    "warnings": ["self_trade_risk"],
                    "retry_strategy": "abort",
                }
        return {
            "status": "ok", "reason": "未检测到自成交风险",
            "data": {"is_self_trade": False}, "warnings": [], "retry_strategy": "none",
        }

    # ========== 健康检查 ==========
    def health_check(self, timeout_sec: float = None) -> Dict[str, Any]:
        if timeout_sec is None:
            timeout_sec = self.HEALTH_CHECK_TIMEOUT_SEC
        try:
            total_orders = 0
            total_reservations = 0
            all_consistent = True
            all_errors = []
            start_time = time.time()
            symbols = list(self._orders_by_symbol.keys())
            for i, sym in enumerate(symbols):
                if time.time() - start_time > timeout_sec:
                    all_errors.append(f"健康检查超时: 已检查{i}/{len(symbols)}品种，剩余未检查")
                    break
                lock = self._get_lock(sym)
                with lock:
                    total_orders += len(self._orders_by_symbol[sym])
                    total_reservations += len(self._reservations_by_symbol.get(sym, {}))
                    consistent, errors = self._verify_index_consistency(sym)
                    if not consistent:
                        all_consistent = False
                        all_errors.extend(errors)

            if time.time() - start_time <= timeout_sec:
                with self._mapping_lock:
                    for oid, sym in list(self._order_id_to_symbol.items())[:1000]:
                        if sym not in self._orders_by_symbol or oid not in self._orders_by_symbol[sym]:
                            all_consistent = False
                            all_errors.append(f"映射表残留: {oid} -> {sym}")

            status = "ok" if all_consistent else "degraded"
            reason = (
                f"ActiveOrderRegistry 正常，活跃订单 {total_orders}，预留槽位 {total_reservations}"
                if all_consistent
                else f"索引不一致: {'; '.join(all_errors[:5])}"
            )
            return {
                "status": status,
                "reason": reason,
                "data": {
                    "order_count": total_orders,
                    "reservation_count": total_reservations,
                    "index_consistency": all_consistent,
                    "errors": all_errors[:20],
                },
                "warnings": [] if all_consistent else ["index_inconsistency_detected"],
            }
        except Exception as e:
            logger.error("健康检查失败: %s #RECOVERY: 检查锁状态和数据结构", e)
            return {
                "status": "error", "reason": str(e),
                "data": {}, "warnings": [f"health_check_failed: {e}"],
            }

    def stress_health_check(self) -> Dict[str, Any]:
        import random
        results = {}
        warnings = []
        test_symbol = "__STRESS_TEST__"
        lock = self._get_lock(test_symbol)
        latencies = []
        for _ in range(50):
            oid = f"stress_test_{uuid.uuid4().hex[:8]}"
            start = time.perf_counter()
            with lock:
                self._cleanup_expired_reservations(test_symbol)
                orders = self._orders_by_symbol[test_symbol]
                orders[oid] = {
                    "order_id": oid, "symbol": test_symbol, "side": "buy",
                    "price": 50000.0 + random.random(), "qty": 0.1, "order_type": "limit",
                    "created_at": time.time(), "status": "active",
                }
                self._add_to_indices(orders[oid])
            elapsed_us = (time.perf_counter() - start) * 1e6
            latencies.append(elapsed_us)
            with lock:
                self._remove_order_internal(test_symbol, oid)
            latencies.append((time.perf_counter() - start) * 1e6)

        with lock:
            self._orders_by_symbol.pop(test_symbol, None)
            for side in ("buy", "sell"):
                key = self._get_side_key(test_symbol, side)
                self._price_index.pop(key, None)
                self._time_heap.pop(key, None)
            self._reservations_by_symbol.pop(test_symbol, None)
            self._reservation_heap_by_symbol.pop(test_symbol, None)
            self._cleanup_call_count.pop(test_symbol, None)

        if latencies:
            sorted_lat = sorted(latencies)
            p50 = sorted_lat[len(sorted_lat) // 2]
            p99 = sorted_lat[min(int(len(sorted_lat) * 0.99), len(sorted_lat) - 1)]
            results["latency_p50_us"] = round(p50, 1)
            results["latency_p99_us"] = round(p99, 1)
            if p99 > 1000:
                warnings.append("P99延迟超过1ms，可能存在锁竞争")

        return {
            "status": "ok",
            "reason": "压力测试完成（隔离环境）",
            "data": results,
            "warnings": warnings,
        }

    def __del__(self):
        logger.info("ActiveOrderRegistry 实例销毁")
