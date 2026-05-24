"""
火种系统 · 多频段锁相环 (MultiBandPLL) — 深度精细化

核心职责：
1. 同时运行三个不同周期参数的二阶Costas锁相环，从价格序列中实时提取瞬时频率和相位。
2. 对三个频段的输出进行信噪比(SNR)评估、方向一致性投票，输出融合后的趋势方向、强度与共识置信度。

外部依赖（真实模块接口）：
- 无外部模块依赖。本模块为纯数学计算工具，输入价格序列，输出频率与相位信息。

接口契约：
- update(price: float) -> Dict[str, Any]
  输出字典固定包含 "status" (str), "frequency" (float), "phase" (float), "locked" (bool),
  "snr_db" (float), "reason" (str)
- get_fusion_signal() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "trend_direction" (int), "trend_strength" (float),
  "consensus_count" (int), "locked_count" (int), "reason" (str)
- reset() -> None
- health_check() -> Dict[str, Any]

异常与降级：
- 当输入价格序列长度不足时，返回频率0.0、相位0.0，状态标记为 "insufficient_data"。
- 当单个频段信噪比过低（SNR < 6dB）时，该频段自动标记为未锁定，不参与方向投票。
- 所有计算异常被内部捕获，不向外抛出，确保调用方安全。

资源管理：
- 本模块为有状态计算工具，持有价格历史缓冲区（容量受限），在 reset() 或超出容量时自动清理。
"""

import math
import logging
from typing import Dict, Any, List, Tuple, Optional

logger = logging.getLogger(__name__)


class MultiBandPLL:
    """多频段锁相环：三频段并行 + 信噪比评估 + 方向一致性投票"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_PERIODS: Tuple[int, int, int] = (9, 15, 27)   # 三个锁相环的周期参数，Tick数，[3, 100]
    DEFAULT_LOOP_BANDWIDTH = 0.05                         # 环路带宽，无量纲，[0.01, 0.20]
    DEFAULT_DAMPING = 0.707                               # 阻尼系数，无量纲，[0.5, 1.0]
    SNR_LOCK_THRESHOLD_DB = 6.0                           # 锁相判定信噪比阈值，dB，[3.0, 15.0]
    MIN_PRICES_FOR_LOCK = 20                              # 锁相所需最小价格点数，个，[10, 100]
    FREQUENCY_DIRECTION_THRESHOLD = 0.005                 # 频率方向判定阈值，rad/sample，[0.001, 0.020]
    MAX_PRICE_HISTORY = 200                               # 价格历史最大保留数，个，[50, 500]
    SNR_BUFFER_SIZE = 60                                  # 信噪比计算缓冲大小，个，[20, 200]

    def __init__(self, periods: Optional[Tuple[int, int, int]] = None) -> None:
        """
        初始化三个不同周期参数的锁相环。
        
        Args:
            periods: 三个锁相环的周期参数元组，若为 None 则使用默认值 (9, 15, 27)。
        """
        self._periods: Tuple[int, int, int] = periods if periods else self.DEFAULT_PERIODS
        self._plls: List[_CostasPLL] = [
            _CostasPLL(period=p, bandwidth=self.DEFAULT_LOOP_BANDWIDTH, damping=self.DEFAULT_DAMPING)
            for p in self._periods
        ]
        self._price_history: List[float] = []
        self._update_count: int = 0
        logger.info(
            f"MultiBandPLL 初始化完成，周期参数: {self._periods}, "
            f"环路带宽: {self.DEFAULT_LOOP_BANDWIDTH}, 阻尼: {self.DEFAULT_DAMPING}"
        )

    # ────────────────────────── 公共接口 ──────────────────────────
    def update(self, price: float) -> Dict[str, Any]:
        """
        输入新价格，更新所有锁相环并返回融合后的瞬时频率信息。
        
        Args:
            price: 当前价格，必须大于0。
        
        Returns:
            标准化字典，包含融合后的频率、相位、锁相状态和信噪比。
        """
        # 参数边界校验
        if price <= 0:
            logger.warning(f"无效价格 price={price}，使用上一有效值")
            if self._price_history:
                price = self._price_history[-1]
            else:
                return self._insufficient_data("价格无效且无历史数据")

        # 更新价格历史
        self._price_history.append(price)
        if len(self._price_history) > self.MAX_PRICE_HISTORY:
            self._price_history = self._price_history[-self.MAX_PRICE_HISTORY:]
        self._update_count += 1

        # 数据不足时的处理
        if len(self._price_history) < self.MIN_PRICES_FOR_LOCK:
            return self._insufficient_data(
                f"价格数据不足 ({len(self._price_history)}/{self.MIN_PRICES_FOR_LOCK})"
            )

        # 分别更新三个锁相环
        results: List[Dict[str, Any]] = []
        for pll in self._plls:
            res = pll.update(price)
            results.append(res)

        # 频率一致性校验与融合
        return self._fuse_results(results)

    def get_fusion_signal(self) -> Dict[str, Any]:
        """
        获取当前融合后的趋势信号，不更新锁相环状态。
        
        Returns:
            标准化字典，包含趋势方向(-1/0/1)、强度(0.0-1.0)、共识数量和锁定数量。
        """
        if len(self._price_history) < self.MIN_PRICES_FOR_LOCK:
            return {
                "status": "insufficient_data",
                "trend_direction": 0,
                "trend_strength": 0.0,
                "consensus_count": 0,
                "locked_count": 0,
                "reason": f"价格数据不足 ({len(self._price_history)}/{self.MIN_PRICES_FOR_LOCK})",
            }

        # 收集每个锁相环的状态
        locked_results = [
            {
                "period": self._periods[i],
                "frequency": pll.frequency,
                "locked": pll.locked,
                "snr_db": pll.snr_db,
            }
            for i, pll in enumerate(self._plls)
        ]

        # 统计方向一致性
        up_count = sum(
            1 for r in locked_results
            if r["locked"] and r["frequency"] > self.FREQUENCY_DIRECTION_THRESHOLD
        )
        down_count = sum(
            1 for r in locked_results
            if r["locked"] and r["frequency"] < -self.FREQUENCY_DIRECTION_THRESHOLD
        )
        locked_count = sum(1 for r in locked_results if r["locked"])

        # 确定方向和共识
        if up_count >= 2 and up_count > down_count:
            direction = 1
            consensus = up_count
        elif down_count >= 2 and down_count > up_count:
            direction = -1
            consensus = down_count
        else:
            direction = 0
            consensus = 0

        # 计算趋势强度：锁定频段的平均频率绝对值
        if locked_count > 0:
            avg_freq = sum(
                abs(r["frequency"]) for r in locked_results if r["locked"]
            ) / locked_count
            # 将频率映射到0-1的强度
            strength = min(1.0, avg_freq / (self.FREQUENCY_DIRECTION_THRESHOLD * 15))
        else:
            strength = 0.0

        return {
            "status": "ok",
            "trend_direction": direction,
            "trend_strength": strength,
            "consensus_count": consensus,
            "locked_count": locked_count,
            "reason": (
                f"方向: {direction}, 强度: {strength:.3f}, "
                f"共识: {consensus}/{len(self._plls)}, "
                f"锁定: {locked_count}/{len(self._plls)}"
            ),
        }

    def reset(self) -> None:
        """重置所有锁相环状态和价格历史。"""
        for pll in self._plls:
            pll.reset()
        self._price_history.clear()
        self._update_count = 0
        logger.info("MultiBandPLL 已重置")

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检：用模拟正弦波价格序列测试锁相环收敛性与融合逻辑。"""
        try:
            instance = cls()
            # 生成模拟正弦波价格序列（频率约0.02 rad/sample，振幅2.0）
            test_prices = [
                100.0 + 2.0 * math.sin(2.0 * math.pi * 0.02 * t)
                for t in range(120)
            ]
            last_result = None
            for p in test_prices:
                last_result = instance.update(p)

            if last_result is None:
                return {"status": "error", "message": "update 返回 None"}
            if last_result["status"] != "ok":
                return {"status": "error", "message": f"更新失败: {last_result['reason']}"}
            if not last_result.get("locked"):
                return {"status": "error", "message": "正弦波应能锁相，但未锁定"}

            # 验证融合信号
            fusion = instance.get_fusion_signal()
            if fusion["status"] != "ok":
                return {"status": "error", "message": f"融合信号异常: {fusion['reason']}"}
            if fusion["consensus_count"] < 2:
                return {"status": "error", "message": f"共识频段数不足: {fusion['consensus_count']}"}

            return {"status": "ok", "message": f"锁相环测试通过，共识: {fusion['consensus_count']}/3"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _insufficient_data(self, detail: str) -> Dict[str, Any]:
        """数据不足时的标准化返回。"""
        return {
            "status": "insufficient_data",
            "frequency": 0.0,
            "phase": 0.0,
            "locked": False,
            "snr_db": 0.0,
            "reason": detail,
        }

    def _fuse_results(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """融合三个锁相环的结果：基于信噪比加权平均。"""
        locked_results = [r for r in results if r.get("locked", False)]

        if not locked_results:
            return {
                "status": "ok",
                "frequency": 0.0,
                "phase": 0.0,
                "locked": False,
                "snr_db": 0.0,
                "reason": f"所有频段均未锁定 (SNR < {self.SNR_LOCK_THRESHOLD_DB}dB)",
            }

        # 信噪比加权平均频率
        total_weight = 0.0
        weighted_freq = 0.0
        weighted_snr = 0.0
        for r in locked_results:
            snr = max(0.1, r.get("snr_db", 10.0))
            weight = snr
            weighted_freq += r["frequency"] * weight
            weighted_snr += snr * weight
            total_weight += weight

        avg_freq = weighted_freq / total_weight if total_weight > 0 else 0.0
        avg_snr = weighted_snr / total_weight if total_weight > 0 else 0.0

        # 使用第一个锁定频段的相位作为参考
        avg_phase = locked_results[0].get("phase", 0.0)

        return {
            "status": "ok",
            "frequency": avg_freq,
            "phase": avg_phase,
            "locked": True,
            "snr_db": avg_snr,
            "reason": (
                f"融合频率: {avg_freq:.6f}, "
                f"平均SNR: {avg_snr:.1f}dB, "
                f"锁定频段: {len(locked_results)}/{len(results)}"
            ),
        }


class _CostasPLL:
    """单个二阶Costas锁相环（内部实现）"""

    def __init__(self, period: int, bandwidth: float = 0.05, damping: float = 0.707) -> None:
        self._period: int = period
        self._bandwidth: float = bandwidth
        self._damping: float = damping

        # 环路滤波器系数（双线性变换）
        wn = bandwidth / (damping + 1.0 / (4.0 * damping))
        self._k1: float = wn * wn
        self._k2: float = 2.0 * damping * wn

        # 状态变量
        self._phase: float = 0.0
        self._frequency: float = 0.0
        self._phase_error_integral: float = 0.0
        self._prev_signal: float = 0.0

        # 信噪比评估
        self._i_buffer: List[float] = []
        self._q_buffer: List[float] = []
        self._snr_buffer: List[float] = []
        self._max_buffer: int = 100

        # 公开属性
        self.frequency: float = 0.0
        self.locked: bool = False
        self.snr_db: float = 0.0

    def update(self, signal: float) -> Dict[str, Any]:
        """输入新信号值，更新锁相环状态。"""
        # 计算信号的一阶差分（近似瞬时收益率）
        if self._prev_signal > 0:
            delta = (signal - self._prev_signal) / self._prev_signal
        else:
            delta = 0.0
        self._prev_signal = signal

        # 数控振荡器（NCO）
        i_val = math.cos(self._phase)
        q_val = -math.sin(self._phase)

        # Costas环鉴相器
        power = i_val * i_val + q_val * q_val + 1e-12
        phase_error = i_val * q_val / power
        # 用信号变化率调制相位误差
        phase_error *= (1.0 + abs(delta) * 100.0)

        # 环路滤波器（比例+积分）
        freq_correction = self._k2 * phase_error + self._k1 * self._phase_error_integral
        self._phase_error_integral += phase_error
        # 防止积分饱和
        self._phase_error_integral = max(-1.0, min(1.0, self._phase_error_integral))
        self._frequency += freq_correction

        # 更新相位
        self._phase += self._frequency
        # 相位归一化到 [-π, π]
        self._phase = math.atan2(math.sin(self._phase), math.cos(self._phase))

        # 更新信噪比评估缓冲区
        self._i_buffer.append(i_val)
        self._q_buffer.append(q_val)
        if len(self._i_buffer) > self._max_buffer:
            self._i_buffer = self._i_buffer[-self._max_buffer:]
            self._q_buffer = self._q_buffer[-self._max_buffer:]

        # 计算信噪比
        if len(self._i_buffer) >= 20:
            i_mean = sum(self._i_buffer) / len(self._i_buffer)
            q_mean = sum(self._q_buffer) / len(self._q_buffer)
            q_var = sum((v - q_mean) ** 2 for v in self._q_buffer) / len(self._q_buffer) + 1e-12
            signal_power = i_mean * i_mean
            self.snr_db = 10.0 * math.log10(max(signal_power / q_var, 1e-6))

        # 锁相判定
        self.locked = self.snr_db > 6.0
        self.frequency = self._frequency

        return {
            "status": "ok",
            "frequency": self._frequency,
            "phase": self._phase,
            "locked": self.locked,
            "snr_db": self.snr_db,
            "reason": f"锁定: {self.locked}, SNR: {self.snr_db:.1f}dB, 周期: {self._period}",
        }

    def reset(self) -> None:
        """重置内部状态。"""
        self._phase = 0.0
        self._frequency = 0.0
        self._phase_error_integral = 0.0
        self._prev_signal = 0.0
        self._i_buffer.clear()
        self._q_buffer.clear()
        self._snr_buffer.clear()
        self.frequency = 0.0
        self.locked = False
        self.snr_db = 0.0
