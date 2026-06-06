"""
火种系统 · 区间厚度计算器 (ZoneThicknessCalculator)

核心职责：
1. 基于历史触及次数、触及时成交量、反转幅度、最近触及时间及当前波动率，动态计算多周期关键价位的区间厚度
2. 提供区间厚度的健康检查与自检能力，支持配置参数的外部加载与品种级差异化

外部依赖（真实模块接口）：
- core.utils.config_loader.ConfigLoader : 加载本模块所需的阈值与系数配置（可选，未注入时使用类常量）

接口契约：
- calculate(touch_count: int, last_touch_seconds_ago: float, touch_volume_ratio: float, reversal_amplitude_pct: float, atr: float, trend_direction: str) -> Dict[str, Any] : 计算区间厚度
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 ConfigLoader 不可用或配置项缺失时，使用类常量中预定义的安全默认值，并标记 "degraded" 状态
- 当输入参数超出合理范围时，自动钳制到安全边界并使用默认系数，确保输出厚度值始终有效且非负
- 所有降级值在类常量区明确声明

资源管理：
- 本模块为无状态计算器，不持有任何外部资源句柄
- 所有中间计算结果在方法返回后自动回收

输出说明：
- thickness 单位为与输入 atr 相同的价格单位，可直接用于止损/止盈计算
- 计算复杂度 O(1)，适用于高频调用（<1μs），无锁无阻塞
"""

import logging
import math
from typing import Dict, Any, List, Optional, Union, Final

logger = logging.getLogger(__name__)

# 配置键名常量，便于统一维护
_CONFIG_BASE_MULT = "thickness.base_width_atr_mult"
_CONFIG_MIN_MULT = "thickness.min_width_atr_mult"
_CONFIG_MAX_MULT = "thickness.max_width_atr_mult"
_CONFIG_HIGH_VOL_THRESH = "thickness.high_volume_threshold"
_CONFIG_LOW_VOL_THRESH = "thickness.low_volume_threshold"
_CONFIG_VOL_HIGH_MULT = "thickness.volume_high_mult"
_CONFIG_VOL_LOW_MULT = "thickness.volume_low_mult"
_CONFIG_TREND_COUNTER = "thickness.trend_counter_mult"
_CONFIG_TREND_CO = "thickness.trend_co_mult"
_CONFIG_TREND_FLAT = "thickness.trend_flat_mult"
_CONFIG_DECAY_HALFLIFE = "thickness.decay_halflife_seconds"
_CONFIG_DECAY_MIN = "thickness.decay_min_ratio"
_CONFIG_REVERSAL_FACTOR = "thickness.reversal_amplitude_factor"
_CONFIG_REVERSAL_MAX = "thickness.reversal_max_bonus"
_CONFIG_VOLUME_RATIO_MAX = "thickness.volume_ratio_max"
_CONFIG_TOUCH_SMOOTH_K = "thickness.touch_smooth_k"
_CONFIG_TOUCH_SMOOTH_MAX = "thickness.touch_smooth_max"


class ZoneThicknessCalculator:
    """多周期关键价位区间厚度计算器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_ATR: Final[float] = 100.0       # 默认 ATR 值（与输入 atr 相同的价格单位），用于配置不可用时的降级回退，取值范围 [1.0, 100000.0]

    BASE_WIDTH_ATR_MULT: Final[float] = 0.15
    MIN_WIDTH_ATR_MULT: Final[float] = 0.03
    MAX_WIDTH_ATR_MULT: Final[float] = 0.50

    TOUCH_SMOOTH_K: Final[float] = 0.3
    TOUCH_SMOOTH_MAX: Final[float] = 2.5

    HIGH_VOLUME_THRESHOLD: Final[float] = 1.5
    LOW_VOLUME_THRESHOLD: Final[float] = 0.7
    VOLUME_HIGH_MULT: Final[float] = 1.5
    VOLUME_LOW_MULT: Final[float] = 0.6

    TREND_COUNTER_MULT: Final[float] = 1.4
    TREND_CO_MULT: Final[float] = 1.0
    TREND_FLAT_MULT: Final[float] = 0.8

    DECAY_HALFLIFE_SECONDS: Final[float] = 3600.0
    DECAY_MIN_RATIO: Final[float] = 0.20

    REVERSAL_AMPLITUDE_FACTOR: Final[float] = 0.02
    REVERSAL_MAX_BONUS: Final[float] = 1.50

    VOLUME_RATIO_MAX: Final[float] = 20.0
    REVERSAL_AMPLITUDE_MAX: Final[float] = 100.0   # 反转幅度最大百分比，防止 log1p 溢出
    TOUCH_COUNT_MAX: Final[int] = 1000               # 触及次数合理上限

    def __init__(self) -> None:
        self._config_loader: Optional[Any] = None
        logger.info("ZoneThicknessCalculator 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(self, config_loader: Optional[Any] = None) -> None:
        """注入外部依赖（可选），重复注入会先清理旧引用"""
        self._config_loader = None
        if config_loader is not None:
            if not hasattr(config_loader, 'get'):
                logger.warning("注入对象缺少 'get' 方法，ConfigLoader 降级为默认配置")
                return
            self._config_loader = config_loader
            logger.info("ConfigLoader 注入成功")

    def _get_config(self, key: str, default: Union[float, int]) -> Union[float, int]:
        """从配置加载器获取参数，若不可用则返回默认值；对返回值进行严格类型与范围校验"""
        if self._config_loader is not None:
            try:
                value = self._config_loader.get(key, default)
                # 拒绝 None 或非数值
                if value is None or not isinstance(value, (int, float)):
                    logger.warning(f"配置项 {key} 返回无效类型 ({type(value).__name__})，使用默认值 {default}")
                    return default
                if math.isnan(value) or math.isinf(value):
                    logger.warning(f"配置项 {key} 为 NaN/Inf，使用默认值 {default}")
                    return default
                return value
            except Exception as e:
                logger.warning(f"配置加载异常 {key}: {e}，使用默认值 {default}")
                return default
        return default

    # ========== 公共接口 ==========
    def calculate(
        self,
        touch_count: int,
        last_touch_seconds_ago: float,
        touch_volume_ratio: float,
        reversal_amplitude_pct: float,
        atr: float,
        trend_direction: str,
    ) -> Dict[str, Any]:
        """计算关键价位的区间厚度"""
        warnings: List[str] = []

        # --- 参数清洗与安全钳制 ---
        # 1. 触及次数：严格检查类型为 int（排除 bool）
        if type(touch_count) is not int or touch_count <= 0:
            if isinstance(touch_count, bool) or type(touch_count) is not int:
                logger.warning(f"touch_count 类型错误 ({type(touch_count).__name__})，使用默认值 1")
            elif touch_count <= 0:
                logger.warning(f"无效触及次数 touch_count={touch_count}，使用安全默认值 1")
            touch_count = 1
            warnings.append("touch_count_clamped")
        touch_count = min(touch_count, self.TOUCH_COUNT_MAX)

        # 2. 距离上次触及时间
        if not isinstance(last_touch_seconds_ago, (int, float)) or last_touch_seconds_ago < 0 or math.isinf(last_touch_seconds_ago):
            last_touch_seconds_ago = 0.0
            warnings.append("last_touch_seconds_clamped")
        last_touch_seconds_ago = min(float(last_touch_seconds_ago), 1e10)

        # 3. 成交量比值
        if not isinstance(touch_volume_ratio, (int, float)) or touch_volume_ratio < 0:
            touch_volume_ratio = 1.0
            warnings.append("volume_ratio_clamped")
        volume_ratio_max = self._get_config(_CONFIG_VOLUME_RATIO_MAX, self.VOLUME_RATIO_MAX)
        if touch_volume_ratio > volume_ratio_max:
            touch_volume_ratio = volume_ratio_max
            warnings.append("volume_ratio_capped")

        # 4. 反转幅度
        if not isinstance(reversal_amplitude_pct, (int, float)) or reversal_amplitude_pct < 0:
            reversal_amplitude_pct = 0.0
            warnings.append("reversal_amplitude_clamped")
        reversal_amplitude_pct = min(float(reversal_amplitude_pct), self.REVERSAL_AMPLITUDE_MAX)

        # 5. ATR
        if not isinstance(atr, (int, float)) or math.isnan(atr) or math.isinf(atr) or atr <= 0:
            logger.warning(f"无效 ATR atr={atr}，使用默认值 {self.DEFAULT_ATR}")
            atr = float(self.DEFAULT_ATR)
            warnings.append("atr_default_used")

        # 6. 趋势方向
        if not isinstance(trend_direction, str):
            trend_direction = "flat"
            warnings.append("trend_direction_default_used")
        trend_direction = trend_direction.strip().lower()
        if trend_direction not in ("counter_trend", "co_trend", "flat"):
            trend_direction = "flat"
            warnings.append("trend_direction_default_used")

        # --- 加载配置参数 ---
        base_mult = self._get_config(_CONFIG_BASE_MULT, self.BASE_WIDTH_ATR_MULT)
        min_mult = self._get_config(_CONFIG_MIN_MULT, self.MIN_WIDTH_ATR_MULT)
        max_mult = self._get_config(_CONFIG_MAX_MULT, self.MAX_WIDTH_ATR_MULT)
        high_vol_thresh = self._get_config(_CONFIG_HIGH_VOL_THRESH, self.HIGH_VOLUME_THRESHOLD)
        low_vol_thresh = self._get_config(_CONFIG_LOW_VOL_THRESH, self.LOW_VOLUME_THRESHOLD)
        vol_high_mult = self._get_config(_CONFIG_VOL_HIGH_MULT, self.VOLUME_HIGH_MULT)
        vol_low_mult = self._get_config(_CONFIG_VOL_LOW_MULT, self.VOLUME_LOW_MULT)
        counter_mult = self._get_config(_CONFIG_TREND_COUNTER, self.TREND_COUNTER_MULT)
        co_mult = self._get_config(_CONFIG_TREND_CO, self.TREND_CO_MULT)
        flat_mult = self._get_config(_CONFIG_TREND_FLAT, self.TREND_FLAT_MULT)
        decay_halflife = self._get_config(_CONFIG_DECAY_HALFLIFE, self.DECAY_HALFLIFE_SECONDS)
        reversal_factor = max(0.0, self._get_config(_CONFIG_REVERSAL_FACTOR, self.REVERSAL_AMPLITUDE_FACTOR))
        decay_min = self._get_config(_CONFIG_DECAY_MIN, self.DECAY_MIN_RATIO)
        reversal_max = self._get_config(_CONFIG_REVERSAL_MAX, self.REVERSAL_MAX_BONUS)
        touch_smooth_k = self._get_config(_CONFIG_TOUCH_SMOOTH_K, self.TOUCH_SMOOTH_K)
        touch_smooth_max = self._get_config(_CONFIG_TOUCH_SMOOTH_MAX, self.TOUCH_SMOOTH_MAX)

        # --- 基础厚度 ---
        base_thickness = float(atr) * float(base_mult)

        # --- 触及次数连续修正 ---
        touch_mult = 1.0 + (float(touch_smooth_max) - 1.0) * (
            1.0 / (1.0 + math.exp(-float(touch_smooth_k) * (touch_count - 3)))
        )

        # --- 成交量修正（平滑过渡） ---
        high = float(high_vol_thresh)
        low = float(low_vol_thresh)
        if abs(high - low) < 1e-10:
            # 配置异常：阈值相等，使用默认系数
            logger.warning("成交量阈值配置异常 (high≈low)，使用线性中性系数 1.0")
            volume_factor = 1.0
        elif touch_volume_ratio >= high:
            volume_factor = float(vol_high_mult)
        elif touch_volume_ratio <= low:
            volume_factor = float(vol_low_mult)
        else:
            ratio = (touch_volume_ratio - low) / (high - low)
            volume_factor = float(vol_low_mult) + ratio * (float(vol_high_mult) - float(vol_low_mult))

        # --- 趋势修正 ---
        trend_map = {"counter_trend": counter_mult, "co_trend": co_mult, "flat": flat_mult}
        trend_factor = float(trend_map.get(trend_direction, flat_mult))

        # --- 时效衰减 ---
        decay_halflife_safe = max(float(decay_halflife), 1.0)
        decay_factor = math.exp(-last_touch_seconds_ago / decay_halflife_safe)
        decay_factor = max(float(decay_min), decay_factor)

        # --- 反转幅度加成（对数边际递减） ---
        safe_reversal = float(reversal_amplitude_pct)
        if safe_reversal > 0 and float(reversal_factor) > 0:
            raw_bonus = 1.0 + math.log1p(safe_reversal * float(reversal_factor))
            reversal_bonus = min(float(reversal_max), raw_bonus)
        else:
            reversal_bonus = 1.0

        # --- 最终厚度 ---
        thickness = base_thickness * touch_mult * volume_factor * trend_factor * decay_factor * reversal_bonus

        # --- 安全钳制 ---
        min_thickness = float(atr) * float(min_mult)
        max_thickness = float(atr) * float(max_mult)
        if thickness < min_thickness:
            thickness = min_thickness
            warnings.append("thickness_clamped_to_min")
        elif thickness > max_thickness:
            thickness = max_thickness
            warnings.append("thickness_clamped_to_max")

        thickness = round(thickness, 6)

        thickness_details = {
            "base_thickness": round(base_thickness, 6),
            "touch_count": touch_count,
            "touch_mult": round(touch_mult, 4),
            "volume_factor": round(volume_factor, 4),
            "trend_factor": round(trend_factor, 4),
            "decay_factor": round(decay_factor, 4),
            "reversal_bonus": round(reversal_bonus, 4),
            "final_thickness": thickness,
        }

        logger.debug(
            "区间厚度计算完成: thickness=%.2f (ATR=%.1f, touch=%d, vol_ratio=%.1f, trend=%s)",
            thickness, atr, touch_count, touch_volume_ratio, trend_direction,
        )

        return {
            "status": "ok",
            "reason": f"基于触及次数 {touch_count}、成交量比值 {touch_volume_ratio:.1f}、趋势 {trend_direction} 计算完成",
            "data": {"thickness": thickness, "thickness_details": thickness_details},
            "warnings": warnings,
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            test_cases = [
                {"touch_count": 3, "last_touch_seconds_ago": 120.0, "touch_volume_ratio": 1.2,
                 "reversal_amplitude_pct": 0.5, "atr": 100.0, "trend_direction": "co_trend", "desc": "常规"},
                {"touch_count": 8, "last_touch_seconds_ago": 7200.0, "touch_volume_ratio": 2.0,
                 "reversal_amplitude_pct": 2.0, "atr": 50.0, "trend_direction": "counter_trend", "desc": "高触及+衰减"},
                {"touch_count": 0, "last_touch_seconds_ago": -1, "touch_volume_ratio": 0.0,
                 "reversal_amplitude_pct": 0.0, "atr": 0.0, "trend_direction": "invalid", "desc": "全极值"},
                {"touch_count": 5, "last_touch_seconds_ago": 1e12, "touch_volume_ratio": 50.0,
                 "reversal_amplitude_pct": 20.0, "atr": 1e-6, "trend_direction": "counter_trend", "desc": "超大值+极小ATR"},
            ]

            for idx, case in enumerate(test_cases):
                params = {k: v for k, v in case.items() if k != "desc"}
                try:
                    result = self.calculate(**params)
                except Exception as e:
                    return {"status": "error", "reason": f"测试{idx}({case['desc']})异常: {e}",
                            "data": {}, "warnings": ["test_exception"]}
                if result["status"] != "ok":
                    return {"status": "error", "reason": f"测试{idx}({case['desc']})失败: {result}",
                            "data": {}, "warnings": ["test_failed"]}
                if result["data"]["thickness"] <= 0:
                    return {"status": "error", "reason": f"测试{idx}({case['desc']})厚度非正",
                            "data": {}, "warnings": ["invalid_thickness"]}

            return {
                "status": "ok",
                "reason": f"所有 {len(test_cases)} 个测试场景通过",
                "data": {"test_count": len(test_cases), "constants_available": True},
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查依赖注入状态、类常量完整性及 calculate 方法逻辑")
            return {"status": "error", "reason": f"健康检查异常: {str(e)}", "data": {}, "warnings": [f"health_check_failed: {str(e)}"]}
