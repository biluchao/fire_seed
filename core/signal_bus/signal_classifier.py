"""
火种系统 · 信号分类器 (SignalClassifier)

核心职责：
1. 根据信号类型（如 pipeline_advance、profit_compression_trigger 等）将其映射到对应的四车道（极速/快速/普通/慢速）及优先级
2. 提供信号优先级查询接口，为车道调度器提供决策依据；内置信号分类统计与异常模式检测

外部依赖（真实模块接口）：
- 无强制外部依赖。可选依赖 NegotiationBus 用于动态配置热更新，未注入时使用内置静态映射表。
- 可选依赖 ConfigLoader 用于从配置文件加载信号映射，加载失败时自动回退到内置默认映射。

接口契约：
- classify(signal_type: str) -> Dict[str, Any] : 返回信号的车道归属、优先级及分类说明
- health_check() -> Dict[str, Any] : 模块自检，包含映射完整性、核心信号存在性、统计数据
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])
- 错误返回固定包含 "error_code" (str)，所有错误码在类常量 ERROR_CODES 中声明

异常与降级：
- 生存级信号（优先级≥9）使用不可变快照 + 快速路径，确保原子性读取和确定性延迟
- 生存级信号快速路径直接构建新字典返回，无堆分配模板，消除浅拷贝竞态和 GC 压力
- 动态配置更新时，强制保护生存级信号不被覆盖或删除；若检测到优先级违规，拒绝应用更新并保持当前配置
- 系统启动时强制校验生存级信号优先级，违规则拒绝启动
- 当传入未知信号类型时，自动降级为普通车道（优先级 P2），并记录 WARNING 日志
- 配置文件加载失败时，自动回退到内置静态映射表
- 动态配置更新通过线程锁保护，确保与 classify 读取不产生竞态条件；所有快照读取均加锁，确保内存可见性
- 所有降级值在类常量区明确声明

资源管理：
- 本模块为纯逻辑分类器，不持有任何外部资源
- 统计数据使用 Counter 存储，通过 stats_lock 保护，设有容量上限和定期淘汰机制
- 定期由 health_check 暴露统计快照
"""

import logging
import threading
import time
from collections import Counter
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class SignalClassifier:
    """信号分类器：将信号类型映射到四车道及优先级"""

    # ========== 错误码定义 ==========
    ERROR_CODES = {
        "SIG_CLASS_001": "无效的信号类型参数（空字符串或非字符串类型）",
        "SIG_CLASS_002": "信号映射表存在无效条目（车道或优先级不合法）",
        "SIG_CLASS_003": "健康检查内部异常",
    }

    # ========== 核心生存级信号列表（健康检查时验证必须存在） ==========
    CRITICAL_SIGNALS = [
        "emergency_close",
        "circuit_breaker_trigger",
        "pipeline_advance",
        "profit_compression_trigger",
        "stop_loss_update",
    ]

    # 生存级信号最低优先级阈值
    CRITICAL_MIN_PRIORITY = 9

    # ========== 静态信号类型 -> (车道, 优先级) 映射表 ==========
    # 车道取值: "express", "fast", "normal", "slow"
    # 优先级取值: 0-10, 10为最高（生存级）
    DEFAULT_SIGNAL_MAP: Dict[str, Tuple[str, int]] = {
        # ---- 极速车道 (P0级，生存与核心交易链路) ----
        "emergency_close": ("express", 10),
        "circuit_breaker_trigger": ("express", 10),
        "pipeline_advance": ("express", 9),
        "profit_compression_trigger": ("express", 9),
        "order_exec_confirm": ("express", 8),
        "stop_loss_update": ("express", 8),
        "position_health_warning": ("express", 8),
        "liquidity_rating_change": ("express", 9),
        "risk_color_change": ("express", 8),
        # 策略执行信号
        "fire_seed_1m_signal_confirm": ("express", 8),
        "fire_seed_1m_position_open": ("express", 9),
        "fire_seed_1m_position_close": ("express", 10),
        "event_driven_entry": ("express", 8),
        "market_making_quote_update": ("express", 9),
        "oscillation_boundary_break": ("express", 8),

        # ---- 快速车道 (P1级，实时辅助决策) ----
        "agent_proposal_fast": ("fast", 6),
        "add_position_check": ("fast", 5),
        "signal_health_alert": ("fast", 5),
        "fire_seed_5m_signal_confirm": ("fast", 7),
        "fire_seed_5m_band_switch": ("fast", 6),
        "oscillation_grid_trigger": ("fast", 6),
        "event_driven_preload": ("fast", 6),
        "market_making_inventory_hedge": ("fast", 7),

        # ---- 普通车道 (P2级，进化与调度) ----
        "factor_ic_update": ("normal", 4),
        "evolution_population_eval": ("normal", 3),
        "shadow_validation_result": ("normal", 3),
        "parameter_drift_alert": ("normal", 4),
        "fire_seed_15m_signal_confirm": ("normal", 5),
        "fire_seed_15m_macro_confirm": ("normal", 4),

        # ---- 慢速车道 (P3级，异步批量处理) ----
        "behavioral_log": ("slow", 1),
        "ops_report": ("slow", 1),
        "cloud_audit_upload": ("slow", 1),
        "experience_sync": ("slow", 2),
    }

    # 默认降级配置
    DEFAULT_LANE = "normal"     # 未知信号默认车道
    DEFAULT_PRIORITY = 4        # 未知信号默认优先级，取值范围 [0, 10]

    # ========== 统计计数器容量限制 ==========
    _MAX_CLASSIFY_STATS_SIZE = 2000        # 分类统计最大条目数
    _MAX_UNKNOWN_STATS_SIZE = 500          # 未知信号统计最大条目数
    _CLASSIFY_STATS_TRIM_SIZE = 1000       # 分类统计触发清理时保留的条目数
    _UNKNOWN_STATS_TRIM_SIZE = 250         # 未知信号统计触发清理时保留的条目数

    def __init__(self):
        # 信号映射表：优先使用配置文件，其次使用默认静态映射
        self._signal_map = self._load_static_config()

        # 生存级信号快速查找集合（确定性延迟）
        self._critical_set: frozenset = self._build_critical_set(self._signal_map)

        # 不可变快照：确保 _signal_map 和 _critical_set 原子性更新
        self._snapshot: Tuple[Dict[str, Tuple[str, int]], frozenset] = (
            self._signal_map,
            self._critical_set,
        )

        # 线程锁保护快照的更新与读取
        self._snapshot_lock = threading.Lock()

        # 统计计数器
        self._stats_lock = threading.Lock()
        self._classify_stats: Counter = Counter()
        self._unknown_stats: Counter = Counter()
        self._last_unknown_alert: Dict[str, float] = {}
        self._unknown_alert_threshold = 10       # 30秒内未知信号超过此次数触发告警
        self._unknown_alert_window = 30          # 告警去重窗口，秒

        # 外部依赖（可选）
        self._negotiation_bus = None

        # 启动时自检：验证生存级信号优先级
        violations = self._validate_critical_priority(self._signal_map)
        if violations:
            logger.error(
                f"启动失败！生存级信号优先级违规: {violations}. "
                f"系统将拒绝启动。"
                f"#RECOVERY: 修复配置文件中的信号优先级设置。"
            )
            raise SystemError(f"Critical signal priority violation: {violations}")

        logger.info(
            "SignalClassifier 初始化完成，已加载 %d 条信号映射规则，"
            "其中生存级信号 %d 个",
            len(self._signal_map), len(self._critical_set)
        )

    # ========== 依赖注入 ==========
    def inject_dependencies(self, negotiation_bus: Optional[Any] = None) -> None:
        """
        注入外部依赖（可选注入，未注入时使用静态映射表）

        Args:
            negotiation_bus: 协商总线实例，用于获取动态信号分类配置
        """
        if negotiation_bus is not None:
            self._negotiation_bus = negotiation_bus
            logger.info("NegotiationBus 注入成功，支持动态信号分类配置")
            self._load_dynamic_config()
        else:
            logger.info("NegotiationBus 未注入，使用内置静态信号映射表")

    # ========== 公共接口 ==========
    def classify(self, signal_type: str) -> Dict[str, Any]:
        """
        对信号进行分类，返回其归属车道和优先级

        Args:
            signal_type: 信号类型标识符（如 'pipeline_advance', 'factor_ic_update'）

        Returns:
            标准响应字典，data 中包含 lane (str), priority (int), is_known (bool)
        """
        # 参数校验
        if not signal_type or not isinstance(signal_type, str):
            logger.warning(
                "无效的信号类型参数: %s "
                "#RECOVERY: 检查信号发送方是否正确构造了信号类型标识符",
                signal_type
            )
            return {
                "status": "error",
                "error_code": "SIG_CLASS_001",
                "reason": f"信号类型必须为非空字符串，当前值: {signal_type}",
                "data": {
                    "lane": self.DEFAULT_LANE,
                    "priority": self.DEFAULT_PRIORITY,
                    "is_known": False,
                },
                "warnings": ["invalid_signal_type"],
            }

        # 原子读取当前快照（加锁确保内存可见性）
        with self._snapshot_lock:
            current_map, current_critical = self._snapshot

        # 生存级信号快速路径（确定性延迟，frozenset O(1) 查找无退化风险）
        if signal_type in current_critical:
            lane, priority = current_map.get(
                signal_type, (self.DEFAULT_LANE, self.DEFAULT_PRIORITY)
            )
            return {
                "status": "ok",
                "reason": f"信号 '{signal_type}' 分类为 {lane} 车道, 优先级 {priority}",
                "data": {
                    "lane": lane,
                    "priority": priority,
                    "is_known": True,
                },
                "warnings": [],
            }

        # 普通信号路径：在锁外进行字典查找
        if signal_type in current_map:
            lane, priority = current_map[signal_type]
            is_known = True
        else:
            lane = self.DEFAULT_LANE
            priority = self.DEFAULT_PRIORITY
            is_known = False

        # 统计更新（仅对非生存级信号执行，避免锁竞争污染关键路径）
        self._update_stats(signal_type, is_known)

        if is_known:
            logger.debug(
                "信号分类: type=%s -> lane=%s, priority=%d",
                signal_type, lane, priority
            )
        else:
            logger.warning(
                "未知信号类型: %s，降级为默认车道 %s (优先级 %d) "
                "#RECOVERY: 1.检查信号发送方是否使用了正确的信号类型标识 "
                "2.如需添加新信号类型，更新 config/signal_classification.yaml 映射表",
                signal_type, lane, priority
            )

        return {
            "status": "ok",
            "reason": f"信号 '{signal_type}' 分类为 {lane} 车道, 优先级 {priority}",
            "data": {
                "lane": lane,
                "priority": priority,
                "is_known": is_known,
            },
            "warnings": [] if is_known else [f"unknown_signal_type: {signal_type}"],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检：验证信号映射表完整性、核心信号存在性、依赖状态、统计数据

        Returns:
            标准健康检查响应字典
        """
        priority_violations: List[str] = []
        try:
            # 原子读取快照
            with self._snapshot_lock:
                current_map, current_critical = self._snapshot
                map_size = len(current_map)
                signal_items = list(current_map.items())
                signal_keys = list(current_map.keys())

            # 验证映射表中所有条目的车道和优先级是否有效
            valid_lanes = {"express", "fast", "normal", "slow"}
            invalid_entries = []
            for sig_type, (lane, priority) in signal_items:
                if lane not in valid_lanes:
                    invalid_entries.append(
                        f"{sig_type}: invalid lane '{lane}'"
                    )
                if not isinstance(priority, int) or priority < 0 or priority > 10:
                    invalid_entries.append(
                        f"{sig_type}: invalid priority {priority}"
                    )

            if invalid_entries:
                return {
                    "status": "degraded",
                    "error_code": "SIG_CLASS_002",
                    "reason": f"信号映射表存在 {len(invalid_entries)} 条无效条目",
                    "data": {"invalid_entries": invalid_entries},
                    "warnings": invalid_entries,
                }

            # 验证核心生存级信号是否缺失
            missing_critical = [
                s for s in self.CRITICAL_SIGNALS if s not in signal_keys
            ]
            if missing_critical:
                return {
                    "status": "critical",
                    "reason": f"核心生存级信号缺失: {missing_critical}",
                    "data": {"missing_signals": missing_critical},
                    "warnings": [
                        f"critical_signal_missing: {', '.join(missing_critical)}"
                    ],
                }

            # 验证生存级信号优先级是否满足最低要求
            priority_violations = self._validate_critical_priority(current_map)
            if priority_violations:
                logger.error(
                    f"生存级信号优先级不足: {priority_violations} "
                    f"#RECOVERY: 检查信号映射配置，确保生存级信号优先级>="
                    f"{self.CRITICAL_MIN_PRIORITY}"
                )

            # 收集统计数据
            with self._stats_lock:
                top_unknown = self._unknown_stats.most_common(5)
                total_unknown = sum(self._unknown_stats.values())
                total_classified = sum(self._classify_stats.values())

            return {
                "status": "ok",
                "reason": f"SignalClassifier 正常，已加载 {map_size} 条信号映射规则",
                "data": {
                    "map_size": map_size,
                    "critical_signals_ok": True,
                    "critical_set_size": len(current_critical),
                    "priority_violations": priority_violations,
                    "stats": {
                        "total_classified": total_classified,
                        "total_unknown": total_unknown,
                        "top_unknown_signals": [
                            {"type": t, "count": c}
                            for t, c in top_unknown
                        ],
                    },
                    "dependencies": {
                        "negotiation_bus": self._negotiation_bus is not None,
                    },
                },
                "warnings": (
                    [f"priority_violations: {priority_violations}"]
                    if priority_violations else []
                ),
            }
        except Exception as e:
            logger.error(
                f"健康检查失败: {e} "
                f"#RECOVERY: 检查信号映射表数据结构完整性和线程锁状态"
            )
            return {
                "status": "error",
                "error_code": "SIG_CLASS_003",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _build_critical_set(
        self, signal_map: Dict[str, Tuple[str, int]]
    ) -> frozenset:
        """基于给定的信号映射表构建生存级信号快速查找集合（优先级 >= CRITICAL_MIN_PRIORITY）"""
        return frozenset(
            sig for sig, (_, pri) in signal_map.items()
            if pri >= self.CRITICAL_MIN_PRIORITY
        )

    def _load_static_config(self) -> Dict[str, Tuple[str, int]]:
        """
        尝试从配置文件加载信号映射，失败则使用内置默认值

        Returns:
            信号类型 -> (车道, 优先级) 映射字典
        """
        try:
            from core.utils.config_loader import ConfigLoader
        except ImportError:
            logger.debug("ConfigLoader 不可用，使用内置默认映射")
            return self.DEFAULT_SIGNAL_MAP.copy()
        except Exception as e:
            logger.error(f"ConfigLoader 导入异常: {e}", exc_info=True)
            return self.DEFAULT_SIGNAL_MAP.copy()

        try:
            loader = ConfigLoader()
            config_map = loader.load_signal_classification()
            if config_map and isinstance(config_map, dict):
                validated: Dict[str, Tuple[str, int]] = {}
                valid_lanes = {"express", "fast", "normal", "slow"}
                for sig, (lane, pri) in config_map.items():
                    if lane in valid_lanes and isinstance(pri, int) and 0 <= pri <= 10:
                        validated[sig] = (lane, pri)
                    else:
                        logger.warning(
                            f"配置文件信号映射无效条目: {sig} -> ({lane}, {pri})，已跳过"
                        )
                if validated:
                    # 确保生存级信号不被配置文件意外覆盖
                    for sig in self.CRITICAL_SIGNALS:
                        if sig in self.DEFAULT_SIGNAL_MAP and sig not in validated:
                            validated[sig] = self.DEFAULT_SIGNAL_MAP[sig]
                            logger.warning(
                                f"配置文件缺失生存级信号 {sig}，已从默认值恢复"
                            )
                    logger.info(f"从配置文件加载 {len(validated)} 条信号映射")
                    return validated
        except Exception as e:
            logger.error(f"配置文件加载异常: {e}，使用内置默认映射", exc_info=True)

        return self.DEFAULT_SIGNAL_MAP.copy()

    def _load_dynamic_config(self) -> None:
        """
        尝试从协商总线加载动态信号分类配置（降级安全，加锁保护）

        关键约束：
        - 动态配置不能覆盖或删除生存级信号
        - 若检测到优先级违规，拒绝应用更新并保持当前配置
        - 原子更新快照，确保读写一致性
        """
        if self._negotiation_bus is None:
            return
        try:
            dynamic_map = self._negotiation_bus.get_signal_classification()
            if dynamic_map and isinstance(dynamic_map, dict):
                merged = self._load_static_config()
                # 仅覆盖非生存级信号
                for sig, (lane, pri) in dynamic_map.items():
                    if sig in self.CRITICAL_SIGNALS:
                        logger.warning(
                            f"动态配置尝试覆盖生存级信号 {sig}，已拒绝"
                        )
                        continue
                    if lane in {"express", "fast", "normal", "slow"} and isinstance(pri, int) and 0 <= pri <= 10:
                        merged[sig] = (lane, pri)
                    else:
                        logger.warning(
                            f"动态配置无效条目: {sig} -> ({lane}, {pri})，已跳过"
                        )

                # 生存级信号强制保护
                for sig in self.CRITICAL_SIGNALS:
                    if sig not in merged:
                        merged[sig] = self.DEFAULT_SIGNAL_MAP.get(
                            sig, (self.DEFAULT_LANE, self.DEFAULT_PRIORITY)
                        )
                        logger.error(
                            f"动态配置缺失生存级信号 {sig}，已从默认值恢复 "
                            f"#RECOVERY: 立即检查动态配置下发流程，排查信号映射完整性"
                        )

                # 优先级反转检测（检测到违规则拒绝更新）
                violations = self._validate_critical_priority(merged)
                if violations:
                    logger.error(
                        f"动态配置更新被拒绝！发现生存级信号优先级违规: {violations}. "
                        f"系统将保持当前配置。"
                        f"#RECOVERY: 修复动态配置源中的优先级设置。"
                    )
                    return

                # 通过全部校验，基于新配置构建临界集，然后原子替换整个快照
                new_critical = self._build_critical_set(merged)
                with self._snapshot_lock:
                    self._snapshot = (merged, new_critical)
                    self._signal_map = merged
                    self._critical_set = new_critical

                logger.info(
                    "动态信号分类配置更新成功，合并后共 %d 条规则，"
                    "其中生存级信号 %d 个",
                    len(merged), len(new_critical)
                )
        except Exception as e:
            logger.warning(
                f"动态配置加载失败，继续使用现有映射表: {e} "
                f"#RECOVERY: 检查 NegotiationBus 的 get_signal_classification 方法是否正确实现"
            )

    def _validate_critical_priority(
        self, signal_map: Dict[str, Tuple[str, int]]
    ) -> List[str]:
        """验证生存级信号优先级是否满足最低要求"""
        violations = []
        for sig in self.CRITICAL_SIGNALS:
            if sig in signal_map:
                _, pri = signal_map[sig]
                if pri < self.CRITICAL_MIN_PRIORITY:
                    violations.append(
                        f"{sig}: priority={pri} < {self.CRITICAL_MIN_PRIORITY}"
                    )
        return violations

    def _update_stats(self, signal_type: str, is_known: bool) -> None:
        """
        更新信号分类统计（加锁保护，含容量限制和淘汰机制）

        注意：此方法仅对非生存级信号调用，避免锁竞争污染关键路径
        """
        with self._stats_lock:
            if is_known:
                self._classify_stats[signal_type] += 1
                # 容量保护：超过上限时保留最近最频繁的条目
                if len(self._classify_stats) > self._MAX_CLASSIFY_STATS_SIZE:
                    self._classify_stats = Counter(
                        dict(self._classify_stats.most_common(
                            self._CLASSIFY_STATS_TRIM_SIZE
                        ))
                    )
                    logger.debug(
                        "分类统计计数器已清理，保留前 %d 条高频条目",
                        self._CLASSIFY_STATS_TRIM_SIZE
                    )
            else:
                self._unknown_stats[signal_type] += 1
                # 容量保护
                if len(self._unknown_stats) > self._MAX_UNKNOWN_STATS_SIZE:
                    self._unknown_stats = Counter(
                        dict(self._unknown_stats.most_common(
                            self._UNKNOWN_STATS_TRIM_SIZE
                        ))
                    )
                    logger.debug(
                        "未知信号统计计数器已清理，保留前 %d 条高频条目",
                        self._UNKNOWN_STATS_TRIM_SIZE
                    )
                # 未知信号突增告警（按信号类型分别去重）
                if self._unknown_stats[signal_type] > self._unknown_alert_threshold:
                    now = time.time()
                    last_time = self._last_unknown_alert.get(signal_type, 0)
                    if now - last_time > self._unknown_alert_window:
                        self._last_unknown_alert[signal_type] = now
                        logger.error(
                            f"未知信号类型 {signal_type} 短时间内出现 "
                            f"{self._unknown_stats[signal_type]} 次 "
                            f"#RECOVERY: 检查上游模块是否引入新信号类型，"
                            f"更新信号映射表"
                )
