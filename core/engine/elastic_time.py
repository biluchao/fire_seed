"""
火种系统 · 弹性时间管理器 (ElasticTimeManager)

核心职责：
1. 根据市场活跃度（波动率分位、成交量比率、交易频率）连续映射时间参数的缩放因子，实现高活跃压缩、低活跃拉伸的动态感知
2. 支持宏观事件时间扭曲：在重大事件（如非农、利率决议）前自动压缩时间窗口，事件后基于内部绝对时钟和波动率回归动态恢复

外部依赖（真实模块接口）：
- core.state_machine.StateMachine : 获取当前市场状态（趋势/震荡/反转），辅助判断波动率分位基线
- core.data_feed.MarketDataFeed : 获取当前成交量比率和宏观事件日历

接口契约：
- update_activity(volatility_percentile: float, volume_ratio: float, trade_frequency: float) -> Dict[str, Any]
  更新当前市场活跃度，计算并缓存时间缩放因子
- get_time_scale() -> float
  返回当前时间缩放因子（>1.0 表示拉伸，<1.0 表示压缩）
- scale_parameter(base_value: float, sensitivity_tier: str = "medium") -> Dict[str, Any]
  根据参数类型（high/medium/low）和当前缩放因子调整时间参数。
  当 conflict_risk=True 时，调用方若处于紧缩利润的大盈或极端阶段，应忽略本输出，直接使用 1.0 作为替代缩放因子。
- get_adjusted_lifecycle_windows(base_windows: Dict[str, int]) -> Dict[str, int]
  对五阶段生命周期窗口进行整体缩放
- apply_event_warp(event_type: str, minutes_to_event: float) -> Dict[str, Any]
  事件前调用：根据宏观事件类型和距离事件的时间，激活时间扭曲
- update_post_event_warp(post_event_vol_ratio: Optional[float] = None) -> Dict[str, Any]
  事件后周期性调用：基于内部时钟驱动恢复进度
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 MarketDataFeed 或 StateMachine 不可用时，使用默认活跃度（50分位）运行，并标记 "degraded" 状态
- 参数校验失败时，使用安全默认值（1.0 缩放因子）继续，并记录 WARNING 日志
- 所有降级值在类常量区明确声明

资源管理：
- 本模块不持有任何外部资源句柄，所有计算在方法内完成
- 内部状态（当前缩放因子、活跃度）通过线程锁保护，确保并发安全
- 使用不可变元组封装核心缩放状态，实现原子性读取

设计风险边界（机构级铁律）：
1. 波动率 > 80 分位时，时间压缩被部分抑制，避免在高不确定性中加速交易
2. 高波动 + 低成交量（流动性枯竭）时，压缩效果减半，倾向于防御
3. 极端风险场景下（波动率突破90分位且流动性枯竭，连续确认后），防御性抑制立即生效，无视回滞保护
4. 与 ProfitCompression 模块叠加时，需通过协商总线确保取最保守值。本模块在 scale_parameter 输出中标记 conflict_risk，
   并附带 recommended_action 指引调用方在特定条件下忽略时间加速。
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ElasticTimeManager:
    """弹性时间管理器：让系统时间感知市场活跃度与宏观事件，并内置风控约束"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_ACTIVITY = 50.0                # 默认市场活跃度（百分位），无量纲，[0, 100]
    DEFAULT_BASE_ACTIVITY = 50.0           # 基准活跃度，无量纲，[0, 100]
    DEFAULT_COMPRESSION_RATE = 0.5         # 高活跃压缩速率，无量纲，[0.2, 0.8]
    DEFAULT_TENSION_RATE = 1.0             # 低活跃拉伸速率，无量纲，[0.5, 2.0]
    MIN_TIME_SCALE = 0.4                   # 最大压缩比（最快响应），无量纲，[0.2, 0.6]
    MAX_TIME_SCALE = 2.5                   # 最大拉伸比（最慢节奏），无量纲，[1.5, 4.0]

    # 参数敏感度分级
    SENSITIVITY_TIERS = {
        "high": 0.15,      # 高敏参数（信号年龄半衰期、休眠超时）：活跃度每升10分位，压缩15%
        "medium": 0.08,    # 中敏参数（冷却时间、确认Tick数）：活跃度每升10分位，压缩8%
        "low": 0.03,       # 低敏参数（生命周期窗口）：活跃度每升10分位，压缩3%
    }

    # 事件时间扭曲配置
    EVENT_WARP_PROFILES = {
        "major_macro": {
            "pre_event_advance_minutes": 15,
            "pre_event_scale": 0.6,
            "post_event_recovery_minutes": 5,
        },
        "medium_macro": {
            "pre_event_advance_minutes": 10,
            "pre_event_scale": 0.8,
            "post_event_recovery_minutes": 3,
        },
        "minor_macro": {
            "pre_event_advance_minutes": 5,
            "pre_event_scale": 0.9,
            "post_event_recovery_minutes": 2,
        },
    }

    # 回滞保护参数
    HYSTERESIS_BASE_SECONDS = 30           # 基础回滞稳定时间，秒，[10, 60]
    HYSTERESIS_MIN_SECONDS = 10            # 最小回滞时间（大幅变化时），秒
    HYSTERESIS_MAX_SECONDS = 60            # 最大回滞时间（微小变化时），秒
    HYSTERESIS_DELTA_MAX = 50.0            # 活跃度变化幅度上限，无量纲

    # 机构级风控约束
    VOLATILITY_COMPRESSION_CAP_PERCENTILE = 80  # 波动率超此分位时，压缩被抑制
    LOW_VOLUME_QUALITY_THRESHOLD = 0.7           # 成交量质量低于此值，压缩减半
    EXTREME_RISK_VOLATILITY_THRESHOLD = 90       # 极端风险波动率分位，触发无条件缩放更新
    EXTREME_RISK_CONFIRMATION_SAMPLES = 3        # 极端风险需连续N次采样确认

    EVENT_WARP_MAX_ACTIVE_SECONDS = 1800  # 事件扭曲最大允许激活时长，秒（30分钟）

    def __init__(self):
        # 核心状态：不可变元组 (activity, time_scale)
        self._scale_state: Tuple[float, float] = (self.DEFAULT_ACTIVITY, 1.0)
        # 回滞上下文：不可变元组 (last_change_timestamp, last_activity_delta)
        self._last_scale_change_context: Tuple[float, float] = (0.0, 0.0)
        self._activity_at_last_scale_change = self.DEFAULT_ACTIVITY
        self._extreme_risk_confirmation_count = 0

        # 事件扭曲状态
        self._event_warp_active = False
        self._event_warp_scale = 1.0
        self._event_recovery_start = 0.0
        self._event_recovery_duration = 0.0
        self._event_id: Optional[str] = None
        self._event_type: Optional[str] = None
        self._event_warp_activated_at: float = 0.0  # 扭曲激活的单调时钟时刻

        # 外部依赖
        self._state_machine = None
        self._data_feed = None

        # 线程安全
        self._lock = threading.Lock()

        logger.info("ElasticTimeManager 初始化完成，基准活跃度=%d，压缩速率=%.2f，拉伸速率=%.2f",
                    self.DEFAULT_BASE_ACTIVITY, self.DEFAULT_COMPRESSION_RATE, self.DEFAULT_TENSION_RATE)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        state_machine: Optional[Any] = None,
        data_feed: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选，注入失败时使用默认值降级）"""
        if state_machine is not None:
            self._state_machine = state_machine
            logger.info("StateMachine 注入成功")
        else:
            logger.warning("StateMachine 未注入，使用默认活跃度计算")

        if data_feed is not None:
            self._data_feed = data_feed
            logger.info("MarketDataFeed 注入成功")
        else:
            logger.warning("MarketDataFeed 未注入，事件扭曲功能将仅依赖时钟")

    # ========== 公共接口 ==========
    def update_activity(self, volatility_percentile: float, volume_ratio: float, trade_frequency: float) -> Dict[str, Any]:
        """
        更新当前市场活跃度，并根据回滞保护决定是否重新计算时间缩放因子
        防御性抑制（极端风险且连续确认）立即生效，无视回滞
        """
        # 参数裁剪
        if not (0.0 <= volatility_percentile <= 100.0):
            logger.warning(f"波动率分位异常: {volatility_percentile}，裁剪至有效范围")
            volatility_percentile = max(0.0, min(100.0, volatility_percentile))
        if volume_ratio < 0:
            volume_ratio = 0.0
        if trade_frequency < 0:
            trade_frequency = 0.0

        # 计算新活跃度
        new_activity = (
            volatility_percentile * 0.4 +
            min(volume_ratio * 100, 100.0) * 0.35 +
            min(trade_frequency * 20, 100.0) * 0.25
        )
        new_activity = max(0.0, min(100.0, new_activity))

        # 极端风险连续确认
        raw_extreme = (volatility_percentile > self.EXTREME_RISK_VOLATILITY_THRESHOLD and
                       volume_ratio < self.LOW_VOLUME_QUALITY_THRESHOLD)
        if raw_extreme:
            self._extreme_risk_confirmation_count += 1
        else:
            self._extreme_risk_confirmation_count = 0
        is_confirmed_extreme_risk = (self._extreme_risk_confirmation_count >= self.EXTREME_RISK_CONFIRMATION_SAMPLES)

        now = time.monotonic()
        with self._lock:
            old_activity, old_scale = self._scale_state
            activity_delta = abs(new_activity - old_activity)
            hysteresis = self._calc_hysteresis(activity_delta)

            last_change_time = self._last_scale_change_context[0]
            in_cooldown = (last_change_time > 0 and (now - last_change_time) < hysteresis)
            if in_cooldown and not is_confirmed_extreme_risk:
                self._scale_state = (new_activity, old_scale)
                logger.debug(f"活跃度已更新为 {new_activity:.1f}，缩放因子保持 {old_scale:.2f} (冷却中)")
                return {
                    "status": "ok",
                    "reason": f"活跃度已更新为 {new_activity:.1f}，缩放因子保持 {old_scale:.2f} (冷却中)",
                    "data": {"activity": new_activity, "time_scale": old_scale},
                    "warnings": ["hysteresis_cooling"],
                }

            # 计算新缩放因子（传入波动率分位和成交量用于风控抑制）
            new_scale = self._calculate_scale(new_activity, volatility_percentile, volume_ratio)
            self._scale_state = (new_activity, new_scale)
            self._activity_at_last_scale_change = new_activity
            self._last_scale_change_context = (now, activity_delta)

            if is_confirmed_extreme_risk:
                logger.warning(
                    f"极端风险场景(连续{self.EXTREME_RISK_CONFIRMATION_SAMPLES}次确认)触发立即缩放更新: "
                    f"vol_pct={volatility_percentile:.1f}, vol_ratio={volume_ratio:.2f}, new_scale={new_scale:.2f}"
                )

        return {
            "status": "ok",
            "reason": f"活跃度更新为 {new_activity:.1f}，时间缩放因子 = {new_scale:.2f}",
            "data": {"activity": new_activity, "time_scale": new_scale},
            "warnings": ["extreme_risk_override"] if is_confirmed_extreme_risk else [],
        }

    def get_time_scale(self) -> float:
        """返回当前时间缩放因子（线程安全）"""
        with self._lock:
            return self._scale_state[1]

    def scale_parameter(self, base_value: float, sensitivity_tier: str = "medium") -> Dict[str, Any]:
        """
        根据参数敏感度和当前市场活跃度缩放时间参数
        当 conflict_risk=True 时，调用方若处于紧缩利润的大盈或极端阶段，应忽略本输出，使用 1.0 作为替代
        """
        if sensitivity_tier not in self.SENSITIVITY_TIERS:
            logger.warning(f"无效敏感度等级: {sensitivity_tier}，使用 medium")
            sensitivity_tier = "medium"

        if base_value <= 0:
            logger.warning(f"基础参数值无效: {base_value}，返回原值作为降级")
            return {
                "status": "warning",
                "reason": f"基础参数值无效: {base_value}，已降级为原值",
                "data": {"scaled_value": base_value, "scale_factor": 1.0, "conflict_risk": False, "recommended_action": None},
                "warnings": ["invalid_base_value_degraded"],
            }

        with self._lock:
            activity, scale = self._scale_state
            last_change_time, last_delta = self._last_scale_change_context
            now = time.monotonic()
            if last_change_time > 0:
                hysteresis = self._calc_hysteresis(last_delta)
                if (now - last_change_time) < hysteresis:
                    activity = self._activity_at_last_scale_change

        sensitivity = self.SENSITIVITY_TIERS[sensitivity_tier]
        deviation = (activity - self.DEFAULT_BASE_ACTIVITY) / 10.0
        adjusted_scale = 1.0 - deviation * sensitivity
        final_scale = adjusted_scale * scale
        final_scale = max(self.MIN_TIME_SCALE, min(self.MAX_TIME_SCALE, final_scale))

        scaled_value = max(1, int(base_value * final_scale)) if isinstance(base_value, int) else base_value * final_scale

        conflict_risk = (scale < 1.0)
        recommended_action = None
        if conflict_risk:
            recommended_action = (
                "time_acceleration_conflict_risk: "
                "若当前处于紧缩利润的大盈或极端阶段，应忽略此缩放因子，使用 1.0 作为替代"
            )

        return {
            "status": "ok",
            "reason": f"基于活跃度{activity:.1f}和敏感度{sensitivity_tier}缩放，系数={final_scale:.2f}",
            "data": {
                "scaled_value": scaled_value,
                "scale_factor": final_scale,
                "conflict_risk": conflict_risk,
                "recommended_action": recommended_action,
            },
            "warnings": ["time_acceleration_conflict_risk"] if conflict_risk else [],
        }

    def get_adjusted_lifecycle_windows(self, base_windows: Dict[str, int]) -> Dict[str, Any]:
        """对五阶段生命周期窗口进行整体缩放"""
        if not base_windows:
            logger.warning("基础窗口为空，返回空字典")
            return {
                "status": "error",
                "reason": "基础窗口字典为空",
                "data": {"windows": {}},
                "warnings": ["empty_base_windows"],
            }

        adjusted = {}
        warnings = []
        for stage, seconds in base_windows.items():
            result = self.scale_parameter(seconds, sensitivity_tier="low")
            adjusted[stage] = result["data"]["scaled_value"]
            if result["warnings"]:
                warnings.extend(result["warnings"])

        logger.debug(f"生命周期窗口缩放: base={base_windows}, adjusted={adjusted}")

        return {
            "status": "ok",
            "reason": f"基于当前时间缩放因子 {self.get_time_scale():.2f} 调整窗口",
            "data": {"windows": adjusted},
            "warnings": warnings,
        }

    def apply_event_warp(self, event_type: str, minutes_to_event: float) -> Dict[str, Any]:
        """
        事件前调用：根据宏观事件类型和距离事件的时间，激活时间扭曲
        注意：此方法仅在事件前使用。事件发生后请使用 update_post_event_warp() 驱动恢复
        """
        if event_type not in self.EVENT_WARP_PROFILES:
            logger.warning(f"未知事件类型: {event_type}")
            return {
                "status": "error",
                "reason": f"未知事件类型: {event_type}",
                "data": {"warp_scale": 1.0, "event_active": False},
                "warnings": ["unknown_event_type"],
            }

        profile = self.EVENT_WARP_PROFILES[event_type]
        now = time.monotonic()

        with self._lock:
            if not (minutes_to_event > 0 and minutes_to_event <= profile["pre_event_advance_minutes"]):
                return {
                    "status": "ok",
                    "reason": "不在事件前激活窗口内",
                    "data": {
                        "warp_scale": self._event_warp_scale,
                        "event_active": self._event_warp_active,
                        "estimated_recovery_remaining_seconds": self._get_remaining_recovery(now) if self._event_warp_active else 0,
                    },
                    "warnings": [],
                }

            event_id = f"{event_type}_{now}"

            if not self._event_warp_active:
                # 首次激活
                self._event_warp_active = True
                self._event_warp_scale = profile["pre_event_scale"]
                self._event_recovery_start = now + minutes_to_event * 60
                self._event_recovery_duration = profile["post_event_recovery_minutes"] * 60
                self._event_id = event_id
                self._event_type = event_type
                self._event_warp_activated_at = now
                logger.info(f"事件时间扭曲激活: type={event_type}, event_id={event_id}, scale={self._event_warp_scale:.2f}")
            elif self._event_id == event_id:
                # 同一事件，仅更新扭曲系数
                self._event_warp_scale = profile["pre_event_scale"]
            else:
                # 不同事件，但上一个事件尚未恢复
                remaining = self._get_remaining_recovery(now)
                logger.warning(f"检测到新事件 {event_id}，但上一事件 {self._event_id} 尚未恢复，预计剩余 {remaining:.0f}s")
                return {
                    "status": "warning",
                    "reason": f"上一事件 {self._event_id} 仍在恢复中，拒绝激活新事件",
                    "data": {
                        "warp_scale": self._event_warp_scale,
                        "event_active": True,
                        "estimated_recovery_remaining_seconds": remaining,
                    },
                    "warnings": ["event_conflict_rejected"],
                }

            effective_scale = self._scale_state[1] * self._event_warp_scale

        return {
            "status": "ok",
            "reason": f"事件扭曲激活: type={event_type}, warp={self._event_warp_scale:.2f}",
            "data": {
                "warp_scale": self._event_warp_scale,
                "effective_scale": effective_scale,
                "event_active": self._event_warp_active,
            },
            "warnings": [],
        }

    def update_post_event_warp(self, post_event_vol_ratio: Optional[float] = None) -> Dict[str, Any]:
        """
        事件后周期性调用：基于内部绝对时钟和波动率回归动态计算恢复进度
        此方法应在事件发生后持续调用，无需传入时间参数
        """
        with self._lock:
            if not self._event_warp_active:
                return {
                    "status": "ok",
                    "reason": "无活跃的事件扭曲",
                    "data": {"warp_scale": 1.0, "effective_scale": self._scale_state[1]},
                    "warnings": [],
                }

            now = time.monotonic()
            if now <= self._event_recovery_start:
                # 事件尚未发生（仍在预激活窗口内）
                effective_scale = self._scale_state[1] * self._event_warp_scale
                return {
                    "status": "ok",
                    "reason": "事件尚未发生，保持预激活扭曲",
                    "data": {
                        "warp_scale": self._event_warp_scale,
                        "effective_scale": effective_scale,
                        "event_active": True,
                    },
                    "warnings": [],
                }

            # 事件已发生，使用内部时钟计算恢复进度
            elapsed = now - self._event_recovery_start
            recovery_duration = self._event_recovery_duration

            # 根据事件后波动率动态延长恢复期
            if post_event_vol_ratio is not None and post_event_vol_ratio > 1.5:
                recovery_duration *= min(post_event_vol_ratio, 3.0)  # 最长延长至3倍
                logger.info(f"事件后波动率仍高({post_event_vol_ratio:.1f}x)，恢复期延长至 {recovery_duration:.0f}s")

            if elapsed >= recovery_duration:
                # 恢复完成
                self._event_warp_active = False
                self._event_warp_scale = 1.0
                self._event_id = None
                self._event_type = None
                self._event_warp_activated_at = 0.0
                logger.info("事件时间扭曲恢复完成")
            else:
                # 使用独立存储的 _event_type 获取起始 scale，消除字符串解析依赖
                event_type = self._event_type or "minor_macro"
                start_scale = self.EVENT_WARP_PROFILES.get(event_type, {"pre_event_scale": 0.9})["pre_event_scale"]
                progress = elapsed / max(recovery_duration, 1.0)
                self._event_warp_scale = start_scale + (1.0 - start_scale) * progress
                logger.debug(f"事件时间扭曲恢复中: scale={self._event_warp_scale:.2f}, progress={progress:.2f}")

            effective_scale = self._scale_state[1] * self._event_warp_scale

        return {
            "status": "ok",
            "reason": f"事件后恢复中: warp={self._event_warp_scale:.2f}, effective={effective_scale:.2f}",
            "data": {
                "warp_scale": self._event_warp_scale,
                "effective_scale": effective_scale,
                "event_active": self._event_warp_active,
            },
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检，包含事件扭曲持续时长诊断"""
        try:
            with self._lock:
                activity, scale = self._scale_state
                warp_active = self._event_warp_active
                activated_at = self._event_warp_activated_at
                event_id = self._event_id

            if not (self.MIN_TIME_SCALE <= scale <= self.MAX_TIME_SCALE):
                return {
                    "status": "error",
                    "reason": f"时间缩放因子异常: {scale}",
                    "data": {},
                    "warnings": ["invalid_time_scale"],
                }

            warnings = []
            if warp_active and activated_at > 0:
                actual_duration = time.monotonic() - activated_at
                if actual_duration > self.EVENT_WARP_MAX_ACTIVE_SECONDS:
                    warnings.append(f"event_warp_stuck: 事件扭曲已激活 {actual_duration:.0f}s，超过上限 {self.EVENT_WARP_MAX_ACTIVE_SECONDS}s")

            return {
                "status": "ok",
                "reason": f"ElasticTimeManager 正常，活跃度={activity:.1f}，缩放因子={scale:.2f}",
                "data": {
                    "activity": activity,
                    "time_scale": scale,
                    "event_warp_active": warp_active,
                    "event_warp_duration_seconds": round(time.monotonic() - activated_at) if warp_active and activated_at > 0 else 0,
                    "dependencies": {
                        "state_machine": self._state_machine is not None,
                        "data_feed": self._data_feed is not None,
                    },
                },
                "warnings": warnings,
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和活跃度数值")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _get_remaining_recovery(self, now: float) -> float:
        """计算旧事件预计剩余恢复时间（秒）"""
        if not self._event_warp_active or self._event_recovery_start <= 0:
            return 0.0
        elapsed = now - self._event_recovery_start
        remaining = max(0.0, self._event_recovery_duration - elapsed)
        return round(remaining, 1)

    def _calc_hysteresis(self, activity_delta: float) -> float:
        """根据活跃度变化幅度动态计算回滞时间"""
        ratio = min(activity_delta / self.HYSTERESIS_DELTA_MAX, 1.0)
        hysteresis = self.HYSTERESIS_MAX_SECONDS - ratio * (self.HYSTERESIS_MAX_SECONDS - self.HYSTERESIS_MIN_SECONDS)
        return max(self.HYSTERESIS_MIN_SECONDS, min(self.HYSTERESIS_MAX_SECONDS, hysteresis))

    def _calculate_scale(self, activity: float, volatility_percentile: float = 50.0, volume_ratio: float = 1.0) -> float:
        """基于活跃度计算时间缩放因子，并引入机构级风控约束"""
        baseline = self.DEFAULT_BASE_ACTIVITY if self.DEFAULT_BASE_ACTIVITY > 0 else 50.0
        ratio = activity / baseline

        if ratio > 1.0:
            compression = (ratio - 1.0) * self.DEFAULT_COMPRESSION_RATE
            # 机构级约束1：波动率超过阈值时，抑制压缩
            if volatility_percentile > self.VOLATILITY_COMPRESSION_CAP_PERCENTILE:
                suppression = (volatility_percentile - self.VOLATILITY_COMPRESSION_CAP_PERCENTILE) / (100.0 - self.VOLATILITY_COMPRESSION_CAP_PERCENTILE)
                compression *= max(0.1, 1.0 - suppression)
            # 机构级约束2：成交量质量低（流动性不足）时，压缩效果减半
            if volume_ratio < self.LOW_VOLUME_QUALITY_THRESHOLD:
                compression *= 0.5
            scale = max(self.MIN_TIME_SCALE, 1.0 - compression)
        else:
            tension = (1.0 - ratio) * self.DEFAULT_TENSION_RATE
            scale = min(self.MAX_TIME_SCALE, 1.0 + tension)

        return round(scale, 4)
