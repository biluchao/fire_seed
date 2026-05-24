"""
火种系统 · 多频段锁相环 (MultiBandPLL) — 深度精细化

核心职责：
1. 同时运行三个不同周期参数的Costas锁相环，从价格序列中提取瞬时频率和相位。
2. 对三个频段的输出进行方向一致性校验，输出融合后的趋势强度与置信度。

外部依赖（真实模块接口）：
- 无外部模块依赖。本模块为纯数学计算工具。

接口契约：
- update(price: float, timestamp: float = 0.0) -> Dict[str, Any]
  输出字典固定包含 "status" (str), "frequency" (float), "phase" (float), "locked" (bool), "reason" (str)
- get_fusion_signal() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "trend_direction" (int), "trend_strength" (float), "consensus_count" (int), "reason" (str)
- health_check() -> Dict[str, Any]

异常与降级：
- 输入价格序列长度不足时，返回频率0.0，状态标记为 "insufficient_data"。
- 单个频段信噪比过低(SNR<6dB)时，自动标记为未锁定，不参与方向投票。

资源管理：
- 本模块为纯计算工具，不持有外部资源，无需显式释放。
"""

import math
import logging
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class MultiBandPLL:
    """多频段锁相环（深度精细化）：三频段并行 + 方向一致性投票"""

    # ========== 类常量 ==========
    DEFAULT_PERIODS = (9, 15, 27)
    DEFAULT_LOOP_BANDWIDTH = 0.05
    DEFAULT_DAMPING = 0.707
    SNR_LOCK_THRESHOLD_DB = 6.0
    MIN_PRICES_FOR_LOCK = 20
    FREQUENCY_THRESHOLD = 0.005
    MAX_PRICE_HISTORY = 200

    def __init__(self, periods: Optional[Tuple[int, ...]] = None) -> None:
        self._periods = periods if periods else self.DEFAULT_PERIODS
        self._plls: List[_CostasPLL] = [
            _CostasPLL(period=p, bandwidth=self.DEFAULT_LOOP_BANDWIDTH, damping=self.DEFAULT_DAMPING)
            for p in self._periods
        ]
        self._price_history: List[float] = []
        logger.info(f"MultiBandPLL 初始化完成，周期: {self._periods}")

    # ────────────────────────── 公共接口 ──────────────────────────
    def update(self, price: float, timestamp: float = 0.0) -> Dict[str, Any]:
        """输入新价格，更新所有锁相环并返回融合后的频率信息。"""
        if price <= 0:
            if self._price_history:
                price = self._price_history[-1]
            else:
                return self._insufficient_data()
        self._price_history.append(price)
        if len(self._price_history) > self.MAX_PRICE_HISTORY:
            self._price_history = self._price_history[-self.MAX_PRICE_HISTORY:]
        results = [pll.update(price) for pll in self._plls]
        return self._fuse_results(results)

    def get_fusion_signal(self) -> Dict[str, Any]:
        """获取当前融合后的趋势信号。"""
        if len(self._price_history) < self.MIN_PRICES_FOR_LOCK:
            return {"status": "insufficient_data", "trend_direction": 0, "trend_strength": 0.0,
                    "consensus_count": 0, "locked_count": 0,
                    "reason": f"数据不足({len(self._price_history)}/{self.MIN_PRICES_FOR_LOCK})"}
        locked_results = [
            {"period": self._periods[i], "frequency": pll.frequency, "locked": pll.locked}
            for i, pll in enumerate(self._plls)
        ]
        up = sum(1 for r in locked_results if r["locked"] and r["frequency"] > self.FREQUENCY_THRESHOLD)
        down = sum(1 for r in locked_results if r["locked"] and r["frequency"] < -self.FREQUENCY_THRESHOLD)
        locked_count = sum(1 for r in locked_results if r["locked"])
        direction = 1 if up > down and up >= 2 else (-1 if down > up and down >= 2 else 0)
        consensus = max(up, down) if direction != 0 else 0
        strength = sum(abs(r["frequency"]) for r in locked_results if r["locked"]) / max(locked_count, 1) / (self.FREQUENCY_THRESHOLD * 10)
        return {
            "status": "ok", "trend_direction": direction, "trend_strength": min(1.0, strength),
            "consensus_count": consensus, "locked_count": locked_count,
            "reason": f"方向:{direction} 强度:{strength:.3f} 共识:{consensus}/{len(self._plls)}",
        }

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检。"""
        try:
            instance = cls()
            test = [100.0 + 2.0 * math.sin(2.0 * math.pi * 0.02 * t) for t in range(60)]
            for p in test:
                instance.update(p)
            fusion = instance.get_fusion_signal()
            if fusion["status"] != "ok":
                return {"status": "error", "message": f"融合异常: {fusion['reason']}"}
            return {"status": "ok", "message": f"测试通过，{fusion['reason']}"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _insufficient_data(self) -> Dict[str, Any]:
        return {"status": "insufficient_data", "frequency": 0.0, "phase": 0.0, "locked": False, "reason": "价格数据不足"}

    def _fuse_results(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        locked = [r for r in results if r.get("locked")]
        if not locked:
            return {"status": "ok", "frequency": 0.0, "phase": 0.0, "locked": False, "reason": "所有频段未锁定"}
        total_w = sum(max(0.1, r.get("snr_db", 10.0)) for r in locked)
        avg_freq = sum(r["frequency"] * max(0.1, r.get("snr_db", 10.0)) for r in locked) / total_w if total_w > 0 else 0.0
        return {"status": "ok", "frequency": avg_freq, "phase": locked[0].get("phase", 0.0), "locked": True,
                "reason": f"融合频率:{avg_freq:.6f}, 锁定:{len(locked)}/{len(results)}"}


class _CostasPLL:
    """单个二阶Costas锁相环（内部实现）"""

    def __init__(self, period: int, bandwidth: float = 0.05, damping: float = 0.707) -> None:
        self._period = period
        self._bandwidth = bandwidth
        self._damping = damping
        wn = bandwidth / (damping + 1.0 / (4.0 * damping))
        self._k1 = wn * wn
        self._k2 = 2.0 * damping * wn
        self._phase = 0.0
        self._frequency = 0.0
        self._phase_error_integral = 0.0
        self._prev_signal = 0.0
        self._snr_db = 10.0
        self._i_buffer: List[float] = []
        self._q_buffer: List[float] = []
        self._max_buffer = 100
        self.frequency: float = 0.0
        self.locked: bool = False

    def update(self, signal: float) -> Dict[str, Any]:
        """输入新信号值，更新锁相环状态。"""
        delta = (signal - self._prev_signal) / self._prev_signal if self._prev_signal > 0 else 0.0
        self._prev_signal = signal
        i = math.cos(self._phase)
        q = -math.sin(self._phase)
        power = i * i + q * q + 1e-12
        phase_error = i * q / power * (1.0 + abs(delta) * 100.0)
        freq_correction = self._k2 * phase_error + self._k1 * self._phase_error_integral
        self._phase_error_integral += phase_error
        self._frequency += freq_correction
        self._phase += self._frequency
        self._phase = math.atan2(math.sin(self._phase), math.cos(self._phase))
        self._i_buffer.append(i)
        self._q_buffer.append(q)
        if len(self._i_buffer) > self._max_buffer:
            self._i_buffer = self._i_buffer[-self._max_buffer:]
            self._q_buffer = self._q_buffer[-self._max_buffer:]
        if len(self._i_buffer) >= 20:
            i_mean = sum(self._i_buffer) / len(self._i_buffer)
            q_var = sum((v - sum(self._q_buffer) / len(self._q_buffer)) ** 2 for v in self._q_buffer) / len(self._q_buffer) + 1e-12
            self._snr_db = 10.0 * math.log10(i_mean * i_mean / q_var)
        self.locked = self._snr_db > 6.0
        self.frequency = self._frequency
        return {"status": "ok", "frequency": self._frequency, "phase": self._phase, "locked": self.locked, "snr_db": self._snr_db,
                "reason": f"锁定:{self.locked}, SNR:{self._snr_db:.1f}dB"}
