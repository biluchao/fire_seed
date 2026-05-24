"""
火种系统 · 区间厚度计算器 (ZoneThicknessCalculator)

核心职责：
1. 基于历史触及次数、触及时的成交量、趋势方向以及当前波动率，动态计算上级周期关键区间的厚度。
2. 输出厚度值及厚度修正系数，用于后续网格布设、仓位调整和穿越监控。

外部依赖（真实模块接口）：
- core.perception.tactile_cortex.TactileCortex : 获取当前波动率分位，用于波动率修正系数计算
- core.behavioral_logger.BehavioralLogger : 记录厚度计算的审计日志
- numpy (可选) : 用于高性能数学计算，不可用时使用纯Python实现

接口契约：
- calculate_thickness(zone: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]
  输出字典固定包含 "thickness" (float), "base_thickness" (float), "volatility_multiplier" (float), "retest_multiplier" (float), "reason" (str), "warnings" (List[str])
- health_check() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "message" (str)

异常与降级：
- 当 TactileCortex 不可用时，使用固定波动率分位（50%），并记录降级警告。
- 当 zone 缺少必要字段时，返回保守的默认厚度，确保系统不会因数据缺失而崩溃。
- 当触及次数为零时，采用默认基础厚度，不依赖历史数据。

资源管理：
- 本模块无状态，不持有需要手动释放的资源，所有结果在方法返回后自动回收。
"""

import time
import logging
import math
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger(__name__)

# 尝试导入 numpy，若不可用则标记降级
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    np = None  # type: ignore
    HAS_NUMPY = False
    logger.warning("numpy 不可用，部分计算将使用纯Python实现，性能可能下降")


class ZoneThicknessCalculator:
    """区间厚度计算器（无状态）"""

    # 类常量（默认配置，附带单位与取值范围注释）
    BASE_WIDTH_ATR_MULT = 0.15          # 基础宽度 ATR 倍数，无量纲，取值范围 [0.05, 0.30]
    MIN_WIDTH_PCT = 0.05                # 最小保护宽度比例（相对ATR），无量纲，取值范围 [0.02, 0.10]
    DEFAULT_ATR = 100.0                 # 降级默认 ATR 值（用于无ATR输入时），无量纲
    # 触及次数加固系数映射 (触及次数区间 -> 系数)
    TOUCH_COUNT_MULTIPLIER = {
        (1, 2): 1.0,
        (3, 5): 1.5,
        (6, 100): 2.0
    }
    # 成交量系数
    HIGH_VOL_TOUCH_MULT = 1.5           # 高成交量触及系数，无量纲，取值范围 [1.0, 2.0]
    NORMAL_VOL_TOUCH_MULT = 1.0         # 正常成交量触及系数，无量纲
    LOW_VOL_TOUCH_MULT = 0.6            # 低成交量触及系数，无量纲，取值范围 [0.4, 1.0]
    # 趋势方向系数
    COUNTER_TREND_MULT = 1.4            # 逆势触及系数（更坚固），无量纲，取值范围 [1.0, 2.0]
    CO_TREND_MULT = 1.0                 # 顺势触及系数，无量纲
    FLAT_TREND_MULT = 0.8               # 走平趋势触及系数，无量纲，取值范围 [0.5, 1.0]
    # 波动率修正系数
    HIGH_VOL_THICKNESS_MULT = 1.3       # 高波动时厚度放大，无量纲，取值范围 [1.0, 1.5]
    LOW_VOL_THICKNESS_MULT = 0.7        # 低波动时厚度缩小，无量纲，取值范围 [0.5, 1.0]
    DEFAULT_VOL_PERCENTILE = 50         # 降级默认波动率分位，%

    def __init__(self, config: Dict[str, Any]):
        # 从配置加载可调节参数，附带安全默认值
        self._base_width_mult = config.get("base_width_atr_mult", self.BASE_WIDTH_ATR_MULT)
        self._min_width_pct = config.get("min_width_pct", self.MIN_WIDTH_PCT)
        self._default_atr = config.get("default_atr", self.DEFAULT_ATR)
        self._touch_mult_map = config.get("touch_count_multiplier", self.TOUCH_COUNT_MULTIPLIER)
        self._high_vol_touch_mult = config.get("high_vol_touch_mult", self.HIGH_VOL_TOUCH_MULT)
        self._normal_vol_touch_mult = config.get("normal_vol_touch_mult", self.NORMAL_VOL_TOUCH_MULT)
        self._low_vol_touch_mult = config.get("low_vol_touch_mult", self.LOW_VOL_TOUCH_MULT)
        self._counter_trend_mult = config.get("counter_trend_mult", self.COUNTER_TREND_MULT)
        self._co_trend_mult = config.get("co_trend_mult", self.CO_TREND_MULT)
        self._flat_trend_mult = config.get("flat_trend_mult", self.FLAT_TREND_MULT)
        self._high_vol_thick_mult = config.get("high_vol_thickness_mult", self.HIGH_VOL_THICKNESS_MULT)
        self._low_vol_thick_mult = config.get("low_vol_thickness_mult", self.LOW_VOL_THICKNESS_MULT)
        self._default_vol_pct = config.get("default_vol_percentile", self.DEFAULT_VOL_PERCENTILE)

        # 外部依赖（延迟注入）
        self._tactile_cortex: Optional[Any] = None
        self._behavioral_logger: Optional[Any] = None

        logger.info("ZoneThicknessCalculator 初始化完成，numpy=%s, 依赖待注入", "可用" if HAS_NUMPY else "不可用")

    # ────────────────────────── 依赖注入 ──────────────────────────
    def inject_dependencies(
        self,
        tactile_cortex: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None
    ) -> None:
        """注入外部依赖模块，并进行鸭子类型校验"""
        self._tactile_cortex = tactile_cortex
        self._behavioral_logger = behavioral_logger

        if tactile_cortex is not None and not hasattr(tactile_cortex, "get_volatility_percentile"):
            logger.warning("TactileCortex 缺少 get_volatility_percentile 方法，波动率感知将降级")
        logger.info("ZoneThicknessCalculator 依赖注入完成")

    # ────────────────────────── 公共接口 ──────────────────────────
    def calculate_thickness(
        self,
        zone: Dict[str, Any],
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        计算区间动态厚度。

        :param zone: 区间信息，必须包含 'touch_count', 'last_touch_volume_ratio', 'trend_direction', 可选 'atr'
        :param context: 附加信息，如当前波动率分位、趋势强度等
        :return: 标准化厚度结果字典
        """
        warnings: List[str] = []
        now = time.time()

        # 参数校验
        touch_count = zone.get("touch_count", 0)
        vol_ratio = zone.get("last_touch_volume_ratio", 1.0)
        trend_direction = zone.get("trend_direction", "flat")  # "counter", "co", "flat"
        atr = zone.get("atr", context.get("atr", self._default_atr))
        if atr <= 0:
            atr = self._default_atr
            warnings.append("ATR无效，使用默认ATR")

        # 1. 获取波动率分位（降级保护）
        vol_percentile = self._default_vol_pct
        if self._tactile_cortex:
            try:
                vol_percentile = self._tactile_cortex.get_volatility_percentile("1m")
                if isinstance(vol_percentile, dict):
                    vol_percentile = vol_percentile.get("percentile", self._default_vol_pct)
            except Exception as e:
                logger.warning(f"获取波动率分位失败: {e}，使用降级值 {vol_percentile}")
                warnings.append(f"波动率感知降级: {e}")
        else:
            warnings.append("TactileCortex 未注入，波动率分位降级为默认值")

        # 2. 计算基础厚度
        base_thickness = self._base_width_mult * atr
        base_thickness = max(base_thickness, atr * self._min_width_pct)

        # 3. 计算触及次数系数
        touch_mult = self._get_touch_multiplier(touch_count)

        # 4. 计算成交量系数
        vol_mult = self._get_volume_multiplier(vol_ratio)

        # 5. 计算趋势方向系数
        trend_mult = self._get_trend_multiplier(trend_direction)

        # 6. 计算波动率修正系数
        if vol_percentile >= 70:
            vol_thick_mult = self._high_vol_thick_mult
            vol_regime = "high"
        elif vol_percentile <= 30:
            vol_thick_mult = self._low_vol_thick_mult
            vol_regime = "low"
        else:
            vol_thick_mult = 1.0
            vol_regime = "normal"

        # 7. 最终厚度
        thickness = base_thickness * touch_mult * vol_mult * trend_mult * vol_thick_mult

        reason = (
            f"区间厚度计算: base={base_thickness:.2f}, touch_mult={touch_mult:.2f}, "
            f"vol_mult={vol_mult:.2f}, trend_mult={trend_mult:.2f}, vol_thick_mult={vol_thick_mult:.2f}, "
            f"final={thickness:.2f} (touch_count={touch_count}, vol_regime={vol_regime})"
        )
        logger.debug(reason)

        # 审计日志
        if self._behavioral_logger:
            try:
                self._behavioral_logger.log_event(
                    module="zone_thickness_calculator",
                    event_type="thickness_calculated",
                    payload={
                        "touch_count": touch_count,
                        "vol_ratio": vol_ratio,
                        "trend_direction": trend_direction,
                        "thickness": thickness,
                        "timestamp": now
                    }
                )
            except Exception:
                pass

        return {
            "thickness": thickness,
            "base_thickness": base_thickness,
            "volatility_multiplier": vol_thick_mult,
            "retest_multiplier": touch_mult,
            "reason": reason,
            "warnings": warnings
        }

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检：验证常量有效性和核心逻辑"""
        try:
            dummy_config = {}
            calculator = cls(dummy_config)

            # 测试基本计算
            test_zone = {
                "touch_count": 3,
                "last_touch_volume_ratio": 1.2,
                "trend_direction": "counter",
                "atr": 150.0
            }
            result = calculator.calculate_thickness(test_zone, {})
            if result["thickness"] <= 0:
                return {"status": "error", "message": "厚度计算异常"}

            # 测试常量有效性
            if cls.BASE_WIDTH_ATR_MULT <= 0 or cls.MIN_WIDTH_PCT <= 0:
                return {"status": "error", "message": "关键常量非法"}

            # 测试缺失字段的降级
            bad_zone = {}
            result2 = calculator.calculate_thickness(bad_zone, {})
            if result2["thickness"] <= 0:
                return {"status": "error", "message": "缺失字段降级失败"}

            return {"status": "ok", "message": "所有测试通过（含降级路径）"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _get_touch_multiplier(self, touch_count: int) -> float:
        """根据触及次数获取加固系数"""
        for (low, high), mult in self._touch_mult_map.items():
            if low <= touch_count <= high:
                return mult
        # 超出所有区间则取最后一个区间的系数
        return list(self._touch_mult_map.values())[-1] if self._touch_mult_map else 1.0

    def _get_volume_multiplier(self, vol_ratio: float) -> float:
        """根据触及时的成交量比率获取系数"""
        if vol_ratio >= 1.5:
            return self._high_vol_touch_mult
        elif vol_ratio <= 0.7:
            return self._low_vol_touch_mult
        else:
            return self._normal_vol_touch_mult

    def _get_trend_multiplier(self, trend_direction: str) -> float:
        """根据趋势方向获取系数"""
        direction = trend_direction.lower()
        if direction in ("counter", "counter_trend"):
            return self._counter_trend_mult
        elif direction in ("co", "co_trend"):
            return self._co_trend_mult
        elif direction in ("flat",):
            return self._flat_trend_mult
        else:
            logger.warning(f"未知趋势方向: {trend_direction}，使用默认系数1.0")
            return 1.0
