"""
火种系统 · 感官快照标准化接口 (SensorySnapshot)

核心职责：
1. 为五感皮层提供统一的感知数据采集接口
2. 将原始数据封装为标准化快照字典，供下游评分卡、协商总线等模块使用

外部依赖（真实模块接口）：
- core.perception.visual_cortex.VisualCortex : 获取视觉感知数据
- core.perception.auditory_cortex.AuditoryCortex : 获取听觉感知数据
- core.perception.tactile_cortex.TactileCortex : 获取触觉感知数据
- core.perception.olfactory_cortex.OlfactoryCortex : 获取嗅觉感知数据
- core.perception.gustatory_cortex.GustatoryCortex : 获取味觉感知数据

接口契约：
- capture_snapshot(symbol: str, context: Dict[str, Any]) -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- 返回值固定包含 "status", "reason", "data", "warnings"

异常与降级：
- 当任一感官皮层不可用时，使用保守默认值填充
- 所有感官皮层均不可用时，状态标记为 "degraded"
- 降级值在类常量区明确声明

资源管理：
- 不持有任何需要手动释放的外部资源
"""

import time
import logging
import hashlib
import json
import threading
from typing import Dict, Any, List, Optional, Tuple, Callable
from collections import deque, OrderedDict
from enum import Enum
from types import MappingProxyType

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """熔断器状态"""
    CLOSED = "closed"
    HALF_OPEN = "half_open"
    OPEN = "open"


class SensorySnapshot:
    """感官快照标准化接口"""

    # ========== 类常量（不可变默认降级值） ==========
    DEFAULT_VISUAL = MappingProxyType({
        "candlestick_pattern": None, "orderbook_slope": 0.0,
        "wall_resilience": "unknown", "ma12_position": "unknown"
    })
    DEFAULT_AUDITORY = MappingProxyType({
        "macro_alert_level": "none", "time_to_next_event_sec": 9999,
        "sentiment_score": 0.0, "sentiment_momentum": 0.0
    })
    DEFAULT_TACTILE = MappingProxyType({
        "liquidity_level": "L3", "depth_decay_speed_bps": 0.0,
        "trade_pulse_cv": 0.0, "order_toxicity_flag": False
    })
    DEFAULT_OLFACTORY = MappingProxyType({
        "paper_wall_flag": False, "spread_manipulation_flag": False,
        "contagion_risk_index": 0.0
    })
    DEFAULT_GUSTATORY = MappingProxyType({
        "similar_historical_win_rate": 0.5, "bitter_memory_similarity": 0.0
    })

    # 性能与可靠性配置
    MAX_SENSOR_RESPONSE_SEC = 0.01          # 单个感官最大响应时间（秒）
    MAX_TOTAL_SNAPSHOT_SEC = 0.05           # 整体快照最大耗时（秒）
    CACHE_TTL_NORMAL_SEC = 0.5              # 正常波动缓存有效期（秒）
    CACHE_TTL_HIGH_VOL_SEC = 0.1            # 高波动缓存有效期（秒）
    CACHE_TTL_DEGRADED_SEC = 0.05           # 降级快照最大缓存时间（秒）
    HIGH_VOL_THRESHOLD = 0.8                # 波动率分位阈值
    MAX_WARNINGS_PER_SESSION = 10           # 单次采集最大告警条数
    METRICS_WINDOW = 50                     # 性能统计滑动窗口大小
    SENSOR_CIRCUIT_BREAK_THRESHOLD = 3      # 连续失败次数阈值
    SENSOR_CIRCUIT_BREAK_TIMEOUT = 30.0     # 熔断恢复时间（秒）
    CACHE_HIT_DECAY = 0.9                   # 缓存命中率EMA衰减系数
    CACHE_MISS_DECAY = 0.9                  # 缓存未命中率EMA衰减系数
    MAX_SYMBOL_LENGTH = 30                  # 交易对最大长度
    CONTEXT_HASH_DEPTH = 5                  # 上下文哈希递归深度
    SENSOR_TIMEOUT_OVERRIDES = MappingProxyType({
        "gustatory": 0.02                   # 味觉超时稍长（秒）
    })
    # 各感官必需字段集（不可变集合）
    REQUIRED_FIELDS: MappingProxyType = MappingProxyType({
        "visual": frozenset({"candlestick_pattern", "orderbook_slope", "wall_resilience", "ma12_position"}),
        "auditory": frozenset({"macro_alert_level", "time_to_next_event_sec", "sentiment_score"}),
        "tactile": frozenset({"liquidity_level", "depth_decay_speed_bps", "trade_pulse_cv", "order_toxicity_flag"}),
        "olfactory": frozenset({"paper_wall_flag", "spread_manipulation_flag", "contagion_risk_index"}),
        "gustatory": frozenset({"similar_historical_win_rate", "bitter_memory_similarity"}),
    })
    # 上下文哈希采样字段
    CONTEXT_HASH_FIELDS = frozenset({"kline", "orderbook", "trade_stream", "system_state", "market_data"})

    def __init__(self):
        """初始化感官快照模块"""
        # 感官皮层实例
        self._visual_cortex: Optional[Any] = None
        self._auditory_cortex: Optional[Any] = None
        self._tactile_cortex: Optional[Any] = None
        self._olfactory_cortex: Optional[Any] = None
        self._gustatory_cortex: Optional[Any] = None

        # 熔断状态（统一使用 monotonic 时间）
        self._sensor_fail_count: Dict[str, int] = {name: 0 for name in self.REQUIRED_FIELDS}
        self._sensor_last_fail_time: Dict[str, float] = {name: 0.0 for name in self.REQUIRED_FIELDS}
        self._sensor_circuit_state: Dict[str, CircuitState] = {name: CircuitState.CLOSED for name in self.REQUIRED_FIELDS}
        self._sensor_circuit_since: Dict[str, float] = {name: 0.0 for name in self.REQUIRED_FIELDS}
        self._sensor_half_open_probe_count: Dict[str, int] = {name: 0 for name in self.REQUIRED_FIELDS}
        self._sensor_lock = threading.Lock()

        # 性能统计
        self._latencies: deque = deque(maxlen=self.METRICS_WINDOW)
        self._latency_sum: float = 0.0         # 维护累加和，避免每次遍历
        self._total_captures: int = 0
        self._failed_captures: int = 0
        self._avg_latency_ms: float = 0.0
        self._last_latency_ms: float = 0.0
        self._cache_hit_rate: float = 0.0
        self._metrics_lock = threading.Lock()

        # 缓存（只存高质量快照）
        self._cached_snapshot: Optional[Dict[str, Any]] = None
        self._cached_symbol: str = ""
        self._cached_context_hash: str = ""
        self._cached_mono_time: float = 0.0
        self._cached_is_degraded: bool = False
        self._cache_lock = threading.Lock()

        logger.info("SensorySnapshot 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        visual_cortex: Optional[Any] = None,
        auditory_cortex: Optional[Any] = None,
        tactile_cortex: Optional[Any] = None,
        olfactory_cortex: Optional[Any] = None,
        gustatory_cortex: Optional[Any] = None,
    ) -> None:
        """注入五感皮层实例"""
        self._visual_cortex = visual_cortex
        self._auditory_cortex = auditory_cortex
        self._tactile_cortex = tactile_cortex
        self._olfactory_cortex = olfactory_cortex
        self._gustatory_cortex = gustatory_cortex

        # 验证方法签名
        for name, cortex in [
            ("visual", visual_cortex), ("auditory", auditory_cortex),
            ("tactile", tactile_cortex), ("olfactory", olfactory_cortex),
            ("gustatory", gustatory_cortex)
        ]:
            method = _get_sensor_method(name)
            if cortex and not hasattr(cortex, method):
                logger.error("传感器 %s 缺少必要方法: %s", name, method)

        missing = [n for n, c in [
            ("视觉", visual_cortex), ("听觉", auditory_cortex),
            ("触觉", tactile_cortex), ("嗅觉", olfactory_cortex),
            ("味觉", gustatory_cortex)
        ] if c is None]
        if missing:
            logger.warning("部分感官皮层未注入，将使用降级值。缺失: %s", ", ".join(missing))

    # ========== 公共接口 ==========
    def capture_snapshot(
        self, symbol: str, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """采集感官快照"""
        if context is None:
            context = {}

        # 符号校验
        symbol = symbol.strip()
        if (not symbol or len(symbol) > self.MAX_SYMBOL_LENGTH or
                not _is_valid_symbol(symbol)):
            return {
                "status": "error",
                "reason": f"无效的交易品种符号: {symbol}",
                "data": {}, "warnings": ["invalid_symbol"]
            }

        # 轻量防御性拷贝：仅拷贝传感器可能读取的顶层字段
        protected_context = _shallow_copy_context(context)

        # 提取波动率分位
        vol_percentile = self._extract_volatility(protected_context)

        # 缓存检查（单调时钟）
        context_hash = self._compute_context_hash(protected_context)
        now_mono = time.monotonic()
        ttl = (self.CACHE_TTL_HIGH_VOL_SEC if vol_percentile > self.HIGH_VOL_THRESHOLD
               else self.CACHE_TTL_NORMAL_SEC)
        with self._cache_lock:
            if (self._cached_symbol == symbol and
                    self._cached_context_hash == context_hash and
                    now_mono - self._cached_mono_time < ttl and
                    self._cached_snapshot is not None):
                with self._metrics_lock:
                    self._cache_hit_rate = (self._cache_hit_rate * self.CACHE_HIT_DECAY +
                                            (1 - self.CACHE_HIT_DECAY))
                return {
                    "status": "ok",
                    "reason": f"返回缓存的感官快照: {symbol}",
                    "data": dict(self._cached_snapshot),
                    "warnings": []
                }

        # 开始采集
        start_mono = time.monotonic()
        snapshot: Dict[str, Any] = {"timestamp": time.time(), "mono_time": start_mono, "symbol": symbol, "source": {}}
        warnings: List[str] = []
        sensor_failures = 0
        snapshot_quality = "full"

        # 传感器配置（从类属性构建）
        sensor_configs = [
            ("visual", self._visual_cortex, self.DEFAULT_VISUAL, "visual",
             lambda ctx: self._visual_cortex.perceive(ctx.get("kline"))),
            ("auditory", self._auditory_cortex, self.DEFAULT_AUDITORY, "auditory",
             lambda ctx: self._auditory_cortex.listen()),
            ("tactile", self._tactile_cortex, self.DEFAULT_TACTILE, "tactile",
             lambda ctx: self._tactile_cortex.sense(ctx.get("orderbook"), ctx.get("trade_stream"))),
            ("olfactory", self._olfactory_cortex, self.DEFAULT_OLFACTORY, "olfactory",
             lambda ctx: self._olfactory_cortex.sniff(ctx.get("system_state"), ctx.get("market_data"))),
            ("gustatory", self._gustatory_cortex, self.DEFAULT_GUSTATORY, "gustatory",
             lambda ctx: self._gustatory_cortex.taste(ctx.get("last_trade"))),
        ]

        for sensor_name, cortex, default, key, capture_fn in sensor_configs:
            if cortex is None:
                snapshot[key] = dict(default)
                snapshot["source"][key] = "unavailable"
                warnings.append(f"{sensor_name}_unavailable")
                sensor_failures += 1
                snapshot_quality = "degraded"
                continue

            with self._sensor_lock:
                state, allowed = self._circuit_allow(sensor_name)
            if not allowed:
                snapshot[key] = dict(default)
                snapshot["source"][key] = "circuit_breaker"
                warnings.append(f"{sensor_name}_circuit_open")
                sensor_failures += 1
                snapshot_quality = "degraded"
            else:
                data, warn = self._execute_with_timeout(capture_fn, protected_context, default, sensor_name, key)
                snapshot[key] = data
                snapshot["source"][key] = "live" if not warn else "degraded"
                warnings.extend(warn)
                if warn:
                    sensor_failures += 1
                    snapshot_quality = "degraded"

        if len(warnings) > self.MAX_WARNINGS_PER_SESSION:
            warnings = warnings[:self.MAX_WARNINGS_PER_SESSION]

        elapsed_ms = (time.monotonic() - start_mono) * 1000
        if elapsed_ms > self.MAX_TOTAL_SNAPSHOT_SEC * 1000:
            logger.warning("快照采集超时: %.2fms", elapsed_ms)

        # 更新性能统计
        with self._metrics_lock:
            if len(self._latencies) >= self.METRICS_WINDOW:
                self._latency_sum -= self._latencies[0]
            self._latencies.append(elapsed_ms)
            self._latency_sum += elapsed_ms
            self._total_captures += 1
            self._last_latency_ms = elapsed_ms
            if self._latencies:
                self._avg_latency_ms = self._latency_sum / len(self._latencies)
            if sensor_failures > 0:
                self._failed_captures += 1
            self._cache_hit_rate = self._cache_hit_rate * self.CACHE_MISS_DECAY

        # 缓存策略
        is_degraded = (snapshot_quality != "full")
        cache_ttl = self.CACHE_TTL_DEGRADED_SEC if is_degraded else ttl
        with self._cache_lock:
            self._cached_snapshot = snapshot
            self._cached_symbol = symbol
            self._cached_context_hash = context_hash
            self._cached_mono_time = now_mono
            self._cached_is_degraded = is_degraded
            # 降级快照使用独立过期时间，通过调整缓存时间戳实现
            if is_degraded:
                self._cached_mono_time = now_mono - (ttl - cache_ttl)

        status = "degraded" if sensor_failures > 0 else "ok"
        reason = f"感官快照采集完成，耗时 {elapsed_ms:.2f}ms，{sensor_failures} 个感官降级"

        return {
            "status": status, "reason": reason,
            "data": snapshot, "warnings": warnings
        }

    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            deps = {
                name: getattr(self, f"_{name}_cortex", None) is not None
                for name in self.REQUIRED_FIELDS
            }
            sensor_health = {}
            for name in self.REQUIRED_FIELDS:
                cortex = getattr(self, f"_{name}_cortex", None)
                if cortex and hasattr(cortex, "health_check"):
                    try:
                        sensor_health[name] = cortex.health_check()
                    except Exception as e:
                        sensor_health[name] = {"status": "error", "message": str(e)}
                else:
                    sensor_health[name] = {"status": "unavailable"}

            with self._sensor_lock:
                circuit_states = {name: state.value for name, state in self._sensor_circuit_state.items()}

            with self._metrics_lock:
                metrics = {
                    "total_captures": self._total_captures,
                    "failed_captures": self._failed_captures,
                    "avg_latency_ms": round(self._avg_latency_ms, 3),
                    "last_latency_ms": round(self._last_latency_ms, 3),
                    "cache_hit_rate": round(self._cache_hit_rate, 3),
                }

            return {
                "status": "ok" if all(deps.values()) else "degraded",
                "reason": f"模块正常，{sum(deps.values())}/5 感官注入",
                "data": {
                    "dependencies": deps,
                    "sensor_health": sensor_health,
                    "circuit_states": circuit_states,
                    "metrics": metrics,
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"]}

    # ========== 私有方法 ==========
    def _extract_volatility(self, context: Dict[str, Any]) -> float:
        """提取波动率分位"""
        try:
            market_data = context.get("market_data")
            if not isinstance(market_data, dict):
                return 0.5
            vol = float(market_data.get("volatility_percentile", 0.5))
            return max(0.0, min(1.0, vol))
        except (TypeError, ValueError):
            return 0.5

    def _compute_context_hash(self, context: Dict[str, Any]) -> str:
        """计算上下文哈希（稳定且高效）"""
        try:
            subset = OrderedDict()
            for field in sorted(self.CONTEXT_HASH_FIELDS):
                if field in context and isinstance(context[field], dict):
                    subset[field] = _sort_dict_recursive(context[field], self.CONTEXT_HASH_DEPTH)
            if len(subset) < 3:
                return hashlib.sha256(symbol.encode()).hexdigest() if 'symbol' in dir() else str(time.monotonic())
            serialized = json.dumps(subset, sort_keys=True, default=str)
            return hashlib.sha256(serialized.encode()).hexdigest()
        except Exception as e:
            logger.debug("哈希计算失败: %s", e)
            return str(time.monotonic())

    def _circuit_allow(self, sensor_name: str) -> Tuple[CircuitState, bool]:
        """检查熔断器是否允许请求通过（需在 self._sensor_lock 内调用）"""
        state = self._sensor_circuit_state[sensor_name]
        now = time.monotonic()

        if state == CircuitState.OPEN:
            if now - self._sensor_circuit_since[sensor_name] > self.SENSOR_CIRCUIT_BREAK_TIMEOUT:
                self._sensor_circuit_state[sensor_name] = CircuitState.HALF_OPEN
                self._sensor_circuit_since[sensor_name] = now
                self._sensor_half_open_probe_count[sensor_name] = 0
                return CircuitState.HALF_OPEN, True
            return CircuitState.OPEN, False

        if state == CircuitState.HALF_OPEN:
            if self._sensor_half_open_probe_count[sensor_name] == 0:
                self._sensor_half_open_probe_count[sensor_name] += 1
                return CircuitState.HALF_OPEN, True
            return CircuitState.HALF_OPEN, False

        return CircuitState.CLOSED, True

    def _record_result(self, sensor_name: str, success: bool) -> None:
        """记录传感器调用结果，维护熔断状态（需在锁内调用）"""
        now = time.monotonic()
        state = self._sensor_circuit_state[sensor_name]

        if state == CircuitState.HALF_OPEN:
            if success:
                # 探测成功，恢复关闭并清空失败计数
                self._sensor_circuit_state[sensor_name] = CircuitState.CLOSED
                self._sensor_circuit_since[sensor_name] = now
                self._sensor_fail_count[sensor_name] = 0
                self._sensor_last_fail_time[sensor_name] = 0.0
                self._sensor_half_open_probe_count[sensor_name] = 0
                logger.info("感官 %s 半开探测成功，熔断恢复", sensor_name)
            else:
                # 探测失败，重新打开
                self._sensor_circuit_state[sensor_name] = CircuitState.OPEN
                self._sensor_circuit_since[sensor_name] = now
                self._sensor_half_open_probe_count[sensor_name] = 0
                logger.warning("感官 %s 半开探测失败，重新熔断", sensor_name)
            return

        if not success:
            self._sensor_fail_count[sensor_name] += 1
            self._sensor_last_fail_time[sensor_name] = now
            if self._sensor_fail_count[sensor_name] >= self.SENSOR_CIRCUIT_BREAK_THRESHOLD:
                self._sensor_circuit_state[sensor_name] = CircuitState.OPEN
                self._sensor_circuit_since[sensor_name] = now
                logger.error("感官 %s 连续失败 %d 次，触发熔断", sensor_name, self._sensor_fail_count[sensor_name])
        else:
            # CLOSED 状态下成功，重置失败计数
            self._sensor_fail_count[sensor_name] = 0
            self._sensor_last_fail_time[sensor_name] = 0.0

    def _execute_with_timeout(
        self, capture_fn: Callable[[Dict[str, Any]], Any], context: Dict[str, Any],
        default: MappingProxyType, sensor_name: str, key: str
    ) -> Tuple[Dict[str, Any], List[str]]:
        """带超时和字段校验的安全执行器"""
        timeout = self.SENSOR_TIMEOUT_OVERRIDES.get(sensor_name, self.MAX_SENSOR_RESPONSE_SEC)
        start = time.monotonic()
        try:
            res = capture_fn(context)
            elapsed = time.monotonic() - start
            if elapsed >= timeout:
                logger.warning("%s 响应超时: %.4fs (阈值 %.4fs)", sensor_name, elapsed, timeout)
                self._record_result(sensor_name, False)
                return dict(default), [f"{sensor_name}_timeout"]
            if not isinstance(res, dict):
                self._record_result(sensor_name, False)
                return dict(default), [f"{sensor_name}_invalid_format"]
            # 必需字段校验
            required: frozenset = self.REQUIRED_FIELDS.get(key, frozenset())
            missing = required - set(res.keys())
            if missing:
                logger.warning("%s 返回数据缺少字段: %s", sensor_name, missing)
                self._record_result(sensor_name, False)
                return dict(default), [f"{sensor_name}_missing_fields", f"missing:{','.join(missing)}"]
            self._record_result(sensor_name, True)
            # 返回浅拷贝，防止外部修改污染传感器原始数据
            return dict(res), []
        except Exception as e:
            logger.error("%s 异常: %s", sensor_name, e)
            self._record_result(sensor_name, False)
            return dict(default), [f"{sensor_name}_failed"]


# ========== 模块级辅助函数 ==========
def _get_sensor_method(name: str) -> str:
    """获取传感器对应的调用方法名"""
    return {
        "visual": "perceive", "auditory": "listen",
        "tactile": "sense", "olfactory": "sniff", "gustatory": "taste"
    }.get(name, "")


def _is_valid_symbol(symbol: str) -> bool:
    """验证交易对符号合法性（允许大小写字母、数字、-、_、/）"""
    return all(c.isalnum() or c in '-_/' for c in symbol)


def _shallow_copy_context(context: Dict[str, Any]) -> Dict[str, Any]:
    """对上下文进行浅拷贝，保护关键字段不被外部修改"""
    protected = {}
    for key in ("kline", "orderbook", "trade_stream", "system_state", "market_data", "last_trade"):
        if key in context:
            protected[key] = context[key]
    return protected


def _sort_dict_recursive(d: Any, depth: int) -> Any:
    """递归排序字典键，限制深度防止栈溢出，确保确定性输出"""
    if depth <= 0:
        if isinstance(d, dict):
            return str(sorted(d.items()))
        elif isinstance(d, list):
            return str([_sort_dict_recursive(i, 0) for i in d])
        return str(d)
    if isinstance(d, dict):
        return OrderedDict(sorted(
            (k, _sort_dict_recursive(v, depth - 1)) for k, v in d.items()
        ))
    if isinstance(d, list):
        return [_sort_dict_recursive(i, depth - 1) for i in d]
    return d
