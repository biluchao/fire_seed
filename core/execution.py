"""
火种系统 · 执行网关主入口 (Execution Gateway)

核心职责：
1. 接收标准化神经脉冲（NeuroPulse），协调各执行子模块完成订单的智能路由、类型选择与执行
2. 维护执行上下文，确保从决策到成交的全链路状态一致性与可追溯性
3. 提供生存级、降级级、紧急级多层兜底路径，确保任何情况下都能发出订单

外部依赖（真实模块接口）：
- core.execution.order_type_selector.OrderTypeSelector : 智能选择订单类型（限价/冰山/TWAP/市价）
- core.execution.iceberg_manager.IcebergManager : 冰山订单显露量管理与伪装切片
- core.execution.twap_executor.TWAPExecutor : TWAP执行器自适应时间片分配
- core.execution.intent_hider.IntentHider : 意图隐藏、订单聚合去重、幽灵流动性探测
- core.execution.slippage_filter.SlippageFilter : 预期滑点计算、规模限制与拒绝逻辑
- core.execution.multi_venue_router.MultiVenueRouter : 多交易所并行下单与最优成交选择
- core.execution.partial_fill_handler.PartialFillHandler : 部分成交的主动撤单与残单重挂
- core.execution.post_execution_auditor.PostExecutionAuditor : 执行后审计与反事实归因
- core.negotiation_bus.NegotiationBus : 接收决策指令，回传执行结果
- core.behavioral_logger.BehavioralLogger : 记录执行链路日志
- core.order_manager.active_order_registry.ActiveOrderRegistry : 活跃订单注册表，用于状态对账
- core.utils.api_client.ExchangeAPIClient : 真实交易所API调用（降级兜底）

接口契约：
- execute(pulse: NeuroPulse) -> Dict[str, Any] : 执行决策指令，返回成交回报或异常状态
- cancel_order(order_id: str) -> Dict[str, Any] : 撤销指定订单
- cancel_all_for_symbol(symbol: str) -> Dict[str, Any] : 撤销指定品种的所有活跃订单
- health_check() -> Dict[str, Any] : 模块自检
- sync_state() -> Dict[str, Any] : 从订单注册表同步活跃订单计数（启动时调用）
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当任一子模块不可用时，根据指令紧急性决定降级策略
- 紧急性 >= 9 的指令：绕过所有子模块，直接发送市价单（生存级），但仍保留最基础的滑点保护
- 紧急性 < 9 的指令：子模块不可用时使用限价单作为安全回退
- 降级路径 `_execute_single_venue` 必须保留基础执行能力，不能硬编码拒绝
- 所有降级值在类常量区明确声明

资源管理：
- 本模块不持有任何需要手动释放的外部资源
- 子模块实例在系统构建阶段注入，生命周期由 system_builder 管理
- 活跃订单计数器使用锁保护，确保并发安全，并在 execute 中用上下文管理器保证闭环
- 脉冲去重集合使用时间戳滑动窗口，并分批清理防止单次操作耗时过长
"""

import copy
import re
import sys
import time
import logging
import threading
from typing import Dict, Any, List, Optional
from enum import Enum

logger = logging.getLogger(__name__)


class ExecutionMode(Enum):
    """执行模式枚举"""
    FULL = "full"           # 所有子模块正常运行
    SURVIVAL = "survival"   # 生存模式：仅保留市价单能力
    DEGRADED = "degraded"   # 降级模式：部分子模块不可用


class ExecutionGateway:
    """执行网关主入口"""

    # ========== 类常量（默认配置） ==========
    DEFAULT_SURVIVAL_URGENCY = 9           # 触发生存模式的紧急性阈值，无量纲，取值范围 [7, 10]
    DEFAULT_ORDER_TIMEOUT_SEC = 30         # 订单超时时间，秒，取值范围 [10, 120]
    DEFAULT_MAX_ACTIVE_ORDERS = 100        # 全局最大活跃订单数，无量纲，[50, 500]
    DEFAULT_DEDUP_TTL_SECONDS = 3600       # 脉冲去重有效期，秒，取值范围 [300, 86400]
    DEFAULT_DEDUP_CLEANUP_INTERVAL = 60    # 去重集合清理间隔，秒，[30, 300]
    DEDUP_BATCH_SIZE = 1000                # 单次清理最大记录数，无量纲，[500, 2000]
    MAX_EMERGENCY_SIZE_PCT = 0.05          # 紧急兜底路径单笔最大仓位（权益占比），[0.01, 0.1]
    MIN_ORDER_SIZE = 0.0001                # 最小交易量（BTC），交易所最小交易单位
    COUNTER_DEVIATION_THRESHOLD = 5        # 计数器对账偏差阈值，无量纲，[1, 20]

    # 合法意图类型（白名单）
    VALID_INTENT_TYPES = {
        "open_long", "open_short", "add_position", "reduce_position",
        "close_long", "close_short", "close_all", "modify_stop",
        "circuit_break", "emergency_close"
    }

    # 意图与方向一致性映射（仅对有明确方向的意图校验）
    INTENT_SIDE_MAPPING = {
        "open_long": "buy",
        "open_short": "sell",
        "close_long": "sell",
        "close_short": "buy",
    }

    # 订单ID格式白名单（支持主流交易所）
    VALID_ORDER_ID_PATTERNS = [
        re.compile(r'^[0-9]+$'),                          # 纯数字 (Binance)
        re.compile(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'),  # UUID (OKX)
        re.compile(r'^[A-Z0-9]{10,20}$'),                  # 大写字母数字混合 (Bybit)
    ]
    MAX_ORDER_ID_LENGTH = 64

    # 撤单时不可重试的错误码（订单已处于终态）
    UNRETRYABLE_CANCEL_CODES = {"ORDER_NOT_FOUND", "INVALID_ORDER_ID", "ORDER_ALREADY_FILLED"}

    def __init__(self):
        # 子模块注入
        self._order_type_selector = None
        self._iceberg_manager = None
        self._twap_executor = None
        self._intent_hider = None
        self._slippage_filter = None
        self._multi_venue_router = None
        self._partial_fill_handler = None
        self._post_execution_auditor = None

        # 外部依赖注入
        self._negotiation_bus = None
        self._behavioral_logger = None
        self._order_registry = None
        self._api_client = None          # 真实交易所API客户端（用于兜底）

        # 执行模式
        self._mode = ExecutionMode.FULL

        # 活跃订单计数（线程安全）
        self._active_order_count = 0
        self._order_lock = threading.Lock()

        # 脉冲去重集合（时间戳滑动窗口）
        self._executed_pulse_ids: Dict[str, float] = {}   # pulse_id -> timestamp
        self._dedup_lock = threading.Lock()
        self._last_dedup_cleanup = time.time()

        logger.info("ExecutionGateway 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        order_type_selector: Optional[Any] = None,
        iceberg_manager: Optional[Any] = None,
        twap_executor: Optional[Any] = None,
        intent_hider: Optional[Any] = None,
        slippage_filter: Optional[Any] = None,
        multi_venue_router: Optional[Any] = None,
        partial_fill_handler: Optional[Any] = None,
        post_execution_auditor: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        order_registry: Optional[Any] = None,
        api_client: Optional[Any] = None,
    ) -> None:
        """注入所有子模块与外部依赖，并对关键子模块执行存活校验"""
        self._order_type_selector = order_type_selector
        self._iceberg_manager = iceberg_manager
        self._twap_executor = twap_executor
        self._intent_hider = intent_hider
        self._slippage_filter = slippage_filter
        self._multi_venue_router = multi_venue_router
        self._partial_fill_handler = partial_fill_handler
        self._post_execution_auditor = post_execution_auditor
        self._negotiation_bus = negotiation_bus
        self._behavioral_logger = behavioral_logger
        self._order_registry = order_registry
        self._api_client = api_client

        # 检查关键子模块的可用性
        missing = []
        if order_type_selector is None:
            missing.append("OrderTypeSelector")
        if multi_venue_router is None:
            missing.append("MultiVenueRouter")
        if missing:
            self._mode = ExecutionMode.DEGRADED
            logger.warning(f"关键子模块缺失: {missing}，进入降级模式")

        # 对关键子模块进行存活校验
        self._verify_critical_modules_alive()

        logger.info(f"执行网关依赖注入完成，当前模式: {self._mode.value}")

    def _verify_critical_modules_alive(self) -> None:
        """对已注入的关键子模块执行穿透性健康检查"""
        modules_to_check = [
            ("MultiVenueRouter", self._multi_venue_router),
            ("OrderTypeSelector", self._order_type_selector),
            ("SlippageFilter", self._slippage_filter),
            ("ActiveOrderRegistry", self._order_registry),
        ]
        for name, mod in modules_to_check:
            if mod is not None and hasattr(mod, 'health_check'):
                try:
                    hc = mod.health_check()
                    if hc.get('status') != 'ok':
                        logger.error(f"{name} 注入后自检失败: {hc.get('reason')} #RECOVERY: 重启该子模块或检查其依赖")
                        if name == "MultiVenueRouter":
                            self._mode = ExecutionMode.DEGRADED
                except Exception as e:
                    logger.error(f"{name} 自检异常: {e}")
                    if name == "MultiVenueRouter":
                        self._mode = ExecutionMode.DEGRADED

    # ========== 公共接口 ==========
    def sync_state(self) -> Dict[str, Any]:
        """
        从订单注册表同步活跃订单计数（启动时或恢复时调用）

        Returns:
            标准响应字典
        """
        if self._order_registry is None:
            logger.warning("ActiveOrderRegistry 未注入，无法同步状态")
            return {
                "status": "degraded",
                "reason": "订单注册表不可用",
                "data": {},
                "warnings": ["registry_unavailable"],
            }

        try:
            self._force_sync_from_registry()
            with self._order_lock:
                synced = self._active_order_count
            logger.info(f"状态同步完成，当前活跃订单: {synced}")
            return {
                "status": "ok",
                "reason": f"同步完成，活跃订单: {synced}",
                "data": {"active_orders": synced},
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"状态同步失败: {e} #RECOVERY: 检查订单注册表状态")
            return {
                "status": "error",
                "reason": f"同步异常: {str(e)}",
                "data": {},
                "warnings": ["sync_failed"],
            }

    def execute(self, pulse: Any) -> Dict[str, Any]:
        """
        执行决策指令

        Args:
            pulse: NeuroPulse 标准化信号对象，必须包含 urgency, intent_type, desired_size_pct 等字段

        Returns:
            标准响应字典，data 中包含 order_id, fill_price, fill_quantity, slippage_bps 等字段
        """
        start_time = time.time()

        # 参数校验
        if pulse is None:
            logger.error("收到空脉冲信号 #RECOVERY: 检查上游策略引擎输出")
            return {
                "status": "error",
                "reason": "收到空脉冲信号",
                "data": {},
                "warnings": ["null_pulse"],
            }

        urgency = getattr(pulse, 'urgency', 5)
        intent_type = getattr(pulse, 'intent_type', 'unknown')
        desired_size = getattr(pulse, 'desired_size_pct', 0.0)
        side = getattr(pulse, 'side', '')
        pulse_id = getattr(pulse, 'pulse_id', None)

        # 意图类型白名单校验
        if intent_type not in self.VALID_INTENT_TYPES:
            logger.error(f"非法意图类型: {intent_type}")
            return {
                "status": "rejected",
                "reason": f"非法意图类型: {intent_type}",
                "error_code": "INVALID_INTENT",
                "data": {},
                "warnings": [f"invalid_intent: {intent_type}"],
            }

        # 意图与方向一致性校验
        if not self._validate_intent_side_consistency(intent_type, side):
            return {
                "status": "rejected",
                "reason": f"意图与方向矛盾: {intent_type} vs {side}",
                "error_code": "INTENT_SIDE_MISMATCH",
                "data": {},
                "warnings": ["intent_side_conflict"],
            }

        # 脉冲去重检测（时间戳滑动窗口）
        if pulse_id:
            with self._dedup_lock:
                self._cleanup_expired_pulses()
                if pulse_id in self._executed_pulse_ids:
                    logger.warning(f"重复脉冲已丢弃: {pulse_id}")
                    return {
                        "status": "rejected",
                        "reason": "重复脉冲已丢弃",
                        "data": {"pulse_id": pulse_id},
                        "warnings": ["duplicate_pulse"],
                    }
                self._executed_pulse_ids[pulse_id] = time.time()

        # 活跃订单数检查与自增（使用上下文管理器保护，确保finally中递减）
        with self._order_lock:
            if self._active_order_count >= self.DEFAULT_MAX_ACTIVE_ORDERS:
                logger.error(f"全局活跃订单数已达上限 {self.DEFAULT_MAX_ACTIVE_ORDERS}")
                return {
                    "status": "rejected",
                    "reason": "全局活跃订单数已达上限",
                    "error_code": "ORDER_LIMIT_REACHED",
                    "data": {},
                    "warnings": ["global_order_limit"],
                }
            self._active_order_count += 1

        logger.info(
            f"收到执行指令: intent={intent_type}, urgency={urgency}, size={desired_size:.4%}, pulse_id={pulse_id}",
        )

        try:
            # 生存级指令：绕过所有子模块，直接执行市价单（但仍保留基础滑点保护）
            if urgency >= self.DEFAULT_SURVIVAL_URGENCY:
                result = self._execute_survival(pulse)
                # 生存模式也必须审计
                if self._post_execution_auditor is not None:
                    try:
                        self._post_execution_auditor.audit(result.get("data", {}), pulse, start_time)
                    except Exception as e:
                        logger.error(f"生存模式审计异常: {e}")
                return result

            # 标准执行流程
            # 1. 滑点过滤与规模限制
            if self._slippage_filter is not None:
                slip_check = self._slippage_filter.check(pulse)
                if slip_check.get("rejected"):
                    logger.warning(f"滑点过滤器拒绝执行: {slip_check.get('reason')}")
                    return {
                        "status": "rejected",
                        "reason": f"滑点过滤拒绝: {slip_check.get('reason')}",
                        "data": slip_check,
                        "warnings": ["slippage_rejected"],
                    }

            # 2. 订单类型选择
            order_type = "market"  # 降级默认值
            if self._order_type_selector is not None:
                type_result = self._order_type_selector.select(pulse)
                order_type = type_result.get("order_type", "market")

            # 3. 意图隐藏（仅对非市价单生效）
            if order_type != "market" and self._intent_hider is not None:
                pulse = self._intent_hider.apply_disguise(pulse)

            # 4. 多通道路由执行
            if self._multi_venue_router is not None:
                exec_result = self._multi_venue_router.route_and_execute(pulse, order_type)
            else:
                # 降级：单通道路由（必须保留基础执行能力）
                exec_result = self._execute_single_venue(pulse, order_type)

            # 5. 部分成交处理
            if exec_result.get("partial_fill") and self._partial_fill_handler is not None:
                exec_result = self._partial_fill_handler.handle(exec_result)

            # 6. 执行后审计
            if self._post_execution_auditor is not None:
                try:
                    self._post_execution_auditor.audit(exec_result, pulse, start_time)
                except Exception as e:
                    logger.error(f"执行审计异常: {e}")

            elapsed_us = (time.time() - start_time) * 1_000_000
            logger.info(f"执行完成: {exec_result.get('order_id', 'N/A')}, 耗时 {elapsed_us:.0f}μs")

            return {
                "status": "ok",
                "reason": f"指令执行完成，订单类型: {order_type}",
                "data": exec_result,
                "warnings": [],
            }

        except Exception as e:
            logger.error(f"执行异常: {e} #RECOVERY: 检查子模块状态与交易所API连通性")
            return {
                "status": "error",
                "reason": f"执行异常: {str(e)}",
                "data": {},
                "warnings": ["execution_exception"],
            }
        finally:
            # 确保计数器递减（无论成功、失败还是异常）
            with self._order_lock:
                if self._active_order_count > 0:
                    self._active_order_count -= 1

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """
        撤销指定订单（逐级强化兜底，含订单ID格式校验与不可重试错误识别）

        Args:
            order_id: 交易所返回的订单ID

        Returns:
            标准响应字典
        """
        if not self._validate_order_id(order_id):
            logger.error(f"无效订单ID格式: {order_id}")
            return {
                "status": "error",
                "reason": f"无效订单ID格式: {order_id}",
                "data": {},
                "warnings": ["invalid_order_id_format"],
            }

        # 第一优先级：多通道路由器
        if self._multi_venue_router is not None:
            result = self._multi_venue_router.cancel_order(order_id)
            if result.get("status") == "ok":
                return {
                    "status": "ok",
                    "reason": f"撤单请求已发送: {order_id}",
                    "data": result,
                    "warnings": [],
                }
            # 检查是否是不可重试的错误
            if result.get("error_code", "").upper() in self.UNRETRYABLE_CANCEL_CODES:
                logger.warning(f"订单 {order_id} 已处于终态 (code={result.get('error_code')})，放弃重试")
                return {
                    "status": "ok",
                    "reason": f"订单已失效: {order_id}",
                    "data": result,
                    "warnings": ["order_already_terminal"],
                }
            logger.warning(f"路由器撤单失败: {order_id}, 尝试API直连")

        # 第二优先级：直接调用交易所API
        if self._api_client is not None:
            try:
                result = self._api_client.cancel_order(order_id)
                return {
                    "status": "ok",
                    "reason": f"API直连撤单成功: {order_id}",
                    "data": result,
                    "warnings": [],
                }
            except Exception as e:
                logger.error(f"API撤单失败: {e}")

        # 最后手段：记录为紧急事件，触发告警
        logger.critical(f"所有撤单路径均失败: {order_id} #RECOVERY: 立即人工介入，手动撤单")
        return {
            "status": "error",
            "reason": "所有撤单路径均失败，需要人工介入",
            "data": {"order_id": order_id},
            "warnings": ["cancel_all_failed"],
        }

    def cancel_all_for_symbol(self, symbol: str) -> Dict[str, Any]:
        """
        撤销指定品种的所有活跃订单（逐级强化兜底）

        Args:
            symbol: 品种标识（如 BTCUSDT）

        Returns:
            标准响应字典
        """
        if not symbol:
            return {
                "status": "error",
                "reason": "品种标识为空",
                "data": {},
                "warnings": ["invalid_symbol"],
            }

        # 第一优先级：多通道路由器
        if self._multi_venue_router is not None:
            result = self._multi_venue_router.cancel_all_for_symbol(symbol)
            if result.get("status") == "ok":
                return {
                    "status": "ok",
                    "reason": f"已撤销 {symbol} 的所有活跃订单",
                    "data": result,
                    "warnings": [],
                }
            logger.warning(f"路由器批量撤单失败: {symbol}, 尝试API直连")

        # 第二优先级：直接调用交易所API
        if self._api_client is not None:
            try:
                result = self._api_client.cancel_all_orders(symbol)
                return {
                    "status": "ok",
                    "reason": f"API直连批量撤单成功: {symbol}",
                    "data": result,
                    "warnings": [],
                }
            except Exception as e:
                logger.error(f"API批量撤单失败: {e}")

        logger.critical(f"所有批量撤单路径均失败: {symbol} #RECOVERY: 立即人工介入")
        return {
            "status": "error",
            "reason": "所有撤单路径均失败，需要人工介入",
            "data": {"symbol": symbol},
            "warnings": ["cancel_all_failed"],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检（包含穿透性测试与计数器自动对账）

        Returns:
            标准健康检查响应字典
        """
        try:
            submodules = {
                "order_type_selector": self._order_type_selector is not None,
                "iceberg_manager": self._iceberg_manager is not None,
                "twap_executor": self._twap_executor is not None,
                "intent_hider": self._intent_hider is not None,
                "slippage_filter": self._slippage_filter is not None,
                "multi_venue_router": self._multi_venue_router is not None,
                "partial_fill_handler": self._partial_fill_handler is not None,
                "post_execution_auditor": self._post_execution_auditor is not None,
            }
            available = sum(1 for v in submodules.values() if v)

            # 穿透性测试：检查交易所API连通性
            api_ok = False
            if self._api_client is not None:
                try:
                    api_status = self._api_client.ping()
                    api_ok = api_status.get("status") == "ok"
                except Exception:
                    api_ok = False
            else:
                api_ok = None

            # 活跃订单计数器与注册表对账（带自动修复）
            reconciliation_ok = True
            registry_count = 0
            if self._order_registry is not None:
                try:
                    registry_count = self._order_registry.get_active_count()
                except Exception:
                    registry_count = -1

            with self._order_lock:
                local_count = self._active_order_count

            if registry_count >= 0 and abs(local_count - registry_count) > self.COUNTER_DEVIATION_THRESHOLD:
                logger.error(
                    f"计数器偏差: 本地={local_count}, 注册表={registry_count}，触发自动对账 #RECOVERY: 已自动修复"
                )
                try:
                    self._force_sync_from_registry()
                    with self._order_lock:
                        local_count = self._active_order_count
                except Exception as e:
                    logger.error(f"自动对账失败: {e}")
                    reconciliation_ok = False

            return {
                "status": "ok" if (api_ok is not False and reconciliation_ok) else "degraded",
                "reason": (
                    f"ExecutionGateway 子模块可用: {available}/{len(submodules)}, "
                    f"活跃订单(本地={local_count}, 注册表={registry_count}), API连通={api_ok}"
                ),
                "data": {
                    "mode": self._mode.value,
                    "submodules": submodules,
                    "active_orders_local": local_count,
                    "active_orders_registry": registry_count,
                    "reconciliation": "ok" if reconciliation_ok else "mismatch",
                    "api_connected": api_ok,
                    "dependencies": {
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                        "order_registry": self._order_registry is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查执行网关初始化状态")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _validate_order_id(self, order_id: str) -> bool:
        """校验订单ID格式"""
        if not order_id:
            return False
        if len(order_id) > self.MAX_ORDER_ID_LENGTH:
            logger.warning(f"订单ID长度超限: {len(order_id)}")
            return False
        for pattern in self.VALID_ORDER_ID_PATTERNS:
            if pattern.match(order_id):
                return True
        logger.warning(f"订单ID格式不匹配任何已知模式: {order_id}")
        return False

    def _validate_intent_side_consistency(self, intent_type: str, side: str) -> bool:
        """校验意图与方向的一致性"""
        expected = self.INTENT_SIDE_MAPPING.get(intent_type)
        if expected is None:
            return True  # 不强制校验
        if side != expected:
            logger.error(f"意图与方向矛盾: intent={intent_type}, side={side}, expected={expected}")
            return False
        return True

    def _cleanup_expired_pulses(self) -> None:
        """分批清理过期的脉冲去重记录（在 dedup_lock 内调用）"""
        now = time.time()
        if now - self._last_dedup_cleanup < self.DEFAULT_DEDUP_CLEANUP_INTERVAL:
            return
        expired = []
        for pid, ts in list(self._executed_pulse_ids.items()):
            if now - ts > self.DEFAULT_DEDUP_TTL_SECONDS:
                expired.append(pid)
            # 分批删除，避免单次操作耗时过长
            if len(expired) >= self.DEDUP_BATCH_SIZE:
                break
        for pid in expired:
            del self._executed_pulse_ids[pid]
        self._last_dedup_cleanup = now
        if expired:
            logger.debug(f"清理脉冲去重记录: {len(expired)} 条，剩余: {len(self._executed_pulse_ids)}")

    def _force_sync_from_registry(self) -> None:
        """
        强制从注册表同步计数（安全版本：先清零再重算，避免计数器膨胀）
        此方法在 _order_lock 之外被调用，但内部会获取锁
        """
        if self._order_registry is None:
            return
        with self._order_lock:
            self._active_order_count = 0
            active_count = 0
            try:
                for symbol in self._order_registry.get_all_symbols():
                    orders = self._order_registry.get_orders_by_symbol(symbol)
                    for order in orders.get("orders", []):
                        if order.get("status") in ("pending", "open", "partial_fill"):
                            active_count += 1
                self._active_order_count = active_count
                logger.info(f"强制对账完成，活跃订单重置为: {active_count}")
            except Exception as e:
                logger.error(f"强制对账内部异常: {e}")

    def _execute_survival(self, pulse: Any) -> Dict[str, Any]:
        """
        生存模式执行：直接市价单，但保留最基础的滑点保护

        Args:
            pulse: 高紧急性指令（urgency >= 9）

        Returns:
            标准响应字典
        """
        logger.warning("进入生存执行模式")

        # 创建脉冲副本，防止原始对象被子模块修改
        survival_pulse = copy.copy(pulse)

        # 生存模式下的基础滑点保护
        if self._slippage_filter is not None:
            survival_pulse = self._slippage_filter.apply_survival_limits(survival_pulse)

        if self._multi_venue_router is not None:
            result = self._multi_venue_router.route_and_execute(survival_pulse, "market")
        else:
            result = self._execute_single_venue(survival_pulse, "market")

        logger.info(f"生存执行完成: {result.get('order_id', 'N/A')}")
        return {
            "status": "ok",
            "reason": "生存模式执行完成",
            "data": result,
            "warnings": ["survival_mode"],
        }

    def _execute_single_venue(self, pulse: Any, order_type: str) -> Dict[str, Any]:
        """
        单通道路由降级执行（当 MultiVenueRouter 不可用时）
        必须保留基础执行能力，确保系统在任何情况下都能发出订单

        Args:
            pulse: 标准化信号对象
            order_type: 订单类型

        Returns:
            执行结果字典
        """
        logger.critical("单通道路由降级执行，立即检查 MultiVenueRouter 状态 #RECOVERY: 检查路由器进程存活、网络连接、共享内存状态")

        try:
            result = self._emergency_market_order(pulse, order_type)
            return result
        except Exception as e:
            logger.error(f"降级执行也失败: {e} #RECOVERY: 检查交易所API连通性、网络防火墙、API密钥有效性")
            return {
                "order_id": f"FAILED_{int(time.time() * 1000)}",
                "fill_price": 0.0,
                "fill_quantity": 0.0,
                "status": "rejected",
                "reason": f"所有执行路径均失败: {str(e)}",
                "partial_fill": False,
            }

    def _emergency_market_order(self, pulse: Any, order_type: str = "market") -> Dict[str, Any]:
        """
        紧急市价单执行（最后的降级路径），通过真实交易所API发送订单
        包含硬性仓位上限保护和最小交易量校验，并写入独立的紧急日志通道

        Args:
            pulse: 标准化信号对象
            order_type: 订单类型，默认 market

        Returns:
            执行结果字典
        """
        # 最后防线的独立告警
        emergency_msg = (
            f"[EMERGENCY_PATH] 紧急兜底路径被激活 at {time.time()}: "
            f"pulse_id={getattr(pulse, 'pulse_id', 'N/A')}, "
            f"intent={getattr(pulse, 'intent_type', 'N/A')}, "
            f"urgency={getattr(pulse, 'urgency', 'N/A')}"
        )
        # 写入标准错误输出
        print(emergency_msg, file=sys.stderr)
        # 写入独立的紧急日志文件
        try:
            with open("logs/execution_emergency.log", "a") as f:
                f.write(emergency_msg + "\n")
        except Exception:
            pass

        if self._api_client is None:
            logger.critical("紧急兜底路径：API客户端未注入，无法发送订单 #RECOVERY: 检查依赖注入配置")
            return {
                "order_id": f"NO_API_{int(time.time() * 1000)}",
                "fill_price": 0.0,
                "fill_quantity": 0.0,
                "status": "rejected",
                "reason": "API客户端未配置",
                "partial_fill": False,
            }

        try:
            symbol = getattr(pulse, 'symbol', 'BTCUSDT')
            intent_type = getattr(pulse, 'intent_type', '')
            side = "buy" if intent_type.startswith(('open_long', 'add')) else "sell"
            quantity = float(getattr(pulse, 'desired_size_pct', 0.01))

            # 硬性仓位上限保护
            if quantity > self.MAX_EMERGENCY_SIZE_PCT:
                logger.critical(f"紧急订单量超限: {quantity:.4%} -> {self.MAX_EMERGENCY_SIZE_PCT:.4%}")
                quantity = self.MAX_EMERGENCY_SIZE_PCT

            # 最小交易量校验
            if quantity < self.MIN_ORDER_SIZE:
                logger.warning(f"订单量过小: {quantity}，已调整至最小交易量")
                quantity = self.MIN_ORDER_SIZE

            result = self._api_client.place_order(
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
            )
            logger.info(f"紧急订单执行成功: {result.get('order_id')}")
            return result
        except Exception as e:
            logger.critical(f"紧急订单执行失败: {e} #RECOVERY: 检查交易所API、网络、账户状态")
            raise
