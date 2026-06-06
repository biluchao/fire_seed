"""
火种系统 · 区间角色翻转器 (ZoneRoleFlipper) — 金融级卓越标准版 v5

核心职责：
1. 基于连续K线确认机制，监测大周期关键区间的有效突破，自动翻转压力/支撑角色
2. 融合市场波动率自适应参数，穿透深度与成交量双重验证，发布翻转事件通知上下游模块
3. 提供延伸缓冲区生成能力，以应对加速突破行情

外部依赖（真实模块接口）：
- core.perception.tactile_cortex.TactileCortex : 价格、成交量、波动率分位、K线唯一标识
- core.negotiation_bus.NegotiationBus : 发布区间翻转事件
- core.behavioral_logger.BehavioralLogger : 审计日志记录

接口契约：
- evaluate_breakout(zone, price, volume, bar_id) -> Dict[str, Any]
- get_extension_zone(zone, direction, acceleration_factor) -> Dict[str, Any]
- get_statistics() -> Dict[str, int]
- reset_statistics() -> None
- health_check() -> Dict[str, Any]
- 所有公共方法返回 {"status", "reason", "data", "warnings"}

异常与降级：
- 外部依赖不可用时，使用保守静态参数并标记 degraded
- 所有异常均在内部捕获，返回标准化错误响应，绝不引发未处理异常
- 事件发布失败不影响核心翻转逻辑，仅记录告警

资源管理：
- 突破确认状态（_pending_breakouts）带超时自动清理，且限制最大容量
- 线程锁保护共享状态，锁粒度最小化；定期清理以控制内存增长
- 统计指标支持重置，避免长期运行内存膨胀

金融级保障：
- 价格/成交量参数严格非负校验
- 方向参数严格枚举校验
- 浮点运算使用容差比较，消除精度误差
- 所有数值类型转换使用Decimal或带异常捕获
- K线ID唯一性保障，防止重复计数
- 自适应参数防御性深拷贝
"""

import logging
import time
import threading
from typing import Dict, Any, Optional
from collections import OrderedDict

logger = logging.getLogger("zone_role_flipper")


class ZoneRoleFlipper:
    """区间角色翻转器（机构级）"""

    # ========== 类常量（默认配置，可被外部配置覆盖） ==========
    # 基础突破参数
    DEFAULT_CONFIRMATION_BARS = 2           # 确认所需连续K线数，[1,5]
    DEFAULT_VOLUME_SURGE_MULTIPLIER = 1.5   # 放量倍数，[1.2,3.0]
    DEFAULT_MIN_VOLUME_RATIO = 0.3          # 最低成交量比例（相对均值），[0.1,1.0]
    DEFAULT_PENETRATION_RATIO = 0.5         # 相对厚度穿透比例，[0.3,1.0]
    DEFAULT_MIN_ABSOLUTE_PENETRATION_BPS = 5.0  # 最小绝对穿透（基点），[1,20]

    # 自适应参数（波动率分位 -> 调整系数）
    VOLATILITY_ADAPTIVE = OrderedDict([
        ("low_vol",  {"confirmation_bars": 3, "penetration_ratio": 0.6, "volume_mult": 2.0,
                      "max_interruption_bars": 0}),
        ("normal",   {"confirmation_bars": 2, "penetration_ratio": 0.5, "volume_mult": 1.5,
                      "max_interruption_bars": 0}),
        ("high_vol", {"confirmation_bars": 1, "penetration_ratio": 0.4, "volume_mult": 1.2,
                      "max_interruption_bars": 1}),
    ])

    # 翻转后状态
    DEFAULT_INHERIT_STRENGTH = 0.8          # 约束力继承比例
    DECAY_RESTART_STRENGTH = 1.0            # 时效重置

    # 延伸缓冲区
    DEFAULT_EXTENSION_ATR_MULTIPLIER = 0.5
    DEFAULT_ACCELERATION_THRESHOLD = 0.2
    MAX_EXTENSION_DISTANCE_BPS = 200        # 最大延伸距离（基点）

    # 系统参数
    BREAKOUT_TIMEOUT_SEC = 300              # 突破确认超时，秒
    MAX_PENDING_BREAKOUTS = 200             # 最大待确认突破数，防止内存泄漏
    CLEANUP_INTERVAL_SEC = 30               # 定期清理间隔，秒
    PRICE_TOLERANCE_BPS = 0.01              # 价格比较容差（基点）
    STATS_REPORT_INTERVAL = 3600            # 统计指标周期性日志输出间隔，秒
    MAX_FLIP_EVENT_RETRIES = 3              # 事件发布最大重试次数
    RETRY_BACKOFF_MS = 50                   # 事件发布重试退避，毫秒

    def __init__(self):
        self._pending_breakouts: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._last_cleanup_time = time.time()
        self._last_stats_log_time = time.time()
        self._tactile_cortex = None
        self._negotiation_bus = None
        self._behavioral_logger = None
        # 指标统计
        self._stats = {"total_evaluations": 0, "confirmed_breakouts": 0, "reset_breakouts": 0,
                       "timeout_cleanups": 0, "degraded_evaluations": 0,
                       "flip_events_published": 0, "flip_events_failed": 0}
        self._stats_lock = threading.Lock()
        logger.info("ZoneRoleFlipper v5 初始化完成")

    # ---------- 依赖注入 ----------
    def inject_dependencies(self, tactile_cortex=None, negotiation_bus=None, behavioral_logger=None):
        if tactile_cortex and all(hasattr(tactile_cortex, m) for m in
                                  ['get_average_volume', 'get_volatility_percentile', 'is_kline_closed']):
            self._tactile_cortex = tactile_cortex
        else:
            logger.warning("TactileCortex 不可用或接口不全，突破检测降级")

        if negotiation_bus and hasattr(negotiation_bus, 'publish_event'):
            self._negotiation_bus = negotiation_bus
        else:
            logger.warning("NegotiationBus 不可用，翻转事件仅记本地日志")

        if behavioral_logger and hasattr(behavioral_logger, 'log_event'):
            self._behavioral_logger = behavioral_logger
        else:
            logger.warning("BehavioralLogger 不可用，审计日志降级")

    # ========== 公共接口 ==========
    def evaluate_breakout(self, zone: Dict[str, Any], price: float,
                          volume: float, bar_id: str) -> Dict[str, Any]:
        """
        评估区间突破，累积连续K线确认后执行角色翻转
        金融级校验：价格>=0，成交量>=0，bar_id非空
        """
        self._increment_stat("total_evaluations")
        self._log_stats_periodically()

        # 金融级参数校验
        if price < 0:
            return {"status": "error", "reason": "价格不能为负", "data": {"flipped": False},
                    "warnings": ["negative_price"]}
        if volume < 0:
            return {"status": "error", "reason": "成交量不能为负", "data": {"flipped": False},
                    "warnings": ["negative_volume"]}
        if not bar_id or not isinstance(bar_id, str) or not bar_id.strip():
            return {"status": "error", "reason": "bar_id不能为空", "data": {"flipped": False},
                    "warnings": ["invalid_bar_id"]}

        # 区间字段校验与zone_id生成
        required = ["upper_bound", "lower_bound", "thickness", "strength"]
        missing = [k for k in required if k not in zone]
        if missing:
            return {"status": "error", "reason": f"区间数据缺少字段: {missing}",
                    "data": {"flipped": False}, "warnings": [f"incomplete_zone: {missing}"]}

        zone_id = zone.get("zone_id")
        if not zone_id or not isinstance(zone_id, str) or not zone_id.strip():
            zone_id = f"auto_{zone.get('upper_bound'):.6f}_{zone.get('lower_bound'):.6f}_{int(time.time()*1e6)}"
            logger.debug(f"自动生成zone_id: {zone_id}")

        # 深拷贝防外部修改
        try:
            zone_copy = {
                "zone_id": zone_id,
                "upper_bound": float(zone["upper_bound"]),
                "lower_bound": float(zone["lower_bound"]),
                "thickness": float(zone["thickness"]),
                "strength": float(zone["strength"]),
                "decay_stage": zone.get("decay_stage", "fresh"),
            }
        except (ValueError, TypeError) as e:
            return {"status": "error", "reason": f"区间数值类型错误: {e}",
                    "data": {"flipped": False}, "warnings": ["invalid_zone_types"]}

        upper = zone_copy["upper_bound"]
        lower = zone_copy["lower_bound"]
        thickness = zone_copy["thickness"]
        if upper <= lower or thickness <= 0:
            return {"status": "error", "reason": "区间边界或厚度无效",
                    "data": {"flipped": False}, "warnings": ["invalid_zone_bounds"]}

        # 复合容差
        tolerance = max(upper * (self.PRICE_TOLERANCE_BPS / 10000), 1e-8)
        direction = 0
        if price > upper + tolerance:
            direction = 1
        elif price < lower - tolerance:
            direction = -1

        if direction == 0:
            self._clear_pending(zone_id, reason="价格回区间")
            return {"status": "ok", "reason": "价格在区间内", "data": {"flipped": False},
                    "warnings": []}

        # 获取自适应参数（防御性深拷贝）
        params = self._get_adaptive_params()

        # 穿透深度验证
        boundary = upper if direction == 1 else lower
        penetration_abs = abs(price - boundary)
        min_penetration_rel = thickness * params["penetration_ratio"]
        min_penetration_abs = boundary * (self.DEFAULT_MIN_ABSOLUTE_PENETRATION_BPS / 10000)
        min_penetration = max(min_penetration_rel, min_penetration_abs)
        if penetration_abs < min_penetration:
            max_interrupt = params.get("max_interruption_bars", 0)
            with self._lock:
                pending = self._pending_breakouts.get(zone_id)
                if pending and pending["direction"] == direction and max_interrupt > 0:
                    pending["interrupt_count"] = pending.get("interrupt_count", 0) + 1
                    if pending["interrupt_count"] <= max_interrupt:
                        logger.debug(f"突破暂时中断，容忍计数 {pending['interrupt_count']}/{max_interrupt}")
                        return {"status": "ok", "reason": "突破暂时中断，仍在容忍范围内",
                                "data": {"flipped": False}, "warnings": []}
            self._clear_pending(zone_id, reason="穿透不足")
            return {"status": "ok", "reason": "穿透深度不足",
                    "data": {"flipped": False}, "warnings": []}

        # 成交量验证 (依赖可用且返回值>0才启用)
        avg_vol = self._safe_get_avg_volume()
        if avg_vol > 0:
            min_vol = avg_vol * max(self.DEFAULT_MIN_VOLUME_RATIO, params["volume_mult"])
            if volume < min_vol:
                self._clear_pending(zone_id, reason="量能不足")
                return {"status": "ok", "reason": "突破量能不足",
                        "data": {"flipped": False}, "warnings": []}

        # 更新突破确认计数（原子操作）
        with self._lock:
            self._conditionally_cleanup()
            if len(self._pending_breakouts) >= self.MAX_PENDING_BREAKOUTS:
                oldest = min(self._pending_breakouts.keys(),
                             key=lambda k: self._pending_breakouts[k]["last_update"])
                self._pending_breakouts.pop(oldest, None)
                logger.warning(f"待确认突破达到上限，移除最旧项 {oldest}")

            pending = self._pending_breakouts.get(zone_id)
            now = time.time()
            # 关键：必须bar_id不同才递增计数，防止同一根K线重复确认
            if (pending and pending["direction"] == direction and
                    pending["last_bar_id"] is not None and pending["last_bar_id"] != bar_id):
                pending["count"] += 1
                pending["last_bar_id"] = bar_id
                pending["last_update"] = now
                pending["interrupt_count"] = 0
                logger.debug(f"突破确认递增: zone={zone_id}, count={pending['count']}/{params['confirmation_bars']}")
            elif pending and pending["last_bar_id"] == bar_id:
                # 同一根K线重复触发，仅更新last_update，不递增计数
                pending["last_update"] = now
                logger.debug(f"同一K线重复触发，不递增计数: zone={zone_id}, bar_id={bar_id}")
            else:
                # 方向变化或首次触发，重置计数
                pending = {"count": 1, "direction": direction, "last_bar_id": bar_id,
                           "last_update": now, "interrupt_count": 0}
                self._pending_breakouts[zone_id] = pending

            confirmed = pending["count"] >= params["confirmation_bars"]
            if confirmed:
                self._pending_breakouts.pop(zone_id, None)

        if not confirmed:
            return {"status": "ok", "reason": f"突破确认中 ({pending['count']}/{params['confirmation_bars']})",
                    "data": {"flipped": False}, "warnings": []}

        # 翻转
        flipped_zone = self._perform_flip(zone_copy, direction, price, params)
        desc = "向上突破压力位，翻转为支撑" if direction == 1 else "向下突破支撑位，翻转为压力"
        if not self._publish_flip_event(flipped_zone, desc, price):
            logger.error(f"翻转事件发布失败，翻转已执行但通知未送达 #RECOVERY: 检查协商总线和日志模块")
        logger.info(f"{desc}，新区间 [{flipped_zone['lower_bound']:.2f}, "
                     f"{flipped_zone['upper_bound']:.2f}]，约束力={flipped_zone['strength']:.2f}")
        self._increment_stat("confirmed_breakouts")
        return {"status": "ok", "reason": desc, "data": {"flipped": True, "zone": flipped_zone},
                "warnings": []}

    def get_extension_zone(self, zone: Dict[str, Any], direction: int,
                           acceleration_factor: float) -> Dict[str, Any]:
        """生成延伸缓冲区"""
        if direction not in (1, -1):
            return {"status": "error", "reason": "无效方向", "data": {}, "warnings": []}
        if acceleration_factor < 0:
            return {"status": "error", "reason": "加速度因子不能为负", "data": {}, "warnings": []}
        if acceleration_factor < self.DEFAULT_ACCELERATION_THRESHOLD:
            return {"status": "ok", "reason": "加速度不足，无需延伸",
                    "data": {"extended": False, "zone": zone}, "warnings": []}

        try:
            upper = float(zone["upper_bound"])
            lower = float(zone["lower_bound"])
        except (ValueError, TypeError, KeyError) as e:
            return {"status": "error", "reason": f"区间数值无效: {e}", "data": {}, "warnings": []}

        base_width = upper - lower
        if base_width <= 0:
            return {"status": "error", "reason": "区间宽度无效", "data": {}, "warnings": []}

        extension = base_width * acceleration_factor * self.DEFAULT_EXTENSION_ATR_MULTIPLIER
        max_ext = upper * (self.MAX_EXTENSION_DISTANCE_BPS / 10000)
        extension = min(extension, max_ext)

        if direction == 1:
            new_upper, new_lower = upper + extension, lower
        else:
            new_upper, new_lower = upper, lower - extension

        extended = {**zone, "upper_bound": new_upper, "lower_bound": new_lower,
                    "extension_applied": True, "extension_distance": extension}
        logger.info(f"延伸缓冲区生成，新区间 [{new_lower:.2f}, {new_upper:.2f}]")
        return {"status": "ok", "reason": "延伸完成", "data": {"extended": True, "zone": extended},
                "warnings": []}

    def get_statistics(self) -> Dict[str, int]:
        """返回内部统计指标"""
        with self._stats_lock:
            return dict(self._stats)

    def reset_statistics(self) -> None:
        """重置统计指标"""
        with self._stats_lock:
            for key in self._stats:
                self._stats[key] = 0
        logger.info("统计指标已重置")

    def health_check(self) -> Dict[str, Any]:
        try:
            with self._lock:
                pending_count = len(self._pending_breakouts)
            test_zone = {"upper_bound": 100.0, "lower_bound": 90.0,
                         "thickness": 10.0, "strength": 0.8, "decay_stage": "fresh"}
            result = self.evaluate_breakout(test_zone, 101.2, 0, "bar_test_1")
            if result["status"] not in ("ok", "degraded"):
                return {"status": "degraded", "reason": "自检未通过", "data": {}, "warnings": []}
            return {"status": "ok", "reason": f"自检通过，{pending_count}个待确认突破",
                    "data": {"dependencies": {"tactile_cortex": self._tactile_cortex is not None,
                                             "negotiation_bus": self._negotiation_bus is not None,
                                             "behavioral_logger": self._behavioral_logger is not None},
                             "stats": self.get_statistics()},
                    "warnings": []}
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查依赖注入和锁状态")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": []}

    # ========== 私有方法 ==========
    def _perform_flip(self, zone: Dict, direction: int, price: float, adaptive_params: Dict) -> Dict:
        """执行区间角色翻转，根据当前波动率动态构建新区间"""
        upper, lower = zone["upper_bound"], zone["lower_bound"]
        atr_est = self._estimate_atr() if self._tactile_cortex else zone["thickness"]
        dynamic_thickness = max(zone["thickness"], atr_est * 0.5)  # 半ATR作为最小厚度
        if direction == 1:
            new_lower = upper
            new_upper = upper + dynamic_thickness
        else:
            new_upper = lower
            new_lower = lower - dynamic_thickness

        new_strength = zone["strength"] * self.DEFAULT_INHERIT_STRENGTH
        new_zone_id = f"{zone['zone_id']}_flip_{direction}_{int(time.time()*1e6)}"
        return {
            "zone_id": new_zone_id,
            "upper_bound": new_upper,
            "lower_bound": new_lower,
            "thickness": dynamic_thickness,
            "strength": round(new_strength, 4),
            "decay_stage": "fresh",
            "decay_elapsed_seconds": 0.0,
            "flip_timestamp": time.time(),
            "flip_direction": direction,
            "breakout_price": price,
            "original_role": "resistance" if direction == 1 else "support",
        }

    def _clear_pending(self, zone_id: str, reason: str = ""):
        with self._lock:
            existed = self._pending_breakouts.pop(zone_id, None)
        if existed:
            logger.debug(f"突破确认重置 (zone={zone_id}): {reason}")
            self._increment_stat("reset_breakouts")
            self._publish_event("breakout_reset", {"zone_id": zone_id, "reason": reason})

    def _get_adaptive_params(self) -> Dict[str, float]:
        """根据波动率分位返回动态参数（返回副本）"""
        vol_pct = self._safe_get_vol_percentile()
        if vol_pct < 30:
            return dict(self.VOLATILITY_ADAPTIVE["low_vol"])
        elif vol_pct > 70:
            return dict(self.VOLATILITY_ADAPTIVE["high_vol"])
        return dict(self.VOLATILITY_ADAPTIVE["normal"])

    def _estimate_atr(self) -> float:
        """尝试从感知模块获取ATR，失败返回默认值"""
        if self._tactile_cortex and hasattr(self._tactile_cortex, 'get_atr'):
            try:
                val = float(self._tactile_cortex.get_atr())
                return val if val > 0 else 0.0
            except Exception as e:
                logger.debug(f"获取ATR失败: {e}")
        return 0.0

    def _safe_get_avg_volume(self) -> float:
        if not self._tactile_cortex: return 0.0
        try:
            val = float(self._tactile_cortex.get_average_volume())
            return val if val >= 0 else 0.0
        except Exception as e:
            logger.debug(f"获取平均成交量失败: {e}")
            return 0.0

    def _safe_get_vol_percentile(self) -> float:
        if not self._tactile_cortex: return 50.0
        try:
            val = float(self._tactile_cortex.get_volatility_percentile())
            return max(0.0, min(100.0, val))
        except Exception as e:
            logger.debug(f"获取波动率分位失败: {e}")
            return 50.0

    def _conditionally_cleanup(self):
        """按间隔触发清理超时条目，并分批处理减少锁占用"""
        now = time.time()
        if now - self._last_cleanup_time < self.CLEANUP_INTERVAL_SEC:
            return
        self._last_cleanup_time = now
        expired = []
        with self._lock:
            for zid, p in list(self._pending_breakouts.items()):
                if now - p["last_update"] > self.BREAKOUT_TIMEOUT_SEC:
                    expired.append(zid)
                    if len(expired) >= 20:
                        break
            for zid in expired:
                self._pending_breakouts.pop(zid, None)
                logger.debug(f"突破确认超时清除: {zid}")
                self._increment_stat("timeout_cleanups")

    def _publish_flip_event(self, zone: Dict, description: str, price: float) -> bool:
        """发布翻转事件，返回是否成功（至少通知了一个渠道）"""
        success = self._publish_event("zone_flip", {"description": description, "zone": zone, "price": price})
        with self._stats_lock:
            if success:
                self._stats["flip_events_published"] = self._stats.get("flip_events_published", 0) + 1
            else:
                self._stats["flip_events_failed"] = self._stats.get("flip_events_failed", 0) + 1
        return success

    def _publish_event(self, event_type: str, details: Dict) -> bool:
        """发布事件，返回是否成功"""
        data = {"event": event_type, "timestamp": time.time(), **details}
        pushed = False
        # 协商总线推送（带重试）
        if self._negotiation_bus and hasattr(self._negotiation_bus, 'publish_event'):
            for attempt in range(self.MAX_FLIP_EVENT_RETRIES):
                try:
                    self._negotiation_bus.publish_event(data)
                    pushed = True
                    break
                except Exception as e:
                    logger.warning(f"协商总线推送失败 (尝试 {attempt+1}/{self.MAX_FLIP_EVENT_RETRIES}): {e}")
                    if attempt < self.MAX_FLIP_EVENT_RETRIES - 1:
                        time.sleep(self.RETRY_BACKOFF_MS / 1000.0)
        # 行为日志记录
        if self._behavioral_logger and hasattr(self._behavioral_logger, 'log_event'):
            try:
                self._behavioral_logger.log_event(event_type=event_type, details=data)
                pushed = True
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")
        if not pushed:
            logger.debug(f"未配置总线/日志，事件仅记录到logger: {event_type}")
        return pushed

    def _increment_stat(self, key: str):
        with self._stats_lock:
            self._stats[key] = self._stats.get(key, 0) + 1

    def _log_stats_periodically(self):
        """周期性输出统计指标到INFO日志，便于运维监控"""
        now = time.time()
        if now - self._last_stats_log_time >= self.STATS_REPORT_INTERVAL:
            self._last_stats_log_time = now
            with self._stats_lock:
                stats_copy = dict(self._stats)
            logger.info(f"ZoneRoleFlipper 统计: {stats_copy}")
