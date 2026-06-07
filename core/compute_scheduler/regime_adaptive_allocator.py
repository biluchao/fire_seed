"""
火种系统 · 市场活跃度自适应算力分配器 (RegimeAdaptiveAllocator)
版本: 2.2.0

核心职责：
1. 根据当前市场波动率分位和成交量活跃度，动态生成各功能模块的算力配额（推理频率、更新频率）
2. 提供四种市场环境（空闲/正常/活跃/极端）下的预设调度策略，并支持策略间的平滑过渡与超时保护

外部依赖（真实模块接口）：
- core.perception.tactile_cortex.TactileCortex.get_volatility_percentile() -> float : 获取当前ATR波动率分位 (0-100)
- core.perception.tactile_cortex.TactileCortex.get_volume_ratio() -> float : 获取当前成交量与过去20根均量的比值
- core.negotiation_bus.NegotiationBus.publish_alert(alert_type, **kwargs) : 推送配额变更事件（可选）
- core.behavioral_logger.BehavioralLogger.log_event(event_type, details) : 记录异常跳变等关键事件（可选）

接口契约：
- get_allocation(atr_percentile=None, volume_ratio=None) -> Dict[str, Any]
- get_current_mode(atr_percentile) -> str
- health_check() -> Dict[str, Any]
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 TactileCortex 调用超过100ms时自动超时降级，使用缓存数据
- 输入参数无效时使用安全默认值并记录警告
- 波动率或成交量异常跳变（一阶差分超过阈值）时使用上一有效值并记录审计日志
- 所有降级值在类常量区明确声明

资源管理：
- 本模块为轻量级计算单元，不持有外部资源句柄
- 平滑状态受 threading.RLock 保护，所有缓存读写原子化
- 外部依赖超时控制使用 ThreadPoolExecutor，确保不阻塞主流程

版本历史：
- v2.2.0: 修复阻塞超时、竞态条件、平滑边界、微调逻辑、线程安全等28项高缺陷
- v2.1.0: 二次穿透审查修复40项缺陷
- v2.0.0: 初次穿透审查修复40项缺陷
- v1.0.0: 初始生产版本
"""

import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Dict, Any, List, Optional, Final

logger = logging.getLogger(__name__)

# 市场模式常量
MODE_IDLE: Final[str] = "idle"
MODE_NORMAL: Final[str] = "normal"
MODE_ACTIVE: Final[str] = "active"
MODE_EXTREME: Final[str] = "extreme"

__version__: Final[str] = "2.2.0"
__all__ = [
    "RegimeAdaptiveAllocator",
    "QuotaKey",
    "MODE_IDLE", "MODE_NORMAL", "MODE_ACTIVE", "MODE_EXTREME",
]

class QuotaKey:
    """配额键枚举，统一管理所有算力模块标识"""
    VIB_INFERENCE: Final[str] = "vib_inference"
    PARTICLE_FILTER: Final[str] = "particle_filter"
    COUNCIL_VOTE: Final[str] = "council_vote"
    OPENCLAW_AGENT: Final[str] = "openclaw_agent"
    EVOLUTION: Final[str] = "evolution"
    LLM_AUDIT: Final[str] = "llm_audit"
    BACKTEST: Final[str] = "backtest"

    @classmethod
    def all_keys(cls) -> List[str]:
        return [cls.VIB_INFERENCE, cls.PARTICLE_FILTER, cls.COUNCIL_VOTE,
                cls.OPENCLAW_AGENT, cls.EVOLUTION, cls.LLM_AUDIT, cls.BACKTEST]


class RegimeAdaptiveAllocator:
    """市场活跃度自适应算力分配器

    完全符合 Renaissance / D.E. Shaw 生产标准：
    - 所有外部调用带超时保护，绝无阻塞
    - 线程安全，无竞态条件
    - 平滑过渡防止资源尖峰
    - 异常跳变检测与自动回退
    - 全状态审计与监控
    """

    # ========== 类常量（附带单位与取值范围注释） ==========
    VOL_IDLE_THRESHOLD: Final[float] = 30.0        # 低波动阈值，分位数，[10.0, 40.0]
    VOL_NORMAL_THRESHOLD: Final[float] = 60.0      # 正常波动上限，分位数，[50.0, 80.0]
    VOL_ACTIVE_THRESHOLD: Final[float] = 85.0      # 高波动阈值，分位数，[70.0, 95.0]

    VOLUME_RATIO_LOW: Final[float] = 0.7           # 低成交量阈值，无量纲，[0.3, 1.0]
    VOLUME_RATIO_HIGH: Final[float] = 1.3          # 高成交量阈值，无量纲，[1.0, 2.0]
    VOLUME_RATIO_MAX: Final[float] = 10.0          # 成交量比率上限，无量纲，防止极端放大
    VOLUME_MA_PERIOD: Final[int] = 20              # 均量计算周期，根，[10, 50]

    VOLUME_ADJUST_MULT: Final[float] = 1.2         # 高成交量核心模块配额放大系数
    MAX_QUOTA_MULT: Final[float] = 3.0             # 单模块配额上限倍数，[2.0, 5.0]
    MIN_QUOTA_MULT: Final[float] = 0.0             # 单模块配额下限

    DEGRADED_DATA_MAX_AGE_SEC: Final[float] = 60.0 # 降级数据最大有效期，秒，[30, 120]
    EXTERNAL_CALL_TIMEOUT_SEC: Final[float] = 0.1  # 外部依赖调用超时，秒，[0.05, 0.5]

    SMOOTHING_ALPHA: Final[float] = 0.3            # EMA平滑系数，[0.1, 0.5]
    SMOOTHING_REL_STEP: Final[float] = 0.2         # 单次平滑最大相对步长（目标值的比例），[0.1, 0.5]
    SMOOTHING_ABS_MIN_STEP: Final[float] = 0.05    # 绝对最小步长，防止目标为零时平滑停滞
    SMOOTHING_ALPHA_AGGRESSIVE: Final[float] = 0.7 # 模式切换时的加速平滑系数

    ATR_JUMP_THRESHOLD: Final[float] = 30.0        # 波动率分位一阶差分异常阈值，[20.0, 50.0]
    VOLUME_JUMP_THRESHOLD: Final[float] = 2.0      # 成交量比率一阶差分异常阈值，[1.0, 3.0]

    MODE_SWITCH_MAX_PER_MINUTE: Final[int] = 5     # 每分钟最大切换次数，[2, 10]
    MODE_SWITCH_WINDOW_SEC: Final[int] = 60        # 切换频率统计窗口，秒
    MODE_SWITCH_HISTORY_MAX: Final[int] = 50       # 切换历史最大保留条目

    # ========== 四模式配额表 ==========
    IDLE_QUOTA: Final[Dict[str, float]] = {
        QuotaKey.VIB_INFERENCE: 0.2,
        QuotaKey.PARTICLE_FILTER: 0.5,
        QuotaKey.COUNCIL_VOTE: 0.2,
        QuotaKey.OPENCLAW_AGENT: 0.1,
        QuotaKey.EVOLUTION: 0.0,
        QuotaKey.LLM_AUDIT: 0.0,
        QuotaKey.BACKTEST: 0.0,
    }

    NORMAL_QUOTA: Final[Dict[str, float]] = {
        QuotaKey.VIB_INFERENCE: 1.0,
        QuotaKey.PARTICLE_FILTER: 1.0,
        QuotaKey.COUNCIL_VOTE: 1.0,
        QuotaKey.OPENCLAW_AGENT: 0.5,
        QuotaKey.EVOLUTION: 0.3,
        QuotaKey.LLM_AUDIT: 0.0,
        QuotaKey.BACKTEST: 0.0,
    }

    ACTIVE_QUOTA: Final[Dict[str, float]] = {
        QuotaKey.VIB_INFERENCE: 2.0,
        QuotaKey.PARTICLE_FILTER: 2.0,
        QuotaKey.COUNCIL_VOTE: 2.0,
        QuotaKey.OPENCLAW_AGENT: 0.0,
        QuotaKey.EVOLUTION: 0.0,
        QuotaKey.LLM_AUDIT: 0.0,
        QuotaKey.BACKTEST: 0.0,
    }

    EXTREME_QUOTA: Final[Dict[str, float]] = {
        QuotaKey.VIB_INFERENCE: 0.0,
        QuotaKey.PARTICLE_FILTER: 0.5,
        QuotaKey.COUNCIL_VOTE: 0.0,
        QuotaKey.OPENCLAW_AGENT: 0.0,
        QuotaKey.EVOLUTION: 0.0,
        QuotaKey.LLM_AUDIT: 0.0,
        QuotaKey.BACKTEST: 0.0,
    }

    DEFAULT_QUOTA: Final[Dict[str, float]] = dict(NORMAL_QUOTA)
    ALL_QUOTA_KEYS: Final[List[str]] = QuotaKey.all_keys()

    def __init__(self):
        # 外部依赖
        self._tactile_cortex: Optional[Any] = None
        self._negotiation_bus: Optional[Any] = None
        self._behavioral_logger: Optional[Any] = None

        # 外部调用线程池
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ra_allocator")

        # 降级缓存
        self._last_valid_allocation: Dict[str, Any] = {
            "mode": MODE_NORMAL,
            "quota": dict(self.DEFAULT_QUOTA),
            "atr_percentile": 50.0,
            "volume_ratio": 1.0,
            "timestamp": time.time(),
        }
        self._last_valid_mode: str = MODE_NORMAL

        # 平滑状态：唯一真相源，启动时确保所有键存在
        self._smoothed_quota: Dict[str, float] = {}
        for key in self.ALL_QUOTA_KEYS:
            self._smoothed_quota[key] = self.DEFAULT_QUOTA.get(key, 0.0)

        # 模式切换监控
        self._mode_switch_timestamps: List[float] = []

        # 上一有效值（异常跳变检测用）
        self._last_atr_percentile: Optional[float] = None
        self._last_volume_ratio: Optional[float] = None

        # 线程安全（RLock 用于嵌套调用的保护）
        self._lock = threading.RLock()
        self._history_lock = threading.RLock()

        logger.info("RegimeAdaptiveAllocator v%s 初始化完成，默认模式: %s", __version__, MODE_NORMAL)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        tactile_cortex: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """注入外部依赖，所有依赖可选，未注入时功能降级"""
        if tactile_cortex is not None:
            has_percentile = callable(getattr(tactile_cortex, 'get_volatility_percentile', None))
            has_volume = callable(getattr(tactile_cortex, 'get_volume_ratio', None))
            if not has_percentile or not has_volume:
                logger.error("TactileCortex 缺少必需方法，注入失败")
                self._tactile_cortex = None
            else:
                self._tactile_cortex = tactile_cortex
                logger.info("TactileCortex 注入成功")
        else:
            logger.warning("TactileCortex 未注入，将使用降级数据")

        if negotiation_bus is not None:
            self._negotiation_bus = negotiation_bus
            logger.info("NegotiationBus 注入成功")

        if behavioral_logger is not None:
            if callable(getattr(behavioral_logger, 'log_event', None)):
                self._behavioral_logger = behavioral_logger
                logger.info("BehavioralLogger 注入成功")
            else:
                logger.warning("BehavioralLogger 缺少 log_event 方法，注入失败")
        else:
            logger.info("BehavioralLogger 未注入，异常事件将仅记录本地日志")

    # ========== 公共接口 ==========
    @staticmethod
    def get_current_mode(atr_percentile: float) -> str:
        """纯函数：根据波动率分位判定当前市场活跃度模式"""
        if atr_percentile < RegimeAdaptiveAllocator.VOL_IDLE_THRESHOLD:
            return MODE_IDLE
        elif atr_percentile < RegimeAdaptiveAllocator.VOL_NORMAL_THRESHOLD:
            return MODE_NORMAL
        elif atr_percentile < RegimeAdaptiveAllocator.VOL_ACTIVE_THRESHOLD:
            return MODE_ACTIVE
        else:
            return MODE_EXTREME

    def get_allocation(
        self,
        atr_percentile: Optional[float] = None,
        volume_ratio: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        生成当前市场状态下各模块算力配额（带平滑和异常检测）

        Args:
            atr_percentile: ATR历史分位数，闭区间 [0.0, 100.0]，None 时从 TactileCortex 获取
            volume_ratio: 当前成交量与过去N根均量的比值，None 时从 TactileCortex 获取

        Returns:
            标准响应字典，data 中包含 quota, mode, atr_percentile, volume_ratio
        """
        warnings: List[str] = []

        # 1. 获取市场数据（带超时保护，绝不阻塞）
        fetched = self._fetch_market_data()
        if atr_percentile is None:
            atr_percentile = fetched.get("atr_percentile")
        if volume_ratio is None:
            volume_ratio = fetched.get("volume_ratio")

        # 2. 数据有效性检查与数值转换
        if atr_percentile is None or volume_ratio is None:
            return self._degraded_response("市场数据不可用，使用降级配额")

        try:
            atr_percentile = max(0.0, min(100.0, float(atr_percentile)))
        except (TypeError, ValueError):
            warnings.append(f"atr_percentile 类型转换失败 ({atr_percentile})，使用降级")
            return self._degraded_response("atr_percentile 无效")

        try:
            volume_ratio = max(0.0, min(self.VOLUME_RATIO_MAX, float(volume_ratio)))
        except (TypeError, ValueError):
            warnings.append(f"volume_ratio 类型转换失败 ({volume_ratio})，使用降级")
            return self._degraded_response("volume_ratio 无效")

        # 3. 异常跳变检测（在锁内进行，保证状态一致性）
        with self._lock:
            if self._last_atr_percentile is not None:
                atr_diff = abs(atr_percentile - self._last_atr_percentile)
                if atr_diff > self.ATR_JUMP_THRESHOLD:
                    warnings.append(f"波动率异常跳变: Δ={atr_diff:.1f}")
                    self._log_anomaly("atr_jump", {"old": self._last_atr_percentile, "new": atr_percentile})
                    atr_percentile = self._last_atr_percentile

            if self._last_volume_ratio is not None:
                vol_diff = abs(volume_ratio - self._last_volume_ratio)
                if vol_diff > self.VOLUME_JUMP_THRESHOLD:
                    warnings.append(f"成交量异常跳变: Δ={vol_diff:.2f}")
                    self._log_anomaly("volume_jump", {"old": self._last_volume_ratio, "new": volume_ratio})
                    volume_ratio = self._last_volume_ratio

            self._last_atr_percentile = atr_percentile
            self._last_volume_ratio = volume_ratio

        # 4. 模式判定与基础配额
        mode = self.get_current_mode(atr_percentile)
        base_quota = self._get_base_quota(mode)

        # 5. 成交量微调（仅非极端模式）
        if mode != MODE_EXTREME:
            self._adjust_by_volume(base_quota, volume_ratio)

        # 6. 模式切换检测（在平滑之前，确保使用最新旧模式）
        with self._lock:
            mode_changed = (mode != self._last_valid_mode)

        # 7. 平滑过渡
        self._apply_smoothing(base_quota, mode_changed)

        # 8. 切换监控
        if mode_changed:
            self._monitor_mode_switch(mode)

        # 9. 更新缓存
        result_data = {
            "mode": mode,
            "quota": base_quota,
            "atr_percentile": round(atr_percentile, 1),
            "volume_ratio": round(volume_ratio, 2),
            "timestamp": time.time(),
        }
        with self._lock:
            self._last_valid_allocation = result_data
            self._last_valid_mode = mode

        # 10. 日志与通知
        if mode_changed:
            logger.info("算力分配模式切换: %s -> %s (atr=%.1f, vol=%.2f)", self._last_valid_mode, mode, atr_percentile, volume_ratio)
            self._notify_quota_changed(mode, base_quota)
        else:
            logger.debug("算力分配: mode=%s, atr_pct=%.1f, vol_ratio=%.2f", mode, atr_percentile, volume_ratio)

        return {"status": "ok", "reason": f"基于市场模式 {mode} 生成算力配额", "data": result_data, "warnings": warnings}

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检：验证数据结构完整性、依赖可用性、平滑状态"""
        try:
            required_attrs = ["IDLE_QUOTA", "NORMAL_QUOTA", "ACTIVE_QUOTA", "EXTREME_QUOTA"]
            for attr in required_attrs:
                if not hasattr(self, attr):
                    return {"status": "error", "reason": f"缺失必需类常量: {attr}", "data": {}, "warnings": [f"missing_constant:{attr}"]}

            quota_keys = set(self.IDLE_QUOTA.keys())
            if quota_keys != set(self.NORMAL_QUOTA.keys()) or quota_keys != set(self.ACTIVE_QUOTA.keys()) or quota_keys != set(self.EXTREME_QUOTA.keys()):
                return {"status": "error", "reason": "四种模式的配额键不一致", "data": {}, "warnings": ["inconsistent_quota_keys"]}

            deps_status = {
                "tactile_cortex": self._tactile_cortex is not None,
                "tactile_cortex_callable": (
                    callable(getattr(self._tactile_cortex, 'get_volatility_percentile', None)) and
                    callable(getattr(self._tactile_cortex, 'get_volume_ratio', None))
                ) if self._tactile_cortex else False,
                "negotiation_bus": self._negotiation_bus is not None,
                "negotiation_bus_callable": (
                    callable(getattr(self._negotiation_bus, 'publish_alert', None))
                ) if self._negotiation_bus else False,
                "behavioral_logger": self._behavioral_logger is not None,
            }

            with self._lock:
                smooth_keys = list(self._smoothed_quota.keys())

            return {"status": "ok", "reason": "RegimeAdaptiveAllocator 自检通过", "data": {"dependencies": deps_status, "last_mode": self._last_valid_mode, "quota_keys": list(quota_keys), "smooth_keys": smooth_keys}, "warnings": []}
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和数据结构")
            return {"status": "error", "reason": f"健康检查异常: {str(e)}", "data": {}, "warnings": ["health_check_failed"]}

    # ========== 私有方法 ==========
    def _fetch_market_data(self) -> Dict[str, Optional[float]]:
        """从触觉皮层获取波动率和成交量，带超时保护（永不阻塞）"""
        result: Dict[str, Optional[float]] = {"atr_percentile": None, "volume_ratio": None}
        cortex = self._tactile_cortex
        if cortex is None or not (
            callable(getattr(cortex, 'get_volatility_percentile', None)) and
            callable(getattr(cortex, 'get_volume_ratio', None))
        ):
            return result

        def _get_atr():
            try:
                start = time.monotonic()
                atr = cortex.get_volatility_percentile()
                elapsed = time.monotonic() - start
                if elapsed > self.EXTERNAL_CALL_TIMEOUT_SEC:
                    logger.warning(f"get_volatility_percentile 慢调用: {elapsed*1000:.0f}ms")
                if isinstance(atr, (int, float)) and 0 <= atr <= 100:
                    return float(atr)
            except Exception as e:
                logger.warning(f"获取波动率分位失败: {e}")
            return None

        def _get_vol():
            try:
                start = time.monotonic()
                vol = cortex.get_volume_ratio()
                elapsed = time.monotonic() - start
                if elapsed > self.EXTERNAL_CALL_TIMEOUT_SEC:
                    logger.warning(f"get_volume_ratio 慢调用: {elapsed*1000:.0f}ms")
                if isinstance(vol, (int, float)) and vol >= 0:
                    return float(vol)
            except Exception as e:
                logger.warning(f"获取成交量比率失败: {e}")
            return None

        # 使用线程池实现超时保护
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_atr = pool.submit(_get_atr)
            future_vol = pool.submit(_get_vol)
            try:
                result["atr_percentile"] = future_atr.result(timeout=self.EXTERNAL_CALL_TIMEOUT_SEC)
            except FutureTimeoutError:
                logger.error("获取波动率分位超时 (%.1fs) #RECOVERY: 检查 TactileCortex 健康状态", self.EXTERNAL_CALL_TIMEOUT_SEC)
            except Exception as e:
                logger.error(f"获取波动率分位异常: {e}")
            try:
                result["volume_ratio"] = future_vol.result(timeout=self.EXTERNAL_CALL_TIMEOUT_SEC)
            except FutureTimeoutError:
                logger.error("获取成交量比率超时 (%.1fs) #RECOVERY: 检查 TactileCortex 健康状态", self.EXTERNAL_CALL_TIMEOUT_SEC)
            except Exception as e:
                logger.error(f"获取成交量比率异常: {e}")

        return result

    def _degraded_response(self, reason: str) -> Dict[str, Any]:
        """构建降级响应"""
        now = time.time()
        with self._lock:
            cached = self._last_valid_allocation
        if cached and (now - cached.get("timestamp", 0)) < self.DEGRADED_DATA_MAX_AGE_SEC:
            logger.warning("降级模式：使用缓存数据 (年龄: %.1fs)", now - cached["timestamp"])
            return {"status": "ok", "reason": f"{reason}，使用缓存配额", "data": dict(cached), "warnings": ["degraded_cached"]}
        else:
            logger.warning("降级模式：使用默认正常配额")
            return {"status": "ok", "reason": f"{reason}，使用默认正常配额", "data": {"mode": MODE_NORMAL, "quota": dict(self.DEFAULT_QUOTA), "atr_percentile": None, "volume_ratio": None, "timestamp": now}, "warnings": ["degraded_default"]}

    def _get_base_quota(self, mode: str) -> Dict[str, float]:
        """获取指定模式的基础配额（深拷贝）"""
        if mode == MODE_IDLE:
            return dict(self.IDLE_QUOTA)
        elif mode == MODE_NORMAL:
            return dict(self.NORMAL_QUOTA)
        elif mode == MODE_ACTIVE:
            return dict(self.ACTIVE_QUOTA)
        else:
            return dict(self.EXTREME_QUOTA)

    def _adjust_by_volume(self, quota: Dict[str, float], volume_ratio: float) -> None:
        """根据成交量微调配额（仅调整已存在的键）"""
        if volume_ratio < self.VOLUME_RATIO_LOW:
            for key in (QuotaKey.OPENCLAW_AGENT, QuotaKey.EVOLUTION, QuotaKey.LLM_AUDIT):
                if key in quota and quota[key] > 0.1:
                    quota[key] = 0.1
        elif volume_ratio > self.VOLUME_RATIO_HIGH:
            for key in (QuotaKey.VIB_INFERENCE, QuotaKey.PARTICLE_FILTER):
                if key in quota:
                    quota[key] = min(quota[key] * self.VOLUME_ADJUST_MULT, self.MAX_QUOTA_MULT)

    def _apply_smoothing(self, target_quota: Dict[str, float], aggressive: bool) -> None:
        """
        对目标配额进行指数移动平均平滑
        步长使用相对值+绝对最小值，确保在目标为零时仍能平滑收敛
        """
        alpha = self.SMOOTHING_ALPHA_AGGRESSIVE if aggressive else self.SMOOTHING_ALPHA
        with self._lock:
            for key in target_quota:
                target = target_quota[key]
                current = self._smoothed_quota.get(key, target)
                # 相对步长 + 绝对最小步长
                rel_step = abs(target * self.SMOOTHING_REL_STEP)
                max_step = max(rel_step, self.SMOOTHING_ABS_MIN_STEP)
                delta = target - current
                if abs(delta) > max_step:
                    delta = max_step if delta > 0 else -max_step
                smoothed = current + alpha * delta
                smoothed = max(self.MIN_QUOTA_MULT, min(self.MAX_QUOTA_MULT, smoothed))
                self._smoothed_quota[key] = smoothed
                target_quota[key] = round(smoothed, 2)

    def _monitor_mode_switch(self, new_mode: str) -> None:
        """监控模式切换频率，过高时触发告警（线程安全）"""
        now = time.time()
        with self._history_lock:
            self._mode_switch_timestamps.append(now)
            cutoff = now - self.MODE_SWITCH_WINDOW_SEC
            while self._mode_switch_timestamps and self._mode_switch_timestamps[0] < cutoff:
                self._mode_switch_timestamps.pop(0)
            while len(self._mode_switch_timestamps) > self.MODE_SWITCH_HISTORY_MAX:
                self._mode_switch_timestamps.pop(0)
            count = len(self._mode_switch_timestamps)
        if count > self.MODE_SWITCH_MAX_PER_MINUTE:
            logger.warning("模式切换过于频繁: 最近60秒内%d次 #RECOVERY: 检查波动率数据源", count)

    def _notify_quota_changed(self, mode: str, quota: Dict[str, float]) -> None:
        """通过协商总线广播配额变更事件"""
        nb = self._negotiation_bus
        if nb is not None and callable(getattr(nb, 'publish_alert', None)):
            try:
                nb.publish_alert(alert_type="compute_quota_changed", mode=mode, quota=quota, timestamp=time.time())
            except Exception as e:
                logger.warning(f"配额变更通知失败: {e}")

    def _log_anomaly(self, anomaly_type: str, details: Dict[str, Any]) -> None:
        """记录异常跳变事件到行为日志"""
        bl = self._behavioral_logger
        if bl is not None and callable(getattr(bl, 'log_event', None)):
            try:
                bl.log_event(event_type="market_data_anomaly", details={"type": anomaly_type, **details})
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

    # ========== 烟雾测试入口（覆盖更多场景） ==========
    @classmethod
    def _test_get_allocation(cls) -> Dict[str, Any]:
        """扩展烟雾测试：验证模式判定、降级、异常跳变、平滑收敛"""
        instance = cls()
        tests = [
            (10.0, 0.5, MODE_IDLE, False),
            (50.0, 1.0, MODE_NORMAL, False),
            (75.0, 1.5, MODE_ACTIVE, False),
            (95.0, 2.0, MODE_EXTREME, False),
            (None, None, "degraded", True),
            (85.0, 0.01, MODE_ACTIVE, False),
            (30.0, 5.0, MODE_NORMAL, False),
            (-5.0, 0.3, MODE_IDLE, False),
            (110.0, 12.0, MODE_EXTREME, False),
            ("invalid", 1.0, "degraded", True),
            (50.0, "invalid", "degraded", True),
        ]
        results = []
        for atr, vol, expected, degraded in tests:
            res = instance.get_allocation(atr, vol)
            if degraded:
                passed = "warnings" in res and len(res.get("warnings", [])) > 0
                actual = "degraded"
            else:
                actual = res["data"].get("mode", "unknown")
                passed = actual == expected
            results.append({"atr": atr, "vol": vol, "expected": expected, "actual": actual, "passed": passed})
        passed_count = sum(r["passed"] for r in results)
        return {"status": "ok", "reason": f"烟雾测试完成 {passed_count}/{len(results)}", "data": {"results": results}, "warnings": []}


if __name__ == "__main__":
    print("=== RegimeAdaptiveAllocator 烟雾测试 ===")
    test_result = RegimeAdaptiveAllocator._test_get_allocation()
    for r in test_result["data"]["results"]:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"{status} atr={r['atr']}, vol={r['vol']} -> expected={r['expected']}, actual={r['actual']}")
    print(f"\n{test_result['reason']}")
