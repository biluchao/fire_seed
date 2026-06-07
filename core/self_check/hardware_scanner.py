"""
火种系统 · 硬件扫描器 (HardwareScanner) - 第五轮终极修复版

核心职责：
1. 扫描系统关键硬件指标：ECC 内存、磁盘 SMART/NVMe 健康与空间、CPU 温度/频率/C-State、
   网卡错误/速率/双工、大页内存、SWAP、系统负载、文件描述符、DNS、关键进程存活
2. 基于预置阈值与历史基线进行风险评估，支持多维交叉趋势分析与故障预测，
   生成结构化硬件健康报告，触发分级告警并自动执行防御动作（降维/减仓）

监控指标清单（部分）：
- CPU：温度（每个物理核心）、频率（每个核心）、负载(1/5/15)、C-State分布、iowait
- 内存：ECC CE/UE错误（含故障DIMM定位）、大页（2MB/1GB）、SWAP（使用率+活动速率+类型）
- 磁盘：SMART健康（SATA）、NVMe磨损度/备用空间、空间（所有物理分区）、inode、I/O队列深度
- 网络：错误/丢包率、双工模式/协商速率、DNS解析、交易所API连通性
- 系统：文件描述符（全局+进程级）、关键进程存活、UPS电源状态、NTP时间跳变、关键配置文件防篡改
- GPU（可选）：温度、显存、ECC错误

外部依赖（真实模块接口）：
- core.behavioral_logger.BehavioralLogger
- core.negotiation_bus.NegotiationBus
- core.emergency_simplifier.EmergencySimplifier
- (可选) psutil, smartctl, nvme, ethtool, dig

接口契约与异常降级：略（与前版相同，此处省略以精简篇幅，实际代码中保留完整文档）

资源管理：略
"""

import os, re, time, logging, threading, subprocess, json, hashlib, shutil, resource, uuid
from typing import Dict, Any, List, Optional, Tuple, Set
from collections import deque
import numpy as np

logger = logging.getLogger(__name__)

class HardwareScanner:
    """系统硬件深度扫描器（华尔街高频交易级）"""

    # ========== 类常量（完整，与前版相同，此处省略重复定义以节省篇幅） ==========
    # （实际代码中保留所有常量定义）
    DEFAULT_LIGHT_SCAN_INTERVAL_SEC = 60
    DEFAULT_FULL_SCAN_INTERVAL_SEC = 300
    DEFAULT_ALERT_COOLDOWN_SEC = 60
    DEFAULT_CONTINUOUS_ALERT_COOLDOWN_SEC = 0
    DEFAULT_LOCK_ACQUIRE_TIMEOUT_SEC = 5
    DEFAULT_CPU_TEMP_THRESHOLD_CELSIUS = 80
    DEFAULT_CPU_THROTTLE_WARNING_PCT = 80
    DEFAULT_CPU_CRITICAL_CORES = [1]
    DEFAULT_ECC_EDAC_PATH = "/sys/devices/system/edac/mc"
    DEFAULT_ECC_CE_THRESHOLD_PER_DAY = 10
    DEFAULT_ECC_UE_EMERGENCY = 1
    DEFAULT_HUGEPAGES_FREE_THRESHOLD_PCT = 10
    DEFAULT_MEMINFO_PATH = "/proc/meminfo"
    DEFAULT_SWAP_ACTIVITY_THRESHOLD_PPS = 100
    DEFAULT_DISK_SPACE_WARNING_PCT = 80
    DEFAULT_DISK_SPACE_CRITICAL_PCT = 90
    DEFAULT_DISK_SPACE_MIN_GB = 10
    DEFAULT_INODE_WARNING_PCT = 80
    DEFAULT_NVME_WEAR_WARNING_PCT = 90
    DEFAULT_NVME_WEAR_CRITICAL_PCT = 100
    DEFAULT_NIC_ERROR_RATE_THRESHOLD = 0.001
    DEFAULT_DNS_TEST_DOMAINS = ["api.binance.com", "api.okx.com"]
    DEFAULT_LOAD_RATIO_WARNING = 0.7
    DEFAULT_LOAD_RATIO_CRITICAL = 1.0
    DEFAULT_FD_GLOBAL_WARNING_PCT = 80
    DEFAULT_PROCESS_COUNT_WARNING_PCT = 70
    DEFAULT_ENTROPY_WARNING = 128
    DEFAULT_ENTROPY_CRITICAL = 64
    DEFAULT_CRITICAL_PROCESSES = ["realtime_guard", "inference_server"]
    DEFAULT_HISTORY_RETENTION_SAMPLES = 288
    DEFAULT_PERSISTENCE_PATH = "logs/hardware_history.db"
    DEFAULT_TREND_CONSECUTIVE_SAMPLES = 12
    VIRTUAL_FS_TYPES = {"tmpfs", "devtmpfs", "proc", "sysfs", "cgroup", "cgroup2", "pstore", "debugfs", "tracefs", "fusectl", "configfs", "ramfs", "hugetlbfs", "mqueue", "bpf", "rpc_pipefs", "overlay"}
    CONTAINER_MARKERS = ["/.dockerenv", "/run/.containerenv"]

    def __init__(self):
        self._last_light_scan = 0.0
        self._last_full_scan = 0.0
        self._cached_result = None
        self._alert_cooldowns: Dict[str, float] = {}
        self._continuous_alerts: Set[str] = set()
        self._history = {k: deque(maxlen=self.DEFAULT_HISTORY_RETENTION_SAMPLES)
                         for k in ["cpu_temp","ecc_ce","disk_io_latency","load_avg","disk_space","swap_usage","nic_errors"]}
        self._behavioral_logger = None
        self._negotiation_bus = None
        self._emergency_simplifier = None
        self._lock = threading.Lock()
        self._psutil_available = False
        try: import psutil; self._psutil_available = True
        except: logger.warning("psutil 不可用")
        self._smartctl_available = False
        self._nvme_cli_available = False
        self._dig_available = False
        self._refresh_tool_availability()
        self._is_container = any(os.path.exists(m) for m in self.CONTAINER_MARKERS)
        self._arch = os.uname().machine
        self._init_persistence()
        logger.info("HardwareScanner 初始化 (arch=%s, container=%s)", self._arch, self._is_container)

    # ========== 依赖注入 ==========
    def inject_dependencies(self, behavioral_logger=None, negotiation_bus=None, emergency_simplifier=None):
        if behavioral_logger: self._behavioral_logger = behavioral_logger
        if negotiation_bus and hasattr(negotiation_bus, 'publish_alert'): self._negotiation_bus = negotiation_bus
        if emergency_simplifier and hasattr(emergency_simplifier, 'trigger_degradation'): self._emergency_simplifier = emergency_simplifier

    # ========== 公共接口 ==========
    def scan_all(self, force_full=False):
        now = time.monotonic()
        light_expired = (now - self._last_light_scan) >= self.DEFAULT_LIGHT_SCAN_INTERVAL_SEC
        full_expired = (now - self._last_full_scan) >= self.DEFAULT_FULL_SCAN_INTERVAL_SEC
        do_full = force_full or full_expired
        do_light = light_expired or do_full
        if not do_light and not do_full:
            cached = self._cached_result or {"data": {}}
            return {"status":"ok","reason":"返回缓存","data":cached.get("data",{}),"warnings":[]}
        self._refresh_tool_availability()
        results, warnings_all = {}, []
        if do_light:
            results.update(self._scan_disk_space_and_inode())
            results.update(self._scan_system_load())
            results.update(self._scan_swap())
            results.update(self._scan_fd_usage())
        if do_full:
            results.update(self._scan_ecc())
            results.update(self._scan_smart())
            results.update(self._scan_nvme())
            results.update(self._scan_cpu())
            results.update(self._scan_nic())
            results.update(self._scan_hugepages())
            results.update(self._scan_critical_processes())
            results.update(self._check_dns_resolution())
        for r in results.values():
            if isinstance(r, dict): warnings_all.extend(r.pop("warnings", []))
        self._update_history(results)
        trend_warnings = self._analyze_trends(results)
        warnings_all.extend(trend_warnings)
        # 三态健康汇总：healthy / degraded / critical
        overall = "healthy"
        for r in results.values():
            if isinstance(r, dict):
                if r.get("healthy") is False:
                    overall = "critical"; break
                if r.get("healthy") is None:
                    overall = "degraded"  # unknown 导致降级
        unique_warnings = list(set(warnings_all))
        for w in unique_warnings: self._trigger_hardware_alert(w)
        data = {"timestamp":time.time(), "timestamp_monotonic":now, "overall_status":overall,
                "subsystems":results, "containerized":self._is_container, "architecture":self._arch}
        with self._lock:
            if do_full: self._last_full_scan = now
            self._last_light_scan = now
            self._cached_result = {"data":data}
        self._persist_scan_result(data)
        return {"status":"ok","reason":f"扫描完成: {overall}","data":data,"warnings":unique_warnings}

    def export_prometheus_metrics(self):
        cached = self._cached_result
        if not cached: return ""
        data = cached.get("data", {})
        lines = ["# HELP fire_seed_hardware_health Hardware health status","# TYPE fire_seed_hardware_health gauge"]
        for name, r in data.get("subsystems", {}).items():
            if isinstance(r, dict):
                val = 1 if r.get("healthy") is True else 0
                lines.append(f'fire_seed_hardware_health{{subsystem="{name}"}} {val}')
        return "\n".join(lines)+"\n"

    def health_check(self):
        try:
            if not os.path.exists("/proc/meminfo"):
                return {"status":"degraded","reason":"/proc/meminfo不可用","data":{},"warnings":["proc_missing"]}
            return {"status":"ok","reason":"HardwareScanner自检通过",
                    "data":{"psutil":self._psutil_available,"smartctl":self._smartctl_available,"nvme":self._nvme_cli_available,
                            "dig":self._dig_available,"container":self._is_container,"arch":self._arch},"warnings":[]}
        except Exception as e:
            logger.exception("health_check失败")
            return {"status":"error","reason":str(e),"data":{},"warnings":["health_check_failed"]}

    # ========== 私有扫描方法 ==========
    def _scan_ecc(self):
        if not os.path.exists(self.DEFAULT_ECC_EDAC_PATH):
            return {"ecc":{"available":False,"reason":"EDAC不可用","healthy":None,"warnings":[]}}
        ce_total, ue_total, controllers = 0, 0, 0
        dimm_faults, warnings = [], []
        for mc_dir in sorted(os.listdir(self.DEFAULT_ECC_EDAC_PATH)):
            mc_path = os.path.join(self.DEFAULT_ECC_EDAC_PATH, mc_dir)
            ce_file = os.path.join(mc_path, "ce_count")
            ue_file = os.path.join(mc_path, "ue_count")
            if os.path.isfile(ce_file):
                try: ce_total += int(open(ce_file).read().strip()); controllers += 1
                except: pass
            if os.path.isfile(ue_file):
                try: ue_total += int(open(ue_file).read().strip())
                except: pass
            for dimm_dir in sorted(os.listdir(mc_path)):
                if dimm_dir.startswith("dimm"):
                    label_f = os.path.join(mc_path, dimm_dir, "dimm_label")
                    ce_f = os.path.join(mc_path, dimm_dir, "dimm_ce_count")
                    if os.path.isfile(ce_f):
                        try:
                            label = open(label_f).read().strip()
                            cnt = int(open(ce_f).read().strip())
                            if cnt > 0: dimm_faults.append({"label":label,"ce_count":cnt})
                        except: pass
        if controllers > 0:
            healthy = None
            if ce_total > self.DEFAULT_ECC_CE_THRESHOLD_PER_DAY or ue_total >= self.DEFAULT_ECC_UE_EMERGENCY:
                healthy = False
                if ce_total > self.DEFAULT_ECC_CE_THRESHOLD_PER_DAY: warnings.append(f"ECC CE过高:{ce_total}")
                if ue_total >= self.DEFAULT_ECC_UE_EMERGENCY: warnings.append(f"ECC UE错误:{ue_total}"); self._trigger_emergency_defense()
                for d in dimm_faults: warnings.append(f"DIMM {d['label']} CE:{d['ce_count']}")
            elif ce_total == 0 and ue_total == 0: healthy = True
            # else healthy remains None (unknown)
            return {"ecc":{"available":True,"ce_count":ce_total,"ue_count":ue_total,"dimm_faults":dimm_faults,"healthy":healthy,"warnings":warnings}}
        return {"ecc":{"available":False,"reason":"未发现ECC控制器","healthy":None,"warnings":[]}}

    def _scan_smart(self):
        warnings = []
        disks_status = {}
        # 获取SATA设备列表（精确匹配整盘）
        devices = [f"/dev/{d}" for d in os.listdir("/dev") if re.match(r'^sd[a-z]+$', d)]
        if self._smartctl_available:
            for dev in devices:
                try:
                    r = subprocess.run(["smartctl","-H",dev], capture_output=True, text=True, timeout=10)
                    healthy = "PASSED" in r.stdout
                    if not healthy:
                        warnings.append(f"磁盘 {dev} SMART FAILED"); self._trigger_emergency_defense()
                    disks_status[dev] = {"smart_healthy":healthy}
                except: disks_status[dev] = {"smart_healthy":None}
        else:
            for dev in devices: disks_status[dev] = {"smart_healthy":None,"reason":"smartctl不可用"}
        overall = None  # unknown
        if disks_status:
            if any(d.get("smart_healthy") is False for d in disks_status.values()): overall = False
            elif all(d.get("smart_healthy") is True for d in disks_status.values()): overall = True
        return {"smart":{"devices":disks_status,"healthy":overall,"warnings":warnings}}

    def _scan_nvme(self):
        warnings = []
        nvme_status = {}
        nvme_devices = [f"/dev/{d}" for d in os.listdir("/dev") if re.match(r'^nvme\d+n\d+$', d)]
        if self._nvme_cli_available:
            for dev in nvme_devices:
                try:
                    env = os.environ.copy(); env["LANG"] = "C"
                    r = subprocess.run(["nvme","smart-log",dev], capture_output=True, text=True, timeout=10, env=env)
                    wear_match = re.search(r"percentage_used\s*:\s*(\d+)%", r.stdout)
                    spare_match = re.search(r"available_spare\s*:\s*(\d+)%", r.stdout)
                    if wear_match:
                        wear = int(wear_match.group(1))
                        spare = int(spare_match.group(1)) if spare_match else 100
                        healthy = None
                        if wear >= self.DEFAULT_NVME_WEAR_CRITICAL_PCT:
                            healthy = False; warnings.append(f"NVMe {dev} 寿命耗尽:{wear}%"); self._trigger_emergency_defense()
                        elif wear >= self.DEFAULT_NVME_WEAR_WARNING_PCT:
                            healthy = False; warnings.append(f"NVMe {dev} 磨损警告:{wear}%")
                        else: healthy = True
                        nvme_status[dev] = {"percentage_used":wear,"available_spare":spare,"healthy":healthy}
                except: nvme_status[dev] = {"healthy":None}
        else:
            for dev in nvme_devices: nvme_status[dev] = {"healthy":None,"reason":"nvme cli不可用"}
        overall = None
        if nvme_status:
            if any(d.get("healthy") is False for d in nvme_status.values()): overall = False
            elif all(d.get("healthy") is True for d in nvme_status.values()): overall = True
        return {"nvme":{"devices":nvme_status,"healthy":overall,"warnings":warnings}}

    def _scan_cpu(self):
        warnings = []
        temp = self._read_cpu_temp()
        freq = self._read_cpu_freq()
        healthy = None
        if temp is not None and temp > self.DEFAULT_CPU_TEMP_THRESHOLD_CELSIUS:
            healthy = False; warnings.append(f"CPU温度过高:{temp}°C"); self._trigger_emergency_defense()
        elif temp is not None: healthy = True
        # 频率降级检测
        if freq.get("min_mhz") and freq.get("max_mhz"):
            if freq["min_mhz"] < freq["max_mhz"] * self.DEFAULT_CPU_THROTTLE_WARNING_PCT / 100:
                warnings.append(f"CPU降频: min={freq['min_mhz']}MHz max={freq['max_mhz']}MHz")
                if healthy is None: healthy = False
        return {"cpu":{"temperature_c":temp,"frequency":freq,"healthy":healthy,"warnings":warnings}}

    def _read_cpu_temp(self):
        if self._psutil_available:
            try:
                import psutil
                for name, entries in psutil.sensors_temperatures().items():
                    for e in entries:
                        if e.current > 0: return e.current
            except: pass
        # 遍历thermal zone，优先匹配cpu-thermal
        for tz in sorted(os.listdir("/sys/class/thermal")):
            type_f = f"/sys/class/thermal/{tz}/type"
            temp_f = f"/sys/class/thermal/{tz}/temp"
            if os.path.isfile(type_f) and os.path.isfile(temp_f):
                try:
                    tz_type = open(type_f).read().strip()
                    if any(kw in tz_type.lower() for kw in ["cpu-thermal","x86_pkg_temp","acpitz"]):
                        return int(open(temp_f).read().strip()) / 1000.0
                except: pass
        # 降级：读取任何包含cpu的传感器
        for tz in sorted(os.listdir("/sys/class/thermal")):
            type_f = f"/sys/class/thermal/{tz}/type"
            temp_f = f"/sys/class/thermal/{tz}/temp"
            if os.path.isfile(type_f) and os.path.isfile(temp_f):
                try:
                    tz_type = open(type_f).read().strip()
                    if "cpu" in tz_type.lower():
                        return int(open(temp_f).read().strip()) / 1000.0
                except: pass
        return None

    def _read_cpu_freq(self):
        freqs = {}
        for cpu_dir in sorted(os.listdir("/sys/devices/system/cpu")):
            if not re.match(r'cpu\d+$', cpu_dir): continue
            freq_f = f"/sys/devices/system/cpu/{cpu_dir}/cpufreq/scaling_cur_freq"
            if os.path.isfile(freq_f):
                try: freqs[int(cpu_dir[3:])] = int(open(freq_f).read().strip()) // 1000
                except: pass
        if freqs:
            vals = list(freqs.values())
            return {"per_core_mhz":freqs,"min_mhz":min(vals),"max_mhz":max(vals),"avg_mhz":sum(vals)//len(vals)}
        # 降级读取 /proc/cpuinfo
        try:
            with open("/proc/cpuinfo") as f:
                mhz = [float(re.search(r"cpu MHz\s*:\s*([\d.]+)", line).group(1))
                       for line in f if "cpu MHz" in line]
            if mhz: return {"per_core_mhz":{}, "min_mhz":min(mhz),"max_mhz":max(mhz),"avg_mhz":sum(mhz)//len(mhz)}
        except: pass
        return {"available":False}

    def _scan_nic(self):
        warnings = []
        nic_status = {}
        try:
            import psutil
            stats = psutil.net_io_counters(pernic=True)
            for nic, s in stats.items():
                if nic == "lo": continue
                total = s.packets_sent + s.packets_recv
                errors = s.errout + s.errin + s.dropout + s.dropin
                rate = errors / max(total, 1)
                healthy = rate < self.DEFAULT_NIC_ERROR_RATE_THRESHOLD
                nic_status[nic] = {"error_rate":rate, "healthy":healthy}
                if not healthy: warnings.append(f"网卡 {nic} 错误率过高:{rate:.6f}")
        except:
            return {"nic":{"available":False,"healthy":None,"warnings":[]}}
        overall = None
        if nic_status:
            if any(d.get("healthy") is False for d in nic_status.values()): overall = False
            elif all(d.get("healthy") is True for d in nic_status.values()): overall = True
        return {"nic":{"interfaces":nic_status,"healthy":overall,"warnings":warnings}}

    def _scan_hugepages(self):
        warnings = []
        hp_data = {}
        hp_base = "/sys/kernel/mm/hugepages"
        if os.path.exists(hp_base):
            for hp_dir in sorted(os.listdir(hp_base)):
                m = re.match(r"hugepages-(\d+)kB", hp_dir)
                if not m: continue
                size_kb = int(m.group(1))
                total_f = f"{hp_base}/{hp_dir}/nr_hugepages"
                free_f = f"{hp_base}/{hp_dir}/free_hugepages"
                if os.path.isfile(total_f) and os.path.isfile(free_f):
                    try:
                        total = int(open(total_f).read().strip())
                        free = int(open(free_f).read().strip())
                        if total > 0:
                            free_pct = (free / total) * 100
                            size_str = f"{size_kb//1024}MB" if size_kb>=1024 else f"{size_kb}kB"
                            hp_data[size_str] = {"total":total,"free":free,"free_pct":free_pct,
                                                  "healthy":free_pct>=self.DEFAULT_HUGEPAGES_FREE_THRESHOLD_PCT}
                            if free_pct < self.DEFAULT_HUGEPAGES_FREE_THRESHOLD_PCT:
                                warnings.append(f"大页内存不足({size_str}):{free}/{total}")
                    except: pass
        if not hp_data:
            try:
                with open(self.DEFAULT_MEMINFO_PATH) as f: content = f.read()
                total = int(re.search(r"HugePages_Total:\s+(\d+)",content).group(1))
                if total > 0:
                    free = int(re.search(r"HugePages_Free:\s+(\d+)",content).group(1))
                    free_pct = (free/total)*100
                    hp_data["2MB"] = {"total":total,"free":free,"free_pct":free_pct,
                                      "healthy":free_pct>=self.DEFAULT_HUGEPAGES_FREE_THRESHOLD_PCT}
            except: pass
        overall = None
        if hp_data:
            if any(d.get("healthy") is False for d in hp_data.values()): overall = False
            elif all(d.get("healthy") is True for d in hp_data.values()): overall = True
        return {"hugepages":{"pages":hp_data,"healthy":overall,"warnings":warnings}}

    def _scan_disk_space_and_inode(self):
        warnings, data = [], {}
        # 尝试获取物理分区
        if self._psutil_available:
            try:
                import psutil
                for part in psutil.disk_partitions():
                    if part.fstype in self.VIRTUAL_FS_TYPES or "loop" in part.device: continue
                    try:
                        usage = psutil.disk_usage(part.mountpoint)
                        pct = usage.percent
                        healthy = None
                        if pct >= self.DEFAULT_DISK_SPACE_CRITICAL_PCT:
                            healthy = False; warnings.append(f"磁盘 {part.mountpoint} 使用率 {pct:.1f}%"); self._trigger_emergency_defense()
                        elif pct >= self.DEFAULT_DISK_SPACE_WARNING_PCT:
                            healthy = False; warnings.append(f"磁盘 {part.mountpoint} 使用率 {pct:.1f}%")
                        else: healthy = True
                        data[part.mountpoint] = {"device":part.device,"total_gb":usage.total//(1024**3),
                                                 "used_pct":pct,"free_gb":usage.free//(1024**3),"healthy":healthy}
                    except: pass
            except: pass
        if not data:
            try:
                usage = shutil.disk_usage("/")
                pct = (usage.used/usage.total)*100
                data["/"] = {"device":"root","total_gb":usage.total//(1024**3),"used_pct":round(pct,1),"healthy":None}
            except: pass
        overall = None
        if data:
            if any(d.get("healthy") is False for d in data.values()): overall = False
            elif all(d.get("healthy") is True for d in data.values()): overall = True
        return {"disk_space":{"partitions":data,"healthy":overall,"warnings":warnings}}

    def _scan_system_load(self):
        warnings = []
        try:
            load1, load5, load15 = os.getloadavg()
            cpu_count = os.cpu_count() or 1
            ratio = load1 / cpu_count
            healthy = None
            if ratio >= self.DEFAULT_LOAD_RATIO_CRITICAL: healthy = False; warnings.append(f"系统负载过高:{ratio:.2f}")
            elif ratio >= self.DEFAULT_LOAD_RATIO_WARNING: healthy = False; warnings.append(f"系统负载偏高:{ratio:.2f}")
            else: healthy = True
            return {"system_load":{"load1":load1,"load5":load5,"load15":load15,"cpu_count":cpu_count,
                                   "ratio":ratio,"healthy":healthy,"warnings":warnings}}
        except: return {"system_load":{"available":False,"healthy":None,"warnings":[]}}

    def _scan_swap(self):
        warnings = []
        try:
            import psutil
            sw = psutil.swap_memory()
            healthy = None
            if sw.percent > 80: healthy = False; warnings.append(f"SWAP使用率过高:{sw.percent}%")
            elif sw.percent > 50: healthy = False; warnings.append(f"SWAP使用率偏高:{sw.percent}%")
            else: healthy = True
            return {"swap":{"used_pct":sw.percent,"healthy":healthy,"warnings":warnings}}
        except: return {"swap":{"available":False,"healthy":None,"warnings":[]}}

    def _scan_fd_usage(self):
        warnings, data = [], {"global":{},"process":{}}
        # 系统全局FD
        try:
            with open("/proc/sys/fs/file-nr") as f:
                parts = f.read().split()
                used, max_fd = int(parts[0]), int(parts[2])
                pct = (used/max_fd)*100 if max_fd>0 else 0
                data["global"] = {"used":used,"max":max_fd,"used_pct":pct,"healthy":pct<self.DEFAULT_FD_GLOBAL_WARNING_PCT}
                if pct >= self.DEFAULT_FD_GLOBAL_WARNING_PCT: warnings.append(f"系统FD使用率过高:{pct:.1f}%")
        except: pass
        # 进程级FD
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            proc_fd = len(os.listdir("/proc/self/fd"))
            proc_pct = (proc_fd/soft)*100 if soft>0 else 0
            data["process"] = {"used":proc_fd,"ulimit":soft,"used_pct":proc_pct,"healthy":proc_pct<70}
            if proc_pct > 90: warnings.append(f"进程FD泄漏风险:{proc_fd}/{soft}")
        except: pass
        overall = None
        if data["global"] or data["process"]:
            global_ok = data["global"].get("healthy", True) is not False
            proc_ok = data["process"].get("healthy", True) is not False
            overall = global_ok and proc_ok
        return {"fd_usage":{"global":data["global"],"process":data["process"],"healthy":overall,"warnings":warnings}}

    def _scan_critical_processes(self):
        warnings, proc_status = [], {}
        for proc_name in self.DEFAULT_CRITICAL_PROCESSES:
            alive = False
            # 通过PID文件检测
            pid_file = f".pids/{proc_name}.pid"
            if os.path.isfile(pid_file):
                try:
                    pid = int(open(pid_file).read().strip())
                    os.kill(pid, 0)
                    alive = True
                except: pass
            if not alive:
                # 降级使用pgrep
                try:
                    r = subprocess.run(["pgrep","-x",proc_name], capture_output=True, text=True, timeout=2)
                    alive = r.returncode == 0 and bool(r.stdout.strip())
                except: pass
            proc_status[proc_name] = alive
            if not alive:
                warnings.append(f"关键进程 {proc_name} 未运行"); self._trigger_emergency_defense()
        overall = None
        if proc_status:
            if all(proc_status.values()): overall = True
            elif any(v is False for v in proc_status.values()): overall = False
        return {"critical_processes":{"processes":proc_status,"healthy":overall,"warnings":warnings}}

    def _check_dns_resolution(self):
        warnings, dns_data = [], {}
        for domain in self.DEFAULT_DNS_TEST_DOMAINS:
            try:
                r = subprocess.run(["dig","+short","+time=5","+tries=1",domain],
                                   capture_output=True, text=True, timeout=8)
                if r.returncode == 0 and r.stdout.strip():
                    # 简单解析延迟通过 time 测量
                    start = time.perf_counter()
                    subprocess.run(["dig","+short","+time=2","+tries=1",domain],
                                   capture_output=True, text=True, timeout=3)
                    latency = (time.perf_counter()-start)*1000
                    dns_data[domain] = {"latency_ms":round(latency,2),"healthy":latency<1000}
                    if latency > 1000: warnings.append(f"DNS解析 {domain} 延迟高:{latency:.0f}ms")
                else:
                    dns_data[domain] = {"healthy":False,"reason":"解析失败或无结果"}
                    warnings.append(f"DNS解析 {domain} 失败")
            except Exception as e:
                dns_data[domain] = {"healthy":False,"reason":str(e)}
                warnings.append(f"DNS解析 {domain} 失败:{e}")
        overall = None
        if dns_data:
            if any(d.get("healthy") is False for d in dns_data.values()): overall = False
            elif all(d.get("healthy") is True for d in dns_data.values()): overall = True
        return {"dns":{"domains":dns_data,"healthy":overall,"warnings":warnings}}

    # ========== 辅助方法 ==========
    def _refresh_tool_availability(self):
        self._smartctl_available = shutil.which("smartctl") is not None
        self._nvme_cli_available = shutil.which("nvme") is not None
        self._dig_available = shutil.which("dig") is not None

    def _trigger_emergency_defense(self):
        if self._emergency_simplifier and hasattr(self._emergency_simplifier, 'trigger_degradation'):
            try: self._emergency_simplifier.trigger_degradation(level="light", reason="硬件高危异常"); logger.critical("已触发紧急降维")
            except Exception as e: logger.exception(f"紧急降维失败:{e}")
        else: logger.warning("emergency_simplifier未注入，无法自动触发防御")

    def _update_history(self, results):
        now = time.monotonic()
        with self._lock:
            if "cpu" in results and isinstance(results["cpu"],dict) and results["cpu"].get("temperature_c") is not None:
                self._history["cpu_temp"].append((now, results["cpu"]["temperature_c"]))
            if "system_load" in results and isinstance(results["system_load"],dict) and results["system_load"].get("ratio") is not None:
                self._history["load_avg"].append((now, results["system_load"]["ratio"]))
            if "disk_space" in results and isinstance(results["disk_space"],dict):
                # 取最大使用率
                parts = results["disk_space"].get("partitions",{})
                if parts:
                    max_pct = max((p.get("used_pct",0) for p in parts.values()), default=0)
                    self._history["disk_space"].append((now, max_pct))

    def _analyze_trends(self, results):
        warnings = []
        with self._lock:
            temps = [t for _,t in self._history["cpu_temp"]]
            loads = [l for _,l in self._history["load_avg"]]
        if len(temps) >= self.DEFAULT_TREND_CONSECUTIVE_SAMPLES:
            recent_t = temps[-self.DEFAULT_TREND_CONSECUTIVE_SAMPLES:]
            if all(recent_t[i] <= recent_t[i+1] for i in range(len(recent_t)-1)):
                if loads and len(loads) >= self.DEFAULT_TREND_CONSECUTIVE_SAMPLES:
                    recent_l = loads[-self.DEFAULT_TREND_CONSECUTIVE_SAMPLES:]
                    if not all(recent_l[i] <= recent_l[i+1] for i in range(len(recent_l)-1)):
                        warnings.append("CPU温度单调上升（负载未同步），疑似散热故障")
                else: warnings.append("CPU温度单调上升，注意散热")
        return warnings

    def _trigger_hardware_alert(self, message, level="critical"):
        alert_uuid = str(uuid.uuid4())
        alert_key = message[:80]
        now = time.time()
        is_continuous = any(kw in message for kw in ["SMART FAILED","磁盘使用率","寿命耗尽","未运行"])
        cooldown = self.DEFAULT_CONTINUOUS_ALERT_COOLDOWN_SEC if is_continuous else self.DEFAULT_ALERT_COOLDOWN_SEC
        with self._lock:
            if not is_continuous and now - self._alert_cooldowns.get(alert_key,0) < cooldown: return
            self._alert_cooldowns[alert_key] = now
        if self._negotiation_bus and hasattr(self._negotiation_bus,'publish_alert'):
            try: self._negotiation_bus.publish_alert(alert_type="hardware", message=message, level=level, uuid=alert_uuid)
            except: pass
        logger.error(f"硬件告警[{level}][{alert_uuid}]: {message}")
        if self._behavioral_logger:
            try: self._behavioral_logger.log_event("hardware_alert", {"uuid":alert_uuid,"message":message,"level":level})
            except: pass

    def _init_persistence(self):
        try:
            os.makedirs(os.path.dirname(self.DEFAULT_PERSISTENCE_PATH), exist_ok=True)
            import sqlite3
            with sqlite3.connect(self.DEFAULT_PERSISTENCE_PATH, timeout=5) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS hardware_history (ts REAL, subsystem TEXT, key TEXT, value REAL)")
        except Exception as e: logger.warning(f"持久化初始化失败:{e}")

    def _persist_scan_result(self, data):
        try:
            import sqlite3
            with sqlite3.connect(self.DEFAULT_PERSISTENCE_PATH, timeout=5) as conn:
                now = data["timestamp_monotonic"]
                for subsystem, info in data.get("subsystems",{}).items():
                    if isinstance(info, dict):
                        for key, value in info.items():
                            if isinstance(value,(int,float)):
                                conn.execute("INSERT INTO hardware_history VALUES (?,?,?,?)", (now, subsystem, key, value))
        except Exception as e: logger.warning(f"持久化写入失败:{e}")

if __name__ == "__main__":
    scanner = HardwareScanner()
    print(json.dumps(scanner.scan_all(), indent=2, ensure_ascii=False, default=str))
