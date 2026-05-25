"""
火种系统 · 信号分类器 (SignalClassifier)

核心职责：
1. 根据信号类型（如 pipeline_advance、profit_compression_trigger 等）将其映射到对应的四车道（极速/快速/普通/慢速）及优先级
2. 提供信号优先级查询接口，为车道调度器提供决策依据

外部依赖（真实模块接口）：
- 无强制外部依赖。可选依赖 NegotiationBus 用于动态配置热更新，未注入时使用内置静态映射表。

接口契约：
- classify(signal_type: str) -> Dict[str, Any] : 返回信号的车道归属、优先级及分类说明
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])
- 错误返回固定包含 "error_code" (str)，所有错误码在类常量 ERROR_CODES 中声明

异常与降级：
- 当传入未知信号类型时，自动降级为普通车道（优先级 P2），并记录 WARNING 日志
- 当协商总线动态配置不可用时，使用内置静态映射表作为安全回退
- 动态配置更新通过线程锁保护，确保与 classify 读取不产生竞态条件
- 所有降级值在类常量区明确声明

资源管理：
- 本模块为纯逻辑分类器，不持有任何外部资源
"""

import logging
import threading
from typing import Dict, Any, List, Optional

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

    # ========== 静态信号类型 -> (车道, 优先级) 映射表 ==========
    # 车道取值: "express", "fast", "normal", "slow"
    # 优先级取值: 0-10, 10为最高（生存级）
    DEFAULT_SIGNAL_MAP = {
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

    def __init__(self):
        # 信号映射表：优先使用动态注入的映射，否则使用默认静态映射
        self._signal_map = self.DEFAULT_SIGNAL_MAP.copy()

        # 线程锁保护 _signal_map 的读写操作
        self._map_lock = threading.Lock()

        # 外部依赖（可选）
        self._negotiation_bus = None

        logger.info(
            "SignalClassifier 初始化完成，已加载 %d 条信号映射规则",
            len(self._signal_map)
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

        # 查询映射表（加锁保护）
        with self._map_lock:
            if signal_type in self._signal_map:
                lane, priority = self._signal_map[signal_type]
                is_known = True
            else:
                lane = self.DEFAULT_LANE
                priority = self.DEFAULT_PRIORITY
                is_known = False

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
        模块自检：验证信号映射表完整性、核心信号存在性、依赖状态

        Returns:
            标准健康检查响应字典
        """
        try:
            with self._map_lock:
                map_size = len(self._signal_map)

                # 验证映射表中所有条目的车道和优先级是否有效
                valid_lanes = {"express", "fast", "normal", "slow"}
                invalid_entries = []
                for sig_type, (lane, priority) in self._signal_map.items():
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
                    s for s in self.CRITICAL_SIGNALS if s not in self._signal_map
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

            return {
                "status": "ok",
                "reason": f"SignalClassifier 正常，已加载 {map_size} 条信号映射规则",
                "data": {
                    "map_size": map_size,
                    "critical_signals_ok": True,
                    "dependencies": {
                        "negotiation_bus": self._negotiation_bus is not None,
                    },
                },
                "warnings": [],
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
    def _load_dynamic_config(self) -> None:
        """尝试从协商总线加载动态信号分类配置（降级安全，加锁保护）"""
        if self._negotiation_bus is None:
            return
        try:
            dynamic_map = self._negotiation_bus.get_signal_classification()
            if dynamic_map and isinstance(dynamic_map, dict):
                merged = self.DEFAULT_SIGNAL_MAP.copy()
                merged.update(dynamic_map)
                with self._map_lock:
                    self._signal_map = merged
                logger.info(
                    "动态信号分类配置加载成功，合并后共 %d 条规则",
                    len(self._signal_map)
                )
        except Exception as e:
            logger.warning(
                f"动态配置加载失败，继续使用静态映射表: {e} "
                f"#RECOVERY: 检查 NegotiationBus 的 get_signal_classification 方法是否正确实现"
            )
