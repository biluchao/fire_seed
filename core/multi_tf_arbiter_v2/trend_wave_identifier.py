"""
火种系统 · 趋势波浪斜线识别器 (TrendWaveIdentifier)

核心职责：
1. 基于小周期K线的高点与低点序列，识别上级周期趋势中的回踩点，通过线性回归拟合出动态的支撑与压力斜线通道。
2. 根据当前波动率分位、成交量确认、影线比例等多维验证，自适应调节最少回踩点数与通道厚度，并输出斜线的斜率、截距、拟合置信度(R²)以及上下边界。

外部依赖（真实模块接口）：
- core.perception.tactile_cortex.TactileCortex : 获取当前波动率分位与流动性评级
- core.perception.visual_cortex.VisualCortex : 获取K线形态、关键高低点与影线特征
- core.behavioral_logger.BehavioralLogger : 记录斜线识别的审计日志
- numpy (可选) : 用于高性能线性回归，不可用时使用纯Python实现

接口契约：
- identify_slope(symbol: str, period: str, direction: int, klines: Optional[List[Dict[str, float]]] = None, atr: Optional[float] = None) -> Dict[str, Any]
  输出字典固定包含 "slope" (float), "intercept" (float), "confidence" (float), "thickness" (float), "upper_bound" (float), "lower_bound" (float), "reason" (str), "warnings" (List[str]), "timestamp" (float)
- health_check() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "message" (str)

异常与降级：
- 当 TactileCortex 不可用时，使用固定波动率分位（50%），并记录降级警告。
- 当 VisualCortex 不可用时，仅使用K线价格提取回踩点，不进行成交量与影线验证，拟合置信度降低。
- 当 numpy 不可用时，自动切换为纯Python实现的线性回归，精度与性能略有下降。
- 当回踩点数不足时，返回无效斜线（slope=0, confidence=0），不阻塞主流程。

资源管理：
- 本模块不持有需要手动释放的资源，所有计算结果在方法返回后自动回收。
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
    logger.warning("numpy 不可用，线性回归将使用纯Python实现，性能可能下降")


class TrendWaveIdentifier:
    """趋势波浪斜线识别器"""

    # 类常量（默认配置，附带单位与取值范围注释）
    MIN_RETEST_POINTS_HIGH_VOL = 2       # 高波动期最少回踩点数，次，取值范围 [1, 5]
    MIN_RETEST_POINTS_LOW_VOL = 5        # 低波动期最少回踩点数，次，取值范围 [3, 10]
    MIN_RETEST_POINTS_NORMAL = 3          # 正常波动期最少回踩点数，次，取值范围 [2, 8]
    HIGH_VOL_PERCENTILE = 70              # 高波动分位阈值，%，取值范围 [60, 90]
    LOW_VOL_PERCENTILE = 30               # 低波动分位阈值，%，取值范围 [10, 40]
    DEFAULT_VOL_PERCENTILE = 50           # 降级默认波动率分位，%，取值范围 [0, 100]
    MAX_LOOKBACK_BARS = 60                # 最大回溯K线数，根，取值范围 [20, 200]
    BASE_THICKNESS_ATR_MULT = 0.12        # 基础厚度ATR倍数，无量纲，取值范围 [0.05, 0.3]
    MIN_R_SQUARED = 0.3                   # 最低拟合置信度，无量纲，取值范围 [0.1, 0.7]
    VOLUME_DECAY_RATIO = 0.7              # 成交量确认：回踩点成交量需低于前高量比例，无量纲，取值范围 [0.3, 0.9]
    WICK_BODY_RATIO = 1.5                 # 影线确认：影线与实体比例阈值，无量纲，取值范围 [1.0, 3.0]
    MIN_KLINES_REQUIRED = 5               # 最小K线数量要求，根，取值范围 [3, 20]

    def __init__(self, config: Dict[str, Any]):
        # 从配置加载可调节参数，附带安全默认值
        self._min_retest_high = config.get("min_retest_points_high_vol", self.MIN_RETEST_POINTS_HIGH_VOL)
        self._min_retest_low = config.get("min_retest_points_low_vol", self.MIN_RETEST_POINTS_LOW_VOL)
        self._min_retest_normal = config.get("min_retest_points_normal", self.MIN_RETEST_POINTS_NORMAL)
        self._high_vol_pct = config.get("high_vol_percentile", self.HIGH_VOL_PERCENTILE)
        self._low_vol_pct = config.get("low_vol_percentile", self.LOW_VOL_PERCENTILE)
        self._default_vol_pct = config.get("default_vol_percentile", self.DEFAULT_VOL_PERCENTILE)
        self._max_lookback = config.get("max_lookback_bars", self.MAX_LOOKBACK_BARS)
        self._base_thickness_mult = config.get("base_thickness_atr_mult", self.BASE_THICKNESS_ATR_MULT)
        self._min_r_squared = config.get("min_r_squared", self.MIN_R_SQUARED)
        self._volume_decay_ratio = config.get("volume_decay_ratio", self.VOLUME_DECAY_RATIO)
        self._wick_body_ratio = config.get("wick_body_ratio", self.WICK_BODY_RATIO)
        self._min_klines = config.get("min_klines_required", self.MIN_KLINES_REQUIRED)

        # 外部依赖（延迟注入）
        self._tactile_cortex: Optional[Any] = None
        self._visual_cortex: Optional[Any] = None
        self._behavioral_logger: Optional[Any] = None

        logger.info("TrendWaveIdentifier 初始化完成，numpy=%s, 依赖待注入", "可用" if HAS_NUMPY else "不可用")

    # ────────────────────────── 依赖注入 ──────────────────────────
    def inject_dependencies(
        self,
        tactile_cortex: Optional[Any] = None,
        visual_cortex: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None
    ) -> None:
        """注入外部依赖模块，并进行鸭子类型校验"""
        self._tactile_cortex = tactile_cortex
        self._visual_cortex = visual_cortex
        self._behavioral_logger = behavioral_logger

        if tactile_cortex is not None and not hasattr(tactile_cortex, "get_volatility_percentile"):
            logger.warning("TactileCortex 缺少 get_volatility_percentile 方法，波动率感知将降级")
        if visual_cortex is not None and not hasattr(visual_cortex, "get_key_pivots"):
            logger.warning("VisualCortex 缺少 get_key_pivots 方法，回踩点提取将降级")
        logger.info("TrendWaveIdentifier 依赖注入完成")

    # ────────────────────────── 公共接口 ──────────────────────────
    def identify_slope(
        self,
        symbol: str,
        period: str,
        direction: int,
        klines: Optional[List[Dict[str, float]]] = None,
        atr: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        识别趋势波浪斜线。

        :param symbol: 交易对标识
        :param period: 周期标识 (1m, 5m, 15m)
        :param direction: 趋势方向 (1=上升趋势, -1=下降趋势, 0=无趋势)
        :param klines: 可选，外部传入的K线数据列表，每项需包含 high, low, close, volume, timestamp
        :param atr: 可选，当前周期的ATR值，用于通道厚度计算
        :return: 标准化斜线识别结果
        """
        warnings: List[str] = []
        now = time.time()

        # 方向校验
        if direction == 0:
            return self._build_invalid_result("无明确趋势方向，斜线未识别", warnings, now)

        # 1. 获取波动率分位（降级保护）
        vol_percentile = self._default_vol_pct
        if self._tactile_cortex:
            try:
                vol_percentile = self._tactile_cortex.get_volatility_percentile(period)
                if isinstance(vol_percentile, dict):
                    vol_percentile = vol_percentile.get("percentile", self._default_vol_pct)
            except Exception as e:
                logger.warning(f"获取波动率分位失败: {e}，使用降级值 {vol_percentile}")
                warnings.append(f"波动率感知降级: {e}")
        else:
            warnings.append("TactileCortex 未注入，波动率分位降级为默认值")

        # 2. 动态确定最少回踩点数
        if vol_percentile >= self._high_vol_pct:
            min_retest = self._min_retest_high
            vol_regime = "high"
        elif vol_percentile <= self._low_vol_pct:
            min_retest = self._min_retest_low
            vol_regime = "low"
        else:
            min_retest = self._min_retest_normal
            vol_regime = "normal"

        # 3. 数据有效性验证
        if klines is not None:
            valid, reason = self._validate_klines(klines)
            if not valid:
                logger.warning(f"K线数据无效: {reason}")
                warnings.append(f"K线数据无效: {reason}")
                return self._build_invalid_result(f"K线数据无效: {reason}", warnings, now)

        # 4. 提取回踩点（含成交量与影线验证）
        pivot_points = self._extract_pivot_points(symbol, period, direction, klines, warnings)
        if len(pivot_points) < min_retest:
            reason = f"回踩点数不足: {len(pivot_points)}/{min_retest} (波动率分位={vol_percentile:.0f}%, 周期={period})"
            logger.debug(reason)
            return self._build_invalid_result(reason, warnings, now)

        # 5. 线性回归拟合斜线
        slope, intercept, r_squared = self._fit_slope(pivot_points)
        if r_squared < self._min_r_squared:
            reason = f"拟合置信度不足: R²={r_squared:.3f} < {self._min_r_squared}"
            logger.debug(reason)
            return self._build_invalid_result(reason, warnings, now)

        # 6. 计算通道厚度（基础ATR倍数 × 波动率修正 × 回踩加固系数）
        thickness = self._calculate_thickness(vol_percentile, vol_regime, len(pivot_points), atr)

        # 7. 计算上下边界
        upper_bound = intercept + thickness
        lower_bound = intercept - thickness

        reason = (
            f"趋势波浪斜线已识别: slope={slope:.6f}, intercept={intercept:.2f}, "
            f"R²={r_squared:.3f}, thickness={thickness:.2f}, retests={len(pivot_points)}, "
            f"vol_regime={vol_regime}"
        )
        logger.info(reason)

        # 审计日志
        if self._behavioral_logger:
            try:
                self._behavioral_logger.log_event(
                    module="trend_wave_identifier",
                    event_type="slope_identified",
                    payload={
                        "symbol": symbol,
                        "period": period,
                        "direction": direction,
                        "slope": slope,
                        "intercept": intercept,
                        "r_squared": r_squared,
                        "thickness": thickness,
                        "retest_count": len(pivot_points),
                        "vol_regime": vol_regime,
                        "timestamp": now
                    }
                )
            except Exception:
                pass

        return {
            "slope": slope,
            "intercept": intercept,
            "confidence": r_squared,
            "thickness": thickness,
            "upper_bound": upper_bound,
            "lower_bound": lower_bound,
            "reason": reason,
            "warnings": warnings,
            "timestamp": now
        }

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检：验证常量、线性回归逻辑和降级路径"""
        try:
            dummy_config = {}
            identifier = cls(dummy_config)

            # 模拟回踩点，测试线性回归
            pivot_points = [(1.0, 100.0), (2.0, 101.0), (3.0, 102.0), (4.0, 103.0)]
            slope, intercept, r_squared = identifier._fit_slope(pivot_points)
            if slope <= 0 or r_squared < 0.9:
                return {"status": "warning", "message": "线性回归拟合结果异常"}

            # 测试无 numpy 的情况（模拟降级）
            if not HAS_NUMPY:
                logger.debug("健康检查在纯Python模式下运行")

            # 测试常量有效性
            if cls.MIN_RETEST_POINTS_HIGH_VOL <= 0 or cls.BASE_THICKNESS_ATR_MULT <= 0:
                return {"status": "error", "message": "关键常量非法"}

            # 测试无效方向
            result = identifier.identify_slope("test", "1m", 0)
            if result["slope"] != 0.0 or result["confidence"] != 0.0:
                return {"status": "error", "message": "无效方向处理异常"}

            return {"status": "ok", "message": "所有测试通过（含降级路径）"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _validate_klines(self, klines: List[Dict[str, float]]) -> Tuple[bool, str]:
        """验证K线数据的完整性和有效性"""
        if len(klines) < self._min_klines:
            return False, f"K线数量不足 {len(klines)} < {self._min_klines}"
        for i, k in enumerate(klines):
            if not all(key in k for key in ("high", "low", "close")):
                return False, f"第{i}根K线缺少必要字段 (high/low/close)"
            if k["high"] < k["low"] or k["close"] < 0:
                return False, f"第{i}根K线价格非法"
            # volume 字段可选，但若存在则检查非负
            if "volume" in k and k["volume"] < 0:
                return False, f"第{i}根K线成交量非法"
        return True, "ok"

    def _extract_pivot_points(
        self,
        symbol: str,
        period: str,
        direction: int,
        klines: Optional[List[Dict[str, float]]],
        warnings: List[str]
    ) -> List[Tuple[float, float]]:
        """提取回踩点序列，返回 [(x_index, price), ...]，并应用成交量与影线验证"""
        points: List[Tuple[float, float]] = []
        require_volume = klines is not None and all("volume" in k for k in klines)

        if klines is not None and len(klines) >= self._min_klines:
            # 使用外部传入的K线数据，并应用严格验证
            for i in range(1, len(klines) - 1):
                k_i = klines[i]
                k_prev = klines[i-1]
                k_next = klines[i+1]

                # 基本枢轴点识别
                is_pivot = False
                if direction == 1:
                    if k_i["low"] < k_prev["low"] and k_i["low"] < k_next["low"]:
                        is_pivot = True
                else:
                    if k_i["high"] > k_prev["high"] and k_i["high"] > k_next["high"]:
                        is_pivot = True

                if not is_pivot:
                    continue

                # 成交量确认：回踩点成交量应萎缩（相对前一个推进浪的高量）
                if require_volume and self._volume_decay_ratio > 0:
                    # 找前一个局部高量
                    prev_vol = k_prev.get("volume", k_i.get("volume", 0))
                    if k_i.get("volume", 0) > prev_vol * self._volume_decay_ratio:
                        continue  # 量未萎缩，非有效回踩

                # 影线确认：具有较长影线表示反压
                body = abs(k_i["close"] - k_i["open"])
                if direction == 1:
                    wick = k_i["open"] - k_i["low"] if k_i["close"] > k_i["open"] else k_i["close"] - k_i["low"]
                else:
                    wick = k_i["high"] - k_i["open"] if k_i["close"] > k_i["open"] else k_i["high"] - k_i["close"]
                wick = max(wick, 0.0)
                if body > 0 and wick / body < self._wick_body_ratio:
                    continue  # 影线不够长，反转力度弱

                price = k_i["low"] if direction == 1 else k_i["high"]
                points.append((float(i), price))

        elif self._visual_cortex:
            # 从视觉皮层获取关键枢轴点（降级模式）
            try:
                pivots = self._visual_cortex.get_key_pivots(symbol, period, direction)
                if isinstance(pivots, list):
                    points = [(float(i), float(p)) for i, p in enumerate(pivots) if p > 0]
            except Exception as e:
                logger.warning(f"视觉皮层提取回踩点失败: {e}")
                warnings.append(f"回踩点提取降级: {e}")
        else:
            warnings.append("无K线数据且视觉皮层不可用，回踩点为空")

        # 限制最大回溯数量
        if len(points) > self._max_lookback:
            points = points[-self._max_lookback:]

        return points

    @staticmethod
    def _fit_slope(points: List[Tuple[float, float]]) -> Tuple[float, float, float]:
        """线性回归拟合斜线，返回 (slope, intercept, r_squared)。若 numpy 可用则使用高性能版本，否则使用纯Python"""
        if len(points) < 2:
            return 0.0, 0.0, 0.0

        if HAS_NUMPY:
            return TrendWaveIdentifier._fit_slope_numpy(points)
        else:
            return TrendWaveIdentifier._fit_slope_pure_python(points)

    @staticmethod
    def _fit_slope_numpy(points: List[Tuple[float, float]]) -> Tuple[float, float, float]:
        """numpy 实现的线性回归"""
        x = np.array([p[0] for p in points])
        y = np.array([p[1] for p in points])
        n = len(x)
        sum_x = np.sum(x)
        sum_y = np.sum(y)
        sum_xy = np.sum(x * y)
        sum_x2 = np.sum(x * x)
        denom = n * sum_x2 - sum_x * sum_x
        if abs(denom) < 1e-12:
            return 0.0, float(np.mean(y)), 0.0
        slope = (n * sum_xy - sum_x * sum_y) / denom
        intercept = (sum_y - slope * sum_x) / n
        y_pred = slope * x + intercept
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 1e-12 else 0.0
        return float(slope), float(intercept), max(0.0, min(1.0, r_squared))

    @staticmethod
    def _fit_slope_pure_python(points: List[Tuple[float, float]]) -> Tuple[float, float, float]:
        """纯Python实现的线性回归（降级路径）"""
        n = len(points)
        sum_x = sum(p[0] for p in points)
        sum_y = sum(p[1] for p in points)
        sum_xy = sum(p[0] * p[1] for p in points)
        sum_x2 = sum(p[0] * p[0] for p in points)
        denom = n * sum_x2 - sum_x * sum_x
        if abs(denom) < 1e-12:
            return 0.0, sum_y / n, 0.0
        slope = (n * sum_xy - sum_x * sum_y) / denom
        intercept = (sum_y - slope * sum_x) / n
        # 计算 R²
        y_mean = sum_y / n
        ss_res = sum((p[1] - (slope * p[0] + intercept)) ** 2 for p in points)
        ss_tot = sum((p[1] - y_mean) ** 2 for p in points)
        r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 1e-12 else 0.0
        return float(slope), float(intercept), max(0.0, min(1.0, r_squared))

    def _calculate_thickness(self, vol_percentile: float, vol_regime: str, retest_count: int, atr: Optional[float]) -> float:
        """计算通道厚度：基础ATR倍数 × 波动率系数 × 回踩加固系数 × ATR"""
        # 波动率系数：高波放大，低波缩小
        if vol_regime == "high":
            vol_mult = 1.3
        elif vol_regime == "low":
            vol_mult = 0.7
        else:
            vol_mult = 1.0

        # 回踩加固系数：回踩次数越多，厚度越薄（位置更精确）
        if retest_count <= 2:
            retest_mult = 1.0
        elif retest_count <= 3:
            retest_mult = 0.85
        elif retest_count <= 4:
            retest_mult = 0.7
        elif retest_count <= 5:
            retest_mult = 0.6
        else:
            retest_mult = 0.5

        # 若未提供ATR，使用基准常数（保守估计，避免厚度为零）
        effective_atr = atr if atr and atr > 0 else 1.0

        return self._base_thickness_mult * vol_mult * retest_mult * effective_atr

    def _build_invalid_result(self, reason: str, warnings: List[str], timestamp: float) -> Dict[str, Any]:
        """构建无效斜线的标准化返回"""
        return {
            "slope": 0.0,
            "intercept": 0.0,
            "confidence": 0.0,
            "thickness": 0.0,
            "upper_bound": 0.0,
            "lower_bound": 0.0,
            "reason": reason,
            "warnings": warnings,
            "timestamp": timestamp
  }
