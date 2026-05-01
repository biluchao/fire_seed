#!/usr/bin/env python3
"""
火种系统 (FireSeed) 多账户跟单路由
====================================
提供主账户与子账户的管理与跟单控制：
- 列出所有子账户及其状态
- 切换主界面显示的当前账户
- 手动触发跟单同步
- 查看跟单历史与错误日志
"""

import time
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Body
from pydantic import BaseModel, Field

from api.server import get_engine, get_config
from api.routes.auth import get_current_user
from core.behavioral_logger import EventType

router = APIRouter()


# ======================== 数据模型 ========================
class SubAccountInfo(BaseModel):
    name: str = Field(..., description="账户名称")
    api_key_masked: str = Field(..., description="已脱敏的API密钥")
    follow_ratio: float = Field(..., description="跟单比例")
    max_position_pct: float = Field(..., description="单品种最大仓位占比")
    status: str = Field(..., description="连接状态：online / offline / disabled")
    last_sync: Optional[str] = Field(None, description="上次同步时间")
    daily_pnl: float = Field(0.0, description="今日盈亏")

class AccountSwitchRequest(BaseModel):
    account_name: str = Field(..., description="要切换到的账户名称")
    # 切换账户只需登录即可，不需要额外验证

class SyncRequest(BaseModel):
    account_name: Optional[str] = Field(None, description="要同步的特定账户，留空则同步所有")

class CopyTradeStatus(BaseModel):
    enabled: bool
    master_account: str
    sub_accounts_count: int
    online_count: int
    last_global_sync: Optional[str]


# ======================== 辅助函数 ========================
def get_copy_trader():
    """获取跟单引擎实例"""
    engine = get_engine()
    if not hasattr(engine, 'copy_trading'):
        raise HTTPException(status_code=500, detail="多账户跟单模块未初始化")
    return engine.copy_trading


# ======================== 端点实现 ========================
@router.get("/status", response_model=CopyTradeStatus)
async def copy_trading_status(user: dict = Depends(get_current_user)):
    """获取跟单系统的整体状态"""
    trader = get_copy_trader()
    subs = trader.list_sub_accounts()
    online = sum(1 for s in subs if s.get('status') == 'online')
    return CopyTradeStatus(
        enabled=trader.enabled,
        master_account=trader.master_account_name,
        sub_accounts_count=len(subs),
        online_count=online,
        last_global_sync=trader.last_sync_time.isoformat() if trader.last_sync_time else None
    )


@router.get("/accounts", response_model=List[SubAccountInfo])
async def list_sub_accounts(user: dict = Depends(get_current_user)):
    """列出所有子账户及其当前状态"""
    trader = get_copy_trader()
    subs = trader.list_sub_accounts()
    result = []
    for sub in subs:
        result.append(SubAccountInfo(
            name=sub.get('name', 'unknown'),
            api_key_masked=sub.get('api_key_masked', '****'),
            follow_ratio=sub.get('follow_ratio', 1.0),
            max_position_pct=sub.get('max_position_pct', 50),
            status=sub.get('status', 'offline'),
            last_sync=sub.get('last_sync').isoformat() if sub.get('last_sync') else None,
            daily_pnl=sub.get('daily_pnl', 0.0)
        ))
    return result


@router.post("/switch")
async def switch_account(request: AccountSwitchRequest, user: dict = Depends(get_current_user)):
    """
    切换前端显示的当前账户（仅影响主界面显示，不改变实际交易）。
    此操作不涉及资金风险，无需TOTP。
    """
    trader = get_copy_trader()
    valid_names = [s.get('name') for s in trader.list_sub_accounts()] + [trader.master_account_name]
    if request.account_name not in valid_names:
        raise HTTPException(status_code=404, detail=f"账户 '{request.account_name}' 不存在")

    # 设置当前显示的账户
    trader.set_display_account(request.account_name)
    engine = get_engine()
    engine.behavior_log.log(
        EventType.SYSTEM, "MultiAccount",
        f"前端切换至账户: {request.account_name}"
    )
    return {"current_account": request.account_name, "status": "switched"}


@router.post("/sync")
async def trigger_sync(request: SyncRequest, user: dict = Depends(get_current_user)):
    """
    手动触发跟单同步。将主账户当前持仓和最新订单克隆到子账户。
    """
    trader = get_copy_trader()
    try:
        if request.account_name:
            result = await trader.sync_single(request.account_name)
        else:
            result = await trader.sync_all()
        engine = get_engine()
        engine.behavior_log.log(
            EventType.SYSTEM, "MultiAccount",
            f"手动触发跟单同步: {request.account_name or 'all'}",
            {"result": result}
        )
        return {"status": "success", "details": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"同步失败: {str(e)}")


@router.get("/errors")
async def recent_errors(limit: int = Query(20, ge=1, le=100), user: dict = Depends(get_current_user)):
    """获取最近的跟单错误日志"""
    trader = get_copy_trader()
    errors = trader.get_error_logs(limit)
    return [
        {
            "timestamp": e.timestamp.isoformat(),
            "account": e.account_name,
            "error": e.error_message,
            "resolved": e.resolved
        }
        for e in errors
    ]


@router.post("/disable/{account_name}")
async def disable_sub_account(account_name: str, user: dict = Depends(get_current_user)):
    """临时禁用一个子账户的跟单（需要登录，未来可加TOTP）"""
    trader = get_copy_trader()
    success = trader.disable_account(account_name)
    if not success:
        raise HTTPException(status_code=404, detail=f"账户 '{account_name}' 不存在或已禁用")
    engine = get_engine()
    engine.behavior_log.log(
        EventType.SYSTEM, "MultiAccount",
        f"禁用跟单账户: {account_name}"
    )
    return {"account": account_name, "status": "disabled"}


@router.post("/enable/{account_name}")
async def enable_sub_account(account_name: str, user: dict = Depends(get_current_user)):
    """重新启用一个已禁用的子账户"""
    trader = get_copy_trader()
    success = trader.enable_account(account_name)
    if not success:
        raise HTTPException(status_code=404, detail=f"账户 '{account_name}' 未处于禁用状态")
    engine = get_engine()
    engine.behavior_log.log(
        EventType.SYSTEM, "MultiAccount",
        f"启用跟单账户: {account_name}"
    )
    return {"account": account_name, "status": "enabled"}
