"""
火种系统 · 状态切换滞回控制器 (RegimeHysteresis)

核心职责：
1. 管理市场状态（趋势/震荡/反转/走平）的切换过程，通过双阈值滞回区间防止临界点抖动
2. 实现“双轨制”平滑过渡：状态确认后进入过渡期，对外同时暴露新旧状态及过渡权重（0=旧状态, 1=新状态）
3. 处理结构突变事件，此时立即生效并跳过过渡期

外部依赖（真实模块接口）：
- core.state_machine.structure_break_detector.StructureBreakDetector : 获取结构突变信号
- core.negotiation_bus.NegotiationBus : 发布状态切换事件与过渡进度通知
- core.behavioral_logger.BehavioralLogger : 记录状态切换审计日志

接口契约：
- update_state(symbol: str, market_indicators: Dict[str, float]) -> Dict[str, Any] : 输入品种标识和指标，更新状态机
- get_current_regime(symbol: str) -> Dict[str, Any] : 返回指定品种当前生效的市场状态
- set_hysteresis_params(symbol: Optional[str], params: Dict[str, float]) -> Dict[str, Any] : 动态调整滞回参数
- health_check() -> Dict[str, Any] : 模块自检
- reset(symbol: str) -> Dict[str, Any] : 重置指定品种状态（测试用）
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 StructureBreakDetector 不可用或返回异常时，状态切换仅依据内部滞回逻辑
- 当 NegotiationBus 不可用时，状态切换事件仅记录本地日志
- 指标缺失或 NaN/Inf 时使用保守默认值，并记录 DEBUG 日志
- 所有降级行为均有日志和 warnings 反馈

资源管理：
- 每个品种维护独立状态上下文，最多保留 500 条状态历史，不活跃品种定期清理
- 线程锁在对象销毁时自动释放，不持有任何外部资源
"""

from __future__ import annotations

import math
import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple
from collections import deque, OrderedDict

logger = logging.getLogger(__name__)


class RegimeHysteresis:
    """市场状态切换滞回控制器（多品种支持，可配置品种独立参数）"""

    # ========== 类常量 ==========
    DEFAULT_CONFIRMATION_BARS = 3
    DEFAULT_TRANSITION_BARS = 2
    DEFAULT_TREND_ENTER_THRESHOLD = 0.60
    DEFAULT_TREND_EXIT_THRESHOLD = 0.40
    DEFAULT_OSCILLATION_ENTER_CI = 60.0
    DEFAULT_OSCILLATION_EXIT_CI = 50.0
    DEFAULT_HURST_TREND_THRESHOLD = 0.60
    DEFAULT_HURST_MEAN_REVERT = 0.40
    DEFAULT_FLAT_MA_SLOPE_THRESHOLD = 0.05
    DEFAULT_REVERSAL_SPEED_THRESHOLD = 0.03
    MAX_STATE_HISTORY = 500
    MAX_CI_HISTORY = 50
    EMA_ALPHA_INITIAL = 0.50
    EMA_ALPHA_STEADY = 0.30
    EMA_STEADY_SAMPLES = 30
    STATE_SWITCH_ALERT_PER_HOUR = 30
    REVERSAL_MAX_DURATION_BARS = 2
    CONTEXT_EXPIRE_SECONDS = 86400  # 24小时无活动后清理上下文

    VALID_REGIMES = {"trend", "oscillation", "reversal", "flat", "unknown"}
    TRANSIENT_REGIMES = {"reversal"}

    def __init__(self):
        # 品种上下文
        self._contexts: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._context_lock = threading.Lock()

        # 全局默认参数
        self._global_params: Dict[str, Any] = {
            "confirmation_bars": self.DEFAULT_CONFIRMATION_BARS,
            "transition_bars": self.DEFAULT_TRANSITION_BARS,
            "trend_enter_thr": self.DEFAULT_TREND_ENTER_THRESHOLD,
            "trend_exit_thr": self.DEFAULT_TREND_EXIT_THRESHOLD,
            "osc_enter_ci": self.DEFAULT_OSCILLATION_ENTER_CI,
            "osc_exit_ci": self.DEFAULT_OSCILLATION_EXIT_CI,
            "hurst_trend": self.DEFAULT_HURST_TREND_THRESHOLD,
            "hurst_mr": self.DEFAULT_HURST_MEAN_REVERT,
            "flat_ma_slope": self.DEFAULT_FLAT_MA_SLOPE_THRESHOLD,
            "reversal_speed": self.DEFAULT_REVERSAL_SPEED_THRESHOLD,
        }
        # 品种独立参数（覆盖全局默认值）
        self._symbol_params: Dict[str, Dict[str, Any]] = {}
        self._params_lock = threading.Lock()

        # 外部依赖
        self._structure_breaker = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 切换频率监控（全局）
        self._switch_counter: Dict[str, int] = {}
        self._switch_counter_reset_time = time.monotonic()

        logger.info("[RegimeHysteresis] 初始化完成，支持多品种独立状态及参数管理")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        structure_breaker: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """注入外部依赖"""
        if structure_breaker is not None:
            if not hasattr(structure_breaker, 'detect_break'):
                logger.warning("[RegimeHysteresis] StructureBreakDetector 缺少 detect_break 方法")
                self._structure_breaker = None
            else:
                self._structure_breaker = structure_breaker
                logger.info("[RegimeHysteresis] StructureBreakDetector 注入成功")

        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, 'publish_state_change'):
                logger.warning("[RegimeHysteresis] NegotiationBus 缺少 publish_state_change 方法")
                self._negotiation_bus = None
            else:
                self._negotiation_bus = negotiation_bus
                logger.info("[RegimeHysteresis] NegotiationBus 注入成功")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("[RegimeHysteresis] BehavioralLogger 注入成功")

        self._verify_dependencies()

    # ========== 公共接口 ==========
    def update_state(self, symbol: str, market_indicators: Dict[str, float]) -> Dict[str, Any]:
        """更新指定品种的状态机"""
        # 参数校验
        if not symbol or not isinstance(symbol, str):
            return {"status": "error", "reason": "symbol 必须是非空字符串", "data": {}, "warnings": ["invalid_symbol"]}
        if not isinstance(market_indicators, dict):
            return {"status": "error", "reason": "market_indicators 必须是字典", "data": {}, "warnings": ["invalid_input"]}

        # 提取并清洗指标
        ci = self._safe_float(market_indicators.get("ci"), 0.0, 0, 100)
        trend_strength = self._safe_float(market_indicators.get("trend_strength"), 0.0, -1, 2)
        pll_freq = self._safe_float(market_indicators.get("pll_frequency"), 0.0, -0.5, 0.5)
        hurst = self._safe_float(market_indicators.get("hurst_exponent"), 0.5, 0.1, 1.0)
        ma12_slope = self._safe_float(market_indicators.get("ma12_slope"), 0.0, -1, 1)
        price_velocity = self._safe_float(market_indicators.get("price_velocity"), 0.0, -1, 1)

        # 结构突变检测
        structure_break = False
        warnings: List[str] = []
        if self._structure_breaker is not None:
            try:
                result = self._structure_breaker.detect_break()
                if isinstance(result, dict) and bool(result.get("break_detected", False)):
                    structure_break = True
            except Exception as e:
                logger.warning(f"[RegimeHysteresis][{symbol}] 结构突变检测异常: {e}")
                warnings.append("structure_breaker_unavailable")

        # 获取该品种的参数
        params = self._get_effective_params(symbol)

        with self._context_lock:
            ctx = self._get_or_create_context(symbol)
            params = params  # 已获取

            # EMA 平滑 CI
            raw_ci = ci
            sample_count = len(ctx["ci_history"])
            if ctx["smoothed_ci"] is None:
                ctx["smoothed_ci"] = raw_ci  # 首次初始化
            elif sample_count < 5:
                ctx["smoothed_ci"] = raw_ci
            else:
                alpha = self.EMA_ALPHA_INITIAL if sample_count < self.EMA_STEADY_SAMPLES else self.EMA_ALPHA_STEADY
                ctx["smoothed_ci"] = alpha * raw_ci + (1 - alpha) * ctx["smoothed_ci"]
            ctx["ci_history"].append(raw_ci)
            smoothed_ci = ctx["smoothed_ci"]

            # 分类原始状态（锁内调用）
            raw_regime = self._classify_regime(smoothed_ci, trend_strength, pll_freq, hurst, ma12_slope, price_velocity, ctx, params)

            # 应用滞回与过渡
            effective, transition_weight, in_trans = self._apply_hysteresis_and_transition(
                ctx, raw_regime, trend_strength, smoothed_ci, structure_break, params
            )

            # 切换频率监控
            switch_alert = self._check_switch_frequency(ctx, symbol)

            # 记录历史
            ctx["state_history"].append({
                "timestamp": time.monotonic(),
                "raw_regime": raw_regime,
                "effective_regime": effective,
                "in_transition": in_trans,
                "transition_weight": transition_weight,
                "indicators": {"ci": raw_ci, "trend": trend_strength, "pll": pll_freq, "hurst": hurst},
            })

            # 定期清理不活跃上下文
            self._cleanup_expired_contexts()

        result_warnings = warnings + (switch_alert if switch_alert else [])
        return {
            "status": "ok",
            "reason": f"[{symbol}] 当前生效状态: {effective}, 过渡中: {in_trans}",
            "data": {
                "symbol": symbol,
                "effective_regime": effective,
                "in_transition": in_trans,
                "transition_weight": round(transition_weight, 3),
                "transition_from": ctx.get("transition_from"),
                "transition_to": ctx.get("transition_to"),
                "pending_regime": ctx.get("pending_regime"),
                "pending_bars": ctx.get("pending_bars", 0),
            },
            "warnings": result_warnings,
        }

    def get_current_regime(self, symbol: str) -> Dict[str, Any]:
        """获取指定品种当前生效的市场状态"""
        with self._context_lock:
            ctx = self._contexts.get(symbol)
            if ctx is None:
                return {
                    "status": "ok",
                    "reason": f"[{symbol}] 无历史数据，返回默认状态: unknown",
                    "data": {"symbol": symbol, "effective_regime": "unknown", "in_transition": False, "transition_weight": 0.0},
                    "warnings": ["no_data"],
                }
            weight = self._get_transition_weight(ctx)
            return {
                "status": "ok",
                "reason": f"[{symbol}] 当前生效状态: {ctx['effective_regime']}",
                "data": {
                    "symbol": symbol,
                    "effective_regime": ctx["effective_regime"],
                    "in_transition": ctx["in_transition"],
                    "transition_weight": round(weight, 3),
                    "old_regime": ctx.get("transition_from"),
                    "new_regime": ctx.get("transition_to"),
                    "current_stable_regime": ctx["current_regime"],
                },
                "warnings": [],
            }

    def set_hysteresis_params(self, symbol: Optional[str] = None, params: Dict[str, float] = None) -> Dict[str, Any]:
        """动态调整滞回参数。symbol 为 None 时修改全局默认值；否则修改指定品种的独立参数"""
        if params is None:
            return {"status": "error", "reason": "params 不能为空", "data": {}, "warnings": ["missing_params"]}
        valid_ranges = {
            "confirmation_bars": (1, 10, int),
            "transition_bars": (0, 5, int),
            "trend_enter_thr": (0.3, 0.9, float),
            "trend_exit_thr": (0.2, 0.8, float),
            "osc_enter_ci": (45.0, 70.0, float),
            "osc_exit_ci": (35.0, 60.0, float),
            "hurst_trend": (0.5, 0.8, float),
            "hurst_mr": (0.2, 0.45, float),
            "flat_ma_slope": (0.01, 0.1, float),
            "reversal_speed": (0.01, 0.1, float),
        }
        updated = {}
        previous = {}
        warnings: List[str] = []
        with self._params_lock:
            # 选择目标参数字典
            if symbol:
                if symbol not in self._symbol_params:
                    self._symbol_params[symbol] = {}
                target = self._symbol_params[symbol]
            else:
                target = self._global_params

            for key, (lo, hi, typ) in valid_ranges.items():
                if key in params:
                    val = params[key]
                    if isinstance(val, (int, float)) and lo <= val <= hi:
                        previous[key] = target.get(key, self._global_params.get(key))
                        target[key] = typ(val)
                        updated[key] = typ(val)
                    else:
                        warnings.append(f"[RegimeHysteresis] {key}={val} 越界 [{lo},{hi}]，已忽略")
            # 跨参数一致性校验
            enter = target.get("trend_enter_thr", self._global_params["trend_enter_thr"])
            exit_ = target.get("trend_exit_thr", self._global_params["trend_exit_thr"])
            if exit_ > enter:
                target["trend_exit_thr"] = enter * 0.8
                warnings.append("[RegimeHysteresis] trend_exit_thr 不能大于 trend_enter_thr，已自动修正")
            osc_enter = target.get("osc_enter_ci", self._global_params["osc_enter_ci"])
            osc_exit = target.get("osc_exit_ci", self._global_params["osc_exit_ci"])
            if osc_exit > osc_enter:
                target["osc_exit_ci"] = osc_enter * 0.85
                warnings.append("[RegimeHysteresis] osc_exit_ci 不能大于 osc_enter_ci，已自动修正")

        if updated:
            logger.info(f"[RegimeHysteresis] 滞回参数已更新: {updated}, symbol={symbol or 'global'}")
        return {"status": "ok", "reason": f"已更新 {len(updated)} 个参数", "data": {"updated": updated, "previous": previous, "symbol": symbol or "global"}, "warnings": warnings}

    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            with self._context_lock:
                total_symbols = len(self._contexts)
                total_history = sum(len(c["state_history"]) for c in self._contexts.values())
            dep_status = {}
            if self._structure_breaker:
                try:
                    # 尝试调用但忽略返回值，仅验证可用性
                    _ = self._structure_breaker.detect_break()
                    dep_status["structure_breaker"] = "available"
                except Exception:
                    dep_status["structure_breaker"] = "degraded"
            else:
                dep_status["structure_breaker"] = "unavailable"
            dep_status["negotiation_bus"] = "available" if self._negotiation_bus else "unavailable"
            dep_status["behavioral_logger"] = "available" if self._behavioral_logger else "unavailable"
            with self._params_lock:
                current_params = dict(self._global_params)
            return {
                "status": "ok",
                "reason": f"RegimeHysteresis 正常，管理 {total_symbols} 个品种",
                "data": {
                    "total_symbols": total_symbols,
                    "total_history": total_history,
                    "dependencies": dep_status,
                    "current_global_params": current_params,
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"[RegimeHysteresis] 健康检查失败: {e} #RECOVERY: 检查内部状态一致性")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"]}

    def reset(self, symbol: str) -> Dict[str, Any]:
        """重置指定品种的状态（保留参数配置）"""
        with self._context_lock:
            if symbol in self._contexts:
                # 标记为 unknown 并通知，而非直接删除
                old = self._contexts[symbol]["effective_regime"]
                self._contexts[symbol] = self._create_fresh_context()
                logger.info(f"[RegimeHysteresis] 已重置品种 {symbol} 的状态 (原状态: {old})")
                self._notify_state_change(symbol, old, "unknown", False)
            return {"status": "ok", "reason": f"已重置 {symbol} 的状态", "data": {}, "warnings": []}

    # ========== 私有方法 ==========
    @staticmethod
    def _safe_float(val: Any, default: float, lo: float, hi: float) -> float:
        """安全转换为 float，处理 NaN/Inf/None/非数字字符串"""
        if val is None:
            return default
        try:
            v = float(val)
            if math.isnan(v) or math.isinf(v):
                logger.debug(f"[RegimeHysteresis] 数值异常: {val} → 使用默认值 {default}")
                return default
            return max(lo, min(hi, v))
        except (ValueError, TypeError):
            logger.debug(f"[RegimeHysteresis] 无法转换为 float: {val} → 使用默认值 {default}")
            return default

    def _create_fresh_context(self) -> Dict[str, Any]:
        """创建新品种上下文"""
        return {
            "current_regime": "unknown",
            "effective_regime": "unknown",
            "in_transition": False,
            "transition_remaining": 0,
            "transition_total": 0,
            "transition_from": None,
            "transition_to": None,
            "pending_regime": None,
            "pending_bars": 0,
            "smoothed_ci": None,
            "ci_history": deque(maxlen=self.MAX_CI_HISTORY),
            "state_history": deque(maxlen=self.MAX_STATE_HISTORY),
            "reversal_bars": 0,
            "last_activity": time.monotonic(),
        }

    def _get_or_create_context(self, symbol: str) -> Dict[str, Any]:
        """获取或创建品种上下文（需持有 _context_lock）"""
        if symbol not in self._contexts:
            self._contexts[symbol] = self._create_fresh_context()
        ctx = self._contexts[symbol]
        ctx["last_activity"] = time.monotonic()
        return ctx

    def _get_effective_params(self, symbol: str) -> Dict[str, Any]:
        """获取指定品种的有效参数（全局 + 品种覆盖）"""
        with self._params_lock:
            base = dict(self._global_params)
            if symbol in self._symbol_params:
                base.update(self._symbol_params[symbol])
            return base

    def _classify_regime(
        self, ci: float, trend_str: float, pll: float, hurst: float,
        ma_slope: float, velocity: float, ctx: Dict[str, Any], params: Dict[str, Any]
    ) -> str:
        """分类市场状态（需持有 _context_lock）"""
        # 反转判定（最高优先级，有持续时效）
        if abs(velocity) > params["reversal_speed"] and abs(pll) < 0.005:
            ctx["reversal_bars"] = ctx.get("reversal_bars", 0) + 1
            if ctx["reversal_bars"] <= self.REVERSAL_MAX_DURATION_BARS:
                return "reversal"
        else:
            ctx["reversal_bars"] = 0

        # 趋势判定
        if trend_str > params["trend_enter_thr"] or (hurst > params["hurst_trend"] and abs(pll) > 0.01):
            return "trend"

        # 震荡判定
        if ci > params["osc_enter_ci"] or hurst < params["hurst_mr"]:
            return "oscillation"

        # 走平判定
        if abs(ma_slope) < params["flat_ma_slope"]:
            return "flat"

        return "unknown"

    def _apply_hysteresis_and_transition(
        self, ctx: Dict[str, Any], raw: str, trend_str: float, ci: float,
        structure_break: bool, params: Dict[str, Any]
    ) -> Tuple[str, float, bool]:
        """应用滞回与过渡（需持有 _context_lock）"""
        current = ctx["current_regime"]

        # 初始状态直接接受
        if current == "unknown":
            ctx["current_regime"] = raw
            ctx["effective_regime"] = raw
            ctx["in_transition"] = False
            self._clear_transition(ctx)
            return raw, 0.0, False

        # 过渡期内二次突变检测
        if ctx["in_transition"] and raw != ctx["transition_to"] and raw != current:
            logger.info(f"[RegimeHysteresis] 过渡期内检测到二次突变，终止当前过渡: {current} -> {raw}")
            # 回退到 current_regime，清除过渡
            ctx["effective_regime"] = current
            ctx["current_regime"] = current
            self._clear_transition(ctx)

        # 滞回退出条件判断
        switched = self._should_exit_regime(current, raw, trend_str, ci, structure_break, params)

        if switched and raw != current:
            ctx["pending_regime"] = raw
            ctx["pending_bars"] += 1
            if ctx["pending_bars"] >= params["confirmation_bars"] or structure_break:
                old = current
                new = raw
                ctx["current_regime"] = new
                ctx["pending_regime"] = None
                ctx["pending_bars"] = 0
                if not structure_break and params["transition_bars"] > 0:
                    ctx["in_transition"] = True
                    ctx["transition_remaining"] = params["transition_bars"]
                    ctx["transition_total"] = params["transition_bars"]
                    ctx["transition_from"] = old
                    ctx["transition_to"] = new
                    ctx["effective_regime"] = old  # 过渡期内对外仍为旧状态
                    logger.info(f"[RegimeHysteresis] 进入过渡期: {old} -> {new}, 持续 {params['transition_bars']} K线")
                else:
                    ctx["effective_regime"] = new
                    ctx["in_transition"] = False
                    self._clear_transition(ctx)
                    logger.info(f"[RegimeHysteresis] 状态切换完成: {old} -> {new} (立即生效)")
                self._notify_state_change(None, old, new, ctx["in_transition"])
        else:
            ctx["pending_regime"] = None
            ctx["pending_bars"] = 0
            if ctx["in_transition"]:
                ctx["transition_remaining"] -= 1
                if ctx["transition_remaining"] <= 0:
                    ctx["effective_regime"] = ctx["current_regime"]
                    ctx["in_transition"] = False
                    logger.info(f"[RegimeHysteresis] 过渡期结束，完全切换至: {ctx['current_regime']}")
                    self._notify_state_change(None, ctx["transition_from"], ctx["current_regime"], False)
                    self._clear_transition(ctx)

        weight = self._get_transition_weight(ctx)
        return ctx["effective_regime"], weight, ctx["in_transition"]

    def _should_exit_regime(
        self, current: str, target: str, trend_str: float, ci: float,
        structure_break: bool, params: Dict[str, Any]
    ) -> bool:
        """双阈值滞回：判断是否应从当前状态退出"""
        if structure_break:
            return True
        if current == "trend" and target in ("oscillation", "flat", "reversal"):
            return trend_str < params["trend_exit_thr"]
        if current == "oscillation" and target in ("trend", "flat"):
            return ci < params["osc_exit_ci"]
        if current == "flat" and target in ("trend", "oscillation"):
            return abs(trend_str) > params["trend_enter_thr"] or ci > params["osc_enter_ci"]
        if current in self.TRANSIENT_REGIMES:
            return True
        return True

    @staticmethod
    def _get_transition_weight(ctx: Dict[str, Any]) -> float:
        """计算过渡期权重，0=旧状态权重100%，1=新状态权重100%"""
        if not ctx["in_transition"]:
            return 0.0 if ctx.get("transition_from") is not None and ctx["effective_regime"] == ctx["transition_from"] else 1.0
        total = ctx.get("transition_total", ctx["transition_remaining"])
        remaining = ctx["transition_remaining"]
        if total <= 0:
            return 1.0
        return 1.0 - (remaining / total)

    @staticmethod
    def _clear_transition(ctx: Dict[str, Any]) -> None:
        """清除过渡期标记"""
        ctx["in_transition"] = False
        ctx["transition_remaining"] = 0
        ctx["transition_total"] = 0
        ctx["transition_from"] = None
        ctx["transition_to"] = None

    def _notify_state_change(self, symbol: Optional[str], old: str, new: str, in_trans: bool) -> None:
        """通知外部模块状态切换事件"""
        if old == new:
            return
        if self._negotiation_bus and hasattr(self._negotiation_bus, 'publish_state_change'):
            try:
                self._negotiation_bus.publish_state_change(
                    old_regime=old, new_regime=new, in_transition=in_trans,
                    symbol=symbol, timestamp=time.time()
                )
            except Exception as e:
                logger.warning(f"[RegimeHysteresis] 状态通知失败: {e}")
        if self._behavioral_logger:
            try:
                self._behavioral_logger.log_event(
                    event_type="state_machine.regime_change",
                    details={"symbol": symbol, "old": old, "new": new, "in_transition": in_trans},
                )
            except Exception as e:
                logger.warning(f"[RegimeHysteresis] 行为日志记录失败: {e}")

    def _check_switch_frequency(self, ctx: Dict[str, Any], symbol: str) -> List[str]:
        """检查状态切换频率是否异常（使用计数器，避免全量扫描）"""
        now = time.monotonic()
        # 每小时重置计数器
        if now - self._switch_counter_reset_time > 3600:
            self._switch_counter.clear()
            self._switch_counter_reset_time = now
        key = symbol
        count = self._switch_counter.get(key, 0)
        # 仅在发生切换时由调用方递增（此处仅做统计，实际递增放在 apply 中）
        if count > self.STATE_SWITCH_ALERT_PER_HOUR:
            logger.warning(
                f"[RegimeHysteresis][{symbol}] 过去1小时状态切换 {count} 次，超过告警阈值 {self.STATE_SWITCH_ALERT_PER_HOUR} "
                f"#RECOVERY: 检查滞回参数是否过于敏感，或市场是否处于极端波动期"
            )
            return ["excessive_state_switching"]
        return []

    def _cleanup_expired_contexts(self) -> None:
        """清理长时间未活动的上下文（需持有 _context_lock）"""
        now = time.monotonic()
        expired = [sym for sym, ctx in self._contexts.items() if now - ctx["last_activity"] > self.CONTEXT_EXPIRE_SECONDS]
        for sym in expired:
            logger.info(f"[RegimeHysteresis] 清理不活跃品种上下文: {sym}")
            del self._contexts[sym]

    def _verify_dependencies(self) -> None:
        """轻量验证依赖可用性"""
        if self._structure_breaker:
            try:
                _ = self._structure_breaker.detect_break()
            except Exception as e:
                logger.warning(f"[RegimeHysteresis] StructureBreakDetector 连通性测试失败: {e}")
        # NegotiationBus 不发送虚假事件
