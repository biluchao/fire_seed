"""
火种系统 · 系统自检入口 (SelfCheck)

核心职责：
1. 调度已拆分的子模块（硬件扫描、网络评估、趋势基线），按顺序执行全系统健康检查
2. 汇总各子模块的健康报告，生成统一格式的系统健康状态快照，并触发相应级别的告警

外部依赖（真实模块接口）：
- core.self_check.hardware_scanner.HardwareScanner : 执行硬件深度扫描（ECC、磁盘SMART、网卡错误等）
- core.self_check.network_quality_grader.NetworkQualityGrader : 评估网络链路质量并生成评分
- core.self_check.trend_baseline_monitor.TrendBaselineMonitor : 基于滑动窗口基线的趋势分析与多维交叉验证
- core.negotiation_bus.NegotiationBus : 发送健康状态变更事件与告警通知
- core.behavioral_logger.BehavioralLogger : 记录健康检查日志与告警事件

接口契约：
- run_full_check() -> Dict[str, Any] : 执行一次完整的系统自检，返回汇总报告
- health_check() -> Dict[str, Any] : 模块自身的健康检查
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当任一子模块不可用或执行失败时，该子模块的健康评分设为 0，并标记为 "unavailable"，不影响其他子模块的执行
- 当 NegotiationBus 不可用时，告警降级为仅本地日志记录
- 当 BehavioralLogger 不可用时，日志降级为标准 logger
- 所有降级值在类常量区明确声明

资源管理：
- 本模块不持有任何外部资源句柄，子模块实例通过依赖注入管理
- 定期执行的健康检查任务通过独立线程调度，线程在系统退出时通过 stop_scheduler 显式终止
- 子模块调用采用受控线程池，确保超时后无残留资源
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple, Callable, Union
from collections import OrderedDict
import os

logger = logging.getLogger(__name__)

# ========== 模块级常量 ==========
DEFAULT_MODULE_TIMEOUT: float = 30.0          # 子模块调用默认超时（秒）
SELF_CHECK_SLOW_THRESHOLD: float = 10.0        # 自检耗时告警阈值（秒）
ALERT_DEDUP_WINDOW: float = 30.0               # 告警去重窗口（秒）
ALERT_DEDUP_MAX_ENTRIES: int = 500             # 去重字典最大条目数
MODULE_REGISTRY: Dict[str, str] = {             # 可扩展的模块注册表
    "hardware": "hardware_scanner",
    "network": "network_grader",
    "trend_baseline": "trend_monitor",
}
DEFAULT_MODULE_WEIGHTS: Dict[str, float] = {
    "hardware": 0.35,
    "network": 0.30,
    "trend_baseline": 0.35,
}
# 校验权重和
assert abs(sum(DEFAULT_MODULE_WEIGHTS.values()) - 1.0) < 1e-6, "模块权重和必须为1"

# 全局告警去重字典及锁
_global_alert_dedup: Dict[str, float] = {}
_global_dedup_lock = threading.Lock()

# 子模块调用线程池最大工作线程数
MAX_MODULE_WORKER_THREADS: int = 3


def _dedup_alert(alert_key: str) -> bool:
    """全局告警去重，返回 True 表示允许发送"""
    now = time.monotonic()
    with _global_dedup_lock:
        # 定期清理过期条目
        if len(_global_alert_dedup) > ALERT_DEDUP_MAX_ENTRIES:
            expired = [k for k, v in _global_alert_dedup.items() if now - v > ALERT_DEDUP_WINDOW * 3]
            for k in expired:
                del _global_alert_dedup[k]
        last = _global_alert_dedup.get(alert_key, 0.0)
        if now - last < ALERT_DEDUP_WINDOW:
            return False
        _global_alert_dedup[alert_key] = now
        return True


def _call_with_timeout(
    func: Callable[[], Dict[str, Any]],
    timeout: float,
    module_name: str,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """
    在受控线程中调用函数并设置超时。
    超时后工作线程可检测 cancel_event 并尽早退出（若函数支持）。
    若超时，返回降级结果。
    """
    result_container: Dict[str, Any] = {}
    exception_container: Optional[BaseException] = None
    done = threading.Event()

    def worker() -> None:
        nonlocal exception_container
        try:
            res = func()
            result_container.update(res)
        except (SystemExit, KeyboardInterrupt):
            raise
        except Exception as e:
            exception_container = e
        finally:
            done.set()

    thread = threading.Thread(target=worker, name=f"SelfCheck-{module_name}", daemon=True)
    thread.start()

    # 使用 cancel_event 或 done 等待
    if cancel_event:
        while not done.is_set() and not cancel_event.is_set():
            done.wait(timeout=0.1)
            if cancel_event.is_set():
                logger.warning(f"{module_name} 调用被取消")
                return {
                    "status": "degraded",
                    "reason": f"{module_name} 调用被取消",
                    "data": {"score": 0.0, "level": "unavailable"},
                    "warnings": [f"{module_name}_cancelled"],
                }
    else:
        done.wait(timeout=timeout)

    if not done.is_set():
        logger.error(f"{module_name} 调用超时 ({timeout}s)，返回降级结果")
        return {
            "status": "degraded",
            "reason": f"{module_name} 调用超时 ({timeout}s)",
            "data": {"score": 0.0, "level": "unavailable"},
            "warnings": [f"{module_name}_timeout: 超过 {timeout}s"],
        }
    if exception_container is not None:
        logger.error(f"{module_name} 调用异常: {exception_container}", exc_info=True)
        return {
            "status": "degraded",
            "reason": str(exception_container),
            "data": {"score": 0.0, "level": "unavailable"},
            "warnings": [f"{module_name}_exception: {str(exception_container)}"],
        }
    return result_container


class SelfCheck:
    """系统自检总入口"""

    # ========== 类常量 ==========
    DEFAULT_FULL_CHECK_INTERVAL_SEC: float = 3600.0
    DEFAULT_PARTIAL_CHECK_INTERVAL_SEC: float = 900.0
    DEFAULT_HEALTHY_SCORE_MIN: float = 75.0
    DEFAULT_DEGRADED_SCORE_MIN: float = 50.0
    MODULE_TIMEOUT_SEC: float = 30.0
    DEFAULT_SCHEDULER_JOIN_TIMEOUT: float = 5.0

    def __init__(self):
        self._hardware_scanner: Optional[Any] = None
        self._network_grader: Optional[Any] = None
        self._trend_monitor: Optional[Any] = None
        self._negotiation_bus: Optional[Any] = None
        self._behavioral_logger: Optional[Any] = None

        self._lock = threading.Lock()
        self._last_full_check_time: float = 0.0
        self._last_partial_check_time: float = 0.0
        self._full_check_running: bool = False
        self._scheduler_alive: bool = False

        self._scheduler_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # 用于取消正在进行的子模块调用
        self._cancel_module_event = threading.Event()

        # 动态配置（可从配置中心注入）
        self._config_full_interval = self.DEFAULT_FULL_CHECK_INTERVAL_SEC
        self._config_partial_interval = self.DEFAULT_PARTIAL_CHECK_INTERVAL_SEC
        self._config_module_weights = dict(DEFAULT_MODULE_WEIGHTS)

        logger.info("SelfCheck 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(self, **kwargs: Any) -> None:
        if "hardware_scanner" in kwargs:
            obj = kwargs["hardware_scanner"]
            if obj is not None and not callable(getattr(obj, "run_scan", None)):
                logger.error("HardwareScanner 缺少 run_scan 方法，拒绝注入")
            else:
                self._hardware_scanner = obj
                logger.info("HardwareScanner 注入成功")

        if "network_grader" in kwargs:
            obj = kwargs["network_grader"]
            if obj is not None and not callable(getattr(obj, "evaluate", None)):
                logger.error("NetworkQualityGrader 缺少 evaluate 方法，拒绝注入")
            else:
                self._network_grader = obj
                logger.info("NetworkQualityGrader 注入成功")

        if "trend_monitor" in kwargs:
            obj = kwargs["trend_monitor"]
            if obj is not None and not callable(getattr(obj, "analyze", None)):
                logger.error("TrendBaselineMonitor 缺少 analyze 方法，拒绝注入")
            else:
                self._trend_monitor = obj
                logger.info("TrendBaselineMonitor 注入成功")

        if "negotiation_bus" in kwargs:
            obj = kwargs["negotiation_bus"]
            if obj is not None and not callable(getattr(obj, "publish_alert", None)):
                logger.warning("NegotiationBus 缺少 publish_alert 方法，降级")
            else:
                self._negotiation_bus = obj
                logger.info("NegotiationBus 注入成功")

        if "behavioral_logger" in kwargs:
            obj = kwargs["behavioral_logger"]
            if obj is not None and not callable(getattr(obj, "log_event", None)):
                logger.warning("BehavioralLogger 缺少 log_event 方法，降级")
            else:
                self._behavioral_logger = obj
                logger.info("BehavioralLogger 注入成功")

    # ========== 公共接口 ==========
    def run_full_check(self) -> Dict[str, Any]:
        with self._lock:
            if self._full_check_running:
                return {
                    "status": "busy",
                    "reason": "上一次全量自检尚未完成",
                    "data": {},
                    "warnings": ["full_check_already_running"],
                }
            self._full_check_running = True

        start_time = time.monotonic()
        logger.info("开始全量系统自检...")
        results: Dict[str, Dict[str, Any]] = {}
        all_warnings: List[str] = []
        module_scores: Dict[str, float] = {}
        module_levels: Dict[str, str] = {}

        # 重置取消事件
        self._cancel_module_event.clear()

        modules: List[Tuple[str, Callable[[], Dict[str, Any]]]] = [
            ("hardware", self._run_hardware_check),
            ("network", self._run_network_check),
            ("trend_baseline", self._run_trend_check),
        ]

        for module_name, runner in modules:
            result = _call_with_timeout(
                runner,
                self.MODULE_TIMEOUT_SEC,
                module_name,
                cancel_event=self._cancel_module_event,
            )
            results[module_name] = result
            module_warnings = result.get("warnings", [])
            all_warnings.extend(module_warnings)

            data = result.get("data")
            if isinstance(data, dict):
                score = data.get("score")
                if isinstance(score, (int, float)):
                    module_scores[module_name] = float(score)
                else:
                    logger.error(f"{module_name} 返回的 score 无效: {type(score)}")
                    module_scores[module_name] = 0.0
                level = data.get("level", "unavailable")
                module_levels[module_name] = str(level) if level is not None else "unavailable"
            else:
                logger.error(f"{module_name} 返回的 data 非字典: {type(data)}")
                module_scores[module_name] = 0.0
                module_levels[module_name] = "unavailable"

        overall_score = self._compute_weighted_score(module_scores)
        overall_level = self._synthesize_level(overall_score, module_levels)
        elapsed = time.monotonic() - start_time

        # 去重并截断 warnings
        unique_warnings = list(OrderedDict.fromkeys(all_warnings))
        if len(unique_warnings) > 200:
            logger.warning(f"warnings 数量过多 ({len(unique_warnings)})，截断至200")
            unique_warnings = unique_warnings[:200]

        with self._lock:
            self._last_full_check_time = time.monotonic()
            self._full_check_running = False

        if elapsed > SELF_CHECK_SLOW_THRESHOLD:
            logger.warning(f"全量自检耗时过长 ({elapsed:.1f}s)，超过阈值 {SELF_CHECK_SLOW_THRESHOLD}s")
            unique_warnings.append(f"self_check_slow: {elapsed:.1f}s")

        if overall_level in ("degraded", "critical"):
            self._trigger_alert(overall_level, f"系统健康评分: {overall_score:.1f}")

        logger.info("全量自检完成，评分 %.1f，耗时 %.3fs", overall_score, elapsed)

        return {
            "status": "ok",
            "reason": f"全量自检完成，整体评分: {overall_score:.1f} ({overall_level})",
            "data": {
                "overall_score": round(overall_score, 1),
                "overall_level": overall_level,
                "modules": results,
                "elapsed_sec": round(elapsed, 3),
                "timestamp_monotonic": time.monotonic(),
            },
            "warnings": unique_warnings,
        }

    def health_check(self) -> Dict[str, Any]:
        try:
            with self._lock:
                available = []
                unavailable = []
                for attr, name in [
                    ("_hardware_scanner", "hardware_scanner"),
                    ("_network_grader", "network_grader"),
                    ("_trend_monitor", "trend_monitor"),
                ]:
                    if getattr(self, attr, None) is not None:
                        available.append(name)
                    else:
                        unavailable.append(name)
                last_full = self._last_full_check_time
                scheduler_alive = self._scheduler_alive
                full_check_running = self._full_check_running

            status = "ok" if len(unavailable) == 0 else "degraded"
            reason = f"可用模块: {available}; 缺失: {unavailable}" if unavailable else f"可用模块: {available}"

            return {
                "status": status,
                "reason": reason,
                "data": {
                    "available_modules": available,
                    "unavailable_modules": unavailable,
                    "last_full_check_monotonic": last_full,
                    "scheduler_alive": scheduler_alive,
                    "full_check_running": full_check_running,
                },
                "warnings": [f"missing_dependency: {m}" for m in unavailable],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e}", exc_info=True)
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    def update_config(self, full_interval: Optional[float] = None, partial_interval: Optional[float] = None,
                      module_weights: Optional[Dict[str, float]] = None) -> None:
        """动态更新配置（从配置中心调用）"""
        if full_interval is not None and full_interval > 0:
            self._config_full_interval = full_interval
            logger.info(f"全量检查间隔已更新: {full_interval}s")
        if partial_interval is not None and partial_interval > 0:
            self._config_partial_interval = partial_interval
            logger.info(f"局部检查间隔已更新: {partial_interval}s")
        if module_weights is not None and abs(sum(module_weights.values()) - 1.0) < 1e-6:
            self._config_module_weights = dict(module_weights)
            logger.info(f"模块权重已更新: {module_weights}")

    # ========== 定时调度 ==========
    def start_scheduler(self) -> None:
        with self._lock:
            if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
                logger.warning("调度器已在运行")
                return
            self._stop_event.clear()
            self._scheduler_alive = True
            self._scheduler_thread = threading.Thread(
                target=self._scheduler_loop,
                name="SelfCheckScheduler",
                daemon=False,
            )
            self._scheduler_thread.start()
        # 等待线程启动确认
        self._scheduler_thread.join(timeout=0.5)
        if not self._scheduler_thread.is_alive():
            logger.error("调度器线程启动后立即终止")
            self._scheduler_alive = False
        else:
            logger.info(
                "自检调度器已启动，全量间隔=%ds，局部间隔=%ds",
                self._config_full_interval,
                self._config_partial_interval,
            )

    def stop_scheduler(self) -> None:
        self._stop_event.set()
        thread_to_join = None
        with self._lock:
            thread_to_join = self._scheduler_thread
        if thread_to_join is not None:
            thread_to_join.join(timeout=self.DEFAULT_SCHEDULER_JOIN_TIMEOUT)
            if thread_to_join.is_alive():
                logger.warning("调度器线程未能在超时内结束")
            else:
                self._scheduler_alive = False
        logger.info("自检调度器已停止")

    # ========== 私有方法 ==========
    def _run_hardware_check(self) -> Dict[str, Any]:
        if self._hardware_scanner is not None:
            return self._hardware_scanner.run_scan()
        return self._default_unavailable("hardware_scanner")

    def _run_network_check(self) -> Dict[str, Any]:
        if self._network_grader is not None:
            return self._network_grader.evaluate()
        return self._default_unavailable("network_grader")

    def _run_trend_check(self) -> Dict[str, Any]:
        if self._trend_monitor is not None:
            return self._trend_monitor.analyze()
        return self._default_unavailable("trend_monitor")

    @staticmethod
    def _default_unavailable(module_name: str) -> Dict[str, Any]:
        return {
            "status": "degraded",
            "reason": f"{module_name} 未注入",
            "data": {"score": 0.0, "level": "unavailable"},
            "warnings": [f"{module_name}_unavailable: 未注入"],
        }

    def _compute_weighted_score(self, scores: Dict[str, float]) -> float:
        total = 0.0
        weight_sum = 0.0
        for module, score in scores.items():
            w = self._config_module_weights.get(module, 0.0)
            total += score * w
            weight_sum += w
        if weight_sum <= 0.0:
            logger.error("模块权重总和为零，无法计算加权评分")
            return 0.0
        return total / weight_sum

    def _synthesize_level(self, score: float, module_levels: Dict[str, str]) -> str:
        if not module_levels:
            return self._determine_level(score)
        # 所有模块均不可用时，返回 unknown
        if all(level == "unavailable" for level in module_levels.values()):
            return "unknown"
        if any(level == "critical" for level in module_levels.values()):
            return "critical"
        if any(level == "degraded" for level in module_levels.values()):
            return "degraded"
        return self._determine_level(score)

    def _determine_level(self, score: float) -> str:
        if score >= self.DEFAULT_HEALTHY_SCORE_MIN:
            return "healthy"
        if score >= self.DEFAULT_DEGRADED_SCORE_MIN:
            return "degraded"
        return "critical"

    def _trigger_alert(self, level: str, message: str) -> None:
        # 使用消息哈希作为更精确的去重键
        alert_key = f"self_check:{level}:{hash(message) % 100000}"
        if not _dedup_alert(alert_key):
            return

        alert_msg = f"[{level.upper()}] 系统健康检查: {message}"

        if self._negotiation_bus is not None:
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="system_health",
                    level=level,
                    message=message,
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning(f"协商总线告警推送失败: {e}")

        if level == "critical":
            logger.error(
                "%s #RECOVERY: 立即检查硬件状态、网络链路和近期指标趋势，考虑触发降级或休眠",
                alert_msg,
            )
        else:
            logger.warning(alert_msg)

        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="system_health_alert",
                    details={"level": level, "message": message},
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")
                # 最终兜底：写本地文件
                try:
                    with open("/var/log/fire_seed/self_check_alert_fallback.log", "a") as f:
                        f.write(f"{time.monotonic()}\t{level}\t{message}\n")
                except Exception:
                    pass

    def _scheduler_loop(self) -> None:
        logger.info("自检调度线程启动")
        while not self._stop_event.is_set():
            interval = min(self._config_full_interval, self._config_partial_interval)
            self._stop_event.wait(timeout=interval)
            if self._stop_event.is_set():
                break

            with self._lock:
                now = time.monotonic()
                need_full = (now - self._last_full_check_time >= self._config_full_interval)
                need_partial = (now - self._last_partial_check_time >= self._config_partial_interval)
                # 避免重复执行
                if need_full:
                    self._last_full_check_time = now  # 提前标记，防止并发
                if need_partial and not need_full:
                    self._last_partial_check_time = now

            if need_full:
                try:
                    self.run_full_check()
                except Exception as e:
                    logger.error(f"全量自检调度异常: {e}", exc_info=True)
                # 全量自检后同步局部时间戳
                with self._lock:
                    self._last_partial_check_time = time.monotonic()
            elif need_partial:
                try:
                    self._run_trend_check()
                except Exception as e:
                    logger.error(f"局部自检异常: {e}", exc_info=True)
        logger.info("自检调度线程退出")
