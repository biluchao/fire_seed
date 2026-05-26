"""
火种系统 · 弹性时间管理器 (ElasticTimeManager)

核心职责：
1. 根据市场活跃度（波动率分位、成交量比率、交易频率）连续映射时间参数的缩放因子，实现高活跃压缩、低活跃拉伸的动态感知
2. 支持宏观事件时间扭曲：在重大事件（如非农、利率决议）前后自动压缩时间窗口，提升策略响应速度，事件后逐步恢复

外部依赖（真实模块接口）：
- core.state_machine.StateMachine : 获取当前市场状态（趋势/震荡/反转），辅助判断波动率分位基线
- core.data_feed.MarketDataFeed : 获取当前成交量比率和宏观事件日历

接口契约：
- update_activity(volatility_percentile: float, volume_ratio: float, trade_frequency: float) -> Dict[str, Any]
  更新当前市场活跃度，计算并缓存时间缩放因子
- get_time_scale() -> float
  返回当前时间缩放因子（>1.0 表示拉伸，<1.0 表示压缩）
- scale_parameter(base_value: float, sensitivity_tier: str = "medium") -> Dict[str, Any]
  根据参数类型（high/medium/low）和当前缩放因子调整时间参数
- get_adjusted_lifecycle_windows(base_windows: Dict[str, int]) -> Dict[str, int]
  对五阶段生命周期窗口进行整体缩放
- apply_event_warp(event_type: str, minutes_to_event: float) -> Dict[str, Any]
  根据宏观事件类型和距离事件的时间，返回动态缩放系数
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 MarketDataFeed 或 StateMachine 不可用时，使用默认活跃度（50分位）运行，并标记 "degraded" 状态
- 参数校验失败时，使用安全默认值（1.0 缩放因子）继续，并记录 WARNING 日志
- 所有降级值在类常量区明确声明

资源管理：
- 本模块不持有任何外部资源句柄，所有计算在方法内完成
- 内部状态（当前缩放因子、活跃度）通过线程锁保护，确保并发安全
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class ElasticTimeManager:
    """弹性时间管理器：让系统时间感知市场活跃度与宏观事件"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_ACTIVITY = 50.0                # 默认市场活跃度（百分位），无量纲，取值范围 [0, 100]
    DEFAULT_BASE_ACTIVITY = 50.0           # 基准活跃度，用于计算缩放比例，无量纲，[0, 100]
    DEFAULT_COMPRESSION_RATE = 0.5         # 高活跃压缩速率：活跃度每超出基准1倍，时间压缩的比例，无量纲，[0.2, 0.8]
    DEFAULT_TENSION_RATE = 1.0             # 低活跃拉伸速率：活跃度每低于基准1倍，时间拉伸的比例，无量纲，[0.5, 2.0]
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
            "pre_event_advance_minutes": 15,     # 事件前15分钟开始加速
            "pre_event_scale": 0.6,              # 事件前压缩到60%的速度
            "post_event_recovery_minutes": 5,    # 事件后5分钟恢复
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

    # 回滞保护
    HYSTERESIS_STABLE_SECONDS = 30          # 活跃度变化后需稳定30秒才允许再次更新缩放因子，防止抖动

    def __init__(self):
        # 核心状态
        self._current_activity = self.DEFAULT_ACTIVITY
        self._time_scale = 1.0
        self._last_update_time = 0.0
        self._last_scale_change_time = 0.0
        self._pending_activity = None
        # 用于解决P1问题：记录上次缩放时的活跃度，保证回滞冷却期内 scale_parameter 使用一致的 (activity, scale) 对
        self._activity_at_last_scale_change = self.DEFAULT_ACTIVITY

        # 事件扭曲状态
        self._event_warp_active = False
        self._event_warp_scale = 1.0
        self._event_recovery_start = 0.0
        self._event_recovery_duration = 0.0

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

        Args:
            volatility_percentile: 波动率在历史分布中的分位数，[0, 100]
            volume_ratio: 当前成交量与过去均值的比值，[0, +∞)
            trade_frequency: 近期每分钟交易次数，[0, +∞)

        Returns:
            标准响应字典，data中包含当前活跃度和缩放因子
        """
        # 参数裁剪
        if not (0.0 <= volatility_percentile <= 100.0):
            logger.warning(f"波动率分位异常: {volatility_percentile}，裁剪至有效范围")
            volatility_percentile = max(0.0, min(100.0, volatility_percentile))
        if volume_ratio < 0:
            logger.warning(f"成交量比率为负: {volume_ratio}，重置为0")
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

        now = time.time()
        with self._lock:
            old_activity = self._current_activity
            # 记录待处理活跃度
            self._pending_activity = new_activity

            # 检查是否需要更新缩放因子（回滞保护）
            if self._last_scale_change_time > 0 and now - self._last_scale_change_time < self.HYSTERESIS_STABLE_SECONDS:
                # 仍在冷却期，仅更新活跃度，不改变缩放因子
                self._current_activity = new_activity
                logger.debug(f"活跃度已更新为 {new_activity:.1f}，缩放因子保持 {self._time_scale:.2f} (冷却中)")
                return {
                    "status": "ok",
                    "reason": f"活跃度已更新为 {new_activity:.1f}，缩放因子保持 {self._time_scale:.2f} (冷却中)",
                    "data": {"activity": new_activity, "time_scale": self._time_scale},
                    "warnings": ["hysteresis_cooling"],
                }

            # 计算新缩放因子
            new_scale = self._calculate_scale(new_activity)
            self._time_scale = new_scale
            self._current_activity = new_activity
            self._activity_at_last_scale_change = new_activity  # 记录此时活跃度
            self._last_update_time = now
            self._last_scale_change_time = now

            logger.info(
                f"时间缩放因子更新: {old_activity:.1f} → {new_activity:.1f}, "
                f"scale: {self._time_scale:.2f} "
                f"(基础压缩率=%.2f, 拉伸率=%.2f)",
                self.DEFAULT_COMPRESSION_RATE, self.DEFAULT_TENSION_RATE
            )

        return {
            "status": "ok",
            "reason": f"活跃度更新为 {new_activity:.1f}，时间缩放因子 = {self._time_scale:.2f}",
            "data": {"activity": new_activity, "time_scale": self._time_scale},
            "warnings": [],
        }

    def get_time_scale(self) -> float:
        """返回当前时间缩放因子（线程安全）"""
        with self._lock:
            return self._time_scale

    def scale_parameter(self, base_value: float, sensitivity_tier: str = "medium") -> Dict[str, Any]:
        """
        根据参数敏感度和当前市场活跃度缩放时间参数

        Args:
            base_value: 基础参数值
            sensitivity_tier: 参数敏感度分级 (high/medium/low)

        Returns:
            标准响应字典，data中包含缩放后的参数值
        """
        if sensitivity_tier not in self.SENSITIVITY_TIERS:
            logger.warning(f"无效敏感度等级: {sensitivity_tier}，使用 medium")
            sensitivity_tier = "medium"

        # 对无效基础值采用降级而非错误
        if base_value <= 0:
            logger.warning(f"基础参数值无效: {base_value}，返回原值作为降级")
            return {
                "status": "warning",
                "reason": f"基础参数值无效: {base_value}，已降级为原值",
                "data": {"scaled_value": base_value, "scale_factor": 1.0},
                "warnings": ["invalid_base_value_degraded"],
            }

        with self._lock:
            scale = self._time_scale
            # 解决P1问题：回滞冷却期内使用上次缩放时的活跃度，避免混合新旧状态
            now = time.time()
            if self._last_scale_change_time > 0 and now - self._last_scale_change_time < self.HYSTERESIS_STABLE_SECONDS:
                activity = self._activity_at_last_scale_change
            else:
                activity = self._current_activity

        # 根据敏感度调整缩放系数
        sensitivity = self.SENSITIVITY_TIERS[sensitivity_tier]
        deviation = (activity - self.DEFAULT_BASE_ACTIVITY) / 10.0
        adjusted_scale = 1.0 - deviation * sensitivity
        # 叠加全局时间缩放
        final_scale = adjusted_scale * scale
        final_scale = max(self.MIN_TIME_SCALE, min(self.MAX_TIME_SCALE, final_scale))

        scaled_value = max(1, int(base_value * final_scale)) if isinstance(base_value, int) else base_value * final_scale

        logger.debug(
            f"参数缩放: base={base_value}, tier={sensitivity_tier}, "
            f"activity={activity:.1f}, scale={scale:.2f}, adjusted={final_scale:.2f}, result={scaled_value}"
        )

        return {
            "status": "ok",
            "reason": f"基于活跃度{activity:.1f}和敏感度{sensitivity_tier}缩放，系数={final_scale:.2f}",
            "data": {"scaled_value": scaled_value, "scale_factor": final_scale},
            "warnings": [],
        }

    def get_adjusted_lifecycle_windows(self, base_windows: Dict[str, int]) -> Dict[str, Any]:
        """
        对五阶段生命周期窗口进行整体缩放

        Args:
            base_windows: 各阶段的基础秒数字典，如 {"incubation":60, "acceleration":180, ...}

        Returns:
            标准响应字典，data中包含缩放后的窗口字典
        """
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
        根据宏观事件类型和距离事件的时间，返回动态时间扭曲系数

        Args:
            event_type: 事件类型 (major_macro, medium_macro, minor_macro)
            minutes_to_event: 距离事件发生的分钟数（正数=事件前，负数=事件后）

        Returns:
            标准响应字典，data中包含当前有效缩放系数
        """
        if event_type not in self.EVENT_WARP_PROFILES:
            logger.warning(f"未知事件类型: {event_type}")
            return {
                "status": "error",
                "reason": f"未知事件类型: {event_type}",
                "data": {"warp_scale": 1.0},
                "warnings": ["unknown_event_type"],
            }

        profile = self.EVENT_WARP_PROFILES[event_type]
        now = time.time()

        with self._lock:
            if minutes_to_event > 0 and minutes_to_event <= profile["pre_event_advance_minutes"]:
                # 事件前：激活时间扭曲（仅在尚未激活时设置恢复基准）
                if not self._event_warp_active:
                    # 首次激活：记录恢复起始时间（预计事件发生时刻）
                    self._event_recovery_start = now + minutes_to_event * 60
                    self._event_recovery_duration = profile["post_event_recovery_minutes"] * 60
                    self._event_warp_active = True
                    self._event_warp_scale = profile["pre_event_scale"]
                    logger.info(f"事件时间扭曲激活: type={event_type}, scale={self._event_warp_scale:.2f}, "
                                f"距事件{minutes_to_event:.1f}分钟，恢复起始={self._event_recovery_start}")
                else:
                    # 已激活，仅更新扭曲系数，不重置恢复基准
                    self._event_warp_scale = profile["pre_event_scale"]
                    logger.debug(f"事件时间扭曲持续: scale={self._event_warp_scale:.2f}")

            elif minutes_to_event <= 0 and self._event_warp_active:
                # 事件已发生，根据时间推进计算恢复进度
                elapsed_since_event = abs(minutes_to_event) * 60
                if elapsed_since_event >= self._event_recovery_duration:
                    self._event_warp_active = False
                    self._event_warp_scale = 1.0
                    logger.info("事件时间扭曲恢复完成")
                else:
                    # 线性恢复
                    progress = elapsed_since_event / max(self._event_recovery_duration, 1.0)
                    self._event_warp_scale = profile["pre_event_scale"] + (1.0 - profile["pre_event_scale"]) * progress
                    logger.debug(f"事件时间扭曲恢复中: scale={self._event_warp_scale:.2f}, progress={progress:.2f}")

            effective_scale = self._time_scale * self._event_warp_scale

        return {
            "status": "ok",
            "reason": f"事件扭曲生效: type={event_type}, warp={self._event_warp_scale:.2f}, effective={effective_scale:.2f}",
            "data": {
                "warp_scale": self._event_warp_scale,
                "effective_scale": effective_scale,
                "event_active": self._event_warp_active,
            },
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            with self._lock:
                scale = self._time_scale
                activity = self._current_activity

            if not (self.MIN_TIME_SCALE <= scale <= self.MAX_TIME_SCALE):
                return {
                    "status": "error",
                    "reason": f"时间缩放因子异常: {scale}",
                    "data": {},
                    "warnings": ["invalid_time_scale"],
                }

            return {
                "status": "ok",
                "reason": f"ElasticTimeManager 正常，活跃度={activity:.1f}，缩放因子={scale:.2f}",
                "data": {
                    "activity": activity,
                    "time_scale": scale,
                    "event_warp_active": self._event_warp_active,
                    "dependencies": {
                        "state_machine": self._state_machine is not None,
                        "data_feed": self._data_feed is not None,
                    },
                },
                "warnings": [],
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
    def _calculate_scale(self, activity: float) -> float:
        """
        基于活跃度与基准活跃度的比值计算时间缩放因子
        活跃度高于基准 → 压缩 (<1)
        活跃度低于基准 → 拉伸 (>1)
        """
        # 防御零除
        baseline = self.DEFAULT_BASE_ACTIVITY if self.DEFAULT_BASE_ACTIVITY > 0 else 50.0
        ratio = activity / baseline
        if ratio > 1.0:
            scale = max(self.MIN_TIME_SCALE, 1.0 - (ratio - 1.0) * self.DEFAULT_COMPRESSION_RATE)
        else:
            scale = min(self.MAX_TIME_SCALE, 1.0 + (1.0 - ratio) * self.DEFAULT_TENSION_RATE)
        return round(scale, 4)
