#!/usr/bin/env python3
"""
火种系统 (FireSeed) 感知融合模块
==================================
负责多模态市场感知，将原始数据转化为结构化状态信号：
- 粒子滤波：Heston 随机波动率模型状态估计
- 锁相环 (PLL)：价格序列瞬时频率追踪
- VIB 表征：变分信息瓶颈深度学习订单簿语义
- 跳跃检测：Lee-Mykland 非参数跳跃检验
- 赫斯特指数：趋势/震荡判别
- 市场状态机：趋势/震荡/反转/极端

输出统一的市场感知快照，供评分卡和仲裁器使用。
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.stats import norm

logger = logging.getLogger("fire_seed.perception")


# ======================== 状态数据类 ========================
@dataclass
class ParticleFilterState:
    """粒子滤波估计的波动率状态"""
    v: float = 0.0001              # 当前方差估计
    mu: float = 0.0                # 漂移率估计
    kappa: float = 2.0             # 均值回复速度
    theta: float = 0.0005          # 长期方差
    xi: float = 0.3                # 波动率的波动率
    rho: float = -0.5              # 杠杆效应
    atr: float = 0.0               # 当前 ATR 估计
    ess: int = 5000                # 有效样本量
    hurst_bf: float = 1.0          # 赫斯特指数贝叶斯因子


@dataclass
class PLLState:
    """锁相环状态"""
    frequency: float = 0.0         # 瞬时频率 (rad/sample)
    freq_per_day: float = 0.0      # 年化频率
    phase: float = 0.0             # 当前相位
    locked: bool = False           # 是否锁定
    snr_db: float = 20.0           # 信噪比 (dB)
    phase_error: float = 0.0       # 鉴相误差


@dataclass
class VIBState:
    """VIB 深度学习表征"""
    buy_pressure: float = 0.5
    sell_pressure: float = 0.5
    trend_persistence: float = 0.5
    reversal_risk: float = 0.3
    volatility_regime: float = 0.5
    latent_vector: Optional[np.ndarray] = None


@dataclass
class MarketRegime:
    """市场状态判定"""
    regime: str = "unknown"        # trend / oscillation / reversal / extreme
    ci: float = 50.0               # Choppiness Index
    hurst_bf: float = 1.0          # 赫斯特贝叶斯因子
    is_oscillation: bool = False
    is_extreme: bool = False


# ======================== 感知融合引擎 ========================
class PerceptionFusion:
    """
    多模态感知融合引擎。
    整合粒子滤波、锁相环、VIB 推理、跳跃检测、市场状态判定。

    使用方法：
    1. 每 Tick 调用 update_pll / update_particle_filter / update_vib
    2. 通过 get_state() 获取融合后的 PerceptState
    """

    def __init__(self, config: Dict = None):
        self.config = config or {}

        # 粒子滤波参数
        self.n_particles = self.config.get("perception.n_particles", 3000)
        self._pf = HestonParticleFilter(n_particles=self.n_particles)

        # 锁相环
        self._pll = CostasPLL(bandwidth=self.config.get("perception.pll_bandwidth", 0.05),
                              damping=self.config.get("perception.pll_damping", 0.707))

        # VIB 推理（ONNX Runtime）
        self._vib = VIBSession(model_path=self.config.get("perception.vib_model_path", ""))

        # 跳跃检测
        self._jump_detector = JumpDetector(k=self.config.get("perception.jump_k", 4.0))

        # 状态历史（用于自省）
        self._price_history: deque = deque(maxlen=200)
        self._returns: deque = deque(maxlen=200)

        # 冻结标志
        self._frozen = False
        self._freeze_reason = ""

        # 最近的状态快照
        self._last_pf = ParticleFilterState()
        self._last_pll = PLLState()
        self._last_vib = VIBState()
        self._last_regime = MarketRegime()

        logger.info(f"感知融合引擎初始化完成，粒子数: {self.n_particles}")

    # ======================== 冻结/解冻 ========================
    def freeze(self, reason: str = "") -> None:
        self._frozen = True
        self._freeze_reason = reason
        logger.warning(f"感知层冻结: {reason}")

    def unfreeze(self) -> None:
        self._frozen = False
        self._freeze_reason = ""
        logger.info("感知层解冻")

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    # ======================== 核心更新方法 ========================
    def update_pll(self, price: float) -> PLLState:
        """更新锁相环状态"""
        self._price_history.append(price)
        if self._frozen:
            return self._last_pll
        result = self._pll.update(price)
        self._last_pll = PLLState(
            frequency=result.get("frequency", 0.0),
            freq_per_day=result.get("freq_per_day", 0.0),
            phase=result.get("phase", 0.0),
            locked=result.get("locked", False),
            snr_db=result.get("snr_db", 20.0),
            phase_error=result.get("phase_error", 0.0)
        )
        return self._last_pll

    def update_particle_filter(self, kline: Dict) -> ParticleFilterState:
        """更新粒子滤波状态"""
        if self._frozen:
            return self._last_pf

        close = kline.get("close", 0)
        high = kline.get("high", 0)
        low = kline.get("low", 0)
        prev_close = getattr(self, "_pf_prev_close", close)
        self._pf_prev_close = close

        if prev_close and close:
            log_ret = np.log(close / prev_close)
            if high > low:
                parkinson_var = (np.log(high / low) ** 2) / (4 * np.log(2))
            else:
                parkinson_var = 0.0001
            self._pf.update(log_ret, parkinson_var)
            self._returns.append(log_ret)

        pf_state = self._pf.get_state()
        atr = np.sqrt(pf_state.v * 1440) * close if close else 0.0  # 日化ATR

        self._last_pf = ParticleFilterState(
            v=pf_state.v,
            mu=pf_state.mu,
            kappa=pf_state.kappa,
            theta=pf_state.theta,
            xi=pf_state.xi,
            rho=pf_state.rho,
            atr=atr,
            ess=pf_state.ess,
            hurst_bf=self._estimate_hurst_bf()
        )
        return self._last_pf

    def update_vib(self, orderbook_vector: Optional[List[float]]) -> VIBState:
        """更新 VIB 订单簿深层语义"""
        if self._frozen or not orderbook_vector:
            return self._last_vib
        result = self._vib.infer(orderbook_vector)
        if result:
            self._last_vib = VIBState(
                buy_pressure=result.get("buy_pressure", 0.5),
                sell_pressure=result.get("sell_pressure", 0.5),
                trend_persistence=result.get("trend_persistence", 0.5),
                reversal_risk=result.get("reversal_risk", 0.3),
                volatility_regime=result.get("volatility_regime", 0.5),
                latent_vector=result.get("latent_vector")
            )
        return self._last_vib

    # ======================== 跳跃检测 ========================
    def check_jump(self, returns: List[float]) -> Tuple[bool, float]:
        """检测是否存在显著跳跃"""
        if self._frozen:
            return False, 0.0
        return self._jump_detector.detect(returns)

    # ======================== 综合状态 ========================
    def get_full_state(self) -> Dict:
        """返回完整感知状态字典"""
        return {
            "pll": self._last_pll,
            "pf": self._last_pf,
            "vib": self._last_vib,
            "regime": self._last_regime,
            "frozen": self._frozen,
        }

    def determine_regime(self) -> MarketRegime:
        """综合判断当前市场状态"""
        pll = self._last_pll
        pf = self._last_pf
        vib = self._last_vib

        # 震荡判定：基于CI和锁相环未锁定
        ci = self._calc_choppiness_index()
        is_osc = ci > 55 and not pll.locked and vib.volatility_regime > 0.5

        # 极端判定：基于赫斯特贝叶斯因子极低或波动率异常
        is_extreme = pf.hurst_bf < 0.3 or vib.reversal_risk > 0.8

        # 趋势判定
        if pll.locked and abs(pll.frequency) > 0.02 and pf.hurst_bf > 10:
            regime = "trend"
        elif is_extreme:
            regime = "extreme"
        elif is_osc:
            regime = "oscillation"
        elif vib.reversal_risk > 0.7:
            regime = "reversal"
        else:
            regime = "neutral"

        self._last_regime = MarketRegime(
            regime=regime,
            ci=ci,
            hurst_bf=pf.hurst_bf,
            is_oscillation=is_osc,
            is_extreme=is_extreme
        )
        return self._last_regime

    # ======================== 内部辅助 ========================
    def _estimate_hurst_bf(self) -> float:
        """简化赫斯特指数贝叶斯因子估算"""
        returns = list(self._returns)[-64:]
        if len(returns) < 32:
            return 1.0
        # 简单 R/S 分析替代
        n = len(returns)
        cumulative = np.cumsum(returns - np.mean(returns))
        r = np.max(cumulative) - np.min(cumulative)
        s = np.std(returns) + 1e-10
        rs = r / s
        h_est = np.log(rs) / np.log(n)
        # 简化的贝叶斯因子
        return np.exp(abs(h_est - 0.5) * 5)

    def _calc_choppiness_index(self, period: int = 14) -> float:
        """计算 Choppiness Index"""
        prices = list(self._price_history)[-period:]
        if len(prices) < period:
            return 50.0
        high = max(prices)
        low = min(prices)
        if high == low:
            return 50.0
        tr_sum = sum(abs(prices[i] - prices[i-1]) for i in range(1, len(prices)))
        ci = 100 * np.log10(tr_sum / (high - low)) / np.log10(period)
        return np.clip(ci, 0, 100)


# ======================== Heston 粒子滤波 ========================
class HestonParticleFilter:
    """基于粒子滤波的 Heston 随机波动率模型在线估计"""

    def __init__(self, n_particles: int = 3000):
        self.N = min(n_particles, 5000)
        # 初始化参数粒子
        self.kappa = np.random.uniform(1.0, 5.0, self.N)
        self.theta = np.random.uniform(0.0001, 0.0025, self.N)
        self.xi = np.random.uniform(0.1, 0.8, self.N)
        self.rho = np.random.uniform(-0.9, -0.3, self.N)
        self.mu = np.random.uniform(-0.001, 0.001, self.N)
        self.v = np.random.uniform(0.0001, 0.001, self.N)
        self.weights = np.ones(self.N) / self.N

    def update(self, log_ret: float, parkinson_var: float):
        """基于观测更新粒子权重并重采样"""
        dt = 1 / 1440
        # 状态转移
        dW_v = np.random.normal(0, np.sqrt(dt), self.N)
        self.v = np.maximum(1e-8, self.v + self.kappa * (self.theta - self.v) * dt
                            + self.xi * np.sqrt(np.maximum(0, self.v)) * dW_v)
        # 计算粒子权重
        price_std = np.sqrt(self.v * dt)
        price_likelihood = norm.pdf(log_ret, loc=self.mu * dt, scale=price_std)
        var_likelihood = norm.pdf(np.sqrt(parkinson_var), loc=np.sqrt(self.v), scale=0.1 * np.sqrt(self.v))
        self.weights = price_likelihood * var_likelihood + 1e-300
        self.weights /= self.weights.sum()
        # 有效样本量
        ess = 1 / np.sum(self.weights ** 2)
        if ess < self.N / 2:
            self._resample()
        self._ess = ess

    def _resample(self):
        indices = np.random.choice(self.N, self.N, p=self.weights, replace=True)
        self.kappa, self.theta, self.xi = self.kappa[indices], self.theta[indices], self.xi[indices]
        self.rho, self.mu, self.v = self.rho[indices], self.mu[indices], self.v[indices]
        # 注入正则化噪声
        self.v += np.random.uniform(-1e-6, 1e-6, self.N)
        self.weights = np.ones(self.N) / self.N

    def get_state(self) -> ParticleFilterState:
        return ParticleFilterState(
            v=np.average(self.v, weights=self.weights),
            mu=np.average(self.mu, weights=self.weights),
            kappa=np.average(self.kappa, weights=self.weights),
            theta=np.average(self.theta, weights=self.weights),
            xi=np.average(self.xi, weights=self.weights),
            rho=np.average(self.rho, weights=self.weights),
            atr=np.sqrt(max(0, np.average(self.v, weights=self.weights))),
            ess=int(self._ess),
        )


# ======================== Costas 锁相环 ========================
class CostasPLL:
    """二阶 Costas 环，追踪价格序列的瞬时频率"""

    def __init__(self, bandwidth: float = 0.05, damping: float = 0.707):
        wn = bandwidth / (damping + 1 / (4 * damping))
        self.k1 = wn ** 2
        self.k2 = 2 * damping * wn
        self.phase = 0.0
        self.frequency = 0.0
        self.phase_error_integral = 0.0
        self.prev_I, self.prev_Q = 0.0, 0.0
        self.price_history: deque = deque(maxlen=64)

    def update(self, price: float) -> Dict:
        self.price_history.append(price)
        if len(self.price_history) < 3:
            return {"frequency": 0, "locked": False, "phase": 0}

        log_prices = np.log(list(self.price_history)[-32:]) if len(self.price_history) >= 32 else np.log(list(self.price_history))
        signal = log_prices[-1] - log_prices[-2] if len(log_prices) >= 2 else 0

        I = np.cos(self.phase)
        Q = -np.sin(self.phase)
        phase_error = I * Q / (I**2 + Q**2 + 1e-10)

        self.phase_error_integral += self.k1 * phase_error
        self.frequency += self.k2 * phase_error + self.phase_error_integral
        self.phase += self.frequency
        self.phase = ((self.phase + np.pi) % (2 * np.pi)) - np.pi

        locked = abs(np.mean([self.prev_I, I])) > 0.1
        snr_db = 10 * np.log10(I**2 / (np.var([self.prev_Q, Q]) + 1e-10)) if np.var([self.prev_Q, Q]) > 0 else 20

        self.prev_I, self.prev_Q = I, Q
        return {
            "frequency": self.frequency,
            "freq_per_day": self.frequency * 1440,
            "phase": self.phase,
            "locked": locked,
            "snr_db": snr_db,
            "phase_error": abs(phase_error)
        }

    def reset(self):
        self.phase = 0.0
        self.frequency = 0.0
        self.phase_error_integral = 0.0
        self.prev_I, self.prev_Q = 0.0, 0.0


# ======================== VIB 推理会话 ========================
class VIBSession:
    """ONNX Runtime VIB 模型推理会话"""

    def __init__(self, model_path: str = ""):
        self._session = None
        if model_path:
            try:
                import onnxruntime as ort
                self._session = ort.InferenceSession(model_path)
                logger.info(f"VIB 模型加载成功: {model_path}")
            except Exception as e:
                logger.warning(f"VIB 模型加载失败，将返回默认值: {e}")

    def infer(self, input_vector: List[float]) -> Optional[Dict]:
        if not self._session or not input_vector:
            # 返回基于输入的模拟值
            return {
                "buy_pressure": np.clip(np.mean(input_vector[:10]) * 2, 0, 1),
                "sell_pressure": np.clip(np.mean(input_vector[10:20]) * 2, 0, 1),
                "trend_persistence": 0.5,
                "reversal_risk": 0.3,
                "volatility_regime": 0.5,
            }
        try:
            inp = np.array(input_vector, dtype=np.float32).reshape(1, -1)
            outputs = self._session.run(None, {"input": inp})
            return {
                "buy_pressure": float(outputs[0][0, 0]),
                "sell_pressure": float(outputs[0][0, 1]),
                "trend_persistence": float(outputs[0][0, 2]),
                "reversal_risk": float(outputs[0][0, 3]),
                "volatility_regime": float(outputs[0][0, 4]),
            }
        except Exception as e:
            logger.error(f"VIB 推理失败: {e}")
            return None


# ======================== Lee-Mykland 跳跃检测 ========================
class JumpDetector:
    """基于 Lee-Mykland 非参数统计的跳跃检测"""

    def __init__(self, k: float = 4.0, window: int = 16):
        self.k = k
        self.window = window

    def detect(self, returns: List[float]) -> Tuple[bool, float]:
        if len(returns) < self.window + 2:
            return False, 0.0
        # 计算双幂次变差
        returns = np.array(returns[-self.window:])
        bipower = np.mean(np.abs(returns[1:]) * np.abs(returns[:-1]))
        if bipower == 0:
            return False, 0.0
        realized_vol = np.std(returns) * np.sqrt(len(returns))
        stat = realized_vol / (bipower * 0.7979)
        return stat > self.k, stat
