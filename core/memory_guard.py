"""
火种系统 · 内存保护模块 (MemoryGuard)

核心职责：
1. 实时监控系统内存使用率，基于滑动窗口统计、趋势预测和分级阈值，自动触发保护动作（告警、主动释放、强制降级）
2. 提供多维内存状态查询与历史趋势接口，支持与协商总线、行为日志、资源调度器集成，实现内存压力下的自动弹性响应

外部依赖（真实模块接口）：
- numpy (可选) : 用于统计计算，若不可用则降级为纯 Python 实现
- core.negotiation_bus.NegotiationBus : 发送内存告警事件与保护动作通知
- core.behavioral_logger.BehavioralLogger : 记录内存告警与保护动作日志
- core.resource_governor.ResourceGovernor : 请求释放非关键资源（可选）
- core.module_health_monitor.ModuleHealthMonitor : 上报本模块健康状态变化（可选）

Error Codes:
- PSUTIL_NOT_AVAILABLE : psutil 模块未安装，内存监控功能完全降级
- MEMORY_STATUS_ERROR : 获取内存状态时发生异常
- FORCE_CLEANUP_ERROR : 手动清理过程中发生异常
- HEALTH_CHECK_ERROR : 健康检查自身发生异常
- NUMPY_NOT_AVAILABLE : numpy 不可用时降级为纯 Python 统计
- CGROUP_READ_ERROR : 无法读取 cgroup 内存限制
- CONTAINER_USAGE_ERROR : 无法获取容器当前内存使用量
- HARD_KILL_EXECUTED : 硬终止保护已触发

接口契约：
- check_and_act() -> Dict[str, Any] : 检查内存使用率并执行保护动作，返回详细状态与执行动作
- get_memory_status() -> Dict[str, Any] : 获取当前内存使用详情（绝对值、百分比、趋势）
- get_memory_history(minutes: int = 5) -> Dict[str, Any] : 获取最近 N 分钟的内存使用率历史序列
- force_cleanup() -> Dict[str, Any] : 手动触发内存清理（GC + 缓存释放）
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict),
  "error_code" (Optional[str]), "warnings" (List[str])

异常与降级：
- 当 psutil 不可用时，内存监控功能降级，check_and_act() 返回 "degraded" 并跳过保护逻辑
- 当 numpy 不可用时，所有统计计算降级为纯 Python 实现的修剪均值和中位数，并自动标记降级状态
- 当外部依赖不可用时，告警和日志记录降级为本地 logger
- 当检测到内存持续高压时，自动调用 ResourceGovernor 释放非关键缓存
- 所有降级值在类常量区明确声明

资源管理：
- 本模块不持有任何需要手动释放的资源
- 定时检查功能由外部调度器驱动，本模块不创建常驻线程
- 手动清理接口会触发 Python gc.collect() 并尝试清理内部历史数据缓冲区
"""

import gc
import inspect
import logging
import os
import signal
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---- numpy 降级处理 ----
try:
    import numpy as np

    _NUMPY_AVAILABLE = True
except ImportError:
    np = None  # type: ignore[assignment]
    _NUMPY_AVAILABLE = False
    logger.warning("numpy 未安装，统计功能将降级为纯 Python 实现")

# ---- psutil 降级处理 ----
try:
    import psutil

    _PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None  # type: ignore[assignment]
    _PSUTIL_AVAILABLE = False
    logger.warning("psutil 未安装，内存监控功能降级。请执行 'pip install psutil'")


# ---- 纯 Python 统计函数 (numpy 降级后备) ----
def _py_mean(data: List[float]) -> float:
    if not data:
        return 0.0
    return sum(data) / len(data)


def _py_median(data: List[float]) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    n = len(sorted_data)
    mid = n // 2
    if n % 2 == 0:
        return (sorted_data[mid - 1] + sorted_data[mid]) / 2.0
    return sorted_data[mid]


def _py_trimmed_mean(data: List[float], trim_ratio: float = 0.1) -> float:
    if not data:
        return 0.0
    if len(data) <= 3:
        return _py_mean(data)
    sorted_data = sorted(data)
    trim_count = max(1, int(len(sorted_data) * trim_ratio))
    trimmed = sorted_data[trim_count:-trim_count] if len(sorted_data) > 2 * trim_count else sorted_data
    return _py_mean(trimmed)


def _py_theil_sen_slope(x: List[float], y: List[float]) -> float:
    """纯 Python Theil-Sen 斜率估计器 (基于时间戳)"""
    n = len(x)
    if n < 2:
        return 0.0
    # 采样优化：若点数过多，均匀采样至多 60 个点
    if n > 60:
        step = max(1, n // 60)
        x = x[::step]
        y = y[::step]
        n = len(x)
    slopes: List[float] = []
    for i in range(n):
        for j in range(i + 1, n):
            dx = x[j] - x[i]
            if dx > 0:
                slopes.append((y[j] - y[i]) / dx)
    return _py_median(slopes) if slopes else 0.0


# 统一出口：根据 numpy 可用性选择实现
def _trimmed_mean(data: List[float], trim_ratio: float = 0.1) -> float:
    if _NUMPY_AVAILABLE:
        try:
            if not data:
                return 0.0
            if len(data) <= 3:
                return float(np.mean(data))
            sorted_data = sorted(data)
            trim_count = max(1, int(len(sorted_data) * trim_ratio))
            trimmed = sorted_data[trim_count:-trim_count] if len(sorted_data) > 2 * trim_count else sorted_data
            result = float(np.mean(trimmed))
            if np.isfinite(result):
                return result
        except Exception:
            logger.debug("numpy 计算修剪均值失败，降级为纯 Python 实现")
    return _py_trimmed_mean(data, trim_ratio)


def _theil_sen_slope(x: List[float], y: List[float]) -> float:
    if _NUMPY_AVAILABLE:
        try:
            n = len(x)
            if n < 2:
                return 0.0
            if n > 60:
                step = max(1, n // 60)
                x = x[::step]
                y = y[::step]
                n = len(x)
            slopes: List[float] = []
            for i in range(n):
                for j in range(i + 1, n):
                    dx = x[j] - x[i]
                    if dx > 0:
                        slopes.append((y[j] - y[i]) / dx)
            result = float(np.median(slopes)) if slopes else 0.0
            if np.isfinite(result):
                return result
        except Exception:
            logger.debug("numpy 计算 Theil-Sen 斜率失败，降级为纯 Python 实现")
    return _py_theil_sen_slope(x, y)


def _simple_moving_average(data: List[float], window: int = 10) -> float:
    if not data:
        return 0.0
    recent = data[-window:] if len(data) >= window else data
    return _py_mean(recent)


# ---- cgroup 操作 ----
_CGROUP_MEM_LIMIT_PATH = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
_CGROUP_V2_MAX_PATH = "/sys/fs/cgroup/memory.max"
_CGROUP_V2_CURRENT_PATH = "/sys/fs/cgroup/memory.current"


def _read_file_int(path: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            val = fh.read().strip().split()[0]
        return int(val) if val and val != "max" else None
    except (FileNotFoundError, ValueError, PermissionError, IndexError, OSError):
        return None


def _get_cgroup_memory_limit() -> Optional[int]:
    for p in (_CGROUP_MEM_LIMIT_PATH, _CGROUP_V2_MAX_PATH):
        limit = _read_file_int(p)
        if limit is not None and limit > 0:
            return limit
    return None


def _get_cgroup_current_usage() -> Optional[int]:
    return _read_file_int(_CGROUP_V2_CURRENT_PATH)


class MemoryGuard:
    """系统内存保护器 (机构级)"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # -- 阈值 --
    WARNING_THRESHOLD_PCT: float = 80.0          # 警告阈值（内存使用率百分比），% [50, 95]
    CRITICAL_THRESHOLD_PCT: float = 90.0         # 临界阈值（内存使用率百分比），% [80, 98]
    SWAP_WARNING_PCT: float = 50.0               # 交换空间使用率警告阈值，% [0, 100]
    SWAP_CRITICAL_PCT: float = 80.0              # 交换空间使用率临界阈值，% [50, 100]
    HARD_KILL_THRESHOLD_PCT: float = 98.0        # 强制退出阈值，% [95, 100]，0 表示禁用

    # 强制退出动作说明：
    #   "log_and_exit" : 仅记录致命日志，不主动退出进程 (由外部 watchdog 处理)
    #   "os_exit"      : 立即调用 os._exit(1) 硬终止，不触发任何清理
    #   "signal"       : 向自身进程发送 SIGTERM，尝试优雅退出
    HARD_KILL_ACTION: str = "log_and_exit"

    # -- 检测与抑制 --
    SLIDING_WINDOW_SIZE: int = 12                # 滑动窗口样本数，无量纲，[5, 60]
    SUSTAINED_DURATION_SEC: float = 30.0         # 持续超过阈值的时长才触发动作，秒，[10, 300]
    ALERT_COOLDOWN_SEC: float = 60.0             # 同级别告警最小间隔，秒，[10, 600]
    SWAP_ALERT_COOLDOWN_SEC: float = 120.0       # 交换空间告警最小间隔，秒，[30, 600]
    TREND_PREDICT_WINDOW: int = 5                # 趋势预测所需的最少样本数，无量纲，[3, 20]
    TRIMMED_MEAN_RATIO: float = 0.1              # 修剪均值剔除比例，无量纲，[0.0, 0.25]
    TREND_SLOPE_THRESHOLD: float = 0.3           # 趋势判定斜率阈值 (%/样本)，[0.1, 2.0]
    RSS_LEAK_DETECTION_WINDOW: int = 30          # 内存泄漏检测窗口样本数，无量纲，[10, 120]
    RSS_LEAK_SLOPE_THRESHOLD: float = 0.05       # 泄漏判定斜率阈值 (MB/秒)，[0.01, 1.0]

    # -- 降级常量 --
    DEGRADED_MEMORY_PCT: float = -1.0            # 表示无法获取内存数据
    MAX_HISTORY_MINUTES: int = 60                 # 历史数据最长保留分钟数，[10, 240]
    MAX_HISTORY_POINTS: int = 120                 # 历史数据返回的最大点数，无量纲，[60, 600]

    # -- 进程自我保护 --
    PROCESS_RSS_LIMIT_MB: int = 0                 # 本进程 RSS 上限 (MB)，0 表示禁用，[0, 物理内存-512]

    # -- 容器 / cgroup 感知 --
    _cgroup_mem_limit: Optional[int] = None       # 容器内存硬上限 (字节)
    _cgroup_last_read: float = 0.0                # 上次读取 cgroup 限制的时间戳
    _cgroup_reread_interval: float = 60.0         # cgroup 限制重读间隔，秒
    _cgroup_usage_cache: Optional[int] = None      # 缓存 cgroup 当前使用量
    _cgroup_usage_cache_time: float = 0.0         # 缓存时间戳
    _cgroup_usage_cache_ttl: float = 5.0          # 缓存有效期，秒

    def __init__(self) -> None:
        # ---- 内存使用率滑动窗口 (百分比) ----
        self._mem_history: deque = deque(maxlen=self.SLIDING_WINDOW_SIZE)
        self._swap_history: deque = deque(maxlen=self.SLIDING_WINDOW_SIZE)

        # ---- 压力持续时间追踪 ----
        self._warning_sustained_start: float = 0.0
        self._critical_sustained_start: float = 0.0

        # ---- 告警冷却时间戳 ----
        self._last_warning_alert: float = 0.0
        self._last_critical_alert: float = 0.0
        self._last_swap_critical_alert: float = 0.0

        # ---- 依赖注入 ----
        self._negotiation_bus: Optional[Any] = None
        self._behavioral_logger: Optional[Any] = None
        self._resource_governor: Optional[Any] = None
        self._health_monitor: Optional[Any] = None

        # ---- 线程安全 ----
        self._lock: threading.Lock = threading.Lock()

        # ---- 历史序列 (基于时间窗口而非固定容量) ----
        self._history_timestamps: deque = deque()
        self._history_values: deque = deque()
        self._max_history_seconds: float = self.MAX_HISTORY_MINUTES * 60.0

        # ---- 进程 RSS 泄漏检测 (带时间戳) ----
        self._rss_values: deque = deque(maxlen=self.RSS_LEAK_DETECTION_WINDOW)
        self._rss_timestamps: deque = deque(maxlen=self.RSS_LEAK_DETECTION_WINDOW)

        # ---- 容器内存限制 ----
        if _PSUTIL_AVAILABLE:
            self._read_cgroup_limit()

        logger.info(
            "MemoryGuard 初始化完成 (numpy=%s, psutil=%s)，警告阈值=%.1f%%，临界阈值=%.1f%%，滑动窗口=%d",
            _NUMPY_AVAILABLE,
            _PSUTIL_AVAILABLE,
            self.WARNING_THRESHOLD_PCT,
            self.CRITICAL_THRESHOLD_PCT,
            self.SLIDING_WINDOW_SIZE,
        )

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        resource_governor: Optional[Any] = None,
        health_monitor: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选注入，未注入时对应功能降级）"""
        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, "publish_alert"):
                logger.warning("NegotiationBus 缺少 publish_alert 方法，告警推送不可用")
            else:
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")
        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        if resource_governor is not None:
            if not hasattr(resource_governor, "release_non_critical"):
                logger.warning("ResourceGovernor 缺少 release_non_critical 方法")
            else:
                self._resource_governor = resource_governor
                logger.info("ResourceGovernor 注入成功")
        if health_monitor is not None:
            self._health_monitor = health_monitor
            logger.info("HealthMonitor 注入成功")

    # ========== 公共接口 ==========
    def check_and_act(self) -> Dict[str, Any]:
        """
        检查当前内存使用率，并根据阈值触发分级保护动作。

        Returns:
            标准响应字典
        """
        if not _PSUTIL_AVAILABLE:
            return self._degraded("psutil 模块不可用，无法监控内存", "PSUTIL_NOT_AVAILABLE")

        # 定期重读 cgroup 限制
        if time.monotonic() - self._cgroup_last_read > self._cgroup_reread_interval:
            self._read_cgroup_limit()

        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        now_mono = time.monotonic()

        # 计算内存使用率 (容器感知)
        memory_pct = self._calc_memory_pct(mem)
        swap_pct = swap.percent

        # ---- 阶段一：锁内收集数据与决策 ----
        with self._lock:
            self._mem_history.append(memory_pct)
            self._swap_history.append(swap_pct)
            now_wall = time.time()
            self._history_timestamps.append(now_wall)
            self._history_values.append(memory_pct)
            self._purge_history()
            self._update_rss_history()

            smooth_mem = self._compute_smooth(list(self._mem_history))
            smooth_swap = self._compute_smooth(list(self._swap_history))

            # 阈值评估，只生成诊断结论，不执行外部调用
            diagnosis = self._evaluate_thresholds(smooth_mem, smooth_swap, now_mono)

            data = self._build_result_data(mem, memory_pct, smooth_mem, swap, swap_pct, now_mono,
                                           diagnosis["actions"])

        # ---- 阶段二：锁外执行外部调用 ----
        self._execute_diagnosis(diagnosis, smooth_mem, smooth_swap)

        action_summary = "+".join(diagnosis["actions"]) if diagnosis["actions"] else "none"
        return {
            "status": "ok",
            "reason": f"内存检查完成，平滑使用率 {smooth_mem:.1f}%，动作: {action_summary}",
            "data": data,
            "error_code": None,
            "warnings": diagnosis["warnings"],
        }

    def get_memory_status(self) -> Dict[str, Any]:
        """获取当前系统内存状态快照"""
        if not _PSUTIL_AVAILABLE:
            return self._degraded("psutil 不可用", "PSUTIL_NOT_AVAILABLE")

        try:
            mem = psutil.virtual_memory()
            swap = psutil.swap_memory()
            memory_pct = self._calc_memory_pct(mem)

            with self._lock:
                recent_mem = list(self._mem_history)[-self.TREND_PREDICT_WINDOW:]
                trend = "stable"
                if len(recent_mem) >= self.TREND_PREDICT_WINDOW:
                    xs = list(range(len(recent_mem)))
                    slope = _theil_sen_slope(xs, recent_mem)
                    if slope > self.TREND_SLOPE_THRESHOLD:
                        trend = "rising"
                    elif slope < -self.TREND_SLOPE_THRESHOLD:
                        trend = "falling"
                history_len = len(self._history_values)

            total_mem = self._get_total_mem(mem)
            used_mem = self._get_used_mem(mem, total_mem)

            return {
                "status": "ok",
                "reason": "内存状态获取成功",
                "data": {
                    "memory_total_gb": round(total_mem / (1024**3), 2),
                    "memory_available_gb": round(mem.available / (1024**3), 2),
                    "memory_used_gb": round(used_mem / (1024**3), 2),
                    "memory_pct": round(memory_pct, 1),
                    "swap_total_gb": round(swap.total / (1024**3), 2),
                    "swap_used_gb": round(swap.used / (1024**3), 2),
                    "swap_pct": round(swap.percent, 1),
                    "trend": trend,
                    "history_samples": history_len,
                },
                "error_code": None,
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"获取内存状态失败: {e} #RECOVERY: 检查 psutil 版本与系统权限")
            return self._error("MEMORY_STATUS_ERROR", str(e))

    def get_memory_history(self, minutes: int = 5) -> Dict[str, Any]:
        """获取最近 N 分钟的内存使用率历史序列"""
        minutes = max(1, min(minutes, self.MAX_HISTORY_MINUTES))
        cutoff = time.time() - minutes * 60.0

        with self._lock:
            pairs = [(t, v) for t, v in zip(self._history_timestamps, self._history_values) if t >= cutoff]
            if len(pairs) > self.MAX_HISTORY_POINTS:
                step = max(1, len(pairs) // self.MAX_HISTORY_POINTS)
                pairs = pairs[::step]
            timestamps = [p[0] for p in pairs]
            values = [p[1] for p in pairs]

        return {
            "status": "ok",
            "reason": f"返回最近 {minutes} 分钟内存历史，共 {len(values)} 个样本",
            "data": {"timestamps": timestamps, "values": values, "count": len(values)},
            "error_code": None,
            "warnings": [],
        }

    def force_cleanup(self) -> Dict[str, Any]:
        """手动触发内存清理"""
        actions: List[str] = []
        try:
            rss_before = self._get_rss_mb()
            collected = gc.collect()
            actions.append(f"gc_collected_{collected}_objects")

            with self._lock:
                self._mem_history.clear()
                self._swap_history.clear()
                self._history_timestamps.clear()
                self._history_values.clear()
                self._warning_sustained_start = 0.0
                self._critical_sustained_start = 0.0
                self._rss_values.clear()
                self._rss_timestamps.clear()
                actions.append("internal_buffers_cleared")

            if self._resource_governor is not None and hasattr(self._resource_governor, "release_non_critical"):
                try:
                    self._resource_governor.release_non_critical("manual")
                    actions.append("external_release_requested")
                except Exception as e:
                    logger.warning(f"外部资源释放失败: {e}")

            rss_after = self._get_rss_mb()
            rss_diff = rss_before - rss_after
            actions.append(f"rss_released_{rss_diff:.1f}_MB")

            logger.info("手动内存清理完成，释放 %.1f MB，动作: %s", rss_diff, ", ".join(actions))
            return {
                "status": "ok",
                "reason": f"内存清理完成，释放 {rss_diff:.1f} MB",
                "data": {
                    "actions": actions,
                    "rss_before_mb": round(rss_before, 1),
                    "rss_after_mb": round(rss_after, 1),
                    "rss_released_mb": round(rss_diff, 1),
                    "gc_collected": collected,
                },
                "error_code": None,
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"手动内存清理失败: {e} #RECOVERY: 检查 gc 模块与 resource_governor 注入状态")
            return self._error("FORCE_CLEANUP_ERROR", str(e))

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            if not hasattr(self, '_lock'):
                return self._degraded("模块未完全初始化", "HEALTH_CHECK_ERROR")

            if not _PSUTIL_AVAILABLE:
                return self._degraded("psutil 不可用", "PSUTIL_NOT_AVAILABLE")

            mem = psutil.virtual_memory()
            _ = mem.percent

            with self._lock:
                buffer_count = len(self._mem_history)

            dep_status = {
                "negotiation_bus": self._negotiation_bus is not None,
                "behavioral_logger": self._behavioral_logger is not None,
                "resource_governor": self._resource_governor is not None,
                "health_monitor": self._health_monitor is not None,
            }

            return {
                "status": "ok",
                "reason": f"MemoryGuard 正常，缓冲区样本 {buffer_count}",
                "data": {
                    "psutil_available": True,
                    "numpy_available": _NUMPY_AVAILABLE,
                    "warning_threshold_pct": self.WARNING_THRESHOLD_PCT,
                    "critical_threshold_pct": self.CRITICAL_THRESHOLD_PCT,
                    "sliding_window_size": self.SLIDING_WINDOW_SIZE,
                    "buffer_samples": buffer_count,
                    "dependencies": dep_status,
                },
                "error_code": None,
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查 psutil 安装与系统权限")
            return self._error("HEALTH_CHECK_ERROR", str(e))

    # ========== 内部方法 ==========
    def _calc_memory_pct(self, mem: Any) -> float:
        total = self._cgroup_mem_limit or mem.total
        if total <= 0:
            total = mem.total
            logger.warning("total_mem 计算异常，回退至物理总内存")
        container_usage = self._get_cached_container_usage()
        if self._cgroup_mem_limit and container_usage is not None:
            used = container_usage
        else:
            used = total - mem.available
        return (used / total) * 100.0 if total > 0 else 0.0

    def _get_total_mem(self, mem: Any) -> int:
        return self._cgroup_mem_limit or mem.total

    def _get_used_mem(self, mem: Any, total: int) -> int:
        container_usage = self._get_cached_container_usage()
        if self._cgroup_mem_limit and container_usage is not None:
            return container_usage
        return total - mem.available

    def _get_cached_container_usage(self) -> Optional[int]:
        now = time.monotonic()
        if (self._cgroup_usage_cache is not None and
                now - self._cgroup_usage_cache_time < self._cgroup_usage_cache_ttl):
            return self._cgroup_usage_cache
        usage = _get_cgroup_current_usage()
        self._cgroup_usage_cache = usage
        self._cgroup_usage_cache_time = now
        return usage

    def _compute_smooth(self, data: List[float]) -> float:
        if not data:
            return 0.0
        try:
            return _trimmed_mean(data, self.TRIMMED_MEAN_RATIO)
        except Exception:
            return _simple_moving_average(data, min(10, len(data)))

    def _evaluate_thresholds(self, smooth_mem: float, smooth_swap: float,
                             now_mono: float) -> Dict[str, Any]:
        """
        锁内评估阈值，只生成诊断结论 (warnings, actions, pending_alerts)。
        不执行任何外部调用 (网络、日志、文件)。
        """
        warnings: List[str] = []
        actions: List[str] = []
        pending_alerts: List[Tuple[str, str]] = []  # (level, message)
        pending_resource_release: Optional[str] = None
        pending_audit: Optional[float] = None
        execute_hard_kill = False

        # 1. 强制退出
        if self.HARD_KILL_THRESHOLD_PCT > 0 and smooth_mem >= self.HARD_KILL_THRESHOLD_PCT:
            actions.append("hard_kill")
            warnings.append("hard_kill_triggered")
            logger.critical("内存使用率 %.1f%% 触发强制退出阈值 %.1f%%，执行 %s",
                            smooth_mem, self.HARD_KILL_THRESHOLD_PCT, self.HARD_KILL_ACTION)
            pending_audit = smooth_mem
            execute_hard_kill = True
            return {
                "warnings": warnings,
                "actions": actions,
                "pending_alerts": pending_alerts,
                "pending_resource_release": pending_resource_release,
                "pending_audit": pending_audit,
                "execute_hard_kill": execute_hard_kill,
            }

        # 2. 临界检查
        if smooth_mem >= self.CRITICAL_THRESHOLD_PCT:
            if self._critical_sustained_start == 0.0:
                self._critical_sustained_start = now_mono
            sustained = max(0.0, now_mono - self._critical_sustained_start)
            if sustained >= self.SUSTAINED_DURATION_SEC:
                actions.append("critical_release")
                if now_mono - self._last_critical_alert >= self.ALERT_COOLDOWN_SEC:
                    self._last_critical_alert = now_mono
                    pending_alerts.append(("critical", f"内存使用率 {smooth_mem:.1f}% 持续临界 {sustained:.0f}s"))
                warnings.append("critical_memory_sustained")
                pending_resource_release = "critical"
        else:
            self._critical_sustained_start = 0.0

        # 3. 警告检查
        if smooth_mem >= self.WARNING_THRESHOLD_PCT and smooth_mem < self.CRITICAL_THRESHOLD_PCT:
            if self._warning_sustained_start == 0.0:
                self._warning_sustained_start = now_mono
            sustained = max(0.0, now_mono - self._warning_sustained_start)
            if sustained >= self.SUSTAINED_DURATION_SEC:
                actions.append("warning_throttle")
                if now_mono - self._last_warning_alert >= self.ALERT_COOLDOWN_SEC:
                    self._last_warning_alert = now_mono
                    pending_alerts.append(("warning", f"内存使用率 {smooth_mem:.1f}% 持续警告 {sustained:.0f}s"))
                warnings.append("warning_memory_sustained")
                pending_resource_release = "warning"
        else:
            self._warning_sustained_start = 0.0

        # 4. 交换空间
        if smooth_swap >= self.SWAP_CRITICAL_PCT:
            actions.append("swap_critical")
            if now_mono - self._last_swap_critical_alert >= self.SWAP_ALERT_COOLDOWN_SEC:
                self._last_swap_critical_alert = now_mono
                logger.error("交换空间使用率 %.1f%% 达到临界 #RECOVERY: 物理内存严重不足", smooth_swap)
            warnings.append("critical_swap")
        elif smooth_swap >= self.SWAP_WARNING_PCT:
            warnings.append("warning_swap")

        # 5. 进程 RSS
        if self.PROCESS_RSS_LIMIT_MB > 0:
            rss = self._get_rss_mb()
            if rss > self.PROCESS_RSS_LIMIT_MB:
                actions.append("process_rss_limit")
                warnings.append("process_rss_exceeded")

        # 6. RSS 泄漏检测
        if self._detect_rss_leak():
            actions.append("rss_leak_suspect")
            warnings.append("rss_leak_suspect")

        return {
            "warnings": warnings,
            "actions": actions,
            "pending_alerts": pending_alerts,
            "pending_resource_release": pending_resource_release,
            "pending_audit": pending_audit,
            "execute_hard_kill": execute_hard_kill,
        }

    def _execute_diagnosis(self, diagnosis: Dict[str, Any], smooth_mem: float,
                           smooth_swap: float) -> None:
        """锁外执行诊断结论中的外部动作"""
        # 硬终止
        if diagnosis.get("execute_hard_kill"):
            if diagnosis.get("pending_audit") is not None:
                self._audit_hard_kill(float(diagnosis["pending_audit"]))
            self._perform_hard_kill()
            return

        # 发送告警
        for level, msg in diagnosis.get("pending_alerts", []):
            self._async_alert(level, msg)
            if level == "critical":
                logger.error("%s #RECOVERY: 立即释放非必要缓存、暂停进化工厂与回测任务、检查内存泄漏", msg)

        # 请求资源释放
        if diagnosis.get("pending_resource_release"):
            self._request_resource_release(diagnosis["pending_resource_release"])

    def _build_result_data(self, mem: Any, raw_pct: float, smooth_mem: float,
                           swap: Any, swap_pct: float, now_mono: float,
                           actions: List[str]) -> Dict[str, Any]:
        total_mem = self._get_total_mem(mem)
        return {
            "memory_pct_raw": round(raw_pct, 1),
            "memory_pct_smooth": round(smooth_mem, 1),
            "memory_available_gb": round(mem.available / (1024**3), 2),
            "memory_total_gb": round(total_mem / (1024**3), 2),
            "swap_pct": round(swap_pct, 1),
            "swap_used_gb": round(swap.used / (1024**3), 2),
            "action": "+".join(actions) if actions else "none",
            "actions_detail": actions,
            "sustained_warning_sec": round(max(0.0, now_mono - self._warning_sustained_start), 1)
            if self._warning_sustained_start > 0 else 0.0,
            "sustained_critical_sec": round(max(0.0, now_mono - self._critical_sustained_start), 1)
            if self._critical_sustained_start > 0 else 0.0,
        }

    def _read_cgroup_limit(self) -> None:
        try:
            limit = _get_cgroup_memory_limit()
            self._cgroup_last_read = time.monotonic()
            if limit is not None and limit > 0:
                self._cgroup_mem_limit = limit
                total_mem = psutil.virtual_memory().total
                if limit < total_mem:
                    logger.info("检测到容器内存限制 %.2f GB，将基于该限制进行百分比计算", limit / (1024**3))
        except Exception as e:
            logger.warning(f"读取 cgroup 内存限制失败: {e}")

    def _get_rss_mb(self) -> float:
        if not _PSUTIL_AVAILABLE:
            return 0.0
        try:
            return psutil.Process().memory_info().rss / (1024 * 1024)
        except psutil.NoSuchProcess:
            return 0.0

    def _update_rss_history(self) -> None:
        """更新 RSS 历史记录 (需在锁内调用)"""
        rss = self._get_rss_mb()
        if rss > 0:
            self._rss_values.append(rss)
            self._rss_timestamps.append(time.monotonic())

    def _detect_rss_leak(self) -> bool:
        """基于时间戳的 RSS 斜率检测内存泄漏趋势"""
        if len(self._rss_values) < self.RSS_LEAK_DETECTION_WINDOW:
            return False
        x = list(self._rss_timestamps)
        y = list(self._rss_values)
        # 以秒为单位的斜率 (MB/s)
        slope = _theil_sen_slope(x, y)
        if slope > self.RSS_LEAK_SLOPE_THRESHOLD:
            # 辅助验证：后半段均值 > 前半段
            half = len(y) // 2
            if _py_mean(y[half:]) > _py_mean(y[:half]):
                return True
        return False

    def _perform_hard_kill(self) -> None:
        if self.HARD_KILL_ACTION == "os_exit":
            os._exit(1)
        elif self.HARD_KILL_ACTION == "signal":
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            logger.critical("HARD_KILL 已触发，系统应尽快关闭")

    def _audit_hard_kill(self, memory_pct: float) -> None:
        try:
            if self._behavioral_logger is not None:
                self._behavioral_logger.log_event(
                    event_type="hard_kill_triggered",
                    details={"memory_pct": memory_pct, "threshold": self.HARD_KILL_THRESHOLD_PCT},
                )
        except Exception:
            pass

    def _purge_history(self) -> None:
        if not self._history_timestamps:
            return
        cutoff = time.time() - self._max_history_seconds
        while self._history_timestamps and self._history_timestamps[0] < cutoff:
            self._history_timestamps.popleft()
            self._history_values.popleft()

    def _async_alert(self, level: str, message: str) -> None:
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, "publish_alert"):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="memory_guard",
                    level=level,
                    message=message,
                    timestamp=time.monotonic(),
                )
            except Exception as e:
                logger.warning(f"协商总线告警推送失败: {e}")
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="memory_alert",
                    details={"level": level, "message": message},
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

    def _request_resource_release(self, level: str) -> None:
        if self._resource_governor is not None and hasattr(self._resource_governor, "release_non_critical"):
            try:
                sig = inspect.signature(self._resource_governor.release_non_critical)
                if len(sig.parameters) >= 1:
                    self._resource_governor.release_non_critical(level)
                    logger.info("已请求 ResourceGovernor 释放非关键资源，级别: %s", level)
                else:
                    logger.warning("ResourceGovernor.release_non_critical 签名不兼容")
            except Exception as e:
                logger.warning(f"请求资源释放失败: {e}")

    @staticmethod
    def _degraded(reason: str, code: str = "PSUTIL_NOT_AVAILABLE") -> Dict[str, Any]:
        return {
            "status": "degraded",
            "reason": reason,
            "data": {"memory_pct": -1.0},
            "error_code": code,
            "warnings": [code],
        }

    @staticmethod
    def _error(code: str, msg: str) -> Dict[str, Any]:
        return {
            "status": "error",
            "reason": f"异常: {msg}",
            "data": {},
            "error_code": code,
            "warnings": [msg],
}
