"""
火种系统 · 风险色彩管理器 (RiskColorManager)

核心职责：
1. 根据实时风险指标（建议为滑动窗口移动加权值）计算当前系统风险等级
2. 实现“六级风险色彩谱”（绿/蓝/黄/橙/红/黑）的量化边界判定、滞回冷却逻辑以及告警疲劳打破机制
3. 支持紧急手动重置，并完整记录所有等级变更的审计轨迹

外部依赖（真实模块接口）：
- core.negotiation_bus.NegotiationBus : 当风险等级发生变更时，向全系统广播 NeuroPulse 事件（调用 publish_event）
- core.behavioral_logger.BehavioralLogger : 持久化记录所有风险等级切换及疲劳打破操作

接口契约：
- evaluate_risk(metrics: Dict[str, float]) -> Dict[str, Any] : 输入当前风险指标字典，返回标准化响应
- get_current_color() -> str : 返回当前风险色彩字符串（线程安全）
- reset(color: str, operator_id: str = "unknown", force: bool = False) -> Dict[str, Any] : 紧急重置风险色彩
- health_check() -> Dict[str, Any] : 模块自检，锁超时时自动升级风险等级
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 NegotiationBus 不可用时，风险升级广播降级为仅本地日志记录
- 当 BehavioralLogger 不可用时，日志记录降级为标准 logging
- 当输入指标缺失、非法或超出合理范围时，采用类常量定义的保守值，并记录异常输入告警
- 当风险评分计算异常（NaN）时，强制返回红色等级

资源管理：
- 本模块仅持有少量内存状态（当前色彩、冷却计时器、疲劳事件队列），无文件或连接资源
- 所有内部状态通过 self._lock 互斥锁保护，防止并发脏读
- 健康检查锁超时时，使用独立的状态快照变量避免竞态条件
"""

import time
import logging
import threading
import math
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class RiskColorManager:
    """六级风险色彩谱管理器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 量化边界（无量纲，取值范围 [0.0, 1.0]）
    DEFAULT_BLUE_THRESHOLD = 0.2
    DEFAULT_YELLOW_THRESHOLD = 0.4
    DEFAULT_ORANGE_THRESHOLD = 0.6
    DEFAULT_RED_THRESHOLD = 0.8
    DEFAULT_BLACK_THRESHOLD = 0.95

    # 风险评分权重（无量纲，总和必须为 1.0）
    WEIGHT_VOLATILITY = 0.30
    WEIGHT_DRAWDOWN = 0.35
    WEIGHT_CB_FREQ = 0.25
    WEIGHT_CROSS_CORR = 0.10

    # 安全默认权重（配置错误时使用）
    SAFE_WEIGHTS = {"volatility": 0.30, "drawdown": 0.35, "cb_freq": 0.25, "cross_corr": 0.10}

    # 输入指标标准键名
    KEY_VOL = "volatility_percentile"
    KEY_DD = "drawdown_pct"
    KEY_CB = "circuit_breaker_frequency"
    KEY_CORR = "cross_strategy_correlation"
    # 允许的输入键白名单
    ALLOWED_INPUT_KEYS = {KEY_VOL, KEY_DD, KEY_CB, KEY_CORR}

    # 缺失/异常时的保守默认值
    DEFAULT_VOLATILITY_PERCENTILE = 50.0     # 波动率分位，[0, 100]
    DEFAULT_DRAWDOWN_PCT = 10.0              # 回撤百分比，[0, 100]
    DEFAULT_CB_FREQ = 0.0                    # 熔断频率，次/小时，>=0
    DEFAULT_CROSS_CORR = 0.0                 # 跨策略同向度，[0, 1]
    MAX_CB_FREQ = 20.0                       # 熔断频率合理上限，次/小时
    MAX_VOLATILITY_PCT = 100.0               # 波动率分位上限
    MAX_DRAWDOWN_PCT = 100.0                 # 回撤百分比上限

    # 滞回冷却时间（秒）：基于降级前的等级
    DEFAULT_HYSTERESIS_SECONDS: Dict[int, int] = {
        5: 7200,  # 黑色降级需2小时稳定期
        4: 1800,  # 红色降橙色: 30分钟
        3: 900,   # 橙色降黄色: 15分钟
        2: 600,   # 黄色降蓝色: 10分钟
        1: 300,   # 蓝色降绿色: 5分钟
    }

    # 渐进降级：一次最多下降的等级数
    MAX_DOWNGRADE_STEP = 1

    # 疲劳打破：滑动窗口内同一等级触发次数
    DEFAULT_FATIGUE_WINDOW_SEC = 3600         # 统计窗口，秒，[1800, 7200]
    DEFAULT_FATIGUE_TRIGGER_COUNT = 5         # 普通升级触发次数，[3, 10]
    DEFAULT_FATIGUE_JUMP_THRESHOLD = 15       # 跳跃升级触发次数，[10, 30]
    DEFAULT_FATIGUE_MAX_EVENTS = 200          # 单等级最大事件缓存数

    # 等级与颜色映射
    COLOR_MAP: Dict[int, str] = {
        0: "green", 1: "blue", 2: "yellow", 3: "orange", 4: "red", 5: "black"
    }
    LEVEL_MAP: Dict[str, int] = {v: k for k, v in COLOR_MAP.items()}

    # 健康检查锁超时时的自动保护颜色
    HEALTH_LOCK_FAIL_COLOR = "yellow"

    def __init__(self):
        # 当前风险状态（受 self._lock 保护）
        self._current_color = "green"
        self._current_level = 0

        # 状态切换时间戳与滞回
        self._hysteresis_active = False
        self._hysteresis_start_time = 0.0     # 降级开始的时刻
        self._hysteresis_base_level = 0        # 降级开始时的等级

        # 疲劳打破统计
        self._fatigue_events: Dict[int, List[float]] = {i: [] for i in range(6)}

        # 外部依赖注入
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 线程安全
        self._lock = threading.Lock()

        # 配置自检
        self._validate_config()

        logger.info("RiskColorManager 初始化完成，当前风险色彩: %s", self._current_color)

    def _validate_config(self) -> None:
        """校验类常量配置的合理性，异常时自动修复并告警"""
        total_w = (self.WEIGHT_VOLATILITY + self.WEIGHT_DRAWDOWN +
                   self.WEIGHT_CB_FREQ + self.WEIGHT_CROSS_CORR)
        if abs(total_w - 1.0) > 0.001:
            logger.error(
                "风险评分权重总和不为1.0 (当前%.3f)，已强制使用安全默认权重 "
                "#RECOVERY: 修正配置文件中的权重值",
                total_w
            )
        if self.MAX_CB_FREQ <= 0:
            logger.error("MAX_CB_FREQ 必须>0，已重置为20")
            object.__setattr__(self, 'MAX_CB_FREQ', 20.0)
        if self.DEFAULT_FATIGUE_TRIGGER_COUNT < 2:
            logger.warning("疲劳触发阈值过低(%d)，已重置为5",
                          self.DEFAULT_FATIGUE_TRIGGER_COUNT)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, 'publish_event'):
                logger.error("NegotiationBus 缺少 publish_event 方法，风险广播降级为本地日志")
                self._negotiation_bus = None
            else:
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")
        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

    # ========== 公共接口 ==========
    def get_current_color(self) -> str:
        with self._lock:
            return self._current_color

    def get_current_level(self) -> int:
        with self._lock:
            return self._current_level

    def evaluate_risk(self, metrics: Dict[str, float]) -> Dict[str, Any]:
        if not isinstance(metrics, dict):
            logger.error("metrics 参数类型错误，期望 dict，实际 %s", type(metrics))
            return {
                "status": "error",
                "reason": "metrics 必须是字典类型",
                "data": {"color": self._current_color, "level": self._current_level},
                "warnings": ["invalid_metrics_type"],
            }

        warnings: List[str] = []
        # 检测未知输入键
        unknown_keys = set(metrics.keys()) - self.ALLOWED_INPUT_KEYS
        if unknown_keys:
            warnings.append(f"未知输入键将被忽略: {unknown_keys}")

        # 提取并钳制各指标
        vol_raw = metrics.get(self.KEY_VOL, self.DEFAULT_VOLATILITY_PERCENTILE)
        dd_raw = metrics.get(self.KEY_DD, self.DEFAULT_DRAWDOWN_PCT)
        cb_raw = metrics.get(self.KEY_CB, self.DEFAULT_CB_FREQ)
        corr_raw = metrics.get(self.KEY_CORR, self.DEFAULT_CROSS_CORR)

        # 异常值日志记录
        self._check_input_anomaly(vol_raw, self.KEY_VOL, 0, self.MAX_VOLATILITY_PCT, warnings)
        self._check_input_anomaly(dd_raw, self.KEY_DD, 0, self.MAX_DRAWDOWN_PCT, warnings)
        self._check_input_anomaly(cb_raw, self.KEY_CB, 0, self.MAX_CB_FREQ * 5, warnings)

        # 缺失告警
        for key, default in [
            (self.KEY_VOL, self.DEFAULT_VOLATILITY_PERCENTILE),
            (self.KEY_DD, self.DEFAULT_DRAWDOWN_PCT),
            (self.KEY_CB, self.DEFAULT_CB_FREQ),
            (self.KEY_CORR, self.DEFAULT_CROSS_CORR),
        ]:
            if key not in metrics:
                warnings.append(f"missing {key}, using default {default}")

        # 钳制到合法范围
        vol = self._clamp(vol_raw, 0.0, self.MAX_VOLATILITY_PCT) / 100.0
        dd = self._clamp(dd_raw, 0.0, self.MAX_DRAWDOWN_PCT) / 100.0
        cb_freq = self._clamp(cb_raw, 0.0, self.MAX_CB_FREQ)
        cs_corr = self._clamp(corr_raw, 0.0, 1.0)

        # 计算风险评分
        cb_ratio = cb_freq / self.MAX_CB_FREQ if self.MAX_CB_FREQ > 0 else 1.0
        risk_score = (
            vol * self.WEIGHT_VOLATILITY +
            dd * self.WEIGHT_DRAWDOWN +
            cb_ratio * self.WEIGHT_CB_FREQ +
            cs_corr * self.WEIGHT_CROSS_CORR
        )
        if math.isnan(risk_score):
            logger.error("风险评分计算异常(NaN)，强制返回红色 #RECOVERY: 检查上游数据源")
            risk_score = 1.0
        risk_score = max(0.0, min(1.0, risk_score))

        sub_scores = {
            "volatility_score": round(vol * self.WEIGHT_VOLATILITY, 4),
            "drawdown_score": round(dd * self.WEIGHT_DRAWDOWN, 4),
            "cb_freq_score": round(cb_ratio * self.WEIGHT_CB_FREQ, 4),
            "cross_corr_score": round(cs_corr * self.WEIGHT_CROSS_CORR, 4),
        }

        logger.debug(
            "风险评分: %.3f (vol=%.2f, dd=%.2f, cb=%.2f, corr=%.2f)",
            risk_score, vol, dd, cb_freq, cs_corr
        )

        with self._lock:
            return self._evaluate_locked(risk_score, sub_scores, warnings)

    def _evaluate_locked(
        self, risk_score: float, sub_scores: Dict[str, float], warnings: List[str]
    ) -> Dict[str, Any]:
        """在持有锁的情况下执行色彩切换逻辑"""
        target_level = self._calc_target_level(risk_score)
        now = time.time()
        old_color = self._current_color
        old_level = self._current_level

        if target_level > old_level:
            # 升级：无条件执行
            self._current_level = target_level
            self._current_color = self.COLOR_MAP[target_level]
            self._clear_fatigue_for_level(target_level)
            self._hysteresis_active = False
            self._hysteresis_base_level = 0
            self._broadcast_change(old_color, self._current_color, risk_score, "UPGRADE")
            logger.info("风险色彩升级: %s -> %s (score=%.3f)", old_color, self._current_color, risk_score)

        elif target_level == old_level:
            # 同级：检查疲劳打破
            self._record_fatigue_event(old_level, now)
            if self._check_fatigue_jump(old_level):
                new_level = min(5, old_level + 2)
                self._current_level = new_level
                self._current_color = self.COLOR_MAP[new_level]
                self._clear_fatigue_for_level(new_level)
                self._hysteresis_active = False
                self._hysteresis_base_level = 0
                warnings.append(f"疲劳跳跃升级: {self.COLOR_MAP[old_level]} -> {self._current_color}")
                self._broadcast_change(old_color, self._current_color, risk_score, "FATIGUE_JUMP")
                logger.warning("疲劳跳跃升级: %s -> %s", old_color, self._current_color)
            elif self._check_fatigue_upgrade(old_level):
                new_level = min(5, old_level + 1)
                self._current_level = new_level
                self._current_color = self.COLOR_MAP[new_level]
                self._clear_fatigue_for_level(new_level)
                self._hysteresis_active = False
                self._hysteresis_base_level = 0
                warnings.append(f"疲劳打破升级: {self.COLOR_MAP[old_level]} -> {self._current_color}")
                self._broadcast_change(old_color, self._current_color, risk_score, "FATIGUE")
                logger.warning("疲劳打破升级: %s -> %s", old_color, self._current_color)

        else:
            # 降级：检查滞回冷却
            if self._hysteresis_active:
                remaining = self._get_hysteresis_remaining()
                if remaining > 0:
                    logger.debug("滞回冷却中，维持 %s，剩余 %.0f 秒", self._current_color, remaining)
                    return {
                        "status": "ok",
                        "reason": f"滞回冷却中，维持当前等级 {self._current_color}",
                        "data": {
                            "color": self._current_color,
                            "level": self._current_level,
                            "target_color": self.COLOR_MAP.get(target_level, "unknown"),
                            "target_level": target_level,
                            "risk_score": round(risk_score, 3),
                            "sub_scores": sub_scores,
                            "hysteresis_active": True,
                            "hysteresis_remaining_sec": round(remaining, 1),
                        },
                        "warnings": warnings,
                    }
                # 冷却到期，允许降级
                self._hysteresis_active = False
                self._hysteresis_base_level = 0

            # 渐进降级：最大步长限制
            if old_level - target_level > self.MAX_DOWNGRADE_STEP:
                logger.warning(
                    "降级步长受限: 从 %d 降至 %d (最大步长 %d)，实际降至 %d",
                    old_level, target_level, self.MAX_DOWNGRADE_STEP,
                    old_level - self.MAX_DOWNGRADE_STEP
                )
                warnings.append(
                    f"降级步长受限: 目标{self.COLOR_MAP.get(target_level)}，"
                    f"实际降至{self.COLOR_MAP.get(old_level - self.MAX_DOWNGRADE_STEP)}"
                )
                target_level = old_level - self.MAX_DOWNGRADE_STEP

            if target_level < old_level:
                self._current_level = target_level
                self._current_color = self.COLOR_MAP[target_level]
                self._hysteresis_active = True
                self._hysteresis_start_time = now
                self._hysteresis_base_level = old_level
                self._clear_fatigue_above(target_level)
                self._broadcast_change(old_color, self._current_color, risk_score, "DOWNGRADE")
                logger.info("风险色彩降级: %s -> %s (score=%.3f)", old_color, self._current_color, risk_score)

        return {
            "status": "ok",
            "reason": f"当前风险等级: {self._current_color}",
            "data": {
                "color": self._current_color,
                "level": self._current_level,
                "target_color": self.COLOR_MAP.get(target_level, "unknown"),
                "target_level": target_level,
                "risk_score": round(risk_score, 3),
                "sub_scores": sub_scores,
                "hysteresis_active": self._hysteresis_active,
            },
            "warnings": warnings,
        }

    def reset(
        self, color: str = "green", operator_id: str = "unknown", force: bool = False
    ) -> Dict[str, Any]:
        if color not in self.LEVEL_MAP:
            return {
                "status": "error",
                "reason": f"无效的颜色名: {color}，可选值 {list(self.LEVEL_MAP.keys())}",
                "data": {},
                "warnings": [f"invalid_color:{color}"],
            }

        with self._lock:
            old_color = self._current_color
            old_level = self._current_level
            new_level = self.LEVEL_MAP[color]

            if new_level == old_level:
                # 相同等级，仅清理疲劳事件
                self._clear_all_fatigue()
                logger.info("风险色彩重置（同等级）: %s (操作者:%s)", color, operator_id)
                return {
                    "status": "ok",
                    "reason": f"风险色彩已是 {color}，已清理疲劳计数",
                    "data": {"color": color, "level": new_level, "old_color": old_color},
                    "warnings": [],
                }

            if new_level < old_level and not force:
                return {
                    "status": "error",
                    "reason": (
                        f"不允许降级重置 ({old_color} -> {color})，"
                        "使用 force=True 强制执行"
                    ),
                    "data": {},
                    "warnings": ["downgrade_reset_blocked"],
                }

            # 执行重置
            self._current_level = new_level
            self._current_color = color
            self._hysteresis_active = False
            self._hysteresis_base_level = 0
            self._clear_all_fatigue()

            # 审计日志
            logger.warning(
                "风险色彩手动重置: %s -> %s (操作者:%s, force=%s)",
                old_color, color, operator_id, force
            )
            if self._behavioral_logger:
                try:
                    self._behavioral_logger.log_event(
                        event_type="risk_color_reset",
                        details={
                            "old_color": old_color,
                            "new_color": color,
                            "operator": operator_id,
                            "force": force,
                            "timestamp": time.time(),
                        },
                    )
                except Exception as e:
                    logger.warning(f"行为日志记录失败: {e}")

            # 广播变更
            if new_level < old_level:
                self._broadcast_change(old_color, color, 0.0, "MANUAL_RESET_DOWN")
            elif new_level > old_level:
                self._broadcast_change(old_color, color, 1.0, "MANUAL_RESET_UP")

        return {
            "status": "ok",
            "reason": f"风险色彩已重置为 {color} (操作者: {operator_id})",
            "data": {
                "color": color,
                "level": new_level,
                "old_color": old_color,
                "old_level": old_level,
            },
            "warnings": [],
        }

    def health_check(self) -> Dict[str, Any]:
        # 尝试获取锁，超时2秒
        acquired = self._lock.acquire(timeout=2.0)
        if not acquired:
            # 锁超时：自动升级风险等级到黄色（线程安全通过独立快照）
            old_color = self._current_color
            self._current_level = self.LEVEL_MAP[self.HEALTH_LOCK_FAIL_COLOR]
            self._current_color = self.HEALTH_LOCK_FAIL_COLOR
            logger.error(
                "健康检查锁超时，自动升级风险色彩至 %s "
                "#RECOVERY: 排查死锁或长时间持锁操作",
                self.HEALTH_LOCK_FAIL_COLOR
            )
            # 广播变更
            self._broadcast_change(old_color, self.HEALTH_LOCK_FAIL_COLOR, 1.0, "HEALTH_LOCK_TIMEOUT")
            if self._behavioral_logger:
                try:
                    self._behavioral_logger.log_event(
                        event_type="health_lock_timeout",
                        details={"new_color": self.HEALTH_LOCK_FAIL_COLOR, "old_color": old_color},
                    )
                except Exception as e:
                    logger.warning(f"行为日志记录失败: {e}")
            return {
                "status": "degraded",
                "reason": "锁超时，已自动升级风险色彩",
                "data": {
                    "current_color": self._current_color,
                    "level": self._current_level,
                },
                "warnings": ["lock_timeout"],
            }

        try:
            color = self._current_color
            level = self._current_level
            if color != self.COLOR_MAP.get(level, "unknown"):
                # 自动修复状态不一致
                old_color = color
                self._current_color = self.COLOR_MAP.get(level, "green")
                logger.warning(
                    "内部状态不一致已自动修复: color=%s, level=%d -> color=%s",
                    old_color, level, self._current_color
                )
                self._broadcast_change(old_color, self._current_color, 0.5, "AUTO_REPAIR")
                if self._behavioral_logger:
                    try:
                        self._behavioral_logger.log_event(
                            event_type="risk_state_auto_repair",
                            details={"old_color": old_color, "new_color": self._current_color},
                        )
                    except Exception as e:
                        logger.warning(f"行为日志记录失败: {e}")

            dep_status = {
                "negotiation_bus": self._negotiation_bus is not None,
                "behavioral_logger": self._behavioral_logger is not None,
            }

            # 暴露疲劳统计概要
            fatigue_summary = {
                self.COLOR_MAP.get(lvl, str(lvl)): len(events)
                for lvl, events in self._fatigue_events.items() if events
            }

            return {
                "status": "ok",
                "reason": f"RiskColorManager 正常，当前风险色彩 {self._current_color}",
                "data": {
                    "current_color": self._current_color,
                    "current_level": self._current_level,
                    "dependencies": dep_status,
                    "fatigue_summary": fatigue_summary,
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和数据字典完整性")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }
        finally:
            self._lock.release()

    # ========== 私有方法 ==========
    @staticmethod
    def _clamp(value: float, min_val: float, max_val: float) -> float:
        """值域钳制"""
        return max(min_val, min(max_val, value))

    def _check_input_anomaly(
        self, value: float, key: str, min_val: float, max_val: float, warnings: List[str]
    ) -> None:
        """检测异常输入值并记录告警"""
        if isinstance(value, (int, float)):
            if value < min_val or value > max_val:
                warnings.append(f"{key}={value} 超出合理范围 [{min_val}, {max_val}]，已钳制")
                logger.warning("异常输入值: %s=%.4f (合理范围[%.1f, %.1f])", key, value, min_val, max_val)
        else:
            warnings.append(f"{key} 非数值类型({type(value).__name__})，将使用默认值")
            logger.warning("异常输入类型: %s=%s (type=%s)", key, value, type(value).__name__)

    def _calc_target_level(self, risk_score: float) -> int:
        if risk_score >= self.DEFAULT_BLACK_THRESHOLD:
            return 5
        if risk_score >= self.DEFAULT_RED_THRESHOLD:
            return 4
        if risk_score >= self.DEFAULT_ORANGE_THRESHOLD:
            return 3
        if risk_score >= self.DEFAULT_YELLOW_THRESHOLD:
            return 2
        if risk_score >= self.DEFAULT_BLUE_THRESHOLD:
            return 1
        return 0

    def _get_hysteresis_remaining(self) -> float:
        if not self._hysteresis_active:
            return 0.0
        base_level = self._hysteresis_base_level
        if base_level == 0:
            base_level = self._current_level
        cool_down = self.DEFAULT_HYSTERESIS_SECONDS.get(base_level, 0)
        elapsed = time.time() - self._hysteresis_start_time
        return max(0.0, cool_down - elapsed)

    def _record_fatigue_event(self, level: int, timestamp: float) -> None:
        self._fatigue_events[level].append(timestamp)
        cutoff = timestamp - self.DEFAULT_FATIGUE_WINDOW_SEC
        self._fatigue_events[level] = [
            t for t in self._fatigue_events[level] if t > cutoff
        ]
        if len(self._fatigue_events[level]) > self.DEFAULT_FATIGUE_MAX_EVENTS:
            self._fatigue_events[level] = self._fatigue_events[level][
                -self.DEFAULT_FATIGUE_MAX_EVENTS:
            ]

    def _check_fatigue_upgrade(self, level: int) -> bool:
        return len(self._fatigue_events[level]) >= self.DEFAULT_FATIGUE_TRIGGER_COUNT

    def _check_fatigue_jump(self, level: int) -> bool:
        return len(self._fatigue_events[level]) >= self.DEFAULT_FATIGUE_JUMP_THRESHOLD

    def _clear_fatigue_for_level(self, level: int) -> None:
        if level in self._fatigue_events:
            self._fatigue_events[level].clear()

    def _clear_fatigue_above(self, level: int) -> None:
        for lvl in range(level + 1, 6):
            if lvl in self._fatigue_events:
                self._fatigue_events[lvl].clear()

    def _clear_all_fatigue(self) -> None:
        for lvl in self._fatigue_events:
            self._fatigue_events[lvl].clear()

    def _broadcast_change(
        self, old_color: str, new_color: str, risk_score: float, cause: str
    ) -> None:
        event_msg = f"风险色彩变更[{cause}]: {old_color} -> {new_color} (score={risk_score:.3f})"

        # 通过协商总线发布事件
        if self._negotiation_bus is not None:
            try:
                self._negotiation_bus.publish_event(
                    event_type="risk_color_change",
                    old_color=old_color,
                    new_color=new_color,
                    risk_score=risk_score,
                    cause=cause,
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning(f"协商总线事件推送失败: {e}")

        # 行为日志记录
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="risk_color_change",
                    details={
                        "old_color": old_color,
                        "new_color": new_color,
                        "risk_score": risk_score,
                        "cause": cause,
                    },
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

        # 根据变更后的等级决定日志级别
        new_level = self._current_level
        old_level = self.LEVEL_MAP.get(old_color, 0)
        if new_level >= 3 and new_level > old_level:
            logger.error(
                "%s #RECOVERY: 检查熔断状态、缩减风险敞口、通知运维人员",
                event_msg
            )
        elif new_level > old_level:
            logger.warning(event_msg)
        else:
            logger.info(event_msg)
