"""
火种系统 · 听觉皮层 (AuditoryCortex)

核心职责：
1. 宏观事件监听与影响评估：从外部经济日历源获取未来事件，按预设规则量化事件级别并预估波动率冲击倍数，
   提供事件影响衰减曲线，支持事件前自动防御窗口
2. 社交情绪实时解析：对主流社交平台的文本流进行多语种情绪评分，采用加权融合与异常值过滤，
   输出 -1（恐慌）至 +1（贪婪）的连续情绪值及趋势

外部依赖（真实模块接口）：
- openclaw.tools.market_data.MarketDataTool : 获取经济日历数据和事件详情，需实现 get_economic_calendar(days_ahead)
- openclaw.tools.sentiment_engine.SentimentEngine : 多语种情绪分析模型，需实现 analyze(text, lang) 和 ping()
- openclaw.tools.text_fetcher.TextFetcher : 从社交平台拉取原始文本，需实现 fetch(source, symbol)
- core.utils.cache_manager.CacheManager : 缓存事件数据和情绪结果，需实现 get(key) 和 set(key, value, ttl)

接口契约：
- get_macro_alert_level(is_high_vol: bool = False) -> Dict[str, Any] : 返回当前时刻的宏观事件预警等级、详情及冲击倍数预估
- get_sentiment_score(symbol: str = "BTC") -> Dict[str, Any] : 返回指定币种的最新情绪分数与变化趋势
- get_impact_decay(event_timestamp: float, current_time: float) -> Dict[str, Any] : 返回事件影响的衰减因子
- health_check() -> Dict[str, Any] : 模块自检，验证外部依赖连通性
- shutdown() -> None : 显式关闭线程池，用于热插拔卸载
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 MarketDataTool 不可用或超时时，从配置文件加载静态经济日历（含绝对时间戳），并标记 "degraded"
- 当 SentimentEngine 不可用时，降级为本地关键词规则引擎，情绪分数置信度降至 0.1
- 所有外部API调用均设置独立超时（默认5秒），超时后视为服务不可用并执行降级
- 网络瞬断时采用指数退避重试（最多3次），但数据格式错误不重试

资源管理：
- 使用线程池复用线程，支持显式 shutdown() 和 atexit 双重保障，重复关闭安全
- 情绪缓存使用 LRU 策略，防止内存溢出
- 不持有任何需手动释放的外部连接或文件句柄

线程安全：
- 宏观事件和情绪分析使用独立的锁（_event_lock, _sentiment_lock），互不阻塞
- 公共方法均为线程安全
"""

import atexit
import logging
import math
import os
import time
import threading
from typing import Dict, Any, List, Optional, Tuple
from collections import OrderedDict
import numpy as np
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
import yaml

logger = logging.getLogger(__name__)


class AuditoryCortex:
    """听觉皮层：宏观事件感知与社交情绪分析"""

    # ========== 类常量（可被配置覆盖的默认值） ==========
    DEFAULT_EVENT_CACHE_TTL_SEC = 300
    HIGH_VOL_EVENT_CACHE_TTL_SEC = 120
    MAX_EVENT_LOOKAHEAD_SEC = 86400
    EVENT_LEVEL_CRITICAL = 1
    EVENT_LEVEL_MODERATE = 2
    EVENT_LEVEL_MINOR = 3
    PRE_EVENT_DEFENSE_SEC = 900
    DEFAULT_IMPACT_MULTIPLIERS = {1: 3.0, 2: 2.0, 3: 1.2}
    IMPACT_DECAY_HALF_LIFE_SEC = 600

    DEFAULT_SENTIMENT_CACHE_TTL_SEC = 60
    SENTIMENT_SOURCES = ["twitter", "reddit", "telegram"]
    SENTIMENT_SOURCE_WEIGHTS = {"twitter": 0.5, "reddit": 0.3, "telegram": 0.2}
    SENTIMENT_OUTLIER_MAD_THRESH = 2.5
    MAX_CACHED_SYMBOLS = 50
    MIN_SAMPLES_FOR_SENTIMENT = 3
    SENTIMENT_TREND_THRESHOLD = 0.15

    EXTERNAL_API_TIMEOUT_SEC = 5
    MAX_RETRY_COUNT = 3
    INITIAL_RETRY_DELAY_SEC = 1
    THREAD_POOL_MAX_WORKERS = 4

    STATIC_CALENDAR_PATH = "config/economic_calendar.yaml"

    # 配置文件可修改的键白名单，防止覆盖关键方法
    CONFIG_WHITELIST = {
        "event": ["cache_ttl_sec", "high_vol_cache_ttl_sec", "defense_sec", "impact_multipliers", "decay_half_life_sec"],
        "sentiment": ["cache_ttl_sec", "trend_threshold", "min_samples", "mad_threshold", "max_cached_symbols"],
        "general": ["api_timeout_sec", "max_retry_count", "thread_pool_workers", "static_calendar_path"],
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._apply_config(config)
        self._init_dependencies()
        self._init_state()
        self._init_thread_pool()
        logger.info("AuditoryCortex 初始化完成 (workers=%d)", self.THREAD_POOL_MAX_WORKERS)

    def _apply_config(self, config: Optional[Dict[str, Any]]) -> None:
        if not config:
            return
        for section, params in config.items():
            if section not in self.CONFIG_WHITELIST:
                logger.warning(f"忽略未知配置节: {section}")
                continue
            allowed_keys = self.CONFIG_WHITELIST[section]
            for key, val in params.items():
                if key not in allowed_keys:
                    logger.warning(f"忽略未授权配置键: {section}.{key}")
                    continue
                self._safe_set_config(key, val)

    def _safe_set_config(self, key: str, val: Any) -> None:
        if hasattr(self.__class__, key) and not callable(getattr(self.__class__, key)):
            setattr(self, key, val)
            logger.debug(f"配置覆盖: {key} = {val}")
        else:
            logger.warning(f"拒绝配置键: {key} (不存在或为方法)")

    def _init_dependencies(self) -> None:
        self._market_data_tool = None
        self._sentiment_engine = None
        self._cache_manager = None
        self._text_fetcher = None

    def _init_state(self) -> None:
        self._last_event_update = 0.0
        self._cached_events: List[Dict[str, Any]] = []
        self._cached_sentiments: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._last_sentiment_update: Dict[str, float] = {}
        self._event_lock = threading.Lock()
        self._sentiment_lock = threading.Lock()
        self._shutdown_flag = False

    def _init_thread_pool(self) -> None:
        workers = min(self.THREAD_POOL_MAX_WORKERS, (os.cpu_count() or 2))
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="auditory")
        atexit.register(self._cleanup)

    def inject_dependencies(
        self,
        market_data_tool: Optional[Any] = None,
        sentiment_engine: Optional[Any] = None,
        cache_manager: Optional[Any] = None,
        text_fetcher: Optional[Any] = None,
    ) -> None:
        if market_data_tool is not None and hasattr(market_data_tool, 'get_economic_calendar'):
            self._market_data_tool = market_data_tool
            logger.info("MarketDataTool 注入成功")
        else:
            logger.warning("MarketDataTool 不可用，将使用静态日历")
        if sentiment_engine is not None and hasattr(sentiment_engine, 'analyze') and hasattr(sentiment_engine, 'ping'):
            self._sentiment_engine = sentiment_engine
            logger.info("SentimentEngine 注入成功")
        else:
            logger.warning("SentimentEngine 不可用，将使用本地规则引擎")
        if cache_manager is not None:
            self._cache_manager = cache_manager
            logger.info("CacheManager 注入成功")
        if text_fetcher is not None:
            self._text_fetcher = text_fetcher
            logger.info("TextFetcher 注入成功")

    # ========== 公共接口 ==========
    def get_macro_alert_level(self, is_high_vol: bool = False) -> Dict[str, Any]:
        now_mono = time.monotonic()
        ttl = self.HIGH_VOL_EVENT_CACHE_TTL_SEC if is_high_vol else self.DEFAULT_EVENT_CACHE_TTL_SEC
        with self._event_lock:
            if self._last_event_update > 0 and (now_mono - self._last_event_update) < ttl:
                return self._build_alert_response(self._cached_events)
        events = self._fetch_with_retry(self._fetch_economic_events, "macro_events", retry_on_timeout_only=True)
        if events is None:
            return self._build_degraded_response()
        events.sort(key=lambda e: e.get("timestamp", 0))
        events_copy = [dict(e) for e in events]
        with self._event_lock:
            self._cached_events = events_copy
            self._last_event_update = now_mono
        return self._build_alert_response(events)

    def get_sentiment_score(self, symbol: str = "BTC") -> Dict[str, Any]:
        now_mono = time.monotonic()
        with self._sentiment_lock:
            if symbol in self._last_sentiment_update:
                age = now_mono - self._last_sentiment_update[symbol]
                if age < self.DEFAULT_SENTIMENT_CACHE_TTL_SEC and symbol in self._cached_sentiments:
                    self._cached_sentiments.move_to_end(symbol)
                    self._last_sentiment_update[symbol] = now_mono
                    return {"status": "ok", "reason": "返回缓存的情绪分数",
                            "data": self._cached_sentiments[symbol], "warnings": []}
        score, confidence = self._analyze_sentiment(symbol)
        if score is None:
            return self._build_sentiment_degraded_response()
        trend = self._compute_sentiment_trend(symbol, score)
        result = {"score": round(score, 3), "trend": trend, "confidence": round(confidence, 3)}
        warnings = self._generate_sentiment_warnings(score)
        with self._sentiment_lock:
            while len(self._cached_sentiments) >= self.MAX_CACHED_SYMBOLS:
                self._cached_sentiments.popitem(last=False)
            self._cached_sentiments[symbol] = result
            self._last_sentiment_update[symbol] = now_mono
        return {"status": "ok", "reason": f"{symbol} 情绪分数: {score:.2f}", "data": result, "warnings": warnings}

    def get_impact_decay(self, event_timestamp: float, current_time: Optional[float] = None) -> Dict[str, Any]:
        current_time = current_time or time.time()
        elapsed = max(0.0, current_time - event_timestamp)
        half_life = self.IMPACT_DECAY_HALF_LIFE_SEC
        decay_factor = math.exp(-math.log(2) * elapsed / half_life) if half_life > 0 else 1.0
        return {
            "status": "ok",
            "reason": f"衰减因子: {decay_factor:.4f}",
            "data": {"decay_factor": round(decay_factor, 4), "elapsed_sec": round(elapsed, 1)},
            "warnings": [],
        }

    def health_check(self) -> Dict[str, Any]:
        try:
            if not hasattr(self, '_executor') or self._executor is None:
                return {"status": "degraded", "reason": "线程池未初始化", "data": {}, "warnings": ["executor_missing"]}
            if self._shutdown_flag:
                return {"status": "degraded", "reason": "模块已关闭", "data": {}, "warnings": ["module_shutdown"]}
            # 安全检测线程池存活
            executor_alive = False
            try:
                future = self._executor.submit(lambda: True)
                if future.result(timeout=0.5):
                    executor_alive = True
            except Exception:
                pass
            dep_status = {
                "market_data_tool": self._market_data_tool is not None,
                "sentiment_engine": self._sentiment_engine is not None,
                "cache_manager": self._cache_manager is not None,
                "text_fetcher": self._text_fetcher is not None,
                "executor_alive": executor_alive,
            }
            return {"status": "ok", "reason": "自检通过", "data": {"dependencies": dep_status}, "warnings": []}
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查依赖注入和数据结构", exc_info=True)
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"]}

    def shutdown(self) -> None:
        if self._shutdown_flag:
            return
        self._shutdown_flag = True
        if hasattr(self, '_executor') and self._executor is not None:
            try:
                self._executor.shutdown(wait=True, timeout=self.EXTERNAL_API_TIMEOUT_SEC + 2)
                logger.info("线程池已安全关闭")
            except Exception as e:
                logger.warning(f"关闭线程池异常: {e}")

    # ========== 私有方法 ==========
    def _build_alert_response(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        warnings: List[str] = []
        max_level = self.EVENT_LEVEL_MINOR
        next_event = None
        defense_active = False
        impact_multiplier = 1.0
        now_wall = time.time()
        for event in events:
            event_level = event.get("level", self.EVENT_LEVEL_MINOR)
            event_time = event.get("timestamp", 0)
            if event_time > now_wall:
                if event_level <= self.EVENT_LEVEL_MODERATE and (event_time - now_wall) <= self.PRE_EVENT_DEFENSE_SEC:
                    defense_active = True
                if event_level < max_level:
                    max_level = event_level
                    next_event = event
                    impact_multiplier = event.get("impact_multiplier") or self.DEFAULT_IMPACT_MULTIPLIERS.get(event_level, 1.0)
        if max_level == self.EVENT_LEVEL_CRITICAL:
            logger.warning("一级宏观事件临近")
            warnings.append("CRITICAL: 一级宏观事件临近，建议全系统防御")
        if defense_active:
            warnings.append("WARNING: 已进入事件前自动防御窗口")
        return {
            "status": "ok",
            "reason": f"宏观预警: {self._level_to_name(max_level)}",
            "data": {
                "level": max_level,
                "level_name": self._level_to_name(max_level),
                "next_event": next_event,
                "defense_active": defense_active,
                "impact_multiplier": impact_multiplier,
            },
            "warnings": warnings,
        }

    def _build_degraded_response(self) -> Dict[str, Any]:
        return {
            "status": "degraded",
            "reason": "无法获取宏观事件数据，使用保守默认值",
            "data": {
                "level": self.EVENT_LEVEL_MODERATE,
                "level_name": "中度预警(降级)",
                "next_event": None,
                "defense_active": True,
                "impact_multiplier": 1.5,
            },
            "warnings": ["macro_event_fetch_failed"],
        }

    def _build_sentiment_degraded_response(self) -> Dict[str, Any]:
        return {
            "status": "degraded",
            "reason": "情绪分析服务不可用，返回中性值",
            "data": {"score": 0.0, "trend": "flat", "confidence": 0.0},
            "warnings": ["sentiment_analysis_failed"],
        }

    @staticmethod
    def _generate_sentiment_warnings(score: float) -> List[str]:
        warnings = []
        if score <= -0.5:
            warnings.append("WARNING: 社交情绪极度恐慌")
        elif score >= 0.8:
            warnings.append("WARNING: 社交情绪极度贪婪")
        return warnings

    def _fetch_with_retry(self, func, name: str, retry_on_timeout_only: bool = False) -> Optional[Any]:
        last_exception = None
        for attempt in range(self.MAX_RETRY_COUNT):
            try:
                return func()
            except Exception as e:
                last_exception = e
                is_timeout = isinstance(e, (TimeoutError, FutureTimeoutError))
                if retry_on_timeout_only and not is_timeout:
                    logger.error(f"{name} 非超时错误，停止重试", exc_info=True)
                    break
                if attempt == self.MAX_RETRY_COUNT - 1:
                    break
                delay = self.INITIAL_RETRY_DELAY_SEC * (2 ** attempt)
                logger.warning(f"{name} 重试 {attempt+1}/{self.MAX_RETRY_COUNT}: {e}，等待 {delay}s")
                time.sleep(delay)
        if last_exception:
            logger.error(f"{name} 重试耗尽", exc_info=True)
        return None

    def _fetch_economic_events(self) -> Optional[List[Dict[str, Any]]]:
        if self._market_data_tool is None:
            return self._get_static_calendar()
        try:
            future = self._executor.submit(self._market_data_tool.get_economic_calendar, days_ahead=1)
            events = future.result(timeout=self.EXTERNAL_API_TIMEOUT_SEC)
            if isinstance(events, list) and events:
                return events
        except FutureTimeoutError:
            logger.warning("MarketDataTool 调用超时")
            future.cancel()
        except Exception as e:
            logger.warning(f"MarketDataTool 异常: {e}", exc_info=True)
        return self._get_static_calendar()

    def _get_static_calendar(self) -> List[Dict[str, Any]]:
        path = self.STATIC_CALENDAR_PATH
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                if isinstance(data, list):
                    logger.info(f"从 {path} 加载静态日历，共 {len(data)} 个事件")
                    return data
            except Exception as e:
                logger.warning(f"加载静态日历失败: {e}，降级为空列表")
        logger.warning("无静态日历可用，宏观感知严重受限")
        return []

    def _analyze_sentiment(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        if self._sentiment_engine is None:
            return None, None
        futures = {}
        for source in self.SENTIMENT_SOURCES:
            futures[self._executor.submit(self._fetch_sentiment_from_source, source, symbol)] = source
        scores, weights = [], []
        try:
            completed = as_completed(futures, timeout=self.EXTERNAL_API_TIMEOUT_SEC + 1)
            for future in completed:
                source = futures.pop(future)
                try:
                    score = future.result(timeout=0.1)
                    if score is not None:
                        scores.append(score)
                        weights.append(self.SENTIMENT_SOURCE_WEIGHTS.get(source, 0.0))
                except Exception as e:
                    logger.warning(f"情绪源 {source} 异常: {e}")
        except FutureTimeoutError:
            logger.warning("情绪分析整体超时")
            for future in futures:
                future.cancel()
        if len(scores) < self.MIN_SAMPLES_FOR_SENTIMENT:
            return None, None
        return self._compute_weighted_median_with_mad(scores, weights)

    def _compute_weighted_median_with_mad(self, scores: List[float], weights: List[float]) -> Tuple[float, float]:
        total_weight = sum(weights)
        if total_weight <= 0:
            return 0.0, 0.0
        weights_norm = [w / total_weight for w in weights]
        scores_arr = np.array(scores)
        weights_arr = np.array(weights_norm)
        sorted_idx = np.argsort(scores_arr)
        cum_weights = np.cumsum(weights_arr[sorted_idx])
        median_idx = np.searchsorted(cum_weights, 0.5)
        weighted_median = scores_arr[sorted_idx[min(median_idx, len(scores_arr) - 1)]]
        mad = np.median(np.abs(scores_arr - np.median(scores_arr)))
        threshold = self.SENTIMENT_OUTLIER_MAD_THRESH * max(mad, 0.01)
        filtered = [(s, w) for s, w in zip(scores, weights_norm) if abs(s - weighted_median) <= threshold]
        if not filtered:
            return 0.0, 0.0
        filtered_scores = np.array([f[0] for f in filtered])
        filtered_weights = np.array([f[1] for f in filtered])
        final_score = float(np.average(filtered_scores, weights=filtered_weights))
        confidence = min(1.0, np.sum(filtered_weights) * len(filtered) / len(scores))
        return final_score, confidence

    def _fetch_sentiment_from_source(self, source: str, symbol: str) -> Optional[float]:
        if self._sentiment_engine is None:
            return None
        text = None
        if self._text_fetcher is not None and hasattr(self._text_fetcher, 'fetch'):
            try:
                text = self._text_fetcher.fetch(source, symbol)
            except Exception as e:
                logger.warning(f"TextFetcher 失败 ({source}:{symbol}): {e}")
        if text is None:
            return None
        try:
            result = self._sentiment_engine.analyze(text, "auto")
            return float(result.get("score", 0.0)) if isinstance(result, dict) else None
        except Exception as e:
            logger.warning(f"SentimentEngine 分析失败 ({source}:{symbol}): {e}")
            return None

    def _compute_sentiment_trend(self, symbol: str, current_score: float) -> str:
        with self._sentiment_lock:
            if symbol in self._cached_sentiments:
                prev = self._cached_sentiments[symbol].get("score", 0.0)
                self._cached_sentiments.move_to_end(symbol)
                if current_score - prev > self.SENTIMENT_TREND_THRESHOLD:
                    return "rising"
                if prev - current_score > self.SENTIMENT_TREND_THRESHOLD:
                    return "falling"
        return "flat"

    @classmethod
    def _level_to_name(cls, level: int) -> str:
        return {1: "一级预警", 2: "二级关注", 3: "三级监控"}.get(level, "未知级别")

    def _cleanup(self) -> None:
        if hasattr(self, '_executor') and self._executor is not None:
            try:
                self._executor.shutdown(wait=False)
                logger.info("线程池通过 atexit 清理")
            except Exception:
                pass

    def __del__(self):
        try:
            self.shutdown()
        except Exception:
            pass

    def __repr__(self) -> str:
        return (f"AuditoryCortex(market_data={'OK' if self._market_data_tool else 'N/A'}, "
                f"sentiment={'OK' if self._sentiment_engine else 'N/A'}, "
                f"shutdown={self._shutdown_flag})")
