#!/usr/bin/env python3
"""
火种系统 (FireSeed) OTA 更新后健康检查模块
==============================================
在 OTA 热替换完成后，对新版本进行综合健康检测，
包括：
- 进程存活与基础响应
- 关键 API 可用性
- 短期策略绩效（滚动夏普）是否显著恶化
- 信号分歧度（若启用镜像）
- 磁盘、内存等资源快速检查
"""

import asyncio
import logging
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import psutil
import numpy as np

logger = logging.getLogger("fire_seed.health_check")


class OTAHealthCheck:
    """
    OTA 后的综合健康检查器。
    主要在 OTA 替换后被调用，也支持系统日常巡检调用。
    """

    def __init__(self, config):
        self.config = config
        # 面板端口
        self.api_port = config.get("frontend.port", 8000)
        self.api_base = f"http://127.0.0.1:{self.api_port}"
        # 健康检查超时
        self.timeout = config.get("health_check.timeout_sec", 10)
        # 短期夏普观察窗口（分钟）
        self.sharpe_window_minutes = config.get("health_check.sharpe_window_minutes", 30)
        # 夏普恶化阈值（相对旧版的最大下降比例，如 0.5 表示下降超过50%则失败）
        self.sharpe_decline_threshold = config.get("health_check.sharpe_decline_threshold", 0.5)
        # 信号分歧阈值
        self.divergence_threshold = config.get("health_check.divergence_threshold", 0.4)
        # 最小需要的数据点数
        self.min_data_points = config.get("health_check.min_data_points", 5)

    # ====================== 主入口 ======================
    async def run_full_check(self, old_version_sharpe: Optional[float] = None) -> Dict[str, Any]:
        """
        执行完整的健康检查。
        :param old_version_sharpe: 旧版本的近期夏普值，用于对比。
        :return: 字典，包含 passed (bool), checks (list), overall_health (str)
        """
        checks = []
        overall_passed = True

        # 1. 基础连通性检查
        connectivity_ok = await self._check_connectivity()
        checks.append({
            "name": "API连通性",
            "passed": connectivity_ok,
            "message": "面板端口可达" if connectivity_ok else f"端口 {self.api_port} 不可达"
        })
        if not connectivity_ok:
            overall_passed = False

        # 2. 系统资源快照
        resource_ok, resource_msg = self._check_resources()
        checks.append({
            "name": "系统资源",
            "passed": resource_ok,
            "message": resource_msg
        })

        # 3. 关键服务进程
        service_ok, service_msg = self._check_services()
        checks.append({
            "name": "关键服务",
            "passed": service_ok,
            "message": service_msg
        })

        # 4. 策略绩效（对比夏普）
        sharpe_ok, sharpe_msg = await self._check_sharpe(old_version_sharpe)
        checks.append({
            "name": "策略绩效(夏普)",
            "passed": sharpe_ok,
            "message": sharpe_msg
        })
        if not sharpe_ok:
            overall_passed = False

        # 5. 信号分歧度（若镜像模块提供）
        divergence_ok, divergence_msg = await self._check_signal_divergence()
        checks.append({
            "name": "信号分歧度",
            "passed": divergence_ok,
            "message": divergence_msg
        })
        if not divergence_ok:
            overall_passed = False

        result = {
            "passed": overall_passed,
            "checks": checks,
            "overall_health": "OK" if overall_passed else "FAIL",
            "timestamp": datetime.now().isoformat()
        }
        if not overall_passed:
            logger.error(f"健康检查未通过: {[c['message'] for c in checks if not c['passed']]}")
        else:
            logger.info("健康检查全部通过")
        return result

    # ====================== 子检查实现 ======================
    async def _check_connectivity(self) -> bool:
        """检查面板端口是否可访问"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.api_base}/health", timeout=5) as resp:
                    if resp.status == 200:
                        return True
        except Exception as e:
            logger.warning(f"API 连通性检查失败: {e}")
        return False

    def _check_resources(self) -> Tuple[bool, str]:
        """快速检查系统资源是否在安全范围内"""
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')

        warnings = []
        if cpu > 90:
            warnings.append(f"CPU 使用率 {cpu:.1f}%")
        if mem.percent > 90:
            warnings.append(f"内存使用率 {mem.percent:.1f}%")
        if disk.percent > 95:
            warnings.append(f"磁盘使用率 {disk.percent:.1f}%")

        if warnings:
            return False, "资源超限: " + ", ".join(warnings)
        return True, f"CPU {cpu:.0f}%, 内存 {mem.percent:.0f}%, 磁盘 {disk.percent:.0f}%"

    def _check_services(self) -> Tuple[bool, str]:
        """检查关键依赖服务是否运行（Redis, Docker）"""
        warnings = []
        # 检查 redis-server
        for proc in psutil.process_iter(['name']):
            if 'redis-server' in proc.info['name']:
                break
        else:
            warnings.append("redis-server 未运行")

        # 检查 docker
        for proc in psutil.process_iter(['name']):
            if 'dockerd' in proc.info['name']:
                break
        else:
            # Docker 可能未安装，不强制
            pass

        if warnings:
            return False, ", ".join(warnings)
        return True, "服务正常"

    async def _check_sharpe(self, old_sharpe: Optional[float] = None) -> Tuple[bool, str]:
        """
        通过 API 获取最近的日收益/小时收益数据，
        计算短期滚动夏普，与旧版本对比。
        """
        try:
            # 调用 /api/status/account 获取最近的盈亏数据
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.api_base}/api/status/account", timeout=10) as resp:
                    if resp.status != 200:
                        return False, "无法获取账户数据"
                    data = await resp.json()
                    # 假设返回中包含 'recent_pnl_list' 或 'equity_curve'
                    pnl_list = data.get("recent_pnl_list", [])
                    if not pnl_list:
                        # 从收益曲线API获取
                        async with session.get(f"{self.api_base}/api/status/equity-curve?days=1", timeout=5) as equity_resp:
                            if equity_resp.status == 200:
                                equity_data = await equity_resp.json()
                                values = equity_data.get("values", [])
                                if len(values) >= 2:
                                    pnl_list = [values[i] - values[i-1] for i in range(1, len(values))]
        except Exception as e:
            logger.warning(f"获取绩效数据失败: {e}")
            return True, "绩效数据不可用，跳过检查"  # 不阻断更新

        if len(pnl_list) < self.min_data_points:
            return True, f"数据点不足 ({len(pnl_list)}<{self.min_data_points})，跳过检查"

        # 计算短期夏普（假设每个点为分钟级收益，简化：日收益）
        pnl_arr = np.array(pnl_list[-self.sharpe_window_minutes:])
        mean_ret = np.mean(pnl_arr)
        std_ret = np.std(pnl_arr) if len(pnl_arr) > 1 else 0.001
        sharpe = (mean_ret / (std_ret + 1e-10)) * np.sqrt(252 * 24 * 60)  # 年化（按分钟）

        if old_sharpe is not None and old_sharpe > 0:
            decline = (old_sharpe - sharpe) / old_sharpe
            if decline > self.sharpe_decline_threshold:
                return False, f"夏普显著恶化: 旧={old_sharpe:.2f}, 新={sharpe:.2f} (下降 {decline*100:.0f}%)"
        elif sharpe < -1.0:
            return False, f"夏普过低: {sharpe:.2f}"

        return True, f"夏普: {sharpe:.2f}"

    async def _check_signal_divergence(self) -> Tuple[bool, str]:
        """
        检查镜像流量模块的信号分歧度。
        若未启用镜像，则跳过。
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.api_base}/api/ota/mirror-status", timeout=5) as resp:
                    if resp.status == 404:
                        return True, "未启用镜像流量"
                    if resp.status == 200:
                        mirror_data = await resp.json()
                        mismatch_rate = mirror_data.get("mismatch_rate", 0.0)
                        if mismatch_rate > self.divergence_threshold:
                            return False, f"信号分歧过高: {mismatch_rate*100:.1f}%"
                        return True, f"分歧度: {mismatch_rate*100:.1f}%"
        except Exception as e:
            logger.warning(f"检查信号分歧失败: {e}")
        return True, "无法获取分歧数据，跳过"

    # ====================== 便捷同步方法 ======================
    def run_sync(self, old_sharpe: Optional[float] = None) -> Dict[str, Any]:
        """同步版本的运行方法（用于非异步上下文）"""
        return asyncio.run(self.run_full_check(old_sharpe))

    def quick_health(self) -> bool:
        """最简健康检查：仅检查API连通性和最低资源"""
        connectivity = False
        try:
            import requests
            resp = requests.get(f"{self.api_base}/health", timeout=5)
            connectivity = resp.status_code == 200
        except Exception:
            pass
        return connectivity
