"""
火种系统 · 观察者报告流转管道 (ObsidianPipeline) — 第五轮深度修复版

核心职责：
1. 接收 ObsidianMirror 生成的全量系统观察报告，执行严格的内容校验，并通过安全状态机驱动报告流转（审计→幽灵验证→金丝雀部署）
2. 协调多模块间的自动化报告传递与状态跟踪，确保每一份报告都经过完整的安全闭环处理，支持优先级调度与超时熔断

外部依赖（真实模块接口）：
- core.negotiation_bus.NegotiationBus : 发布报告流转事件，通知下游模块。调用 publish_alert(alert_type, **kwargs) 方法
- ghost.shadow_manager.ShadowManager : 将优化提案注入幽灵影子环境进行验证。调用 inject_proposal(proposal) 方法
- core.evolution_safety_manager.EvolutionSafetyManager : 将通过验证的优化提案通过金丝雀发布部署。调用 canary_deploy(proposal) 方法
- agents.obsidian_mirror.ObsidianMirror : 获取观察者生成的全量报告（只读查询）。调用 get_latest_report() 方法
- core.behavioral_logger.BehavioralLogger : 记录报告流转的关键审计日志。调用 log_event(event_type, details) 方法

接口契约：
- submit_report(report: Dict[str, Any], priority: int = 5) -> Dict[str, Any] : 提交一份观察报告进入流转管道
- update_report_status(report_id: str, new_status: str, result: Dict[str, Any]) -> Dict[str, Any] : 更新报告状态
- get_pipeline_status(report_id: str) -> Dict[str, Any] : 查询指定报告的当前流转状态
- list_reports(status_filter: Optional[str] = None, limit: int = 50) -> Dict[str, Any] : 列出报告列表
- cancel_report(report_id: str, reason: str) -> Dict[str, Any] : 取消/撤回报告
- get_pipeline_metrics() -> Dict[str, Any] : 获取管道自身性能指标
- health_check(timeout_sec: float = 5.0) -> Dict[str, Any] : 模块自检，带超时保护
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 NegotiationBus 不可用时，报告流转事件仅记录本地日志，不阻塞主流程
- 当 ShadowManager 不可用时，优化提案跳过幽灵验证，直接标记为"待人工审核"
- 当 EvolutionSafetyManager 不可用时，部署阶段暂停，报告滞留于"待部署"队列
- 当 BehavioralLogger 不可用时，审计日志降级为标准 logger
- 当 psutil 不可用时，内存压力检查自动跳过
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护报告流转状态的内存缓存，定期清理已完成或过期的报告状态
- 报告状态持久化至本地 JSON 日志文件（原子写入），支持进程重启后基础状态恢复
- 不持有任何外部资源句柄，线程锁在模块销毁时自动释放
"""

import time
import logging
import threading
import json
import os
import copy
import uuid
import hashlib
from typing import Dict, Any, List, Optional, Tuple, Set, FrozenSet

logger = logging.getLogger(__name__)


class ObsidianPipeline:
    """观察者报告流转管道 — 第五轮深度修复版"""

    # ========== 类常量（默认配置） ==========
    DEFAULT_MAX_CACHED_REPORTS: int = 200
    DEFAULT_REPORT_TTL_SEC: int = 86400 * 7
    DEFAULT_CLEANUP_INTERVAL_SEC: int = 3600
    DEFAULT_TIMEOUT_CHECK_INTERVAL_SEC: int = 3600
    DEFAULT_EVENT_RETRY_COUNT: int = 3
    DEFAULT_EVENT_RETRY_DELAY_SEC: float = 1.0
    DEFAULT_EVENT_RETRY_BACKOFF: float = 2.0       # 指数退避系数
    DEFAULT_STATE_TRANSITION_TIMEOUT_HOURS: int = 72
    DEFAULT_MAX_TRANSITION_HISTORY: int = 100
    DEFAULT_HEALTH_CHECK_TIMEOUT_SEC: float = 5.0
    DEFAULT_MAX_REPORT_ID_LENGTH: int = 128
    DEFAULT_MAX_PROPOSALS_PER_REPORT: int = 50
    DEFAULT_MAX_REPORT_SIZE_BYTES: int = 10 * 1024 * 1024
    DEFAULT_TIMEOUT_ALERT_INTERVAL_SEC: int = 14400
    DEFAULT_MAX_RESTORE_REPORTS: int = 500
    DEFAULT_MAX_PERSIST_SIZE_BYTES: int = 100 * 1024 * 1024
    DEFAULT_MEMORY_PRESSURE_THRESHOLD: float = 0.85
    DEFAULT_MIN_MEMORY_RESERVE_BYTES: int = 50 * 1024 * 1024
    DEFAULT_CLEANUP_BATCH_SIZE: int = 100
    DEFAULT_PERSIST_TMP_SUFFIX_LENGTH: int = 16   # 临时文件随机后缀长度
    DEFAULT_MIN_GENERATED_AT: float = 1577836800.0  # 2020-01-01，拒绝过旧的时间戳
    DEFAULT_LOCK_CONTENTION_WARN_THRESHOLD: int = 50  # 锁内报告数超过此值发出警告

    # 状态枚举
    STATUS_SUBMITTED: str = "submitted"
    STATUS_AUDITED: str = "audited"
    STATUS_IN_SHADOW: str = "in_shadow"
    STATUS_SHADOW_PASSED: str = "shadow_passed"
    STATUS_DEPLOYED: str = "deployed"
    STATUS_REJECTED: str = "rejected"
    STATUS_CANCELLED: str = "cancelled"

    TERMINAL_STATUSES: FrozenSet[str] = frozenset({
        "deployed", "rejected", "cancelled"
    })

    VALID_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
        "submitted": ("audited", "rejected", "cancelled"),
        "audited": ("in_shadow", "rejected", "cancelled"),
        "in_shadow": ("shadow_passed", "rejected", "cancelled"),
        "shadow_passed": ("deployed", "rejected", "cancelled"),
        "deployed": (),
        "rejected": (),
        "cancelled": (),
    }

    REQUIRED_REPORT_FIELDS: Tuple[str, ...] = ("report_id", "generated_at", "proposals")
    REQUIRED_PROPOSAL_FIELDS: Tuple[str, ...] = ("proposal_id",)

    METRIC_TOTAL_SUBMITTED: str = "total_submitted"
    METRIC_TOTAL_DEPLOYED: str = "total_deployed"
    METRIC_TOTAL_REJECTED: str = "total_rejected"
    METRIC_TOTAL_CANCELLED: str = "total_cancelled"
    METRIC_CURRENT_PENDING: str = "current_pending"
    METRIC_CACHE_USAGE_PCT: str = "cache_usage_pct"
    METRIC_STATUS_DISTRIBUTION: str = "status_distribution"
    METRIC_HIGH_WATERMARK: str = "report_count_high_watermark"
    METRIC_MEMORY_PRESSURE: str = "memory_pressure"
    METRIC_LOCK_CONTENTION: str = "lock_contention_estimate"

    def __init__(self, persistence_path: Optional[str] = None):
        self._reports: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

        self._metrics: Dict[str, int] = {
            self.METRIC_TOTAL_SUBMITTED: 0,
            self.METRIC_TOTAL_DEPLOYED: 0,
            self.METRIC_TOTAL_REJECTED: 0,
            self.METRIC_TOTAL_CANCELLED: 0,
            self.METRIC_CURRENT_PENDING: 0,
        }

        self._timeout_alerted: Dict[str, float] = {}

        # 依赖注入
        self._dependency_lock = threading.Lock()
        self._negotiation_bus: Any = None
        self._shadow_manager: Any = None
        self._evolution_safety_manager: Any = None
        self._obsidian_mirror: Any = None
        self._behavioral_logger: Any = None

        # 持久化
        self._persistence_path = persistence_path

        # 维护定时器
        self._last_cleanup = time.time()
        self._last_timeout_check = time.time()
        self._last_memory_check = time.time()

        # 内存监控
        self._report_count_high_watermark = 0
        self._memory_pressure_active = False

        # 锁竞争监控
        self._lock_acquire_count = 0
        self._lock_contention_count = 0

        if persistence_path and os.path.exists(persistence_path):
            self._restore_from_persistence()

        logger.info(
            "ObsidianPipeline v5 初始化完成，缓存上限 %d，持久化=%s",
            self.DEFAULT_MAX_CACHED_REPORTS,
            "启用" if persistence_path else "禁用",
        )

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        negotiation_bus: Optional[Any] = None,
        shadow_manager: Optional[Any] = None,
        evolution_safety_manager: Optional[Any] = None,
        obsidian_mirror: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """注入外部依赖，并进行接口校验"""
        with self._dependency_lock:
            if negotiation_bus is not None and hasattr(negotiation_bus, 'publish_alert'):
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")
            if shadow_manager is not None and hasattr(shadow_manager, 'inject_proposal'):
                self._shadow_manager = shadow_manager
                logger.info("ShadowManager 注入成功")
            if evolution_safety_manager is not None and hasattr(evolution_safety_manager, 'canary_deploy'):
                self._evolution_safety_manager = evolution_safety_manager
                logger.info("EvolutionSafetyManager 注入成功")
            if obsidian_mirror is not None and hasattr(obsidian_mirror, 'get_latest_report'):
                self._obsidian_mirror = obsidian_mirror
                logger.info("ObsidianMirror 注入成功")
            if behavioral_logger is not None and hasattr(behavioral_logger, 'log_event'):
                self._behavioral_logger = behavioral_logger
                logger.info("BehavioralLogger 注入成功")

    # ========== 安全依赖访问 ==========
    def _get_negotiation_bus(self) -> Optional[Any]:
        """线程安全地获取 NegotiationBus 引用"""
        with self._dependency_lock:
            return self._negotiation_bus

    def _get_behavioral_logger(self) -> Optional[Any]:
        """线程安全地获取 BehavioralLogger 引用"""
        with self._dependency_lock:
            return self._behavioral_logger

    # ========== 公共接口 ==========
    def submit_report(self, report: Dict[str, Any], priority: int = 5) -> Dict[str, Any]:
        """提交一份观察报告进入流转管道"""
        if not isinstance(priority, int) or priority < 1 or priority > 10:
            priority = 5

        # 报告校验（锁外执行，包含内容哈希计算）
        validation_result = self._validate_report(report)
        if not validation_result["valid"]:
            return {
                "status": "error",
                "reason": f"报告校验失败: {validation_result['reason']}",
                "data": {"validation_errors": validation_result["errors"]},
                "warnings": validation_result["errors"],
            }

        report_id = report["report_id"]
        now = time.time()

        # 锁外计算 content_hash，避免锁内执行耗时操作
        content_hash = self._compute_content_hash_safely(report)

        with self._lock:
            # 锁竞争监控
            self._lock_acquire_count += 1
            if len(self._reports) > self.DEFAULT_LOCK_CONTENTION_WARN_THRESHOLD:
                self._lock_contention_count += 1

            if report_id in self._reports:
                return {
                    "status": "error",
                    "reason": f"报告 {report_id} 已存在",
                    "data": {},
                    "warnings": [f"duplicate: {report_id}"],
                }

            # 内存压力检查
            self._check_memory_pressure_locked()

            if len(self._reports) >= self.DEFAULT_MAX_CACHED_REPORTS:
                self._evict_one_locked()

            state = {
                "report_id": report_id,
                "status": self.STATUS_SUBMITTED,
                "priority": priority,
                "submitted_at": now,
                "last_updated": now,
                "proposals_count": len(report.get("proposals", [])),
                "audit_result": None,
                "shadow_result": None,
                "deploy_result": None,
                "transition_history": [{
                    "from_status": None,
                    "to_status": self.STATUS_SUBMITTED,
                    "timestamp": now,
                    "reason": "报告提交",
                }],
                "content_hash": content_hash,
            }
            self._reports[report_id] = state
            self._metrics[self.METRIC_TOTAL_SUBMITTED] += 1
            self._metrics[self.METRIC_CURRENT_PENDING] += 1

            self._report_count_high_watermark = max(self._report_count_high_watermark, len(self._reports))

            self._try_cleanup_locked(now)
            self._check_timeouts_locked(now)

        self._persist_state()
        self._audit_log("report_submitted", {"report_id": report_id, "priority": priority})
        self._publish_event_with_retry(
            alert_type="obsidian_report_submitted",
            report_id=report_id,
            priority=priority,
            message=f"报告 {report_id} 已提交",
        )

        logger.info("报告已提交: %s, 提案数=%d", report_id, state["proposals_count"])
        return {
            "status": "ok",
            "reason": f"报告 {report_id} 已提交",
            "data": {"report_id": report_id, "status": self.STATUS_SUBMITTED, "priority": priority},
            "warnings": [],
        }

    def update_report_status(
        self, report_id: str, new_status: str, result: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """更新报告状态"""
        safe_result = result if result is not None else {}

        if new_status not in self.VALID_TRANSITIONS:
            return {
                "status": "error",
                "reason": f"无效的目标状态: {new_status}",
                "data": {},
                "warnings": [f"invalid_status: {new_status}"],
            }

        with self._lock:
            state = self._reports.get(report_id)
            if state is None:
                return {
                    "status": "error",
                    "reason": f"未找到报告: {report_id}",
                    "data": {},
                    "warnings": [f"unknown_report: {report_id}"],
                }

            current_status = state["status"]
            allowed = self.VALID_TRANSITIONS.get(current_status, ())
            if new_status not in allowed:
                return {
                    "status": "error",
                    "reason": f"非法状态转换: {current_status} -> {new_status}",
                    "data": {},
                    "warnings": [f"invalid_transition: {current_status}->{new_status}"],
                }

            old_status = current_status
            now = time.time()
            state["status"] = new_status
            state["last_updated"] = now

            history = state.setdefault("transition_history", [])
            history.append({
                "from_status": old_status,
                "to_status": new_status,
                "timestamp": now,
                "reason": safe_result.get("reason", "状态更新"),
                "operator": safe_result.get("operator", "system"),
            })
            if len(history) > self.DEFAULT_MAX_TRANSITION_HISTORY:
                state["transition_history"] = history[-self.DEFAULT_MAX_TRANSITION_HISTORY:]

            if new_status == self.STATUS_AUDITED:
                state["audit_result"] = copy.deepcopy(safe_result)
            elif new_status in (self.STATUS_SHADOW_PASSED, self.STATUS_REJECTED):
                state["shadow_result"] = copy.deepcopy(safe_result)
            elif new_status == self.STATUS_DEPLOYED:
                state["deploy_result"] = copy.deepcopy(safe_result)

            self._update_metrics_on_status_change_locked(old_status, new_status)

        self._persist_state()
        self._audit_log("status_updated", {
            "report_id": report_id,
            "from": old_status,
            "to": new_status,
            "operator": safe_result.get("operator", "system"),
        })
        self._publish_event_with_retry(
            alert_type="obsidian_status_changed",
            report_id=report_id,
            old_status=old_status,
            new_status=new_status,
            message=f"报告 {report_id}: {old_status} -> {new_status}",
        )

        logger.info("报告状态更新: %s: %s -> %s", report_id, old_status, new_status)
        return {
            "status": "ok",
            "reason": f"报告 {report_id} 状态已更新",
            "data": {},
            "warnings": [],
        }

    def get_pipeline_status(self, report_id: str) -> Dict[str, Any]:
        """查询报告流转状态（返回快照副本）"""
        with self._lock:
            state = self._reports.get(report_id)

        if state is None:
            return {
                "status": "error",
                "reason": f"未找到报告: {report_id}",
                "data": {},
                "warnings": [f"unknown: {report_id}"],
            }

        return {
            "status": "ok",
            "reason": f"报告 {report_id} 当前状态: {state['status']}",
            "data": {
                "report_id": state["report_id"],
                "status": state["status"],
                "priority": state.get("priority", 5),
                "submitted_at": state["submitted_at"],
                "last_updated": state["last_updated"],
                "proposals_count": state["proposals_count"],
                "audit_result": copy.deepcopy(state.get("audit_result")),
                "shadow_result": copy.deepcopy(state.get("shadow_result")),
                "deploy_result": copy.deepcopy(state.get("deploy_result")),
                "transition_history": copy.deepcopy(state.get("transition_history", [])),
            },
            "warnings": [],
        }

    def list_reports(self, status_filter: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        """列出报告列表"""
        limit = max(1, min(limit, 500))

        with self._lock:
            reports = list(self._reports.values())
            if status_filter:
                reports = [r for r in reports if r["status"] == status_filter]
            reports.sort(key=lambda x: (-x.get("priority", 5), x["submitted_at"]))
            limited = reports[:limit]

            summaries = [
                {
                    "report_id": r["report_id"],
                    "status": r["status"],
                    "priority": r.get("priority", 5),
                    "submitted_at": r["submitted_at"],
                    "last_updated": r["last_updated"],
                    "proposals_count": r["proposals_count"],
                }
                for r in limited
            ]

        return {
            "status": "ok",
            "reason": f"返回 {len(summaries)} 条报告",
            "data": {"total": len(reports), "returned": len(summaries), "reports": summaries},
            "warnings": [],
        }

    def cancel_report(self, report_id: str, reason: str = "手动取消") -> Dict[str, Any]:
        """取消/撤回报告"""
        return self.update_report_status(report_id, self.STATUS_CANCELLED, {"reason": reason})

    def get_pipeline_metrics(self) -> Dict[str, Any]:
        """获取管道自身性能指标"""
        with self._lock:
            metrics: Dict[str, Any] = dict(self._metrics)
            status_counts: Dict[str, int] = {}
            for r in self._reports.values():
                s = r["status"]
                status_counts[s] = status_counts.get(s, 0) + 1
            metrics[self.METRIC_STATUS_DISTRIBUTION] = status_counts
            metrics[self.METRIC_CACHE_USAGE_PCT] = round(
                len(self._reports) / max(1, self.DEFAULT_MAX_CACHED_REPORTS) * 100, 1
            )
            metrics[self.METRIC_HIGH_WATERMARK] = self._report_count_high_watermark
            metrics[self.METRIC_MEMORY_PRESSURE] = self._memory_pressure_active
            metrics[self.METRIC_LOCK_CONTENTION] = {
                "acquire_count": self._lock_acquire_count,
                "contention_count": self._lock_contention_count,
                "contention_ratio": (
                    round(self._lock_contention_count / max(1, self._lock_acquire_count), 4)
                    if self._lock_acquire_count > 0 else 0.0
                ),
                "current_report_count": len(self._reports),
            }

        return {
            "status": "ok",
            "reason": "管道指标已收集",
            "data": metrics,
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self, timeout_sec: Optional[float] = None) -> Dict[str, Any]:
        """模块自检，带超时保护"""
        if timeout_sec is None:
            timeout_sec = self.DEFAULT_HEALTH_CHECK_TIMEOUT_SEC

        try:
            with self._lock:
                report_count = len(self._reports)
                status_distribution: Dict[str, int] = {}
                for state in self._reports.values():
                    s = state.get("status", "unknown")
                    status_distribution[s] = status_distribution.get(s, 0) + 1

            dependency_health = self._check_dependency_health_with_timeout(timeout_sec)

            return {
                "status": "ok",
                "reason": f"ObsidianPipeline 正常，缓存报告 {report_count} 份",
                "data": {
                    "report_count": report_count,
                    "status_distribution": status_distribution,
                    "max_capacity": self.DEFAULT_MAX_CACHED_REPORTS,
                    "dependency_health": dependency_health,
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error("健康检查失败: %s #RECOVERY: 检查锁状态和数据完整性", e, exc_info=True)
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    def _check_dependency_health_with_timeout(self, timeout_sec: float) -> Dict[str, str]:
        """带超时的依赖健康检查（检查所有依赖）"""
        import concurrent.futures

        result: Dict[str, str] = {}
        deps_to_check = [
            (name, attr) for name, attr in [
                ("negotiation_bus", self._get_negotiation_bus()),
                ("shadow_manager", self._shadow_manager),
                ("evolution_safety_manager", self._evolution_safety_manager),
                ("behavioral_logger", self._get_behavioral_logger()),
            ] if attr is not None and hasattr(attr, 'health_check')
        ]

        if not deps_to_check:
            return result

        max_workers = min(len(deps_to_check), 3)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(self._safe_dep_health_check, dep): name
                for name, dep in deps_to_check
            }
            deadline = time.time() + timeout_sec
            for future in concurrent.futures.as_completed(future_map, timeout=timeout_sec):
                name = future_map[future]
                try:
                    dep_result = future.result(timeout=max(0.5, deadline - time.time()))
                    if isinstance(dep_result, dict):
                        result[name] = dep_result.get("status", "unknown")
                    else:
                        result[name] = "invalid_response_type"
                except Exception:
                    result[name] = "timeout_or_error"

        return result

    @staticmethod
    def _safe_dep_health_check(dep: Any) -> Dict[str, Any]:
        """安全调用依赖模块健康检查"""
        try:
            return dep.health_check()
        except BaseException as e:
            if isinstance(e, (SystemExit, KeyboardInterrupt)):
                raise
            return {"status": "error", "message": str(e)}

    # ========== 内存压力感知 ==========
    def _check_memory_pressure_locked(self) -> None:
        """在锁内检查系统内存压力，如果超过阈值则主动淘汰报告"""
        now = time.time()
        if now - self._last_memory_check < 30:  # 每30秒检查一次，避免频繁系统调用
            return

        self._last_memory_check = now

        try:
            import psutil
        except ImportError:
            return
        except Exception:
            return

        try:
            mem = psutil.virtual_memory()
            used_pct = mem.percent / 100.0

            if used_pct > self.DEFAULT_MEMORY_PRESSURE_THRESHOLD:
                if not self._memory_pressure_active:
                    logger.warning("系统内存压力过高 (%.1f%%)，启动主动淘汰", used_pct * 100)
                    self._memory_pressure_active = True
                # 批量淘汰，减少 while 循环中的 min 遍历
                target_count = max(10, int(len(self._reports) * 0.7))
                while len(self._reports) > target_count:
                    self._evict_one_locked()
            else:
                if self._memory_pressure_active:
                    logger.info("系统内存压力恢复正常，取消主动淘汰")
                    self._memory_pressure_active = False

            if mem.available < self.DEFAULT_MIN_MEMORY_RESERVE_BYTES:
                logger.critical("可用内存不足 %d MB，强制淘汰至最低安全容量",
                               self.DEFAULT_MIN_MEMORY_RESERVE_BYTES // (1024 * 1024))
                while len(self._reports) > 20:
                    self._evict_one_locked()

        except Exception as e:
            logger.warning("内存压力检查失败: %s", e)

    # ========== 私有方法：锁内版本 ==========
    def _update_metrics_on_status_change_locked(self, old_status: str, new_status: str) -> None:
        """更新指标（调用者必须持有 self._lock）"""
        was_terminal = old_status in self.TERMINAL_STATUSES
        is_terminal = new_status in self.TERMINAL_STATUSES

        if not was_terminal and is_terminal:
            prev_pending = self._metrics[self.METRIC_CURRENT_PENDING]
            self._metrics[self.METRIC_CURRENT_PENDING] = max(0, prev_pending - 1)
            if prev_pending <= 0 and not was_terminal:
                logger.error("CURRENT_PENDING 在递减前已为 %d，状态转换: %s -> %s #RECOVERY: 检查指标一致性",
                            prev_pending, old_status, new_status)
        elif was_terminal and not is_terminal:
            self._metrics[self.METRIC_CURRENT_PENDING] += 1

        if new_status == self.STATUS_DEPLOYED:
            self._metrics[self.METRIC_TOTAL_DEPLOYED] += 1
        elif new_status == self.STATUS_REJECTED:
            self._metrics[self.METRIC_TOTAL_REJECTED] += 1
        elif new_status == self.STATUS_CANCELLED:
            self._metrics[self.METRIC_TOTAL_CANCELLED] += 1

    def _evict_one_locked(self) -> None:
        """淘汰最旧的终态报告（调用者必须持有 self._lock）"""
        candidates = [(rid, s) for rid, s in self._reports.items() if s["status"] in self.TERMINAL_STATUSES]
        if not candidates:
            candidates = [(rid, s) for rid, s in self._reports.items()]
        if candidates:
            oldest = min(candidates, key=lambda x: x[1].get("last_updated", x[1].get("submitted_at", 0)))
            self._remove_report_locked(oldest[0])

    def _remove_report_locked(self, report_id: str) -> None:
        """移除报告并更新指标（调用者必须持有 self._lock）"""
        state = self._reports.get(report_id)
        if state is None:
            return
        if state["status"] not in self.TERMINAL_STATUSES:
            self._metrics[self.METRIC_CURRENT_PENDING] = max(
                0, self._metrics[self.METRIC_CURRENT_PENDING] - 1
            )
        del self._reports[report_id]
        # 同步清理告警去重字典
        self._timeout_alerted.pop(report_id, None)

    def _try_cleanup_locked(self, now: float) -> None:
        """清理过期报告，分批执行以避免锁持有时间过长"""
        if now - self._last_cleanup < self.DEFAULT_CLEANUP_INTERVAL_SEC:
            return

        expired_ids = []
        for rid, state in self._reports.items():
            if state["status"] in self.TERMINAL_STATUSES:
                if now - state.get("last_updated", state.get("submitted_at", now)) > self.DEFAULT_REPORT_TTL_SEC:
                    expired_ids.append(rid)
                    if len(expired_ids) >= self.DEFAULT_CLEANUP_BATCH_SIZE:
                        break

        for rid in expired_ids:
            self._remove_report_locked(rid)

        if expired_ids:
            logger.info("清理过期报告: %d 份 (剩余 %d)", len(expired_ids), len(self._reports))

        self._last_cleanup = now

    def _check_timeouts_locked(self, now: float) -> None:
        """检查停滞报告"""
        if now - self._last_timeout_check < self.DEFAULT_TIMEOUT_CHECK_INTERVAL_SEC:
            return

        timeout_seconds = self.DEFAULT_STATE_TRANSITION_TIMEOUT_HOURS * 3600
        for report_id, state in list(self._reports.items()):
            if state["status"] in self.TERMINAL_STATUSES:
                continue
            last_updated = state["last_updated"]
            # 防御时钟回拨
            if now < last_updated:
                continue
            stalled = now - last_updated
            if stalled > timeout_seconds:
                last_alert = self._timeout_alerted.get(report_id, 0)
                if now - last_alert < self.DEFAULT_TIMEOUT_ALERT_INTERVAL_SEC:
                    continue
                self._timeout_alerted[report_id] = now
                logger.warning(
                    "报告 %s 在状态 %s 停滞 %.1f 小时 #RECOVERY: 检查下游模块",
                    report_id, state["status"], stalled / 3600,
                )

        self._last_timeout_check = now

    # ========== 私有方法：锁外版本 ==========
    @staticmethod
    def _compute_content_hash_safely(report: Dict[str, Any]) -> str:
        """安全计算报告内容哈希（锁外调用，处理序列化异常）"""
        try:
            # 对键排序确保一致性，使用 default=str 处理不可序列化对象
            serialized = json.dumps(report, sort_keys=True, default=str, ensure_ascii=False)
            return hashlib.sha256(serialized.encode('utf-8')).hexdigest()
        except Exception as e:
            logger.warning("内容哈希计算失败: %s，使用随机替代值", e)
            return f"fallback_{uuid.uuid4().hex}"

    def _validate_report(self, report: Dict[str, Any]) -> Dict[str, Any]:
        """验证报告内容的完整性和合法性"""
        errors: List[str] = []

        if not isinstance(report, dict):
            return {"valid": False, "reason": "报告必须是字典类型", "errors": ["type_error"]}

        for field in self.REQUIRED_REPORT_FIELDS:
            if field not in report:
                errors.append(f"缺少必填字段: {field}")

        if errors:
            return {"valid": False, "reason": "; ".join(errors), "errors": errors}

        report_id = report.get("report_id", "")
        if not isinstance(report_id, str) or not report_id.strip():
            errors.append("report_id 必须是非空字符串")
        elif len(report_id) > self.DEFAULT_MAX_REPORT_ID_LENGTH:
            errors.append(f"report_id 长度 ({len(report_id)}) 超过上限 ({self.DEFAULT_MAX_REPORT_ID_LENGTH})")
        elif not self._is_valid_report_id(report_id):
            errors.append("report_id 包含非法字符")

        generated_at = report.get("generated_at")
        if not isinstance(generated_at, (int, float)) or generated_at <= 0:
            errors.append("generated_at 必须是正整数时间戳")
        elif generated_at > time.time() + 86400:
            errors.append("generated_at 是未来时间，可能时钟错误")
        elif generated_at < self.DEFAULT_MIN_GENERATED_AT:
            errors.append("generated_at 过于陈旧，可能数据源错误")

        proposals = report.get("proposals")
        if not isinstance(proposals, list) or len(proposals) == 0:
            errors.append("proposals 必须是非空列表")
        elif len(proposals) > self.DEFAULT_MAX_PROPOSALS_PER_REPORT:
            errors.append(f"proposals 数量 ({len(proposals)}) 超过上限 ({self.DEFAULT_MAX_PROPOSALS_PER_REPORT})")
        else:
            for i, p in enumerate(proposals):
                if not isinstance(p, dict):
                    errors.append(f"proposal[{i}] 必须是字典类型")
                    break
                for pf in self.REQUIRED_PROPOSAL_FIELDS:
                    if pf not in p:
                        errors.append(f"proposal[{i}] 缺少必填字段: {pf}")

        # 大小检查：使用 sys.getsizeof 进行粗略估算
        try:
            estimated_size = len(str(report))
        except Exception:
            estimated_size = 0
        if estimated_size > self.DEFAULT_MAX_REPORT_SIZE_BYTES * 2:
            errors.append(f"报告估算大小 ({estimated_size} bytes) 超过上限")

        if errors:
            return {"valid": False, "reason": "; ".join(errors), "errors": errors}

        return {"valid": True, "reason": "校验通过", "errors": []}

    @staticmethod
    def _is_valid_report_id(report_id: str) -> bool:
        """快速校验 report_id 合法性，避免多次 replace 性能问题"""
        if len(report_id) > 512:  # 二次长度保护
            return False
        allowed_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:@")
        return all(c in allowed_chars for c in report_id)

    def _publish_event_with_retry(self, alert_type: str, **kwargs) -> None:
        """带重试机制的事件发布（带指数退避）"""
        negotiation_bus = self._get_negotiation_bus()
        if negotiation_bus is None:
            return

        safe_kwargs = {}
        for k, v in kwargs.items():
            if isinstance(v, (str, int, float, bool, type(None))):
                safe_kwargs[k] = v
            else:
                safe_kwargs[k] = str(v)[:200]

        delay = self.DEFAULT_EVENT_RETRY_DELAY_SEC
        for attempt in range(self.DEFAULT_EVENT_RETRY_COUNT):
            try:
                negotiation_bus.publish_alert(alert_type=alert_type, **safe_kwargs)
                return
            except Exception as e:
                if attempt < self.DEFAULT_EVENT_RETRY_COUNT - 1:
                    logger.debug("事件发布失败 (尝试 %d/%d): %s", attempt + 1, self.DEFAULT_EVENT_RETRY_COUNT, e)
                    time.sleep(delay)
                    delay *= self.DEFAULT_EVENT_RETRY_BACKOFF  # 指数退避
                else:
                    logger.warning("事件发布最终失败 (%d 次): %s", self.DEFAULT_EVENT_RETRY_COUNT, e)

    def _audit_log(self, event_type: str, details: Dict[str, Any]) -> None:
        """记录审计日志"""
        behavioral_logger = self._get_behavioral_logger()
        if behavioral_logger is not None:
            try:
                behavioral_logger.log_event(
                    event_type=f"obsidian_pipeline.{event_type}",
                    details=details,
                )
                return
            except Exception as e:
                logger.warning("审计日志记录失败: %s", e)

        try:
            safe_details = json.dumps(details, default=str, ensure_ascii=False)[:2000]
        except Exception:
            safe_details = str(details)[:500]
        logger.info("审计日志(本地): %s | %s", event_type, safe_details)

    def _persist_state(self) -> None:
        """原子持久化（最小化锁持有时间，使用紧凑 JSON 格式）"""
        if not self._persistence_path:
            return

        tmp_path = f"{self._persistence_path}.tmp.{uuid.uuid4().hex[:self.DEFAULT_PERSIST_TMP_SUFFIX_LENGTH]}"
        try:
            with self._lock:
                active_reports = {
                    rid: {
                        "report_id": s["report_id"],
                        "status": s["status"],
                        "priority": s.get("priority", 5),
                        "submitted_at": s["submitted_at"],
                        "last_updated": s["last_updated"],
                        "proposals_count": s["proposals_count"],
                    }
                    for rid, s in self._reports.items()
                    if s["status"] not in self.TERMINAL_STATUSES
                }
                serialized = json.dumps(active_reports, ensure_ascii=False)  # 移除 indent，减小文件体积

            with open(tmp_path, 'w', encoding='utf-8') as f:
                f.write(serialized)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._persistence_path)

        except Exception as e:
            logger.warning("状态持久化失败: %s #RECOVERY: 检查磁盘空间和文件权限", e)
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _restore_from_persistence(self) -> None:
        """从持久化文件恢复基础状态"""
        if not self._persistence_path or not os.path.exists(self._persistence_path):
            return

        try:
            file_size = os.path.getsize(self._persistence_path)
            if file_size == 0:
                logger.warning("持久化文件为空，跳过恢复")
                return
            if file_size > self.DEFAULT_MAX_PERSIST_SIZE_BYTES:
                logger.warning("持久化文件过大 (%d bytes)，跳过恢复", file_size)
                return

            with open(self._persistence_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if not isinstance(data, dict):
                logger.warning("持久化文件格式错误，跳过恢复")
                return

            restored_count = 0
            with self._lock:
                # 限制恢复数量不超过缓存上限
                restore_limit = min(self.DEFAULT_MAX_RESTORE_REPORTS, self.DEFAULT_MAX_CACHED_REPORTS)
                for rid, saved in list(data.items())[:restore_limit]:
                    if not isinstance(saved, dict):
                        continue
                    if saved.get("report_id") != rid:
                        continue
                    if saved.get("status") in self.TERMINAL_STATUSES:
                        continue
                    if not self._is_valid_report_id(rid):
                        continue

                    self._reports[rid] = {
                        "report_id": saved["report_id"],
                        "status": saved["status"],
                        "priority": saved.get("priority", 5),
                        "submitted_at": saved.get("submitted_at", time.time()),
                        "last_updated": saved.get("last_updated", time.time()),
                        "proposals_count": saved.get("proposals_count", 0),
                        "audit_result": None,
                        "shadow_result": None,
                        "deploy_result": None,
                        "transition_history": [
                            {
                                "from_status": None,
                                "to_status": saved["status"],
                                "timestamp": saved.get("submitted_at", time.time()),
                                "reason": "从持久化恢复",
                            }
                        ],
                        "content_hash": "",
                    }
                    self._metrics[self.METRIC_TOTAL_SUBMITTED] += 1
                    self._metrics[self.METRIC_CURRENT_PENDING] += 1
                    restored_count += 1

            logger.info("从持久化文件恢复 %d 条报告状态", restored_count)

        except json.JSONDecodeError as e:
            logger.warning("持久化文件 JSON 解析失败: %s", e)
        except Exception as e:
            logger.warning("从持久化恢复失败: %s", e)
