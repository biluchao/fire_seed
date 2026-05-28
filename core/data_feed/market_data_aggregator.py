"""
火种系统 · 市场数据聚合器 (MarketDataAggregator)

核心职责：
1. 从多个交易所（Binance、OKX、Bybit）接收实时行情，基于延迟、稳定性、盘口深度和价格一致性动态计算数据源权重，生成盘口深度加权的聚合价格
2. 提供按交易方向和订单量计算的可执行VWAP价格，自动选择真实成交成本最低的交易所，供策略引擎直接用于下单决策
3. 管理所有交易对的合约规格（合约面值、最小变动单位、最大杠杆等），启动时拉取并在运行期间定期刷新，检测变更后自动通知下游模块

外部依赖（真实模块接口）：
- core.data_feed.timestamp_validator.TimestampValidator : 验证数据时间戳的单调性和对齐
- core.negotiation_bus.NegotiationBus : 发送数据源状态变更事件
- core.behavioral_logger.BehavioralLogger : 记录聚合异常事件
- core.utils.api_client.rest_adapter.RestAdapter : 从交易所REST API拉取合约规格

接口契约：
- aggregate_price(symbol: str, prices: Dict[str, Any], orderbooks: Optional[Dict[str, Any]] = None) -> Dict[str, Any] : 返回盘口深度加权的聚合价格及置信度
- aggregate_executable_price(symbol: str, direction: int, order_size: float, prices: Dict[str, Any], orderbooks: Dict[str, Any]) -> Dict[str, Any] : 返回给定订单量的真实VWAP成本及各交易所对比
- check_slippage_risk(symbol: str, aggregated_price: float, orderbooks: Dict[str, Any]) -> Dict[str, Any] : 检测聚合价与盘口偏差(已整合进聚合流程)
- report_source_latency(source: str, latency_sec: float, price_deviation: Optional[float] = None) -> Dict[str, Any] : 上报数据源延迟及价格偏差
- get_contract_spec(symbol: str, field: Optional[str] = None) -> Dict[str, Any] : 查询合约规格
- refresh_contract_specs(symbols: Optional[Set[str]] = None) -> Dict[str, Any] : 强制刷新合约规格缓存
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 某数据源价格跳变过大时立即隔离，权重置零并告警
- 所有数据源不可用时，返回最近一次有效价格作为安全回退
- 合约规格REST API调用失败时，使用本地缓存（若存在），否则使用硬编码保守默认值
- 降级值在类常量区明确声明

资源管理：
- 合约规格缓存刷新时，先写入临时区域再原子替换旧数据
- 不持有任何网络连接或文件句柄
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional, Set, Tuple, Union
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
    MAX_TIMESTAMP_DEVIATION_MS = 2.0             # 最大时间戳偏差，毫秒，[1, 10]
    PRICE_CONSISTENCY_PENALTY_FACTOR = 0.7       # 价格一致性惩罚系数，无量纲，[0.5, 0.95]
    MAX_PRICE_DEVIATION_FOR_CONSISTENCY = 0.005  # 触发一致性惩罚的价格偏差阈值，无量纲，[0.001, 0.01]
    MAX_PRICE_JUMP_RATIO = 0.05                  # 单Tick价格跳变阈值，无量纲，[0.01, 0.1]
    DEFAULT_ORDERBOOK_DEPTH_WEIGHT_POWER = 0.5   # 盘口深度权重指数，<1压缩极值，[0.2, 1.0]
    EVEN_SOURCE_AVERAGING = True                 # 偶数数据源时取中间两价的加权平均

    # 数据源基础权重
    DEFAULT_SOURCE_WEIGHTS = {
        "binance": 0.4,
        "okx": 0.3,
        "bybit": 0.3,
    }

    # 交易所吃单手续费率
    FEE_RATES = {
        "binance": 0.0004,   # 0.04%
        "okx": 0.0005,       # 0.05%
        "bybit": 0.0004,     # 0.04%
    }

    # 合约规格保守默认值
    FALLBACK_SPEC = {
        "BTCUSDT": {"min_qty": 0.001, "min_notional": 10.0, "max_leverage": 125, "tick_size": 0.1, "contract_size": 0.001},
        "ETHUSDT": {"min_qty": 0.001, "min_notional": 10.0, "max_leverage": 100, "tick_size": 0.01, "contract_size": 0.01},
    }

    def __init__(self):
        self._source_weights: Dict[str, float] = self.DEFAULT_SOURCE_WEIGHTS.copy()
        self._source_latencies: Dict[str, deque] = {
            source: deque(maxlen=20) for source in self._source_weights
        }
        self._source_last_active: Dict[str, float] = {source: time.time() for source in self._source_weights}
        self._source_price_deviation: Dict[str, deque] = {
            source: deque(maxlen=20) for source in self._source_weights
        }
        self._last_weight_update = time.time()

        # 存储各数据源的前一次价格，用于跳变检测
        self._previous_prices: Dict[str, float] = {}

        self._contract_specs: Dict[str, Dict[str, Any]] = {}
        self._last_spec_refresh = 0.0

        self._last_aggregated_price: Dict[str, float] = {}
        # 存储各数据源最近一次的价格和盘口摘要，用于联合预警
        self._source_last_price: Dict[str, float] = {}
        self._source_last_spread: Dict[str, float] = {}

        self._timestamp_validator = None
        self._negotiation_bus = None
        self._behavioral_logger = None
        self._rest_adapter = None

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

    # ========== 价格聚合 ==========
    def aggregate_price(self, symbol: str, prices: Dict[str, Any],
                        orderbooks: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        聚合多源价格，生成加权中位数（支持盘口深度加权）

        Args:
            symbol: 交易对标准名称
            prices: 各交易所最新价，格式为 {source: float} 或 {source: {"price": float, "ts": float}}
            orderbooks: 可选，各交易所盘口快照，用于深度加权和滑点风险检测

        Returns:
            标准响应字典，包含 weighted_price, confidence, active_sources，高风险时 confidence 降为 0
        """
        warnings = []
        # 提取价格并校验时间戳、跳变检测
        extracted = {}
        now = time.time()
        for source, data in prices.items():
            price = None
            ts = now
            if isinstance(data, (int, float)) and data > 0:
                price = float(data)
                logger.warning(f"数据源 {source} 未提供时间戳，使用本地时间")
            elif isinstance(data, dict) and "price" in data:
                price = data.get("price")
                ts = data.get("ts", now)
                if not (isinstance(price, (int, float)) and price > 0):
                    logger.warning(f"数据源 {source} 价格无效({price})，已丢弃")
                    continue
                price = float(price)
            else:
                logger.warning(f"数据源 {source} 数据格式无效，已丢弃")
                continue

            # 价格跳变检测
            if source in self._previous_prices and self._previous_prices[source] > 0:
                jump = abs(price - self._previous_prices[source]) / self._previous_prices[source]
                if jump > self.MAX_PRICE_JUMP_RATIO:
                    logger.error(f"数据源 {source} 价格跳变 {jump*100:.2f}%，已隔离 #RECOVERY: 检查该交易所行情接口")
                    warnings.append(f"price_jump:{source}")
                    self._isolate_source(source)
                    continue
            self._previous_prices[source] = price
            extracted[source] = (price, ts)

        if not extracted:
            fallback = self._last_aggregated_price.get(symbol)
            if fallback:
                return {
                    "status": "degraded",
                    "reason": f"所有数据源不可用，使用最近有效价格 {fallback}",
                    "data": {"weighted_price": fallback, "confidence": 0.0, "active_sources": 0},
                    "warnings": warnings + ["all_sources_inactive", "using_fallback_price"],
                }
            return {
                "status": "error",
                "reason": "无可用数据源且无回退价格",
                "data": {"weighted_price": 0.0, "confidence": 0.0, "active_sources": 0},
                "warnings": warnings + ["no_data_available"],
            }

        # 时间戳对齐过滤
        if len(extracted) >= 2:
            ts_values = [ts for _, ts in extracted.values()]
            ts_median = sorted(ts_values)[len(ts_values) // 2]
            for source in list(extracted.keys()):
                _, ts = extracted[source]
                if abs(ts - ts_median) * 1000 > self.MAX_TIMESTAMP_DEVIATION_MS:
                    logger.warning(f"数据源 {source} 时间戳偏差过大({abs(ts-ts_median)*1000:.1f}ms)，已隔离")
                    extracted.pop(source)
                    warnings.append(f"timestamp_deviation:{source}")

        # 转为纯价格字典
        filtered_prices = {s: p for s, (p, _) in extracted.items()}

        # 权重更新（传入价格用于一致性惩罚）
        self._update_source_weights(filtered_prices)

        with self._lock:
            active_weights = {}
            for source, weight in self._source_weights.items():
                if source in filtered_prices and weight > 0.001:
                    active_weights[source] = weight

        # 滑点风险检测（若提供了盘口，在聚合过程中完成）
        confidence_mult = 1.0
        if orderbooks:
            risk_check = self.check_slippage_risk(symbol, 0.0, orderbooks)  # 先不传聚合价
            if risk_check["data"]["risk"] == "high":
                confidence_mult = 0.0
                warnings.append("high_slippage_risk:confidence_zero")
                logger.error(f"滑点风险极高，聚合价格置信度降为零")
            elif risk_check["data"]["risk"] == "moderate":
                confidence_mult = 0.5
                warnings.append("moderate_slippage_risk:confidence_half")

        # 盘口深度加权修正
        if orderbooks:
            depth_weights = self._compute_depth_weights(filtered_prices, orderbooks)
            # 将深度权重与原有权重融合
            for source in active_weights:
                if source in depth_weights:
                    active_weights[source] = active_weights[source] * 0.5 + depth_weights[source] * 0.5
            # 重新归一化
            total = sum(active_weights.values())
            if total > 0:
                for source in active_weights:
                    active_weights[source] /= total

        # 计算加权中位数（偶数源时取平均）
        sorted_items = sorted(filtered_prices.items(), key=lambda x: x[1])
        total_weight = sum(active_weights.get(s, 0.0) for s, _ in sorted_items)
        if total_weight <= 0.0001:
            total_weight = 1.0

        cumulative = 0.0
        target_weight = total_weight / 2.0
        weighted_median = 0.0
        prev_price = None
        prev_cum = 0.0
        for source, price in sorted_items:
            w = active_weights.get(source, 0.0)
            prev_cum = cumulative
            cumulative += w
            if cumulative >= target_weight:
                if self.EVEN_SOURCE_AVERAGING and prev_price is not None and prev_cum > 0 and abs(prev_cum - target_weight) < 0.0001:
                    # 偶数个源，取中间两个价格的加权平均
                    weighted_median = (price * (cumulative - target_weight) + prev_price * (target_weight - prev_cum)) / w if w > 0 else price
                else:
                    weighted_median = price
                break
            prev_price = price
        if weighted_median == 0.0 and sorted_items:
            weighted_median = sorted_items[-1][1]

        if symbol:
            self._last_aggregated_price[symbol] = weighted_median

        base_confidence = round(len(active_weights) / len(self._source_weights), 2)
        final_confidence = base_confidence * confidence_mult

        return {
            "status": "ok",
            "reason": f"聚合完成: {weighted_median}",
            "data": {
                "weighted_price": weighted_median,
                "confidence": final_confidence,
                "active_sources": len(active_weights),
                "risk_adjusted": confidence_mult < 1.0,
            },
            "warnings": warnings,
        }

    def aggregate_executable_price(self, symbol: str, direction: int, order_size: float,
                                   prices: Dict[str, Any], orderbooks: Dict[str, Any]) -> Dict[str, Any]:
        """
        计算给定订单量的真实VWAP成交成本，并推荐最优交易所

        Args:
            symbol: 交易对
            direction: 1=买入, -1=卖出
            order_size: 目标成交量（以基础资产计）
            prices: 各交易所中间价
            orderbooks: 各交易所盘口快照

        Returns:
            各交易所真实VWAP、滑点、总成本（含手续费）及推荐
        """
        results = {}
        with self._lock:
            weights = self._source_weights.copy()
        for source, ob in orderbooks.items():
            if source not in weights or weights[source] < 0.01:
                continue
            levels = ob.get("asks") if direction == 1 else ob.get("bids")
            if not levels:
                results[source] = None
                continue
            # 逐档穿透计算VWAP
            remaining = order_size
            total_cost = 0.0
            filled = 0.0
            for price, volume in levels:
                take = min(remaining, volume)
                total_cost += take * price
                filled += take
                remaining -= take
                if remaining <= 0:
                    break
            if filled == 0:
                results[source] = None
                continue
            vwap = total_cost / filled
            mid_price = prices.get(source, levels[0][0])
            slippage_bps = abs(vwap / mid_price - 1) * 10000
            net_vwap = self.get_net_executable_price(source, direction, vwap)
            results[source] = {
                "mid_price": mid_price,
                "best_price": levels[0][0],
                "vwap": vwap,
                "filled_qty": filled,
                "estimated_slippage_bps": round(slippage_bps, 2),
                "net_vwap": net_vwap,
                "total_cost_inc_fee": net_vwap * filled if direction == 1 else -net_vwap * filled,
            }

        valid = [(s, r) for s, r in results.items() if r is not None]
        # 按净VWAP排序（买入取最低，卖出取最高）
        if direction == 1:
            valid.sort(key=lambda x: x[1]["net_vwap"])
        else:
            valid.sort(key=lambda x: x[1]["net_vwap"], reverse=True)
        recommended = valid[0][0] if valid else None
        return {
            "status": "ok",
            "reason": f"最优执行交易所: {recommended}" if recommended else "无可执行交易所",
            "data": {
                "recommended": recommended,
                "per_exchange": {s: r for s, r in valid},
                "direction": "buy" if direction == 1 else "sell",
                "order_size": order_size,
            },
            "warnings": [] if valid else ["no_executable_exchange"],
        }

    def check_slippage_risk(self, symbol: str, aggregated_price: float,
                            orderbooks: Dict[str, Any]) -> Dict[str, Any]:
        """检测聚合价与各交易所盘口最优价的偏差风险"""
        max_deviation = 0.0
        worst_source = None
        for source, ob in orderbooks.items():
            if not ob.get("bids") or not ob.get("asks"):
                continue
            mid = (ob["bids"][0][0] + ob["asks"][0][0]) / 2
            if aggregated_price > 0:
                deviation = abs(aggregated_price - mid) / mid
            else:
                deviation = abs(ob["asks"][0][0] - ob["bids"][0][0]) / mid
            if deviation > max_deviation:
                max_deviation = deviation
                worst_source = source

        risk = "low"
        if max_deviation > 0.002:
            risk = "high"
        elif max_deviation > 0.001:
            risk = "moderate"

        if risk != "low":
            logger.warning(f"滑点风险[{risk}]: 最大偏差 {max_deviation*100:.2f}% (源:{worst_source})")

        return {
            "status": "ok",
            "reason": f"最大偏差 {max_deviation*100:.2f}%，风险等级: {risk}",
            "data": {
                "max_deviation_pct": round(max_deviation * 100, 3),
                "risk": risk,
                "worst_source": worst_source,
            },
            "warnings": [f"slippage_risk:{risk}"] if risk != "low" else [],
        }

    def get_net_executable_price(self, source: str, direction: int, gross_price: float) -> float:
        """计算扣除手续费后的净价格"""
        rate = self.FEE_RATES.get(source, 0.0005)
        if direction == 1:
            return gross_price * (1 + rate)
        else:
            return gross_price * (1 - rate)

    # ========== 数据源延迟上报（含价格偏差） ==========
    def report_source_latency(self, source: str, latency_sec: float,
                              price_deviation: Optional[float] = None) -> Dict[str, Any]:
        if source not in self._source_latencies:
            return {"status": "error", "reason": f"未知数据源: {source}", "data": {}, "warnings": [f"unknown_source:{source}"]}
        if not isinstance(latency_sec, (int, float)) or latency_sec < 0:
            return {"status": "error", "reason": f"无效延迟值: {latency_sec}", "data": {}, "warnings": ["invalid_latency"]}
        with self._lock:
            self._source_latencies[source].append(latency_sec)
            self._source_last_active[source] = time.time()
            if price_deviation is not None:
                self._source_price_deviation[source].append(price_deviation)
        # 延迟-偏离联合预警
        if price_deviation is not None:
            recent_lat = list(self._source_latencies[source])[-5:]
            recent_dev = list(self._source_price_deviation[source])[-5:]
            if len(recent_lat) >= 3 and len(recent_dev) >= 3:
                avg_lat = np.mean(recent_lat)
                avg_dev = np.mean(recent_dev)
                if avg_lat > 0.05 and avg_dev > 0.002:
                    logger.warning(f"数据源 {source} 延迟({avg_lat*1000:.1f}ms)与价格偏差({avg_dev*100:.2f}%)同时恶化")
        return {"status": "ok", "reason": f"已记录 {source} 延迟", "data": {}, "warnings": []}

    # ========== 合约规格 ==========
    def get_contract_spec(self, symbol: str, field: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            spec = self._contract_specs.get(symbol)
        if spec is None:
            fallback = self.FALLBACK_SPEC.get(symbol, {})
            if fallback:
                logger.warning(f"合约规格 {symbol} 未缓存，使用保守默认值")
                result = fallback
                status = "degraded"
                reason = "规格未缓存，使用保守默认值"
            else:
                return {"status": "error", "reason": f"交易对 {symbol} 无可用规格数据", "data": {}, "warnings": [f"missing_spec:{symbol}"]}
        else:
            result = spec
            status = "ok"
            reason = f"返回 {symbol} 合约规格"

        if field:
            return {"status": status, "reason": reason, "data": {field: result.get(field)}, "warnings": [] if status == "ok" else [f"using_fallback:{symbol}"]}
        return {"status": status, "reason": reason, "data": result, "warnings": [] if status == "ok" else [f"using_fallback:{symbol}"]}

    def refresh_contract_specs(self, symbols: Optional[Set[str]] = None) -> Dict[str, Any]:
        if self._rest_adapter is None:
            return {"status": "error", "reason": "RestAdapter 未注入，无法刷新合约规格", "data": {}, "warnings": ["rest_adapter_unavailable"]}

        now = time.time()
        with self._lock:
            last_refresh = self._last_spec_refresh
        if now - last_refresh < self.DEFAULT_SPEC_REFRESH_INTERVAL_SEC:
            return {"status": "ok", "reason": f"距上次刷新不足 {self.DEFAULT_SPEC_REFRESH_INTERVAL_SEC} 秒，跳过", "data": {"last_refresh": last_refresh}, "warnings": []}

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
        try:
            if not hasattr(self, '_source_weights') or not self._source_weights:
                return {"status": "degraded", "reason": "数据源权重未初始化", "data": {}, "warnings": ["weights_not_initialized"]}
            with self._lock:
                active_count = sum(1 for w in self._source_weights.values() if w > 0.001)
                spec_count = len(self._contract_specs)
                cached_prices = len(self._last_aggregated_price)
            return {
                "status": "ok",
                "reason": f"MarketDataAggregator 正常，活跃数据源 {active_count}/{len(self._source_weights)}",
                "data": {
                    "active_sources": active_count, "total_sources": len(self._source_weights),
                    "cached_specs": spec_count, "cached_prices": cached_prices,
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
            return {"status": "error", "reason": f"健康检查异常: {str(e)}", "data": {}, "warnings": [f"health_check_failed: {str(e)}"]}

    # ========== 私有方法 ==========
    def _compute_depth_weights(self, prices: Dict[str, float],
                               orderbooks: Dict[str, Any]) -> Dict[str, float]:
        """基于盘口深度计算数据源权重（深度越大越可靠）"""
        depths = {}
        for source, ob in orderbooks.items():
            if source not in prices:
                continue
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])
            total_depth = sum(v for _, v in bids[:5]) + sum(v for _, v in asks[:5])
            depths[source] = max(1.0, total_depth)
        if not depths:
            return {s: 1.0 for s in prices}
        # 使用幂变换压缩极值
        max_depth = max(depths.values())
        weights = {}
        for source, d in depths.items():
            normalized = d / max_depth
            weights[source] = normalized ** self.DEFAULT_ORDERBOOK_DEPTH_WEIGHT_POWER
        # 归一化
        total = sum(weights.values())
        if total > 0:
            for source in weights:
                weights[source] /= total
        return weights

    def _update_source_weights(self, prices: Optional[Dict[str, float]] = None) -> None:
        now = time.time()
        if now - self._last_weight_update < self.DEFAULT_WEIGHT_UPDATE_INTERVAL_SEC:
            return

        with self._lock:
            # 超时衰减
            for source in list(self._source_weights.keys()):
                if now - self._source_last_active.get(source, 0) > self.DEFAULT_SOURCE_TIMEOUT_SEC:
                    self._source_weights[source] *= self.DEFAULT_WEIGHT_DECAY_RATE
                    self._source_latencies[source].clear()
                    self._source_price_deviation[source].clear()

            # 延迟反比权重
            avg_latencies = {}
            for source, lat_deque in self._source_latencies.items():
                recent = list(lat_deque)
                avg_latencies[source] = float(np.mean(recent)) if recent else self.DEFAULT_SOURCE_TIMEOUT_SEC

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
                        self._source_weights[source] = 0.7 * new_weight + 0.3 * self._source_weights[source]

            # 价格一致性惩罚
            if prices and len(prices) >= 2:
                prices_list = list(prices.values())
                median = sorted(prices_list)[len(prices_list) // 2]
                for source, price in prices.items():
                    if price <= 0:
                        continue
                    deviation = abs(price - median) / median
                    self._source_price_deviation[source].append(deviation)
                    avg_dev = np.mean(list(self._source_price_deviation[source])) if self._source_price_deviation[source] else 0
                    if avg_dev > self.MAX_PRICE_DEVIATION_FOR_CONSISTENCY:
                        self._source_weights[source] *= self.PRICE_CONSISTENCY_PENALTY_FACTOR
                        logger.debug(f"数据源 {source} 价格一致性差(avg_dev={avg_dev:.4f})，权重惩罚")

            self._last_weight_update = now
            logger.info("数据源权重更新: %s", {s: round(w, 4) for s, w in self._source_weights.items()})

    def _isolate_source(self, source: str) -> None:
        """隔离异常数据源（权重置零）"""
        with self._lock:
            if source in self._source_weights:
                self._source_weights[source] = 0.0
                self._source_latencies[source].clear()
                self._source_price_deviation[source].clear()
            logger.warning(f"数据源 {source} 已被隔离")
        self._notify_source_status_change(source, "isolated")

    def _notify_source_status_change(self, source: str, status: str) -> None:
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(alert_type="data_source_status", source=source, status=status, timestamp=time.time())
            except Exception as e:
                logger.warning(f"数据源状态变更通知失败: {e}")
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(event_type="data_source_status", details={"source": source, "status": status})
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")
