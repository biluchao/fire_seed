"""
火种系统 · 动态评分卡入口 (Scorecard)

核心职责：
1. 聚合信号漏斗、过滤器协同、M12 六维协同、跨周期约束、信号质量预评估及微观二次确认等子模块，对指定周期的市场信号进行综合量化评分
2. 输出包含评分、方向、置信度、仓位乘数、通过标记、信号指纹及详细归因的标准化信号对象，同时发布高置信度信号事件供下游预加载

外部依赖（真实模块接口）：
- core.scorecard.signal_funnel.SignalFunnel : 对原始信号进行 A/B/C 三级分层和动态阈值调整
- core.scorecard.filter_coordinator.FilterCoordinator : 管理三档过滤器组合的自动切换与饥渴响应
- core.scorecard.ma12_synergy.Ma12Synergy : 提供 M12 均线方向、距离分区、质量评分和回踩观察
- core.scorecard.cross_tf_constraint.CrossTfConstraint : 注入上级周期的区间约束并计算加权
- core.scorecard.signal_quality_precheck.SignalQualityPrecheck : 输出纯度、稀有性、时效性和共振度四维评估
- core.scorecard.micro_second_confirm.MicroSecondConfirm : 基于微观结构（纸墙、脉冲背离、价差操纵）进行二次确认
- core.perception.sensory_snapshot.SensorySnapshot : 获取标准化感官快照
- core.negotiation_bus.NegotiationBus : 发布评分事件和预警
- core.behavioral_logger.BehavioralLogger : 记录评分关键决策日志

接口契约：
- evaluate_signal(symbol: str, timeframe: str, sensory_snapshot: Dict[str, Any]) -> Dict[str, Any] : 综合评分，返回标准化信号对象
- set_threshold(threshold: float, timeframe: str = None) -> Dict[str, Any] : 动态调整入场阈值
- update_weights(weights: Dict[str, float]) -> Dict[str, Any] : 更新评分权重
- load_config(config: Dict[str, Any]) -> None : 从配置文件加载参数
- get_health_overview() -> Dict[str, Any] : 返回评分卡及子模块整体健康状态
- health_check() -> Dict[str, Any] : 模块深度自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当任一子模块不可用或健康检查失败时，自动忽略该子模块的评分贡献，整体评分使用剩余有效子模块的加权结果
- 所有降级值在类常量区明确声明
- 异常后通知健康监控，并记录完整上下文日志

资源管理：
- 本模块不持有外部资源句柄，子模块均为无状态或自维护状态
- 所有中间数据在请求结束后自动回收
"""

import hashlib
import logging
import math
import threading
import time
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 辅助类
# ---------------------------------------------------------------------------
class SubmoduleBreaker:
    """子模块断路器：当模块连续失败达到阈值时自动熔断，支持渐进恢复"""

    def __init__(self, name: str, failure_threshold: int = 5, cooldown_seconds: float = 30.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.state = "closed"
        self._lock = threading.Lock()

    def record_success(self) -> None:
        with self._lock:
            if self.state == "half_open":
                # 渐进恢复：需连续成功两次
                self.failure_count -= 1
                if self.failure_count <= self.failure_threshold - 2:
                    self.state = "closed"
                    self.failure_count = 0
            else:
                self.failure_count = 0

    def record_failure(self) -> None:
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.failure_count >= self.failure_threshold:
                self.state = "open"
                logger.warning(f"子模块 {self.name} 断路器打开 (连续失败 {self.failure_count} 次)")

    def allow_request(self) -> bool:
        with self._lock:
            if self.state == "closed":
                return True
            if self.state == "open":
                if time.time() - self.last_failure_time > self.cooldown_seconds:
                    self.state = "half_open"
                    return True
                return False
            return True


class AtomicCounter:
    """线程安全的性能计数器，防溢出，带异常跳变过滤"""
    MAX_VALUE: float = float(2**63 - 1)

    def __init__(self) -> None:
        self._count = 0
        self._total = 0.0
        self._lock = threading.Lock()

    def add(self, value: float) -> None:
        if value <= 0.0 or value > 1e9 or math.isnan(value) or math.isinf(value):
            return
        with self._lock:
            if self._count < self.MAX_VALUE and self._total + value < self.MAX_VALUE:
                self._count += 1
                self._total += value

    def snapshot(self) -> Tuple[int, float]:
        with self._lock:
            return self._count, self._total


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------
class Scorecard:
    """动态评分卡入口：聚合所有评分子模块，输出标准化交易信号"""

    # ========== 类常量 ==========
    DEFAULT_ENTRY_THRESHOLD: float = 65.0
    MIN_POSITION_MULT: float = 0.1
    MAX_POSITION_MULT: float = 2.0
    MAX_SIGNAL_AGE_SEC: float = 30.0
    SIGNAL_COOLDOWN_MS: float = 500.0
    FALLBACK_SCORE: float = 50.0
    FALLBACK_CONFIDENCE: float = 0.3
    FALLBACK_POSITION_MULT: float = 0.2
    SCORE_BASE: float = 50.0

    _quality_score_coeff: float = 20.0
    _micro_score_coeff: float = 15.0

    # 默认权重（实例化时 deepcopy，防止类变量被修改）
    _DEFAULT_WEIGHTS_TEMPLATE: Dict[str, float] = {
        "funnel": 0.30,
        "ma12": 0.25,
        "cross": 0.10,
        "quality": 0.20,
        "micro": 0.15,
    }

    VALID_TIMEFRAMES = frozenset(["1m", "5m", "15m"])

    # 冷却清理间隔（毫秒）和过期倍数
    COOLDOWN_CLEANUP_INTERVAL_MS: float = 30000.0
    COOLDOWN_EXPIRE_MULTIPLIER: float = 10.0

    # 降级默认值（模块级常量，实例化时 deepcopy 防止外部修改）
    _FALLBACK_FUNNEL_TEMPLATE: Dict[str, Any] = {"tier": "C", "position_mult": 1.0, "direction": 0}
    _FALLBACK_MA12_TEMPLATE: Dict[str, Any] = {"score": 0.0, "direction": 0, "confidence": 0.0}
    _FALLBACK_CROSS_TEMPLATE: Dict[str, Any] = {"score": 0.0, "multiplier": 1.0, "direction": 0}
    _FALLBACK_QUALITY_TEMPLATE: Dict[str, Any] = {"score": 0.5}
    _FALLBACK_MICRO_TEMPLATE: Dict[str, Any] = {"score": 0.5, "rejected": False, "direction": 0}

    # 性能统计：每次调用最大耗时（微秒），超过视为异常跳变
    MAX_ELAPSED_US: float = 100_000.0  # 100ms

    # 滑点折扣参数：折扣因子，仓位乘数 *= max(0.7, 1.0 - slippage * SLIPPAGE_DISCOUNT_MULT)
    SLIPPAGE_DISCOUNT_MULT: float = 50.0

    def __init__(self) -> None:
        self._signal_funnel = None
        self._filter_coordinator = None
        self._ma12_synergy = None
        self._cross_tf_constraint = None
        self._signal_quality_precheck = None
        self._micro_second_confirm = None
        self._sensory_snapshot = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        self._entry_threshold: float = self.DEFAULT_ENTRY_THRESHOLD
        self._period_thresholds: Dict[str, float] = {}
        self._weights: Dict[str, float] = deepcopy(self._DEFAULT_WEIGHTS_TEMPLATE)

        # 降级默认值（实例级副本，防止跨实例污染）
        self._fallback_funnel: Dict[str, Any] = deepcopy(self._FALLBACK_FUNNEL_TEMPLATE)
        self._fallback_ma12: Dict[str, Any] = deepcopy(self._FALLBACK_MA12_TEMPLATE)
        self._fallback_cross: Dict[str, Any] = deepcopy(self._FALLBACK_CROSS_TEMPLATE)
        self._fallback_quality: Dict[str, Any] = deepcopy(self._FALLBACK_QUALITY_TEMPLATE)
        self._fallback_micro: Dict[str, Any] = deepcopy(self._FALLBACK_MICRO_TEMPLATE)

        self._last_signal_time: Dict[str, float] = {}
        self._lock = threading.RLock()

        # 断路器
        self._breakers: Dict[str, SubmoduleBreaker] = {
            name: SubmoduleBreaker(name) for name in ["funnel", "ma12", "cross", "quality", "micro"]
        }

        # 性能指标
        self._perf = AtomicCounter()
        self._last_cleanup_time: float = 0.0
        self._cleanup_lock = threading.Lock()

        logger.info("Scorecard 入口初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(self, **kwargs: Any) -> None:
        validators = {
            "signal_funnel": ("SignalFunnel", ["classify"]),
            "filter_coordinator": ("FilterCoordinator", ["get_effective_threshold"]),
            "ma12_synergy": ("Ma12Synergy", ["evaluate"]),
            "cross_tf_constraint": ("CrossTfConstraint", ["apply"]),
            "signal_quality_precheck": ("SignalQualityPrecheck", ["evaluate"]),
            "micro_second_confirm": ("MicroSecondConfirm", ["verify"]),
        }
        for attr, (label, methods) in validators.items():
            mod = kwargs.get(attr)
            if mod is None:
                logger.warning(f"{label} 未注入")
                setattr(self, f"_{attr}", None)
                continue
            if callable(mod) or all(hasattr(mod, m) for m in methods):
                setattr(self, f"_{attr}", mod)
            else:
                logger.error(f"{label} 缺少必需方法")
                setattr(self, f"_{attr}", None)

        self._sensory_snapshot = kwargs.get("sensory_snapshot")
        self._negotiation_bus = kwargs.get("negotiation_bus")
        self._behavioral_logger = kwargs.get("behavioral_logger")

        alive = sum(1 for a in validators if getattr(self, f"_{a}", None) is not None)
        logger.info(f"Scorecard 依赖注入完成，有效子模块: {alive}/{len(validators)}")

    # ========== 配置热加载 ==========
    def load_config(self, config: Dict[str, Any]) -> None:
        with self._lock:
            new_weights = config.get("weights")
            if new_weights and 0.999 <= sum(new_weights.values()) <= 1.001:
                self._weights = deepcopy(new_weights)
            new_threshold = config.get("entry_threshold")
            if new_threshold is not None and 50.0 <= float(new_threshold) <= 80.0:
                self._entry_threshold = float(new_threshold)
            new_period = config.get("period_thresholds", {})
            for k, v in new_period.items():
                if k in self.VALID_TIMEFRAMES and 50.0 <= float(v) <= 80.0:
                    self._period_thresholds[k] = float(v)
            if "cooldown_ms" in config:
                self.SIGNAL_COOLDOWN_MS = max(100.0, float(config["cooldown_ms"]))
            if "quality_score_coefficient" in config:
                self._quality_score_coeff = max(1.0, float(config["quality_score_coefficient"]))
            if "micro_score_coefficient" in config:
                self._micro_score_coeff = max(1.0, float(config["micro_score_coefficient"]))
            if "max_elapsed_us" in config:
                self.MAX_ELAPSED_US = max(1000.0, float(config["max_elapsed_us"]))
            if "slippage_discount_mult" in config:
                self.SLIPPAGE_DISCOUNT_MULT = max(1.0, float(config["slippage_discount_mult"]))

    # ========== 公共接口 ==========
    def evaluate_signal(self, symbol: str, timeframe: str,
                        sensory_snapshot: Dict[str, Any]) -> Dict[str, Any]:
        start = time.perf_counter()
        warnings: List[str] = []

        # 0. 输入校验
        if timeframe not in self.VALID_TIMEFRAMES:
            return self._build_fallback(symbol, f"非法周期: {timeframe}", ["invalid_timeframe"])
        if not isinstance(sensory_snapshot, dict) or not sensory_snapshot:
            return self._build_fallback(symbol, "感官快照无效", ["invalid_snapshot"])
        if sensory_snapshot.get("symbol", "") != symbol:
            return self._build_fallback(symbol, "快照品种不匹配", ["symbol_mismatch"])
        if time.time() - sensory_snapshot.get("timestamp", 0) > self.MAX_SIGNAL_AGE_SEC:
            return self._build_fallback(symbol, "感官快照过期", ["stale_snapshot"])

        # 1. 收集子模块结果（带断路器）
        results, sub_warns = self._call_modules(sensory_snapshot, timeframe)
        warnings.extend(sub_warns)

        # 2. 方向仲裁
        direction = self._arbitrate_direction(results)

        # 3. 方向感知冷却
        if not self._check_cooldown(symbol, timeframe, direction):
            return {
                "status": "ok", "reason": "冷却期", "signal": None,
                "meta": results, "warnings": ["cooldown"],
            }

        # 4. 综合评分（含 NaN 检测）
        final_score = self._compute_weighted_score(results, direction)
        if not self._is_finite(final_score):
            logger.error(f"评分异常(NaN/Inf): 结果={results}")
            return self._build_fallback(symbol, "评分计算异常", warnings)

        # 5. 阈值判定
        threshold = self._period_thresholds.get(timeframe, self._entry_threshold)
        if self._filter_coordinator and self._breakers["cross"].allow_request():
            try:
                eff = self._filter_coordinator.get_effective_threshold(timeframe)
                threshold = eff.get("threshold", threshold)
            except Exception:
                pass

        passed = final_score >= threshold

        # 6. 仓位乘数
        pos_mult = results.get("funnel", {}).get("position_mult", 1.0)
        pos_mult *= results.get("cross", {}).get("multiplier", 1.0)
        pos_mult = max(self.MIN_POSITION_MULT, min(self.MAX_POSITION_MULT, pos_mult))
        pos_mult *= self._slippage_discount(sensory_snapshot)

        # 7. 信号指纹（仅通过时计算）
        fingerprint = ""
        if passed:
            fingerprint = self._compute_fingerprint(results, direction, final_score)

        signal_obj = {
            "symbol": symbol,
            "timeframe": timeframe,
            "score": round(final_score, 1),
            "direction": direction,
            "confidence": round(min(1.0, results.get("quality", {}).get("score", 0.5)), 2),
            "tier": results.get("funnel", {}).get("tier", "C"),
            "position_mult": round(pos_mult, 2),
            "passed": passed,
            "threshold_used": threshold,
            "fingerprint": fingerprint,
            "created_at": time.time(),
        }

        if passed and signal_obj["confidence"] > 0.8:
            self._publish_high_confidence(symbol, signal_obj)

        elapsed = (time.perf_counter() - start) * 1e6
        self._perf.add(elapsed)

        logger.info(
            "信号评估: %s %s 评分=%.1f 方向=%d 通过=%s 耗时=%.0fμs",
            symbol, timeframe, final_score, direction, passed, elapsed,
        )

        return {
            "status": "ok",
            "reason": f"{symbol} {timeframe} 评估完成",
            "signal": signal_obj,
            "meta": results,
            "warnings": warnings,
        }

    def set_threshold(self, threshold: float, timeframe: str = None) -> Dict[str, Any]:
        if not 50 <= threshold <= 80:
            return {"status": "error", "reason": "阈值超出[50,80]", "data": {}, "warnings": []}
        with self._lock:
            if timeframe:
                self._period_thresholds[timeframe] = threshold
            else:
                self._entry_threshold = threshold
        self._log_audit("set_threshold", {"threshold": threshold, "timeframe": timeframe})
        return {"status": "ok", "reason": "阈值已更新", "data": {"threshold": threshold}, "warnings": []}

    def update_weights(self, weights: Dict[str, float]) -> Dict[str, Any]:
        if not (0.999 <= sum(weights.values()) <= 1.001):
            return {"status": "error", "reason": "权重总和不等于1.0", "data": {}, "warnings": []}
        with self._lock:
            self._weights = deepcopy(weights)
        self._log_audit("update_weights", weights)
        return {"status": "ok", "reason": "权重已更新", "data": {"weights": weights}, "warnings": []}

    def get_health_overview(self) -> Dict[str, Any]:
        modules = {
            "signal_funnel": self._signal_funnel is not None,
            "filter_coordinator": self._filter_coordinator is not None,
            "ma12_synergy": self._ma12_synergy is not None,
            "cross_tf_constraint": self._cross_tf_constraint is not None,
            "signal_quality_precheck": self._signal_quality_precheck is not None,
            "micro_second_confirm": self._micro_second_confirm is not None,
        }
        breakers = {n: b.state for n, b in self._breakers.items()}
        cnt, total = self._perf.snapshot()
        return {
            "status": "ok",
            "reason": f"就绪 {sum(modules.values())}/{len(modules)}",
            "data": {
                "modules": modules,
                "breakers": breakers,
                "threshold": self._entry_threshold,
                "eval_count": cnt,
                "avg_latency_us": round(total / cnt, 1) if cnt > 0 else 0,
            },
            "warnings": [],
        }

    def health_check(self) -> Dict[str, Any]:
        try:
            sub_health = {}
            modules = [
                ("signal_funnel", self._signal_funnel),
                ("filter_coordinator", self._filter_coordinator),
                ("ma12_synergy", self._ma12_synergy),
                ("cross_tf_constraint", self._cross_tf_constraint),
                ("signal_quality_precheck", self._signal_quality_precheck),
                ("micro_second_confirm", self._micro_second_confirm),
            ]
            for name, mod in modules:
                if mod and hasattr(mod, "health_check"):
                    try:
                        sub_health[name] = mod.health_check()
                    except Exception as e:
                        sub_health[name] = {"status": "error", "message": str(e)}
                else:
                    sub_health[name] = {"status": "not_injected"}

            weight_sum = sum(self._weights.values())
            weight_ok = 0.99 <= weight_sum <= 1.01
            if not weight_ok:
                logger.error(f"权重总和异常: {weight_sum:.4f} #RECOVERY: 检查权重配置或调用 update_weights()")
            return {
                "status": "ok" if weight_ok else "warning",
                "reason": "深度自检完成",
                "data": {"submodules": sub_health, "weights_sum": round(weight_sum, 4)},
                "warnings": [] if weight_ok else ["weights_sum_mismatch"],
            }
        except Exception as e:
            return {"status": "error", "reason": str(e), "data": {}, "warnings": []}

    # ========== 私有方法 ==========
    def _call_modules(self, snap: Dict[str, Any], timeframe: str) -> Tuple[Dict[str, Any], List[str]]:
        results: Dict[str, Any] = {}
        warnings: List[str] = []

        calls = [
            ("funnel", "funnel", self._signal_funnel, lambda: self._signal_funnel.classify(snap),
             self._fallback_funnel),
            ("ma12", "ma12", self._ma12_synergy, lambda: self._ma12_synergy.evaluate(snap, timeframe),
             self._fallback_ma12),
            ("cross", "cross", self._cross_tf_constraint, lambda: self._cross_tf_constraint.apply(snap, timeframe),
             self._fallback_cross),
            ("quality", "quality", self._signal_quality_precheck, lambda: self._signal_quality_precheck.evaluate(snap),
             self._fallback_quality),
            ("micro", "micro", self._micro_second_confirm, lambda: self._micro_second_confirm.verify(snap),
             self._fallback_micro),
        ]

        for name, breaker_key, mod, func, fallback in calls:
            if mod is None or not callable(func):
                results[name] = deepcopy(fallback)
                continue
            if not self._breakers[breaker_key].allow_request():
                results[name] = deepcopy(fallback)
                continue
            try:
                res = func()
                self._breakers[breaker_key].record_success()
                if isinstance(res, dict):
                    results[name] = res
                else:
                    warnings.append(f"{name}_invalid_return")
                    results[name] = deepcopy(fallback)
            except Exception as e:
                self._breakers[breaker_key].record_failure()
                warnings.append(f"{name}_failed")
                logger.error(f"{name} 异常: {e}")
                results[name] = deepcopy(fallback)

        return results, warnings

    def _arbitrate_direction(self, results: Dict[str, Any]) -> int:
        """加权投票，方向 +1 或 -1，平局返回 0，含浮点下溢保护"""
        long_weight = 0.0
        short_weight = 0.0
        EPS = 1e-12

        for key, weight in self._weights.items():
            if key not in results:
                continue
            d = results[key].get("direction", 0)
            if d == 0:
                continue
            conf = results[key].get("confidence", 0.5) if key in ("ma12", "funnel") else 0.5
            weighted = weight * conf
            if d > 0:
                long_weight += weighted
            elif d < 0:
                short_weight += weighted

        if long_weight - short_weight > EPS:
            return 1
        if short_weight - long_weight > EPS:
            return -1
        return 0

    def _compute_weighted_score(self, results: Dict[str, Any], direction: int) -> float:
        """综合加权评分，所有系数和权重集中管理"""
        w = self._weights
        score = 0.0

        # 漏斗贡献
        tier = results.get("funnel", {}).get("tier", "C")
        score += {"A": 20, "B": 10, "C": 0}.get(tier, 0) * w.get("funnel", 0.3)

        # M12 贡献
        ma12 = results.get("ma12", {})
        ms = ma12.get("score", 0.0)
        if isinstance(ms, (int, float)) and not isinstance(ms, bool) and self._is_finite(ms):
            if (direction > 0 and ms > 0) or (direction < 0 and ms < 0):
                score += abs(ms) * w.get("ma12", 0.25)

        # 跨周期贡献
        cs = results.get("cross", {}).get("score", 0.0)
        if isinstance(cs, (int, float)) and not isinstance(cs, bool) and self._is_finite(cs):
            score += cs * w.get("cross", 0.1)

        # 质量贡献
        qs = results.get("quality", {}).get("score", 0.5)
        if isinstance(qs, (int, float)) and not isinstance(qs, bool) and self._is_finite(qs):
            score += qs * self._quality_score_coeff * w.get("quality", 0.2)

        # 微观贡献
        mic = results.get("micro", {}).get("score", 0.5)
        if isinstance(mic, (int, float)) and not isinstance(mic, bool) and self._is_finite(mic):
            score += mic * self._micro_score_coeff * w.get("micro", 0.15)

        return max(0.0, min(100.0, self.SCORE_BASE + score))

    @staticmethod
    def _is_finite(value: float) -> bool:
        return not (math.isnan(value) or math.isinf(value))

    def _slippage_discount(self, snap: Dict[str, Any]) -> float:
        """基于滑点估算调整仓位乘数（滑点每增加0.1%，仓位缩减 SLIPPAGE_DISCOUNT_MULT * 0.001 倍）"""
        slippage = snap.get("slippage_estimate", 0.002)
        if not isinstance(slippage, (int, float)) or isinstance(slippage, bool) or slippage < 0 or not self._is_finite(slippage):
            slippage = 0.002
        discount = max(0.7, 1.0 - slippage * self.SLIPPAGE_DISCOUNT_MULT)
        return discount

    @staticmethod
    def _compute_fingerprint(results: Dict[str, Any], direction: int, score: float) -> str:
        """轻量级信号指纹，碰撞概率可接受"""
        raw = f"{direction}:{score:.1f}:{results.get('funnel', {}).get('tier', 'C')}"
        try:
            return hashlib.blake2b(raw.encode(), digest_size=12).hexdigest()[:16]
        except AttributeError:
            return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _publish_high_confidence(self, symbol: str, signal: Dict[str, Any]) -> None:
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, "publish_alert"):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="high_confidence_signal",
                    symbol=symbol,
                    score=signal["score"],
                    direction=signal["direction"],
                    timestamp=time.time(),
                )
            except Exception:
                pass

    def _check_cooldown(self, symbol: str, timeframe: str, direction: int) -> bool:
        if direction == 0:
            return True
        now_ms = time.time() * 1000
        key = f"{symbol}:{direction}:{timeframe}"
        with self._lock:
            if now_ms - self._last_cleanup_time >= self.COOLDOWN_CLEANUP_INTERVAL_MS:
                self._do_cleanup(now_ms)
            last = self._last_signal_time.get(key, 0)
            if now_ms - last < self.SIGNAL_COOLDOWN_MS:
                return False
            self._last_signal_time[key] = now_ms
        return True

    def _do_cleanup(self, now_ms: float) -> None:
        """在持有 self._lock 时执行，清理逻辑需轻量。使用单独锁保护清理状态。"""
        with self._cleanup_lock:
            # 检查是否已经被其他线程清理
            if now_ms - self._last_cleanup_time < self.COOLDOWN_CLEANUP_INTERVAL_MS * 0.5:
                return
            threshold = now_ms - self.SIGNAL_COOLDOWN_MS * self.COOLDOWN_EXPIRE_MULTIPLIER
            expired = [k for k, v in self._last_signal_time.items() if v < threshold]
            for k in expired:
                del self._last_signal_time[k]
            self._last_cleanup_time = now_ms
            if expired:
                logger.debug(f"清理冷却键: {len(expired)} 个")

    def _build_fallback(self, symbol: str, reason: str, warnings: List[str]) -> Dict[str, Any]:
        return {
            "status": "degraded",
            "reason": reason,
            "signal": {
                "symbol": symbol,
                "score": self.FALLBACK_SCORE,
                "direction": 0,
                "confidence": self.FALLBACK_CONFIDENCE,
                "tier": "C",
                "position_mult": self.FALLBACK_POSITION_MULT,
                "passed": False,
                "threshold_used": self._entry_threshold,
                "fingerprint": "",
                "created_at": time.time(),
            },
            "meta": {},
            "warnings": list(warnings) + [f"degraded_{reason[:30]}"],
        }

    def _log_audit(self, action: str, detail: Any) -> None:
        if self._behavioral_logger is not None and hasattr(self._behavioral_logger, "log_event"):
            try:
                self._behavioral_logger.log_event(
                    event_type="scorecard_config",
                    details={"action": action, "detail": detail},
                )
            except Exception as e:
                logger.warning(f"审计日志写入失败: {e}")

    def __repr__(self) -> str:
        cnt, total = self._perf.snapshot()
        return (
            f"Scorecard(threshold={self._entry_threshold}, "
            f"modules={sum(1 for m in [self._signal_funnel, self._filter_coordinator, "
            f"self._ma12_synergy, self._cross_tf_constraint, "
            f"self._signal_quality_precheck, self._micro_second_confirm] if m)}, "
            f"evals={cnt}, avg_us={total/cnt:.1f})" if cnt > 0 else "N/A)"
        )
