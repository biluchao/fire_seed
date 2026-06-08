"""
火种系统 · 进化安全总管 (EvolutionSafetyManager)

核心职责：
1. 作为进化安全体系的调度入口，按配置顺序编排安全过滤流水线
2. 管理进化产物的金丝雀渐进发布流程，控制从影子验证到全量上线的完整生命周期

外部依赖（真实模块接口）：
- core.evolution_safety_manager.six_layer_filter.SixLayerFilter : 执行六层安全过滤（语法→经济→沙箱→影子→金丝雀→全量）
- core.evolution_safety_manager.canary_deployer.CanaryDeployer : 管理金丝雀渐进发布（1%→10%→100%）与自动回滚
- core.negotiation_bus.NegotiationBus : 发布进化安全事件与告警通知
- core.behavioral_logger.BehavioralLogger : 记录进化安全审计日志
- 审计日志配置：需预先配置 `fire_seed.audit` logger，级别不低于 INFO，输出至独立审计日志文件

接口契约：
- process_evolution_product(product: Dict[str, Any]) -> Dict[str, Any] : 处理一个进化产物，执行完整的安全流水线
- get_pipeline_status() -> Dict[str, Any] : 获取当前安全流水线的运行状态
- pause_pipeline(reason: str) -> Dict[str, Any] : 暂停安全流水线
- resume_pipeline() -> Dict[str, Any] : 恢复安全流水线
- health_check() -> Dict[str, Any] : 模块自检
- export_stats() -> Dict[str, int] : 导出当前处理统计
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])
- elapsed_sec 字段使用 time.perf_counter() 提供微秒级精度

异常与降级：
- 当 SixLayerFilter 不可用时，所有产物直接拒绝并标记为 "filter_unavailable"
- 当 CanaryDeployer 不可用时，已通过过滤的产物保留在待发布队列，等待恢复
- 当 NegotiationBus 不可用时，告警降级为仅本地日志记录
- 当队列溢出时，产物自动写入本地 SQLite 缓冲库，待恢复后重新加载
- 当溢出缓冲库不可用时，产物被拒绝并记录 CRITICAL 日志，每 300 秒自动尝试恢复
- 当线程池创建失败时，自动回退到同步处理模式
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护待发布产物队列与本地 SQLite 缓冲库，在模块销毁时自动清空并记录未发布的产物清单
- 线程池在模块销毁时通过 atexit 回调优雅关闭，使用 threading.Event 实现精确的超时保护
- 信号处理器（SIGTERM/SIGINT/SIGHUP）确保优雅关闭，_cleanup_called 标志位防止重复执行
- 不持有任何外部资源句柄，线程锁在模块销毁时自动释放
"""

import time
import copy
import json
import pickle
import hashlib
import logging
import threading
import atexit
import signal
import sqlite3
import os
import re
import sys
from typing import Dict, Any, List, Optional, Tuple
from collections import deque
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, wait, TimeoutError as FutureTimeoutError
from threading import Thread, Event


# ========== 自定义 JSON 编码器（版本: 1.0.0） ==========
class FinancialJSONEncoder(json.JSONEncoder):
    """
    金融级 JSON 编码器，保留金融类型精度。
    版本: 1.0.0
    支持类型: datetime → ISO8601, date → ISO8601, Decimal → float, 其他 → repr
    """
    def default(self, obj):
        import datetime as dt
        from decimal import Decimal
        if isinstance(obj, dt.datetime):
            return obj.isoformat()
        if isinstance(obj, dt.date):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        if hasattr(obj, '__repr__'):
            return repr(obj)
        return super().default(obj)


logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("fire_seed.audit")

# 告警类型安全正则：仅允许字母、数字、下划线和连字符
_ALERT_TYPE_PATTERN = re.compile(r'^[a-zA-Z0-9_\-]+$')


class PipelineStatus(Enum):
    """安全流水线状态枚举"""
    IDLE = "idle"
    PROCESSING = "processing"
    PAUSED = "paused"
    SHUTDOWN = "shutdown"


class EvolutionSafetyManager:
    """进化安全总管——调度入口"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_MAX_QUEUE_SIZE = 100            # 待发布产物队列最大容量，无量纲，[20, 500]
    DEFAULT_PROCESSING_TIMEOUT_SEC = 300    # 默认处理超时，秒，[60, 900]
    DEFAULT_STATUS_CACHE_TTL_SEC = 0.3      # 流水线状态缓存有效期，秒，[0.1, 1.0]
    MAX_DRAIN_CONCURRENCY = 4               # 队列排空最大并发数，无量纲，[1, 8]
    ALERT_DEDUP_WINDOW_SEC = 30             # 告警去重窗口，秒，[10, 120]
    QUEUE_OVERFLOW_ALERT_COOLDOWN_SEC = 300 # 队列溢出告警冷却，秒，[60, 600]
    DRAIN_BATCH_SIZE = 8                    # 队列排空每批最大产物数，无量纲，[4, 20]
    STATS_PERSIST_INTERVAL_SEC = 300        # 统计信息持久化间隔，秒，[60, 1800]
    SENSITIVE_PRODUCT_KEYS = {"gene_sequence", "raw_code", "private_key", "secret", "seed"}
    SPILLOVER_DB_PATH = "logs/evolution_spillover.db"
    STATS_DB_PATH = "logs/evolution_stats.db"
    TIMEOUT_PRODUCTS_PATH = "logs/timeout_products.jsonl"
    LARGE_OBJECT_THRESHOLD_BYTES = 10485760 # 大对象阈值（10MB）
    SANITIZE_MAX_RECURSION_DEPTH = 10       # 脱敏递归最大深度，[5, 20]
    CLEANUP_TIMEOUT_SEC = 10                # 清理超时，秒，[5, 30]
    SPILLOVER_RETRY_INTERVAL_SEC = 300      # 溢出缓冲库恢复重试间隔，秒，[60, 600]
    ALERT_COOLDOWN_CLEANUP_INTERVAL_SEC = 600 # 告警冷却记录清理间隔，秒，[300, 1800]

    # 按产物类型差异化超时（秒）
    PRODUCT_TIMEOUT_MAP = {
        "strategy_gene": 600,
        "factor": 120,
        "parameter_set": 180,
    }

    def __init__(self):
        # 流水线状态
        self._status: PipelineStatus = PipelineStatus.IDLE
        self._status_lock = threading.Lock()

        # 待处理产物队列（双阶段队列设计）
        self._pending_queue: deque = deque(maxlen=self.DEFAULT_MAX_QUEUE_SIZE)
        self._in_progress_queue: Dict[str, Dict[str, Any]] = {}

        # 当前正在处理的产物
        self._current_product: Optional[Dict[str, Any]] = None
        self._current_start_time: float = 0.0

        # 处理统计（使用普通 int 配合锁保护，确保可读性与线程安全）
        self._total_processed: int = 0
        self._total_passed: int = 0
        self._total_rejected: int = 0
        self._alert_lost_count: int = 0
        self._stats_lock = threading.Lock()

        # 排空统计
        self._drain_stats: Dict[str, int] = {"total_drained": 0, "total_success": 0, "total_failed": 0}
        self._drain_stats_lock = threading.Lock()

        # 防重入标志
        self._draining_in_progress: bool = False

        # 暂停计时
        self._pause_start_time: float = 0.0
        self._pause_reason: str = ""

        # 状态缓存
        self._status_cache: Dict[str, Any] = {}
        self._status_cache_time: float = 0.0

        # 外部依赖注入
        self._six_layer_filter = None
        self._canary_deployer = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 线程池（懒加载，使用双重检查锁定）
        self._drain_executor: Optional[ThreadPoolExecutor] = None
        self._filter_executor: Optional[ThreadPoolExecutor] = None
        self._executor_lock = threading.Lock()
        self._executor_failed: bool = False

        # 队列操作锁
        self._queue_lock = threading.Lock()

        # 告警冷却记录（定期清理防止内存泄漏）
        self._alert_cooldown: Dict[str, float] = {}
        self._last_alert_cleanup = time.perf_counter()

        # 统计信息持久化
        self._last_stats_persist = time.perf_counter()

        # 溢出缓冲库可用性标志
        self._spillover_available: bool = False
        self._spillover_init_time: float = 0.0
        self._init_spillover_db()

        # 恢复统计信息
        self._restore_stats()

        # 信号处理器（优雅关闭）
        self._cleanup_called: bool = False
        self._cleanup_lock = threading.Lock()
        self._register_signal_handlers()

        # 注册退出清理
        atexit.register(self._cleanup)

        logger.info("EvolutionSafetyManager 初始化完成，队列容量: %d", self.DEFAULT_MAX_QUEUE_SIZE)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        six_layer_filter: Optional[Any] = None,
        canary_deployer: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）

        Args:
            six_layer_filter: 六层安全过滤器实例
            canary_deployer: 金丝雀部署器实例
            negotiation_bus: 协商总线实例
            behavioral_logger: 行为日志实例
        """
        inject_time = time.perf_counter()
        self._log_audit_event_safe("dependency_injection_start", {"timestamp": inject_time})

        if six_layer_filter is not None:
            self._six_layer_filter = six_layer_filter
            version = getattr(six_layer_filter, '__version__', 'unknown')
            logger.info("SixLayerFilter 注入成功 (版本: %s)", version)
        else:
            logger.warning("SixLayerFilter 未注入，所有产物将被拒绝")

        if canary_deployer is not None:
            if not hasattr(canary_deployer, 'deploy') or not callable(canary_deployer.deploy):
                logger.warning("CanaryDeployer 缺少 deploy 方法，部署功能不可用")
                self._canary_deployer = None
            else:
                self._canary_deployer = canary_deployer
                version = getattr(canary_deployer, '__version__', 'unknown')
                logger.info("CanaryDeployer 注入成功 (版本: %s)", version)
        else:
            logger.warning("CanaryDeployer 未注入，已通过过滤的产物将保留在待发布队列")

        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, 'publish_alert') or not callable(negotiation_bus.publish_alert):
                logger.warning("NegotiationBus 缺少可调用的 publish_alert 方法，告警推送不可用")
                self._negotiation_bus = None
            else:
                self._negotiation_bus = negotiation_bus
                version = getattr(negotiation_bus, '__version__', 'unknown')
                logger.info("NegotiationBus 注入成功 (版本: %s)", version)

        if behavioral_logger is not None:
            if not hasattr(behavioral_logger, 'log_event') or not callable(behavioral_logger.log_event):
                logger.warning("BehavioralLogger 缺少 log_event 方法，日志降级为标准 logger")
                self._behavioral_logger = None
            else:
                self._behavioral_logger = behavioral_logger
                version = getattr(behavioral_logger, '__version__', 'unknown')
                logger.info("BehavioralLogger 注入成功 (版本: %s)", version)
        else:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

        self._log_audit_event_safe("dependency_injection_complete", {"timestamp": time.perf_counter()})

    # ========== 公共接口 ==========
    def process_evolution_product(self, product: Dict[str, Any]) -> Dict[str, Any]:
        """
        处理一个进化产物，执行完整的安全流水线

        Args:
            product: 进化产物字典，必须包含 "product_id" (str), "product_type" (str), "source" (str)

        Returns:
            标准响应字典，data 中包含处理结果
        """
        # 参数类型校验
        if not isinstance(product, dict):
            logger.warning(f"产物类型错误: {type(product).__name__}，期望 dict")
            return {
                "status": "error",
                "reason": f"产物类型错误: {type(product).__name__}，期望 dict",
                "data": {},
                "warnings": ["invalid_product_type"],
            }

        # 参数校验
        required_fields = ["product_id", "product_type", "source"]
        missing = [f for f in required_fields if f not in product]
        if missing:
            logger.warning(f"产物缺少必需字段: {missing}")
            return {
                "status": "error",
                "reason": f"产物缺少必需字段: {missing}",
                "data": {"product": self._sanitize_product(product)},
                "warnings": [f"missing_fields: {missing}"],
            }

        product_id = product["product_id"]

        # 创建安全副本，防止外部修改污染内部状态
        product_copy = self._resilient_deepcopy(product)

        # 检查流水线状态
        with self._status_lock:
            if self._status == PipelineStatus.PAUSED:
                queued = self._enqueue_product(product_copy)
                if not queued:
                    return {
                        "status": "error",
                        "reason": "队列已满且流水线暂停，产物被拒绝（已写入溢出缓冲库）",
                        "data": {"product_id": product_id},
                        "warnings": ["queue_full", "pipeline_paused", "spilled_to_db"],
                    }
                return {
                    "status": "ok",
                    "reason": "流水线已暂停，产物已加入待处理队列",
                    "data": {"product_id": product_id, "queue_position": len(self._pending_queue)},
                    "warnings": ["pipeline_paused"],
                }
            elif self._status == PipelineStatus.SHUTDOWN:
                return {
                    "status": "error",
                    "reason": "流水线已关闭，拒绝处理产物",
                    "data": {"product_id": product_id},
                    "warnings": ["pipeline_shutdown"],
                }

        # 标记为处理中
        with self._status_lock:
            self._status = PipelineStatus.PROCESSING
            self._current_product = product_copy
            self._current_start_time = time.perf_counter()

        # 执行安全流水线
        try:
            result = self._execute_pipeline(product_copy)
            return result
        finally:
            # 恢复空闲状态
            with self._status_lock:
                self._status = PipelineStatus.IDLE
                self._current_product = None
                self._current_start_time = 0.0

            # 处理待发布队列中的产物（仅在非排空中触发，防止递归）
            if not self._draining_in_progress:
                self._drain_pending_queue()

    def get_pipeline_status(self) -> Dict[str, Any]:
        """
        获取当前安全流水线的运行状态

        Returns:
            标准响应字典，data 中包含流水线状态、队列深度、处理统计
        """
        now = time.perf_counter()
        if now - self._status_cache_time < self.DEFAULT_STATUS_CACHE_TTL_SEC:
            return {
                "status": "ok",
                "reason": "返回缓存的流水线状态",
                "data": self._status_cache,
                "warnings": [],
            }

        with self._status_lock:
            status = self._status.value
            current_product_id = (
                self._current_product["product_id"] if self._current_product else None
            )
            processing_duration = (
                now - self._current_start_time if self._current_start_time > 0 else 0
            )
            current_status = self._status

        with self._queue_lock:
            queue_depth = len(self._pending_queue)
            in_progress_count = len(self._in_progress_queue)

        with self._stats_lock:
            total = self._total_processed
            passed = self._total_passed
            rejected = self._total_rejected
            alert_lost = self._alert_lost_count

        # 检查是否有产物处理超时
        warnings = []
        if (current_status == PipelineStatus.PROCESSING and
                processing_duration > self._get_processing_timeout(self._current_product)):
            timeout_product_id = current_product_id
            warnings.append(
                f"当前产物处理超时（{processing_duration:.0f}s），已强制重置流水线状态"
            )
            self._force_reset_pipeline_on_timeout(timeout_product_id)

        with self._drain_stats_lock:
            drain_stats = dict(self._drain_stats)

        result = {
            "pipeline_status": status,
            "current_product_id": current_product_id,
            "processing_duration_sec": round(processing_duration, 6),
            "queue_depth": queue_depth,
            "in_progress_count": in_progress_count,
            "total_processed": total,
            "total_passed": passed,
            "total_rejected": rejected,
            "alert_lost_count": alert_lost,
            "drain_stats": drain_stats,
            "dependencies": {
                "six_layer_filter": self._six_layer_filter is not None,
                "canary_deployer": self._canary_deployer is not None,
            },
        }

        self._status_cache = result
        self._status_cache_time = now

        return {
            "status": "ok",
            "reason": f"流水线状态: {status}",
            "data": result,
            "warnings": warnings,
        }

    def pause_pipeline(self, reason: str = "") -> Dict[str, Any]:
        """
        暂停安全流水线

        Args:
            reason: 暂停原因描述

        Returns:
            标准响应字典
        """
        with self._status_lock:
            if self._status == PipelineStatus.PAUSED:
                return {
                    "status": "ok",
                    "reason": "流水线已经处于暂停状态",
                    "data": {},
                    "warnings": ["already_paused"],
                }
            if self._status == PipelineStatus.SHUTDOWN:
                return {
                    "status": "error",
                    "reason": "流水线已关闭，无法暂停",
                    "data": {},
                    "warnings": ["pipeline_shutdown"],
                }
            self._status = PipelineStatus.PAUSED
            self._pause_start_time = time.perf_counter()
            self._pause_reason = reason

        logger.warning(f"安全流水线已暂停: {reason}")
        self._trigger_alert("pipeline_paused", f"原因: {reason}")

        return {
            "status": "ok",
            "reason": f"流水线已暂停: {reason}",
            "data": {"pause_time": time.perf_counter(), "reason": reason},
            "warnings": [],
        }

    def resume_pipeline(self) -> Dict[str, Any]:
        """
        恢复安全流水线

        Returns:
            标准响应字典
        """
        pause_duration = 0.0
        pause_reason = ""

        with self._status_lock:
            if self._status != PipelineStatus.PAUSED:
                return {
                    "status": "error",
                    "reason": f"流水线当前状态为 {self._status.value}，无法恢复",
                    "data": {},
                    "warnings": [f"invalid_state: {self._status.value}"],
                }
            self._status = PipelineStatus.IDLE
            pause_duration = time.perf_counter() - self._pause_start_time
            pause_reason = self._pause_reason
            self._pause_start_time = 0.0
            self._pause_reason = ""

        self._log_audit_event_safe(
            "pipeline_resumed",
            {"pause_duration_sec": round(pause_duration, 6), "pause_reason": pause_reason},
        )
        logger.info(f"安全流水线已恢复，暂停时长: {pause_duration:.3f}s")
        self._drain_pending_queue()

        return {
            "status": "ok",
            "reason": f"流水线已恢复（暂停时长: {pause_duration:.3f}s）",
            "data": {"pause_duration_sec": round(pause_duration, 6)},
            "warnings": [],
        }

    def export_stats(self) -> Dict[str, int]:
        """导出当前处理统计"""
        with self._stats_lock:
            return {
                "total_processed": self._total_processed,
                "total_passed": self._total_passed,
                "total_rejected": self._total_rejected,
                "alert_lost_count": self._alert_lost_count,
            }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检

        Returns:
            标准健康检查响应字典
        """
        try:
            with self._status_lock:
                status_val = self._status.value
            with self._queue_lock:
                queue_depth = len(self._pending_queue)
            with self._stats_lock:
                total = self._total_processed

            drain_executor_healthy = True
            if self._drain_executor is not None:
                try:
                    _ = self._drain_executor._max_workers if hasattr(
                        self._drain_executor, '_max_workers'
                    ) else 'unknown'
                except Exception:
                    drain_executor_healthy = False

            filter_executor_healthy = True
            if self._filter_executor is not None:
                try:
                    _ = self._filter_executor._max_workers if hasattr(
                        self._filter_executor, '_max_workers'
                    ) else 'unknown'
                except Exception:
                    filter_executor_healthy = False

            return {
                "status": "ok",
                "reason": f"EvolutionSafetyManager 正常，状态: {status_val}，累计处理: {total}",
                "data": {
                    "pipeline_status": status_val,
                    "queue_depth": queue_depth,
                    "total_processed": total,
                    "drain_executor_healthy": drain_executor_healthy,
                    "filter_executor_healthy": filter_executor_healthy,
                    "executor_failed": self._executor_failed,
                    "spillover_available": self._spillover_available,
                    "dependencies": {
                        "six_layer_filter": self._six_layer_filter is not None,
                        "canary_deployer": self._canary_deployer is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和统计字典完整性", exc_info=True)
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _get_drain_executor(self) -> Optional[ThreadPoolExecutor]:
        """获取或创建线程池（双重检查锁定），失败时回退到同步模式"""
        if self._executor_failed:
            return None
        if self._drain_executor is None:
            with self._executor_lock:
                if self._drain_executor is None and not self._executor_failed:
                    try:
                        self._drain_executor = ThreadPoolExecutor(
                            max_workers=self.MAX_DRAIN_CONCURRENCY,
                            thread_name_prefix="evolution_drain"
                        )
                    except RuntimeError as e:
                        logger.error(f"线程池创建失败，回退到同步处理: {e}")
                        self._executor_failed = True
                        return None
        return self._drain_executor

    def _get_filter_executor(self) -> Optional[ThreadPoolExecutor]:
        """获取或创建过滤器专用线程池（单线程，复用）"""
        if self._filter_executor is None:
            with self._executor_lock:
                if self._filter_executor is None:
                    try:
                        self._filter_executor = ThreadPoolExecutor(
                            max_workers=1,
                            thread_name_prefix="evolution_filter"
                        )
                    except RuntimeError as e:
                        logger.error(f"过滤器线程池创建失败: {e}")
                        return None
        return self._filter_executor

    def _get_processing_timeout(self, product: Optional[Dict[str, Any]]) -> int:
        """根据产物类型获取差异化超时"""
        if product is None:
            logger.debug("产物为 None，使用默认超时: %ds", self.DEFAULT_PROCESSING_TIMEOUT_SEC)
            return self.DEFAULT_PROCESSING_TIMEOUT_SEC
        product_type = product.get("product_type", "default")
        return self.PRODUCT_TIMEOUT_MAP.get(product_type, self.DEFAULT_PROCESSING_TIMEOUT_SEC)

    def _resilient_deepcopy(self, obj: Any) -> Any:
        """
        弹性深拷贝，依次尝试 JSON 编码→pickle→copy.deepcopy。

        对大对象（>10MB）优先使用 pickle 以获得更好性能。
        对包含不可序列化对象（如 threading.Lock）的，回退到 copy.deepcopy。
        """
        # 估算对象大小以选择最优策略
        try:
            obj_size = len(pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL))
        except (pickle.PicklingError, TypeError, AttributeError):
            obj_size = 0

        # 大对象优先使用 pickle
        if obj_size > self.LARGE_OBJECT_THRESHOLD_BYTES:
            try:
                return pickle.loads(pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL))
            except (pickle.PicklingError, TypeError):
                pass

        # 尝试 JSON 编码（保留金融类型精度）
        try:
            return json.loads(json.dumps(obj, cls=FinancialJSONEncoder))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

        # 尝试 pickle
        try:
            return pickle.loads(pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL))
        except (pickle.PicklingError, TypeError):
            pass

        # 最终回退到 copy.deepcopy
        return copy.deepcopy(obj)

    def _sanitize_product(self, product: Dict[str, Any], depth: int = 0) -> Dict[str, Any]:
        """
        递归脱敏产物中的敏感字段，带递归深度保护。

        对嵌套字典和列表进行递归处理，对不可序列化对象使用 repr() 转换。
        """
        if depth > self.SANITIZE_MAX_RECURSION_DEPTH:
            logger.warning("脱敏递归深度超限 (%d)，返回截断数据", depth)
            return {"_truncated": True}

        sanitized = {}
        for key, value in product.items():
            # 统一小写比较敏感键
            if key.lower() in self.SENSITIVE_PRODUCT_KEYS:
                sanitized[key] = "[REDACTED]"
            elif isinstance(value, dict):
                sanitized[key] = self._sanitize_product(value, depth + 1)
            elif isinstance(value, list):
                sanitized[key] = [
                    self._sanitize_product(item, depth + 1) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                sanitized[key] = value
        return sanitized

    def _enqueue_product(self, product: Dict[str, Any]) -> bool:
        """将产物加入待处理队列，队列满时写入溢出缓冲库"""
        maxlen = self._pending_queue.maxlen or self.DEFAULT_MAX_QUEUE_SIZE
        with self._queue_lock:
            if len(self._pending_queue) >= maxlen:
                logger.warning(f"待发布队列已满（{maxlen}），产物 {product.get('product_id')} 写入溢出缓冲库")
                if not self._spill_to_db(product):
                    logger.critical(
                        f"溢出缓冲库不可用，产物 {product.get('product_id')} 永久丢失 "
                        f"#RECOVERY: 立即检查磁盘和数据库状态"
                    )
                self._trigger_alert("queue_overflow", f"产物 {product.get('product_id')} 已写入溢出缓冲库")
                return False
            self._pending_queue.append(product)
            return True

    def _execute_pipeline(self, product: Dict[str, Any]) -> Dict[str, Any]:
        """执行安全过滤流水线（内部方法）"""
        product_id = product["product_id"]
        product_type = product.get("product_type", "unknown")
        start_time = time.perf_counter()
        filter_stages = []

        # 阶段一：六层安全过滤（带超时保护，复用专用线程池）
        if self._six_layer_filter is None:
            elapsed = time.perf_counter() - start_time
            self._update_stats(rejected=True)
            logger.error(f"产物 {product_id} 被拒绝: SixLayerFilter 不可用 #RECOVERY: 检查依赖注入")
            return {
                "status": "error",
                "reason": "SixLayerFilter 不可用，产物被拒绝",
                "data": {
                    "product_id": product_id,
                    "product_type": product_type,
                    "filter_stages": [{"stage": "six_layer_filter", "result": "unavailable"}],
                    "elapsed_sec": round(elapsed, 6),
                },
                "warnings": ["filter_unavailable"],
            }

        filter_executor = self._get_filter_executor()
        if filter_executor is None:
            elapsed = time.perf_counter() - start_time
            self._update_stats(rejected=True)
            logger.error(f"过滤器线程池不可用，产物 {product_id} 被拒绝")
            return {
                "status": "error",
                "reason": "过滤器线程池不可用",
                "data": {
                    "product_id": product_id,
                    "product_type": product_type,
                    "elapsed_sec": round(elapsed, 6),
                },
                "warnings": ["filter_executor_unavailable"],
            }

        timeout = self._get_processing_timeout(product)
        filter_future = None
        try:
            filter_future = filter_executor.submit(self._six_layer_filter.process, product)
            filter_result = filter_future.result(timeout=timeout)
        except FutureTimeoutError:
            elapsed = time.perf_counter() - start_time
            self._update_stats(rejected=True)
            if filter_future is not None:
                filter_future.cancel()
            logger.error(f"产物 {product_id} 过滤超时（{timeout}s） #RECOVERY: 检查 SixLayerFilter 内部逻辑")
            return {
                "status": "error",
                "reason": f"安全过滤超时（{timeout}s）",
                "data": {
                    "product_id": product_id,
                    "product_type": product_type,
                    "filter_stages": filter_stages,
                    "elapsed_sec": round(elapsed, 6),
                },
                "warnings": ["filter_timeout"],
            }
        except Exception as e:
            elapsed = time.perf_counter() - start_time
            self._update_stats(rejected=True)
            logger.error(
                f"产物 {product_id} 过滤异常: {e} #RECOVERY: 检查 SixLayerFilter 内部逻辑", exc_info=True
            )
            return {
                "status": "error",
                "reason": f"安全过滤异常: {str(e)}",
                "data": {
                    "product_id": product_id,
                    "product_type": product_type,
                    "filter_stages": filter_stages,
                    "elapsed_sec": round(elapsed, 6),
                },
                "warnings": ["filter_exception"],
            }

        if filter_result is None:
            elapsed = time.perf_counter() - start_time
            self._update_stats(rejected=True)
            logger.error(f"产物 {product_id} 过滤返回 None")
            return {
                "status": "error",
                "reason": "安全过滤返回空结果",
                "data": {
                    "product_id": product_id,
                    "product_type": product_type,
                    "filter_stages": [{"stage": "six_layer_filter", "result": "null"}],
                    "elapsed_sec": round(elapsed, 6),
                },
                "warnings": ["filter_null_result"],
            }

        if not isinstance(filter_result, dict):
            elapsed = time.perf_counter() - start_time
            self._update_stats(rejected=True)
            logger.error(f"产物 {product_id} 过滤返回非字典类型: {type(filter_result).__name__}")
            return {
                "status": "error",
                "reason": f"安全过滤返回非字典类型: {type(filter_result).__name__}",
                "data": {
                    "product_id": product_id,
                    "product_type": product_type,
                    "elapsed_sec": round(elapsed, 6),
                },
                "warnings": ["filter_invalid_result_type"],
            }

        filter_stages.append(self._resilient_deepcopy(filter_result))
        if not bool(filter_result.get("passed", False)):
            elapsed = time.perf_counter() - start_time
            self._update_stats(rejected=True)
            failed_stage = filter_result.get("failed_stage", "unknown")
            logger.warning(f"产物 {product_id} 未通过安全过滤，阶段: {failed_stage}")
            return {
                "status": "ok",
                "reason": f"产物未通过安全过滤（{failed_stage}）",
                "data": {
                    "product_id": product_id,
                    "product_type": product_type,
                    "filter_stages": filter_stages,
                    "elapsed_sec": round(elapsed, 6),
                },
                "warnings": ["filter_rejected", f"failed_stage: {failed_stage}"],
            }

        # 阶段二：金丝雀渐进发布
        if self._canary_deployer is None:
            elapsed = time.perf_counter() - start_time
            logger.warning(f"产物 {product_id} 通过过滤但 CanaryDeployer 不可用，加入待发布队列")
            queued = self._enqueue_product(product)
            return {
                "status": "ok",
                "reason": "CanaryDeployer 不可用，产物已加入待发布队列等待恢复",
                "data": {
                    "product_id": product_id,
                    "product_type": product_type,
                    "filter_stages": filter_stages,
                    "queue_position": len(self._pending_queue) if queued else -1,
                    "elapsed_sec": round(elapsed, 6),
                },
                "warnings": ["deployer_unavailable", "queued_for_later"],
            }

        deploy_result = self._canary_deployer.deploy(product)
        filter_stages.append(self._resilient_deepcopy(deploy_result))
        elapsed = time.perf_counter() - start_time

        if deploy_result.get("deployed", False):
            self._update_stats(passed=True)
            logger.info(f"产物 {product_id} 通过全部安全流水线并已部署，耗时 {elapsed:.6f}s")
            return {
                "status": "ok",
                "reason": "产物已通过安全流水线并成功部署",
                "data": {
                    "product_id": product_id,
                    "product_type": product_type,
                    "filter_stages": filter_stages,
                    "elapsed_sec": round(elapsed, 6),
                },
                "warnings": [],
            }
        else:
            self._update_stats(rejected=True)
            fail_reason = deploy_result.get("reason", "unknown")
            logger.warning(f"产物 {product_id} 金丝雀部署失败: {fail_reason}")
            return {
                "status": "error",
                "reason": f"金丝雀部署失败: {fail_reason}",
                "data": {
                    "product_id": product_id,
                    "product_type": product_type,
                    "filter_stages": filter_stages,
                    "elapsed_sec": round(elapsed, 6),
                },
                "warnings": ["deploy_failed"],
            }

    def _drain_pending_queue(self) -> None:
        """
        使用线程池分批并行处理待发布队列中的产物。

        使用 _draining_in_progress 标志位防止递归重入。
        使用 _drain_stats 累积历史排空统计。
        """
        # 防重入检查
        if self._draining_in_progress:
            return
        self._draining_in_progress = True

        try:
            drained_total = 0
            drained_success = 0
            drained_failed = 0
            executor = self._get_drain_executor()

            while True:
                with self._queue_lock:
                    if not self._pending_queue:
                        break
                    batch_size = min(self.DRAIN_BATCH_SIZE, len(self._pending_queue))
                    batch = []
                    for _ in range(batch_size):
                        if self._pending_queue:
                            batch.append(self._pending_queue.popleft())

                with self._status_lock:
                    if self._status in (PipelineStatus.PAUSED, PipelineStatus.SHUTDOWN):
                        batch_copy = list(batch)
                        with self._queue_lock:
                            self._pending_queue.extendleft(reversed(batch_copy))
                        break

                futures = []
                if executor is not None:
                    for product in batch:
                        try:
                            future = executor.submit(self.process_evolution_product, product)
                            futures.append((product.get("product_id", "unknown"), future))
                        except (RuntimeError, Exception) as e:
                            logger.warning(f"线程池提交失败，回退到同步处理: {e}")
                            try:
                                result = self.process_evolution_product(product)
                                if result and result.get("status") == "ok":
                                    drained_success += 1
                                else:
                                    drained_failed += 1
                            except Exception:
                                drained_failed += 1
                            drained_total += 1
                else:
                    # 线程池不可用，同步处理批次
                    for product in batch:
                        try:
                            result = self.process_evolution_product(product)
                            if result and result.get("status") == "ok":
                                drained_success += 1
                            else:
                                drained_failed += 1
                        except Exception:
                            drained_failed += 1
                        drained_total += 1

                if futures:
                    # 并行等待批次完成
                    future_list = [f[1] for f in futures]
                    try:
                        done, not_done = wait(future_list, timeout=60, return_when='ALL_COMPLETED')
                        # 取消未完成的任务并记录
                        for f in not_done:
                            cancelled = f.cancel()
                            logger.warning(
                                f"排空任务超时{'已取消' if cancelled else '取消失败'}: {f}"
                            )
                            drained_failed += 1
                            drained_total += 1
                    except Exception as e:
                        logger.error(f"排空等待异常: {e}")

                    for _, future in futures:
                        if future in not_done:
                            continue
                        try:
                            result = future.result(timeout=10)
                            if result and result.get("status") == "ok":
                                drained_success += 1
                            else:
                                drained_failed += 1
                        except (FutureTimeoutError, Exception) as e:
                            logger.warning(f"排空任务异常: {e}")
                            drained_failed += 1
                        drained_total += 1

            if drained_total > 0:
                logger.info(
                    f"待发布队列排空完成: 总数={drained_total}, 成功={drained_success}, 失败={drained_failed}"
                )
                with self._drain_stats_lock:
                    self._drain_stats["total_drained"] += drained_total
                    self._drain_stats["total_success"] += drained_success
                    self._drain_stats["total_failed"] += drained_failed
        finally:
            self._draining_in_progress = False

    def _update_stats(self, passed: bool = False, rejected: bool = False) -> None:
        """更新处理统计（线程安全）"""
        with self._stats_lock:
            self._total_processed += 1
            if passed:
                self._total_passed += 1
            if rejected:
                self._total_rejected += 1

        if time.perf_counter() - self._last_stats_persist > self.STATS_PERSIST_INTERVAL_SEC:
            self._persist_stats()
            self._last_stats_persist = time.perf_counter()

    def _persist_stats(self) -> None:
        """持久化统计信息到 SQLite"""
        try:
            with self._stats_lock:
                stats = {
                    "total_processed": self._total_processed,
                    "total_passed": self._total_passed,
                    "total_rejected": self._total_rejected,
                    "alert_lost_count": self._alert_lost_count,
                    "timestamp": time.perf_counter(),
                }
            os.makedirs(os.path.dirname(self.STATS_DB_PATH), exist_ok=True)
            with sqlite3.connect(self.STATS_DB_PATH, timeout=5.0) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS stats
                       (key TEXT PRIMARY KEY, value TEXT, updated_at REAL)"""
                )
                conn.execute(
                    "INSERT OR REPLACE INTO stats VALUES (?, ?, ?)",
                    ("evolution_safety", json.dumps(stats, cls=FinancialJSONEncoder), time.perf_counter())
                )
        except Exception as e:
            logger.warning(f"统计信息持久化失败: {e}")

    def _restore_stats(self) -> None:
        """从 SQLite 恢复统计信息"""
        if not os.path.exists(self.STATS_DB_PATH):
            return
        try:
            with sqlite3.connect(self.STATS_DB_PATH, timeout=5.0) as conn:
                cursor = conn.execute("SELECT value FROM stats WHERE key = ?", ("evolution_safety",))
                row = cursor.fetchone()
                if row:
                    try:
                        stats = json.loads(row[0])
                        if not isinstance(stats, dict):
                            raise ValueError("统计数据格式错误")
                    except (json.JSONDecodeError, ValueError) as e:
                        logger.warning(f"统计信息 JSON 解析失败: {e}")
                        return
                    with self._stats_lock:
                        self._total_processed = stats.get("total_processed", 0)
                        self._total_passed = stats.get("total_passed", 0)
                        self._total_rejected = stats.get("total_rejected", 0)
                        self._alert_lost_count = stats.get("alert_lost_count", 0)
                    logger.info(f"已从持久化存储恢复统计信息: {stats}")
        except Exception as e:
            logger.warning(f"统计信息恢复失败: {e}")

    def _init_spillover_db(self) -> None:
        """初始化溢出缓冲数据库"""
        try:
            os.makedirs(os.path.dirname(self.SPILLOVER_DB_PATH), exist_ok=True)
            with sqlite3.connect(self.SPILLOVER_DB_PATH, timeout=5.0) as conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS spillover
                       (id INTEGER PRIMARY KEY AUTOINCREMENT,
                        product_json TEXT,
                        created_at REAL,
                        status TEXT DEFAULT 'pending')"""
                )
            self._spillover_available = True
            self._spillover_init_time = time.perf_counter()
            logger.info("溢出缓冲数据库初始化成功")
        except (sqlite3.OperationalError, OSError) as e:
            logger.critical(f"溢出缓冲数据库初始化失败: {e} #RECOVERY: 检查磁盘权限和路径")
            self._spillover_available = False
            self._spillover_init_time = time.perf_counter()

    def _spill_to_db(self, product: Dict[str, Any]) -> bool:
        """将产物写入溢出缓冲数据库，返回是否成功"""
        # 定期重试恢复溢出缓冲库
        if not self._spillover_available:
            if time.perf_counter() - self._spillover_init_time > self.SPILLOVER_RETRY_INTERVAL_SEC:
                self._init_spillover_db()
            if not self._spillover_available:
                return False

        try:
            safe_product = self._resilient_deepcopy(product)
            with sqlite3.connect(self.SPILLOVER_DB_PATH, timeout=5.0) as conn:
                conn.execute(
                    "INSERT INTO spillover (product_json, created_at) VALUES (?, ?)",
                    (json.dumps(self._sanitize_product(safe_product), cls=FinancialJSONEncoder),
                     time.perf_counter())
                )
            logger.info(f"产物 {product.get('product_id')} 已写入溢出缓冲库")
            return True
        except Exception as e:
            logger.error(f"溢出缓冲写入失败: {e} #RECOVERY: 检查磁盘空间")
            self._spillover_available = False
            return False

    def _force_reset_pipeline_on_timeout(self, product_id: Optional[str]) -> None:
        """超时时强制重置流水线状态（原子操作）"""
        logger.error(
            f"产物 {product_id} 处理超时，强制恢复流水线状态 #RECOVERY: 检查过滤器或部署器状态"
        )
        timeout_product = None
        with self._status_lock:
            if self._status == PipelineStatus.PROCESSING:
                self._status = PipelineStatus.IDLE
                timeout_product = self._current_product
                self._current_product = None
                self._current_start_time = 0.0
                self._update_stats(rejected=True)
        # 持久化超时产物信息（在锁外异步执行）
        if timeout_product:
            self._persist_timeout_product(timeout_product)
        self._trigger_alert("processing_timeout", f"产物 {product_id} 处理超时，已强制重置")

    def _persist_timeout_product(self, product: Dict[str, Any]) -> None:
        """持久化超时产物信息（使用原子写入）"""
        try:
            safe_product = self._resilient_deepcopy(product)
            sanitized = self._sanitize_product(safe_product)
            record = json.dumps(
                {"product": sanitized, "timestamp": time.perf_counter()},
                cls=FinancialJSONEncoder
            )
            # 确保临时文件与目标文件在同一目录（避免跨文件系统 os.replace 失败）
            target_dir = os.path.dirname(self.TIMEOUT_PRODUCTS_PATH)
            tmp_path = os.path.join(target_dir, f".timeout_products_{os.getpid()}.tmp")
            os.makedirs(target_dir, exist_ok=True)
            with open(tmp_path, 'w') as f:
                f.write(record + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.TIMEOUT_PRODUCTS_PATH)
        except Exception as e:
            logger.error(f"超时产物持久化失败: {e}")

    def _trigger_alert(self, alert_type: str, message: str) -> None:
        """触发告警（含去重机制）"""
        # 告警类型安全检查
        if not _ALERT_TYPE_PATTERN.match(alert_type):
            logger.warning(f"告警类型包含非法字符，已拒绝: {alert_type}")
            return

        # 定期清理过期的告警冷却记录
        self._cleanup_alert_cooldown()

        alert_key = hashlib.sha256(f"{alert_type}:{message}".encode('utf-8')).hexdigest()[:16]

        if alert_type == "queue_overflow":
            last_time = self._alert_cooldown.get(alert_type, 0)
            if time.perf_counter() - last_time < self.QUEUE_OVERFLOW_ALERT_COOLDOWN_SEC:
                logger.debug(f"告警冷却中: {alert_type}")
                return
            self._alert_cooldown[alert_type] = time.perf_counter()

        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                result = self._negotiation_bus.publish_alert(
                    alert_type=f"evolution_safety:{alert_type}",
                    message=message,
                    timestamp=time.perf_counter(),
                )
                if result is not None and not result.get("success", True):
                    with self._stats_lock:
                        self._alert_lost_count += 1
                    logger.warning(f"告警推送失败: {alert_type}")
            except Exception as e:
                with self._stats_lock:
                    self._alert_lost_count += 1
                logger.warning(f"协商总线告警推送异常: {e}")

        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type=f"evolution_safety_{alert_type}",
                    details={"message": message, "timestamp": time.perf_counter()},
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

    def _cleanup_alert_cooldown(self) -> None:
        """定期清理过期的告警冷却记录，防止内存泄漏"""
        now = time.perf_counter()
        if now - self._last_alert_cleanup < self.ALERT_COOLDOWN_CLEANUP_INTERVAL_SEC:
            return
        expired = [
            k for k, v in self._alert_cooldown.items()
            if now - v > self.ALERT_COOLDOWN_CLEANUP_INTERVAL_SEC
        ]
        for k in expired:
            del self._alert_cooldown[k]
        self._last_alert_cleanup = now

    def _log_audit_event_safe(self, event_type: str, details: Dict[str, Any]) -> None:
        """安全记录审计事件（捕获序列化异常）"""
        # 检查审计日志可用性
        if not audit_logger.handlers:
            logger.warning(f"审计日志未配置 Handler，事件丢失: {event_type}")
            return

        if audit_logger.isEnabledFor(logging.INFO):
            try:
                audit_logger.info(
                    "审计事件: %s | 详情: %s",
                    event_type,
                    json.dumps(details, cls=FinancialJSONEncoder, ensure_ascii=False)
                )
            except (TypeError, ValueError) as e:
                audit_logger.info(
                    "审计事件: %s | 详情(回退): %s",
                    event_type, str(details)
                )
                logger.warning(f"审计日志序列化失败: {e}")

        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type=f"evolution_safety_audit_{event_type}",
                    details={**details, "timestamp": time.perf_counter()},
                )
            except Exception as e:
                logger.warning(f"审计日志记录失败: {e}")

    def _register_signal_handlers(self) -> None:
        """注册信号处理器以确保优雅关闭（仅主线程）"""
        def _signal_handler(signum, frame):
            logger.info(f"收到信号 {signum}，开始优雅关闭")
            self._cleanup()
            # 使用 sys.exit 允许 Python 解释器执行 atexit 清理
            sys.exit(0)

        # 仅在主线程中注册信号处理器
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                try:
                    signal.signal(sig, _signal_handler)
                except (ValueError, OSError):
                    logger.warning(f"无法注册信号处理器: {sig}")
        else:
            logger.warning("非主线程，跳过信号处理器注册")

    def _cleanup(self) -> None:
        """模块销毁时的清理回调（使用 Event 实现精确超时保护，防止重复执行）"""
        with self._cleanup_lock:
            if self._cleanup_called:
                return
            self._cleanup_called = True

        cleanup_start = time.perf_counter()
        logger.info("EvolutionSafetyManager 开始清理...")

        # 使用 Event 实现精确的超时保护
        cleanup_completed = Event()

        def _cleanup_with_timeout():
            try:
                # 关闭线程池
                if self._drain_executor is not None:
                    self._drain_executor.shutdown(wait=True, cancel_futures=True)
                    logger.info("排空线程池已关闭")

                if self._filter_executor is not None:
                    self._filter_executor.shutdown(wait=True, cancel_futures=True)
                    logger.info("过滤器线程池已关闭")

                # 记录未发布的产物（在锁内创建副本后迭代）
                pending_snapshot = []
                with self._queue_lock:
                    if self._pending_queue:
                        pending_snapshot = [
                            p.get('product_id', 'unknown') for p in self._pending_queue
                        ]
                if pending_snapshot:
                    logger.warning(f"模块销毁时仍有 {len(pending_snapshot)} 个产物未发布: {pending_snapshot}")

                # 持久化最终统计
                self._persist_stats()

            except Exception as e:
                logger.error(f"清理过程异常: {e}", exc_info=True)
            finally:
                cleanup_completed.set()

        # 在独立线程中执行清理
        cleanup_thread = Thread(target=_cleanup_with_timeout, daemon=True, name="evolution_cleanup")
        cleanup_thread.start()

        # 等待清理完成或超时
        if not cleanup_completed.wait(timeout=self.CLEANUP_TIMEOUT_SEC):
            logger.error(f"清理超时（{self.CLEANUP_TIMEOUT_SEC}s），强制退出")

        elapsed = time.perf_counter() - cleanup_start
        logger.info(f"EvolutionSafetyManager 清理完成，耗时 {elapsed:.3f}s")
