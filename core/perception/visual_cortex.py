"""
火种系统 · 视觉皮层 (VisualCortex) — 深度精细化

核心职责：
1. 分析K线形态：支持识别 Pin Bar、吞没、十字星、锤子线、流星线、启明星、黄昏星等常见形态，输出标准化标签与置信度。
2. 评估订单簿结构与挂单墙韧性：计算前N档挂单量的分布斜率、集中度，并基于监控窗口内的撤单率和回补率判定墙的真假。

外部依赖（真实模块接口）：
- 无外部模块依赖。所有必要数据（K线序列、订单簿快照）均由调用方通过方法参数传入。

接口契约：
- perceive(kline_sequence: Optional[List[Dict[str, float]]], orderbook_snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]
  输出字典固定包含 "status" (str), "candlestick_pattern" (str), "pattern_confidence" (float),
  "ma12_position" (str), "orderbook_slope" (float), "orderbook_concentration" (float),
  "wall_resilience" (str), "reason" (str), "warnings" (List[str])
- health_check() -> Dict[str, Any]

异常与降级：
- 当输入K线序列长度不足或为空时，返回 "none" 形态、置信度0.0和降级标记。
- 当订单簿快照为空或格式错误时，斜率与集中度返回0.0，韧性返回 "unknown"。
- 所有计算异常被内部捕获，不向外抛出，确保调用方安全。

资源管理：
- 本模块为有状态缓存（形态识别结果缓存），持有最近K线组合的哈希映射，在价格更新时自动失效。
- 无外部连接或文件句柄，无需显式释放。
"""

import logging
import hashlib
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class VisualCortex:
    """视觉皮层（深度精细化）：K线形态识别与订单簿形状分析"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # -- K线形态阈值 --
    PIN_BAR_WICK_BODY_RATIO = 2.0          # 影线/实体最小倍数，[1.5, 5.0]
    PIN_BAR_BODY_RANGE_RATIO = 0.3         # 实体占波动范围最大比例，[0.1, 0.4]
    ENGULFING_BODY_RATIO = 1.2              # 吞没实体/被吞没实体最小倍数，[1.0, 2.0]
    DOJI_BODY_RANGE_RATIO = 0.1            # 十字星实体占波动范围最大比例，[0.05, 0.2]
    HAMMER_LOWER_WICK_RATIO = 2.0          # 锤子线/流星线下影线上影线最小倍数，[1.5, 5.0]
    HAMMER_BODY_RANGE_RATIO = 0.3          # 实体占波动范围最大比例，[0.1, 0.4]
    MORNING_STAR_CONSECUTIVE = 3           # 启明星/黄昏星需连续3根K线，固定值

    # -- 订单簿分析参数 --
    ORDERBOOK_SLOPE_DEPTH = 10              # 计算斜率的挂单档位数，[5, 20]
    ORDERBOOK_CONCENTRATION_LEVELS = 5      # 计算集中度的前N档，[3, 10]
    WALL_RESILIENCE_CANCEL_RATIO = 0.4      # 纸墙判定撤单率阈值，[0.2, 0.6]
    WALL_RESILIENCE_REFILL_RATIO = 0.7      # 真墙判定回补率阈值，[0.5, 0.9]

    # -- 均线位置分区阈值（ATR倍数） --
    MA12_ON_LINE_ATR = 0.3
    MA12_NEAR_ATR = 0.8
    MA12_FAR_ATR = 1.5

    # -- 降级默认值 --
    DEFAULT_PATTERN = "none"
    DEFAULT_CONFIDENCE = 0.0
    DEFAULT_SLOPE = 0.0
    DEFAULT_CONCENTRATION = 0.0
    DEFAULT_RESILIENCE = "unknown"
    DEFAULT_MA12_POSITION = "on_line"

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """可接受配置字典覆盖默认阈值。"""
        self._config = config or {}
        self._pin_bar_ratio = float(self._config.get("pin_bar_wick_body_ratio", self.PIN_BAR_WICK_BODY_RATIO))
        self._engulfing_ratio = float(self._config.get("engulfing_body_ratio", self.ENGULFING_BODY_RATIO))
        self._doji_max_ratio = float(self._config.get("doji_body_range_ratio", self.DOJI_BODY_RANGE_RATIO))
        self._hammer_ratio = float(self._config.get("hammer_lower_wick_ratio", self.HAMMER_LOWER_WICK_RATIO))
        self._slope_depth = int(self._config.get("slope_depth", self.ORDERBOOK_SLOPE_DEPTH))
        self._conc_levels = int(self._config.get("concentration_levels", self.ORDERBOOK_CONCENTRATION_LEVELS))
        self._wall_cancel_ratio = float(self._config.get("wall_cancel_ratio", self.WALL_RESILIENCE_CANCEL_RATIO))
        self._wall_refill_ratio = float(self._config.get("wall_refill_ratio", self.WALL_RESILIENCE_REFILL_RATIO))

        # 形态缓存：键为K线组合的哈希，值为 (pattern, confidence)
        self._pattern_cache: Dict[str, Tuple[str, float]] = {}
        self._max_cache_size = 128
        logger.info(
            f"VisualCortex 初始化完成，PinBar阈值:{self._pin_bar_ratio}, 吞没阈值:{self._engulfing_ratio}, "
            f"挂单档位:{self._slope_depth}, 集中度计算前{self._conc_levels}档"
        )

    # ────────────────────────── 公共接口 ──────────────────────────
    def perceive(
        self,
        kline_sequence: Optional[List[Dict[str, float]]] = None,
        orderbook_snapshot: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """执行一次视觉感知。"""
        warnings: List[str] = []
        reason_parts: List[str] = []

        pattern, conf = self._identify_pattern(kline_sequence, warnings)
        reason_parts.append(f"形态:{pattern}({conf:.2f})")

        ma12_pos = self._determine_ma12_position(kline_sequence, orderbook_snapshot, warnings)
        reason_parts.append(f"MA12:{ma12_pos}")

        slope, conc = self._analyze_orderbook_shape(orderbook_snapshot, warnings)
        reason_parts.append(f"斜率:{slope:.3f}, 集中度:{conc:.3f}")

        resilience = self._assess_wall_resilience(orderbook_snapshot, warnings)
        reason_parts.append(f"墙:{resilience}")

        reason = "视觉感知完成: " + ", ".join(reason_parts)
        return {
            "status": "ok" if not warnings else "warning",
            "candlestick_pattern": pattern,
            "pattern_confidence": conf,
            "ma12_position": ma12_pos,
            "orderbook_slope": slope,
            "orderbook_concentration": conc,
            "wall_resilience": resilience,
            "reason": reason,
            "warnings": warnings,
        }

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """使用含多种形态的模拟数据进行自检。"""
        try:
            instance = cls()
            # 模拟Pin Bar + 启明星组合
            klines = [
                {"open": 100, "high": 105, "low": 98, "close": 99},   # 长下影 Pin Bar
                {"open": 98, "high": 99, "low": 95, "close": 96},    # 阴线
                {"open": 96, "high": 102, "low": 95.5, "close": 101},# 吞没阳线
            ]
            ob = {
                "bids": [[100, 1.5, "stable"]] * 5,
                "asks": [[101, 1.0, "stable"]] * 3,
                "ma12_value": 99.5,
                "atr_value": 1.2,
                "wall_data": {"cancel_ratio": 0.15, "refill_ratio": 0.9},
            }
            res = instance.perceive(klines, ob)
            if res["status"] not in ("ok", "warning"):
                return {"status": "error", "message": f"正常数据测试失败: {res['reason']}"}
            # 应至少识别出吞没形态
            if "engulfing" not in res["candlestick_pattern"]:
                return {"status": "error", "message": f"吞没形态未识别: {res['candlestick_pattern']}"}
            # 空输入降级
            deg = instance.perceive(None, None)
            if deg["candlestick_pattern"] != cls.DEFAULT_PATTERN or deg["pattern_confidence"] != cls.DEFAULT_CONFIDENCE:
                return {"status": "error", "message": "空输入降级失败"}
            return {"status": "ok", "message": "所有测试通过"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _identify_pattern(
        self, klines: Optional[List[Dict[str, float]]], warnings: List[str]
    ) -> Tuple[str, float]:
        """基于最近3-4根K线识别技术形态并返回置信度。"""
        if not klines or len(klines) < 2:
            warnings.append("K线数据不足，无法识别形态")
            return self.DEFAULT_PATTERN, self.DEFAULT_CONFIDENCE

        # 使用最近4根K线的哈希进行缓存
        cache_key = self._hash_klines(klines[-4:])
        if cache_key in self._pattern_cache:
            return self._pattern_cache[cache_key]

        pattern = "none"
        confidence = 0.0

        try:
            latest = klines[-1]
            prev = klines[-2]
            third = klines[-3] if len(klines) >= 3 else None

            o1, h1, l1, c1 = float(prev["open"]), float(prev["high"]), float(prev["low"]), float(prev["close"])
            o2, h2, l2, c2 = float(latest["open"]), float(latest["high"]), float(latest["low"]), float(latest["close"])
            body1 = abs(c1 - o1)
            body2 = abs(c2 - o2)
            range1 = h1 - l1
            range2 = h2 - l2

            # ---- 1. 启明星/黄昏星 (需要3根K线) ----
            if third is not None:
                o0, c0 = float(third["open"]), float(third["close"])
                pattern, confidence = self._check_star_pattern(o0, c0, o1, c1, o2, c2, body1, body2, range2)
                if confidence > 0.6:
                    self._cache_pattern(cache_key, pattern, confidence)
                    return pattern, confidence

            # ---- 2. 吞没形态 ----
            if body2 > 0 and body1 > 0:
                if c1 < o1 and c2 > o2 and o2 <= c1 and c2 >= o1:  # 阳吞阴
                    pattern = "engulfing_bullish"
                    confidence = min(0.95, body2 / (body1 * self._engulfing_ratio))
                elif c1 > o1 and c2 < o2 and o2 >= c1 and c2 <= o1:  # 阴吞阳
                    pattern = "engulfing_bearish"
                    confidence = min(0.95, body2 / (body1 * self._engulfing_ratio))
                if confidence > 0.5:
                    self._cache_pattern(cache_key, pattern, confidence)
                    return pattern, confidence

            # ---- 3. Pin Bar ----
            if range2 > 0 and body2 > 0:
                upper_wick = h2 - max(o2, c2)
                lower_wick = min(o2, c2) - l2
                wick_body_ratio = max(upper_wick, lower_wick) / body2
                body_range_ratio = body2 / range2
                if wick_body_ratio >= self._pin_bar_ratio and body_range_ratio <= self.PIN_BAR_BODY_RANGE_RATIO:
                    if lower_wick > upper_wick:
                        pattern = "pin_bar_bullish"
                    else:
                        pattern = "pin_bar_bearish"
                    confidence = min(0.9, wick_body_ratio / (self._pin_bar_ratio * 2))
                    self._cache_pattern(cache_key, pattern, confidence)
                    return pattern, confidence

            # ---- 4. 锤子线/流星线 ----
            if range2 > 0 and body2 > 0:
                upper_wick = h2 - max(o2, c2)
                lower_wick = min(o2, c2) - l2
                body_range_ratio = body2 / range2
                if body_range_ratio <= self.HAMMER_BODY_RANGE_RATIO:
                    if lower_wick >= self._hammer_ratio * body2 and upper_wick <= body2 * 0.5:
                        pattern = "hammer"
                        confidence = 0.7
                        self._cache_pattern(cache_key, pattern, confidence)
                        return pattern, confidence
                    if upper_wick >= self._hammer_ratio * body2 and lower_wick <= body2 * 0.5:
                        pattern = "shooting_star"
                        confidence = 0.7
                        self._cache_pattern(cache_key, pattern, confidence)
                        return pattern, confidence

            # ---- 5. 十字星 ----
            if range2 > 0 and body2 / range2 <= self._doji_max_ratio:
                pattern = "doji"
                confidence = 0.6
                self._cache_pattern(cache_key, pattern, confidence)
                return pattern, confidence

        except (KeyError, ValueError, ZeroDivisionError) as e:
            logger.debug(f"形态识别异常: {e}")
            warnings.append(f"形态识别异常: {str(e)[:50]}")

        self._cache_pattern(cache_key, pattern, confidence)
        return pattern, confidence

    def _check_star_pattern(
        self, o0: float, c0: float, o1: float, c1: float,
        o2: float, c2: float, body1: float, body2: float, range2: float
    ) -> Tuple[str, float]:
        """检测启明星/黄昏星。"""
        # 启明星：第一根长阴，第二根小实体（十字星），第三根长阳收盘高于第一根中点
        if c0 < o0 and body1 > 0 and body2 > 0:
            first_body = abs(c0 - o0)
            if body1 < first_body * 0.3 and c2 > o2 and c2 > (o0 + c0) / 2:
                return "morning_star", 0.85
        # 黄昏星：第一根长阳，第二根小实体，第三根长阴收盘低于第一根中点
        if c0 > o0 and body1 > 0 and body2 > 0:
            first_body = abs(c0 - o0)
            if body1 < first_body * 0.3 and c2 < o2 and c2 < (o0 + c0) / 2:
                return "evening_star", 0.85
        return "none", 0.0

    def _analyze_orderbook_shape(
        self, ob: Optional[Dict[str, Any]], warnings: List[str]
    ) -> Tuple[float, float]:
        """计算挂单量分布的对数斜率与集中度。"""
        if not ob or "bids" not in ob or "asks" not in ob:
            warnings.append("订单簿数据缺失")
            return self.DEFAULT_SLOPE, self.DEFAULT_CONCENTRATION

        try:
            all_levels = []
            for side in ("bids", "asks"):
                for i, level in enumerate(ob.get(side, [])[:self._slope_depth]):
                    if len(level) >= 2:
                        qty = float(level[1])
                        if qty > 0:
                            all_levels.append((float(i + 1), qty))

            if len(all_levels) < 3:
                return self.DEFAULT_SLOPE, self.DEFAULT_CONCENTRATION

            # 对数斜率
            log_levels = [(x, math.log(y + 1e-10)) for x, y in all_levels]
            n = len(log_levels)
            sum_x = sum(p[0] for p in log_levels)
            sum_y = sum(p[1] for p in log_levels)
            sum_xy = sum(p[0] * p[1] for p in log_levels)
            sum_x2 = sum(p[0] * p[0] for p in log_levels)
            denom = n * sum_x2 - sum_x * sum_x
            slope = (n * sum_xy - sum_x * sum_y) / denom if denom != 0 else 0.0

            # 集中度 = 前 conc_levels 档挂单量占总深度的比例
            front_vol = sum(float(lvl[1]) for lvl in all_levels[:self._conc_levels]) if len(all_levels) >= self._conc_levels else sum(y for _, y in all_levels)
            total_vol = sum(y for _, y in all_levels)
            concentration = front_vol / total_vol if total_vol > 0 else 0.0

            return max(-5.0, min(5.0, slope)), min(1.0, concentration)
        except Exception as e:
            logger.debug(f"订单簿形状分析异常: {e}")
            warnings.append(f"形状分析异常: {str(e)[:50]}")
            return self.DEFAULT_SLOPE, self.DEFAULT_CONCENTRATION

    def _assess_wall_resilience(self, ob: Optional[Dict[str, Any]], warnings: List[str]) -> str:
        """基于撤单率与回补率判定挂单墙类型。"""
        if not ob or "wall_data" not in ob:
            return self.DEFAULT_RESILIENCE
        try:
            wd = ob["wall_data"]
            cancel = float(wd.get("cancel_ratio", 0.0))
            refill = float(wd.get("refill_ratio", 0.0))
            if cancel >= self._wall_cancel_ratio:
                return "paper_wall"
            if cancel < 0.2 and refill >= self._wall_refill_ratio:
                return "true_wall"
            return "uncertain"
        except Exception as e:
            logger.debug(f"挂单墙韧性评估异常: {e}")
            warnings.append(f"墙韧性异常: {str(e)[:50]}")
            return self.DEFAULT_RESILIENCE

    def _determine_ma12_position(
        self, klines: Optional[List[Dict[str, float]]], ob: Optional[Dict[str, Any]], warnings: List[str]
    ) -> str:
        """判定当前价格相对于M12均线的位置。"""
        if not klines or not ob:
            warnings.append("缺少K线或订单簿数据，使用默认MA12位置")
            return self.DEFAULT_MA12_POSITION
        try:
            price = float(klines[-1].get("close", 0))
            ma12 = float(ob.get("ma12_value", price))
            atr = float(ob.get("atr_value", 1.0))
            if atr <= 0:
                return self.DEFAULT_MA12_POSITION
            dist = abs(price - ma12) / atr
            if dist <= self.MA12_ON_LINE_ATR:
                return "on_line"
            if dist <= self.MA12_NEAR_ATR:
                return "near"
            if dist <= self.MA12_FAR_ATR:
                return "far"
            return "extreme"
        except Exception as e:
            logger.debug(f"MA12位置判定异常: {e}")
            warnings.append(f"MA12异常: {str(e)[:50]}")
            return self.DEFAULT_MA12_POSITION

    # ────────────────────────── 缓存与工具 ──────────────────────────
    def _hash_klines(self, klines: List[Dict[str, float]]) -> str:
        """生成K线序列的短哈希，用于形态缓存。"""
        raw = "".join(
            f"{k.get('open','')}{k.get('high','')}{k.get('low','')}{k.get('close','')}"
            for k in klines
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def _cache_pattern(self, key: str, pattern: str, confidence: float) -> None:
        """写入缓存，并维护容量上限。"""
        if len(self._pattern_cache) >= self._max_cache_size:
            # 随机清除一半旧缓存
            old = list(self._pattern_cache.keys())[:64]
            for k in old:
                del self._pattern_cache[k]
        self._pattern_cache[key] = (pattern, confidence)

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))
