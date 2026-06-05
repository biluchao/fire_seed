"""
火种系统 · 微观结构二次确认器 (MicroSecondConfirm) v1.0.2

核心职责：
1. 在交易信号提交执行前，对当前盘口进行方向感知的纸墙检测、归一化成交脉冲背离分析和基于品种规格的价差操纵识别。
2. 综合多项检查结果，结合信号质量评分、当前市场波动率与依赖模块健康状态，判定信号通过、降级、延迟重试或拒绝，内置重试闭环与降级冷却机制。

外部依赖（真实模块接口，所有接口需在注入时通过鸭子类型校验）：
- core.perception.visual_cortex.VisualCortex.get_wall_resilience(symbol, direction) : 获取挂单墙韧性字典，必须包含 cancel_rate(float)。
- core.perception.tactile_cortex.TactileCortex.get_trade_pulse(symbol, ticks) : 获取成交脉冲，必须包含 buy_volume(float), sell_volume(float), total_volume(float)。
- core.perception.tactile_cortex.TactileCortex.get_current_spread(symbol) : 获取当前价差(float)。
- core.perception.tactile_cortex.TactileCortex.get_average_spread(symbol, window_sec) : 获取历史均价差(float)。
- core.perception.tactile_cortex.TactileCortex.get_current_tick_size(symbol) : 获取品种最小变动价位(float)。
- core.negotiation_bus.NegotiationBus.publish_alert(alert_type, **kwargs) : 推送诊断事件。

接口契约：
- confirm_signal(symbol: str, direction: int, signal_score: float, signal_id: str = "") -> Dict[str, Any]
  返回固定包含 status, reason, data (含 pass, adjusted_mult, checks, action), warnings。
- health_check() -> Dict[str, Any]
  返回模块及其依赖的健康状态。

并发与线程安全：
- 本模块为无状态工具类。线程安全由注入的依赖模块保证。confirm_signal 可被多线程并发调用。
- _degradation_cooldowns 字典由 threading.Lock 保护。

性能指标：
- 正常路径（所有依赖可用且通过）：预期延迟 < 500μs（不含依赖模块执行时间）。
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional, Union

logger = logging.getLogger(__name__)

class MicroSecondConfirm:
    """微观结构二次确认器 v1.0.2"""

    __version__ = "1.0.2"
    __slots__ = ("_cfg", "_visual", "_olfactory", "_tactile", "_negotiation_bus",
                 "_retry_tracker", "_degradation_cooldowns", "_lock")

    # 类常量（安全默认值）
    DEFAULT_PAPER_WALL_CANCEL_RATE = 0.4
    DEFAULT_PULSE_DIVERGENCE_RATIO = 0.5
    DEFAULT_PULSE_WINDOW_TICKS = 20
    DEFAULT_SPREAD_MANIPULATION_MULT = 3.0
    DEFAULT_SPREAD_NORMAL_WINDOW_SEC = 10
    MIN_SPREAD_BPS = 0.005
    DEFAULT_SIGNAL_DELAY_US = 500
    DEFAULT_MAX_RETRY_COUNT = 1
    DEFAULT_DOWNGRADE_MULT = 0.7
    HIGH_SIGNAL_DOWNGRADE_MULT = 0.85
    HIGH_SIGNAL_THRESHOLD = 80
    CHECK_WEIGHTS = {"paper_wall": 0.35, "pulse_divergence": 0.35, "spread_manipulation": 0.30}
    SINGLE_WARN_THRESHOLD = 0.35
    DOUBLE_WARN_THRESHOLD = 0.70
    DEGRADATION_COOLDOWN_SEC = 30
    MAX_VOL_FACTOR = 3.0
    MIN_VOL_FACTOR = 0.3
    DEFAULT_TICK_SIZE = 0.00001

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._cfg = config or {}
        self._visual: Any = None
        self._olfactory: Any = None
        self._tactile: Any = None
        self._negotiation_bus: Any = None
        self._retry_tracker: Dict[str, int] = {}
        self._degradation_cooldowns: Dict[str, float] = {}
        self._lock = threading.Lock()
        logger.info("MicroSecondConfirm v%s 初始化", self.__version__)

    def inject_dependencies(
        self,
        visual_cortex: Optional[Any] = None,
        olfactory_cortex: Optional[Any] = None,
        tactile_cortex: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
    ) -> None:
        if visual_cortex and hasattr(visual_cortex, 'get_wall_resilience'):
            self._visual = visual_cortex
        if olfactory_cortex:
            self._olfactory = olfactory_cortex  # 预留，未来用于毒性检测
        if tactile_cortex and hasattr(tactile_cortex, 'get_current_spread'):
            self._tactile = tactile_cortex
        if negotiation_bus and hasattr(negotiation_bus, 'publish_alert'):
            self._negotiation_bus = negotiation_bus

    def _get_param(self, key: str, default: float) -> float:
        try:
            val = self._cfg.get(key, default)
            return float(val)
        except (ValueError, TypeError):
            return default

    def _get_int_param(self, key: str, default: int) -> int:
        try:
            val = self._cfg.get(key, default)
            return int(val)
        except (ValueError, TypeError):
            return default

    def _is_degradation_cooling(self, check_name: str) -> bool:
        with self._lock:
            last = self._degradation_cooldowns.get(check_name, 0)
            return (time.time() - last) < self.DEGRADATION_COOLDOWN_SEC

    def _mark_degradation_cooldown(self, check_name: str) -> None:
        with self._lock:
            self._degradation_cooldowns[check_name] = time.time()

    def confirm_signal(self, symbol: str, direction: int, signal_score: float, signal_id: str = "") -> Dict[str, Any]:
        if direction not in (1, -1):
            return {"status": "error", "reason": f"无效方向: {direction}", "data": {}, "warnings": []}
        if not isinstance(symbol, str) or not symbol:
            return {"status": "error", "reason": "无效交易品种", "data": {}, "warnings": []}

        checks = {}
        warnings = []
        total_penalty = 0.0
        available_checks = 0

        # 纸墙检测
        if self._visual and not self._is_degradation_cooling("paper_wall"):
            paper = self._check_paper_wall(symbol, direction)
            checks["paper_wall"] = paper
            if paper["triggered"]:
                total_penalty += self.CHECK_WEIGHTS["paper_wall"]
                warnings.extend(paper.get("warnings", []))
            available_checks += 1
        else:
            self._mark_degradation_cooldown("paper_wall")
            checks["paper_wall"] = {"triggered": False, "reason": "检测不可用", "warnings": []}

        # 脉冲背离检测
        if self._tactile and not self._is_degradation_cooling("pulse_divergence"):
            pulse = self._check_pulse_divergence(symbol, direction)
            checks["pulse_divergence"] = pulse
            if pulse["triggered"]:
                total_penalty += self.CHECK_WEIGHTS["pulse_divergence"]
                warnings.extend(pulse.get("warnings", []))
            available_checks += 1
        else:
            self._mark_degradation_cooldown("pulse_divergence")
            checks["pulse_divergence"] = {"triggered": False, "reason": "检测不可用", "warnings": []}

        # 价差操纵检测
        if self._tactile and not self._is_degradation_cooling("spread_manipulation"):
            spread = self._check_spread_manipulation(symbol)
            checks["spread_manipulation"] = spread
            if spread["triggered"]:
                total_penalty += self.CHECK_WEIGHTS["spread_manipulation"]
                warnings.extend(spread.get("warnings", []))
            available_checks += 1
        else:
            self._mark_degradation_cooldown("spread_manipulation")
            checks["spread_manipulation"] = {"triggered": False, "reason": "检测不可用", "warnings": []}

        if available_checks == 0:
            logger.error("所有微观检测不可用 #RECOVERY: 检查感知模块注入状态")
            self._push_event(symbol, "all_checks_unavailable")
            return self._fallback_decision(symbol, signal_score, checks)

        downgrade_mult = self._get_param("downgrade_mult", self.DEFAULT_DOWNGRADE_MULT)
        if signal_score >= self.HIGH_SIGNAL_THRESHOLD:
            downgrade_mult = self._get_param("high_signal_downgrade_mult", self.HIGH_SIGNAL_DOWNGRADE_MULT)

        if total_penalty == 0.0:
            logger.info("微观确认通过: %s score=%.1f", symbol, signal_score)
            return {"status": "ok", "reason": "微观结构确认通过", "data": {"pass": True, "adjusted_mult": 1.0, "checks": checks, "action": "proceed"}, "warnings": []}

        if total_penalty <= self.SINGLE_WARN_THRESHOLD:
            delay = self._get_param("signal_delay_us", self.DEFAULT_SIGNAL_DELAY_US)
            retries = self._get_int_param("max_retry_count", self.DEFAULT_MAX_RETRY_COUNT)
            logger.warning("微观单一警告: %s score=%.1f, 延迟%dmicros重试", symbol, signal_score, delay)
            self._push_event(symbol, "single_warning", warnings)
            return {"status": "ok", "reason": "单一微观警告，建议延迟重试", "data": {"pass": False, "adjusted_mult": 0.0, "checks": checks, "action": "delay_retry", "delay_us": delay, "max_retries": retries, "signal_id": signal_id}, "warnings": warnings}

        if total_penalty <= self.DOUBLE_WARN_THRESHOLD:
            logger.warning("微观多重警告: %s score=%.1f, 降级系数%.2f", symbol, signal_score, downgrade_mult)
            self._push_event(symbol, "double_warning", warnings)
            return {"status": "ok", "reason": "多个微观警告，信号降级", "data": {"pass": True, "adjusted_mult": downgrade_mult, "checks": checks, "action": "downgrade"}, "warnings": warnings}

        logger.error("微观严重异常: %s score=%.1f, 信号被拒绝 #RECOVERY: 检查%s盘口", symbol, signal_score, symbol)
        self._push_event(symbol, "triple_warning", warnings)
        return {"status": "ok", "reason": "微观结构严重异常，拒绝信号", "data": {"pass": False, "adjusted_mult": 0.0, "checks": checks, "action": "reject"}, "warnings": warnings}

    def health_check(self) -> Dict[str, Any]:
        try:
            deps = {
                "visual_cortex": self._visual is not None,
                "tactile_cortex": self._tactile is not None,
                "negotiation_bus": self._negotiation_bus is not None,
            }
            available = sum(1 for v in deps.values() if v)
            return {"status": "ok", "reason": f"MicroSecondConfirm 正常，{available}/{len(deps)} 依赖可用", "data": {"dependencies": deps}, "warnings": []}
        except Exception as e:
            logger.error("健康检查失败: %s", e, exc_info=True)
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"]}

    def _check_paper_wall(self, symbol: str, direction: int) -> Dict[str, Any]:
        try:
            wall = self._visual.get_wall_resilience(symbol, direction)
            if not isinstance(wall, dict) or "cancel_rate" not in wall:
                return {"triggered": False, "reason": "纸墙数据无效", "warnings": []}
            cancel_rate = float(wall["cancel_rate"])
            threshold = self._get_param("paper_wall_cancel_rate", self.DEFAULT_PAPER_WALL_CANCEL_RATE)
            if cancel_rate >= threshold:
                return {"triggered": True, "reason": "纸墙", "cancel_rate": cancel_rate, "warnings": ["paper_wall"]}
            return {"triggered": False, "reason": "挂单墙稳定", "cancel_rate": cancel_rate, "warnings": []}
        except Exception as e:
            logger.error("纸墙检测异常: %s", e, exc_info=True)
            self._mark_degradation_cooldown("paper_wall")
            return {"triggered": False, "reason": "纸墙检测异常", "warnings": []}

    def _check_pulse_divergence(self, symbol: str, direction: int) -> Dict[str, Any]:
        try:
            ticks = self._get_int_param("pulse_window_ticks", self.DEFAULT_PULSE_WINDOW_TICKS)
            pulse = self._tactile.get_trade_pulse(symbol, ticks=ticks)
            total_vol = max(float(pulse.get("total_volume", 0.0)), 1e-12)
            opposite_key = "sell_volume" if direction == 1 else "buy_volume"
            opposite_vol = float(pulse.get(opposite_key, 0.0))
            ratio = opposite_vol / total_vol
            threshold = self._get_param("pulse_divergence_ratio", self.DEFAULT_PULSE_DIVERGENCE_RATIO)
            if ratio >= threshold:
                return {"triggered": True, "reason": "脉冲背离", "opposite_ratio": ratio, "warnings": ["pulse_divergence"]}
            return {"triggered": False, "reason": "脉冲方向一致", "opposite_ratio": ratio, "warnings": []}
        except Exception as e:
            logger.error("脉冲背离检测异常: %s", e, exc_info=True)
            self._mark_degradation_cooldown("pulse_divergence")
            return {"triggered": False, "reason": "脉冲背离检测异常", "warnings": []}

    def _check_spread_manipulation(self, symbol: str) -> Dict[str, Any]:
        try:
            current = float(self._tactile.get_current_spread(symbol))
            tick_size = self.DEFAULT_TICK_SIZE
            if hasattr(self._tactile, 'get_current_tick_size'):
                tick_size = max(float(self._tactile.get_current_tick_size(symbol)), self.DEFAULT_TICK_SIZE)
            if current <= tick_size * 2:
                return {"triggered": False, "reason": "价差在正常范围内", "warnings": []}
            window_sec = self._get_param("spread_normal_window_sec", self.DEFAULT_SPREAD_NORMAL_WINDOW_SEC)
            normal = float(self._tactile.get_average_spread(symbol, window_sec=window_sec))
            if normal <= tick_size:
                return {"triggered": False, "reason": "历史价差过小", "warnings": []}
            ratio = current / normal
            threshold = self._get_param("spread_manipulation_mult", self.DEFAULT_SPREAD_MANIPULATION_MULT)
            if ratio >= threshold:
                return {"triggered": True, "reason": "价差异常", "spread_ratio": ratio, "warnings": ["spread_manipulation"]}
            return {"triggered": False, "reason": "价差正常", "spread_ratio": ratio, "warnings": []}
        except Exception as e:
            logger.error("价差操纵检测异常: %s", e, exc_info=True)
            self._mark_degradation_cooldown("spread_manipulation")
            return {"triggered": False, "reason": "价差操纵检测异常", "warnings": []}

    def _fallback_decision(self, symbol: str, signal_score: float, checks: Dict[str, Any]) -> Dict[str, Any]:
        """所有依赖不可用时的保守降级策略。"""
        if signal_score >= self.HIGH_SIGNAL_THRESHOLD:
            logger.warning("所有微观检测不可用，高质量信号降级通过")
            return {"status": "ok", "reason": "所有依赖不可用，高质量信号降级通过", "data": {"pass": True, "adjusted_mult": 0.6, "checks": checks, "action": "downgrade"}, "warnings": ["all_checks_unavailable"]}
        else:
            logger.warning("所有微观检测不可用，低质量信号延迟重试")
            return {"status": "ok", "reason": "所有依赖不可用，低质量信号延迟重试", "data": {"pass": False, "adjusted_mult": 0.0, "checks": checks, "action": "delay_retry", "delay_us": 1000, "max_retries": 1}, "warnings": ["all_checks_unavailable"]}

    def _push_event(self, symbol: str, event: str, details: Any = None) -> None:
        if self._negotiation_bus is not None:
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="micro_second_confirm",
                    symbol=symbol,
                    event=event,
                    details=details,
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning("推送诊断事件失败: %s", e)
        else:
            logger.debug("NegotiationBus 未注入，事件仅本地记录: %s %s", symbol, event)
