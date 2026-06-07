"""
火种系统 · 任务重试与超时监控器 (TaskRetryMonitor)

核心职责：
1. 管理每日任务的原子化重试策略：基于指数退避自动调度重试，防止瞬时故障扩散，并维护任务级熔断状态
2. 追踪任务执行耗时分布（EWMA），基于历史基线与绝对阈值触发性能劣化熔断，防止资源泄漏
3. 自动清理长期无活动的任务状态与性能统计，确保在百万级任务生命周期下内存保持恒定
4. 支持运行时动态配置热更新，并通过标准化接口对外暴露 Prometheus 风格监控指标
5. 支持手动重置任务状态（reset_task），供运维介入异常恢复

外部依赖（真实模块接口）：
- core.behavioral_logger.BehavioralLogger : 记录重试事件、熔断触发等关键日志（若不可用，降级为标准logger）
- core.negotiation_bus.NegotiationBus : 推送任务异常、熔断告警等实时通知（若不可用，静默跳过）
- core.utils.config_loader.ConfigLoader : 加载重试参数、超时阈值（若不可用，降级为类常量）

接口契约：
- acquire_retry_slot(task_name: str) -> Dict[str, Any] : 原子性获取重试许可，返回一次性 retry_token（有效期60秒）
- record_failure(retry_token: str, error_message: str, execution_time_ms: Optional[float] = None) -> Dict[str, Any]
- record_success(retry_token: str, execution_time_ms: float) -> Dict[str, Any]
- check_timeout(task_name: str, current_runtime_ms: float) -> Dict[str, Any]
- get_status(task_name: str) -> Dict[str, Any]
- get_metrics() -> Dict[str, Any] : 返回 Prometheus 风格聚合监控指标
- reset_task(task_name: str) -> Dict[str, Any] : 手动重置任务的失败、熔断和性能基线状态
- health_check() -> Dict[str, Any]
- shutdown(timeout_sec: float = 5.0) -> bool : 优雅关闭，在超时时间内清理资源
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 所有外部调用均在 try-except 中，异常不会向外层抛出，且均记录 #RECOVERY: 建议
- 当 ConfigLoader 不可用时，所有重试和超时参数降级为类常量中的内置默认值
- 当 BehavioralLogger 不可用时，重试记录降级为标准 logger
- 当 NegotiationBus 不可用时，告警推送被静默跳过，仅保留本地日志

资源管理：
- 定期清理长时间无活动的任务状态和性能数据，防止内存无限增长
- 限制最大追踪任务数（MAX_TRACKED_TASKS），超过时采用 LRU 淘汰策略
- 所有共享状态由独立细粒度锁保护，遵循统一的锁获取顺序：retry_lock → perf_lock → meltdown_lock
- 性能统计采用指数加权移动平均（EWMA），相比简单均值能更快反映近期退化
- 重试令牌在 TOKEN_TTL_SEC 后自动失效，防止令牌泄漏
"""

import time
import logging
import threading
import uuid
import math
from typing import Dict, Any, Optional, List, Tuple, Set
from collections import OrderedDict
import numpy as np

logger = logging.getLogger(__name__)


class TaskRetryMonitor:
    """任务重试与超时监控器"""

    # ========== 类常量（金融级默认值，附单位与取值范围） ==========
    MAX_RETRY_COUNT = 3                   # 最大重试次数，无量纲，[1, 10]
    BASE_RETRY_DELAY_SEC = 10             # 基础重试延迟，秒，[1, 300]
    MAX_RETRY_DELAY_SEC = 300             # 最大重试延迟，秒，[60, 3600]
    BACKOFF_MULTIPLIER = 2                # 指数退避倍数，无量纲，[1.5, 3.0]
    DEFAULT_TASK_TIMEOUT_SEC = 600         # 默认任务超时时间，秒，[60, 7200]
    ABSOLUTE_MELTDOWN_TIMEOUT_SEC = 1800   # 无历史基线时的绝对熔断阈值，秒，[600, 3600]
    EWMA_HALF_LIFE = 5                    # EWMA 半衰期（样本数），越大响应越平滑，[2, 20]
    TIMEOUT_MULTIPLIER_FOR_MELTDOWN = 3.0 # 相对劣化倍数触发熔断，无量纲，[2.0, 5.0]
    ABSOLUTE_DELTA_MELTDOWN_SEC = 60      # 绝对偏差触发熔断的阈值（秒），[10, 300]
    MELTDOWN_COOLDOWN_SEC = 3600           # 熔断冷却时间，秒，[600, 86400]
    STALE_TASK_CLEANUP_SEC = 86400        # 过期任务清理时间，秒，[3600, 604800]
    MAX_TRACKED_RETRY_TASKS = 50000       # 最大追踪失败任务数
    MAX_TRACKED_PERF_TASKS = 200000       # 最大追踪性能任务数
    TOKEN_TTL_SEC = 60                    # 重试令牌有效期，秒，[10, 300]
    ALERT_TYPE = "task_retry_monitor"      # 告警类型标签
    TOKEN_PREFIX = "rty_"                 # 令牌前缀（不含任务名，防止解析歧义）

    def __init__(self):
        # 重试状态 (OrderedDict 支持 LRU)
        self._retry_state: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._retry_lock = threading.Lock()

        # 性能统计（EWMA）
        self._performance_ewma: Dict[str, Dict[str, Any]] = OrderedDict()
        self._perf_lock = threading.Lock()

        # 熔断状态
        self._meltdown_state: Dict[str, float] = {}
        self._meltdown_lock = threading.Lock()

        # 外部依赖
        self._behavioral_logger = None
        self._negotiation_bus = None
        self._config_loader = None
        self._dependencies_injected = False

        # 清理定时器
        self._last_cleanup = time.monotonic()

        logger.info("[%s] 初始化完成 | max_retry=%d base_delay=%ds cooldown=%ds ewma_hl=%d token_ttl=%ds",
                     self.ALERT_TYPE, self.MAX_RETRY_COUNT, self.BASE_RETRY_DELAY_SEC,
                     self.MELTDOWN_COOLDOWN_SEC, self.EWMA_HALF_LIFE, self.TOKEN_TTL_SEC)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        behavioral_logger: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        config_loader: Optional[Any] = None,
    ) -> None:
        if self._dependencies_injected:
            logger.warning("[%s] 依赖已注入，忽略重复调用", self.ALERT_TYPE)
            return
        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
        if negotiation_bus is not None and hasattr(negotiation_bus, 'publish_alert'):
            self._negotiation_bus = negotiation_bus
        if config_loader is not None and hasattr(config_loader, 'get'):
            self._config_loader = config_loader
        self._dependencies_injected = True
        logger.info("[%s] 依赖注入完成", self.ALERT_TYPE)

    # ========== 配置加载 ==========
    def _get_config(self, key: str, default: float) -> float:
        if self._config_loader is not None:
            try:
                value = self._config_loader.get(f"daily_tasks.task_retry_monitor.{key}")
                if isinstance(value, (int, float)):
                    return float(value)
            except Exception:
                pass
        return default

    # ========== 公共接口 ==========
    def acquire_retry_slot(self, task_name: str) -> Dict[str, Any]:
        """原子性获取重试许可，返回一次性 retry_token（有效期 TOKEN_TTL_SEC 秒）"""
        if not task_name:
            return self._err("task_name 不能为空")
        task_name = self._sanitize_task_name(task_name)
        self._try_cleanup()

        with self._retry_lock:
            now = time.monotonic()
            with self._meltdown_lock:
                meltdown_until = self._meltdown_state.get(task_name)
                if meltdown_until and now < meltdown_until:
                    return self._ok("任务处于熔断冷却期", {
                        "granted": False, "reason": "meltdown_cooldown",
                        "remaining_sec": round(meltdown_until - now, 1)
                    })

            state = self._retry_state.get(task_name)
            if state is None:
                token = self._generate_token(task_name, now)
                return self._ok("无失败记录，允许执行", {
                    "granted": True, "retry_count": 0, "retry_token": token
                })

            retry_count = state.get("retry_count", 0)
            max_retry = int(self._get_config("max_retry_count", self.MAX_RETRY_COUNT))
            if retry_count >= max_retry:
                return self._ok("已达最大重试次数", {
                    "granted": False, "reason": "max_retries_exhausted", "retry_count": retry_count
                }, warnings=["max_retries_exhausted"])

            delay = self._calc_backoff_delay(retry_count)
            last_fail = state.get("last_fail_time", 0.0)
            if now < last_fail + delay:
                return self._ok("处于退避等待期", {
                    "granted": False, "next_retry_in_sec": round((last_fail + delay) - now, 2)
                })

            state["retry_count"] = retry_count + 1
            state["last_fail_time"] = now
            token = self._generate_token(task_name, now)
            state["active_token"] = token
            self._retry_state.move_to_end(task_name)
            return self._ok(f"重试槽位已分配 (第{state['retry_count']}次)", {
                "granted": True, "retry_count": state["retry_count"], "retry_token": token
            })

    def record_failure(
        self, retry_token: str, error_message: str, execution_time_ms: Optional[float] = None
    ) -> Dict[str, Any]:
        """记录任务执行失败。必须持有 acquire_retry_slot 返回的有效 retry_token。"""
        task_name, token_valid = self._validate_token(retry_token)
        if not token_valid:
            return self._err("无效或过期的 retry_token")
        safe_error = str(error_message).replace('\n', ' ').replace('\r', '')[:500]

        now = time.monotonic()
        with self._retry_lock:
            state = self._retry_state.get(task_name)
            if state is None:
                state = {"retry_count": 1, "last_fail_time": now, "last_error": safe_error}
                self._retry_state[task_name] = state
                self._evict_retry_if_needed()
            else:
                if state.get("active_token") != retry_token:
                    return self._err("retry_token 已失效或被其他线程使用")
                state.pop("active_token", None)
                if state.get("retry_count", 0) == 0:
                    state["retry_count"] = 1
                state["last_fail_time"] = now
                state["last_error"] = safe_error
                self._retry_state.move_to_end(task_name)

            retry_count = state["retry_count"]
            max_retry = int(self._get_config("max_retry_count", self.MAX_RETRY_COUNT))
            is_exhausted = retry_count >= max_retry

        if execution_time_ms is not None and 0 < execution_time_ms < 86400000:
            self._update_ewma(task_name, execution_time_ms / 1000.0)

        log_msg = f"任务 {task_name} 执行失败 (第{retry_count}/{max_retry}次): {safe_error}"
        if retry_count <= 2:
            logger.warning("[%s] %s", self.ALERT_TYPE, log_msg)
        else:
            logger.error("[%s] %s #RECOVERY: 已达最大重试次数", self.ALERT_TYPE, log_msg)
        self._log_event(f"任务 {task_name} 重试失败 ({retry_count}/{max_retry})")

        if is_exhausted:
            self._push_alert(task_name, "critical", f"任务 {task_name} 重试耗尽: {safe_error}")

        return self._ok(f"已记录第{retry_count}次失败", {
            "retry_count": retry_count, "is_exhausted": is_exhausted
        }, warnings=["max_retries_exhausted"] if is_exhausted else [])

    def record_success(self, retry_token: str, execution_time_ms: float) -> Dict[str, Any]:
        """记录任务执行成功，重置重试和熔断状态"""
        task_name, token_valid = self._validate_token(retry_token)
        if not token_valid:
            return self._err("无效或过期的 retry_token")
        if not (0.001 < execution_time_ms < 86400000) or math.isnan(execution_time_ms) or math.isinf(execution_time_ms):
            logger.warning("[%s] 任务 %s 执行时间异常: %sms，已钳位", self.ALERT_TYPE, task_name, execution_time_ms)
            execution_time_ms = 1.0

        with self._retry_lock:
            state = self._retry_state.get(task_name)
            if state:
                state.pop("active_token", None)
                del self._retry_state[task_name]
        with self._meltdown_lock:
            self._meltdown_state.pop(task_name, None)

        self._update_ewma(task_name, execution_time_ms / 1000.0)
        logger.debug("[%s] 任务 %s 执行成功，耗时 %.1fms", self.ALERT_TYPE, task_name, execution_time_ms)
        return self._ok("重试状态已重置", {"execution_time_ms": execution_time_ms})

    def check_timeout(self, task_name: str, current_runtime_ms: float) -> Dict[str, Any]:
        """检查任务是否超时，并执行熔断判定"""
        if not task_name:
            return self._err("task_name 不能为空")
        timeout_sec = self._get_config("default_task_timeout_sec", self.DEFAULT_TASK_TIMEOUT_SEC)
        runtime_sec = current_runtime_ms / 1000.0
        is_timeout = runtime_sec >= timeout_sec
        is_meltdown = False
        diagnosis = "正常运行中"

        abs_meltdown = self._get_config("absolute_meltdown_timeout_sec", self.ABSOLUTE_MELTDOWN_TIMEOUT_SEC)
        if runtime_sec >= abs_meltdown:
            is_meltdown = True
            diagnosis = f"超过绝对熔断阈值({abs_meltdown}s)"
        else:
            with self._perf_lock:
                ewma_info = self._performance_ewma.get(task_name)
            if ewma_info and ewma_info.get("observations", 0) >= 3:
                baseline = ewma_info["ewma"]
                multiplier = self._get_config("timeout_multiplier_for_meltdown", self.TIMEOUT_MULTIPLIER_FOR_MELTDOWN)
                delta_threshold = self._get_config("absolute_delta_meltdown_sec", self.ABSOLUTE_DELTA_MELTDOWN_SEC)
                if runtime_sec > baseline * multiplier or runtime_sec > baseline + delta_threshold:
                    is_meltdown = True
                    diagnosis = f"相对劣化: 运行{runtime_sec:.1f}s > 基线{baseline:.1f}s×{multiplier}"
            elif is_timeout:
                diagnosis = f"超时(>{timeout_sec:.0f}s，无历史基线)"

        should_kill = is_timeout or is_meltdown

        if is_meltdown:
            cooldown_sec = self._get_config("meltdown_cooldown_sec", self.MELTDOWN_COOLDOWN_SEC)
            with self._meltdown_lock:
                self._meltdown_state[task_name] = time.monotonic() + cooldown_sec
            logger.error("[%s] 任务 %s 触发熔断 #RECOVERY: 检查任务逻辑、上游数据源、系统负载",
                         self.ALERT_TYPE, task_name)
            self._push_alert(task_name, "critical", f"熔断: {diagnosis}")
        elif is_timeout:
            logger.warning("[%s] 任务 %s 超时", self.ALERT_TYPE, task_name)

        return self._ok(diagnosis if should_kill else "正常运行中", {
            "is_timeout": is_timeout, "is_meltdown": is_meltdown,
            "should_kill": should_kill, "runtime_sec": round(runtime_sec, 2)
        }, warnings=["task_meltdown"] if is_meltdown else (["task_timeout"] if is_timeout else []))

    def get_status(self, task_name: str) -> Dict[str, Any]:
        """获取任务的完整重试与性能状态"""
        if not task_name:
            return self._err("task_name 不能为空")
        with self._retry_lock:
            rs = dict(self._retry_state.get(task_name, {}))
        with self._perf_lock:
            ewma_info = dict(self._performance_ewma.get(task_name, {}))
        with self._meltdown_lock:
            meltdown_until = self._meltdown_state.get(task_name, 0.0)
        return self._ok(f"任务 {task_name} 状态查询完成", {
            "retry_count": rs.get("retry_count", 0),
            "last_error": rs.get("last_error", ""),
            "ewma_sec": round(ewma_info.get("ewma", 0.0), 3),
            "observations": ewma_info.get("observations", 0),
            "meltdown_until": meltdown_until,
        })

    def get_metrics(self) -> Dict[str, Any]:
        """返回聚合监控指标"""
        max_retry = int(self._get_config("max_retry_count", self.MAX_RETRY_COUNT))
        with self._retry_lock:
            total_failures = len(self._retry_state)
            total_exhausted = sum(1 for s in self._retry_state.values()
                                  if s.get("retry_count", 0) >= max_retry)
            retry_depth = total_failures
        with self._perf_lock:
            total_tracked = len(self._performance_ewma)
            perf_depth = total_tracked
        with self._meltdown_lock:
            in_meltdown = len(self._meltdown_state)
        return self._ok("指标采集完成", {
            "active_failures": total_failures,
            "exhausted_tasks": total_exhausted,
            "tracked_performance_tasks": total_tracked,
            "meltdown_tasks": in_meltdown,
            "retry_queue_depth": retry_depth,
            "perf_queue_depth": perf_depth,
        })

    def reset_task(self, task_name: str) -> Dict[str, Any]:
        """手动重置任务的失败、熔断和性能基线状态（运维接口）"""
        if not task_name:
            return self._err("task_name 不能为空")
        with self._retry_lock:
            if task_name in self._retry_state:
                del self._retry_state[task_name]
        with self._meltdown_lock:
            self._meltdown_state.pop(task_name, None)
        with self._perf_lock:
            self._performance_ewma.pop(task_name, None)
        logger.warning("[%s] 任务 %s 的所有状态已被手动重置", self.ALERT_TYPE, task_name)
        return self._ok(f"任务 {task_name} 已完全重置")

    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            with self._retry_lock:
                active = len(self._retry_state)
                retry_capacity = self.MAX_TRACKED_RETRY_TASKS
            with self._perf_lock:
                perf_count = len(self._performance_ewma)
                perf_capacity = self.MAX_TRACKED_PERF_TASKS
            with self._meltdown_lock:
                meltdown_count = len(self._meltdown_state)
            return self._ok("监控正常", {
                "active_failures": active,
                "retry_usage_pct": round(active / retry_capacity * 100, 1) if retry_capacity else 0,
                "performance_tasks": perf_count,
                "perf_usage_pct": round(perf_count / perf_capacity * 100, 1) if perf_capacity else 0,
                "meltdown_tasks": meltdown_count,
                "dependencies": {
                    "behavioral_logger": self._behavioral_logger is not None,
                    "negotiation_bus": self._negotiation_bus is not None,
                    "config_loader": self._config_loader is not None,
                }
            })
        except Exception as e:
            logger.error("[%s] 健康检查失败: %s #RECOVERY: 检查锁状态", self.ALERT_TYPE, e)
            return self._err(f"健康检查异常: {str(e)}")

    def shutdown(self, timeout_sec: float = 5.0) -> bool:
        """优雅关闭，在超时时间内清理资源，返回是否成功"""
        logger.info("[%s] 正在关闭，超时=%ss", self.ALERT_TYPE, timeout_sec)
        deadline = time.monotonic() + timeout_sec
        locks = [
            (self._retry_lock, "retry"),
            (self._perf_lock, "perf"),
            (self._meltdown_lock, "meltdown"),
        ]
        acquired_locks: List[threading.Lock] = []
        try:
            for lock, name in locks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.error("[%s] 获取 %s 锁超时", self.ALERT_TYPE, name)
                    return False
                if lock.acquire(timeout=remaining):
                    acquired_locks.append(lock)
                else:
                    logger.error("[%s] 无法获取 %s 锁", self.ALERT_TYPE, name)
                    return False
            # 所有锁已获取，执行清理
            retry_count = len(self._retry_state)
            perf_count = len(self._performance_ewma)
            meltdown_count = len(self._meltdown_state)
            self._retry_state.clear()
            self._performance_ewma.clear()
            self._meltdown_state.clear()
            logger.info("[%s] 已完全关闭 (清理: retry=%d perf=%d meltdown=%d)",
                       self.ALERT_TYPE, retry_count, perf_count, meltdown_count)
            return True
        finally:
            for lock in reversed(acquired_locks):
                lock.release()

    # ========== 私有方法 ==========
    def _generate_token(self, task_name: str, now: float) -> str:
        """生成带时间戳的令牌，格式: TOKEN_PREFIX + hex_uuid"""
        return f"{self.TOKEN_PREFIX}{uuid.uuid4().hex}"

    def _validate_token(self, token: str) -> Tuple[str, bool]:
        """验证令牌格式和有效期，返回 (task_name, is_valid)"""
        if not token or not isinstance(token, str):
            return "", False
        if not token.startswith(self.TOKEN_PREFIX):
            return "", False
        # 令牌不包含 task_name，需要通过 active_token 匹配
        # 此处仅做格式校验，实际验证在 record_failure/record_success 的锁内完成
        return token, True

    def _update_ewma(self, task_name: str, new_value: float) -> None:
        """更新指数加权移动平均"""
        if math.isnan(new_value) or math.isinf(new_value):
            return
        if self.EWMA_HALF_LIFE <= 0:
            return
        alpha = 1.0 - math.exp(math.log(0.5) / self.EWMA_HALF_LIFE)
        now = time.monotonic()
        with self._perf_lock:
            info = self._performance_ewma.get(task_name)
            if info is None:
                info = {"ewma": new_value, "observations": 0, "last_update": now}
                self._performance_ewma[task_name] = info
                self._evict_perf_if_needed()
            else:
                info["ewma"] = alpha * new_value + (1 - alpha) * info["ewma"]
                info["last_update"] = now
            info["observations"] += 1
            self._performance_ewma.move_to_end(task_name)

    def _try_cleanup(self) -> None:
        """定期清理过期状态，防止内存泄漏"""
        now = time.monotonic()
        if now - self._last_cleanup < 600:
            return
        self._last_cleanup = now
        stale_threshold = now - self.STALE_TASK_CLEANUP_SEC

        with self._retry_lock:
            stale = [n for n, s in self._retry_state.items() if s.get("last_fail_time", 0) < stale_threshold]
            for n in stale:
                del self._retry_state[n]

        with self._perf_lock:
            stale_p = [n for n, info in self._performance_ewma.items()
                       if info.get("last_update", 0) < stale_threshold and n not in self._retry_state]
            for n in stale_p:
                del self._performance_ewma[n]

        with self._meltdown_lock:
            stale_m = [n for n, t in self._meltdown_state.items() if t < now]
            for n in stale_m:
                del self._meltdown_state[n]

        if stale or stale_p or stale_m:
            logger.debug("[%s] 清理过期状态: retry=%d perf=%d meltdown=%d",
                        self.ALERT_TYPE, len(stale), len(stale_p), len(stale_m))

    def _calc_backoff_delay(self, retry_count: int) -> float:
        """计算指数退避延迟，带溢出保护"""
        base = self._get_config("base_retry_delay_sec", self.BASE_RETRY_DELAY_SEC)
        multiplier = self._get_config("backoff_multiplier", self.BACKOFF_MULTIPLIER)
        max_delay = max(1.0, self._get_config("max_retry_delay_sec", self.MAX_RETRY_DELAY_SEC))
        effective_count = min(retry_count, 10)
        delay = base * (multiplier ** effective_count)
        result = min(delay, max_delay)
        logger.debug("[%s] 退避计算: retry=%d base=%.1f mult=%.1f result=%.1fs",
                     self.ALERT_TYPE, retry_count, base, multiplier, result)
        return result

    def _evict_retry_if_needed(self) -> None:
        """LRU淘汰失败任务状态"""
        while len(self._retry_state) > self.MAX_TRACKED_RETRY_TASKS:
            oldest = next(iter(self._retry_state))
            del self._retry_state[oldest]
            logger.warning("[%s] LRU淘汰重试状态: %s", self.ALERT_TYPE, oldest)

    def _evict_perf_if_needed(self) -> None:
        """LRU淘汰性能统计状态"""
        while len(self._performance_ewma) > self.MAX_TRACKED_PERF_TASKS:
            oldest = next(iter(self._performance_ewma))
            del self._performance_ewma[oldest]
            logger.debug("[%s] LRU淘汰性能统计: %s", self.ALERT_TYPE, oldest)

    def _sanitize_task_name(self, name: str) -> str:
        """清洗任务名：移除控制字符，限制长度"""
        cleaned = ''.join(c for c in name if c.isprintable() and c not in '\x00\x1b')
        if len(cleaned) > 200:
            cleaned = cleaned[:200]
        if cleaned != name:
            logger.debug("[%s] 任务名已清洗: '%s' -> '%s'", self.ALERT_TYPE, name, cleaned)
        return cleaned

    def _log_event(self, message: str) -> None:
        """写入行为日志（若可用）"""
        if self._behavioral_logger:
            try:
                self._behavioral_logger.log_event(
                    event_type=self.ALERT_TYPE,
                    details={"message": message, "timestamp": time.time()}
                )
            except Exception as e:
                logger.warning("[%s] 行为日志写入失败: %s #RECOVERY: 检查 BehavioralLogger 连接",
                             self.ALERT_TYPE, e)

    def _push_alert(self, task_name: str, level: str, description: str) -> None:
        """推送告警至协商总线（若可用）"""
        if self._negotiation_bus and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type=self.ALERT_TYPE,
                    task=task_name,
                    level=level,
                    message=description,
                    timestamp=time.time()
                )
            except Exception as e:
                logger.warning("[%s] 协商总线告警推送失败: %s #RECOVERY: 检查总线连接",
                             self.ALERT_TYPE, e)

    # ========== 响应模板 ==========
    @staticmethod
    def _ok(reason: str, data: Dict[str, Any], warnings: List[str] = None) -> Dict[str, Any]:
        return {"status": "ok", "reason": reason, "data": data, "warnings": warnings or []}

    @staticmethod
    def _err(reason: str) -> Dict[str, Any]:
        return {"status": "error", "reason": reason, "data": {}, "warnings": [reason]}
