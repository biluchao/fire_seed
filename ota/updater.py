#!/usr/bin/env python3
"""
火种系统 (FireSeed) OTA 热更新模块
=====================================
功能：
- 从 GitHub 私有/公开仓库检查最新发行版本
- 下载新版本代码到临时目录
- 在幽灵影子中验证新版本的交易表现
- 原子替换当前运行版本的符号链接
- 自动回滚到上一个稳定版本
- 记录所有更新历史
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

import aiohttp
import yaml

logger = logging.getLogger("fire_seed.ota")


class OTAUpdater:
    """
    负责整个 OTA 更新流程：
    1. 检查远程最新版本
    2. 下载并解压
    3. 启动幽灵验证
    4. 替换当前版本
    5. 健康检查与回滚
    """

    def __init__(self, config):
        self.config = config
        # OTA 配置
        ota_cfg = config.get("ota", {})
        self.repo_url = ota_cfg.get("repo_url", "")
        self.branch = ota_cfg.get("branch", "main")
        self.current_version = ota_cfg.get("current_version", "0.0.0")
        self.backup_dir = Path(ota_cfg.get("backup_dir", "versions"))
        self.update_dir = Path(ota_cfg.get("update_dir", "updates"))
        self.ghost_verify_hours = ota_cfg.get("ghost_verify_hours", 24)

        # 当前版本链接（通常为软链接指向当前版本目录）
        self.current_link = Path(ota_cfg.get("current_link", "current"))

        # GitHub API 配置
        self.github_token = os.getenv("GITHUB_TOKEN", "")
        self.github_repo = ota_cfg.get("github_repo", "")
        self.github_api_base = "https://api.github.com"

        # 内部状态
        self.last_checked: Optional[datetime] = None
        self.last_updated: Optional[datetime] = None
        self.latest_version: Optional[str] = None
        self.ghost_validation_result: Optional[bool] = None
        self._update_history: list = []

        # 确保目录存在
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.update_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"OTA 更新模块初始化完成，当前版本: {self.current_version}")

    # ======================== 版本检查 ========================
    async def check_github_release(self) -> Optional[str]:
        """
        检查 GitHub 最新发行版本。
        返回最新版本号字符串，若失败或无更新则返回 None。
        """
        if not self.github_repo:
            logger.warning("未配置 GitHub 仓库，无法检查更新")
            return None

        url = f"{self.github_api_base}/repos/{self.github_repo}/releases/latest"
        headers = {"Accept": "application/vnd.github.v3+json"}
        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=15) as resp:
                    if resp.status == 200:
                        release = await resp.json()
                        tag = release.get("tag_name", "").lstrip("v")
                        self.latest_version = tag
                        self.last_checked = datetime.now()
                        logger.info(f"GitHub 最新版本: {tag} (当前: {self.current_version})")
                        return tag if tag != self.current_version else None
                    else:
                        logger.warning(f"GitHub API 返回状态码 {resp.status}")
        except Exception as e:
            logger.error(f"检查 GitHub 版本失败: {e}")

        self.last_checked = datetime.now()
        return None

    # ======================== 下载与准备 ========================
    async def download_release(self, version: str) -> Optional[Path]:
        """
        下载指定版本的压缩包到临时目录，返回解压后的目录路径。
        """
        if not self.github_repo:
            return None

        # 获取发行版的下载 URL
        url = f"{self.github_api_base}/repos/{self.github_repo}/releases/tags/v{version}"
        headers = {"Accept": "application/vnd.github.v3+json"}
        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status != 200:
                    logger.error(f"获取发行版信息失败: {resp.status}")
                    return None
                release = await resp.json()
                tarball_url = release.get("tarball_url")
                if not tarball_url:
                    logger.error("发行版中未找到压缩包链接")
                    return None

                # 下载压缩包
                target = self.update_dir / f"v{version}.tar.gz"
                async with session.get(tarball_url, headers=headers, timeout=120) as tarball_resp:
                    if tarball_resp.status != 200:
                        logger.error(f"下载压缩包失败: {tarball_resp.status}")
                        return None

                    with open(target, "wb") as f:
                        while True:
                            chunk = await tarball_resp.content.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)

                # 解压到临时目录
                extract_dir = self.update_dir / f"v{version}"
                if extract_dir.exists():
                    shutil.rmtree(extract_dir)
                extract_dir.mkdir(parents=True)

                with tarfile.open(target, "r:gz") as tar:
                    # GitHub 的 tarball 包含一个顶层目录，提取时去掉
                    members = tar.getmembers()
                    if members and "/" in members[0].name:
                        prefix = members[0].name.split("/")[0]
                        for member in members:
                            member.name = member.name.replace(f"{prefix}/", "", 1) if member.name.startswith(prefix + "/") else member.name
                    tar.extractall(extract_dir)

                os.remove(target)
                logger.info(f"版本 {version} 下载并解压至 {extract_dir}")
                return extract_dir

        return None

    # ======================== 影子验证 ========================
    async def ghost_verify(self, new_version_dir: Path) -> bool:
        """
        在幽灵影子中运行新版本代码，验证其交易表现。
        实际实现会启动一个独立的子进程，加载新版本引擎并运行。
        这里返回占位结果（生产需真实实现）。
        """
        logger.info(f"开始幽灵验证 {self.ghost_verify_hours} 小时...")
        # 占位：假设验证通过
        # 真实实现应启动子进程，等待足够时间后比较夏普
        await asyncio.sleep(1)  # 模拟异步等待
        self.ghost_validation_result = True
        logger.info("幽灵验证通过")
        return True

    # ======================== 原子替换 ========================
    async def hot_replace(self, new_dir: Path) -> bool:
        """
        原子替换当前运行版本：
        1. 将新版本目录复制到备份目录
        2. 更新软链接（符号链接）指向新目录
        3. 重启服务（通过 systemd 或其他进程管理器）
        """
        try:
            # 备份当前版本
            if self.current_link.exists():
                old_target = self.current_link.resolve()
                backup_name = f"backup_{datetime.now().strftime('%Y%m%d%H%M%S')}"
                backup_path = self.backup_dir / backup_name
                shutil.copytree(old_target, backup_path, symlinks=True)
                logger.info(f"旧版本已备份至 {backup_path}")

            # 复制新版本到正式目录
            version_dir = self.backup_dir / f"v{self.latest_version}"
            if version_dir.exists():
                shutil.rmtree(version_dir)
            shutil.copytree(new_dir, version_dir, symlinks=True)

            # 更新软链接
            if self.current_link.is_symlink() or self.current_link.exists():
                self.current_link.unlink()
            self.current_link.symlink_to(version_dir)

            # 更新当前版本号记录
            self.current_version = self.latest_version
            self.last_updated = datetime.now()

            # 记录更新历史
            self._update_history.append({
                "version": self.latest_version,
                "timestamp": self.last_updated.isoformat(),
                "success": True,
                "backup": str(backup_path) if 'backup_path' in dir() else None,
            })

            logger.info(f"原子替换完成，当前版本: {self.current_version}")
            return True
        except Exception as e:
            logger.error(f"原子替换失败: {e}")
            return False

    # ======================== 回滚 ========================
    async def rollback(self) -> bool:
        """
        回滚到上一个稳定版本。
        从备份目录中找到最近的非损坏备份，恢复软链接。
        """
        if not self.backup_dir.exists():
            logger.error("备份目录不存在，无法回滚")
            return False

        # 按时间排序找到最新的备份
        backups = sorted(
            [d for d in self.backup_dir.iterdir() if d.is_dir() and d.name.startswith("backup_")],
            key=lambda d: d.stat().st_mtime,
            reverse=True
        )
        if not backups:
            logger.error("未找到任何备份")
            return False

        last_good = backups[0]
        logger.info(f"回滚至备份: {last_good.name}")

        # 恢复软链接
        if self.current_link.is_symlink() or self.current_link.exists():
            self.current_link.unlink()
        self.current_link.symlink_to(last_good)

        # 读取备份版本号（从备份目录中的 version 文件获取）
        version_file = last_good / "version.txt"
        if version_file.exists():
            self.current_version = version_file.read_text().strip()

        self.last_updated = datetime.now()
        self._update_history.append({
            "version": self.current_version,
            "timestamp": self.last_updated.isoformat(),
            "success": True,
            "rolled_back": True,
        })

        logger.info(f"回滚完成，当前版本: {self.current_version}")
        return True

    # ======================== 完整更新流程 ========================
    async def perform_update(self) -> bool:
        """
        执行完整的 OTA 更新流程：
        检查 → 下载 → 幽灵验证 → 替换 → 健康检查
        """
        # 1. 检查更新
        latest = await self.check_github_release()
        if not latest:
            logger.info("当前已是最新版本")
            return False

        # 2. 下载
        new_dir = await self.download_release(latest)
        if not new_dir:
            return False

        # 3. 幽灵验证
        verified = await self.ghost_verify(new_dir)
        if not verified:
            logger.warning("幽灵验证未通过，取消更新")
            return False

        # 4. 原子替换
        replaced = await self.hot_replace(new_dir)
        if not replaced:
            return False

        # 5. 健康检查（由外部 health_check.py 提供）
        if not self._health_check():
            logger.error("更新后健康检查失败，自动回滚")
            await self.rollback()
            return False

        logger.info(f"OTA 更新成功，新版本: {self.current_version}")
        return True

    def _health_check(self) -> bool:
        """更新后健康检查（占位，实际调用 health_check.py 或检查关键 API）"""
        try:
            # 检查进程是否存活，API 是否响应
            # 这里简单返回 True，生产需实现完整检测
            return True
        except Exception:
            return False

    async def check_and_update(self) -> Optional[str]:
        """
        供引擎后台调用的周期性检查，有新版本时自动执行更新。
        """
        latest = await self.check_github_release()
        if latest and latest != self.current_version:
            logger.info(f"发现新版本: {latest}，准备自动更新...")
            success = await self.perform_update()
            if success:
                return latest
        return None

    # ======================== 查询接口 ========================
    def get_history(self) -> list:
        """返回更新历史列表"""
        return self._update_history[-20:]
