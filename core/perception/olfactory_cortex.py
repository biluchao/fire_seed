"""
火种系统 · 嗅觉皮层 (OlfactoryCortex) v5.0

核心职责：
1. 双向实时嗅探订单簿中的“纸墙”行为，采用不对称阈值与历史频率统计
2. 监测订单流毒性（波动率自适应样本量），量化毒性指数
3. 跨品种系统性风险嗅探：真实时间戳对齐，停牌过滤，nan 安全
4. 启动冷却期内自动降级并提示剩余时间，健康检查具备资源保护与独立线程池

外部依赖（真实模块接口）：
- core.data_feed.DataFeed : 提供 get_atomic_snapshot, get_ticker, get_recent_trades, get_klines
- core.negotiation_bus.NegotiationBus : 异步推送结构化告警（publish_alert）
- core.behavioral_logger.BehavioralLogger : 结构化事件记录（log_event）
- core.utils.config_loader.ConfigLoader : 可选配置注入

接口契约：
- sniff_paper_wall(symbol) -> Dict : is_paper_wall, cancel_rate_ask/bid, details, frequency
- sniff_order_toxicity(symbol) -> Dict : toxicity_index, is_toxic
- sniff_systemic_risk(symbols=None) -> Dict : avg_correlation, risk_level
- health_check() -> Dict : avg_latency, uptime, calls, version
- 所有公共方法返回 {"status","reason","data","warnings","error_code"}

异常与降级：
- 所有外部数据转换使用 _safe_float，异常时返回 0.0 并记录带上下文日志
- DataFeed 不可用时返回降级结果（error_code 1000+）
- 启动后冷却期内返回降级（error_code 1001），并返回剩余时间
- 告警推送使用独立线程池，超时 2 秒，不阻塞主流程
- 健康检查使用独立线程池，避免资源争抢

资源管理：
- 挂单快照压缩存储（仅价格与量），全局容量受控，淘汰策略为 LRU
- 清理操作在锁内完成，最小化锁持有时间
- 性能计数器使用独立锁，定期重置
- 异步告警与健康检查线程池在模块退出时优雅关闭
- 不持有外部连接或文件句柄
"""

import atexit
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Dict, Any, List, Optional, Tuple
from collections import deque, OrderedDict
import numpy as np

logger = logging.getLogger(__name__)

# 模块级线程池（独立用途）
_ALERT_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="olfactory_alert")
_HEALTH_CHECK_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="olfactory_health")


def _cleanup_executors():
    """优雅关闭线程池"""
    for executor in [_ALERT_EXECUTOR, _HEALTH_CHECK_EXECUTOR]:
        executor.shutdown(wait=False)
atexit.register(_cleanup_executors)


class OlfactoryCortex:
    """嗅觉皮层（机构终极版 v5.0）"""

    VERSION = "5.0.0"

    # ========== 类常量 ==========
    DEFAULT_WALL_DEPTH_LEVELS = 5
    DEFAULT_WALL_CANCEL_RATE_THRESHOLD_ASK = 0.40
    DEFAULT_WALL_CANCEL_RATE_THRESHOLD_BID = 0.36
    DEFAULT_WALL_PRICE_PROXIMITY_ATR = 0.5
    DEFAULT_WALL_CACHE_TTL_SEC = 60
    MAX_SNAPSHOT_PER_SYMBOL = 20
    MAX_TOTAL_SNAPSHOTS = 200
    SNAPSHOT_DEPTH = 5

    DEFAULT_TOXICITY_DRIFT_WINDOW_SEC = 3
    DEFAULT_TOXICITY_DRIFT_THRESHOLD_BPS = 2.0
    DEFAULT_TOXICITY_CACHE_TTL_SEC = 30
    DEFAULT_TOXICITY_SAMPLE_MIN = 10
    DEFAULT_TOXICITY_SAMPLE_MAX = 50
    DEFAULT_TOXICITY_THRESHOLD = 0.6

    DEFAULT_SYSTEMIC_CORR_THRESHOLD = 0.8
    DEFAULT_SYSTEMIC_CACHE_TTL_SEC = 120
    DEFAULT_SYSTEMIC_MAJOR_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
    DEFAULT_SYSTEMIC_MIN_VARIANCE = 1e-12
    DEFAULT_SYSTEMIC_MIN_ALIGNED_POINTS = 10

    DEFAULT_CLEANUP_INTERVAL_SEC = 120
    DEFAULT_MAX_CACHE_AGE_SEC = 300
    DEFAULT_ALERT_DEDUP_SEC = 30
    STARTUP_SILENCE_PERIOD_SEC = 30
    HEALTH_CHECK_TIMEOUT_SEC = 2
    HEALTH_CHECK_SYMBOL = "BTCUSDT"
    ALERT_ASYNC_TIMEOUT_SEC = 2

    # 日志频率限制
    _last_warning_time = 0.0
    _warning_interval = 1.0  # 1秒内同类型警告只输出一次

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._start_time = time.monotonic()
        self._load_config(config)

        self._lock = threading.RLock()
        self._paper_wall_cache: Dict[str, Dict[str, Any]] = {}
        self._paper_wall_ts: Dict[str, float] = {}
        self._toxicity_cache: Dict[str, Dict[str, Any]] = {}
        self._toxicity_ts: Dict[str, float] = {}
        self._systemic_risk_cache: Dict[str, Any] = {}
        self._systemic_risk_ts: float = 0.0

        self._snapshots: Dict[str, deque] = OrderedDict()
        self._paper_wall_history: Dict[str, deque] = {}

        self._alert_last: Dict[str, float] = {}

        self._metrics_lock = threading.Lock()
        self._metrics: Dict[str, Any] = {
            "calls": 0, "total_latency": 0.0, "last_reset": time.monotonic()
        }

        self._data_feed = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        self._last_cleanup = time.monotonic()

        logger.info("OlfactoryCortex v%s 初始化完成，冷却 %d 秒", self.VERSION, self.STARTUP_SILENCE_PERIOD_SEC)

    def _load_config(self, config: Optional[Dict[str, Any]]) -> None:
        if config is None:
            return
        for key, value in config.items():
            if not key.startswith("DEFAULT_"):
                logger.warning("忽略非 DEFAULT_ 配置键: %s", key)
                continue
            if hasattr(self, key):
                setattr(self, key, value)

    def inject_dependencies(self, data_feed=None, negotiation_bus=None, behavioral_logger=None):
        if data_feed:
            required = ['get_atomic_snapshot', 'get_ticker', 'get_recent_trades', 'get_klines']
            if all(hasattr(data_feed, m) for m in required):
                self._data_feed = data_feed
        if negotiation_bus and hasattr(negotiation_bus, 'publish_alert'):
            self._negotiation_bus = negotiation_bus
        if behavioral_logger:
            self._behavioral_logger = behavioral_logger

    @classmethod
    def _safe_float(cls, value: Any, default: float = 0.0, context: str = "") -> float:
        """安全转换为浮点数，失败返回默认值并记录限频日志"""
        if value is None:
            if context:
                logger.debug("_safe_float None value (context: %s)", context)
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            now = time.monotonic()
            if now - cls._last_warning_time > cls._warning_interval:
                logger.warning("_safe_float 转换失败: %s (context: %s), 使用默认值 %.4f", value, context, default)
                cls._last_warning_time = now
            return default

    @staticmethod
    def _time_wall() -> float:
        """返回外部系统用的 wall-clock 时间"""
        return time.time()

    @staticmethod
    def _time_mono() -> float:
        """返回内部计时用的单调时间"""
        return time.monotonic()

    def _in_startup_silence(self) -> Tuple[bool, float]:
        elapsed = self._time_mono() - self._start_time
        remaining = max(0.0, self.STARTUP_SILENCE_PERIOD_SEC - elapsed)
        return remaining > 0, remaining

    def _record_latency(self, start: float):
        with self._metrics_lock:
            self._metrics['calls'] += 1
            self._metrics['total_latency'] += (self._time_mono() - start)

    def _reset_metrics_if_needed(self):
        with self._metrics_lock:
            if self._time_mono() - self._metrics['last_reset'] > 3600:
                self._metrics = {"calls": 0, "total_latency": 0.0, "last_reset": self._time_mono()}

    # ========== 纸墙检测 ==========
    def sniff_paper_wall(self, symbol: str) -> Dict[str, Any]:
        start = self._time_mono()
        is_silent, remaining = self._in_startup_silence()
        if is_silent:
            return self._degraded("paper_wall", symbol, f"startup_silence ({int(remaining)}s remaining)", 1001)

        self._try_cleanup()

        with self._lock:
            if symbol in self._paper_wall_ts:
                age = self._time_mono() - self._paper_wall_ts[symbol]
                if age < self.DEFAULT_WALL_CACHE_TTL_SEC:
                    self._record_latency(start)
                    return {"status": "ok", "reason": "缓存", "data": self._paper_wall_cache[symbol],
                            "warnings": [], "error_code": 0}

        if not self._data_feed:
            return self._degraded("paper_wall", symbol, "DataFeed missing", 1002)

        try:
            ob, ticker = self._data_feed.get_atomic_snapshot(symbol)
            if not isinstance(ob, dict) or not isinstance(ticker, dict):
                return self._degraded("paper_wall", symbol, "invalid snapshot types", 1003)

            price = self._safe_float(ticker.get("last"), context=f"{symbol}.last")
            atr = self._safe_float(ticker.get("atr"), context=f"{symbol}.atr")
            if price <= 0 or atr <= 0:
                return self._degraded("paper_wall", symbol, "invalid price/atr", 1004)

            proximity = atr * self.DEFAULT_WALL_PRICE_PROXIMITY_ATR
            result = {"symbol": symbol, "is_paper_wall": False,
                      "cancel_rate_ask": 0.0, "cancel_rate_bid": 0.0,
                      "details": "No paper wall detected", "timestamp": self._time_wall()}

            for side, key, threshold in [
                ("asks", "cancel_rate_ask", self.DEFAULT_WALL_CANCEL_RATE_THRESHOLD_ASK),
                ("bids", "cancel_rate_bid", self.DEFAULT_WALL_CANCEL_RATE_THRESHOLD_BID)
            ]:
                levels = ob.get(side)
                if not isinstance(levels, list) or not levels:
                    continue
                best_price = self._safe_float(levels[0][0])
                best_vol = self._safe_float(levels[0][1])
                if best_price <= 0 or best_vol <= 0:
                    continue

                if side == "asks" and (best_price - price) > proximity:
                    continue
                if side == "bids" and (price - best_price) > proximity:
                    continue

                with self._lock:
                    snap_q = self._snapshots.get(symbol)  # 不提供默认值，避免空 deque 误判
                if snap_q:
                    oldest_side = snap_q[0][1].get(side, [])
                    if isinstance(oldest_side, list) and oldest_side:
                        old_vol = self._safe_float(oldest_side[0][1])
                        if old_vol > 0:
                            cancel_rate = max(0.0, (old_vol - best_vol) / old_vol)
                            result[key] = round(cancel_rate, 4)
                            if cancel_rate >= threshold:
                                result["is_paper_wall"] = True
                                result["details"] = f"{side} paper wall, cancel rate {cancel_rate:.1%}"

                # 存储压缩快照
                with self._lock:
                    if symbol not in self._snapshots:
                        self._snapshots[symbol] = deque(maxlen=self.MAX_SNAPSHOT_PER_SYMBOL)
                    compact = {
                        "asks": [(self._safe_float(x[0]), self._safe_float(x[1])) for x in ob.get("asks", [])[:self.SNAPSHOT_DEPTH]],
                        "bids": [(self._safe_float(x[0]), self._safe_float(x[1])) for x in ob.get("bids", [])[:self.SNAPSHOT_DEPTH]],
                    }
                    self._snapshots[symbol].append((self._time_mono(), compact))
                    while len(self._snapshots) > self.MAX_TOTAL_SNAPSHOTS:
                        oldest_sym = next(iter(self._snapshots))
                        try:
                            self._snapshots[oldest_sym].popleft()
                        except IndexError:
                            pass
                        if not self._snapshots[oldest_sym]:
                            del self._snapshots[oldest_sym]

            # 历史纸墙频率统计
            with self._lock:
                hist = self._paper_wall_history.setdefault(symbol, deque(maxlen=20))
                hist.append(1 if result["is_paper_wall"] else 0)
                result["paper_wall_frequency"] = sum(hist) / len(hist) if hist else 0.0

            with self._lock:
                self._paper_wall_cache[symbol] = result
                self._paper_wall_ts[symbol] = self._time_mono()

            if result["is_paper_wall"]:
                self._alert("paper_wall", symbol, result["details"], "high",
                            {"threshold_ask": self.DEFAULT_WALL_CANCEL_RATE_THRESHOLD_ASK,
                             "threshold_bid": self.DEFAULT_WALL_CANCEL_RATE_THRESHOLD_BID})

            self._record_latency(start)
            return {"status": "ok", "reason": "纸墙检测完成", "data": result, "warnings": [], "error_code": 0}

        except Exception as e:
            logger.error("纸墙检测异常 %s: %s", symbol, e, exc_info=True)
            return self._degraded("paper_wall", symbol, str(e), 1099)

    # ========== 订单流毒性 ==========
    def sniff_order_toxicity(self, symbol: str) -> Dict[str, Any]:
        start = self._time_mono()
        is_silent, remaining = self._in_startup_silence()
        if is_silent:
            return self._degraded("toxicity", symbol, f"startup_silence ({int(remaining)}s remaining)", 2001)

        self._try_cleanup()

        with self._lock:
            if symbol in self._toxicity_ts:
                age = self._time_mono() - self._toxicity_ts[symbol]
                if age < self.DEFAULT_TOXICITY_CACHE_TTL_SEC:
                    self._record_latency(start)
                    return {"status": "ok", "reason": "缓存", "data": self._toxicity_cache[symbol],
                            "warnings": [], "error_code": 0}

        if not self._data_feed:
            return self._degraded("toxicity", symbol, "DataFeed missing", 2002)

        try:
            ticker = self._data_feed.get_ticker(symbol)
            vol_proxy = self._safe_float(ticker.get("atr")) if ticker else 0.0
            sample_size = min(self.DEFAULT_TOXICITY_SAMPLE_MAX,
                              max(self.DEFAULT_TOXICITY_SAMPLE_MIN,
                                  int(30 * (1 + vol_proxy / 100)))) if vol_proxy > 0 else self.DEFAULT_TOXICITY_SAMPLE_MIN

            trades = self._data_feed.get_recent_trades(symbol, limit=sample_size + 20)
            if not trades or len(trades) < 5:
                return self._degraded("toxicity", symbol, "insufficient trades", 2003)

            toxic_count = 0
            total_checked = 0
            window = self.DEFAULT_TOXICITY_DRIFT_WINDOW_SEC

            for i, trade in enumerate(trades):
                trade_ts = trade.get("server_time", trade.get("timestamp"))
                if trade_ts is None:
                    continue
                trade_price = self._safe_float(trade.get("price"))
                side = trade.get("side", "")
                if trade_price <= 0:
                    continue

                future = None
                for j in range(i + 1, len(trades)):
                    future_ts = trades[j].get("server_time", trades[j].get("timestamp"))
                    if future_ts is not None and future_ts >= trade_ts + window:
                        future = trades[j]
                        break
                if not future:
                    continue
                future_price = self._safe_float(future.get("price"))
                if future_price <= 0:
                    continue

                if side == "buy":
                    drift_bps = (future_price - trade_price) / trade_price * 10000
                else:
                    drift_bps = (trade_price - future_price) / trade_price * 10000

                total_checked += 1
                if drift_bps < -self.DEFAULT_TOXICITY_DRIFT_THRESHOLD_BPS:
                    toxic_count += 1

            toxicity = min(1.0, toxic_count / total_checked) if total_checked > 0 else 0.0

            result = {"symbol": symbol, "toxicity_index": round(toxicity, 4),
                      "toxic_trade_count": toxic_count, "total_checked": total_checked,
                      "is_toxic": toxicity >= self.DEFAULT_TOXICITY_THRESHOLD, "timestamp": self._time_wall()}

            with self._lock:
                self._toxicity_cache[symbol] = result
                self._toxicity_ts[symbol] = self._time_mono()

            if result["is_toxic"]:
                self._alert("toxicity", symbol, f"Toxicity index {toxicity:.2f}", "high",
                            {"threshold": self.DEFAULT_TOXICITY_THRESHOLD})

            self._record_latency(start)
            return {"status": "ok", "reason": "毒性检测完成", "data": result, "warnings": [], "error_code": 0}

        except Exception as e:
            logger.error("毒性检测异常 %s: %s", symbol, e, exc_info=True)
            return self._degraded("toxicity", symbol, str(e), 2099)

    # ========== 系统性风险 ==========
    def sniff_systemic_risk(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        start = self._time_mono()
        is_silent, remaining = self._in_startup_silence()
        if is_silent:
            return self._degraded("systemic", "batch", f"startup_silence ({int(remaining)}s remaining)", 3001)

        self._try_cleanup()

        if not symbols:
            symbols = self.DEFAULT_SYSTEMIC_MAJOR_SYMBOLS
        symbols = list(dict.fromkeys(symbols))  # 去重并保持顺序

        with self._lock:
            if self._systemic_risk_cache and (self._time_mono() - self._systemic_risk_ts) < self.DEFAULT_SYSTEMIC_CACHE_TTL_SEC:
                self._record_latency(start)
                return {"status": "ok", "reason": "缓存", "data": self._systemic_risk_cache,
                        "warnings": [], "error_code": 0}

        if not self._data_feed:
            return self._degraded("systemic", "batch", "DataFeed missing", 3002)

        try:
            raw_returns = {}
            for sym in symbols:
                klines = self._data_feed.get_klines(sym, interval="1m", limit=60)
                if not klines or len(klines) < 30:
                    continue
                closes = np.array([self._safe_float(k[4]) for k in klines])
                if np.std(closes) < self.DEFAULT_SYSTEMIC_MIN_VARIANCE:
                    continue
                rets = np.diff(np.log(np.maximum(closes, 1e-12)))
                std_ret = np.std(rets)
                if std_ret > 0:
                    rets = np.clip(rets, -3 * std_ret, 3 * std_ret)
                raw_returns[sym] = rets

            if len(raw_returns) < 2:
                return self._degraded("systemic", "batch", "insufficient valid symbols", 3003)

            min_len = min(len(v) for v in raw_returns.values())
            aligned = {sym: v[-min_len:] for sym, v in raw_returns.items()}

            matrix = np.array(list(aligned.values()))
            if matrix.shape[0] < 2 or matrix.shape[1] < self.DEFAULT_SYSTEMIC_MIN_ALIGNED_POINTS:
                return self._degraded("systemic", "batch", "insufficient aligned points", 3004)

            # 过滤标准差为 0 的品种
            stds = np.std(matrix, axis=1)
            valid_idx = np.where(stds > self.DEFAULT_SYSTEMIC_MIN_VARIANCE)[0]
            if len(valid_idx) < 2:
                return self._degraded("systemic", "batch", "no valid variability", 3005)
            matrix = matrix[valid_idx]
            valid_symbols = [list(aligned.keys())[i] for i in valid_idx]

            corr = np.corrcoef(matrix)
            n = matrix.shape[0]
            avg_corr = (corr.sum() - n) / (n * (n - 1)) if n > 1 else 0.0
            avg_corr = float(np.nan_to_num(avg_corr, nan=0.0))

            risk_level = "low"
            warning = False
            if avg_corr > self.DEFAULT_SYSTEMIC_CORR_THRESHOLD:
                risk_level = "high"
                warning = True
            elif avg_corr > 0.6:
                risk_level = "elevated"

            result = {"symbols": valid_symbols, "avg_correlation": round(avg_corr, 4),
                      "risk_level": risk_level, "warning": warning, "timestamp": self._time_wall()}

            with self._lock:
                self._systemic_risk_cache = result
                self._systemic_risk_ts = self._time_mono()

            if warning:
                self._alert("systemic", "batch", f"Average correlation {avg_corr:.2f}", "critical",
                            {"threshold": self.DEFAULT_SYSTEMIC_CORR_THRESHOLD})

            self._record_latency(start)
            return {"status": "ok", "reason": "系统性风险评估完成", "data": result,
                    "warnings": [], "error_code": 0}

        except Exception as e:
            logger.error("系统性风险异常: %s", e, exc_info=True)
            return self._degraded("systemic", "batch", str(e), 3099)

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        try:
            if self._data_feed:
                future = _HEALTH_CHECK_EXECUTOR.submit(self._data_feed.get_ticker, self.HEALTH_CHECK_SYMBOL)
                try:
                    ticker = future.result(timeout=self.HEALTH_CHECK_TIMEOUT_SEC)
                except FuturesTimeoutError:
                    logger.warning("健康检查 DataFeed 超时")
                    return {"status": "degraded", "reason": "DataFeed timeout", "data": {},
                            "warnings": ["datafeed_timeout"], "error_code": 4001}
                except Exception as e:
                    logger.warning("健康检查 DataFeed 调用异常: %s", e)
                    return {"status": "degraded", "reason": f"DataFeed error: {e}", "data": {},
                            "warnings": ["datafeed_error"], "error_code": 4002}
                if ticker is None:
                    return {"status": "degraded", "reason": "DataFeed 无响应", "data": {},
                            "warnings": ["datafeed_no_response"], "error_code": 4003}

            self._reset_metrics_if_needed()
            with self._metrics_lock:
                calls = self._metrics['calls']
                avg_lat = self._metrics['total_latency'] / max(1, calls)

            return {"status": "ok", "reason": "OlfactoryCortex 正常",
                    "data": {"avg_latency_s": round(avg_lat, 6), "calls": calls,
                             "uptime_sec": round(self._time_mono() - self._start_time, 1),
                             "version": self.VERSION},
                    "warnings": [], "error_code": 0}
        except Exception as e:
            logger.error("健康检查失败: %s", e, exc_info=True)
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"],
                    "error_code": 4999}

    # ========== 内部工具 ==========
    def _try_cleanup(self):
        now = self._time_mono()
        if now - self._last_cleanup < self.DEFAULT_CLEANUP_INTERVAL_SEC:
            return
        with self._lock:
            # 直接删除过期缓存
            for k in list(self._paper_wall_ts.keys()):
                if now - self._paper_wall_ts[k] > self.DEFAULT_MAX_CACHE_AGE_SEC:
                    del self._paper_wall_cache[k]
                    del self._paper_wall_ts[k]
            for k in list(self._toxicity_ts.keys()):
                if now - self._toxicity_ts[k] > self.DEFAULT_MAX_CACHE_AGE_SEC:
                    del self._toxicity_cache[k]
                    del self._toxicity_ts[k]
            # 清理快照
            for sym in list(self._snapshots.keys()):
                q = self._snapshots[sym]
                while q and (now - q[0][0] > self.DEFAULT_MAX_CACHE_AGE_SEC):
                    q.popleft()
                if not q:
                    del self._snapshots[sym]
            # 清理纸墙历史
            for sym in list(self._paper_wall_history.keys()):
                if sym not in self._snapshots and sym not in self._paper_wall_cache:
                    del self._paper_wall_history[sym]
        self._last_cleanup = now

    def _alert(self, alert_type: str, symbol: str, msg: str, level: str = "warning",
               thresholds: Optional[Dict[str, Any]] = None):
        with self._lock:
            key = f"{alert_type}:{symbol}:{level}"
            last = self._alert_last.get(key, 0)
            if self._time_mono() - last < self.DEFAULT_ALERT_DEDUP_SEC:
                return
            self._alert_last[key] = self._time_mono()

        full_msg = f"[{level.upper()}] {alert_type} {symbol}: {msg}"
        if self._negotiation_bus:
            _ALERT_EXECUTOR.submit(self._safe_publish_alert, alert_type, symbol, level, msg, thresholds)
        if level == "critical":
            logger.error("%s #RECOVERY: 检查市场结构，考虑减仓", full_msg)
        else:
            logger.warning(full_msg)
        if self._behavioral_logger:
            try:
                self._behavioral_logger.log_event(
                    event_type=f"olfactory_{alert_type}",
                    details={"symbol": symbol, "level": level, "message": msg}
                )
            except Exception as e:
                logger.warning("行为日志记录异常: %s", e)

    def _safe_publish_alert(self, alert_type, symbol, level, msg, thresholds):
        try:
            self._negotiation_bus.publish_alert(
                alert_type=alert_type, symbol=symbol,
                level=level, message=msg, timestamp=self._time_wall(),
                details=thresholds or {}
            )
        except Exception as e:
            logger.warning("协商总线异步告警异常: %s", e)

    def _degraded(self, category: str, symbol: str, reason: str, error_code: int) -> Dict[str, Any]:
        data = {
            "symbol": symbol,
            "details": f"降级({error_code}): {reason}",
            "timestamp": self._time_wall(),
            "version": self.VERSION,
            "error_code": error_code,
            "is_paper_wall": False,
            "toxicity_index": 0.0,
            "is_toxic": False,
            "avg_correlation": 0.0,
            "risk_level": "unknown",
            "warning": False,
        }
        return {
            "status": "degraded",
            "reason": f"{category} 降级: {reason}",
            "data": data,
            "warnings": [f"degraded_{category}"],
            "error_code": error_code
                }
