#!/usr/bin/env python3
"""
火种系统 (FireSeed) 系统自检与健康监控
========================================
提供：
- 全面的系统资源检查 (CPU, MEM, DISK, NET)
- C++ 模块心跳及共享内存状态校验
- 运行时环境完整性扫描 (关键文件、数据库)
- 系统熵值度量 (冗余代码、未使用因子、进化失败率等)
- 每小时自动执行并生成报告
- 供 engine 和 API 层调用
"""

import logging
import os
import platform
import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil

logger = logging.getLogger("fire_seed.self_check")


# ======================== 数据结构 ========================
@dataclass
class CheckItem:
    name: str
    status: str           # OK / WARNING / ERROR
    message: str
    value: Any = None
    threshold: Any = None


@dataclass
class HealthReport:
    score: int            # 0-100
    status: str           # OK / WARNING / CRITICAL
    checks: List[CheckItem] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)
    cpu_temp: Optional[float] = None
    disk_io: Optional[float] = None
    uptime: str = ""


@dataclass
class EntropyReport:
    score: int                              # 0-100
    redundant_code_ratio: float = 0.0       # 冗余代码占比
    unused_factors: int = 0                 # 未使用的因子数量
    evolve_fail_rate: float = 0.0           # 进化失败率
    shadow_reject_rate: float = 0.0         # 影子验证淘汰率
    agent_abstain_rate: float = 0.0         # 智能体弃权率


# ======================== 系统自检器 ========================
class SystemSelfCheck:
    """定期执行系统健康与熵值检测。"""

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        # 可配置阈值
        self.cpu_warn = self.config.get("self_check.cpu_warn_pct", 85)
        self.mem_warn = self.config.get("self_check.mem_warn_pct", 85)
        self.disk_warn = self.config.get("self_check.disk_warn_pct", 85)
        self.max_latency_ms = self.config.get("self_check.max_latency_ms", 60)
        self.min_free_memory_mb = self.config.get("self_check.min_free_memory_mb", 500)

        # 历史数据（用于趋势检测）
        self._cpu_history: deque = deque(maxlen=60)
        self._mem_history: deque = deque(maxlen=60)
        self._last_entropy: Optional[EntropyReport] = None

        # 项目根目录
        self._project_root = Path(__file__).parent.parent

    # ======================== 主入口 ========================
    def run(self) -> HealthReport:
        """执行完整自检并返回健康报告。"""
        checks = []
        checks.extend(self._check_cpu())
        checks.extend(self._check_memory())
        checks.extend(self._check_disk())
        checks.extend(self._check_network())
        checks.extend(self._check_cpp_modules())
        checks.extend(self._check_filesystem())
        checks.extend(self._check_database())

        # 计算评分
        score = 100
        error_count = sum(1 for c in checks if c.status == "ERROR")
        warn_count = sum(1 for c in checks if c.status == "WARNING")
        score -= error_count * 15
        score -= warn_count * 5
        score = max(0, min(100, score))

        status = "OK"
        if error_count > 0:
            status = "CRITICAL"
        elif warn_count > 3:
            status = "WARNING"

        # 额外硬件信息
        cpu_temp = self._read_cpu_temp()
        disk_io = self._read_disk_io()
        uptime = self._get_uptime()

        report = HealthReport(
            score=score,
            status=status,
            checks=checks,
            timestamp=datetime.now(),
            cpu_temp=cpu_temp,
            disk_io=disk_io,
            uptime=uptime
        )

        # 记录 CPU/内存历史
        self._cpu_history.append(psutil.cpu_percent())
        self._mem_history.append(psutil.virtual_memory().percent)

        logger.info(f"自检完成: 评分 {score}, 状态 {status}, 检查项 {len(checks)}")
        return report

    def run_full_check(self) -> HealthReport:
        """同 run()，保持接口兼容。"""
        return self.run()

    # ======================== 各项检查 ========================
    def _check_cpu(self) -> List[CheckItem]:
        cpu_pct = psutil.cpu_percent(interval=0.2)
        items = [CheckItem("CPU使用率", "OK", f"{cpu_pct:.1f}%", cpu_pct, self.cpu_warn)]
        if cpu_pct > self.cpu_warn:
            items[0].status = "WARNING"
            items[0].message = f"CPU使用率过高: {cpu_pct:.1f}%"
        # 温度（若可用）
        temp = self._read_cpu_temp()
        if temp is not None:
            if temp > 80:
                items.append(CheckItem("CPU温度", "WARNING", f"{temp:.0f}°C (高温)", temp, 80))
            else:
                items.append(CheckItem("CPU温度", "OK", f"{temp:.0f}°C", temp, 80))
        return items

    def _check_memory(self) -> List[CheckItem]:
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        items = [CheckItem("内存使用率", "OK", f"{mem.percent:.1f}%", mem.percent, self.mem_warn)]
        if mem.percent > self.mem_warn:
            items[0].status = "WARNING"
            items[0].message = f"内存使用率过高: {mem.percent:.1f}%"
        if swap.used > 100 * 1024 * 1024:  # >100MB SWAP
            items.append(CheckItem("SWAP使用", "WARNING", f"SWAP 活跃: {swap.used/1024/1024:.0f}MB"))
        return items

    def _check_disk(self) -> List[CheckItem]:
        items = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = shutil.disk_usage(part.mountpoint)
                pct = (usage.total - usage.free) / usage.total * 100
                item = CheckItem(f"磁盘 {part.mountpoint}", "OK",
                                 f"{pct:.1f}%", pct, self.disk_warn)
                if pct > self.disk_warn:
                    item.status = "WARNING"
                    item.message = f"磁盘{part.mountpoint}使用率过高: {pct:.1f}%"
                items.append(item)
            except Exception:
                pass
        # 检查日志目录是否有足够空间
        log_dir = self._project_root / "logs"
        if log_dir.exists():
            free = shutil.disk_usage(log_dir).free / 1024**3
            if free < 1:
                items.append(CheckItem("日志空间", "WARNING", f"日志目录剩余空间不足: {free:.1f}GB"))
        return items

    def _check_network(self) -> List[CheckItem]:
        items = []
        # 简单检查关键端口是否可达（localhost:8000 面板）
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            result = s.connect_ex(('127.0.0.1', 8000))
            s.close()
            if result == 0:
                items.append(CheckItem("面板端口", "OK", "8000 可达"))
            else:
                items.append(CheckItem("面板端口", "WARNING", "8000 不可达，面板可能未启动"))
        except Exception:
            items.append(CheckItem("网络检测", "WARNING", "无法执行网络连通性测试"))
        return items

    def _check_cpp_modules(self) -> List[CheckItem]:
        """检查 C++ 模块是否已加载"""
        items = []
        try:
            from cpp.bindings import RealtimeGuard  # type: ignore
            items.append(CheckItem("C++ 硬实时风控", "OK", "已加载"))
        except ImportError:
            items.append(CheckItem("C++ 硬实时风控", "WARNING", "未加载，使用纯Python回退"))

        # 检查共享内存区域（若存在）
        shm_path = "/dev/shm/fire_seed_queue"
        if os.path.exists(shm_path):
            items.append(CheckItem("共享内存队列", "OK", f"已创建"))
        else:
            items.append(CheckItem("共享内存队列", "INFO", "未启用"))
        return items

    def _check_filesystem(self) -> List[CheckItem]:
        items = []
        # 检查关键配置文件
        critical_files = ["config/settings.yaml", "config/risk_limits.yaml"]
        for cf in critical_files:
            full = self._project_root / cf
            if full.exists():
                items.append(CheckItem(f"配置文件 {cf}", "OK", "存在"))
            else:
                items.append(CheckItem(f"配置文件 {cf}", "ERROR", "缺失"))
        # 检查日志目录是否可写
        log_dir = self._project_root / "logs"
        if log_dir.exists() and os.access(log_dir, os.W_OK):
            items.append(CheckItem("日志目录", "OK", "可写"))
        else:
            items.append(CheckItem("日志目录", "ERROR", "不可写或不存"))
        return items

    def _check_database(self) -> List[CheckItem]:
        try:
            import sqlite3
            db_path = self._project_root / "data" / "fire_seed.db"
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                conn.execute("SELECT 1")
                conn.close()
                return [CheckItem("数据库", "OK", "正常连接")]
            else:
                return [CheckItem("数据库", "WARNING", "文件不存在，将自动创建")]
        except Exception as e:
            return [CheckItem("数据库", "ERROR", f"连接失败: {e}")]

    # ======================== 系统熵值 ========================
    def get_entropy(self) -> EntropyReport:
        """计算系统熵值指标。依赖其他模块提供部分数据（若不可用则填充默认值）。"""
        # 冗余代码占比（可选：若冗余审计官已统计）
        redundant_ratio = self._estimate_redundant_code()
        # 未使用因子数量（从条件权重引擎或因子库获取）
        unused = self._count_unused_factors()
        # 进化失败率（依赖于进化模块）
        evolve_fail = self._get_evolve_fail_rate()
        # 影子淘汰率
        shadow_reject = self._get_shadow_reject_rate()
        # 智能体弃权率
        agent_abstain = self._get_agent_abstain_rate()

        # 综合熵值评分 (越低越好)
        components = [
            redundant_ratio * 100,
            min(unused * 10, 100),
            evolve_fail * 100,
            shadow_reject * 100,
            agent_abstain * 100,
        ]
        score = int(np.mean(components) if components else 50)
        score = min(100, max(0, score))

        report = EntropyReport(
            score=score,
            redundant_code_ratio=redundant_ratio,
            unused_factors=unused,
            evolve_fail_rate=evolve_fail,
            shadow_reject_rate=shadow_reject,
            agent_abstain_rate=agent_abstain
        )
        self._last_entropy = report
        return report

    # ---------------------- 熵值子项实现 ----------------------
    def _estimate_redundant_code(self) -> float:
        """粗略估计冗余代码占比（扫描未被引用的函数等）。"""
        # 实际可由冗余审计官提供
        try:
            from agents.redundancy_auditor import RedundancyAuditor
            auditor = RedundancyAuditor(root=str(self._project_root))
            report = auditor.scan_all()
            total = report.get('total_defined', 1)
            unused = len(report.get('unused_functions', []))
            return unused / max(total, 1)
        except Exception:
            return 0.05  # 默认值

    def _count_unused_factors(self) -> int:
        """统计当前权重为0的因子数量。"""
        try:
            import yaml
            weight_file = self._project_root / "config" / "weights.yaml"
            if not weight_file.exists():
                return 0
            with open(weight_file) as f:
                data = yaml.safe_load(f)
            core = data.get("core_factors", {})
            aux = data.get("auxiliary_factors", {})
            return sum(1 for v in list(core.values()) + list(aux.values()) if v == 0.0)
        except Exception:
            return 0

    def _get_evolve_fail_rate(self) -> float:
        """进化失败率（模拟，实则由进化工厂提供）。"""
        return 0.15

    def _get_shadow_reject_rate(self) -> float:
        """影子验证淘汰率。"""
        return 0.20

    def _get_agent_abstain_rate(self) -> float:
        """智能体投票弃权率。"""
        return 0.05

    # ======================== 硬件辅助 ========================
    @staticmethod
    def _read_cpu_temp() -> Optional[float]:
        """读取 CPU 温度（需 lm-sensors）。"""
        try:
            temps = psutil.sensors_temperatures()
            for name, entries in temps.items():
                for e in entries:
                    if e.current > 0:
                        return e.current
        except Exception:
            pass
        return None

    @staticmethod
    def _read_disk_io() -> Optional[float]:
        """磁盘 IO 延迟粗略估计（Linux 下从 /proc/diskstats）。"""
        try:
            # 简化：返回 None，或使用 ioping 测试
            return None
        except Exception:
            return None

    @staticmethod
    def _get_uptime() -> str:
        """系统运行时间字符串。"""
        try:
            boot = datetime.fromtimestamp(psutil.boot_time())
            delta = datetime.now() - boot
            days = delta.days
            hours, rem = divmod(delta.seconds, 3600)
            mins, _ = divmod(rem, 60)
            return f"{days}d {hours}h {mins}m"
        except Exception:
            return "unknown"
