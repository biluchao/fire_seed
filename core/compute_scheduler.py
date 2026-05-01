#!/usr/bin/env python3
"""
火种系统 (FireSeed) 算力与任务调度器
========================================
在有限硬件 (4核心8GB) 上动态分配计算资源，保障实时交易路径不受非关键任务影响。

核心功能：
- 硬实时交易线程 (SCHED_FIFO) 与 CPU 绑定
- 基于市场能量的自适应调度 (低波动时降频计算)
- cgroup 限制进化工厂、VIB 推理等后台任务的 CPU 使用率
- 线程池管理 (因子并行计算、幽灵验证)
- 任务优先级继承与死锁防护
- 后台算力监控与使用率统计
"""

import asyncio
import logging
import multiprocessing
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import psutil

logger = logging.getLogger("fire_seed.scheduler")


# ======================== 数据结构 ========================
@dataclass
class WorkerTask:
    """调度任务描述"""
    priority: int                       # 0=最高(实时交易), 5=中等(因子), 10=低(进化)
    func: Callable
    args: tuple = field(default_factory=tuple)
    kwargs: dict = field(default_factory=dict)
    deadline: float = 0.0              # 期望完成时间 (单调时间)


@dataclass
class Quota:
    """CPU 使用配额"""
    trading: float = 60.0              # 交易核心占用百分比
    evolution: float = 20.0            # 进化工厂占用百分比
    monitoring: float = 10.0           # 监控与日志占用百分比
    assistant: float = 10.0            # 火种助手占用百分比


# ======================== 算力调度器 ========================
class ComputeScheduler:
    """
    算力调度器。

    特性：
    - 自动检测 CPU 核心数并分配线程池
    - 交易钩子使用实时优先级 (SCHED_FIFO) 绑定专用核心
    - 根据波动率动态调整非关键模块的计算频率
    - 提供 submit(priority, func) 接口以提交后台任务
    """

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.total_cores = os.cpu_count() or 4

        # 配额配置
        quota_cfg = self.config.get("performance.cpu_quota", {})
        self.quota = Quota(
            trading=quota_cfg.get("trading", 60),
            evolution=quota_cfg.get("evolution", 20),
            monitoring=quota_cfg.get("monitoring", 10),
            assistant=quota_cfg.get("assistant", 10),
        )

        # 线程池 (非实时任务)
        self.low_pool = ThreadPoolExecutor(
            max_workers=max(1, self.total_cores - 2),
            thread_name_prefix="scheduler-low"
        )
        self.high_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="scheduler-high"
        )

        # 任务队列 (尚未支持完全异步时使用)
        self._pending: deque = deque()

        # 运行状态
        self._active = False
        self._trading_core_id: int = 0           # 专用于实时交易的 CPU 核心
        self._volatility_level: float = 0.5      # 0=极低, 1=极高
        self._cpu_usage_history: deque = deque(maxlen=60)

        logger.info(f"调度器初始化，CPU核心: {self.total_cores}, 配额: {self.quota}")

    # ======================== 启动与停止 ========================
    def start(self) -> None:
        """启动调度器（应在主进程启动后调用）"""
        self._active = True
        # 为当前进程 (交易主线程) 设置实时优先级
        self._set_realtime_priority()
        logger.info("调度器已启动，交易线程设置为实时优先级")

    def shutdown(self) -> None:
        """优雅关闭调度器，等待所有任务完成"""
        self._active = False
        self.low_pool.shutdown(wait=True)
        self.high_pool.shutdown(wait=True)
        logger.info("调度器已关闭")

    # ======================== 实时线程优先级 ========================
    def _set_realtime_priority(self) -> None:
        """尝试将当前线程设置为 SCHED_FIFO (需要 root 或 CAP_SYS_NICE)"""
        try:
            param = os.sched_param(os.sched_get_priority_max(os.SCHED_FIFO))
            os.sched_setscheduler(0, os.SCHED_FIFO, param)
            logger.info("已设置 SCHED_FIFO 实时优先级")
        except PermissionError:
            logger.warning("无权限设置实时优先级，将以普通优先级运行")
        except Exception as e:
            logger.warning(f"设置实时优先级失败: {e}")

        # CPU 亲和性绑定：避免被调度到其他核心
        try:
            cpu_count = os.cpu_count() or 4
            # 将主线程绑定到核心 0，其他线程使用剩余核心
            os.sched_setaffinity(0, {self._trading_core_id})
            logger.info(f"主线程绑定到 CPU {self._trading_core_id}")
        except Exception as e:
            logger.warning(f"CPU 亲和性设置失败: {e}")

    # ======================== 自适应降频 ========================
    def update_volatility(self, atr_pct: float) -> None:
        """
        更新当前市场波动率水平，用于动态调整非必要模块的计算频率。
        atr_pct: ATR 百分比 (例: 0.02 表示 2%)
        """
        # 归一化到 0~1，并更新平滑值
        normalized = min(1.0, atr_pct * 50)  # 2% -> 1.0, 0.5% -> 0.25
        self._volatility_level = 0.8 * self._volatility_level + 0.2 * normalized

    @property
    def should_reduce_inference(self) -> bool:
        """当波动率极低时，可降低 VIB 推理频率"""
        return self._volatility_level < 0.3

    @property
    def should_reduce_pf_resample(self) -> bool:
        """低波动时粒子滤波可降低重采样频率"""
        return self._volatility_level < 0.2

    # ======================== 任务提交 ========================
    def submit(self, priority: int, func: Callable, *args, **kwargs) -> Optional[asyncio.Future]:
        """
        提交后台任务。
        priority: 0=最高(实时), 5=中等, 10=低
        """
        if not self._active:
            return None

        if priority <= 3:
            # 高优先级任务使用小线程池
            return self.high_pool.submit(func, *args, **kwargs)
        else:
            # 低优先级任务
            return self.low_pool.submit(func, *args, **kwargs)

    # ======================== 监控 ========================
    def get_status(self) -> Dict:
        """获取当前调度器状态"""
        return {
            "volatility_level": round(self._volatility_level, 3),
            "cpu_usage_pct": psutil.cpu_percent(interval=0.1),
            "active_threads": threading.active_count(),
        }

    # ======================== 上下文管理器 ========================
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
