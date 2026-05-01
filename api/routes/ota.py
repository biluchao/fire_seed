#!/usr/bin/env python3
"""
火种系统 (FireSeed) OTA 热更新路由
===================================
提供：
- GET  /status          查看当前版本与更新状态
- POST /check           检查 GitHub 最新版本
- POST /update          触发 OTA 更新（需 TOTP + 议会投票）
- POST /rollback        回滚到上一版本（需 TOTP）
- GET  /history         获取更新历史
"""

import os
from typing import Optional, List
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, validator

from api.server import get_engine, get_config
from api.routes.auth import get_current_user, verify_totp_code
from core.behavioral_logger import EventType

router = APIRouter()


# ======================== 数据模型 ========================
class OTAStatusResponse(BaseModel):
    current_version: str = Field(..., description="当前运行版本")
    latest_version: Optional[str] = Field(None, description="远程最新版本")
    update_available: bool = Field(False, description="是否有可用更新")
    last_checked: Optional[str] = Field(None, description="上次检查时间")
    last_updated: Optional[str] = Field(None, description="上次更新时间")
    ghost_validation_passed: Optional[bool] = Field(None, description="最新版本是否通过幽灵验证")

class OTAUpdateRequest(BaseModel):
    totp: str = Field(..., min_length=6, max_length=6, regex=r'^\d{6}$', description="TOTP 动态码")
    force: bool = Field(False, description="是否跳过议会投票（仅限紧急情况）")

class OTARollbackRequest(BaseModel):
    totp: str = Field(..., min_length=6, max_length=6, regex=r'^\d{6}$')

class UpdateHistoryItem(BaseModel):
    version: str
    timestamp: str
    success: bool
    rolled_back: bool = False
    details: Optional[str] = None


# ======================== 端点实现 ========================
@router.get("/status", response_model=OTAStatusResponse)
async def ota_status(user: dict = Depends(get_current_user)):
    """查看OTA更新状态与版本信息"""
    engine = get_engine()
    if not hasattr(engine, 'ota_updater'):
        raise HTTPException(status_code=500, detail="OTA模块未初始化")

    updater = engine.ota_updater
    current_ver = updater.current_version
    latest_ver = updater.latest_version
    update_avail = latest_ver is not None and latest_ver != current_ver
    ghost_ok = updater.ghost_validation_result if hasattr(updater, 'ghost_validation_result') else None

    return OTAStatusResponse(
        current_version=current_ver,
        latest_version=latest_ver,
        update_available=update_avail,
        last_checked=updater.last_checked.isoformat() if updater.last_checked else None,
        last_updated=updater.last_updated.isoformat() if updater.last_updated else None,
        ghost_validation_passed=ghost_ok,
    )


@router.post("/check")
async def check_for_update(user: dict = Depends(get_current_user)):
    """手动触发检查 GitHub 最新版本（无需 TOTP）"""
    engine = get_engine()
    if not hasattr(engine, 'ota_updater'):
        raise HTTPException(status_code=500, detail="OTA模块未初始化")

    updater = engine.ota_updater
    try:
        result = await updater.check_github_release()  # 假设是异步方法
        engine.behavior_log.log(EventType.SYSTEM, "OTA", f"检查更新: 当前 {updater.current_version}, 最新 {result}")
        return {
            "current_version": updater.current_version,
            "latest_version": result,
            "update_available": result != updater.current_version
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"检查更新失败: {str(e)}")


@router.post("/update")
async def trigger_ota_update(request: OTAUpdateRequest, user: dict = Depends(get_current_user)):
    """
    触发 OTA 热更新。
    标准流程：
    1. 验证 TOTP
    2. 若不是强制模式，需通过智能体议会投票（两票通过）
    3. 执行幽灵验证
    4. 下载新版本并执行原子替换
    """
    # 验证 TOTP
    if not verify_totp_code(request.totp):
        raise HTTPException(status_code=401, detail="TOTP 动态码无效")

    engine = get_engine()
    if not hasattr(engine, 'ota_updater'):
        raise HTTPException(status_code=500, detail="OTA模块未初始化")

    # 非强制模式下需要议会投票
    if not request.force:
        council = engine.agent_council
        if hasattr(council, 'vote_for_ota'):
            approved = await council.vote_for_ota()
            if not approved:
                engine.behavior_log.log(EventType.SYSTEM, "OTA", "更新被议会否决")
                raise HTTPException(status_code=403, detail="更新被智能体议会否决")

    updater = engine.ota_updater
    try:
        # 执行更新流程
        success = await updater.perform_update()
        if success:
            engine.behavior_log.log(
                EventType.SYSTEM, "OTA",
                f"OTA 更新成功: {updater.current_version}",
                {"new_version": updater.current_version}
            )
            return {"status": "success", "version": updater.current_version}
        else:
            raise HTTPException(status_code=500, detail="更新失败，请查看日志")
    except Exception as e:
        engine.behavior_log.log(EventType.SYSTEM, "OTA", f"更新异常: {str(e)}")
        raise HTTPException(status_code=500, detail=f"更新异常: {str(e)}")


@router.post("/rollback")
async def rollback_ota(request: OTARollbackRequest, user: dict = Depends(get_current_user)):
    """
    回滚到上一个版本（需 TOTP）
    """
    if not verify_totp_code(request.totp):
        raise HTTPException(status_code=401, detail="TOTP 动态码无效")

    engine = get_engine()
    if not hasattr(engine, 'ota_updater'):
        raise HTTPException(status_code=500, detail="OTA模块未初始化")

    updater = engine.ota_updater
    try:
        success = await updater.rollback()
        if success:
            engine.behavior_log.log(
                EventType.SYSTEM, "OTA",
                f"回滚成功，当前版本: {updater.current_version}"
            )
            return {"status": "success", "version": updater.current_version}
        else:
            raise HTTPException(status_code=500, detail="回滚失败，请查看日志")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"回滚异常: {str(e)}")


@router.get("/history", response_model=List[UpdateHistoryItem])
async def update_history(user: dict = Depends(get_current_user)):
    """获取OTA更新历史记录"""
    engine = get_engine()
    if not hasattr(engine, 'ota_updater'):
        raise HTTPException(status_code=500, detail="OTA模块未初始化")

    updater = engine.ota_updater
    history = updater.get_history() if hasattr(updater, 'get_history') else []
    return [
        UpdateHistoryItem(
            version=h.get('version', 'unknown'),
            timestamp=h.get('timestamp', datetime.now()).isoformat() if isinstance(h.get('timestamp'), datetime) else str(h.get('timestamp', '')),
            success=h.get('success', False),
            rolled_back=h.get('rolled_back', False),
            details=h.get('details')
        )
        for h in history
                             ]
