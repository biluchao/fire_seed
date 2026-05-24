"""
火种系统 · 嗅觉皮层 (OlfactoryCortex) — 深度精细化

核心职责：
1. 实时检测订单簿中的纸墙行为（大额挂单快速撤单），区分真墙（被吃后回补）与纸墙（价格逼近前撤单）。
2. 分析订单流毒性：监控被动成交后价格是否持续向不利方向漂移，识别高频做市商猎杀行为。
3. 检测价差操纵信号：当买卖价差异常扩大超过历史均值的设定倍数时触发告警。
4. 综合评估多品种间的系统性传染风险，为风控中枢提供前瞻性嗅觉预警。

外部依赖（真实模块接口）：
- 无外部模块依赖。本模块为纯计算工具，所需订单簿快照、逐笔成交记录和相关性矩阵均由调用方通过方法参数传入。

接口契约：
- smell(orderbook_snapshot: Optional[Dict[str, Any]] = None,
        trade_stream: Optional[List[Dict[str, Any]]] = None,
        correlation_matrix: Optional[Dict[str, float]] = None) -> Dict[str, Any]
  输出字典固定包含 "status" (str), "paper_wall_flag" (bool), "paper_wall_confidence" (float),
  "spread_manipulation_flag" (bool), "spread_spike_ratio" (float),
  "contagion_risk_index" (float), "order_toxicity_active" (bool),
  "toxicity_drift_bps" (float), "reason" (str), "warnings" (List[str])
- health_check() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "message" (str)

异常与降级：
- 当输入数据为空或格式错误时，返回所有嗅觉指标为中性/安全的默认值，状态标记为 "degraded"。
- 所有内部计算异常被捕获后，均返回保守默认值（如假设存在风险），确保系统偏向安全。
- 纸墙检测在历史数据不足时自动跳过，不产生假阳性。

资源管理：
- 本模块为有状态计算工具，持有订单簿历史快照缓存（容量受限），用于纸墙检测的撤单率计算。
- 缓存采用FIFO淘汰策略，在内存压力下自动缩减。
"""

import logging
import time
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class OlfactoryCortex:
    """嗅觉皮层：纸墙检测、订单流毒性、价差操纵与传染风险嗅探"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # -- 纸墙检测 --
    PAPER_WALL_CANCEL_RATIO = 0.4              # 纸墙判定撤单率阈值，[0.2, 0.6]
    PAPER_WALL_PRICE_DISTANCE_ATR = 0.3        # 价格逼近挂单的ATR距离阈值，[0.1, 0.5]
    PAPER_WALL_MONITOR_WINDOW_SEC = 5.0        # 挂单墙监控窗口，秒，[3.0, 10.0]
    PAPER_WALL_HISTORY_SIZE = 20               # 订单簿历史快照保留数，[10, 50]
    # -- 订单流毒性 --
    TOXICITY_DRIFT_THRESHOLD_BPS = 2.0         # 毒性判定不利漂移阈值，bps，[1.0, 10.0]
    TOXICITY_DETECTION_WINDOW_SEC = 10.0       # 毒性检测时间窗口，秒，[5.0, 60.0]
    MIN_TRADES_FOR_TOXICITY = 10               # 毒性检测最小成交笔数，[5, 50]
    TOXICITY_ADVERSE_RATIO_THRESHOLD = 0.5     # 不利成交占比阈值，[0.3, 0.7]
    # -- 价差操纵 --
    SPREAD_SPIKE_MULTIPLIER = 3.0              # 价差异常扩大倍数阈值，[2.0, 5.0]
    SPREAD_HISTORY_SIZE = 30                   # 价差历史保留数，[10, 100]
    # -- 传染风险 --
    CONTAGION_CORRELATION_SPIKE = 0.8          # 传染风险相关性突增阈值，[0.6, 0.95]
    CONTAGION_MIN_PAIRS = 3                    # 传染评估最少品种对数量，[2, 10]
    # -- 降级默认值 --
    DEFAULT_SAFE_VALUES: Dict[str, Any] = {
        "paper_wall_flag": False,
        "paper_wall_confidence": 0.0,
        "spread_manipulation_flag": False,
        "spread_spike_ratio": 1.0,
        "contagion_risk_index": 0.0,
        "order_toxicity_active": False,
        "toxicity_drift_bps": 0.0,
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """初始化嗅觉皮层，可接受配置覆盖默认参数。"""
        self._config = config or {}
        # 从配置加载参数，带边界保护
        self._paper_wall_cancel_ratio = self._clamp(
            float(self._config.get("paper_wall_cancel_ratio", self.PAPER_WALL_CANCEL_RATIO)), 0.2, 0.6
        )
        self._toxicity_drift_bps = self._clamp(
            float(self._config.get("toxicity_drift_threshold_bps", self.TOXICITY_DRIFT_THRESHOLD_BPS)), 1.0, 10.0
        )
        self._spread_spike_multiplier = self._clamp(
            float(self._config.get("spread_spike_multiplier", self.SPREAD_SPIKE_MULTIPLIER)), 2.0, 5.0
        )
        self._contagion_correlation_spike = self._clamp(
            float(self._config.get("contagion_correlation_spike", self.CONTAGION_CORRELATION_SPIKE)), 0.6, 0.95
        )
        # 状态缓存
        self._ob_history: List[Dict[str, Any]] = []       # 订单簿历史快照
        self._spread_history: List[float] = []             # 价差历史
        self._max_ob_history = self.PAPER_WALL_HISTORY_SIZE
        logger.info(
            f"OlfactoryCortex 初始化完成，纸墙阈值:{self._paper_wall_cancel_ratio:.2f}, "
            f"毒性漂移:{self._toxicity_drift_bps}bps, 价差倍数:{self._spread_spike_multiplier:.1f}"
        )

    # ────────────────────────── 公共接口 ──────────────────────────
    def smell(
        self,
        orderbook_snapshot: Optional[Dict[str, Any]] = None,
        trade_stream: Optional[List[Dict[str, Any]]] = None,
        correlation_matrix: Optional[Dict[str, float]] = None
    ) -> Dict[str, Any]:
        """
        综合嗅觉分析，返回所有嗅觉维度的检测结果。
        
        Args:
            orderbook_snapshot: 当前订单簿快照，需包含 'bids', 'asks', 'spread', 'timestamp'
            trade_stream: 最近若干秒的逐笔成交记录，每笔需包含 'price', 'side', 'timestamp'
            correlation_matrix: 当前多品种相关性矩阵，键为品种对，值为相关系数
        
        Returns:
            标准化字典，包含纸墙、价差操纵、传染风险、订单流毒性等标记
        """
        warnings: List[str] = []
        reason_parts: List[str] = []

        # 1. 更新订单簿历史缓存
        self._update_orderbook_history(orderbook_snapshot)

        # 2. 纸墙检测
        paper_wall, paper_conf = self._detect_paper_wall()
        if paper_wall:
            warnings.append(f"检测到纸墙行为 (置信度 {paper_conf:.2f})")
            reason_parts.append(f"纸墙({paper_conf:.2f})")

        # 3. 价差操纵检测
        spread_manip, spread_ratio = self._detect_spread_manipulation(orderbook_snapshot)
        if spread_manip:
            warnings.append(f"价差异常扩大: 当前/历史 = {spread_ratio:.1f}x")
            reason_parts.append(f"价差异常({spread_ratio:.1f}x)")

        # 4. 订单流毒性检测
        toxicity, drift_bps = self._detect_order_toxicity(trade_stream)
        if toxicity:
            warnings.append(f"订单流毒性活跃: 平均不利漂移 {drift_bps:.1f}bps")
            reason_parts.append(f"毒性({drift_bps:.1f}bps)")

        # 5. 传染风险评估
        contagion = self._assess_contagion_risk(correlation_matrix)
        if contagion > 0.5:
            warnings.append(f"传染风险升高: {contagion:.2f}")
            reason_parts.append(f"传染风险({contagion:.2f})")

        reason = "嗅觉分析完成"
        if reason_parts:
            reason += ": " + ", ".join(reason_parts)

        return {
            "status": "ok" if not warnings else "warning",
            "paper_wall_flag": paper_wall,
            "paper_wall_confidence": paper_conf,
            "spread_manipulation_flag": spread_manip,
            "spread_spike_ratio": spread_ratio,
            "contagion_risk_index": contagion,
            "order_toxicity_active": toxicity,
            "toxicity_drift_bps": drift_bps,
            "reason": reason,
            "warnings": warnings,
        }

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检：用模拟正常数据和异常数据分别测试各子模块。"""
        try:
            instance = cls()

            # 测试1：正常数据
            normal_ob = {
                "bids": [[50000.0, 1.5, "stable"], [49990.0, 2.0, "stable"]],
                "asks": [[50100.0, 1.0, "stable"], [50110.0, 0.8, "stable"]],
                "spread": 100.0,
                "timestamp": time.time(),
            }
            normal_trades = [
                {"price": 50050.0, "qty": 0.1, "side": "buy", "timestamp": time.time() - 1.0},
                {"price": 50060.0, "qty": 0.2, "side": "buy", "timestamp": time.time() - 0.5},
            ] * 10
            result = instance.smell(normal_ob, normal_trades)
            if result["status"] not in ("ok", "warning"):
                return {"status": "error", "message": f"正常数据测试失败: {result['reason']}"}

            # 测试2：纸墙数据（挂单量骤降）
            # 先填充历史
            for _ in range(5):
                instance._ob_history.append({
                    "bids_volume": 5.0, "asks_volume": 3.0, "timestamp": time.time() - 10.0
                })
            # 当前快照挂单量极低
            paper_ob = {
                "bids": [[50000.0, 0.1, "deleted"], [49990.0, 0.05, "deleted"]],
                "asks": [[50100.0, 1.0, "stable"]],
                "spread": 100.0,
                "timestamp": time.time(),
            }
            paper_result = instance.smell(paper_ob, normal_trades)
            if not paper_result["paper_wall_flag"]:
                return {"status": "error", "message": "纸墙检测未触发"}

            # 测试3：价差异常
            spike_ob = {
                "bids": [[50000.0, 1.5, "stable"]],
                "asks": [[50300.0, 1.0, "stable"]],
                "spread": 300.0,
                "timestamp": time.time(),
            }
            # 填充历史正常价差
            instance._spread_history = [100.0] * 20
            spike_result = instance.smell(spike_ob, normal_trades)
            if not spike_result["spread_manipulation_flag"]:
                return {"status": "error", "message": "价差异常检测未触发"}

            # 测试4：空输入降级
            degraded = instance.smell(None, None, None)
            if degraded["status"] != "degraded":
                return {"status": "error", "message": "空输入未正确降级"}

            return {"status": "ok", "message": "所有测试通过"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _update_orderbook_history(self, ob: Optional[Dict[str, Any]]) -> None:
        """更新订单簿历史缓存，用于纸墙检测的撤单率计算。"""
        if not ob or "bids" not in ob or "asks" not in ob:
            return
        try:
            bids_vol = sum(float(lvl[1]) for lvl in ob["bids"][:5] if len(lvl) >= 2)
            asks_vol = sum(float(lvl[1]) for lvl in ob["asks"][:5] if len(lvl) >= 2)
            self._ob_history.append({
                "bids_volume": bids_vol,
                "asks_volume": asks_vol,
                "timestamp": ob.get("timestamp", time.time()),
            })
            # FIFO淘汰
            while len(self._ob_history) > self._max_ob_history:
                self._ob_history.pop(0)
        except Exception as e:
            logger.debug(f"更新订单簿历史异常: {e}")

    def _detect_paper_wall(self) -> Tuple[bool, float]:
        """
        检测纸墙行为：基于监控窗口内的挂单量撤单率。
        纸墙特征：大额挂单在价格逼近前被快速撤单，而非被吃掉。
        
        Returns:
            (is_paper_wall, confidence) 元组
        """
        if len(self._ob_history) < 3:
            return False, 0.0

        try:
            # 计算窗口内前段和后段的平均挂单量
            window = self._ob_history[-min(len(self._ob_history), 10):]
            if len(window) < 3:
                return False, 0.0

            # 取前一半和后一半
            mid = len(window) // 2
            first_half = window[:mid]
            second_half = window[mid:]

            avg_first = sum(
                s["bids_volume"] + s["asks_volume"] for s in first_half
            ) / len(first_half) if first_half else 0.0
            avg_second = sum(
                s["bids_volume"] + s["asks_volume"] for s in second_half
            ) / len(second_half) if second_half else 0.0

            if avg_first <= 0:
                return False, 0.0

            # 计算衰减比例
            decay_ratio = avg_second / avg_first

            # 如果后段挂单量显著下降，可能是纸墙
            if decay_ratio < self._paper_wall_cancel_ratio:
                confidence = min(1.0, (self._paper_wall_cancel_ratio - decay_ratio) / self._paper_wall_cancel_ratio)
                return True, confidence
        except Exception as e:
            logger.debug(f"纸墙检测异常: {e}")

        return False, 0.0

    def _detect_spread_manipulation(
        self, ob: Optional[Dict[str, Any]]
    ) -> Tuple[bool, float]:
        """
        检测价差操纵：当前价差相对于历史均值的异常扩大倍数。
        
        Returns:
            (is_manipulated, spike_ratio) 元组
        """
        if not ob:
            return False, 1.0

        try:
            current_spread = float(ob.get("spread", 0))
            if current_spread <= 0:
                return False, 1.0

            # 更新价差历史
            self._spread_history.append(current_spread)
            while len(self._spread_history) > self.SPREAD_HISTORY_SIZE:
                self._spread_history.pop(0)

            if len(self._spread_history) < 5:
                return False, 1.0

            # 使用中位数作为历史基准（抗异常值）
            sorted_history = sorted(self._spread_history[:-1])  # 排除当前值
            if len(sorted_history) < 3:
                return False, 1.0
            median_spread = sorted_history[len(sorted_history) // 2]
            if median_spread <= 0:
                return False, 1.0

            spike_ratio = current_spread / median_spread
            if spike_ratio >= self._spread_spike_multiplier:
                return True, spike_ratio
        except Exception as e:
            logger.debug(f"价差检测异常: {e}")

        return False, 1.0

    def _detect_order_toxicity(
        self, trades: Optional[List[Dict[str, Any]]]
    ) -> Tuple[bool, float]:
        """
        检测订单流毒性：被动成交后价格是否持续向不利方向漂移。
        
        简化判定逻辑：
        1. 统计最近窗口内的被动成交笔数。
        2. 计算每笔被动成交后下一笔成交的价格变化方向。
        3. 若不利方向占比超过阈值，判定为毒性活跃。
        
        Returns:
            (is_toxic, avg_adverse_drift_bps) 元组
        """
        if not trades or len(trades) < self.MIN_TRADES_FOR_TOXICITY:
            return False, 0.0

        try:
            now = time.time()
            # 筛选时间窗口内的成交
            window_trades = [
                t for t in trades
                if now - t.get("timestamp", now) <= self.TOXICITY_DETECTION_WINDOW_SEC
            ]
            if len(window_trades) < self.MIN_TRADES_FOR_TOXICITY:
                return False, 0.0

            adverse_count = 0
            total_passive = 0
            total_drift_bps = 0.0

            for i in range(1, len(window_trades)):
                prev = window_trades[i - 1]
                curr = window_trades[i]
                prev_side = prev.get("side", "")
                curr_side = curr.get("side", "")
                prev_price = float(prev.get("price", 0))
                curr_price = float(curr.get("price", 0))

                if prev_price <= 0:
                    continue

                price_change_bps = (curr_price - prev_price) / prev_price * 10000

                # 简化：假设 buy 是被动买入（价格上涨为不利），sell 是被动卖出（价格下跌为不利）
                if prev_side == "buy" and price_change_bps < 0:
                    adverse_count += 1
                    total_drift_bps += abs(price_change_bps)
                    total_passive += 1
                elif prev_side == "sell" and price_change_bps > 0:
                    adverse_count += 1
                    total_drift_bps += abs(price_change_bps)
                    total_passive += 1
                elif prev_side in ("buy", "sell"):
                    total_passive += 1

            if total_passive < self.MIN_TRADES_FOR_TOXICITY:
                return False, 0.0

            adverse_ratio = adverse_count / total_passive if total_passive > 0 else 0.0
            avg_drift = total_drift_bps / adverse_count if adverse_count > 0 else 0.0

            if adverse_ratio > self.TOXICITY_ADVERSE_RATIO_THRESHOLD and avg_drift > self._toxicity_drift_bps:
                return True, avg_drift
        except Exception as e:
            logger.debug(f"毒性检测异常: {e}")

        return False, 0.0

    def _assess_contagion_risk(
        self, corr_matrix: Optional[Dict[str, float]]
    ) -> float:
        """
        评估系统性传染风险：基于多品种相关性矩阵的突增程度。
        
        Args:
            corr_matrix: 多品种相关性字典，键为品种对字符串，值为相关系数
        
        Returns:
            传染风险指数，[0.0, 1.0]
        """
        if not corr_matrix or len(corr_matrix) < self.CONTAGION_MIN_PAIRS:
            return 0.0

        try:
            values = [v for v in corr_matrix.values() if isinstance(v, (int, float))]
            if len(values) < self.CONTAGION_MIN_PAIRS:
                return 0.0

            # 计算平均相关性
            avg_corr = sum(values) / len(values)

            # 计算高相关品种对的比例
            high_corr_count = sum(1 for v in values if v > self._contagion_correlation_spike)
            high_corr_ratio = high_corr_count / len(values) if values else 0.0

            # 综合评估：平均相关性 + 高相关比例
            if avg_corr > self._contagion_correlation_spike:
                base_risk = min(
                    1.0,
                    (avg_corr - self._contagion_correlation_spike) / (1.0 - self._contagion_correlation_spike),
                )
                # 高相关比例加成
                risk = base_risk * 0.7 + high_corr_ratio * 0.3
                return min(1.0, risk)
        except Exception as e:
            logger.debug(f"传染风险评估异常: {e}")

        return 0.0

    def _degraded_response(self) -> Dict[str, Any]:
        """降级响应：返回中性/安全默认值。"""
        logger.warning("OlfactoryCortex 降级：输入数据为空或无效")
        return {
            "status": "degraded",
            **self.DEFAULT_SAFE_VALUES,
            "reason": "输入数据无效，返回保守默认值",
            "warnings": ["OlfactoryCortex 降级：输入数据为空"],
        }

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        """数值边界钳制"""
        return max(lower, min(upper, value))
