"""
火种系统 · M12协同引擎 (MA12Synergy)

核心职责：
1. 实时计算M12均线的方向（强/弱/平/反向）、价格相对M12的距离分区（贴线/近端/远端/极端）
2. 评估M12质量（穿透频率、回归成功率、触碰弹性、连续走平K线数），输出动态协同参数（仓位系数、止损止盈调整、回踩响应）

外部依赖（真实模块接口）：
- core.perception.visual_cortex.VisualCortex : 获取M12均线值、近期K线数据、ATR、斜率等
- core.utils.config_loader.ConfigLoader : 加载M12协同配置参数（可选）
- core.behavioral_logger.BehavioralLogger : 记录协同决策日志

接口契约：
- evaluate(position_direction: int, current_price: float) -> Dict[str, Any]
  输出字典包含 "ma12_direction" (str), "distance_zone" (str), "quality_score" (float),
  "position_multiplier" (float), "stop_adjustment" (float), "take_profit_anchor" (float|None),
  "retest_action" (str), "reason" (str), "warnings" (List[str])

- health_check() -> Dict[str, Any] : 模块自检

异常与降级：
- 当 VisualCortex 不可用时，M12值使用缓存最近有效值，若缓存也为空则返回保守中性参数
- 当 ConfigLoader 不可用时，使用类常量中的默认配置
- 所有降级值在类常量区明确声明

资源管理：
- 本模块无外部资源持有，仅依赖注入的外部模块引用
- 内部缓存（最近有效M12值、最近K线）由线程锁保护，模块销毁时自动释放
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional
from collections import deque
import numpy as np

logger = logging.getLogger(__name__)


class MA12Synergy:
    """M12均线协同引擎"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 斜率阈值（单根K线的价格变化比例，无量纲）
    STRONG_UP_SLOPE = 0.0003             # 强向上斜率，取值范围 [0.0001, 0.001]
    WEAK_UP_SLOPE = 0.00005              # 弱向上斜率，取值范围 [0.00001, 0.0005]
    FLAT_RANGE = (-0.00005, 0.00005)     # 走平区间
    STRONG_DOWN_SLOPE = -0.0003          # 强向下斜率（负值）

    # 距离分区阈值（ATR倍数，无量纲）
    ON_LINE_DISTANCE_ATR = 0.3           # 贴线区，[0.2, 0.5]
    NEAR_DISTANCE_ATR = 0.8              # 近端区，[0.5, 1.2]
    FAR_DISTANCE_ATR = 1.5               # 远端区，[1.0, 2.0]
    # 超出 FAR_DISTANCE_ATR 为极端区

    # M12质量评分参数
    QUALITY_PENETRATION_WEIGHT = 0.35    # 穿透频率权重
    QUALITY_REGRESSION_WEIGHT = 0.35     # 回归成功率权重
    QUALITY_ELASTICITY_WEIGHT = 0.20     # 触碰弹性权重
    QUALITY_FLAT_DURATION_WEIGHT = 0.10  # 连续走平K线数权重

    # 默认保守参数（降级时使用）
    DEFAULT_POSITION_MULTIPLIER = 1.0    # 默认仓位系数，[0.5, 1.5]
    DEFAULT_STOP_ADJUSTMENT = 0.0        # 默认止损调整（ATR倍数），正=放宽
    DEFAULT_RETEST_ACTION = "ignore"     # 默认回踩动作

    # 仓位系数和止损调整的硬性边界
    MIN_POSITION_MULT = 0.2
    MAX_POSITION_MULT = 2.0
    MIN_STOP_ADJUST = -1.0               # 最多收紧1个ATR
    MAX_STOP_ADJUST = 1.0                # 最多放宽1个ATR

    # 回踩观察参数
    RETEST_PAUSE_SECONDS = 3             # 回踩暂停观察时间，秒，[2, 10]

    def __init__(self):
        # 缓存最近的有效M12值和ATR，用于降级
        self._last_valid_ma12: Optional[float] = None
        self._last_valid_atr: Optional[float] = None
        self._last_valid_timestamp: float = 0.0

        # M12质量评估所需的历史数据
        self._penetration_history: deque = deque(maxlen=50)   # 穿透次数记录
        self._regression_history: deque = deque(maxlen=50)    # 回归次数记录

        # 外部依赖
        self._visual_cortex = None
        self._behavioral_logger = None
        self._config = None

        # 线程安全锁
        self._lock = threading.Lock()

        logger.info("MA12Synergy 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        visual_cortex: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        config: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选注入，未注入时对应功能降级）"""
        if visual_cortex is not None:
            # 校验接口：必须实现 get_ma12, get_atr, get_ma12_slope
            for method in ["get_ma12", "get_atr", "get_ma12_slope"]:
                if not hasattr(visual_cortex, method):
                    logger.error("VisualCortex 缺少方法: %s，拒绝注入", method)
                    visual_cortex = None
                    break
            if visual_cortex is not None:
                self._visual_cortex = visual_cortex
                logger.info("VisualCortex 注入成功")
        else:
            logger.warning("VisualCortex 未注入，M12协同降级为保守模式")

        if behavioral_logger is not None and hasattr(behavioral_logger, "log_event"):
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            if behavioral_logger is not None:
                logger.warning("BehavioralLogger 缺少 log_event 方法，降级为标准logger")
            self._behavioral_logger = None

        if config is not None:
            self._load_config(config)
        else:
            logger.info("未注入配置，使用类常量默认值")

    # ========== 配置加载（热重载支持） ==========
    def _load_config(self, config: Any) -> None:
        """从配置对象加载参数，覆盖类常量默认值"""
        try:
            self.STRONG_UP_SLOPE = float(config.get("ma12.strong_up_slope", self.STRONG_UP_SLOPE))
            self.WEAK_UP_SLOPE = float(config.get("ma12.weak_up_slope", self.WEAK_UP_SLOPE))
            flat_low = float(config.get("ma12.flat_low", self.FLAT_RANGE[0]))
            flat_high = float(config.get("ma12.flat_high", self.FLAT_RANGE[1]))
            self.FLAT_RANGE = (flat_low, flat_high)
            self.STRONG_DOWN_SLOPE = float(config.get("ma12.strong_down_slope", self.STRONG_DOWN_SLOPE))
            self.ON_LINE_DISTANCE_ATR = float(config.get("ma12.on_line_atr", self.ON_LINE_DISTANCE_ATR))
            self.NEAR_DISTANCE_ATR = float(config.get("ma12.near_atr", self.NEAR_DISTANCE_ATR))
            self.FAR_DISTANCE_ATR = float(config.get("ma12.far_atr", self.FAR_DISTANCE_ATR))
            # 质量权重
            self.QUALITY_PENETRATION_WEIGHT = float(config.get("ma12.q_penetration", self.QUALITY_PENETRATION_WEIGHT))
            self.QUALITY_REGRESSION_WEIGHT = float(config.get("ma12.q_regression", self.QUALITY_REGRESSION_WEIGHT))
            self.QUALITY_ELASTICITY_WEIGHT = float(config.get("ma12.q_elasticity", self.QUALITY_ELASTICITY_WEIGHT))
            self.QUALITY_FLAT_DURATION_WEIGHT = float(config.get("ma12.q_flat_duration", self.QUALITY_FLAT_DURATION_WEIGHT))
            logger.info("M12协同配置加载成功")
        except (KeyError, ValueError, TypeError) as e:
            logger.error("配置加载失败: %s，使用类常量默认值", e)

    # ========== 公共接口 ==========
    def evaluate(self, position_direction: int, current_price: float) -> Dict[str, Any]:
        """
        根据当前持仓方向和价格，评估M12协同状态，输出调整参数。

        Args:
            position_direction: 持仓方向，1表示多头，-1表示空头
            current_price: 当前最新价格

        Returns:
            标准化响应字典
        """
        # 参数校验
        if position_direction not in (1, -1):
            logger.warning("无效持仓方向: %d，使用中性默认参数", position_direction)
            return self._neutral_response("无效持仓方向")
        if current_price <= 0:
            logger.warning("无效价格: %.2f，使用中性默认参数", current_price)
            return self._neutral_response("无效价格")

        # 获取M12值及所需指标
        ma12_value, atr_value = self._get_ma12_and_atr()
        if ma12_value is None or atr_value is None or atr_value <= 0:
            logger.warning("无法获取有效M12/ATR值，降级为保守模式")
            return self._neutral_response("M12/ATR数据不可用")

        # 1. 计算M12方向
        ma12_direction = self._calc_ma12_direction()
        # 2. 计算距离分区
        distance_zone = self._calc_distance_zone(current_price, ma12_value, atr_value)
        # 3. 评估M12质量
        quality_score = self._evaluate_quality()
        # 4. 计算仓位系数和止损调整
        position_mult, stop_adj = self._calc_position_and_stop(
            position_direction, ma12_direction, distance_zone, quality_score
        )
        # 硬边界钳制
        position_mult = max(self.MIN_POSITION_MULT, min(self.MAX_POSITION_MULT, position_mult))
        stop_adj = max(self.MIN_STOP_ADJUST, min(self.MAX_STOP_ADJUST, stop_adj))
        # 5. 计算止盈锚点
        take_profit_anchor = self._calc_take_profit_anchor(
            position_direction, ma12_direction, distance_zone, ma12_value, atr_value
        )
        # 6. 回踩动作判定
        retest_action = self._determine_retest_action(
            position_direction, ma12_direction, distance_zone, current_price, ma12_value, atr_value
        )

        reason = (
            f"M12方向={ma12_direction}, 距离={distance_zone}, 质量={quality_score:.2f}, "
            f"仓位系数={position_mult:.2f}, 止损调整={stop_adj:+.2f}ATR, "
            f"回踩动作={retest_action}"
        )
        logger.info(reason)

        # 行为日志记录
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="ma12_synergy",
                    details={
                        "direction": position_direction,
                        "price": current_price,
                        "ma12": ma12_value,
                        "atr": atr_value,
                        "ma12_direction": ma12_direction,
                        "distance_zone": distance_zone,
                        "quality": quality_score,
                        "position_mult": position_mult,
                        "stop_adj": stop_adj,
                    },
                )
            except Exception as e:
                logger.warning("行为日志记录失败: %s", e)

        return {
            "status": "ok",
            "reason": reason,
            "data": {
                "ma12_direction": ma12_direction,
                "distance_zone": distance_zone,
                "quality_score": round(quality_score, 3),
                "position_multiplier": round(position_mult, 4),
                "stop_adjustment": round(stop_adj, 4),
                "take_profit_anchor": round(take_profit_anchor, 2) if take_profit_anchor is not None else None,
                "retest_action": retest_action,
            },
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            deps_ok = {
                "visual_cortex": self._visual_cortex is not None,
                "behavioral_logger": self._behavioral_logger is not None,
            }
            return {
                "status": "ok",
                "reason": "MA12Synergy 模块正常",
                "data": {"dependencies": deps_ok, "cache_valid": self._last_valid_ma12 is not None},
                "warnings": [],
            }
        except Exception as e:
            logger.error("健康检查失败: %s #RECOVERY: 检查依赖注入和内部缓存", e)
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _neutral_response(self, reason: str) -> Dict[str, Any]:
        """生成中性默认响应（降级使用）"""
        return {
            "status": "ok",
            "reason": reason + "，使用中性默认值",
            "data": {
                "ma12_direction": "flat",
                "distance_zone": "on_line",
                "quality_score": 0.5,
                "position_multiplier": self.DEFAULT_POSITION_MULTIPLIER,
                "stop_adjustment": self.DEFAULT_STOP_ADJUSTMENT,
                "take_profit_anchor": None,
                "retest_action": self.DEFAULT_RETEST_ACTION,
            },
            "warnings": [reason],
        }

    def _get_ma12_and_atr(self) -> tuple:
        """获取M12值和ATR，带降级处理。返回 (ma12, atr) 或 (None, None)"""
        if self._visual_cortex is not None:
            try:
                ma12_val = self._visual_cortex.get_ma12()
                atr_val = self._visual_cortex.get_atr()
                if ma12_val is not None and atr_val is not None:
                    with self._lock:
                        self._last_valid_ma12 = ma12_val
                        self._last_valid_atr = atr_val
                        self._last_valid_timestamp = time.time()
                    return ma12_val, atr_val
            except Exception as e:
                logger.warning("VisualCortex 调用失败: %s，尝试使用缓存值", e)

        # 降级：使用最近缓存值（10分钟内有效）
        with self._lock:
            if (self._last_valid_ma12 is not None and self._last_valid_atr is not None
                    and time.time() - self._last_valid_timestamp < 600):
                logger.debug("使用缓存的M12/ATR值")
                return self._last_valid_ma12, self._last_valid_atr
        return None, None

    def _calc_ma12_direction(self) -> str:
        """计算M12方向。依赖 VisualCortex.get_ma12_slope()"""
        if self._visual_cortex is not None and hasattr(self._visual_cortex, "get_ma12_slope"):
            try:
                slope = self._visual_cortex.get_ma12_slope()
                if slope is not None:
                    if slope > self.STRONG_UP_SLOPE:
                        return "strong_up"
                    elif slope > self.WEAK_UP_SLOPE:
                        return "weak_up"
                    elif self.FLAT_RANGE[0] <= slope <= self.FLAT_RANGE[1]:
                        return "flat"
                    elif slope > self.STRONG_DOWN_SLOPE:
                        return "weak_down"
                    else:
                        return "strong_down"
            except Exception as e:
                logger.warning("获取M12斜率失败: %s", e)
        return "flat"

    def _calc_distance_zone(self, price: float, ma12: float, atr: float) -> str:
        """计算价格相对M12的距离分区"""
        if atr <= 0:
            return "on_line"
        distance = abs(price - ma12) / atr
        if distance <= self.ON_LINE_DISTANCE_ATR:
            return "on_line"
        elif distance <= self.NEAR_DISTANCE_ATR:
            return "near"
        elif distance <= self.FAR_DISTANCE_ATR:
            return "far"
        else:
            return "extreme"

    def _evaluate_quality(self) -> float:
        """
        评估M12均线质量（0-1，越高越可靠）。
        基于 VisualCortex 提供的穿透/回归统计，若不可用则返回0.5。
        """
        if self._visual_cortex is not None:
            try:
                # 尝试获取质量相关数据
                penetration_rate = getattr(self._visual_cortex, "get_ma12_penetration_rate", lambda: None)()
                regression_rate = getattr(self._visual_cortex, "get_ma12_regression_rate", lambda: None)()
                elasticity = getattr(self._visual_cortex, "get_ma12_elasticity", lambda: None)()
                flat_bars = getattr(self._visual_cortex, "get_ma12_flat_bars", lambda: 0)()

                # 加权计算（数据缺失时采用默认值）
                q_pen = penetration_rate if penetration_rate is not None else 0.5
                q_reg = regression_rate if regression_rate is not None else 0.5
                q_ela = elasticity if elasticity is not None else 0.5
                # 连续走平K线数归一化到0-1（假设20根为满分）
                flat_score = min(1.0, (flat_bars or 0) / 20.0)

                quality = (self.QUALITY_PENETRATION_WEIGHT * q_pen +
                           self.QUALITY_REGRESSION_WEIGHT * q_reg +
                           self.QUALITY_ELASTICITY_WEIGHT * q_ela +
                           self.QUALITY_FLAT_DURATION_WEIGHT * flat_score)
                return max(0.0, min(1.0, quality))
            except Exception as e:
                logger.warning("质量评估计算失败: %s", e)
        return 0.5

    def _calc_position_and_stop(
        self,
        direction: int,
        ma12_dir: str,
        zone: str,
        quality: float,
    ) -> tuple:
        """计算仓位系数和止损调整（ATR倍数）。返回 (mult, stop_adj)"""
        mult = 1.0
        stop_adj = 0.0

        # 顺势/逆势判断
        is_aligned = (direction == 1 and "up" in ma12_dir) or (direction == -1 and "down" in ma12_dir)
        is_counter = (direction == 1 and "down" in ma12_dir) or (direction == -1 and "up" in ma12_dir)
        is_strong = "strong" in ma12_dir

        if is_aligned and is_strong:
            mult += 0.2
            stop_adj += 0.2
        elif is_aligned and not is_strong:
            mult += 0.1
        elif is_counter and is_strong:
            mult -= 0.5
            stop_adj -= 0.3
        elif is_counter and not is_strong:
            mult -= 0.2
            stop_adj -= 0.1

        if zone == "extreme":
            mult -= 0.2
            stop_adj -= 0.1
        elif zone == "far":
            mult -= 0.1

        # 质量因子调节：高质量时扩大调整幅度，低质量时向1.0回归
        quality_factor = 0.5 + 0.5 * quality
        mult = 1.0 + (mult - 1.0) * quality_factor
        stop_adj *= quality_factor

        return mult, stop_adj

    def _calc_take_profit_anchor(
        self,
        direction: int,
        ma12_dir: str,
        zone: str,
        ma12: float,
        atr: float,
    ) -> Optional[float]:
        """计算止盈锚点。价格远离均线时锚定在M12附近，否则返回None"""
        if zone in ("far", "extreme"):
            # 预期回归到M12±0.5ATR
            return ma12 + direction * atr * 0.5
        return None

    def _determine_retest_action(
        self,
        direction: int,
        ma12_dir: str,
        zone: str,
        price: float,
        ma12: float,
        atr: float,
    ) -> str:
        """
        判断回踩动作。当价格处于贴线区时，根据近期K线形态决定：
        - "bounce": 反弹，恢复宽松
        - "entangle": 纠缠，加速收紧
        - "breakdown": 跌破，直接保本
        需要 VisualCortex 提供最近K线行为数据，否则返回 "pause_observe"
        """
        if zone != "on_line":
            return "ignore"

        if self._visual_cortex is not None and hasattr(self._visual_cortex, "get_recent_ma12_retest_behavior"):
            try:
                behavior = self._visual_cortex.get_recent_ma12_retest_behavior(direction)
                if behavior in ("bounce", "entangle", "breakdown"):
                    return behavior
            except Exception as e:
                logger.warning("获取回踩行为失败: %s", e)

        return "pause_observe"
