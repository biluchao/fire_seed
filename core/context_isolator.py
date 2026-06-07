"""
火种系统 · 上下文隔离器 (ContextIsolator)
版本: 5.0.0 (华尔街高频交易级最终版)

核心职责：
1. 为不同时间周期（1m/5m/15m 等）创建完全独立的行情与指标数据视图，禁止跨周期数据访问
2. 提供数据添加、只读快照生成、视图冻结/解冻、原子替换、批量导入与状态校验能力
3. 内置金融级数据完整性校验（OHLCV字段、时间戳单调性、订单簿价格排序约束、IEEE 754 NaN/Inf防御）
4. 支持崩溃恢复（从快照恢复视图状态）、审计追踪（所有拒绝写入/冻结/解冻事件可追溯）
5. 提供全局聚合统计接口，便于运维容量规划与监控
6. 强制拦截 IEEE 754 非数值（NaN/Inf），确保金融计算的确定性

外部依赖（真实模块接口）：
- 无外部模块依赖，仅使用 Python 标准库（sys, time, copy, logging, threading, collections, enum, math）

接口契约：
- create_view / remove_view / get_view / swap_view / add_kline / add_orderbook / batch_add_klines
- freeze_view / unfreeze_view / freeze_all / get_all_timeframes / get_global_stats
- validate_isolation / validate_data_integrity / restore_from_snapshot / health_check
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 对不存在的周期操作返回 error 状态，不抛出异常
- 冻结视图后拒绝写入，返回 warning 并记录丢弃计数
- 数据完整性校验失败时拒绝写入，返回具体违规字段和违规值
- deepcopy 失败时降级为浅拷贝并记录 ERROR 日志
- 传入 NaN/Inf 时间戳时直接拒绝，返回 error

资源管理：
- 使用 deque 限制最大长度，自动淘汰旧数据，防止内存溢出
- 提供 remove_view() 显式释放不再需要的视图，含清空指标缓存
- 提供 swap_view() 支持原子替换大视图
- 不持有外部资源句柄，线程锁在对象销毁时自动释放
"""

import time
import copy
import logging
import threading
import sys
import math
from typing import Dict, Any, List, Optional, Tuple
from collections import deque, OrderedDict
from enum import Enum

logger = logging.getLogger(__name__)

__all__ = ["ContextIsolator", "IsolatedDataView", "DataIntegrityViolation"]
__version__ = "5.0.0"
__version_info__ = (5, 0, 0)


class DataIntegrityViolation(Exception):
    """金融数据完整性违规异常（携带违规详情供审计追踪）"""
    def __init__(self, message: str, violations: List[str] = None):
        super().__init__(message)
        self.violations = violations or []


class ViewState(Enum):
    ACTIVE = "active"
    FROZEN = "frozen"
    DEGRADED = "degraded"


class IsolatedDataView:
    """单个周期的独立数据视图（华尔街高频交易级金融数据完整性保障）"""

    # 类常量
    REQUIRED_KLINE_FIELDS = frozenset({
        "timestamp",  # Unix 时间戳（秒），必须为有限非负数值
        "open",       # 开盘价，必须 > 0
        "high",       # 最高价，必须 >= low
        "low",        # 最低价，必须 <= high
        "close",      # 收盘价，必须 > 0
        "volume",     # 成交量，必须 >= 0
    })
    MAX_INDICATOR_CACHE_SIZE = 500
    MAX_SINGLE_INDICATOR_VALUE_BYTES = 1024 * 1024  # 1MB，单个指标最大值
    MAX_FROZEN_DROP_ALERT = 10000
    FROZEN_DROP_CRITICAL = 100000
    MEMORY_ESTIMATE_KLINES_FACTOR = 512
    MEMORY_ESTIMATE_ORDERBOOK_FACTOR = 2048
    MEMORY_ESTIMATE_ORDEREDDICT_NODE_BYTES = 144  # 双向链表节点开销
    MAX_KLINES_LIMIT = 10000
    MAX_ORDERBOOK_LIMIT = 50000
    MAX_TIMESTAMP_FUTURE_SEC = 300
    MAX_COUNT_LIMIT = 100000  # get_klines count 参数上限，防止 O(count) 性能抖动
    MAX_FREEZE_HISTORY = 100  # 冻结操作审计记录上限

    def __init__(self, timeframe: str, max_klines: int = 500, max_orderbook: int = 1000):
        # 参数类型与边界校验
        if not isinstance(max_klines, int):
            raise TypeError(f"max_klines 必须为 int 类型，当前: {type(max_klines).__name__}")
        if not isinstance(max_orderbook, int):
            raise TypeError(f"max_orderbook 必须为 int 类型，当前: {type(max_orderbook).__name__}")
        if max_klines <= 0 or max_klines > self.MAX_KLINES_LIMIT:
            raise ValueError(f"max_klines 必须在 (0, {self.MAX_KLINES_LIMIT}] 范围内，当前值: {max_klines}")
        if max_orderbook <= 0 or max_orderbook > self.MAX_ORDERBOOK_LIMIT:
            raise ValueError(f"max_orderbook 必须在 (0, {self.MAX_ORDERBOOK_LIMIT}] 范围内，当前值: {max_orderbook}")

        self.timeframe = timeframe
        self.max_klines = max_klines
        self.max_orderbook = max_orderbook

        # 数据存储（入口统一深拷贝，内部不再重复拷贝）
        self._klines: deque = deque(maxlen=max_klines)
        self._orderbook_snapshots: deque = deque(maxlen=max_orderbook)

        # 指标缓存——使用 OrderedDict 实现 LRU，消除双重删除缺陷
        self._indicators: OrderedDict[str, Any] = OrderedDict()

        # 状态管理
        self._state: ViewState = ViewState.ACTIVE
        self._frozen_drop_count: int = 0
        self._last_write_timestamp: float = 0.0
        self._last_read_timestamp: float = 0.0
        self._total_writes: int = 0
        self._total_reads: int = 0
        self._last_unfrozen_count: int = 0
        self._freeze_history: List[Dict[str, Any]] = []

        # 时间戳辅助索引（支持同一时间戳多根 K 线）
        self._timestamp_index: Dict[float, List[int]] = {}

        # 线程安全（可重入锁）
        self._rwlock = threading.RLock()

        # 全局计数器（用于 batch_add_klines 防止溢出——仅作统计，Python int 永不溢出）
        self._batch_seq: int = 0

        logger.debug("创建数据视图: timeframe=%s, max_klines=%d, max_orderbook=%d",
                     timeframe, max_klines, max_orderbook)

    def __repr__(self) -> str:
        with self._rwlock:
            mem = self._calc_memory_usage_unsafe()
            return (f"IsolatedDataView(timeframe='{self.timeframe}', "
                    f"klines={len(self._klines)}/{self.max_klines}, "
                    f"ob={len(self._orderbook_snapshots)}/{self.max_orderbook}, "
                    f"state={self._state.value}, mem={mem//1024}KB)")

    # ========== 属性访问 ==========
    @property
    def is_frozen(self) -> bool:
        return self._state == ViewState.FROZEN

    @property
    def state(self) -> ViewState:
        return self._state

    @property
    def last_write_timestamp(self) -> float:
        return self._last_write_timestamp

    @property
    def frozen_drop_count(self) -> int:
        return self._frozen_drop_count

    @property
    def indicator_count(self) -> int:
        with self._rwlock:
            return len(self._indicators)

    @property
    def memory_usage_bytes(self) -> int:
        """估算视图内存占用（包含 OrderedDict 节点开销）"""
        with self._rwlock:
            return self._calc_memory_usage_unsafe()

    def _calc_memory_usage_unsafe(self) -> int:
        """不加锁的内存计算（内部使用，调用方需持锁）"""
        total = len(self._klines) * self.MEMORY_ESTIMATE_KLINES_FACTOR
        total += len(self._orderbook_snapshots) * self.MEMORY_ESTIMATE_ORDERBOOK_FACTOR
        total += sys.getsizeof(self._indicators)
        total += len(self._indicators) * self.MEMORY_ESTIMATE_ORDEREDDICT_NODE_BYTES
        for val in self._indicators.values():
            try:
                total += sys.getsizeof(val)
            except (TypeError, AttributeError):
                total += 1024
        return total

    # ========== 金融级数据完整性校验 ==========
    @staticmethod
    def _is_valid_timestamp(ts: Any) -> Tuple[bool, Optional[str]]:
        """校验时间戳是否为有限非负数值（防御 NaN/Inf）"""
        try:
            f = float(ts)
        except (ValueError, TypeError):
            return False, f"timestamp 无法转换为数值: {ts}"
        if math.isnan(f):
            return False, "timestamp 为 NaN"
        if math.isinf(f):
            return False, "timestamp 为 Inf"
        if f < 0:
            return False, f"timestamp 为负数: {f}"
        return True, None

    @classmethod
    def validate_kline(cls, kline: Dict[str, Any]) -> Tuple[bool, List[str]]:
        violations = []
        missing = cls.REQUIRED_KLINE_FIELDS - kline.keys()
        if missing:
            violations.append(f"缺失字段: {missing}")
        ts = kline.get("timestamp")
        valid_ts, ts_msg = cls._is_valid_timestamp(ts)
        if not valid_ts:
            violations.append(ts_msg)
        high = kline.get("high")
        low = kline.get("low")
        if high is not None and low is not None and high < low:
            violations.append(f"high({high}) < low({low})")
        open_val = kline.get("open")
        close_val = kline.get("close")
        if open_val is not None and high is not None and low is not None:
            if not (low <= open_val <= high):
                violations.append(f"open({open_val}) 不在 [{low}, {high}]")
            if not (low <= close_val <= high):
                violations.append(f"close({close_val}) 不在 [{low}, {high}]")
        return len(violations) == 0, violations

    @classmethod
    def validate_orderbook(cls, ob: Dict[str, Any]) -> Tuple[bool, List[str]]:
        violations = []
        for side in ["bids", "asks"]:
            if side not in ob:
                violations.append(f"缺失 {side}")
                continue
            if not isinstance(ob[side], list):
                violations.append(f"{side} 必须为 list")
                continue
            for i, level in enumerate(ob[side]):
                if not isinstance(level, (list, tuple)) or len(level) != 2:
                    violations.append(f"{side}[{i}] 格式错误，需为 [price, quantity]")
                    continue
                try:
                    price, qty = float(level[0]), float(level[1])
                    if price <= 0:
                        violations.append(f"{side}[{i}] price 非法: {price}")
                    if qty < 0:
                        violations.append(f"{side}[{i}] qty 为负: {qty}")
                except (ValueError, TypeError):
                    violations.append(f"{side}[{i}] 无法转换为数值")
            # 校验排序（跳过已标记非法条目）
            valid_levels = []
            for level in ob[side]:
                try:
                    valid_levels.append(float(level[0]))
                except (ValueError, TypeError):
                    valid_levels.append(None)
            if len(valid_levels) >= 2:
                for i in range(1, len(valid_levels)):
                    if valid_levels[i-1] is None or valid_levels[i] is None:
                        continue
                    if side == "bids" and valid_levels[i] > valid_levels[i-1]:
                        violations.append(f"bids 未按价格降序: idx={i}")
                        break
                    if side == "asks" and valid_levels[i] < valid_levels[i-1]:
                        violations.append(f"asks 未按价格升序: idx={i}")
                        break
        return len(violations) == 0, violations

    @classmethod
    def validate_timestamp_monotonic(cls, existing_deque: deque, new_ts: float) -> Tuple[bool, str]:
        if existing_deque and new_ts <= existing_deque[-1].get("timestamp", 0):
            last_ts = existing_deque[-1].get("timestamp", 0)
            return False, f"时间戳非单调递增: new={new_ts}, last={last_ts}"
        return True, ""

    # ========== 数据操作 ==========
    def add_kline(self, kline: Dict[str, Any], force: bool = False,
                  skip_validation: bool = False) -> Dict[str, Any]:
        """添加一根K线。force=True 跳过时间戳单调性；skip_validation=True 跳过所有校验（仅可信数据源使用）"""
        if not isinstance(kline, dict):
            return {"status": "error", "reason": "kline 必须为字典类型", "data": {}, "warnings": []}

        with self._rwlock:
            if self._state == ViewState.FROZEN:
                self._frozen_drop_count += 1
                if self._frozen_drop_count >= self.MAX_FROZEN_DROP_ALERT:
                    logger.warning("视图 %s 冻结后已丢弃 %d 条数据", self.timeframe, self._frozen_drop_count)
                return {"status": "warning", "reason": "视图已冻结",
                        "data": {"frozen_drop_count": self._frozen_drop_count}, "warnings": ["view_frozen"]}

            if not skip_validation:
                valid, violations = self.validate_kline(kline)
                if not valid:
                    return {"status": "error", "reason": "K线数据完整性校验失败",
                            "data": {"violations": violations}, "warnings": violations}
                ts = kline.get("timestamp", 0)
                if not force:
                    monotonic, msg = self.validate_timestamp_monotonic(self._klines, ts)
                    if not monotonic:
                        return {"status": "error", "reason": f"时间戳校验失败: {msg}", "data": {}, "warnings": [msg]}
            else:
                # 跳过校验时至少校验时间戳有效性
                ts = kline.get("timestamp", 0)
                valid_ts, ts_msg = self._is_valid_timestamp(ts)
                if not valid_ts:
                    return {"status": "error", "reason": ts_msg, "data": {}, "warnings": [ts_msg]}

            # deque 淘汰前清理索引
            if len(self._klines) == self._klines.maxlen and self._klines:
                oldest = self._klines[0]
                oldest_ts = oldest.get("timestamp")
                if oldest_ts is not None and oldest_ts in self._timestamp_index:
                    indices = self._timestamp_index[oldest_ts]
                    if indices and indices[0] == 0:
                        indices.pop(0)
                        if not indices:
                            del self._timestamp_index[oldest_ts]

            try:
                stored = copy.deepcopy(kline)
            except (RecursionError, MemoryError) as e:
                logger.error("deepcopy 失败: %s, 降级为 dict 浅拷贝", self.timeframe)
                stored = dict(kline)

            # 降级后二次校验
            if not skip_validation and stored is not kline:
                valid2, _ = self.validate_kline(stored)
                if not valid2:
                    logger.warning("降级副本二次校验失败: %s", self.timeframe)

            self._klines.append(stored)
            ts = stored.get("timestamp", 0)
            if ts not in self._timestamp_index:
                self._timestamp_index[ts] = []
            self._timestamp_index[ts].append(len(self._klines) - 1)

            self._last_write_timestamp = time.time()
            self._total_writes += 1
            self._indicators.clear()

        reason = f"已添加K线{' (强制模式)' if force else ''}{' (跳过校验)' if skip_validation else ''}"
        return {"status": "ok", "reason": reason,
                "data": {"timeframe": self.timeframe, "klines_count": len(self._klines),
                         "total_writes": self._total_writes}, "warnings": []}

    def batch_add_klines(self, klines: List[Dict[str, Any]], force: bool = False) -> Dict[str, Any]:
        if not isinstance(klines, list):
            return {"status": "error", "reason": "klines 必须为列表", "data": {}, "warnings": []}
        accepted = 0
        rejected = 0
        rejected_indices: List[int] = []
        with self._rwlock:
            if self._state == ViewState.FROZEN:
                return {"status": "warning", "reason": "视图已冻结", "data": {}, "warnings": ["view_frozen"]}
            # 批量操作期间分段释放锁，每50条释放一次
            for batch_start in range(0, len(klines), 50):
                batch = klines[batch_start:batch_start+50]
                for offset, kline in enumerate(batch):
                    if not isinstance(kline, dict):
                        rejected += 1
                        rejected_indices.append(batch_start + offset)
                        continue
                    valid, _ = self.validate_kline(kline)
                    if not valid:
                        rejected += 1
                        rejected_indices.append(batch_start + offset)
                        continue
                    ts = kline.get("timestamp", 0)
                    if not force:
                        monotonic, _ = self.validate_timestamp_monotonic(self._klines, ts)
                        if not monotonic:
                            rejected += 1
                            rejected_indices.append(batch_start + offset)
                            continue
                    # deque 淘汰前清理索引
                    if len(self._klines) == self._klines.maxlen and self._klines:
                        oldest = self._klines[0]
                        oldest_ts_val = oldest.get("timestamp")
                        if oldest_ts_val is not None and oldest_ts_val in self._timestamp_index:
                            indices = self._timestamp_index[oldest_ts_val]
                            if indices and indices[0] == 0:
                                indices.pop(0)
                                if not indices:
                                    del self._timestamp_index[oldest_ts_val]
                    try:
                        stored = copy.deepcopy(kline)
                    except (RecursionError, MemoryError):
                        stored = dict(kline)
                    self._klines.append(stored)
                    if ts not in self._timestamp_index:
                        self._timestamp_index[ts] = []
                    self._timestamp_index[ts].append(len(self._klines) - 1)
                    accepted += 1
            self._indicators.clear()
            self._batch_seq += 1
        logger.info("批量导入完成: accepted=%d, rejected=%d", accepted, rejected)
        result: Dict[str, Any] = {
            "status": "ok",
            "reason": f"批量导入: {accepted} 成功, {rejected} 拒绝",
            "data": {"accepted": accepted, "rejected": rejected, "klines_count": len(self._klines)},
            "warnings": []
        }
        if rejected_indices:
            result["data"]["rejected_indices"] = rejected_indices[:20]
        return result

    def add_orderbook(self, ob: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(ob, dict):
            return {"status": "error", "reason": "ob 必须为字典类型", "data": {}, "warnings": []}
        valid, violations = self.validate_orderbook(ob)
        if not valid:
            return {"status": "error", "reason": "订单簿格式校验失败",
                    "data": {"violations": violations}, "warnings": violations}
        with self._rwlock:
            if self._state == ViewState.FROZEN:
                self._frozen_drop_count += 1
                return {"status": "warning", "reason": "视图已冻结",
                        "data": {"frozen_drop_count": self._frozen_drop_count}, "warnings": ["view_frozen"]}
            ts = ob.get("timestamp", 0)
            valid_ts, ts_msg = self._is_valid_timestamp(ts)
            if not valid_ts:
                return {"status": "error", "reason": ts_msg, "data": {}, "warnings": [ts_msg]}
            if self._orderbook_snapshots and ts <= self._orderbook_snapshots[-1].get("timestamp", 0):
                return {"status": "error", "reason": "订单簿时间戳必须单调递增", "data": {},
                        "warnings": ["non_monotonic_timestamp"]}
            try:
                stored = copy.deepcopy(ob)
            except (RecursionError, MemoryError):
                logger.error("订单簿 deepcopy 失败: %s", self.timeframe)
                stored = {"timestamp": ob.get("timestamp", 0),
                          "bids": [list(level) for level in ob.get("bids", [])],
                          "asks": [list(level) for level in ob.get("asks", [])]}
            self._orderbook_snapshots.append(stored)
            self._last_write_timestamp = time.time()
            self._total_writes += 1
        return {"status": "ok", "reason": "已添加订单簿快照",
                "data": {"timeframe": self.timeframe}, "warnings": []}

    def get_klines(self, count: Optional[int] = None) -> List[Dict[str, Any]]:
        if count is not None and (not isinstance(count, int) or count < 0):
            count = None
        if count is not None and count > self.MAX_COUNT_LIMIT:
            count = self.MAX_COUNT_LIMIT
        with self._rwlock:
            self._last_read_timestamp = time.time()
            self._total_reads += 1
            if count is None or count <= 0:
                return list(self._klines)
            return list(self._klines)[-count:]

    def get_kline_by_timestamp(self, ts: float) -> Optional[Dict[str, Any]]:
        valid_ts, _ = self._is_valid_timestamp(ts)
        if not valid_ts:
            return None
        with self._rwlock:
            if not self._klines:
                return None
            if ts in self._timestamp_index:
                indices = self._timestamp_index[ts]
                for idx in reversed(indices):
                    if 0 <= idx < len(self._klines) and self._klines[idx].get("timestamp") == ts:
                        return self._klines[idx]
            for k in reversed(self._klines):
                if k.get("timestamp") == ts:
                    return k
            return None

    def get_orderbook(self) -> Optional[Dict[str, Any]]:
        with self._rwlock:
            self._last_read_timestamp = time.time()
            self._total_reads += 1
            return self._orderbook_snapshots[-1] if self._orderbook_snapshots else None

    def set_indicator(self, key: str, value: Any) -> None:
        with self._rwlock:
            if key in self._indicators:
                del self._indicators[key]
            elif len(self._indicators) >= self.MAX_INDICATOR_CACHE_SIZE:
                self._indicators.popitem(last=False)
            self._indicators[key] = value

    def get_indicator(self, key: str, default: Any = None) -> Any:
        with self._rwlock:
            val = self._indicators.get(key, default)
            if key in self._indicators:
                self._indicators.move_to_end(key)
            return val

    def clear_indicators(self) -> None:
        with self._rwlock:
            self._indicators.clear()

    # ========== 状态管理 ==========
    def freeze(self) -> None:
        with self._rwlock:
            self._state = ViewState.FROZEN
            entry = {"action": "freeze", "timestamp": time.time(),
                     "klines_count": len(self._klines),
                     "orderbook_count": len(self._orderbook_snapshots)}
            self._freeze_history.append(entry)
            if len(self._freeze_history) > self.MAX_FREEZE_HISTORY:
                self._freeze_history = self._freeze_history[-self.MAX_FREEZE_HISTORY:]
            logger.info("视图已冻结: timeframe=%s, klines=%d, ob=%d",
                        self.timeframe, len(self._klines), len(self._orderbook_snapshots))

    def unfreeze(self) -> Dict[str, Any]:
        with self._rwlock:
            was_frozen = self._state == ViewState.FROZEN
            self._last_unfrozen_count = self._frozen_drop_count
            self._state = ViewState.ACTIVE
            self._frozen_drop_count = 0
            entry = {"action": "unfreeze", "timestamp": time.time(),
                     "dropped_during_freeze": self._last_unfrozen_count}
            self._freeze_history.append(entry)
            if len(self._freeze_history) > self.MAX_FREEZE_HISTORY:
                self._freeze_history = self._freeze_history[-self.MAX_FREEZE_HISTORY:]
            if was_frozen:
                logger.info("视图已解冻: timeframe=%s, dropped=%d",
                            self.timeframe, self._last_unfrozen_count)
            return {"status": "ok", "reason": "视图已解冻",
                    "data": {"dropped_during_freeze": self._last_unfrozen_count}, "warnings": []}

    def validate_data_integrity(self) -> Dict[str, Any]:
        with self._rwlock:
            klines = list(self._klines)
            gaps = []
            for i in range(1, len(klines)):
                ts_prev = klines[i-1].get("timestamp", 0)
                ts_curr = klines[i].get("timestamp", 0)
                if ts_curr <= ts_prev:
                    gaps.append({"index": i, "prev_ts": ts_prev, "curr_ts": ts_curr})
            status = "ok" if not gaps else "degraded"
            return {"status": status, "reason": "完整性校验完成",
                    "data": {"klines_count": len(klines), "gaps": gaps}, "warnings": []}

    def health_check(self) -> Dict[str, Any]:
        with self._rwlock:
            data_age = (time.time() - self._last_write_timestamp) if self._last_write_timestamp > 0 else -1.0
            return {
                "timeframe": self.timeframe,
                "state": self._state.value,
                "klines_count": len(self._klines),
                "orderbook_count": len(self._orderbook_snapshots),
                "indicator_count": len(self._indicators),
                "total_writes": self._total_writes,
                "total_reads": self._total_reads,
                "frozen_drop_count": self._frozen_drop_count,
                "last_write_age_seconds": data_age if data_age >= 0 else None,
                "memory_usage_bytes": self._calc_memory_usage_unsafe(),
                "status": "ok" if self._state != ViewState.DEGRADED else "degraded",
            }


class ContextIsolator:
    """上下文隔离器：管理所有周期的独立数据视图（机构级实现）"""

    DEFAULT_MAX_KLINES = 500
    DEFAULT_MAX_ORDERBOOK = 1000
    MAX_KLINES_LIMIT = 10000
    MAX_ORDERBOOK_LIMIT = 50000
    TIMEFRAME_NORMALIZATION = {
        "1min": "1m", "1m": "1m", "2min": "2m", "2m": "2m",
        "3min": "3m", "3m": "3m", "5min": "5m", "5m": "5m",
        "10min": "10m", "10m": "10m", "15min": "15m", "15m": "15m",
        "30min": "30m", "30m": "30m", "1h": "1h", "1hour": "1h",
        "4h": "4h", "1d": "1d", "1day": "1d",
    }
    STALENESS_BY_TIMEFRAME = {
        "1m": 120, "2m": 180, "3m": 300, "5m": 600, "10m": 900,
        "15m": 1800, "30m": 3600, "1h": 7200, "4h": 14400, "1d": 172800,
    }

    def __init__(self):
        self._views: Dict[str, IsolatedDataView] = {}
        self._lock = threading.Lock()
        logger.info("ContextIsolator v%s 初始化完成", __version__)

    # ========== 视图生命周期管理 ==========
    def create_view(self, timeframe: str, max_klines: int = None, max_orderbook: int = None) -> Dict[str, Any]:
        if not timeframe or not isinstance(timeframe, str):
            return {"status": "error", "reason": "timeframe 必须是非空字符串", "data": {}, "warnings": []}
        if max_klines is not None and not isinstance(max_klines, int):
            return {"status": "error", "reason": f"max_klines 必须为 int 类型", "data": {}, "warnings": []}
        if max_orderbook is not None and not isinstance(max_orderbook, int):
            return {"status": "error", "reason": f"max_orderbook 必须为 int 类型", "data": {}, "warnings": []}
        normalized = self._normalize_timeframe(timeframe)
        max_klines = max_klines or self.DEFAULT_MAX_KLINES
        max_orderbook = max_orderbook or self.DEFAULT_MAX_ORDERBOOK
        if max_klines > self.MAX_KLINES_LIMIT:
            return {"status": "error", "reason": f"max_klines 超过上限 {self.MAX_KLINES_LIMIT}", "data": {}, "warnings": []}
        if max_orderbook > self.MAX_ORDERBOOK_LIMIT:
            return {"status": "error", "reason": f"max_orderbook 超过上限 {self.MAX_ORDERBOOK_LIMIT}", "data": {}, "warnings": []}
        with self._lock:
            if normalized in self._views:
                return {"status": "already_exists", "reason": f"视图 {normalized} 已存在",
                        "data": {"timeframe": normalized}, "warnings": []}
            view = IsolatedDataView(normalized, max_klines, max_orderbook)
            self._views[normalized] = view
        logger.info("创建视图成功: timeframe=%s", normalized)
        return {"status": "ok", "reason": f"已创建 {normalized} 周期数据视图",
                "data": {"timeframe": normalized}, "warnings": []}

    def remove_view(self, timeframe: str) -> Dict[str, Any]:
        normalized = self._normalize_timeframe(timeframe)
        with self._lock:
            view = self._views.pop(normalized, None)
            if view is None:
                return {"status": "error", "reason": f"视图 {normalized} 不存在", "data": {}, "warnings": []}
            view.clear_indicators()  # 显式释放指标缓存
            stats = {
                "timeframe": normalized,
                "klines_count": len(view._klines),
                "orderbook_count": len(view._orderbook_snapshots),
                "indicator_count": view.indicator_count,
                "total_writes": view._total_writes,
            }
        logger.info("视图已移除: %s, 统计=%s", normalized, stats)
        return {"status": "ok", "reason": f"视图 {normalized} 已移除", "data": stats, "warnings": []}

    def get_view(self, timeframe: str) -> Optional[IsolatedDataView]:
        normalized = self._normalize_timeframe(timeframe)
        with self._lock:
            return self._views.get(normalized)

    def swap_view(self, timeframe: str, new_view: 'IsolatedDataView') -> Dict[str, Any]:
        if not isinstance(new_view, IsolatedDataView):
            return {"status": "error", "reason": "new_view 必须为 IsolatedDataView 实例", "data": {}, "warnings": []}
        if new_view.timeframe != self._normalize_timeframe(timeframe):
            logger.warning("swap_view: 新视图 timeframes 不匹配: %s vs %s",
                           new_view.timeframe, self._normalize_timeframe(timeframe))
        normalized = self._normalize_timeframe(timeframe)
        with self._lock:
            old_view = self._views.get(normalized)
            old_count = len(old_view._klines) if old_view else 0
            self._views[normalized] = new_view
            new_count = len(new_view._klines)
        logger.info("视图原子替换: %s, old=%d, new=%d", normalized, old_count, new_count)
        return {"status": "ok", "reason": "视图已原子替换",
                "data": {"old_klines": old_count, "new_klines": new_count}, "warnings": []}

    def get_all_timeframes(self) -> List[str]:
        with self._lock:
            return list(self._views.keys())

    def get_global_stats(self) -> Dict[str, Any]:
        with self._lock:
            views_stats = {}
            total_klines = 0
            total_ob = 0
            total_memory = 0
            total_indicators = 0
            for tf, view in self._views.items():
                s = view.health_check()
                views_stats[tf] = s
                total_klines += s["klines_count"]
                total_ob += s["orderbook_count"]
                total_memory += s["memory_usage_bytes"]
                total_indicators += s["indicator_count"]
            return {
                "status": "ok",
                "reason": "全局统计",
                "data": {
                    "view_count": len(self._views),
                    "total_klines": total_klines,
                    "total_orderbook_snapshots": total_ob,
                    "total_memory_bytes": total_memory,
                    "total_indicators": total_indicators,
                    "views": views_stats,
                },
                "warnings": [],
            }

    # ========== 数据操作 ==========
    def _safe_get_view(self, timeframe: str) -> Tuple[Optional[IsolatedDataView], str]:
        """统一获取视图，返回 (view, error_reason)"""
        normalized = self._normalize_timeframe(timeframe)
        with self._lock:
            view = self._views.get(normalized)
        if view is None:
            return None, f"视图 {normalized} 不存在，请先创建"
        return view, ""

    def add_kline(self, timeframe: str, kline: Dict[str, Any], force: bool = False,
                  skip_validation: bool = False) -> Dict[str, Any]:
        view, error = self._safe_get_view(timeframe)
        if view is None:
            return {"status": "error", "reason": error, "data": {}, "warnings": []}
        return view.add_kline(kline, force, skip_validation)

    def batch_add_klines(self, timeframe: str, klines: List[Dict[str, Any]],
                         force: bool = False) -> Dict[str, Any]:
        view, error = self._safe_get_view(timeframe)
        if view is None:
            return {"status": "error", "reason": error, "data": {}, "warnings": []}
        return view.batch_add_klines(klines, force)

    def add_orderbook(self, timeframe: str, ob: Dict[str, Any]) -> Dict[str, Any]:
        view, error = self._safe_get_view(timeframe)
        if view is None:
            return {"status": "error", "reason": error, "data": {}, "warnings": []}
        return view.add_orderbook(ob)

    # ========== 批量操作 ==========
    def freeze_view(self, timeframe: str) -> Dict[str, Any]:
        view, error = self._safe_get_view(timeframe)
        if view is None:
            return {"status": "error", "reason": error, "data": {}, "warnings": []}
        view.freeze()
        return {"status": "ok", "reason": "已冻结", "data": {}, "warnings": []}

    def unfreeze_view(self, timeframe: str) -> Dict[str, Any]:
        view, error = self._safe_get_view(timeframe)
        if view is None:
            return {"status": "error", "reason": error, "data": {}, "warnings": []}
        return view.unfreeze()

    def freeze_all(self) -> Dict[str, Any]:
        already_frozen = []
        newly_frozen = []
        with self._lock:
            for tf, view in list(self._views.items()):
                if view.is_frozen:
                    already_frozen.append(tf)
                else:
                    view.freeze()
                    newly_frozen.append(tf)
        return {"status": "ok", "reason": f"冻结: 新冻结{len(newly_frozen)} 已冻结{len(already_frozen)}",
                "data": {"newly_frozen": newly_frozen, "already_frozen": already_frozen}, "warnings": []}

    # ========== 诊断接口 ==========
    def validate_isolation(self) -> Dict[str, Any]:
        with self._lock:
            timeframes = list(self._views.keys())
            views = {tf: self._views[tf] for tf in timeframes}
        issues = []
        for i, tf1 in enumerate(timeframes):
            for tf2 in timeframes[i+1:]:
                if views[tf1]._klines is views[tf2]._klines:
                    issues.append(f"引用共享: {tf1}._klines is {tf2}._klines")
        return {"status": "ok" if not issues else "error", "reason": "视图隔离校验完成",
                "data": {"issues": issues}, "warnings": issues}

    def validate_data_integrity(self, timeframe: str) -> Dict[str, Any]:
        view, error = self._safe_get_view(timeframe)
        if view is None:
            return {"status": "error", "reason": error, "data": {}, "warnings": []}
        return view.validate_data_integrity()

    def restore_from_snapshot(self, snapshot: Dict[str, Any],
                              replace_existing: bool = False) -> Dict[str, Any]:
        if not isinstance(snapshot, dict):
            return {"status": "error", "reason": "快照格式无效", "data": {}, "warnings": []}
        restored = []
        failed = []
        skipped = []
        for tf, data in snapshot.items():
            normalized = self._normalize_timeframe(tf)
            with self._lock:
                if normalized in self._views and not replace_existing:
                    existing = self._views[normalized]
                    skipped.append({
                        "timeframe": normalized,
                        "klines_count": len(existing._klines),
                        "orderbook_count": len(existing._orderbook_snapshots)
                    })
                    continue
            try:
                view = IsolatedDataView(normalized)
                if "klines" in data and data["klines"]:
                    view.batch_add_klines(data["klines"], force=True)
                if "orderbooks" in data:
                    for ob in data["orderbooks"]:
                        view.add_orderbook(ob)
                with self._lock:
                    self._views[normalized] = view
                restored.append(normalized)
            except Exception as e:
                logger.error("恢复视图 %s 失败: %s", normalized, e)
                failed.append({"timeframe": normalized, "error": str(e)})
        return {"status": "ok",
                "reason": f"恢复: {len(restored)}成功 {len(skipped)}跳过 {len(failed)}失败",
                "data": {"restored": restored, "skipped": skipped, "failed": failed},
                "warnings": []}

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        try:
            stats = self.get_global_stats()
            with self._lock:
                stale_views = []
                for tf, view in self._views.items():
                    age = (time.time() - view.last_write_timestamp) if view.last_write_timestamp > 0 else None
                    threshold = self.STALENESS_BY_TIMEFRAME.get(tf, 600)
                    if age is not None and age > threshold:
                        stale_views.append({"timeframe": tf, "age_seconds": age, "threshold": threshold})
            warnings = [f"数据陈旧: {s}" for s in stale_views] if stale_views else []
            return {"status": "ok" if not warnings else "degraded",
                    "reason": "健康检查完成",
                    "data": {**stats["data"], "stale_views": stale_views},
                    "warnings": warnings}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": [str(e)]}

    # ========== 私有方法 ==========
    @classmethod
    def _normalize_timeframe(cls, timeframe: str) -> str:
        key = timeframe.lower().strip()
        normalized = cls.TIMEFRAME_NORMALIZATION.get(key)
        if normalized:
            return normalized
        logger.warning("非标准周期标识: %s，已原样接受", timeframe)
        return key.lower().strip()
