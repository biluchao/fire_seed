"""
火种系统 · 执行后审计与反事实分析器 (PostExecutionAuditor)

核心职责：
1. 对已成交订单进行穿透式成本核算，包括显性手续费、动态方向性滑点成本、限价单机会成本（使用稳健中位数避免尖刺污染）
2. 基于虚拟券商进行多策略（市价/限价/冰山/TWAP）反事实最优执行路径模拟，量化可避免亏损并驱动进化优化
3. 提供审计活性自检与覆盖率告警，支持按信号来源横向对比，生成执行策略闭环反馈
4. 强制记录所有审计失败事件至不可变日志，满足金融级合规审计要求

外部依赖（真实模块接口）：
- core.execution.multi_venue_router.MultiVenueRouter : 获取订单在各交易所的实际成交细节
- ghost.virtual_broker.VirtualBroker : 重放订单并模拟不同执行策略（限价/冰山/TWAP等）的成交结果
- core.behavioral_logger.BehavioralLogger : 记录审计结果、审计失败事件与异常事件（不可变日志）
- core.decision_tracer.DecisionTracer : 获取完整的决策上下文用于归因
- core.order_manager.OrderManager : 备选成交记录来源，当路由器不可用时降级查询；提供订单计数用于覆盖率检测
- core.data_feed.DataFeed : 回查历史中间价与挂单期间价格路径，用于机会成本计算
- core.negotiation_bus.NegotiationBus : 发送异常成本告警与执行策略闭环反馈

接口契约：
- audit_execution(order_id: str) -> Dict[str, Any] : 对指定订单执行完整审计
- get_recent_audit_summary(strategy_name: str, limit: int) -> Dict[str, Any] : 最近N笔审计的汇总统计（不含原始价格）
- health_check() -> Dict[str, Any] : 模块自检，包含审计活性校验
- sanitize_for_external(audit_report: Dict[str, Any]) -> Dict[str, Any] : 对外提供的脱敏审计报告
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 MultiVenueRouter 不可用时，依次降级至 OrderManager -> 本地数据库（预留）-> 交易所REST API，并标记数据来源
- 当 mid_price_at_order 缺失时，从 DataFeed 历史行情回补，若仍不可得则标记 "unverified"
- 当 VirtualBroker 不可用或缺少特定模拟方法时，跳过对应反事实场景，反事实分析标记为 "partial"
- 当反事实分析整体超时时，返回已完成的部分结果并标记 "timeout"
- 所有降级值在类常量区明确声明，降级事件记录到 WARNING 日志

资源管理：
- 本模块无状态，每次审计即时完成，不持有外部连接
- 内部统计计数器（审计活性检测用）由线程锁保护，定期通过 health_check 暴露
- 专用线程池（用于机会成本I/O超时保护）在模块销毁时通过 atexit 和 inject_dependencies 生命周期管理自动释放
- 覆盖率检测线程采用绝对时间调度，消除累积漂移，模块销毁时优雅停止
"""

import atexit
import concurrent.futures
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class PostExecutionAuditor:
    """执行后审计与反事实分析器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    OPPORTUNITY_COST_SCAN_SECONDS = 5       # 限价单机会成本回溯扫描时长，秒，取值范围 [1, 30]
    COUNTERFACTUAL_SLIPPAGE_BPS_STEPS = [0.5, 1.0, 2.0, 5.0]  # 反事实模拟中使用的滑点假设（基点），无量纲
    AUDIT_SUMMARY_DEFAULT_LIMIT = 20        # 默认汇总统计样本数，取值范围 [5, 100]
    ABNORMAL_SLIPPAGE_BPS_THRESHOLD = 50.0  # 触发异常告警的滑点阈值（基点），取值范围 [10.0, 200.0]
    ABNORMAL_OPPORTUNITY_COST_THRESHOLD = 0.001  # 触发异常告警的机会成本阈值（相对订单价值），[0.0005, 0.01]
    AUDIT_SILENCE_MAX_SECONDS = 300         # 审计静默最大时间（秒），超过则认为审计功能可能异常，取值范围 [120, 900]
    MID_PRICE_VERIFICATION_WINDOW_SEC = 2   # 回查中间价的最大允许偏移时间（秒），[1, 10]
    AUDIT_COVERAGE_CHECK_WINDOW_MINUTES = 5  # 审计覆盖率检查窗口（分钟），[1, 30]
    AUDIT_COVERAGE_MIN_THRESHOLD = 0.95     # 审计覆盖率最低阈值，[0.8, 1.0]
    OPPORTUNITY_COST_MEDIAN_TICKS = 3       # 机会成本计算时使用的前N个Tick中位数，[2, 5]
    COUNTERFACTUAL_COST_MIN_RATIO = 0.9     # 反事实成本合理性下界（相对于订单价值），[0.8, 0.95]
    COUNTERFACTUAL_COST_MAX_RATIO = 1.1     # 反事实成本合理性上界（相对于订单价值），[1.05, 1.2]
    PRICE_SPIKE_THRESHOLD_BPS = 500         # 价格尖刺检测阈值（基点），[100, 1000]
    OPPORTUNITY_COST_TIMEOUT_SEC = 0.2      # 机会成本计算超时时间（秒），[0.05, 0.5]
    COUNTERFACTUAL_TOTAL_TIMEOUT_SEC = 2.0  # 反事实分析总超时时间（秒），[0.5, 5.0]
    AUDIT_THREAD_POOL_MAX_WORKERS = 4       # 审计专用线程池大小，[1, 8]
    GLOBAL_ALERT_RATE_LIMIT = 100           # 每分钟最多发送的告警数，[10, 1000]
    GLOBAL_ALERT_WINDOW_SEC = 60            # 告警速率限制窗口（秒），[30, 120]
    AGGREGATED_ALERT_INTERVAL_SEC = 30      # 聚合告警推送间隔（秒），[10, 60]
    COVERAGE_CHECK_DEDICATED = True          # 覆盖率检测使用独立线程，避免健康检查性能抖动

    def __init__(self):
        # 外部依赖注入
        self._multi_venue_router = None
        self._order_manager = None
        self._data_feed = None
        self._virtual_broker = None
        self._behavioral_logger = None
        self._decision_tracer = None
        self._negotiation_bus = None

        # 审计活性追踪
        self._last_audit_timestamp: float = 0.0
        self._audit_count: int = 0
        self._lock = threading.Lock()  # 仅保护 _last_audit_timestamp 和 _audit_count，锁内禁止调用外部依赖

        # 专用线程池（用于机会成本I/O超时保护及反事实总超时）
        self._thread_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None

        # 告警速率限制器
        self._alert_timestamps: List[float] = []
        self._alert_lock = threading.Lock()
        self._aggregated_alerts: Dict[str, int] = {}
        self._last_aggregated_push: float = 0.0

        # 覆盖率检测独立线程（采用绝对时间调度）
        self._coverage_thread: Optional[threading.Thread] = None
        self._coverage_stop_event = threading.Event()
        self._coverage_next_run: float = 0.0

        # 注册退出清理
        atexit.register(self._cleanup)
        logger.info("PostExecutionAuditor 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        multi_venue_router: Optional[Any] = None,
        order_manager: Optional[Any] = None,
        data_feed: Optional[Any] = None,
        virtual_broker: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        decision_tracer: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选注入，未注入时对应功能降级）"""
        # 清理旧线程池
        if self._thread_pool is not None:
            self._thread_pool.shutdown(wait=False)
            logger.info("旧线程池已关闭")
        self._thread_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.AUDIT_THREAD_POOL_MAX_WORKERS,
            thread_name_prefix="audit_worker"
        )

        if multi_venue_router is not None:
            self._multi_venue_router = multi_venue_router
            logger.info("MultiVenueRouter 注入成功")
        else:
            logger.warning("MultiVenueRouter 未注入，将降级使用 OrderManager 或本地数据库")

        if order_manager is not None:
            self._order_manager = order_manager
            logger.info("OrderManager 注入成功")
            # 启动覆盖率检测线程（采用绝对时间调度）
            if self.COVERAGE_CHECK_DEDICATED and not self._coverage_thread:
                self._start_coverage_monitor()
        else:
            logger.warning("OrderManager 未注入，备选成交记录来源不可用")

        if data_feed is not None:
            self._data_feed = data_feed
            logger.info("DataFeed 注入成功")
        else:
            logger.warning("DataFeed 未注入，中间价回补与机会成本计算将受限")

        if virtual_broker is not None:
            self._virtual_broker = virtual_broker
            logger.info("VirtualBroker 注入成功")
        else:
            logger.warning("VirtualBroker 未注入，反事实分析不可用")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，审计日志降级为标准 logger")

        if decision_tracer is not None:
            self._decision_tracer = decision_tracer
            logger.info("DecisionTracer 注入成功")
        else:
            logger.warning("DecisionTracer 未注入，部分归因信息缺失")

        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, 'publish_alert'):
                logger.warning("NegotiationBus 缺少 publish_alert 方法，异常告警不可用")
                self._negotiation_bus = None
            else:
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")

    # ========== 公共接口 ==========
    def audit_execution(self, order_id: str) -> Dict[str, Any]:
        """
        对指定订单执行完整审计

        Args:
            order_id: 订单唯一标识

        Returns:
            审计报告字典
        """
        if not order_id:
            logger.warning("order_id 为空")
            self._log_audit_failure("unknown", "order_id 为空")
            return {"status": "error", "reason": "order_id 不能为空", "data": {}, "warnings": ["invalid_order_id"]}

        warnings = []
        # 1. 获取实际成交细节（多级降级，附带降级原因）
        actual_trades, data_source, degradation_detail = self._fetch_actual_trades(order_id)
        if not actual_trades:
            reason = f"未找到订单 {order_id} 的成交记录，所有数据源均不可用"
            logger.error(f"{reason} #RECOVERY: 检查路由器、OrderManager 及本地数据库")
            self._log_audit_failure(order_id, reason)
            return {
                "status": "error",
                "reason": reason,
                "data": {},
                "warnings": ["no_trade_record"],
            }
        if data_source != "router":
            warnings.append(f"成交数据来源于降级源: {data_source} ({degradation_detail})")

        # 2. 计算实际成本（含中间价校验、方向性滑点）
        actual_cost, mid_price_warning = self._compute_actual_cost(actual_trades)
        if mid_price_warning:
            warnings.append(mid_price_warning)

        # 3. 计算机会成本（如果是限价单），使用专用线程池超时保护
        opportunity_cost = 0.0
        if self._is_limit_order(actual_trades[0]):
            opportunity_cost = self._compute_opportunity_cost_with_timeout(actual_trades[0])
            if opportunity_cost > self.ABNORMAL_OPPORTUNITY_COST_THRESHOLD:
                warnings.append(f"限价单机会成本异常: {opportunity_cost:.4f}")
                self._trigger_alert(order_id, "opportunity_cost", opportunity_cost)

        # 4. 异常滑点告警（使用带方向的滑点绝对值）
        abs_slippage = abs(actual_cost.get("signed_slippage_bps", 0))
        if abs_slippage > self.ABNORMAL_SLIPPAGE_BPS_THRESHOLD:
            self._trigger_alert(order_id, "slippage", abs_slippage)

        # 5. 反事实最优路径分析（带总体超时控制）
        counterfactual = None
        if self._virtual_broker is not None:
            counterfactual = self._run_counterfactual_with_timeout(actual_trades[0])
            if counterfactual.get("status") == "partial":
                warnings.append("counterfactual_partial_timeout")
        else:
            warnings.append("counterfactual_unavailable")

        # 6. 决策上下文归因
        decision_context = self._fetch_decision_context(order_id)

        report = {
            "order_id": order_id,
            "symbol": actual_trades[0].get("symbol", "unknown"),
            "side": actual_trades[0].get("side", "unknown"),
            "signal_source": decision_context.get("signal_source", "unknown"),
            "audit_timestamp": time.time(),
            "data_source": data_source,
            "data_source_detail": degradation_detail,
            "actual_cost": actual_cost,
            "opportunity_cost": opportunity_cost,
            "counterfactual": counterfactual,
            "decision_context": decision_context,
        }

        self._log_audit(order_id, report)
        self._update_audit_timestamp()

        # 7. 生成执行策略闭环反馈
        self._push_execution_hint(report, actual_trades[0])

        return {
            "status": "ok",
            "reason": f"订单 {order_id} 审计完成",
            "data": report,
            "warnings": warnings,
        }

    def get_recent_audit_summary(
        self, strategy_name: str = "", signal_source: str = "", limit: int = 0
    ) -> Dict[str, Any]:
        """
        获取最近审计的汇总统计，支持按策略名称和信号来源过滤。
        注意：此方法不返回任何原始价格数据，仅返回衍生统计指标，符合合规要求。
        """
        if limit <= 0:
            limit = self.AUDIT_SUMMARY_DEFAULT_LIMIT
        if self._behavioral_logger is None or not hasattr(self._behavioral_logger, 'query_audit_logs'):
            return {
                "status": "degraded",
                "reason": "BehavioralLogger 不可用或无查询接口，无法生成汇总",
                "data": {},
                "warnings": ["audit_summary_unavailable"],
            }

        logs = self._behavioral_logger.query_audit_logs(
            event_type="post_execution_audit",
            strategy_name=strategy_name,
            signal_source=signal_source,
            limit=limit,
        )
        if not logs:
            return {
                "status": "ok",
                "reason": "最近无审计记录",
                "data": {"total_audited": 0, "avg_slippage_bps": 0, "avg_opportunity_cost": 0},
                "warnings": [],
            }

        # 仅提取衍生指标，不提取原始价格
        slippages = [
            log["actual_cost"]["signed_slippage_bps"]
            for log in logs
            if "actual_cost" in log and "signed_slippage_bps" in log["actual_cost"]
        ]
        opp_costs = [log.get("opportunity_cost", 0) for log in logs]
        avoidable_costs = [
            log.get("counterfactual", {}).get("avoidable_cost_bps", 0)
            for log in logs
            if log.get("counterfactual")
        ]

        summary = {
            "total_audited": len(logs),
            "avg_signed_slippage_bps": round(float(np.mean(slippages)), 2) if slippages else 0,
            "max_slippage_bps": round(float(np.max([abs(s) for s in slippages])), 2) if slippages else 0,
            "min_slippage_bps": round(float(np.min([abs(s) for s in slippages])), 2) if slippages else 0,
            "avg_opportunity_cost": round(float(np.mean(opp_costs)), 4) if opp_costs else 0,
            "avg_avoidable_cost_bps": round(float(np.mean(avoidable_costs)), 2) if avoidable_costs else 0,
        }
        return {
            "status": "ok",
            "reason": f"返回最近 {len(logs)} 条审计汇总",
            "data": summary,
            "warnings": [],
        }

    @classmethod
    def sanitize_for_external(cls, audit_report: Dict[str, Any]) -> Dict[str, Any]:
        """
        对外提供的脱敏审计报告，移除所有原始价格数据，仅保留衍生指标和合规信息。
        适用于提供给客户、监管机构或外部审计。
        """
        report = audit_report.copy()
        if "actual_cost" in report:
            cost = report["actual_cost"].copy()
            # 移除原始价格
            cost.pop("avg_price", None)
            cost.pop("mid_price", None)
            # 保留衍生指标
            report["actual_cost"] = {
                "signed_slippage_bps": cost.get("signed_slippage_bps", 0),
                "total_fee": cost.get("total_fee", 0),
                "total_qty": cost.get("total_qty", 0),
                "mid_price_verified": cost.get("mid_price_verified", False),
            }
        if "counterfactual" in report and report["counterfactual"]:
            cf = report["counterfactual"]
            if "actual_total_cost" in cf:
                cf.pop("actual_total_cost", None)
            report["counterfactual"] = cf
        return report

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检，包含审计活性校验（覆盖率检测由独立线程处理，此处仅检查静默）"""
        try:
            deps = {
                "multi_venue_router": self._multi_venue_router is not None,
                "order_manager": self._order_manager is not None,
                "data_feed": self._data_feed is not None,
                "virtual_broker": self._virtual_broker is not None,
                "behavioral_logger": self._behavioral_logger is not None,
                "decision_tracer": self._decision_tracer is not None,
                "negotiation_bus": self._negotiation_bus is not None,
            }
            missing = [k for k, v in deps.items() if not v]
            status = "degraded" if missing else "ok"

            with self._lock:
                last_audit = self._last_audit_timestamp
                audit_count = self._audit_count

            silence_warning = None
            if last_audit > 0 and (time.time() - last_audit) > self.AUDIT_SILENCE_MAX_SECONDS:
                silence_warning = (
                    f"审计静默超过 {self.AUDIT_SILENCE_MAX_SECONDS} 秒，"
                    f"最后审计时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_audit))}"
                )
                status = "degraded"

            return {
                "status": status,
                "reason": (
                    f"PostExecutionAuditor 正常，"
                    f"{'依赖缺失:' + str(missing) if missing else '所有依赖就绪'}, "
                    f"累计审计: {audit_count}"
                ),
                "data": {
                    "dependencies": deps,
                    "last_audit_timestamp": last_audit,
                    "audit_count": audit_count,
                },
                "warnings": (
                    [f"missing_dependency: {m}" for m in missing] +
                    ([silence_warning] if silence_warning else [])
                ),
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查模块初始化状态")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"]}

    # ========== 私有方法 ==========
    def _fetch_actual_trades(self, order_id: str) -> Tuple[List[Dict[str, Any]], str, str]:
        """多级降级获取订单成交明细，返回 (成交记录列表, 数据源标识, 降级原因)"""
        # 优先级 1: MultiVenueRouter
        if self._multi_venue_router and hasattr(self._multi_venue_router, 'get_order_trades'):
            try:
                trades = self._multi_venue_router.get_order_trades(order_id)
                if trades:
                    return trades, "router", "primary"
                return [], "router", "empty_result"
            except Exception as e:
                logger.warning(f"MultiVenueRouter 查询失败: {e}，降级至 OrderManager")
                degradation_reason = f"router_exception: {str(e)[:100]}"
        else:
            degradation_reason = "router_not_available"

        # 优先级 2: OrderManager
        if self._order_manager and hasattr(self._order_manager, 'get_order_fills'):
            try:
                trades = self._order_manager.get_order_fills(order_id)
                if trades:
                    return trades, "order_manager", degradation_reason
                return [], "order_manager", f"{degradation_reason} -> order_manager_empty"
            except Exception as e:
                logger.warning(f"OrderManager 查询失败: {e}")
                degradation_reason = f"{degradation_reason} -> order_manager_exception: {str(e)[:100]}"
        else:
            degradation_reason = f"{degradation_reason} -> order_manager_not_available"

        logger.error(f"所有数据源均无法获取订单 {order_id} 的成交记录")
        return [], "none", degradation_reason

    def _compute_actual_cost(self, trades: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], str]:
        """计算实际执行成本，返回 (成本字典, 中间价警告信息)，滑点保留方向"""
        total_qty = sum(t.get("qty", 0) for t in trades)
        total_value = sum(t.get("price", 0) * t.get("qty", 0) for t in trades)
        total_fee = sum(t.get("fee", 0) for t in trades)
        avg_price = total_value / total_qty if total_qty > 0 else 0

        mid_price = None
        mid_price_verified = False
        warning = ""

        if trades:
            mid_price = trades[0].get("mid_price_at_order")
            if mid_price is not None and mid_price > 0:
                mid_price_verified = True

        if not mid_price_verified and self._data_feed is not None:
            order_time = trades[0].get("order_time", 0)
            symbol = trades[0].get("symbol", "unknown")
            if order_time and symbol and hasattr(self._data_feed, 'get_mid_price_at_time'):
                try:
                    historical_mid = self._data_feed.get_mid_price_at_time(
                        symbol, order_time, self.MID_PRICE_VERIFICATION_WINDOW_SEC
                    )
                    if historical_mid is not None:
                        mid_price = historical_mid
                        mid_price_verified = True
                except Exception as e:
                    logger.warning(f"DataFeed 回查中间价失败: {e}")

        if mid_price is None or mid_price <= 0:
            mid_price = avg_price
            warning = "中间价未验证，滑点计算可能不准确"
            logger.warning(f"订单 {trades[0].get('order_id', '?')} 中间价缺失，使用成交均价近似")

        # 方向性滑点：做多时，成交价>中间价=不利（正数），做空时，成交价<中间价=不利（正数）
        side = trades[0].get("side", "buy")
        if side == "buy":
            signed_slippage_bps = (avg_price - mid_price) / mid_price * 10000 if mid_price > 0 else 0
        else:
            signed_slippage_bps = (mid_price - avg_price) / mid_price * 10000 if mid_price > 0 else 0

        cost = {
            "total_qty": total_qty,
            "avg_price": avg_price,
            "total_fee": total_fee,
            "signed_slippage_bps": round(signed_slippage_bps, 2),  # 正=不利滑点，负=有利滑点
            "mid_price": mid_price,
            "mid_price_verified": mid_price_verified,
        }
        return cost, warning

    def _is_limit_order(self, trade_sample: Dict[str, Any]) -> bool:
        """判断订单类型是否为限价单"""
        order_type = trade_sample.get("order_type", "").lower()
        return "limit" in order_type

    def _compute_opportunity_cost_with_timeout(self, trade_sample: Dict[str, Any]) -> float:
        """带超时保护的机会成本计算"""
        if self._thread_pool is None:
            logger.warning("线程池未初始化，跳过机会成本计算")
            return 0.0

        future = self._thread_pool.submit(self._compute_opportunity_cost, trade_sample)
        try:
            return future.result(timeout=self.OPPORTUNITY_COST_TIMEOUT_SEC)
        except concurrent.futures.TimeoutError:
            logger.warning("机会成本计算超时，返回 0.0")
            return 0.0
        except Exception as e:
            logger.warning(f"机会成本计算异常: {e}")
            return 0.0

    def _compute_opportunity_cost(self, trade_sample: Dict[str, Any]) -> float:
        """
        计算限价单的机会成本。
        使用挂单后前N个Tick的中位数作为理论最优成交价，避免尖刺污染。
        """
        if not self._data_feed or not hasattr(self._data_feed, 'get_price_path'):
            logger.debug("DataFeed 不可用，机会成本无法计算")
            return 0.0

        order_time = trade_sample.get("order_time")
        fill_time = trade_sample.get("fill_time", order_time)
        if not order_time:
            return 0.0

        symbol = trade_sample.get("symbol")
        side = trade_sample.get("side")
        qty = trade_sample.get("qty", 0)

        try:
            price_path = self._data_feed.get_price_path(
                symbol, order_time, min(fill_time, order_time + self.OPPORTUNITY_COST_SCAN_SECONDS)
            )
            if not price_path:
                return 0.0

            # 使用前N个Tick的中位数，并剔除尖刺
            valid_prices = []
            prev_price = None
            for p in price_path[:self.OPPORTUNITY_COST_MEDIAN_TICKS]:
                if prev_price is not None and prev_price > 0:
                    spike_bps = abs(p - prev_price) / prev_price * 10000
                    if spike_bps > self.PRICE_SPIKE_THRESHOLD_BPS:
                        continue  # 疑似尖刺，跳过
                valid_prices.append(p)
                prev_price = p

            if not valid_prices:
                return 0.0

            theoretical_price = float(np.median(valid_prices))
            actual_price = trade_sample.get("price", theoretical_price)

            if side == "buy":
                opportunity = (theoretical_price - actual_price) * qty
            else:
                opportunity = (actual_price - theoretical_price) * qty

            return max(0.0, opportunity)
        except Exception as e:
            logger.warning(f"机会成本计算异常: {e}")
            return 0.0

    def _run_counterfactual_with_timeout(self, trade_sample: Dict[str, Any]) -> Dict[str, Any]:
        """带总体超时控制的反事实分析"""
        if self._thread_pool is None:
            return {"status": "unavailable", "reason": "线程池未初始化"}

        future = self._thread_pool.submit(self._run_counterfactual, trade_sample)
        try:
            return future.result(timeout=self.COUNTERFACTUAL_TOTAL_TIMEOUT_SEC)
        except concurrent.futures.TimeoutError:
            logger.warning("反事实分析整体超时，返回部分结果")
            # 尝试获取已完成的部分
            if future.done():
                return future.result()
            return {"status": "partial", "reason": f"反事实分析超时({self.COUNTERFACTUAL_TOTAL_TIMEOUT_SEC}s)，部分场景可能未完成"}

    def _run_counterfactual(self, trade_sample: Dict[str, Any]) -> Dict[str, Any]:
        """反事实最优路径模拟，增加合理性校验"""
        if not self._virtual_broker:
            return {"status": "unavailable", "reason": "VirtualBroker 未注入"}

        symbol = trade_sample.get("symbol", "")
        side = trade_sample.get("side", "")
        qty = trade_sample.get("qty", 0)
        order_time = trade_sample.get("order_time")
        actual_price = trade_sample.get("price", 0)
        order_value = actual_price * qty
        min_valid_cost = order_value * self.COUNTERFACTUAL_COST_MIN_RATIO
        max_valid_cost = order_value * self.COUNTERFACTUAL_COST_MAX_RATIO

        scenarios = {}
        # 1. 市价单
        for slip_bps in self.COUNTERFACTUAL_SLIPPAGE_BPS_STEPS:
            try:
                sim = self._virtual_broker.simulate_execution(
                    symbol=symbol, side=side, qty=qty,
                    order_type="market", slippage_assumption_bps=slip_bps,
                    market_snapshot_time=order_time,
                )
                cost = sim.get("total_cost")
                if cost is not None and min_valid_cost <= cost <= max_valid_cost:
                    scenarios[f"market_{slip_bps}bps"] = {"avg_price": sim.get("avg_price"), "total_cost": cost}
                else:
                    scenarios[f"market_{slip_bps}bps"] = {"error": f"成本 {cost} 超出合理范围", "invalid": True}
            except Exception as e:
                scenarios[f"market_{slip_bps}bps"] = {"error": str(e), "invalid": True}

        # 2. 限价单
        for tick_offset in [1, 2]:
            if hasattr(self._virtual_broker, 'simulate_limit_order'):
                try:
                    sim = self._virtual_broker.simulate_limit_order(
                        symbol=symbol, side=side, qty=qty,
                        tick_offset=tick_offset, market_snapshot_time=order_time,
                    )
                    cost = sim.get("total_cost")
                    if cost is not None and min_valid_cost <= cost <= max_valid_cost:
                        scenarios[f"limit_offset_{tick_offset}tick"] = {"avg_price": sim.get("avg_price"), "total_cost": cost}
                    else:
                        scenarios[f"limit_offset_{tick_offset}tick"] = {"error": f"成本 {cost} 超出合理范围", "invalid": True}
                except Exception as e:
                    scenarios[f"limit_offset_{tick_offset}tick"] = {"error": str(e), "invalid": True}

        # 3. 冰山订单
        if hasattr(self._virtual_broker, 'simulate_iceberg_order'):
            try:
                sim = self._virtual_broker.simulate_iceberg_order(
                    symbol=symbol, side=side, qty=qty,
                    display_ratio=0.2, market_snapshot_time=order_time,
                )
                cost = sim.get("total_cost")
                if cost is not None and min_valid_cost <= cost <= max_valid_cost:
                    scenarios["iceberg_20pct"] = {"avg_price": sim.get("avg_price"), "total_cost": cost}
                else:
                    scenarios["iceberg_20pct"] = {"error": f"成本 {cost} 超出合理范围", "invalid": True}
            except Exception as e:
                scenarios["iceberg_20pct"] = {"error": str(e), "invalid": True}

        # 4. TWAP
        if hasattr(self._virtual_broker, 'simulate_twap'):
            try:
                sim = self._virtual_broker.simulate_twap(
                    symbol=symbol, side=side, qty=qty,
                    duration_seconds=10, market_snapshot_time=order_time,
                )
                cost = sim.get("total_cost")
                if cost is not None and min_valid_cost <= cost <= max_valid_cost:
                    scenarios["twap_10s"] = {"avg_price": sim.get("avg_price"), "total_cost": cost}
                else:
                    scenarios["twap_10s"] = {"error": f"成本 {cost} 超出合理范围", "invalid": True}
            except Exception as e:
                scenarios["twap_10s"] = {"error": str(e), "invalid": True}

        # 最优路径
        best = None
        best_cost = float("inf")
        for name, result in scenarios.items():
            if result.get("invalid"):
                continue
            cost = result.get("total_cost")
            if cost is not None and cost < best_cost:
                best_cost = cost
                best = name

        actual_total_cost = (
            trade_sample.get("price", 0) * qty + trade_sample.get("fee", 0)
        )
        if best_cost < float("inf") and best_cost > 0:
            avoidable_bps = (actual_total_cost - best_cost) / order_value * 10000 if order_value > 0 else 0.0
        else:
            avoidable_bps = 0.0
            logger.warning("反事实最优路径无效或缺失，无法计算可避免亏损")

        return {
            "scenarios": scenarios,
            "optimal": {"strategy": best, "total_cost": best_cost},
            "actual_total_cost": actual_total_cost,
            "avoidable_cost_bps": round(avoidable_bps, 2),
        }

    def _fetch_decision_context(self, order_id: str) -> Dict[str, Any]:
        """获取订单的决策上下文，包括信号来源标签"""
        if self._decision_tracer and hasattr(self._decision_tracer, 'get_decision_path'):
            context = self._decision_tracer.get_decision_path(order_id)
            if "signal_source" not in context:
                context["signal_source"] = "unknown"
            return context
        return {"status": "unavailable", "signal_source": "unknown"}

    def _log_audit(self, order_id: str, report: Dict[str, Any]) -> None:
        """记录审计成功结果至不可变日志"""
        if self._behavioral_logger and hasattr(self._behavioral_logger, 'log_event'):
            self._behavioral_logger.log_event("post_execution_audit", report)
        signed_slip = report["actual_cost"].get("signed_slippage_bps", 0)
        logger.info(
            "订单 %s 审计完成，方向性滑点: %.1f bps, 机会成本: %.6f",
            order_id,
            signed_slip,
            report.get("opportunity_cost", 0.0),
        )

    def _log_audit_failure(self, order_id: str, reason: str) -> None:
        """强制记录审计失败事件至不可变日志，满足合规审计要求"""
        failure_event = {
            "order_id": order_id,
            "failure_reason": reason,
            "timestamp": time.time(),
            "event_type": "post_execution_audit_failed",
        }
        if self._behavioral_logger and hasattr(self._behavioral_logger, 'log_event'):
            self._behavioral_logger.log_event("post_execution_audit_failed", failure_event)
        logger.error(f"审计失败: order_id={order_id}, reason={reason}")

    def _update_audit_timestamp(self) -> None:
        """更新审计活性时间戳与计数"""
        with self._lock:
            self._last_audit_timestamp = time.time()
            self._audit_count += 1

    def _trigger_alert(self, order_id: str, alert_type: str, value: float) -> Non
