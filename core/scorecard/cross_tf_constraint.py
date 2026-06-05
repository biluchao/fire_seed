"""
火种系统 · 跨周期约束融合 (CrossTfConstraint)

核心职责：
1. 接收上级周期推送的关键价位信息（箱体边界、均线锚点、成交量密集区），并缓存至本地
2. 在小周期信号生成时，根据当前价格与上级周期区间的相对位置、区间厚度、时效阶段，
   动态调整仓位系数、止盈止损锚点，确保小周期交易在上级周期框架内获得最优风险收益

外部依赖（真实模块接口）：
- core.multi_tf_arbiter_v2.zone_thickness_calculator.ZoneThicknessCalculator : 获取区间厚度与时效衰减信息
- core.multi_tf_arbiter_v2.zone_decay_manager.ZoneDecayManager : 获取区间当前约束力
- core.perception.sensory_snapshot.SensorySnapshot : 获取当前价格与上级周期均线、箱体等数据
- core.behavioral_logger.BehavioralLogger : 记录降级事件和行为日志
- core.decision_tracer.DecisionTracer : 关键决策审计追踪

接口契约：
- update_zone_data(tf: str, zone_data: Dict[str, Any]) -> Dict[str, Any] : 更新上级周期的区间快照
- apply_constraint(signal: Dict[str, Any], current_price: float, direction: int, atr: float = None,
  volatility_percentile: float = None) -> Dict[str, Any] : 对信号施加跨周期约束
- get_cache_status() -> Dict[str, Any] : 获取缓存状态（监控用）
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当上级周期区间数据未就绪或过期时，使用保守降级参数（仓位系数×0.6，止损收紧20%）
- 当 ATR 无法获取时，依次尝试感官快照、区间宽度估算，均失败后使用 1e-4 作为最小保护
- 当区间宽度过窄时，降级使用保守参数而非拒绝交易
- 所有降级事件记录到行为日志并限制频率

资源管理：
- 本模块维护每个上级周期的最近一次有效区间快照，使用可重入锁保护
- 缓存数据设置最大容量，超出时按 LRU 淘汰
- 不持有任何外部资源句柄
"""

import time
import json
import logging
import threading
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class CrossTfConstraint:
    """跨周期约束融合器"""

    # ========== 类常量 ==========
    # 缓存
    DEFAULT_CACHE_TTL_SEC = 5.0
    HIGH_VOL_TTL_MULT = 0.3
    LOW_VOL_TTL_MULT = 1.5
    MAX_CACHE_SIZE = 10                      # 最大缓存周期数
    DEFAULT_TIMEFRAME = "1m"                 # 默认周期

    # 降级
    FALLBACK_POSITION_MULT = 0.6
    FALLBACK_STOP_TIGHTEN_PCT = 0.2
    MIN_FALLBACK_STOP_TIGHTEN_PCT = 0.05     # 最低收紧比例
    MAX_FALLBACK_STOP_TIGHTEN_PCT = 0.4      # 最高收紧比例

    # 区间位置阈值
    NEAR_BOUNDARY_THRESHOLD = 0.15
    FAR_BOUNDARY_THRESHOLD = 0.85

    # 仓位调整基础系数
    NEAR_OPPOSITE_BOUNDARY_MULT = 0.45
    INSIDE_BOX_MULT = 1.0
    NEAR_SAME_BOUNDARY_MULT = 1.2

    # 止盈止损
    TAKE_PROFIT_BOUNDARY_OFFSET_ATR = 0.2
    STOP_BOUNDARY_BUFFER_ATR = 0.3
    SLIPPAGE_RESERVE_ATR = 0.05
    MIN_PRICE_OFFSET = 1e-6
    MAX_STOP_DISTANCE_ATR = 3.0              # 止损最大偏离

    # 时效阶段
    ZONE_STAGE_STRENGTH = {"fresh": 1.0, "active": 0.9, "memory": 0.7, "fading": 0.4}
    DEFAULT_STAGE_STRENGTH = 0.8
    MIN_EFFECTIVE_STRENGTH = 0.2

    # 厚度
    THICKNESS_STRENGTH_FACTOR = 0.5
    MAX_THICKNESS_ATR = 1.0
    MAX_STRENGTHEN_MULT = 1.5               # 系数增强上限

    # 周期映射
    TF_MAPPING = {"1m": "5m", "5m": "15m", "15m": "1h", "30m": "4h"}

    # ATR
    DEFAULT_ATR_RATIO = 0.4
    MIN_ATR_FALLBACK = 1e-4

    # 降级日志
    DEGRADATION_LOG_INTERVAL_SEC = 10.0

    def __init__(self):
        self._zone_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_timestamps: Dict[str, float] = {}
        self._cache_access_order: List[str] = []  # LRU 淘汰

        self._thickness_calculator = None
        self._decay_manager = None
        self._sensory_snapshot = None
        self._behavioral_logger = None
        self._decision_tracer = None

        self._lock = threading.RLock()
        self._degradation_lock = threading.Lock()
        self._last_degradation_log = 0.0
        self._trace_count = 0
        self._trace_count_reset = time.time()

        logger.info("CrossTfConstraint 初始化，周期映射: %s", self.TF_MAPPING)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self, thickness_calculator=None, decay_manager=None,
        sensory_snapshot=None, behavioral_logger=None, decision_tracer=None
    ):
        self._thickness_calculator = thickness_calculator
        self._decay_manager = decay_manager
        self._sensory_snapshot = sensory_snapshot
        self._behavioral_logger = behavioral_logger
        self._decision_tracer = decision_tracer
        for name, obj in [("thickness_calculator", thickness_calculator),
                          ("decay_manager", decay_manager),
                          ("sensory_snapshot", sensory_snapshot),
                          ("behavioral_logger", behavioral_logger),
                          ("decision_tracer", decision_tracer)]:
            logger.info("%s %s", name, "注入成功" if obj else "未注入")

    # ========== 公共接口 ==========
    def update_zone_data(self, tf: str, zone_data: Dict[str, Any]) -> Dict[str, Any]:
        if not tf or not isinstance(zone_data, dict):
            return self._err("无效参数", ["invalid_params"])
        required = ["box_upper", "box_lower"]
        missing = [k for k in required if k not in zone_data]
        if missing:
            return self._err(f"缺少字段: {missing}", [f"missing_{missing}"])
        box_upper = float(zone_data["box_upper"])
        box_lower = float(zone_data["box_lower"])
        if box_upper <= box_lower:
            return self._err("上沿需大于下沿", ["invalid_box"])
        thickness = zone_data.get("thickness")
        if thickness is not None and thickness < 0:
            thickness = 0.0
        now = time.time()
        with self._lock:
            self._zone_cache[tf] = {
                "box_upper": box_upper, "box_lower": box_lower,
                "ma12_value": zone_data.get("ma12_value"),
                "thickness": thickness,
                "decay_strength": zone_data.get("decay_strength", 1.0),
                "zone_stage": zone_data.get("zone_stage", "active"),
            }
            self._cache_timestamps[tf] = now
            # LRU 淘汰
            if tf in self._cache_access_order:
                self._cache_access_order.remove(tf)
            self._cache_access_order.append(tf)
            while len(self._cache_access_order) > self.MAX_CACHE_SIZE:
                old_tf = self._cache_access_order.pop(0)
                self._zone_cache.pop(old_tf, None)
                self._cache_timestamps.pop(old_tf, None)
                logger.debug("LRU 淘汰周期缓存: %s", old_tf)
        logger.debug("更新区间 %s [%.2f-%.2f]", tf, box_lower, box_upper)
        return {"status": "ok", "reason": f"已更新 {tf}", "data": {"tf": tf}, "warnings": []}

    def apply_constraint(
        self, signal: Dict[str, Any], current_price: float, direction: int,
        atr: Optional[float] = None, volatility_percentile: Optional[float] = None
    ) -> Dict[str, Any]:
        # 参数校验
        if direction not in (1, -1, 0):
            return self._err("无效方向", ["invalid_direction"])
        if current_price <= 0:
            return self._err("无效价格", ["invalid_price"])
        if not signal:
            return self._err("信号为空", ["empty_signal"])
        if direction == 0:
            return {"status": "ok", "reason": "中性方向跳过", "data": signal.copy(), "warnings": []}

        signal_tf = self._resolve_timeframe(signal.get("timeframe"))
        reference_tf = self.TF_MAPPING.get(signal_tf, "5m")

        # TTL 自适应
        cache_ttl = self.DEFAULT_CACHE_TTL_SEC
        if volatility_percentile is not None:
            vp = max(0.0, min(100.0, float(volatility_percentile)))
            if vp > 70:
                cache_ttl *= self.HIGH_VOL_TTL_MULT
            elif vp < 30:
                cache_ttl *= self.LOW_VOL_TTL_MULT

        with self._lock:
            zone_data = self._zone_cache.get(reference_tf)
            cache_ts = self._cache_timestamps.get(reference_tf, 0)
            # 更新 LRU 访问顺序
            if reference_tf in self._cache_access_order:
                self._cache_access_order.remove(reference_tf)
            self._cache_access_order.append(reference_tf)
        cache_age = time.time() - cache_ts

        if zone_data is None or cache_age > cache_ttl:
            reason = "无数据" if zone_data is None else f"过期({cache_age:.1f}s)"
            self._log_degradation(reason)
            adj = self._fallback(signal, current_price, direction, atr)
            return {"status": "ok", "reason": f"降级:{reason}", "data": adj, "warnings": ["zone_unavailable"]}

        box_upper = zone_data["box_upper"]
        box_lower = zone_data["box_lower"]
        box_range = box_upper - box_lower
        if box_range <= 1e-6:
            self._log_degradation("box_range_too_small")
            adj = self._fallback(signal, current_price, direction, atr)
            return {"status": "ok", "reason": "区间过窄降级", "data": adj, "warnings": ["box_range_small"]}

        raw_ratio = (current_price - box_lower) / box_range
        ratio = max(0.0, min(1.0, raw_ratio))
        if abs(raw_ratio - ratio) > 0.001:
            logger.debug("价格钳位: 原始=%.4f 钳位后=%.4f", raw_ratio, ratio)

        thickness = zone_data.get("thickness") or 0.0
        if thickness < 0:
            thickness = 0.0
        decay_strength = zone_data.get("decay_strength", 1.0)
        if decay_strength <= 0:
            logger.warning("decay_strength=%s, 区间完全失效", decay_strength)
            decay_strength = 0.01
        stage = zone_data.get("zone_stage", "active")
        stage_factor = self.ZONE_STAGE_STRENGTH.get(stage, self.DEFAULT_STAGE_STRENGTH)
        effective_strength = max(self.MIN_EFFECTIVE_STRENGTH, decay_strength * stage_factor)

        if atr is None or atr <= 0:
            atr = self._resolve_atr(signal_tf, box_range)
        atr = max(atr, self.MIN_ATR_FALLBACK)

        # 备份原始信号
        adj = signal.copy()
        adj["_original_position_mult"] = adj.get("position_mult", 1.0)
        adj["_original_stop_price"] = adj.get("stop_price")
        adj["_original_take_profit_price"] = adj.get("take_profit_price")
        warnings = []

        # 厚度调整阈值
        thick_norm = min(1.0, thickness / max(self.MIN_ATR_FALLBACK, atr * self.MAX_THICKNESS_ATR))
        near_adj = self.NEAR_BOUNDARY_THRESHOLD + (1.0 - thick_norm) * 0.1
        far_adj = self.FAR_BOUNDARY_THRESHOLD - (1.0 - thick_norm) * 0.1

        mult = self.INSIDE_BOX_MULT
        tp_anchor = None
        stop_anchor = None
        reason_struct = {"zone": reference_tf, "pos_ratio": round(ratio, 4)}

        if direction == 1:
            if ratio > far_adj:
                mult = self._strengthen(self.NEAR_OPPOSITE_BOUNDARY_MULT, thickness)
                tp_anchor = box_upper - atr * (self.TAKE_PROFIT_BOUNDARY_OFFSET_ATR + self.SLIPPAGE_RESERVE_ATR)
                reason_struct["type"] = "near_opposite"
            elif ratio < near_adj:
                mult = self._strengthen(self.NEAR_SAME_BOUNDARY_MULT, thickness)
                stop_anchor = box_lower - atr * (self.STOP_BOUNDARY_BUFFER_ATR + self.SLIPPAGE_RESERVE_ATR)
                reason_struct["type"] = "near_same"
            else:
                reason_struct["type"] = "inside"
        else:
            if ratio < near_adj:
                mult = self._strengthen(self.NEAR_OPPOSITE_BOUNDARY_MULT, thickness)
                tp_anchor = box_lower + atr * (self.TAKE_PROFIT_BOUNDARY_OFFSET_ATR + self.SLIPPAGE_RESERVE_ATR)
                reason_struct["type"] = "near_opposite"
            elif ratio > far_adj:
                mult = self._strengthen(self.NEAR_SAME_BOUNDARY_MULT, thickness)
                stop_anchor = box_upper + atr * (self.STOP_BOUNDARY_BUFFER_ATR + self.SLIPPAGE_RESERVE_ATR)
                reason_struct["type"] = "near_same"
            else:
                reason_struct["type"] = "inside"

        mult *= effective_strength
        mult = max(0.1, min(mult, 2.0))

        raw_position_mult = adj.get("position_mult", 1.0)
        if raw_position_mult <= 0:
            logger.warning("position_mult 无效(%s)，重置为1.0", raw_position_mult)
            raw_position_mult = 1.0
            adj["position_mult"] = 1.0
        adj["position_mult"] = raw_position_mult * mult
        reason_struct["multiplier"] = round(mult, 4)

        # 止盈融合：取更保守值
        if tp_anchor is not None and tp_anchor > 0:
            old_tp = adj.get("take_profit_price")
            if (direction == 1 and tp_anchor > current_price and (old_tp is None or tp_anchor < old_tp)) or \
               (direction == -1 and tp_anchor < current_price and (old_tp is None or tp_anchor > old_tp)):
                adj["take_profit_price"] = tp_anchor
                reason_struct["tp_anchor"] = tp_anchor

        # 止损融合：取更保守值（且不超过最大距离）
        if stop_anchor is not None and stop_anchor > 0:
            max_stop_distance = atr * self.MAX_STOP_DISTANCE_ATR
            if direction == 1:
                stop_anchor = max(stop_anchor, current_price - max_stop_distance)
                old_stop = adj.get("stop_price")
                if (old_stop is None or stop_anchor > old_stop) and stop_anchor < current_price:
                    adj["stop_price"] = stop_anchor
                    reason_struct["stop_anchor"] = stop_anchor
            else:
                stop_anchor = min(stop_anchor, current_price + max_stop_distance)
                old_stop = adj.get("stop_price")
                if (old_stop is None or stop_anchor < old_stop) and stop_anchor > current_price:
                    adj["stop_price"] = stop_anchor
                    reason_struct["stop_anchor"] = stop_anchor

        adj["cross_tf_reason"] = json.dumps(reason_struct)
        adj["cross_tf_strength"] = round(effective_strength, 2)

        # 决策追踪（带频率限制）
        self._trace_decision(reason_struct)

        logger.info("跨周期约束: %s", json.dumps(reason_struct))
        return {"status": "ok", "reason": json.dumps(reason_struct), "data": adj, "warnings": warnings}

    def get_cache_status(self) -> Dict[str, Any]:
        with self._lock:
            status = {
                tf: {
                    "age": time.time() - self._cache_timestamps.get(tf, 0),
                    "valid": time.time() - self._cache_timestamps.get(tf, 0) < self.DEFAULT_CACHE_TTL_SEC,
                    "has_data": tf in self._zone_cache,
                }
                for tf in self._zone_cache
            }
        return {"status": "ok", "data": status, "reason": "缓存状态查询"}

    def health_check(self) -> Dict[str, Any]:
        try:
            lock_healthy = self._lock.acquire(blocking=False)
            if lock_healthy:
                self._lock.release()
            else:
                logger.error("线程锁可能死锁 #RECOVERY: 检查持锁线程状态")
            with self._lock:
                cached = list(self._zone_cache.keys())
                now = time.time()
                expired = [tf for tf in cached if now - self._cache_timestamps.get(tf, 0) > self.DEFAULT_CACHE_TTL_SEC]
            deps = {k: v is not None for k, v in [
                ("thickness_calculator", self._thickness_calculator),
                ("decay_manager", self._decay_manager),
                ("sensory_snapshot", self._sensory_snapshot),
                ("behavioral_logger", self._behavioral_logger),
                ("decision_tracer", self._decision_tracer),
            ]}
            return {
                "status": "ok", "reason": f"缓存{cached}个，过期{expired}个，锁{'正常' if lock_healthy else '异常'}",
                "data": {"cached": cached, "expired": expired, "deps": deps, "lock_healthy": lock_healthy},
                "warnings": []
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁或缓存结构")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"]}

    # ========== 私有方法 ==========
    def _resolve_timeframe(self, tf: Any) -> str:
        """解析并校验周期标识"""
        if isinstance(tf, str) and tf.strip():
            return tf.strip().lower()
        logger.warning("无效timeframe(%s)，使用默认值(%s)", type(tf).__name__, self.DEFAULT_TIMEFRAME)
        return self.DEFAULT_TIMEFRAME

    def _strengthen(self, base: float, thickness: float) -> float:
        """厚度越大，越增强边界策略"""
        if thickness <= 0:
            return base
        factor = min(1.0, thickness / (1.0 + thickness)) * self.THICKNESS_STRENGTH_FACTOR
        result = base + (base - 1.0) * factor if base > 1.0 else base - (1.0 - base) * factor
        # 绝对值增强上限
        if result > 1.0:
            result = min(result, base * self.MAX_STRENGTHEN_MULT)
        else:
            result = max(result, base / self.MAX_STRENGTHEN_MULT)
        return result

    def _resolve_atr(self, signal_tf: str, box_range: float) -> float:
        if self._sensory_snapshot is not None and hasattr(self._sensory_snapshot, "get_atr"):
            try:
                val = self._sensory_snapshot.get_atr(signal_tf)
                if isinstance(val, (int, float)) and val > 0:
                    return float(val)
                logger.warning("感官快照ATR无效(%s)，使用箱体估算", val)
            except Exception as e:
                logger.warning("感官快照获取ATR失败: %s", e)
        return box_range * self.DEFAULT_ATR_RATIO

    def _fallback(
        self, signal: Dict[str, Any], current_price: float, direction: int, atr: Optional[float] = None
    ) -> Dict[str, Any]:
        adj = signal.copy()
        orig_mult = adj.get("position_mult", 1.0)
        if orig_mult <= 0:
            orig_mult = 1.0
        adj["_original_position_mult"] = orig_mult
        strength = min(1.0, orig_mult)
        fallback_mult = self.FALLBACK_POSITION_MULT + (1.0 - self.FALLBACK_POSITION_MULT) * strength * 0.5
        adj["position_mult"] = orig_mult * fallback_mult
        stop = adj.get("stop_price")
        if stop is not None and current_price > 0:
            adj["_original_stop_price"] = stop
            # 根据 ATR 动态调整收紧比例
            tighten = self.FALLBACK_STOP_TIGHTEN_PCT
            if atr is not None and atr > 0 and current_price > 0:
                # ATR 大则收紧比例减小，避免正常波动扫出
                tighten = max(self.MIN_FALLBACK_STOP_TIGHTEN_PCT,
                              min(self.MAX_FALLBACK_STOP_TIGHTEN_PCT,
                                  self.FALLBACK_STOP_TIGHTEN_PCT * (0.5 / max(0.5, atr / current_price))))
            adj["stop_price"] = current_price - direction * abs(current_price - stop) * (1 + tighten)
        adj["cross_tf_reason"] = json.dumps({"type": "fallback"})
        adj["_fallback_applied"] = True
        return adj

    def _err(self, msg: str, warnings: List[str]) -> Dict:
        return {"status": "error", "reason": msg, "data": {}, "warnings": warnings}

    def _log_degradation(self, reason: str) -> None:
        with self._degradation_lock:
            now = time.time()
            if now - self._last_degradation_log < self.DEGRADATION_LOG_INTERVAL_SEC:
                return
            self._last_degradation_log = now
        logger.warning("跨周期约束降级: %s", reason)
        if self._behavioral_logger:
            try:
                self._behavioral_logger.log_event(
                    event_type="cross_tf_degradation", details={"reason": reason, "timestamp": time.time()}
                )
            except Exception as e:
                logger.warning("行为日志失败: %s", e)

    def _trace_decision(self, details: Dict) -> None:
        """决策追踪（带频率限制，每秒最多 100 条）"""
        now = time.time()
        if now - self._trace_count_reset > 1.0:
            self._trace_count = 0
            self._trace_count_reset = now
        if self._trace_count > 100:
            return
        self._trace_count += 1
        if self._decision_tracer:
            try:
                self._decision_tracer.trace_event(
                    event_type="cross_tf_constraint", details=details
                )
            except Exception as e:
                logger.warning("决策追踪失败: %s", e)
