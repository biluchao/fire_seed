"""
火种系统 · 触觉皮层 (TactileCortex) — 深度精细化

核心职责：
1. 基于订单簿快照评估市场流动性纹理：输出流动性等级(L1-L5)和深度衰减速率。
2. 分析逐笔成交数据，计算成交脉搏变异系数(CV)，区分算法主导/散户主导的交易环境。
3. 基于短期与长期ATR的比值，评估当前波动率结构（扩张/正常/收缩）。

外部依赖（真实模块接口）：
- 无外部模块依赖。所有必要数据（订单簿快照、逐笔成交流、ATR值）均由调用方通过方法参数传入。

接口契约：
- sense(orderbook_snapshot: Dict[str, Any], trade_stream: List[Dict[str, Any]], atr_short: float, atr_long: float) -> Dict[str, Any]
  输出字典固定包含 "status" (str), "liquidity_level" (str), "depth_decay_speed" (float),
  "trade_pulse_cv" (float), "volatility_regime" (str), "reason" (str), "warnings" (List[str])
- health_check() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "message" (str)

异常与降级：
- 当输入数据为空或无效时，返回基于保守假设的默认值（流动性L1、高CV、波动率扩张），状态标记为 "degraded"。
- 所有计算异常被内部捕获，不向外抛出，确保调用方安全。

资源管理：
- 本模块为无状态计算工具，不持有任何外部资源，无需显式释放。
"""

import logging
import math
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class TactileCortex:
    """触觉皮层：流动性纹理、成交脉搏与波动率结构感知"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 流动性等级阈值（前N档深度占1小时均值的比例）
    LIQUIDITY_L5_RATIO = 3.0              # L5 极度充裕，[2.0, 5.0]
    LIQUIDITY_L4_RATIO = 1.5              # L4 充裕，[1.2, 2.0]
    LIQUIDITY_L3_RATIO = 1.0              # L3 正常，[0.8, 1.2]
    LIQUIDITY_L2_RATIO = 0.5              # L2 稀薄，[0.3, 0.8]
    # 成交脉搏CV阈值
    CV_ALGORITHMIC_THRESHOLD = 0.3         # CV < 0.3 判定为算法做市商主导
    CV_MANUAL_THRESHOLD = 0.7             # CV > 0.7 判定为散户/手动交易为主
    CV_WINDOW_TRADES = 50                  # 计算CV所需最少成交笔数，[20, 200]
    # 波动率结构阈值
    VOL_REGIME_EXPANDING = 1.3            # 短期ATR/长期ATR > 1.3 判定为扩张
    VOL_REGIME_CONTRACTING = 0.7          # 短期ATR/长期ATR < 0.7 判定为收缩
    # 深度衰减速率计算参数
    DEPTH_DECAY_WINDOW_SEC = 10           # 深度衰减计算窗口，秒，[5, 30]
    # 降级默认值（保守假设）
    DEFAULT_LIQUIDITY = "L1"              # 默认假设极度稀薄
    DEFAULT_CV = 1.5                      # 默认假设高度杂乱
    DEFAULT_VOL_REGIME = "expanding"      # 默认假设波动扩张
    DEFAULT_DECAY_SPEED = 10.0            # 默认深度衰减速率 bps/s

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """可接受配置覆盖默认参数。"""
        self._config = config or {}
        self._l5 = self._clamp(self._config.get("l5_ratio", self.LIQUIDITY_L5_RATIO), 2.0, 5.0)
        self._l4 = self._clamp(self._config.get("l4_ratio", self.LIQUIDITY_L4_RATIO), 1.2, 2.0)
        self._l3 = self._clamp(self._config.get("l3_ratio", self.LIQUIDITY_L3_RATIO), 0.8, 1.2)
        self._l2 = self._clamp(self._config.get("l2_ratio", self.LIQUIDITY_L2_RATIO), 0.3, 0.8)
        logger.info("TactileCortex 初始化完成")

    # ────────────────────────── 公共接口 ──────────────────────────
    def sense(
        self,
        orderbook_snapshot: Optional[Dict[str, Any]] = None,
        trade_stream: Optional[List[Dict[str, Any]]] = None,
        atr_short: Optional[float] = None,
        atr_long: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        综合触觉感知：一次调用获取流动性、成交脉搏和波动率结构。
        """
        warnings: List[str] = []
        reason_parts: List[str] = []

        liquidity_level, depth_decay = self._assess_liquidity(orderbook_snapshot, warnings)
        reason_parts.append(f"流动性 {liquidity_level}")

        cv = self._calculate_trade_pulse_cv(trade_stream, warnings)
        reason_parts.append(f"脉搏CV {cv:.2f}")

        regime = self._assess_volatility_regime(atr_short, atr_long, warnings)
        reason_parts.append(f"波动率 {regime}")

        reason = "触觉感知完成: " + ", ".join(reason_parts)

        return {
            "status": "ok" if not warnings else "warning",
            "liquidity_level": liquidity_level,
            "depth_decay_speed": depth_decay,
            "trade_pulse_cv": cv,
            "volatility_regime": regime,
            "reason": reason,
            "warnings": warnings
        }

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检：用模拟数据测试核心逻辑。"""
        try:
            instance = cls()
            normal_ob = {"total_depth_ratio": 1.2}
            normal_trades = [
                {"timestamp": 1000.0, "qty": 0.1}, {"timestamp": 1001.0, "qty": 0.5},
                {"timestamp": 1002.0, "qty": 0.2}, {"timestamp": 1003.0, "qty": 1.0},
                {"timestamp": 1004.0, "qty": 0.3},
            ] * 20  # 生成100笔
            result = instance.sense(normal_ob, normal_trades, 1.5, 1.2)
            if result["status"] not in ("ok", "warning"):
                return {"status": "error", "message": f"正常数据测试失败: {result['reason']}"}
            if result["liquidity_level"] not in ("L1", "L2", "L3", "L4", "L5"):
                return {"status": "error", "message": f"流动性等级无效: {result['liquidity_level']}"}
            degraded = instance.sense(None, None, None, None)
            if degraded["status"] != "degraded":
                return {"status": "error", "message": "空输入未正确降级"}
            return {"status": "ok", "message": "所有测试通过"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _assess_liquidity(self, ob: Optional[Dict[str, Any]], warnings: List[str]) -> (str, float):
        """评估流动性等级和深度衰减速率"""
        if not ob or "total_depth_ratio" not in ob:
            warnings.append("订单簿数据缺失，使用保守流动性等级L1")
            return self.DEFAULT_LIQUIDITY, self.DEFAULT_DECAY_SPEED
        try:
            ratio = float(ob["total_depth_ratio"])
        except (ValueError, TypeError):
            warnings.append(f"深度比率无效: {ob.get('total_depth_ratio')}")
            return self.DEFAULT_LIQUIDITY, self.DEFAULT_DECAY_SPEED

        if ratio >= self._l5:
            level = "L5"
        elif ratio >= self._l4:
            level = "L4"
        elif ratio >= self._l3:
            level = "L3"
        elif ratio >= self._l2:
            level = "L2"
        else:
            level = "L1"

        decay_speed = float(ob.get("depth_decay_bps", 0.0))
        return level, decay_speed

    def _calculate_trade_pulse_cv(self, trades: Optional[List[Dict[str, Any]]], warnings: List[str]) -> float:
        """计算成交时间间隔的变异系数(CV)，判断交易主导方"""
        if not trades or len(trades) < self.CV_WINDOW_TRADES:
            warnings.append(f"成交笔数不足 ({len(trades) if trades else 0})，使用默认高CV")
            return self.DEFAULT_CV
        try:
            timestamps = [t.get("timestamp", 0.0) for t in trades if t.get("timestamp")]
            if len(timestamps) < 2:
                warnings.append("有效时间戳不足，使用默认高CV")
                return self.DEFAULT_CV
            timestamps.sort()
            intervals = [timestamps[i] - timestamps[i - 1] for i in range(1, len(timestamps))]
            if not intervals:
                return self.DEFAULT_CV
            mean_interval = sum(intervals) / len(intervals)
            if mean_interval <= 0:
                return self.DEFAULT_CV
            variance = sum((x - mean_interval) ** 2 for x in intervals) / len(intervals)
            std_interval = math.sqrt(variance)
            cv = std_interval / mean_interval
            return min(cv, 5.0)
        except Exception as e:
            logger.debug(f"计算CV异常: {e}")
            warnings.append(f"CV计算异常: {str(e)[:50]}")
            return self.DEFAULT_CV

    def _assess_volatility_regime(self, atr_short: Optional[float], atr_long: Optional[float], warnings: List[str]) -> str:
        """评估波动率结构"""
        if atr_short is None or atr_long is None or atr_long <= 0:
            warnings.append("ATR数据缺失，使用默认波动率扩张")
            return self.DEFAULT_VOL_REGIME
        try:
            ratio = atr_short / atr_long
            if ratio > self.VOL_REGIME_EXPANDING:
                return "expanding"
            elif ratio < self.VOL_REGIME_CONTRACTING:
                return "contracting"
            else:
                return "normal"
        except Exception as e:
            logger.debug(f"波动率结构评估异常: {e}")
            warnings.append(f"波动率评估异常: {str(e)[:50]}")
            return self.DEFAULT_VOL_REGIME

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))
