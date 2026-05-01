#!/usr/bin/env python3
"""
火种系统 (FireSeed) 管理员路由
================================
提供系统级别的管理功能，包括：
- API 密钥管理（列表、添加、删除，需 TOTP 二次验证）
- 全局配置热更新（需 TOTP）
- 系统重启（需议会投票 + TOTP）
- 操作日志与审计记录查询
"""

import os
import time
from datetime import datetime
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Body
from pydantic import BaseModel, Field, validator

from api.server import get_engine, get_config
from api.routes.auth import get_current_user, verify_totp_code
from core.behavioral_logger import EventType

router = APIRouter()


# ======================== 数据模型 ========================
class ApiKeyInfo(BaseModel):
    id: str = Field(..., description="密钥标识")
    exchange: str = Field(..., description="交易所名称")
    label: str = Field(..., description="用户自定义标签")
    public_key: str = Field(..., description="API Key 脱敏显示")
    permissions: List[str] = Field(..., description="权限列表")
    enabled: bool = Field(True, description="是否启用")
    created_at: str = Field(..., description="创建时间")
    last_used: Optional[str] = Field(None, description="最后使用时间")

class AddApiKeyRequest(BaseModel):
    exchange: str = Field(..., description="交易所，如 binance")
    label: str = Field(..., description="标签")
    api_key: str = Field(..., description="API Key 明文")
    api_secret: str = Field(..., description="API Secret 明文")
    permissions: List[str] = Field(default=["trade"], description="权限")
    totp: str = Field(..., min_length=6, max_length=6, regex=r'^\d{6}$')

class DeleteApiKeyRequest(BaseModel):
    key_id: str = Field(..., description="密钥 ID")
    totp: str = Field(..., min_length=6, max_length=6, regex=r'^\d{6}$')

class ConfigUpdateRequest(BaseModel):
    section: str = Field(..., description="配置段，如 risk.daily_loss_limit")
    value: Any = Field(..., description="新值，可以是字符串、数字或布尔")
    totp: str = Field(..., min_length=6, max_length=6, regex=r'^\d{6}$')

class RestartRequest(BaseModel):
    totp: str = Field(..., min_length=6, max_length=6, regex=r'^\d{6}$')
    force: bool = Field(False, description="是否跳过议会投票")


# ======================== API 密钥管理 ========================
@router.get("/keys", response_model=List[ApiKeyInfo])
async def list_api_keys(user: dict = Depends(get_current_user)):
    """
    列出所有已配置的 API 密钥（脱敏信息）。
    需要登录。
    """
    engine = get_engine()
    keys = engine.execution.get_api_key_list() if hasattr(engine.execution, 'get_api_key_list') else []
    return [
        ApiKeyInfo(
            id=k.get('id', 'unknown'),
            exchange=k.get('exchange', 'binance'),
            label=k.get('label', ''),
            public_key=mask_api_key(k.get('api_key', '')),
            permissions=k.get('permissions', []),
            enabled=k.get('enabled', True),
            created_at=k.get('created_at', datetime.now().isoformat()),
            last_used=k.get('last_used')
        )
        for k in keys
    ]


@router.post("/keys", response_model=ApiKeyInfo)
async def add_api_key(request: AddApiKeyRequest, user: dict = Depends(get_current_user)):
    """
    添加或更新 API 密钥。
    需要 TOTP 二次验证。
    """
    if not verify_totp_code(request.totp):
        raise HTTPException(status_code=401, detail="TOTP 动态码无效")

    engine = get_engine()
    try:
        # 加密存储密钥
        key_id = engine.execution.add_api_key(
            exchange=request.exchange,
            label=request.label,
            api_key=request.api_key,
            api_secret=request.api_secret,
            permissions=request.permissions
        )
        engine.behavior_log.log(
            EventType.SYSTEM, "Admin",
            f"添加 API 密钥: {request.exchange}/{request.label}",
            {"key_id": key_id}
        )
        return ApiKeyInfo(
            id=key_id,
            exchange=request.exchange,
            label=request.label,
            public_key=mask_api_key(request.api_key),
            permissions=request.permissions,
            enabled=True,
            created_at=datetime.now().isoformat(),
            last_used=None
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"添加密钥失败: {str(e)}")


@router.delete("/keys/{key_id}")
async def delete_api_key(key_id: str, request: DeleteApiKeyRequest, user: dict = Depends(get_current_user)):
    """
    删除指定 API 密钥。
    需要 TOTP 二次验证。
    """
    if not verify_totp_code(request.totp):
        raise HTTPException(status_code=401, detail="TOTP 动态码无效")

    engine = get_engine()
    try:
        success = engine.execution.remove_api_key(key_id)
        if not success:
            raise HTTPException(status_code=404, detail="密钥未找到")
        engine.behavior_log.log(
            EventType.SYSTEM, "Admin",
            f"删除 API 密钥: {key_id}"
        )
        return {"status": "deleted", "key_id": key_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除密钥失败: {str(e)}")


# ======================== 配置管理 ========================
@router.get("/config")
async def get_config_snapshot(user: dict = Depends(get_current_user)):
    """
    获取当前系统配置快照（脱敏）。
    需要登录。
    """
    config = get_config()
    # 移除敏感字段
    safe_config = config.data.copy() if hasattr(config, 'data') else {}
    for section in ['auth', 'api_keys', 'multi_account']:
        if section in safe_config:
            safe_config[section] = "***REDACTED***"
    return safe_config


@router.post("/config")
async def update_config(request: ConfigUpdateRequest, user: dict = Depends(get_current_user)):
    """
    动态更新配置项。
    需要 TOTP 二次验证。
    配置变更会立即写入并触发热重载（部分需要重启）。
    """
    if not verify_totp_code(request.totp):
        raise HTTPException(status_code=401, detail="TOTP 动态码无效")

    config = get_config()
    engine = get_engine()
    try:
        old_val = config.get(request.section) if config.exists(request.section) else None
        config.set(request.section, request.value)
        config.save()

        # 通知引擎热重载
        if hasattr(engine, 'reload_config'):
            engine.reload_config()

        engine.behavior_log.log(
            EventType.SYSTEM, "Admin",
            f"配置更新: {request.section} {old_val} -> {request.value}"
        )
        return {"status": "updated", "section": request.section, "old_value": old_val, "new_value": request.value}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新配置失败: {str(e)}")


# ======================== 系统操作 ========================
@router.post("/restart")
async def restart_system(request: RestartRequest, user: dict = Depends(get_current_user)):
    """
    重启火种引擎。
    需要 TOTP + 非强制模式下需议会投票通过。
    ⚠️ 该操作将中断交易，谨慎执行。
    """
    if not verify_totp_code(request.totp):
        raise HTTPException(status_code=401, detail="TOTP 动态码无效")

    engine = get_engine()

    # 非强制模式检查议会
    if not request.force:
        council = engine.agent_council
        if hasattr(council, 'vote_for_action'):
            approved = await council.vote_for_action("restart")
            if not approved:
                raise HTTPException(status_code=403, detail="重启被智能体议会否决")

    engine.behavior_log.log(EventType.SYSTEM, "Admin", "系统重启指令已执行")
    # 在真实环境中，可以调用外部脚本或发送信号
    # 这里返回提示，实际重启由外部进程管理器完成
    return {"status": "accepted", "message": "重启请求已接收，服务将在几秒后重启"}


@router.get("/logs")
async def get_system_logs(
    level: Optional[str] = Query(None, regex="^(DEBUG|INFO|WARN|ERROR|CRITICAL)$"),
    lines: int = Query(100, ge=1, le=1000),
    user: dict = Depends(get_current_user)
):
    """
    获取最近的行为/系统日志。
    """
    engine = get_engine()
    log_entries = engine.behavior_log.get_recent(lines, level=level)
    return [
        {
            "timestamp": entry.ts_str,
            "level": entry.level if hasattr(entry, 'level') else "INFO",
            "module": entry.module,
            "content": entry.content
        }
        for entry in log_entries
    ]


# ======================== 辅助函数 ========================
def mask_api_key(key: str) -> str:
    """对 API Key 进行脱敏处理，仅显示前4位和后4位"""
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]
