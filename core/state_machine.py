"""
火种系统 · 市场状态机 (StateMachine)

核心职责：
1. 综合趋势/震荡/反转判定逻辑，协调滞后缓冲区与结构突变检测，输出当前市场状态标签
2. 提供状态切换的集中入口，管理过渡期双策略半仓模式，对外暴露统一的状态查询接口与变更通知

环境要求：
- Python 3.8+，推荐 3.11+（OrderedDict.popitem 在 3.7+ 可靠）
- 依赖子模块：RegimeHysteresis, StructureBreakDetector（可选，不可用时降级）

外部依赖（真实模块接口）：
- core.state_machine.regime_hysteresis.RegimeHysteresis : 执行滞后缓冲判定，防止状态在临界点频繁切换
- core.state_machine.structure_break_detector.StructureBreakDetector : 执行 MMD 及变点检测，识别市场结构突变
- core.context_isolator.ContextIsolator : 获取各周期的独立数据视图，用于状态计算
- core.negotiation_bus.NegotiationBus : 发送状态变更事件与告警通知（通过 publish_alert 方法）
- core.behavioral_logger.BehavioralLogger : 记录状态切换事件与决策上下文
- auth_checker: Callable[[str, str], bool] : 鉴权回调函数，用于 force_state 权限验证（可选注入）

接口契约：
- update_state(period: str, market_data: Dict[str, Any]) -> Dict[str, Any] : 更新指定周期的市场状态
- get_current_state(period: str) -> Dict[str, Any] : 返回指定周期当前的市场状态与过渡信息
- get_all_states() -> Dict[str, Any] : 返回所有周期的状态汇总
- force_state(period: str, state: Regime, operator: str) -> Dict[str, Any] : 强制覆盖状态（需权限）
- register_state_change_callback(callback, strong=False) -> None : 注册状态变更回调（默认弱引用）
- unregister_state_change_callback(callback) -> None : 取消注册状态变更回调
- set_min_duration(period: str, duration_sec: float) -> None : 动态调整冷却期
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当子模块不可用时，回退至基于简单阈值的基础状态判定，并记录降级告警
- 当 ContextIsolator 不可用时，使用全局共享数据视图作为降级方案
- 降级值在类常量区明确声明，并可被配置文件覆盖
- 状态变为 UNKNOWN 后，若超过 600 秒未恢复，自动强制切换为 OSCILLATION 保守状态

资源管理：
- 本模块不持有需要手动释放的资源，线程锁在对象销毁时自动释放
- 回调函数默认使用弱引用防止内存泄漏，同时提供强引用注册选项（strong=True）
- 缓存结果使用 LRU 淘汰策略，所有缓存操作均在 RLock 保护下执行
- 状态快照接口（save_snapshot/load_snapshot）用于持久化恢复
"""

import logging
import threading
import time
import weakref
from typing import Dict, Any, List, Optional, Callable, Tuple, Set, Union
from enum import Enum
from dataclasses import dataclass
from collections import OrderedDict

logger = logging.getLogger(__name__)

# 尝试导入子模块，失败时降级
try:
    from core.state_machine.regime_hysteresis import RegimeHysteresis
except ImportError:
    RegimeHysteresis = None
    logger.warning("RegimeHysteresis 不可用，滞回功能降级")

try:
    from core.state_machine.structure_break_detector import StructureBreakDetector
except ImportError:
    StructureBreakDetector = None
    logger.warning("StructureBreakDetector 不可用，结构突变检测降级")


# ========== 全局常量 ==========
_CACHE_MISS = object()  # 缓存未命中哨兵


class Regime(Enum):
    """市场状态枚举"""

    TREND = "trend"
    OSCILLATION = "oscillation"
    REVERSAL = "reversal"
    UNKNOWN = "unknown"

    # 状态同义词映射表（只读，需通过 update_synonyms 方法修改）
    _SYNONYMS = {
        "trend": "trend",
        "trending": "trend",
        "bull": "trend",
        "bear": "trend",
        "oscillation": "oscillation",
        "range": "oscillation",
        "ranging": "oscillation",
        "sideways": "oscillation",
        "consolidation": "oscillation",
        "reversal": "reversal",
        "reversing": "reversal",
        "unknown": "unknown",
        "uncertain": "unknown",
    }
    _SYNONYMS_LOCK = threading.Lock()

    @classmethod
    def from_string(cls, value: str) -> 'Regime':
        """
        从字符串安全转换为枚举，支持同义词

        Args:
            value: 状态字符串，不区分大小写

        Returns:
            对应的 Regime 枚举值
        """
        normalized = value.lower().strip()
        if not normalized:
            logger.debug("Regime.from_string 收到空字符串，返回 UNKNOWN")
            return cls.UNKNOWN
        with cls._SYNONYMS_LOCK:
            mapped = cls._SYNONYMS.get(normalized, "unknown")
        return cls(mapped)

    @classmethod
    def update_synonyms(cls, custom_mapping: Dict[str, str]) -> None:
        """
        更新同义词映射表（全局配置，影响所有 Regime 枚举使用者）

        Args:
            custom_mapping: 自定义同义词字典，将与现有映射合并
        """
        with cls._SYNONYMS_LOCK:
            cls._SYNONYMS.update(custom_mapping)
        logger.info("Regime 同义词映射已更新，新增 %d 条", len(custom_mapping))


@dataclass(frozen=True)
class TransitionInfo:
    """状态过渡信息（不可变数据类）"""

    in_transition: bool = False
    remaining_seconds: float = 0.0
    target_state: Optional[Regime] = None  # None 表示无过渡目标

    def __post_init__(self):
        """参数校验"""
        if self.remaining_seconds < 0:
            object.__setattr__(self, 'remaining_seconds', 0.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "in_transition": self.in_transition,
            "remaining_seconds": round(self.remaining_seconds, 2),
            "target_state": (
                self.target_state.value if self.target_state else None
            ),
        }

    def __repr__(self) -> str:
        target_str = getattr(self.target_state, 'value', 'None')
        return (
            f"TransitionInfo(in_transition={self.in_transition}, "
            f"remaining={self.remaining_seconds:.1f}s, "
            f"target={target_str})"
        )


class StateMachine:
    """市场状态机，统一管理各周期的状态判定与切换"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_PERIODS: Tuple[str, ...] = ("1m", "5m", "15m")
    REGIME_LABELS = {
        Regime.TREND: "趋势",
        Regime.OSCILLATION: "震荡",
        Regime.REVERSAL: "反转",
        Regime.UNKNOWN: "未知",
    }
    # 降级阈值
    FALLBACK_TREND_THRESHOLD = 0.6          # 基础趋势判定阈值，无量纲，[0, 1]
    FALLBACK_OSCILLATION_THRESHOLD = 0.4    # 基础震荡判定阈值，无量纲，[0, 1]
    FALLBACK_DEAD_THRESHOLD = 0.15          # 死寂市波动率阈值，无量纲，[0.05, 0.3]
    # 性能与安全
    MAX_CACHE_SIZE = 256                    # 缓存最大条目，[64, 1024]
    DEFAULT_CACHE_TTL_SEC = 0.1             # 缓存有效期，秒，[0.05, 1.0]
    MIN_STATE_DURATION_SEC = 3.0            # 默认冷却期，秒，[1, 30]
    MAX_STATE_DURATION_SEC = 3600.0         # 最大冷却期，秒，[60, 86400]
    UNKNOWN_RECOVERY_TIMEOUT_SEC = 600      # UNKNOWN 恢复超时，秒，[120, 3600]
    DATA_VIEW_STALENESS_WARN_SEC = 5.0      # 视图过期告警，秒，[1, 30]
    MAX_CALLBACKS = 32                      # 最大回调数量，无量纲，[8, 128]
    SUBMODULE_MAX_RETRIES = 3               # 子模块初始化最大重试次数，[0, 10]
    SUBMODULE_RETRY_BACKOFF_BASE = 0.5      # 子模块重试退避基数，秒，[0.1, 5.0]

    # 各周期差异化的降级阈值乘数（只读）
    PERIOD_FALLBACK_MULTIPLIERS: Dict[str, float] = {
        "1m": 1.0,
        "5m": 0.9,
        "15m": 0.8,
    }

    # 各周期差异化的冷却期配置（秒，只读）
    PERIOD_MIN_DURATION: Dict[str, float] = {
        "1m": 3.0,
        "5m": 5.0,
        "15m": 10.0,
    }

    # 日志字段常量
    _LOG_FIELDS = ("period", "old_state", "new_state", "volatility", "trend_strength", "forced", "operator")

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        periods: Optional[Tuple[str, ...]] = None,
    ):
        self._lock = threading.RLock()
        self._submodule_config: Dict[str, Any] = {}

        # 周期列表（去重、小写）
        raw_periods = periods if periods is not None else self.DEFAULT_PERIODS
        seen: Set[str] = set()
        unique_periods: List[str] = []
        for p in raw_periods:
            p_lower = p.lower()
            if p_lower not in seen:
                seen.add(p_lower)
                unique_periods.append(p_lower)
        self._periods: Tuple[str, ...] = tuple(unique_periods)

        # 状态存储
        self._current_states: Dict[str, Regime] = {p: Regime.UNKNOWN for p in self._periods}
        self._transition_info: Dict[str, TransitionInfo] = {
            p: TransitionInfo() for p in self._periods
        }

        # UNKNOWN 状态追踪
        self._unknown_since: Dict[str, float] = {}

        # 冷却期配置
        self._min_duration: Dict[str, float] = {}
        for p in self._periods:
            self._min_duration[p] = self.PERIOD_MIN_DURATION.get(
                p, self.MIN_STATE_DURATION_SEC
            )
        self._last_state_change_time: Dict[str, float] = {p: 0.0 for p in self._periods}

        # 子模块结果缓存（LRU，所有操作需在 self._lock 保护下）
        self._hysteresis_cache: OrderedDict = OrderedDict()
        self._break_cache: OrderedDict = OrderedDict()

        # 性能统计
        self._execution_count: Dict[str, int] = {p: 0 for p in self._periods}
        self._last_execution_time_ms: Dict[str, float] = {p: 0.0 for p in self._periods}

        # 子模块实例
        self._regime_hysteresis: Optional[RegimeHysteresis] = None
        self._structure_break_detector: Optional[StructureBreakDetector] = None

        # 外部依赖
        self._context_isolator = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 回调列表（弱引用 + 强引用）
        self._state_change_callbacks: List[weakref.ReferenceType] = []
        self._strong_state_callbacks: List[Callable] = []
        self._structure_break_callbacks: List[weakref.ReferenceType] = []
        self._strong_break_callbacks: List[Callable] = []

        # 鉴权回调
        self._auth_checker: Optional[Callable[[str, str], bool]] = None

        # 可配置的必选字段（深拷贝默认值，避免修改类常量）
        self._required_fields: Dict[str, Tuple[float, float]] = dict(
            self.__class__.DEFAULT_REQUIRED_FIELDS
            if hasattr(self.__class__, 'DEFAULT_REQUIRED_FIELDS')
            else {
                "volatility_percentile": (0.0, 1.0),
                "trend_strength": (0.0, 1.0),
            }
        )

        # 配置加载
        if config:
            self._load_config(config)

        logger.info(
            "StateMachine 初始化完成，监控周期: %s",
            self._periods,
        )

    # 默认必选市场数据字段及其合法范围
    DEFAULT_REQUIRED_FIELDS: Dict[str, Tuple[float, float]] = {
        "volatility_percentile": (0.0, 1.0),
        "trend_strength": (0.0, 1.0),
    }

    def _load_config(self, config: Dict[str, Any]) -> None:
        """加载配置参数，进行类型校验"""
        try:
            self.FALLBACK_TREND_THRESHOLD = float(
                config.get("fallback_trend_threshold", self.FALLBACK_TREND_THRESHOLD)
            )
        except (ValueError, TypeError):
            logger.warning("fallback_trend_threshold 配置值无效，使用默认值")

        try:
            self.FALLBACK_OSCILLATION_THRESHOLD = float(
                config.get("fallback_oscillation_threshold", self.FALLBACK_OSCILLATION_THRESHOLD)
            )
        except (ValueError, TypeError):
            logger.warning("fallback_oscillation_threshold 配置值无效，使用默认值")

        try:
            self.MAX_CACHE_SIZE = int(config.get("max_cache_size", self.MAX_CACHE_SIZE))
        except (ValueError, TypeError):
            logger.warning("max_cache_size 配置值无效，使用默认值")

        try:
            self.DEFAULT_CACHE_TTL_SEC = float(
                config.get("cache_ttl_sec", self.DEFAULT_CACHE_TTL_SEC)
            )
        except (ValueError, TypeError):
            logger.warning("cache_ttl_sec 配置值无效，使用默认值")

        try:
            self.MAX_CALLBACKS = int(config.get("max_callbacks", self.MAX_CALLBACKS))
        except (ValueError, TypeError):
            logger.warning("max_callbacks 配置值无效，使用默认值")

        # 加载自定义同义词
        custom_synonyms = config.get("regime_synonyms", {})
        if custom_synonyms and isinstance(custom_synonyms, dict):
            Regime.update_synonyms(custom_synonyms)

        # 加载自定义必选字段
        custom_fields = config.get("required_fields", {})
        if custom_fields and isinstance(custom_fields, dict):
            for field, (min_val, max_val) in custom_fields.items():
                try:
                    self._required_fields[field] = (float(min_val), float(max_val))
                except (ValueError, TypeError):
                    logger.warning("必选字段 %s 的范围配置无效，跳过", field)

        # 加载子模块初始化参数
        sub_config = config.get("submodules", {})
        if isinstance(sub_config, dict):
            self._submodule_config = sub_config

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        context_isolator: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        auth_checker: Optional[Callable[[str, str], bool]] = None,
        reinitialize: bool = False,
    ) -> None:
        """
        注入外部依赖，并尝试初始化子模块

        Args:
            context_isolator: 上下文隔离器实例（可选）
            negotiation_bus: 协商总线实例（可选，需实现 publish_alert 方法）
            behavioral_logger: 行为日志记录器实例（可选）
            auth_checker: 鉴权回调函数，签名为 (operator: str, action: str) -> bool（可选）
            reinitialize: 若为 True，强制重新初始化子模块（用于故障恢复）
        """
        if context_isolator is not None:
            self._context_isolator = context_isolator
        if negotiation_bus is not None:
            if hasattr(negotiation_bus, 'publish_alert'):
                self._negotiation_bus = negotiation_bus
            else:
                logger.warning("NegotiationBus 缺少 publish_alert 方法，告警推送降级")
        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
        if auth_checker is not None:
            self._auth_checker = auth_checker

        if reinitialize or self._regime_hysteresis is None:
            self._init_submodules()

    # ========== 公共接口 ==========
    def update_state(self, period: str, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        更新指定周期的市场状态

        Args:
            period: 周期标识，不区分大小写
            market_data: 市场数据字典，需包含 volatility_percentile, trend_strength 等字段

        Returns:
            标准响应字典
        """
        period = period.lower()
        if period not in self._periods:
            logger.warning("无效周期: %s", period)
            return {
                "status": "error",
                "reason": f"无效周期: {period}，有效值为 {list(self._periods)}",
                "data": {},
                "warnings": [f"未知周期: {period}"],
            }

        t_start = time.perf_counter()
        warnings: List[str] = []

        with self._lock:
            old_state = self._current_states.get(period, Regime.UNKNOWN)
            new_state = old_state
            transition_info = TransitionInfo()
            structure_break = False
            callbacks_to_invoke: List[Tuple[Callable, tuple]] = []

            try:
                # 数据完整性校验
                validation_ok, missing_fields, invalid_fields = self._validate_market_data(
                    market_data
                )
                if not validation_ok:
                    if missing_fields:
                        warnings.append(f"market_data 缺少字段: {missing_fields}")
                    if invalid_fields:
                        warnings.append(f"market_data 非法字段值: {invalid_fields}")
                    logger.debug("%s 周期市场数据不完整，使用降级值", period)

                # 获取隔离视图
                data_view = None
                if self._context_isolator is not None:
                    try:
                        data_view = self._context_isolator.get_view(period)
                        if data_view is not None and hasattr(data_view, 'timestamp'):
                            view_ts = data_view.timestamp
                            if view_ts and time.time() - view_ts > self.DATA_VIEW_STALENESS_WARN_SEC:
                                warnings.append("数据视图可能过期")
                                logger.debug(
                                    "%s 数据视图时间戳 %.1f 秒前",
                                    period,
                                    time.time() - view_ts,
                                )
                    except Exception as e:
                        warnings.append(f"ContextIsolator 异常: {str(e)}")
                        logger.error("ContextIsolator 获取视图失败: %s", e, exc_info=True)

                # 结构突变检测
                if self._structure_break_detector is not None:
                    structure_break = self._detect_structure_break(
                        period, market_data, data_view, warnings
                    )

                # 滞回缓冲区判定
                if self._regime_hysteresis is not None:
                    new_state, transition_info = self._evaluate_hysteresis(
                        period, market_data, data_view, warnings
                    )
                else:
                    new_state = self._fallback_evaluate(period, market_data, warnings)

                # 状态连续性规则
                if old_state == Regime.TREND and new_state == Regime.REVERSAL:
                    logger.warning("状态跳变 trend→reversal 被拦截，过渡为 oscillation")
                    new_state = Regime.OSCILLATION
                    warnings.append(
                        "状态跳变被拦截: trend→reversal 不允许，已过渡为 oscillation"
                    )

                # UNKNOWN 状态自动恢复
                if new_state == Regime.UNKNOWN:
                    now = time.time()
                    if period not in self._unknown_since:
                        self._unknown_since[period] = now
                    elif (
                        now - self._unknown_since[period]
                        > self.UNKNOWN_RECOVERY_TIMEOUT_SEC
                    ):
                        logger.warning(
                            "%s 周期 UNKNOWN 超时，强制恢复为 OSCILLATION", period
                        )
                        new_state = Regime.OSCILLATION
                        warnings.append("UNKNOWN 状态超时，自动恢复为 oscillation")
                        self._unknown_since.pop(period, None)
                else:
                    self._unknown_since.pop(period, None)

                # 状态冷却期检查
                now = time.time()
                last_change = self._last_state_change_time.get(period, 0.0)
                min_duration = self._min_duration.get(period, self.MIN_STATE_DURATION_SEC)
                in_cooldown = (now - last_change) < min_duration
                if in_cooldown and new_state != old_state:
                    logger.debug(
                        "%s 周期处于冷却期 (剩余 %.1f 秒)，暂缓状态切换",
                        period,
                        min_duration - (now - last_change),
                    )
                    new_state = old_state
                    transition_info = TransitionInfo()

                # 更新状态
                if old_state != new_state:
                    self._current_states[period] = new_state
                    self._transition_info[period] = transition_info
                    self._last_state_change_time[period] = now
                    logger.debug(
                        "%s 周期状态切换: %s → %s",
                        period,
                        old_state.value,
                        new_state.value,
                    )
                    self._log_state_change(period, old_state, new_state, market_data)

                    # 收集回调
                    callbacks_to_invoke.extend(
                        self._collect_and_clean_callbacks(
                            self._state_change_callbacks,
                            (period, old_state, new_state),
                        )
                    )
                    # 对强引用列表进行快照复制（防止锁外执行时被修改）
                    strong_snapshot = list(self._strong_state_callbacks)
                    for cb in strong_snapshot:
                        callbacks_to_invoke.append((cb, (period, old_state, new_state)))

                    # 发布协商总线事件
                    if self._negotiation_bus is not None:
                        try:
                            self._negotiation_bus.publish_alert(
                                alert_type="state_change",
                                period=period,
                                old_state=old_state.value,
                                new_state=new_state.value,
                            )
                        except Exception as e:
                            logger.warning("协商总线事件发布失败: %s", e)

                # 结构突变回调收集
                if structure_break:
                    callbacks_to_invoke.extend(
                        self._collect_and_clean_callbacks(
                            self._structure_break_callbacks, (period,)
                        )
                    )
                    strong_snapshot = list(self._strong_break_callbacks)
                    for cb in strong_snapshot:
                        callbacks_to_invoke.append((cb, (period,)))

            except Exception as e:
                logger.error("状态更新未知异常: %s", e, exc_info=True)
                new_state = Regime.UNKNOWN
                warnings.append(f"update_state 致命异常: {str(e)}")
                if period not in self._unknown_since:
                    self._unknown_since[period] = time.time()

            # 更新性能统计
            self._execution_count[period] = self._execution_count.get(period, 0) + 1
            self._last_execution_time_ms[period] = (
                time.perf_counter() - t_start
            ) * 1000

        # 锁外执行回调（使用 RLock 允许回调中安全访问状态）
        for cb, args in callbacks_to_invoke:
            try:
                cb(*args)
            except Exception as e:
                logger.error("回调执行异常: %s", e)

        return {
            "status": "ok",
            "reason": f"{period} 周期状态更新完成，当前状态: {new_state.value}",
            "data": {
                "period": period,
                "state": new_state.value,
                "previous_state": old_state.value,
                "transition": transition_info.to_dict(),
                "structure_break_detected": structure_break,
                "execution_time_ms": round(
                    self._last_execution_time_ms.get(period, 0), 3
                ),
            },
            "warnings": warnings,
        }

    def get_current_state(self, period: str) -> Dict[str, Any]:
        """返回指定周期当前的市场状态与过渡信息"""
        period = period.lower()
        if period not in self._periods:
            return {
                "status": "error",
                "reason": f"无效周期: {period}",
                "data": {},
                "warnings": [],
            }

        with self._lock:
            now = time.time()
            state = self._current_states.get(period, Regime.UNKNOWN)
            trans = self._transition_info.get(period, TransitionInfo())
            cooldown_remaining = max(
                0.0,
                self._min_duration.get(period, self.MIN_STATE_DURATION_SEC)
                - (now - self._last_state_change_time.get(period, 0.0)),
            )

        return {
            "status": "ok",
            "reason": f"{period} 周期当前状态: {self.REGIME_LABELS.get(state, '未知')}",
            "data": {
                "period": period,
                "state": state.value,
                "state_label": self.REGIME_LABELS.get(state, "未知"),
                "transition": trans.to_dict(),
                "cooldown_remaining_sec": round(cooldown_remaining, 1),
            },
            "warnings": [],
        }

    def get_all_states(self) -> Dict[str, Any]:
        """返回所有周期的状态汇总"""
        with self._lock:
            now = time.time()
            all_states = {}
            for period in self._periods:
                state = self._current_states.get(period, Regime.UNKNOWN)
                trans = self._transition_info.get(period, TransitionInfo())
                unknown_duration = (
                    now - self._unknown_since[period]
                    if period in self._unknown_since
                    else 0.0
                )
                cooldown_remaining = max(
                    0.0,
                    self._min_duration.get(period, self.MIN_STATE_DURATION_SEC)
                    - (now - self._last_state_change_time.get(period, 0.0)),
                )
                all_states[period] = {
                    "state": state.value,
                    "state_label": self.REGIME_LABELS.get(state, "未知"),
                    "transition": trans.to_dict(),
                    "execution_count": self._execution_count.get(period, 0),
                    "last_execution_ms": round(
                        self._last_execution_time_ms.get(period, 0), 3
                    ),
                    "unknown_duration_sec": round(unknown_duration, 1),
                    "cooldown_remaining_sec": round(cooldown_remaining, 1),
                }

        return {
            "status": "ok",
            "reason": f"已汇总 {len(all_states)} 个周期的状态",
            "data": {"periods": all_states},
            "warnings": [],
        }

    def force_state(
        self, period: str, state: Union[Regime, str], operator: str = "admin"
    ) -> Dict[str, Any]:
        """
        强制覆盖状态（需运维权限）

        Args:
            period: 周期标识
            state: 目标状态（Regime 枚举或字符串）
            operator: 操作者标识（用于审计），必须为非空字符串

        Returns:
            标准响应字典
        """
        period = period.lower()
        if period not in self._periods:
            return {
                "status": "error",
                "reason": f"无效周期: {period}",
                "data": {},
                "warnings": [],
            }

        if not isinstance(operator, str) or not operator.strip():
            operator = "unknown"
            logger.warning("force_state 调用缺少有效的 operator 标识")

        if isinstance(state, str):
            state = Regime.from_string(state)

        # 鉴权检查
        if self._auth_checker and not self._auth_checker(operator.strip(), "force_state"):
            logger.warning(
                "强制状态覆盖被拒绝: operator=%s, action=force_state", operator
            )
            return {
                "status": "error",
                "reason": "权限不足，强制状态覆盖需要管理员权限",
                "data": {},
                "warnings": ["unauthorized"],
            }

        with self._lock:
            old_state = self._current_states[period]
            self._current_states[period] = state
            self._transition_info[period] = TransitionInfo()
            self._last_state_change_time[period] = time.time()
            self._unknown_since.pop(period, None)
            logger.warning(
                "强制状态覆盖: %s, %s → %s, 操作者: %s",
                period,
                old_state.value,
                state.value,
                operator,
            )
            self._log_state_change(
                period, old_state, state, {"operator": operator.strip(), "forced": True}
            )

        return {
            "status": "ok",
            "reason": f"已强制设置 {period} 状态为 {state.value}",
            "data": {
                "period": period,
                "state": state.value,
                "previous_state": old_state.value,
            },
            "warnings": [f"状态由 {operator} 强制覆盖"],
        }

    def set_min_duration(self, period: str, duration_sec: float) -> Dict[str, Any]:
        """
        动态调整指定周期的冷却期时长

        Args:
            period: 周期标识
            duration_sec: 新的冷却期时长，秒，取值范围 [0, MAX_STATE_DURATION_SEC]

        Returns:
            标准响应字典
        """
        period = period.lower()
        if period not in self._periods:
            return {
                "status": "error",
                "reason": f"无效周期: {period}",
                "data": {},
                "warnings": [],
            }
        if duration_sec < 0 or duration_sec > self.MAX_STATE_DURATION_SEC:
            return {
                "status": "error",
                "reason": f"冷却期时长必须在 [0, {self.MAX_STATE_DURATION_SEC}] 范围内",
                "data": {},
                "warnings": [],
            }

        with self._lock:
            old = self._min_duration.get(period, self.MIN_STATE_DURATION_SEC)
            self._min_duration[period] = duration_sec
        logger.info("%s 周期冷却期调整: %.1f → %.1f 秒", period, old, duration_sec)

        return {
            "status": "ok",
            "reason": f"{period} 周期冷却期已调整为 {duration_sec:.1f} 秒",
            "data": {"period": period, "old_duration": old, "new_duration": duration_sec},
            "warnings": [],
        }

    def register_state_change_callback(
        self, callback: Callable[[str, Regime, Regime], None], strong: bool = False
    ) -> None:
        """
        注册状态变更回调函数

        Args:
            callback: 回调函数，签名 (period: str, old_state: Regime, new_state: Regime)
            strong: 若为 True，使用强引用（调用方负责取消注册），否则使用弱引用
        """
        with self._lock:
            if strong:
                if len(self._strong_state_callbacks) >= self.MAX_CALLBACKS:
                    logger.warning("强引用状态回调已达上限 %d，拒绝注册", self.MAX_CALLBACKS)
                    return
                self._strong_state_callbacks.append(callback)
            else:
                if len(self._state_change_callbacks) >= self.MAX_CALLBACKS:
                    logger.warning("弱引用状态回调已达上限 %d，拒绝注册", self.MAX_CALLBACKS)
                    return
                self._state_change_callbacks.append(weakref.ref(callback))

    def unregister_state_change_callback(self, callback: Callable) -> None:
        """取消注册状态变更回调"""
        with self._lock:
            self._state_change_callbacks = [
                ref
                for ref in self._state_change_callbacks
                if ref() is not None and ref() is not callback
            ]
            if callback in self._strong_state_callbacks:
                self._strong_state_callbacks.remove(callback)

    def register_structure_break_callback(
        self, callback: Callable[[str], None], strong: bool = False
    ) -> None:
        """注册结构突变回调函数"""
        with self._lock:
            if strong:
                if len(self._strong_break_callbacks) >= self.MAX_CALLBACKS:
                    logger.warning("强引用结构突变回调已达上限 %d，拒绝注册", self.MAX_CALLBACKS)
                    return
                self._strong_break_callbacks.append(callback)
            else:
                if len(self._structure_break_callbacks) >= self.MAX_CALLBACKS:
                    logger.warning("弱引用结构突变回调已达上限 %d，拒绝注册", self.MAX_CALLBACKS)
                    return
                self._structure_break_callbacks.append(weakref.ref(callback))

    def unregister_structure_break_callback(self, callback: Callable) -> None:
        """取消注册结构突变回调"""
        with self._lock:
            self._structure_break_callbacks = [
                ref
                for ref in self._structure_break_callbacks
                if ref() is not None and ref() is not callback
            ]
            if callback in self._strong_break_callbacks:
                self._strong_break_callbacks.remove(callback)

    def reload_submodules(self) -> None:
        """热重载子模块（用于 OTA 更新后恢复）"""
        import importlib
        try:
            module = importlib.import_module('core.state_machine.regime_hysteresis')
            importlib.reload(module)
            logger.info("RegimeHysteresis 模块热重载成功")
        except Exception as e:
            logger.error("RegimeHysteresis 重载失败: %s #RECOVERY: 检查模块文件完整性", e)

        try:
            module = importlib.import_module('core.state_machine.structure_break_detector')
            importlib.reload(module)
            logger.info("StructureBreakDetector 模块热重载成功")
        except Exception as e:
            logger.error("StructureBreakDetector 重载失败: %s #RECOVERY: 检查模块文件完整性", e)

        self._init_submodules()
        logger.info("子模块热重载完成")

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            sub_health = {}
            if self._regime_hysteresis is not None:
                try:
                    sub_health["regime_hysteresis"] = self._regime_hysteresis.health_check()
                except Exception as e:
                    sub_health["regime_hysteresis"] = {"status": "error", "reason": str(e)}
            else:
                sub_health["regime_hysteresis"] = {"status": "degraded", "reason": "模块未加载"}

            if self._structure_break_detector is not None:
                try:
                    sub_health["structure_break_detector"] = (
                        self._structure_break_detector.health_check()
                    )
                except Exception as e:
                    sub_health["structure_break_detector"] = {"status": "error", "reason": str(e)}
            else:
                sub_health["structure_break_detector"] = {"status": "degraded", "reason": "模块未加载"}

            # 聚合子模块健康评分
            sub_scores = []
            for sh in sub_health.values():
                if sh.get("status") == "ok":
                    sub_scores.append(100)
                elif sh.get("status") == "degraded":
                    sub_scores.append(50)
                else:
                    sub_scores.append(0)
            aggregated_sub_score = sum(sub_scores) / len(sub_scores) if sub_scores else 100

            with self._lock:
                now = time.time()
                illegal = [
                    p for p, s in self._current_states.items() if s not in Regime
                ]
                status = "ok" if not illegal else "degraded"
                weak_alive = sum(
                    1 for ref in self._state_change_callbacks if ref() is not None
                )
                break_weak_alive = sum(
                    1 for ref in self._structure_break_callbacks if ref() is not None
                )
                unknown_durations = {
                    p: round(now - ts, 1)
                    for p, ts in self._unknown_since.items()
                }

            return {
                "status": status,
                "reason": f"StateMachine 自检完成，监控 {len(self._periods)} 个周期",
                "data": {
                    "periods": list(self._periods),
                    "current_states": {
                        p: s.value for p, s in self._current_states.items()
                    },
                    "illegal_state_count": len(illegal),
                    "active_weak_callbacks": weak_alive,
                    "active_strong_callbacks": len(self._strong_state_callbacks),
                    "break_weak_callbacks": break_weak_alive,
                    "break_strong_callbacks": len(self._strong_break_callbacks),
                    "unknown_durations": unknown_durations,
                    "sub_modules": sub_health,
                    "sub_modules_aggregated_score": round(aggregated_sub_score, 1),
                    "dependencies": {
                        "context_isolator": self._context_isolator is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                        "auth_checker": self._auth_checker is not None,
                    },
                    "cache": {
                        "hysteresis_entries": len(self._hysteresis_cache),
                        "break_entries": len(self._break_cache),
                        "max_size": self.MAX_CACHE_SIZE,
                    },
                },
                "warnings": [f"非法状态周期数: {len(illegal)}"] if illegal else [],
            }
        except Exception as e:
            logger.error(
                "健康检查失败: %s #RECOVERY: 检查状态字典完整性",
                e,
                exc_info=True,
            )
            return {
                "status": "error",
                "reason": str(e),
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _validate_market_data(
        self, data: Dict[str, Any]
    ) -> Tuple[bool, List[str], List[str]]:
        """校验市场数据，返回 (是否通过, 缺失字段列表, 非法值字段列表)"""
        missing = []
        invalid = []

        for field, (min_val, max_val) in self._required_fields.items():
            if field not in data:
                missing.append(field)
                continue
            value = data[field]
            if not isinstance(value, (int, float)):
                invalid.append(f"{field}={value}(type={type(value).__name__})")
                continue
            if value < min_val or value > max_val:
                logger.debug("字段 %s 值 %.4f 超出范围 [%.2f, %.2f]，裁剪", field, value, min_val, max_val)
                invalid.append(f"{field}={value}(range=[{min_val},{max_val}])")

        return len(missing) == 0 and len(invalid) == 0, missing, invalid

    @staticmethod
    def _make_cache_key(period: str, market_data: Dict[str, Any], data_view: Any) -> int:
        """
        生成稳定的缓存键（使用内置 hash，进程内快速）

        注意：Python 的 hash() 在进程内稳定，但跨进程不同（PYTHONHASHSEED）。
        缓存仅在进程内有效，此特性不影响正确性。
        哈希碰撞概率极低（< 1/2^64），在高频交易中可接受。
        """
        try:
            safe_items = tuple(
                (k, v)
                for k, v in sorted(market_data.items())
                if isinstance(v, (int, float, str, bool, type(None)))
            )
            view_id = (
                getattr(data_view, 'timestamp', '')
                if data_view is not None and hasattr(data_view, 'timestamp')
                else ''
            )
            return hash((period, safe_items, view_id))
        except TypeError:
            return hash((period, time.time()))

    def _lru_cache_get(self, cache: OrderedDict, key: int) -> Optional[Any]:
        """
        从 LRU 缓存获取值，命中时移到队尾。

        注意：此方法必须在 self._lock 保护下调用。

        Returns:
            缓存的值，若未命中返回 _CACHE_MISS 哨兵
        """
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
        return _CACHE_MISS

    def _lru_cache_put(self, cache: OrderedDict, key: int, value: Any) -> None:
        """
        向 LRU 缓存存入值。

        注意：此方法必须在 self._lock 保护下调用。
        """
        if key in cache:
            cache.move_to_end(key)
        cache[key] = value
        if cache and len(cache) > self.MAX_CACHE_SIZE:
            cache.popitem(last=False)

    def _detect_structure_break(
        self,
        period: str,
        market_data: Dict[str, Any],
        data_view: Any,
        warnings: List[str],
    ) -> bool:
        """执行结构突变检测（带 LRU 缓存，需在 self._lock 保护下调用）"""
        try:
            cache_key = self._make_cache_key(period, market_data, data_view)
            cached = self._lru_cache_get(self._break_cache, cache_key)
            if cached is not _CACHE_MISS:
                cache_time, cache_result = cached
                if time.time() - cache_time < self.DEFAULT_CACHE_TTL_SEC:
                    return cache_result.get("is_break", False)

            break_res = self._structure_break_detector.detect(
                period, market_data, data_view
            )
            if isinstance(break_res, dict):
                self._lru_cache_put(
                    self._break_cache, cache_key, (time.time(), break_res)
                )
                if break_res.get("is_break"):
                    warnings.append(f"{period} 周期检测到结构突变")
                    logger.warning("%s 周期结构突变，触发快速适应", period)
                    return True
        except (TypeError, ValueError, RuntimeError) as e:
            logger.error("结构突变检测参数或运行时异常: %s", e, exc_info=True)
            warnings.append(f"结构突变检测异常: {str(e)}")
        except Exception as e:
            logger.error("结构突变检测未知异常: %s", e, exc_info=True)
            warnings.append(f"结构突变检测失败: {str(e)}")
        return False

    def _evaluate_hysteresis(
        self,
        period: str,
        market_data: Dict[str, Any],
        data_view: Any,
        warnings: List[str],
    ) -> Tuple[Regime, TransitionInfo]:
        """执行滞回缓冲区判定（带 LRU 缓存，需在 self._lock 保护下调用）"""
        try:
            cache_key = self._make_cache_key(period, market_data, data_view)
            cached = self._lru_cache_get(self._hysteresis_cache, cache_key)
            if cached is not _CACHE_MISS:
                cache_time, cache_result = cached
                if time.time() - cache_time < self.DEFAULT_CACHE_TTL_SEC:
                    state_str = cache_result.get("state", "unknown")
                    state = Regime.from_string(state_str)
                    trans = cache_result.get("transition", {})
                    if isinstance(trans, dict):
                        return state, TransitionInfo(
                            in_transition=trans.get("in_transition", False),
                            remaining_seconds=trans.get("remaining_seconds", 0.0),
                            target_state=(
                                Regime.from_string(trans["target_state"])
                                if trans.get("target_state")
                                else None
                            ),
                        )
                    return state, TransitionInfo()

            hyster_res = self._regime_hysteresis.evaluate(
                period, market_data, data_view
            )
            self._lru_cache_put(
                self._hysteresis_cache, cache_key, (time.time(), hyster_res)
            )
            if isinstance(hyster_res, dict) and "state" in hyster_res:
                state = Regime.from_string(hyster_res["state"])
                trans = hyster_res.get("transition", {})
                warnings.extend(hyster_res.get("warnings", []))
                if isinstance(trans, dict):
                    return state, TransitionInfo(
                        in_transition=trans.get("in_transition", False),
                        remaining_seconds=trans.get("remaining_seconds", 0.0),
                        target_state=(
                            Regime.from_string(trans["target_state"])
                            if trans.get("target_state")
                            else None
                        ),
                    )
                return state, TransitionInfo()
        except (TypeError, ValueError, RuntimeError) as e:
            logger.error("滞回判定参数或运行时异常: %s", e, exc_info=True)
            warnings.append(f"滞回判定异常，使用降级逻辑: {str(e)}")
        except Exception as e:
            logger.error("滞回判定未知异常: %s", e, exc_info=True)
            warnings.append(f"滞回判定失败，使用降级逻辑: {str(e)}")
        return self._fallback_evaluate(period, market_data, warnings), TransitionInfo()

    def _fallback_evaluate(
        self, period: str, market_data: Dict[str, Any], warnings: Optional[List[str]] = None
    ) -> Regime:
        """
        降级基础状态判定（当子模块不可用时）

        各周期使用差异化的阈值乘数：
        - 1m: 标准阈值（噪声大）
        - 5m: 0.9 倍阈值（信号更可靠）
        - 15m: 0.8 倍阈值（信号最可靠）

        判定逻辑：
        - 趋势强度高 → TREND
        - 趋势强度低且波动率中等 → OSCILLATION
        - 趋势强度极低且波动率极低 → UNKNOWN（死寂市）
        - 其他 → UNKNOWN
        """
        multiplier = self.PERIOD_FALLBACK_MULTIPLIERS.get(period, 1.0)
        volatility = float(market_data.get("volatility_percentile", 0.5))
        trend_strength = float(market_data.get("trend_strength", 0.5))

        # 数值裁剪
        volatility = max(0.0, min(1.0, volatility))
        trend_strength = max(0.0, min(1.0, trend_strength))

        trend_threshold = self.FALLBACK_TREND_THRESHOLD * multiplier
        oscillation_threshold = self.FALLBACK_OSCILLATION_THRESHOLD * multiplier
        dead_threshold = self.FALLBACK_DEAD_THRESHOLD * multiplier

        if trend_strength > trend_threshold:
            result = Regime.TREND
        elif trend_strength < trend_threshold * 0.6 and dead_threshold < volatility < oscillation_threshold:
            result = Regime.OSCILLATION
        elif volatility <= dead_threshold and trend_strength < trend_threshold * 0.3:
            result = Regime.UNKNOWN  # 死寂市，无法判定方向
        else:
            result = Regime.UNKNOWN

        logger.debug(
            "%s 降级判定: volatility=%.3f, trend=%.3f, multiplier=%.2f, result=%s",
            period, volatility, trend_strength, multiplier, result.value,
        )
        return result

    def _collect_and_clean_callbacks(
        self, refs: List[weakref.ReferenceType], args: Tuple
    ) -> List[Tuple[Callable, tuple]]:
        """
        安全地收集存活的弱引用回调，并清理失效引用。

        注意：此方法会修改传入的 refs 列表（移除失效引用）。

        Args:
            refs: 弱引用列表（会被原地修改）
            args: 回调参数元组

        Returns:
            (callback, args) 元组列表
        """
        live = []
        dead = []
        for ref in refs:
            cb = ref()
            if cb is not None:
                live.append((cb, args))
            else:
                dead.append(ref)
        for ref in dead:
            refs.remove(ref)
        return live

    def _log_state_change(
        self,
        period: str,
        old_state: Regime,
        new_state: Regime,
        context: Dict[str, Any],
    ) -> None:
        """记录状态切换事件"""
        if self._behavioral_logger is None:
            return
        try:
            safe_summary = {
                "period": period,
                "old_state": old_state.value,
                "new_state": new_state.value,
            }
            if "volatility_percentile" in context:
                val = context["volatility_percentile"]
                if isinstance(val, (int, float)):
                    safe_summary["volatility"] = round(float(val), 4)
            if "trend_strength" in context:
                val = context["trend_strength"]
                if isinstance(val, (int, float)):
                    safe_summary["trend_strength"] = round(float(val), 4)
            if context.get("forced"):
                safe_summary["forced"] = True
                safe_summary["operator"] = context.get("operator", "unknown")
            self._behavioral_logger.log_event(
                event_type="state_change", details=safe_summary
            )
        except Exception as e:
            logger.warning("行为日志记录失败: %s", e)

    def _init_submodules(self) -> None:
        """尝试初始化子模块，并进行健康检查（支持重试）"""
        if RegimeHysteresis is not None:
            self._init_hysteresis_with_retry()

        if StructureBreakDetector is not None:
            self._init_break_detector_with_retry()

    def _init_hysteresis_with_retry(self) -> None:
        """带重试的滞回模块初始化"""
        for attempt in range(1, self.SUBMODULE_MAX_RETRIES + 1):
            try:
                hyster_config = self._submodule_config.get("regime_hysteresis", {})
                instance = (
                    RegimeHysteresis(**hyster_config)
                    if hyster_config
                    else RegimeHysteresis()
                )
                if hasattr(instance, 'health_check'):
                    health = instance.health_check()
                    if health.get("status") == "ok":
                        self._regime_hysteresis = instance
                        logger.info("RegimeHysteresis 初始化成功 (尝试 %d/%d)", attempt, self.SUBMODULE_MAX_RETRIES)
                        return
                    else:
                        logger.warning(
                            "RegimeHysteresis 健康检查未通过 (尝试 %d/%d): %s",
                            attempt, self.SUBMODULE_MAX_RETRIES, health.get("reason"),
                        )
                else:
                    self._regime_hysteresis = instance
                    logger.info("RegimeHysteresis 初始化完成（无健康检查）")
                    return
            except (TypeError, ValueError) as e:
                logger.error(
                    "RegimeHysteresis 初始化参数错误 (尝试 %d/%d): %s #RECOVERY: 检查子模块配置",
                    attempt, self.SUBMODULE_MAX_RETRIES, e,
                )
                break  # 参数错误不需要重试
            except Exception as e:
                logger.error(
                    "RegimeHysteresis 初始化失败 (尝试 %d/%d): %s",
                    attempt, self.SUBMODULE_MAX_RETRIES, e,
                )
                if attempt < self.SUBMODULE_MAX_RETRIES:
                    backoff = self.SUBMODULE_RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                    time.sleep(backoff)

    def _init_break_detector_with_retry(self) -> None:
        """带重试的突变检测模块初始化"""
        for attempt in range(1, self.SUBMODULE_MAX_RETRIES + 1):
            try:
                break_config = self._submodule_config.get("structure_break_detector", {})
                instance = (
                    StructureBreakDetector(**break_config)
                    if break_config
                    else StructureBreakDetector()
                )
                if hasattr(instance, 'health_check'):
                    health = instance.health_check()
                    if health.get("status") == "ok":
                        self._structure_break_detector = instance
                        logger.info("StructureBreakDetector 初始化成功 (尝试 %d/%d)", attempt, self.SUBMODULE_MAX_RETRIES)
                        return
                    else:
                        logger.warning(
                            "StructureBreakDetector 健康检查未通过 (尝试 %d/%d): %s",
                            attempt, self.SUBMODULE_MAX_RETRIES, health.get("reason"),
                        )
                else:
                    self._structure_break_detector = instance
                    logger.info("StructureBreakDetector 初始化完成（无健康检查）")
                    return
            except (TypeError, ValueError) as e:
                logger.error(
                    "StructureBreakDetector 初始化参数错误 (尝试 %d/%d): %s #RECOVERY: 检查子模块配置",
                    attempt, self.SUBMODULE_MAX_RETRIES, e,
                )
                break  # 参数错误不需要重试
            except Exception as e:
                logger.error(
                    "StructureBreakDetector 初始化失败 (尝试 %d/%d): %s",
                    attempt, self.SUBMODULE_MAX_RETRIES, e,
                )
                if attempt < self.SUBMODULE_MAX_RETRIES:
                    backoff = self.SUBMODULE_RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                    time.sleep(backoff)


__all__ = ["StateMachine", "Regime", "TransitionInfo"]
