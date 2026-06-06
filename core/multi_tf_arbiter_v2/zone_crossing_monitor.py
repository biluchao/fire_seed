"""
火种系统 · 三阶段穿越监控器 (ZoneCrossingMonitor)

核心职责：
1. 监控小周期价格触及大周期关键区间边界后的微观穿越行为，将穿越过程划分为试探、拉锯、突破/回弹三个阶段，动态输出仓位调整、止损锚点与建议动作
2. 根据价格行为（方向、成交量、时间）、波动率环境与上级周期区间状态自动判定穿越阶段切换，并触发相应的风险控制动作

外部依赖（真实模块接口）：
- core.perception.tactile_cortex.TactileCortex : 获取实时流动性评级、成交脉搏与波动率分位（可选，未注入时使用保守默认值）
- core.negotiation_bus.NegotiationBus : 发布穿越状态变更事件（可选，未注入时仅本地日志）
- core.order_manager.profit_compression.ProfitCompression : 查询指定品种的当前紧缩利润阶段（可选，未注入时使用默认系数）
- core.multi_tf_arbiter_v2.zone_thickness_calculator.ZoneThicknessCalculator : 获取区间当前厚度（可选，未注入时使用默认厚度）

接口契约：
- start_monitoring(symbol: str, direction: int, entry_price: float, boundary_price: float, zone_info: Dict, original_position_size: float = 0.0) -> str : 启动穿越监控，返回监控ID
- update_price(symbol: str, price: float, volume: float = 0.0, timestamp: float = None, zone_info: Dict = None) -> Dict[str, Any] : 更新价格并返回当前阶段与建议动作
- stop_monitoring(symbol: str) -> Dict[str, Any] : 停止指定品种的监控
- get_active_monitors() -> Dict[str, Any] : 获取所有活跃监控的摘要信息
- health_check() -> Dict[str, Any] : 模块自检
- reset_for_testing() -> None : 测试环境专用重置
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 TactileCortex 不可用时，波动率分位默认为50，成交量默认为0，突破判定使用价格+时间的保守模式
- 当 NegotiationBus 不可用时，穿越状态变更仅记录本地日志；若推送失败，事件写入本地环形缓冲区待恢复后补推
- 当 ProfitCompression 不可用时，仓位调整建议使用默认系数（不根据紧缩利润阶段缩放松紧）
- 当 ZoneThicknessCalculator 不可用时，区间厚度默认为 ATR×0.15；若 zone_info 中包含 atr 值，用于计算实际止损偏移
- 价格跳变连续超过 MAX_PRICE_JUMP_STRIKES 次时，自动终止该品种监控

资源管理：
- 本模块维护活跃监控字典，在 stop_monitoring、监控超时或终局阶段后自动清理对应条目
- 后台清理线程在模块销毁时自动停止，atexit 注册兜底清理
- 活跃监控数量超过 max_active_monitors 时拒绝新请求，防止内存耗尽
"""

import time
import logging
import threading
import atexit
from typing import Dict, Any, List, Optional, Tuple
from collections import deque, namedtuple
from enum import Enum
import math
import uuid

logger = logging.getLogger(__name__)


class SuggestedAction(Enum):
    """建议动作枚举"""
    HOLD = "hold"
    REDUCE = "reduce"
    RESTORE = "restore"
    EXIT = "exit"
    TIGHTEN = "tighten"


class ZoneCrossingMonitor:
    """三阶段穿越监控器"""

    __version__ = "2.2.0"

    # ========== 类常量 ==========
    DEFAULT_PROBING_SECONDS = 3.0
    DEFAULT_WRESTLING_SECONDS = 30.0
    DEFAULT_MONITOR_TIMEOUT_SEC = 60.0
    DEFAULT_WRESTLING_TIMEOUT_EXTRA = 60.0
    DEFAULT_REASSESS_INTERVAL_SEC = 5.0
    DEFAULT_STAGE_COOLDOWN_SEC = 1.0
    DEFAULT_RESOLUTION_LINGER_SEC = 3.0

    DEFAULT_PROBING_POSITION_MULT = 0.85
    DEFAULT_WRESTLING_POSITION_MULT = 0.85
    DEFAULT_FAVORABLE_RESTORE_PCT = 0.90
    DEFAULT_UNFAVORABLE_STOP_TIGHTEN = 0.5
    DEFAULT_MIN_POSITION_MULT = 0.3

    BASE_FAVORABLE_BREAK_PCT = 0.002
    BASE_UNFAVORABLE_BREAK_PCT = 0.0015
    BASE_EARLY_TERMINATION_PCT = 0.002
    MIN_EARLY_TERMINATION_PCT = 0.001
    MIN_RECOVERY_PCT = 0.0005

    VOLUME_CONFIRM_MULT = 1.3
    VOLUME_MIN_SAMPLES = 10

    MAX_PRICE_JUMP_PCT = 0.10
    MAX_PRICE_JUMP_STRIKES = 3

    BACKGROUND_CLEANUP_INTERVAL_SEC = 30.0
    MAX_ACTIVE_MONITORS = 500  # 活跃监控上限，防止内存耗尽

    DEGRADED_VOL_PERCENTILE = 50.0
    DEGRADED_ZONE_THICKNESS = 0.15

    EVENT_TYPE_STAGE_CHANGE = "zone_crossing_stage_change"

    # 私有命名元组
    _StageEvaluation = namedtuple("_StageEvaluation", ["stage", "action", "reason"])

    def __init__(self):
        self._monitors: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

        self._tactile_cortex = None
        self._negotiation_bus = None
        self._profit_compression = None
        self._zone_thickness_calculator = None

        # 事件持久化降级缓冲区
        self._event_buffer = deque(maxlen=1000)

        self._stop_cleanup = threading.Event()
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True, name="zone_crossing_cleanup")
        self._cleanup_thread.start()
        atexit.register(self._atexit_cleanup)

        logger.info("ZoneCrossingMonitor v%s 初始化完成", self.__version__)

    # ========== 依赖注入 ==========
    def inject_dependencies(self, tactile_cortex=None, negotiation_bus=None, profit_compression=None, zone_thickness_calculator=None):
        if tactile_cortex is not None:
            self._tactile_cortex = tactile_cortex
            logger.info("TactileCortex 注入成功")
        else:
            logger.warning("TactileCortex 未注入 #RECOVERY: 检查模块加载顺序")
        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, 'publish_event'):
                logger.warning("NegotiationBus 缺少 publish_event 方法 #RECOVERY: 升级或适配")
                self._negotiation_bus = None
            else:
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")
        if profit_compression is not None:
            self._profit_compression = profit_compression
            logger.info("ProfitCompression 注入成功")
        else:
            logger.warning("ProfitCompression 未注入 #RECOVERY: 检查模块加载顺序")
        if zone_thickness_calculator is not None:
            self._zone_thickness_calculator = zone_thickness_calculator
            logger.info("ZoneThicknessCalculator 注入成功")
        else:
            logger.warning("ZoneThicknessCalculator 未注入 #RECOVERY: 检查模块加载顺序")

    # ========== 工具方法 ==========
    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        """标准化品种标识"""
        if not isinstance(symbol, str):
            return ""
        return symbol.upper().replace("-", "").replace("/", "")

    # ========== 公共接口 ==========
    def start_monitoring(self, symbol: str, direction: int, entry_price: float, boundary_price: float,
                         zone_info: Dict[str, Any], original_position_size: float = 0.0) -> Dict[str, Any]:
        raw_symbol = str(symbol)
        normalized = self._normalize_symbol(raw_symbol)
        if len(normalized) < 2:
            return {"status": "error", "reason": f"无效品种标识: {raw_symbol}", "data": {}, "warnings": ["invalid_symbol"]}
        if direction not in (1, -1):
            return {"status": "error", "reason": f"无效方向: {direction}", "data": {}, "warnings": ["invalid_direction"]}
        if entry_price <= 0:
            return {"status": "error", "reason": f"入场价必须>0: {entry_price}", "data": {}, "warnings": ["invalid_entry_price"]}
        if boundary_price <= 0:
            return {"status": "error", "reason": f"边界价必须>0: {boundary_price}", "data": {}, "warnings": ["invalid_boundary_price"]}
        if direction == 1 and boundary_price <= entry_price:
            return {"status": "error", "reason": f"多头穿越压力位时边界价应高于入场价: 入场={entry_price} 边界={boundary_price}", "data": {}, "warnings": ["boundary_consistency_violation"]}
        if direction == -1 and boundary_price >= entry_price:
            return {"status": "error", "reason": f"空头穿越支撑位时边界价应低于入场价: 入场={entry_price} 边界={boundary_price}", "data": {}, "warnings": ["boundary_consistency_violation"]}

        # 仓位比例校验
        if not (0.0 <= original_position_size <= 1.0):
            logger.warning(f"original_position_size={original_position_size} 超出[0,1]，已钳制")
            original_position_size = max(0.0, min(1.0, original_position_size))

        # 时间参数校验
        probing_sec = zone_info.get("probing_seconds", self.DEFAULT_PROBING_SECONDS)
        wrestling_sec = zone_info.get("wrestling_seconds", self.DEFAULT_WRESTLING_SECONDS)
        if probing_sec <= 0 or probing_sec > 30:
            probing_sec = self.DEFAULT_PROBING_SECONDS
        if wrestling_sec <= 0 or wrestling_sec > 300:
            wrestling_sec = self.DEFAULT_WRESTLING_SECONDS

        with self._lock:
            if len(self._monitors) >= self.MAX_ACTIVE_MONITORS:
                logger.error(f"活跃监控数量已达上限 {self.MAX_ACTIVE_MONITORS}，拒绝新监控 #RECOVERY: 检查调用方是否异常创建")
                return {"status": "error", "reason": "活跃监控数量已达上限", "data": {}, "warnings": ["max_monitors_reached"]}
            if normalized in self._monitors:
                logger.info(f"替换 {normalized} 的旧监控实例")
                self._monitors.pop(normalized, None)

            monitor_id = f"{normalized}_{'L' if direction==1 else 'S'}_{int(time.monotonic()*1e6)}_{uuid.uuid4().hex[:6]}"
            now = time.monotonic()

            self._monitors[normalized] = {
                "monitor_id": monitor_id,
                "raw_symbol": raw_symbol,
                "normalized_symbol": normalized,
                "direction": direction,
                "entry_price": entry_price,
                "boundary_price": boundary_price,
                "original_position_size": original_position_size,
                "stage": "probing",
                "stage_start_time": now,
                "last_update_time": now,
                "last_stage_switch_time": now,
                "last_logged_stage": "probing",
                "price_history": deque(maxlen=50),
                "volume_history": deque(maxlen=50),
                "price_jump_count": 0,
                "probing_seconds": probing_sec,
                "wrestling_seconds": wrestling_sec,
                "resolution_reason": "",
                "resolution_linger_until": 0.0,
                "suggested_action": SuggestedAction.HOLD.value,
                "position_mult": self.DEFAULT_PROBING_POSITION_MULT,
                "stop_adjustment": 0.0,
                "stop_anchor": boundary_price,
                "zone_info": zone_info,
            }

        logger.info("启动穿越监控: %s 方向=%s 入场=%.2f 边界=%.2f ID=%s",
                    normalized, "多" if direction == 1 else "空", entry_price, boundary_price, monitor_id)
        return {"status": "ok", "reason": f"已启动 {normalized} 的穿越监控", "data": {"monitor_id": monitor_id}, "warnings": []}

    def update_price(self, symbol: str, price: float, volume: float = 0.0,
                     timestamp: float = None, zone_info: Dict[str, Any] = None) -> Dict[str, Any]:
        normalized = self._normalize_symbol(symbol)
        if len(normalized) < 2:
            return {"status": "error", "reason": f"无效品种标识: {symbol}", "data": {}, "warnings": ["invalid_symbol"]}
        if timestamp is None:
            timestamp = time.monotonic()
        if price is None or (isinstance(price, float) and math.isnan(price)):
            return {"status": "error", "reason": "price 为 None 或 NaN", "data": {}, "warnings": ["invalid_price"]}
        if price <= 0:
            return {"status": "error", "reason": f"price 必须>0: {price}", "data": {}, "warnings": ["invalid_price"]}

        with self._lock:
            state = self._monitors.get(normalized)
            if state is None:
                return {"status": "error", "reason": f"未找到 {normalized} 的活跃监控", "data": {}, "warnings": []}

            # 价格跳变检测
            if state["price_history"]:
                last_price = state["price_history"][-1]
                jump_pct = abs(price - last_price) / last_price
                max_jump_pct = state["zone_info"].get("max_price_jump_pct", self.MAX_PRICE_JUMP_PCT)
                if jump_pct > max_jump_pct:
                    state["price_jump_count"] += 1
                    logger.error(f"{normalized} 价格跳变异常: {jump_pct:.2%} (连续{state['price_jump_count']}次) #RECOVERY: 检查数据源")
                    if state["price_jump_count"] >= self.MAX_PRICE_JUMP_STRIKES:
                        self._monitors.pop(normalized, None)
                        logger.critical(f"{normalized} 连续{self.MAX_PRICE_JUMP_STRIKES}次价格跳变，自动终止监控 #RECOVERY: 重启数据源")
                        return {"status": "error", "reason": "连续价格跳变，监控已终止", "data": {"suggested_action": SuggestedAction.HOLD.value}, "warnings": ["price_jump_terminated"]}
                    return {"status": "error", "reason": f"价格跳变异常: {jump_pct:.2%}", "data": {"stage": state["stage"], "suggested_action": SuggestedAction.HOLD.value}, "warnings": ["price_jump_anomaly"]}
                else:
                    state["price_jump_count"] = 0

            if zone_info is not None:
                state["zone_info"].update(zone_info)
                if "boundary_price" in zone_info:
                    state["boundary_price"] = zone_info["boundary_price"]

            state["last_update_time"] = timestamp
            state["price_history"].append(price)
            state["volume_history"].append(volume)

            # 终局滞留期
            if state["stage"].startswith("resolution_") and timestamp < state.get("resolution_linger_until", 0):
                return {"status": "ok", "reason": f"终局滞留中: {state['stage']}", "data": {
                    "symbol": normalized, "stage": state["stage"], "suggested_action": SuggestedAction.HOLD.value,
                    "position_mult": state["position_mult"], "stop_adjustment": state["stop_adjustment"],
                    "stop_anchor": None}, "warnings": []}

            vol_percentile = self._get_vol_percentile(state.get("raw_symbol", normalized))
            stage_eval = self._evaluate_stage(state, price, volume, timestamp, vol_percentile)
            old_stage = state["stage"]

            now = timestamp
            if stage_eval.stage != old_stage:
                elapsed_since_switch = now - state["last_stage_switch_time"]
                if elapsed_since_switch < self.DEFAULT_STAGE_COOLDOWN_SEC:
                    stage_eval = self._StageEvaluation(old_stage, SuggestedAction.HOLD.value, "阶段切换冷却中")
                else:
                    state["last_stage_switch_time"] = now
                    state["stage_start_time"] = now

            state["stage"] = stage_eval.stage
            state["suggested_action"] = stage_eval.action
            state["resolution_reason"] = stage_eval.reason

            pos_mult, stop_adj, stop_anchor = self._calculate_adjustment(state, price, normalized)
            state["position_mult"] = pos_mult
            state["stop_adjustment"] = stop_adj
            state["stop_anchor"] = stop_anchor

            if stage_eval.stage.startswith("resolution_"):
                state["resolution_linger_until"] = now + self.DEFAULT_RESOLUTION_LINGER_SEC

            if stage_eval.stage != old_stage:
                self._log_and_publish({
                    "symbol": normalized, "old_stage": old_stage, "new_stage": stage_eval.stage,
                    "reason": stage_eval.reason, "action": stage_eval.action, "pos_mult": pos_mult, "stop_anchor": stop_anchor
                })

            return {"status": "ok", "reason": stage_eval.reason, "data": {
                "symbol": normalized, "stage": stage_eval.stage, "suggested_action": stage_eval.action,
                "position_mult": pos_mult, "stop_adjustment": stop_adj, "stop_anchor": stop_anchor}, "warnings": []}

    def stop_monitoring(self, symbol: str) -> Dict[str, Any]:
        normalized = self._normalize_symbol(symbol)
        with self._lock:
            if normalized not in self._monitors:
                return {"status": "error", "reason": f"未找到 {normalized} 的活跃监控", "data": {}, "warnings": []}
            self._monitors.pop(normalized, None)
        logger.info(f"手动停止 {normalized} 穿越监控")
        return {"status": "ok", "reason": f"已停止 {normalized} 的穿越监控", "data": {}, "warnings": []}

    def get_active_monitors(self) -> Dict[str, Any]:
        with self._lock:
            snapshot = {k: dict(v) for k, v in self._monitors.items()}
        now = time.monotonic()
        summary = {}
        for sym, st in snapshot.items():
            summary[sym] = {
                "stage": st["stage"],
                "direction": "多" if st["direction"] == 1 else "空",
                "suggested_action": st["suggested_action"],
                "position_mult": st["position_mult"],
                "stage_elapsed_sec": now - st["stage_start_time"],
            }
        return {"status": "ok", "reason": f"活跃监控: {len(summary)} 个", "data": {"active_monitors": summary}, "warnings": []}

    def health_check(self) -> Dict[str, Any]:
        try:
            with self._lock:
                snapshot = {k: dict(v) for k, v in self._monitors.items()}
            now = time.monotonic()
            active_count = len(snapshot)
            stalled = []
            for sym, st in snapshot.items():
                if st["stage"].startswith("resolution_") and now < st.get("resolution_linger_until", 0):
                    continue
                if now - st["last_update_time"] > self.DEFAULT_MONITOR_TIMEOUT_SEC:
                    stalled.append(sym)
            if stalled:
                logger.warning(f"发现 {len(stalled)} 个疑似卡死的监控: {stalled} #RECOVERY: 检查数据源")

            dep_status = {}
            for dep_name, dep_obj in [("tactile_cortex", self._tactile_cortex),
                                      ("negotiation_bus", self._negotiation_bus),
                                      ("profit_compression", self._profit_compression),
                                      ("zone_thickness_calculator", self._zone_thickness_calculator)]:
                if dep_obj is not None and hasattr(dep_obj, 'health_check'):
                    try:
                        dep_status[dep_name] = dep_obj.health_check().get("status", "unknown")
                    except Exception:
                        dep_status[dep_name] = "error"
                else:
                    dep_status[dep_name] = "not_injected"

            return {"status": "ok", "reason": f"正常，活跃{active_count}，卡死{len(stalled)}",
                    "data": {"active_count": active_count, "stalled_count": len(stalled),
                             "module_version": self.__version__,
                             "dependencies": dep_status},
                    "warnings": [f"stalled: {stalled}"] if stalled else []}
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁与字典一致性")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"]}

    def reset_for_testing(self) -> None:
        """测试环境专用：清空所有监控和依赖"""
        with self._lock:
            self._monitors.clear()
        self._tactile_cortex = None
        self._negotiation_bus = None
        self._profit_compression = None
        self._zone_thickness_calculator = None
        logger.info("测试重置完成")

    # ========== 私有方法 ==========
    def _get_vol_percentile(self, symbol: str) -> float:
        if self._tactile_cortex:
            try:
                return self._tactile_cortex.get_volatility_percentile(symbol)
            except Exception as e:
                logger.warning(f"获取波动率分位失败: {e} #RECOVERY: 检查TactileCortex")
        return self.DEGRADED_VOL_PERCENTILE

    def _get_zone_thickness(self, symbol: str) -> float:
        if self._zone_thickness_calculator:
            try:
                return self._zone_thickness_calculator.get_current_thickness(symbol)
            except Exception as e:
                logger.warning(f"获取区间厚度失败: {e} #RECOVERY: 检查ZoneThicknessCalculator")
        return self.DEGRADED_ZONE_THICKNESS

    def _evaluate_stage(self, state, price, volume, timestamp, vol_percentile):
        direction = state["direction"]
        boundary_price = state.get("boundary_price", state["entry_price"])
        stage = state["stage"]
        elapsed = timestamp - state["stage_start_time"]

        price_change = (price - boundary_price) * direction

        # 波动率自适应阈值：高波时阈值放大，低波时阈值缩小（低波信号更可靠）
        vol_adj = max(0.5, min(2.0, vol_percentile / 50.0))
        favorable_threshold = boundary_price * self.BASE_FAVORABLE_BREAK_PCT * vol_adj
        unfavorable_threshold = boundary_price * self.BASE_UNFAVORABLE_BREAK_PCT * vol_adj
        strong_favorable_threshold = favorable_threshold * 1.5
        early_term_threshold = max(boundary_price * self.MIN_EARLY_TERMINATION_PCT,
                                   boundary_price * self.BASE_EARLY_TERMINATION_PCT * vol_adj)
        recovery_threshold = boundary_price * self.MIN_RECOVERY_PCT

        # 成交量确认
        recent_vol_samples = list(state["volume_history"])[-10:]
        enough_samples = len(recent_vol_samples) >= self.VOLUME_MIN_SAMPLES
        recent_vol_avg = sum(recent_vol_samples) / len(recent_vol_samples) if recent_vol_samples else 0
        volume_confirmed = enough_samples and volume > recent_vol_avg * self.VOLUME_CONFIRM_MULT

        is_favorable = price_change > favorable_threshold
        is_strong_favorable = price_change > strong_favorable_threshold
        is_unfavorable = price_change < -unfavorable_threshold
        is_strong_unfavorable = price_change < -early_term_threshold

        # 试探阶段
        if stage == "probing":
            if elapsed >= state["probing_seconds"]:
                if is_favorable and volume_confirmed:
                    return self._StageEvaluation("resolution_favorable", SuggestedAction.RESTORE.value, "试探结束，价格有利且量能确认")
                elif is_favorable:
                    return self._StageEvaluation("resolution_favorable", SuggestedAction.RESTORE.value, "试探结束，价格有利突破")
                elif is_unfavorable and volume_confirmed:
                    return self._StageEvaluation("resolution_unfavorable", SuggestedAction.EXIT.value, "试探结束，价格不利且量能确认")
                elif is_unfavorable:
                    return self._StageEvaluation("resolution_unfavorable", SuggestedAction.EXIT.value, "试探结束，价格不利")
                else:
                    return self._StageEvaluation("wrestling", SuggestedAction.HOLD.value, "试探结束，价格横盘，进入拉锯")
            else:
                # 提前终止：极端不利且（量能确认 或 成交量样本不足时保守处理）
                if is_strong_unfavorable and (volume_confirmed or not enough_samples):
                    if state.get("original_position_size", 0) > 0.05:
                        logger.error(f"大仓位穿越试探期强反向 #RECOVERY: 审查仓位规模")
                    return self._StageEvaluation("resolution_unfavorable", SuggestedAction.EXIT.value, "试探期价格剧烈反向，提前撤离")
                return self._StageEvaluation("probing", SuggestedAction.HOLD.value, "试探阶段中")

        # 拉锯阶段
        elif stage == "wrestling":
            if elapsed >= state["wrestling_seconds"]:
                reason_prefix = "拉锯超时"
                if price_change >= recovery_threshold:
                    return self._StageEvaluation("resolution_favorable", SuggestedAction.RESTORE.value, f"{reason_prefix}，价格小幅有利，恢复仓位")
                elif price_change <= -recovery_threshold:
                    return self._StageEvaluation("resolution_unfavorable", SuggestedAction.EXIT.value, f"{reason_prefix}，价格小幅不利，退出")
                else:
                    return self._StageEvaluation("resolution_favorable", SuggestedAction.RESTORE.value, f"{reason_prefix}，价格未明确背离，保守恢复")
            if is_strong_favorable and volume_confirmed:
                return self._StageEvaluation("resolution_favorable", SuggestedAction.RESTORE.value, "价格强力突破且量能确认")
            if is_strong_favorable and not volume_confirmed:
                return self._StageEvaluation("resolution_favorable", SuggestedAction.RESTORE.value, "价格极端有利，无量能确认但恢复")
            if is_unfavorable and volume_confirmed:
                return self._StageEvaluation("resolution_unfavorable", SuggestedAction.EXIT.value, "价格不利突破且量能确认")
            return self._StageEvaluation("wrestling", SuggestedAction.HOLD.value, "拉锯阶段中")

        return self._StageEvaluation(stage, state.get("suggested_action", SuggestedAction.HOLD.value), state.get("resolution_reason", "终局"))

    def _calculate_adjustment(self, state, current_price: float = None, normalized_symbol: str = None):
        stage = state["stage"]
        pos_mult = self.DEFAULT_PROBING_POSITION_MULT
        stop_adj = 0.0
        boundary_price = state.get("boundary_price", state["entry_price"])
        direction = state["direction"]

        if current_price is None:
            price_history = state["price_history"]
            current_price = price_history[-1] if price_history else boundary_price

        zone_thickness = self._get_zone_thickness(state.get("raw_symbol", normalized_symbol or ""))
        zone_info = state.get("zone_info", {})
        atr = zone_info.get("atr")
        if atr and atr > 0:
            thickness_offset = atr * zone_thickness * 0.5
        else:
            thickness_offset = boundary_price * zone_thickness * 0.5

        if stage == "probing":
            pos_mult = self.DEFAULT_PROBING_POSITION_MULT
            stop_anchor = boundary_price - direction * thickness_offset
        elif stage == "wrestling":
            pos_mult = self.DEFAULT_WRESTLING_POSITION_MULT
            stop_anchor = boundary_price - direction * thickness_offset
        elif stage == "resolution_favorable":
            pos_mult = self.DEFAULT_FAVORABLE_RESTORE_PCT
            safe_anchor = current_price - direction * thickness_offset * 0.5
            stop_anchor = max(safe_anchor, boundary_price) if direction == 1 else min(safe_anchor, boundary_price)
        elif stage == "resolution_unfavorable":
            pos_mult = 0.0
            stop_adj = self.DEFAULT_UNFAVORABLE_STOP_TIGHTEN
            stop_anchor = current_price - direction * thickness_offset * 0.3
            # 全平时止损锚点无效
            stop_anchor = None
        else:
            stop_anchor = boundary_price

        if stage == "resolution_unfavorable" and state.get("original_position_size", 0) > 0.05:
            logger.error(f"大仓位穿越失败 #RECOVERY: 检查区间有效性")

        if self._profit_compression and stage in ("probing", "wrestling"):
            try:
                comp_stage = self._profit_compression.get_compression_stage(
                    state.get("raw_symbol", normalized_symbol or "")
                )
                if comp_stage in ("large_profit", "extreme"):
                    pos_mult *= 0.9
                    stop_adj = max(stop_adj, 0.1)
            except Exception as e:
                logger.warning(f"查询紧缩利润阶段失败: {e} #RECOVERY: 检查ProfitCompression")

        pos_mult = max(self.DEFAULT_MIN_POSITION_MULT, pos_mult)
        pos_mult = min(1.0, pos_mult)

        return round(pos_mult, 3), round(stop_adj, 3), stop_anchor

    def _log_and_publish(self, snapshot: Dict) -> None:
        logger.info("%s 阶段切换: %s -> %s (原因: %s) 动作=%s 仓位=%.2f",
                    snapshot["symbol"], snapshot["old_stage"], snapshot["new_stage"],
                    snapshot["reason"], snapshot["action"], snapshot["pos_mult"])
        if self._negotiation_bus and hasattr(self._negotiation_bus, 'publish_event'):
            try:
                self._negotiation_bus.publish_event(
                    event_type=self.EVENT_TYPE_STAGE_CHANGE,
                    symbol=snapshot["symbol"],
                    stage=snapshot["new_stage"],
                    suggested_action=snapshot["action"],
                    timestamp=time.time(),  # Unix时间戳
                    monotonic_time=time.monotonic(),
                )
            except Exception as e:
                logger.warning(f"事件推送失败，写入本地缓冲区: {e} #RECOVERY: 检查NegotiationBus")
                self._event_buffer.append({
                    "event": self.EVENT_TYPE_STAGE_CHANGE,
                    "symbol": snapshot["symbol"],
                    "stage": snapshot["new_stage"],
                    "action": snapshot["action"],
                    "timestamp": time.time(),
                })

    def _cleanup_loop(self) -> None:
        while not self._stop_cleanup.is_set():
            self._stop_cleanup.wait(self.BACKGROUND_CLEANUP_INTERVAL_SEC)
            with self._lock:
                now = time.monotonic()
                to_remove = [sym for sym, st in self._monitors.items()
                             if now - st["last_update_time"] > self.DEFAULT_MONITOR_TIMEOUT_SEC
                             and not (st["stage"].startswith("resolution_") and now < st.get("resolution_linger_until", 0))]
            for sym in to_remove:
                with self._lock:
                    self._monitors.pop(sym, None)
                logger.warning(f"后台清理超时监控: {sym}")
            if to_remove:
                logger.info(f"后台清理完成: {len(to_remove)} 个超时监控")

    def _atexit_cleanup(self) -> None:
        self._stop_cleanup.set()
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=3.0)
            if self._cleanup_thread.is_alive():
                logger.warning("清理线程未在3秒内退出")

    def __del__(self):
        self._atexit_cleanup()
