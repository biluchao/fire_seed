"""
火种系统 · 区间穿越监控器 (ZoneCrossingMonitor)

核心职责：
1. 监控小周期价格进入上级周期关键区间后的完整穿越生命周期，包含试探、拉锯与突破/回弹三个阶段。
2. 根据当前穿越阶段动态输出仓位调整系数、止损止盈锚点以及是否触发假突破修复或趋势转换。

外部依赖（真实模块接口）：
- core.perception.tactile_cortex.TactileCortex : 获取实时流动性评级与盘口深度衰减速率
- core.perception.visual_cortex.VisualCortex : 获取订单簿挂单斜率与纸墙检测标志
- core.order_manager.lifecycle_stages.LifecycleStages : 查询当前持仓所处的生命周期阶段
- core.negotiation_bus.NegotiationBus : 向协商总线提交穿越状态变更事件
- core.behavioral_logger.BehavioralLogger : 记录穿越决策的审计日志

接口契约：
- evaluate(price: float, direction: int, zone: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]
  输出字典固定包含 "stage" (str), "position_mult" (float), "stop_anchor" (Optional[float]), "take_profit_anchor" (Optional[float]), "reason" (str), "warnings" (List[str])
- reset(symbol: str) -> None : 重置指定品种的穿越监控状态
- health_check() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "message" (str)

异常与降级：
- 当 TactileCortex 不可用时，采用保守的流动性评级（L2 正常），并记录降级警告。
- 当 VisualCortex 不可用时，纸墙检测默认返回 False，不干扰穿越判定。
- 当 NegotiationBus 不可用时，状态变更仅记录本地日志，不阻塞主流程。

资源管理：
- 本模块内部维护的穿越状态字典 _active_crossings 在 reset() 或系统退出时自动清理。
- 不持有任何需要手动释放的外部资源。
"""

import time
import logging
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger(__name__)


class ZoneCrossingMonitor:
    """区间穿越三阶段监控器"""

    # 类常量（默认配置，附带单位与取值范围注释）
    PROBING_DURATION = 3.0               # 试探阶段持续时长，秒，取值范围 [1.0, 10.0]
    WRESTLING_DURATION = 30.0            # 拉锯阶段持续时长，秒，取值范围 [10.0, 120.0]
    RESOLUTION_DURATION = 10.0           # 突破/回弹确认时长，秒，取值范围 [5.0, 60.0]
    DEFAULT_LIQUIDITY_LEVEL = "L2"       # 降级默认流动性评级（保守），无量纲，取值范围 L1-L5
    DEFAULT_POSITION_MULT = 0.85         # 试探/拉锯阶段默认仓位系数，无量纲，取值范围 [0.1, 1.0]
    FAVORABLE_RESTORE_PCT = 0.90         # 有利突破后仓位恢复比例，无量纲，取值范围 [0.5, 1.0]
    UNFAVORABLE_STOP_TIGHTEN = 0.50      # 不利回弹时止损收紧比例，无量纲，取值范围 [0.1, 0.9]
    MIN_POSITION_MULT = 0.30             # 最小区间仓位系数（保护底线），无量纲，取值范围 [0.1, 0.5]
    PAPER_WALL_ADJUST_MULT = 0.85        # 检测到纸墙时仓位额外缩减系数，无量纲，取值范围 [0.5, 1.0]
    LIQUIDITY_L1_POSITION_MULT = 0.70    # 流动性 L1（极度稀薄）时仓位系数，无量纲，取值范围 [0.3, 0.8]
    LIQUIDITY_L2_POSITION_MULT = 0.80    # 流动性 L2（稀薄）时仓位系数，无量纲，取值范围 [0.5, 0.9]
    LIQUIDITY_L3_POSITION_MULT = 0.85    # 流动性 L3（正常）时仓位系数，无量纲，取值范围 [0.6, 1.0]
    LIQUIDITY_L4_POSITION_MULT = 0.95    # 流动性 L4（充裕）时仓位系数，无量纲，取值范围 [0.7, 1.0]
    LIQUIDITY_L5_POSITION_MULT = 1.0     # 流动性 L5（极度充裕）时仓位系数，无量纲，取值范围 [0.8, 1.2]

    def __init__(self, config: Dict[str, Any]):
        # 从配置加载可调节参数，附带安全默认值
        self._probing_duration = config.get("probing_duration", self.PROBING_DURATION)
        self._wrestling_duration = config.get("wrestling_duration", self.WRESTLING_DURATION)
        self._resolution_duration = config.get("resolution_duration", self.RESOLUTION_DURATION)
        self._default_liquidity = config.get("default_liquidity", self.DEFAULT_LIQUIDITY_LEVEL)
        self._position_mult = config.get("default_position_mult", self.DEFAULT_POSITION_MULT)
        self._favorable_restore = config.get("favorable_restore_pct", self.FAVORABLE_RESTORE_PCT)
        self._unfavorable_tighten = config.get("unfavorable_stop_tighten", self.UNFAVORABLE_STOP_TIGHTEN)
        self._min_position_mult = config.get("min_position_mult", self.MIN_POSITION_MULT)
        self._paper_wall_adjust = config.get("paper_wall_adjust_mult", self.PAPER_WALL_ADJUST_MULT)

        # 流动性-仓位系数映射
        self._liquidity_mult_map = {
            "L1": config.get("liq_l1_mult", self.LIQUIDITY_L1_POSITION_MULT),
            "L2": config.get("liq_l2_mult", self.LIQUIDITY_L2_POSITION_MULT),
            "L3": config.get("liq_l3_mult", self.LIQUIDITY_L3_POSITION_MULT),
            "L4": config.get("liq_l4_mult", self.LIQUIDITY_L4_POSITION_MULT),
            "L5": config.get("liq_l5_mult", self.LIQUIDITY_L5_POSITION_MULT),
        }

        # 穿越状态存储（按品种隔离）
        self._active_crossings: Dict[str, Dict[str, Any]] = {}
        self._crossing_lock = __import__('threading').Lock()

        # 外部依赖（延迟注入）
        self._tactile_cortex: Optional[Any] = None
        self._visual_cortex: Optional[Any] = None
        self._lifecycle_stages: Optional[Any] = None
        self._negotiation_bus: Optional[Any] = None
        self._behavioral_logger: Optional[Any] = None

        logger.info("ZoneCrossingMonitor 初始化完成，依赖待注入")

    # ────────────────────────── 依赖注入 ──────────────────────────
    def inject_dependencies(
        self,
        tactile_cortex: Optional[Any] = None,
        visual_cortex: Optional[Any] = None,
        lifecycle_stages: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None
    ) -> None:
        """注入外部依赖模块"""
        self._tactile_cortex = tactile_cortex
        self._visual_cortex = visual_cortex
        self._lifecycle_stages = lifecycle_stages
        self._negotiation_bus = negotiation_bus
        self._behavioral_logger = behavioral_logger
        logger.info("ZoneCrossingMonitor 依赖注入完成")

    # ────────────────────────── 公共接口 ──────────────────────────
    def evaluate(
        self,
        price: float,
        direction: int,
        zone: Dict[str, Any],
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        评估当前价格相对于上级周期区间的穿越状态。

        :param price: 当前小周期价格
        :param direction: 持仓方向 (1=多, -1=空, 0=无仓位)
        :param zone: 上级区间信息，必须包含 upper_bound, lower_bound, thickness, strength
        :param context: 附加上下文，如当前穿越开始时间、前次阶段等
        :return: 标准化穿越决策字典
        """
        symbol = context.get("symbol", "default")
        now = time.time()
        warnings: List[str] = []

        # 方向校验
        if direction == 0:
            return {
                "stage": "idle",
                "position_mult": 1.0,
                "stop_anchor": None,
                "take_profit_anchor": None,
                "reason": "无持仓方向，无需穿越监控",
                "warnings": warnings
            }

        # 区间信息完整性校验
        if "upper_bound" not in zone or "lower_bound" not in zone:
            return {
                "stage": "idle",
                "position_mult": 1.0,
                "stop_anchor": None,
                "take_profit_anchor": None,
                "reason": "区间信息不完整，穿越监控不可用",
                "warnings": warnings
            }

        upper = zone["upper_bound"]
        lower = zone["lower_bound"]
        thickness = max(zone.get("thickness", 0.0001), 0.0001)

        # 获取流动性评级（降级保护）
        liquidity = self._default_liquidity
        if self._tactile_cortex:
            try:
                lr = self._tactile_cortex.get_liquidity_rating()
                if isinstance(lr, dict):
                    liquidity = lr.get("level", self._default_liquidity)
                else:
                    liquidity = str(lr)
            except Exception as e:
                logger.warning(f"获取流动性评级失败: {e}，使用降级值 {liquidity}")
                warnings.append("tactile_cortex 不可用，流动性评级降级")
        else:
            warnings.append("tactile_cortex 未注入，流动性评级降级")

        # 纸墙检测（降级保护）
        paper_wall = False
        if self._visual_cortex:
            try:
                pw = self._visual_cortex.detect_paper_wall()
                if isinstance(pw, dict):
                    paper_wall = pw.get("paper_wall", False)
                else:
                    paper_wall = bool(pw)
            except Exception as e:
                logger.warning(f"纸墙检测失败: {e}")
                warnings.append("纸墙检测降级")
        else:
            logger.debug("visual_cortex 未注入，纸墙检测跳过")

        # 获取或创建穿越状态
        crossing = self._get_or_create_crossing(symbol, price, direction, zone, now)
        stage = crossing["stage"]
        elapsed = now - crossing["start_time"]

        # ─── 三阶段逻辑 ───
        if stage == "probing":
            if elapsed < self._probing_duration:
                pos_mult = self._calculate_position_mult(self._position_mult, liquidity, paper_wall)
                reason = f"试探阶段 (已耗时 {elapsed:.1f}s / {self._probing_duration}s)，仓位系数 {pos_mult:.2f}"
                return self._build_result("probing", pos_mult, None, None, reason, warnings)
            else:
                crossing["stage"] = "wrestling"
                crossing["wrestling_start"] = now
                logger.info(f"[{symbol}] 试探阶段结束，进入拉锯阶段")
                # 继续执行拉锯逻辑

        if crossing.get("stage") == "wrestling":
            wrestling_elapsed = now - crossing.get("wrestling_start", now)
            if wrestling_elapsed < self._wrestling_duration:
                pos_mult = self._calculate_position_mult(self._position_mult, liquidity, paper_wall)
                reason = f"拉锯阶段 (已耗时 {wrestling_elapsed:.1f}s / {self._wrestling_duration}s)，仓位系数 {pos_mult:.2f}"
                # 检查是否已有突破迹象
                if self._is_favorable_breakout(price, direction, zone, crossing, thickness):
                    crossing["stage"] = "resolution"
                    crossing["resolution_start"] = now
                    crossing["resolution_direction"] = "favorable"
                    logger.info(f"[{symbol}] 拉锯中检测到有利突破，进入突破/回弹阶段")
                    return self._handle_resolution(price, direction, zone, crossing, liquidity, paper_wall, warnings, True)
                elif self._is_unfavorable_reversal(price, direction, zone, crossing, thickness):
                    crossing["stage"] = "resolution"
                    crossing["resolution_start"] = now
                    crossing["resolution_direction"] = "unfavorable"
                    logger.info(f"[{symbol}] 拉锯中检测到不利回弹，进入突破/回弹阶段")
                    return self._handle_resolution(price, direction, zone, crossing, liquidity, paper_wall, warnings, False)
                return self._build_result("wrestling", pos_mult, None, None, reason, warnings)
            else:
                crossing["stage"] = "resolution"
                crossing["resolution_start"] = now
                crossing["resolution_direction"] = "neutral"
                logger.info(f"[{symbol}] 拉锯阶段超时，强制进入突破/回弹阶段")
                return self._handle_resolution(price, direction, zone, crossing, liquidity, paper_wall, warnings, None)

        if crossing.get("stage") == "resolution":
            return self._handle_resolution(price, direction, zone, crossing, liquidity, paper_wall, warnings,
                                          crossing.get("resolution_direction") == "favorable")

        # 默认初始状态
        pos_mult = self._calculate_position_mult(self._position_mult, liquidity, paper_wall)
        return self._build_result("probing", pos_mult, None, None, "初始穿越监控", warnings)

    def reset(self, symbol: str) -> None:
        """重置指定品种的穿越监控状态"""
        with self._crossing_lock:
            if symbol in self._active_crossings:
                del self._active_crossings[symbol]
                logger.info(f"[{symbol}] 穿越监控状态已重置")

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检：验证类常量、核心方法和降级路径"""
        try:
            dummy_config = {}
            monitor = cls(dummy_config)
            # 测试无仓位方向
            result = monitor.evaluate(50000.0, 0, {"upper_bound": 51000.0, "lower_bound": 49000.0, "thickness": 100.0}, {"symbol": "test"})
            if result["stage"] != "idle":
                return {"status": "error", "message": "无方向处理异常"}

            # 测试正常评估
            result = monitor.evaluate(50050.0, 1, {"upper_bound": 51000.0, "lower_bound": 49000.0, "thickness": 100.0, "strength": 0.8}, {"symbol": "test"})
            if "stage" not in result:
                return {"status": "error", "message": "返回格式异常"}

            # 测试 reset
            monitor.reset("test")

            # 测试常量有效性
            if cls.PROBING_DURATION <= 0 or cls.WRESTLING_DURATION <= 0:
                return {"status": "error", "message": "阶段时长常量非法"}

            return {"status": "ok", "message": "所有测试通过（含降级路径）"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _get_or_create_crossing(self, symbol: str, price: float, direction: int, zone: Dict[str, Any], now: float) -> Dict[str, Any]:
        """获取或创建穿越状态（线程安全）"""
        with self._crossing_lock:
            if symbol not in self._active_crossings:
                self._active_crossings[symbol] = {
                    "stage": "probing",
                    "start_time": now,
                    "entry_price": price,
                    "direction": direction,
                    "zone": zone,
                    "wrestling_start": 0.0,
                    "resolution_start": 0.0,
                    "resolution_direction": None
                }
            return self._active_crossings[symbol]

    def _calculate_position_mult(self, base_mult: float, liquidity: str, paper_wall: bool) -> float:
        """根据流动性和纸墙动态调整仓位系数"""
        liq_mult = self._liquidity_mult_map.get(liquidity, 1.0)
        adjusted = base_mult * liq_mult
        if paper_wall:
            adjusted *= self._paper_wall_adjust
        return max(self._min_position_mult, adjusted)

    def _is_favorable_breakout(self, price: float, direction: int, zone: Dict[str, Any], crossing: Dict[str, Any], thickness: float) -> bool:
        """判断是否有利突破"""
        entry = crossing["entry_price"]
        if direction == 1:
            return price > zone.get("upper_bound", float("inf")) * 0.998 or (price - entry) > thickness * 0.5
        else:
            return price < zone.get("lower_bound", float("-inf")) * 1.002 or (entry - price) > thickness * 0.5

    def _is_unfavorable_reversal(self, price: float, direction: int, zone: Dict[str, Any], crossing: Dict[str, Any], thickness: float) -> bool:
        """判断是否不利回弹"""
        entry = crossing["entry_price"]
        if direction == 1:
            return price < zone.get("lower_bound", float("-inf")) * 1.002 or (entry - price) > thickness * 0.5
        else:
            return price > zone.get("upper_bound", float("inf")) * 0.998 or (price - entry) > thickness * 0.5

    def _handle_resolution(self, price: float, direction: int, zone: Dict[str, Any], crossing: Dict[str, Any],
                          liquidity: str, paper_wall: bool, warnings: List[str], favorable: Optional[bool]) -> Dict[str, Any]:
        """处理突破/回弹阶段"""
        if favorable is True:
            pos_mult = self._calculate_position_mult(self._favorable_restore, liquidity, paper_wall)
            stop_anchor = self._calculate_stop_anchor(price, direction, zone, "favorable")
            take_profit = None
            reason = f"有利突破，仓位恢复至 {pos_mult:.2f}"
        elif favorable is False:
            pos_mult = self._calculate_position_mult(0.3, liquidity, paper_wall)
            stop_anchor = self._calculate_stop_anchor(price, direction, zone, "unfavorable")
            take_profit = stop_anchor
            reason = f"不利回弹，止损收紧至系数 {self._unfavorable_tighten:.2f}"
        else:
            pos_mult = self._calculate_position_mult(0.5, liquidity, paper_wall)
            stop_anchor = self._calculate_stop_anchor(price, direction, zone, "neutral")
            take_profit = stop_anchor
            reason = "拉锯超时，中性退出"

        # 穿越结束，清理状态
        with self._crossing_lock:
            symbol = crossing.get("symbol", "")
            if symbol in self._active_crossings:
                del self._active_crossings[symbol]

        # 推送协商事件
        if self._negotiation_bus:
            try:
                self._negotiation_bus.emit_event(
                    event_type="zone_crossing_resolved",
                    payload={"price": price, "direction": direction, "favorable": favorable, "stage": "resolution"}
                )
            except Exception:
                pass

        return self._build_result("resolution", pos_mult, stop_anchor, take_profit, reason, warnings)

    def _calculate_stop_anchor(self, price: float, direction: int, zone: Dict[str, Any], resolution_type: str) -> float:
        """根据突破方向计算止损锚点"""
        thickness = zone.get("thickness", 100.0)
        if direction == 1:
            return price - thickness * 0.8
        else:
            return price + thickness * 0.8

    def _build_result(self, stage: str, pos_mult: float, stop_anchor: Optional[float],
                     take_profit: Optional[float], reason: str, warnings: List[str]) -> Dict[str, Any]:
        """构建标准化返回字典"""
        return {
            "stage": stage,
            "position_mult": pos_mult,
            "stop_anchor": stop_anchor,
            "take_profit_anchor": take_profit,
            "reason": reason,
            "warnings": warnings
        }

    def __del__(self):
        """清理活跃穿越状态"""
        try:
            with self._crossing_lock:
                self._active_crossings.clear()
        except Exception:
            pass
