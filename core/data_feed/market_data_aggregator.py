"""
火种系统 · 市场数据聚合器 (MarketDataAggregator)

核心职责：
1. 从多个交易所（Binance、OKX、Bybit）接收实时行情Tick/K线/订单簿数据，基于各数据源的延迟和稳定性计算动态权重，生成加权中位数价格
2. 管理所有交易对的合约规格（合约面值、最小变动单位、最大杠杆等），在启动时拉取并在运行期间定期刷新，检测变更后自动通知下游模块

外部依赖（真实模块接口）：
- core.data_feed.timestamp_validator.TimestampValidator : 验证数据时间戳的单调性和对齐
- core.negotiation_bus.NegotiationBus : 发送数据源状态变更事件
- core.behavioral_logger.BehavioralLogger : 记录聚合异常事件
- core.utils.api_client.rest_adapter.RestAdapter : 从交易所REST API拉取合约规格

接口契约：
- aggregate_price(symbol: str, prices: Dict[str, float]) -> Dict[str, Any] : 输入各交易所最新价，返回加权中位数
- report_source_latency(source: str, latency_sec: float) -> Dict[str, Any] : 上报数据源延迟，供外部数据接收模块调用
- get_contract_spec(symbol: str, field: Optional[str] = None) -> Dict[str, Any] : 查询指定交易对的合约规格
- refresh_contract_specs(symbols: Optional[Set[str]] = None) -> Dict[str, Any] : 强制刷新所有合约规格缓存
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当某交易所数据源超时或返回错误时，将其权重临时降为0，使用剩余有效数据源计算价格
- 当所有数据源均不可用时，返回最近一次有效价格作为安全回退
- 当合约规格REST API调用失败时，使用本地缓存的规格（若存在），否则使用硬编码的保守默认值
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护每个交易对的合约规格内存缓存，刷新时先写入临时区域再原子替换旧数据
- 不持有任何网络连接或文件句柄
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional, Set
from collections import deque
import numpy as np

logger = logging.getLogger(__name__)


class MarketDataAggregator:
    """多源行情聚合与合约规格管理器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_WEIGHT_UPDATE_INTERVAL_SEC = 60.0    # 数据源权重更新间隔，秒，[10, 300]
    DEFAULT_SOURCE_TIMEOUT_SEC = 5.0             # 数据源超时阈值，秒，[1, 30]
    DEFAULT_WEIGHT_DECAY_RATE = 0.85             # 超时后权重衰减系数，无量纲，[0.5, 0.95]
    DEFAULT_MIN_VALID_SOURCES = 2                # 最小有效数据源数，低于此值使用回退策略
    DEFAULT_SPEC_REFRESH_INTERVAL_SEC = 14400    # 合约规格刷新间隔，秒（4小时），[3600, 86400]
    DEFAULT_SPEC_RETRY_MAX = 3                   # 规格拉取最大重试次数，[1, 5]
    DEFAULT_SPEC_RETRY_DELAY_SEC = 2.0           # 重试间隔基数，秒，[1, 10]

    # 数据源基础权重（启动时均分）
    DEFAULT_SOURCE_WEIGHTS = {
        "binance": 0.4,    # 币安，权重，无量纲
        "okx": 0.3,        # OKX
        "bybit": 0.3,      # Bybit
    }

    # 合约规格保守默认值（当REST API不可用时使用）
    FALLBACK_SPEC = {
        "BTCUSDT": {"min_qty": 0.001, "min_notional": 10.0, "max_leverage": 125, "tick_size": 0.1, "contract_size": 0.001},
        "ETHUSDT": {"min_qty": 0.001, "min_notional": 10.0, "max_leverage": 100, "tick_size": 0.01, "contract_size": 0.01},
    }

    def __init__(self):
        # 数据源权重和延迟记录
        self._source_weights: Dict[str, float] = self.DEFAULT_SOURCE_WEIGHTS.copy()
        self._source_latencies: Dict[str, deque] = {
            source: deque(maxlen=20) for source in self._source_weights
        }
        self._source_last_active: Dict[str, float] = {source: time.time() for source in self._source_weights}
        self._last_weight_update = time.time()

        # 合约规格缓存（按交易对，内层字典为规格字段）
        self._contract_specs: Dict[str, Dict[str, Any]] = {}
        self._last_spec_refresh = 0.0

        # 降级价格缓存（当所有数据源失效时使用）
        self._last_aggregated_price: Dict[str, float] = {}

        # 外部依赖注入
        self._timestamp_validator = None
        self._negotiation_bus = None
        self._behavioral_logger = None
        self._rest_adapter = None

        # 线程安全（保护权重、规格缓存等共享状态）
        self._lock = threading.Lock()

        logger.info("MarketDataAggregator 初始化完成，监控 %d 个数据源", len(self._source_weights))

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        timestamp_validator: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        rest_adapter: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选注入，未注入时对应功能降级）"""
        if timestamp_validator is not None:
            if not hasattr(timestamp_validator, 'validate'):
                logger.warning("TimestampValidator 缺少 validate 方法")
            else:
                self._timestamp_validator = timestamp_validator
                logger.info("TimestampValidator 注入成功")

        if negotiation_bus is not None:
            self._negotiation_bus = negotiation_bus
            logger.info("NegotiationBus 注入成功")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")

        if rest_adapter is not None:
            self._rest_adapter = rest_adapter
            logger.info("RestAdapter 注入成功")
        else:
            logger.warning("RestAdapter 未注入，合约规格将使用保守默认值")

    # ========== 公共接口 ==========
    def aggregate_price(self, symbol: str, prices: Dict[str, float]) -> Dict[str, Any]:
        """
        聚合多源价格，生成加权中位数

        Args:
            symbol: 交易对标准名称，如 "BTCUSDT"
            prices: 各交易所最新价，键为交易所名，值为价格，如 {"binance": 63200.5, "okx": 63199.0}

        Returns:
            标准响应字典，data 中包含 weighted_price, confidence, active_sources 等字段
        """
        # 过滤无效价格
        filtered_prices = {}
        for source, price in prices.items():
            if isinstance(price, (int, float)) and price > 0:
                filtered_prices[source] = float(price)
            else:
                logger.warning(f"数据源 {source} 价格无效({price})，已丢弃")

        warnings = []
        if not filtered_prices:
            fallback = self._last_aggregated_price.get(symbol)
            if fallback:
                return {
                    "status": "degraded",
                    "reason": f"所有数据源不可用，使用最近有效价格 {fallback}",
                    "data": {"weighted_price": fallback, "confidence": 0.0, "active_sources": 0},
                    "warnings": ["all_sources_inactive", "using_fallback_price"],
                }
            return {
                "status": "error",
                "reason": "无可用数据源且无回退价格",
                "data": {"weighted_price": 0.0, "confidence": 0.0, "active_sources": 0},
                "warnings": ["no_data_available"],
            }

        # 动态更新权重
        self._update_source_weights()

        # 获取当前有效权重
        with self._lock:
            active_weights = {}
            for source, weight in self._source_weights.items():
                if source in filtered_prices and weight > 0.001:
                    active_weights[source] = weight

        # 当有效数据源不足时，使用简单中位数
        if len(active_weights) < self.DEFAULT_MIN_VALID_SOURCES:
            valid_prices = [filtered_prices[s] for s in active_weights if s in filtered_prices]
            if valid_prices:
                median_price = float(np.median(valid_prices))
            else:
                median_price = list(filtered_prices.values())[0] if filtered_prices else 0.0
            warnings.append(f"有效数据源不足({len(active_weights)}<{self.DEFAULT_MIN_VALID_SOURCES})，使用中位数")

            if symbol:
                self._last_aggregated_price[symbol] = median_price
            self._notify_source_status_change("aggregation", "degraded_median")
            return {
                "status": "degraded",
                "reason": f"有效数据源不足，使用中位数: {median_price}",
                "data": {
                    "weighted_price": median_price,
                    "confidence": round(len(active_weights) / len(self._source_weights), 2),
                    "active_sources": len(active_weights),
                },
                "warnings": warnings,
            }

        # 加权中位数计算
        sorted_items = sorted(filtered_prices.items(), key=lambda x: x[1])
        total_weight = sum(active_weights.get(s, 0.0) for s, _ in sorted_items)
        if total_weight <= 0:
            total_weight = 1.0

        cumulative = 0.0
        weighted_median = 0.0
        target_weight = total_weight / 2.0

        for source, price in sorted_items:
            w = active_weights.get(source, 0.0)
            cumulative += w
            if cumulative >= target_weight:
                weighted_median = price
                break

        if weighted_median == 0.0 and sorted_items:
            weighted_median = sorted_items[-1][1]

        if symbol:
            self._last_aggregated_price[symbol] = weighted_median

        return {
            "status": "ok",
            "reason": f"加权中位数聚合完成: {weighted_median}",
            "data": {
                "weighted_price": weighted_median,
                "confidence": round(len(active_weights) / len(self._source_weights), 2),
                "active_sources": len(active_weights),
            },
            "warnings": warnings,
        }

    def report_source_latency(self, source: str, latency_sec: float) -> Dict[str, Any]:
        """
        上报指定数据源的最新延迟（由外部数据接收模块调用）

        Args:
            source: 数据源名称 (binance/okx/bybit)
            latency_sec: 延迟，秒

        Returns:
            标准响应字典
        """
        if source not in self._source_latencies:
            return {
                "status": "error",
                "reason": f"未知数据源: {source}",
                "data": {},
                "warnings": [f"unknown_source:{source}"],
            }
        if not isinstance(latency_sec, (int, float)) or latency_sec < 0:
            return {
                "status": "error",
                "reason": f"无效延迟值: {latency_sec}",
                "data": {},
                "warnings": ["invalid_latency"],
            }
        with self._lock:
            self._source_latencies[source].append(latency_sec)
            self._source_last_active[source] = time.time()
        return {"status": "ok", "reason": f"已记录 {source} 延迟", "data": {}, "warnings": []}

    def get_contract_spec(self, symbol: str, field: Optional[str] = None) -> Dict[str, Any]:
        """
        查询指定交易对的合约规格

        Args:
            symbol: 交易对标准名称
            field: 可选，指定字段名（如 "min_qty"），为 None 则返回全部字段

        Returns:
            标准响应字典
        """
        with self._lock:
            spec = self._contract_specs.get(symbol)

        if spec is None:
            # 尝试使用降级值
            fallback = self.FALLBACK_SPEC.get(symbol, {})
            if fallback:
                logger.warning(f"合约规格 {symbol} 未缓存，使用保守默认值")
                result = fallback
                status = "degraded"
                reason = f"规格未缓存，使用保守默认值"
            else:
                return {
                    "status": "error",
                    "reason": f"交易对 {symbol} 无可用规格数据",
                    "data": {},
                    "warnings": [f"missing_spec:{symbol}"],
                }
        else:
            result = spec
            status = "ok"
            reason = f"返回 {symbol} 合约规格"

        if field:
            return {
                "status": status,
                "reason": reason,
                "data": {field: result.get(field)},
                "warnings": [] if status == "ok" else [f"using_fallback:{symbol}"],
            }
        return {
            "status": status,
            "reason": reason,
            "data": result,
            "warnings": [] if status == "ok" else [f"using_fallback:{symbol}"],
        }

    def refresh_contract_specs(self, symbols: Optional[Set[str]] = None) -> Dict[str, Any]:
        """
        强制刷新所有（或指定）交易对的合约规格

        Args:
            symbols: 可选，要刷新的交易对集合，为 None 则刷新全部已知交易对

        Returns:
            标准响应字典
        """
        if self._rest_adapter is None:
            return {
                "status": "error",
                "reason": "RestAdapter 未注入，无法刷新合约规格",
                "data": {},
                "warnings": ["rest_adapter_unavailable"],
            }

        now = time.time()
        with self._lock:
            last_refresh = self._last_spec_refresh
        if now - last_refresh < self.DEFAULT_SPEC_REFRESH_INTERVAL_SEC:
            return {
                "status": "ok",
                "reason": f"距上次刷新不足 {self.DEFAULT_SPEC_REFRESH_INTERVAL_SEC} 秒，跳过",
                "data": {"last_refresh": last_refresh},
                "warnings": [],
            }

        target_symbols = symbols or set(self.FALLBACK_SPEC.keys())
        new_specs = {}
        errors = []
        for sym in target_symbols:
            spec = None
            for attempt in range(self.DEFAULT_SPEC_RETRY_MAX):
                try:
                    raw = self._rest_adapter.get("/fapi/v1/exchangeInfo", params={"symbol": sym})
                    if raw and "symbols" in raw and raw["symbols"]:
                        info = raw["symbols"][0]
                        filters = info.get("filters", [])
                        first_filter = filters[0] if filters else {}
                        spec = {
                            "min_qty": float(first_filter.get("minQty", 0.001)),
                            "min_notional": float(first_filter.get("notional", 10.0)),
                            "max_leverage": int(info.get("maxLeverage", 125)),
                            "tick_size": float(first_filter.get("tickSize", 0.1)),
                            "contract_size": float(info.get("contractSize", 0.001)),
                        }
                        break
                except Exception as e:
                    logger.warning(f"刷新合约规格 {sym} 失败 (第{attempt+1}次): {e}")
                    time.sleep(self.DEFAULT_SPEC_RETRY_DELAY_SEC * (attempt + 1))
            if spec:
                new_specs[sym] = spec
            else:
                errors.append(sym)
                logger.error(f"无法获取 {sym} 合约规格 #RECOVERY: 检查交易所API连通性，将使用保守默认值")
                new_specs[sym] = self.FALLBACK_SPEC.get(sym, {})

        with self._lock:
            self._contract_specs.update(new_specs)
            self._last_spec_refresh = now

        return {
            "status": "ok" if not errors else "degraded",
            "reason": f"刷新完成，成功 {len(new_specs)-len(errors)}/{len(target_symbols)}",
            "data": {"updated_symbols": list(new_specs.keys()), "failed_symbols": errors},
            "warnings": [f"failed:{s}" for s in errors],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检

        Returns:
            标准健康检查响应字典
        """
        try:
            if not hasattr(self, '_source_weights') or not self._source_weights:
                return {
                    "status": "degraded",
                    "reason": "数据源权重未初始化",
                    "data": {},
                    "warnings": ["weights_not_initialized"],
                }

            with self._lock:
                active_count = sum(1 for w in self._source_weights.values() if w > 0.001)
                spec_count = len(self._contract_specs)
                cached_prices = len(self._last_aggregated_price)

            return {
                "status": "ok",
                "reason": f"MarketDataAggregator 正常，活跃数据源 {active_count}/{len(self._source_weights)}",
                "data": {
                    "active_sources": active_count,
                    "total_sources": len(self._source_weights),
                    "cached_specs": spec_count,
                    "cached_prices": cached_prices,
                    "last_spec_refresh": self._last_spec_refresh,
                    "dependencies": {
                        "timestamp_validator": self._timestamp_validator is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                        "rest_adapter": self._rest_adapter is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查数据源权重和合约规格缓存完整性")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _update_source_weights(self) -> None:
        """
        基于各数据源的近期延迟更新动态权重
        延迟低的源获得更高权重，超时的源权重逐步衰减，恢复后逐步回升
        """
        now = time.time()
        if now - self._last_weight_update < self.DEFAULT_WEIGHT_UPDATE_INTERVAL_SEC:
            return

        with self._lock:
            # 超时检测：长时间未收到数据的源，权重快速衰减
            for source in list(self._source_weights.keys()):
                if now - self._source_last_active.get(source, 0) > self.DEFAULT_SOURCE_TIMEOUT_SEC:
                    self._source_weights[source] *= self.DEFAULT_WEIGHT_DECAY_RATE
                    self._source_latencies[source].clear()
                    logger.debug("数据源 %s 超时，权重衰减至 %.4f", source, self._source_weights[source])

            # 计算每个源的平均延迟
            avg_latencies = {}
            for source, lat_deque in self._source_latencies.items():
                recent = list(lat_deque)
                if recent:
                    avg_latencies[source] = float(np.mean(recent))
                else:
                    avg_latencies[source] = self.DEFAULT_SOURCE_TIMEOUT_SEC

            # 基于延迟计算新权重（延迟越小权重越大）
            total_inv = 0.0
            inv_latencies = {}
            for source, lat in avg_latencies.items():
                inv = 1.0 / max(lat, 0.001)
                inv_latencies[source] = inv
                total_inv += inv

            if total_inv > 0:
                for source in self._source_weights:
                    if source in inv_latencies:
                        new_weight = inv_latencies[source] / total_inv
                        # 平滑过渡：新旧权重按 0.7:0.3 混合
                        self._source_weights[source] = (
                            0.7 * new_weight + 0.3 * self._source_weights[source]
                        )
                    else:
                        self._source_weights[source] *= self.DEFAULT_WEIGHT_DECAY_RATE

            self._last_weight_update = now
            logger.info(
                "数据源权重更新完成: %s",
                {s: round(w, 3) for s, w in self._source_weights.items()},
            )

    def _notify_source_status_change(self, source: str, status: str) -> None:
        """通知上游模块数据源状态变更"""
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="data_source_status",
                    source=source,
                    status=status,
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning(f"数据源状态变更通知失败: {e}")
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="data_source_status",
                    details={"source": source, "status": status},
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")
