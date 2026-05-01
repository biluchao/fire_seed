#!/usr/bin/env python3
"""
火种系统 (FireSeed) 策略管理路由
================================
负责策略模式切换、参数调整、状态查询。
所有修改类端点需要静态密码 + TOTP 二次验证。
"""

from typing import Dict, Any, Optional, List
from fastapi import APIRouter, Depends, HTTPException, Body
from pydantic import BaseModel, Field, validator

from api.server import get_engine, get_config
from api.routes.auth import get_current_user, verify_static_password, verify_totp_code
from core.behavioral_logger import EventType

router = APIRouter()


# ======================== 请求/响应模型 ========================
class StrategyModeResponse(BaseModel):
    mode: str = Field(..., description="当前策略模式: aggressive / moderate")
    available_modes: List[str] = Field(["aggressive", "moderate"])

class ModeSwitchRequest(BaseModel):
    mode: str = Field(..., regex="^(aggressive|moderate)$")
    password_hash: str = Field(..., description="静态密码的 SHA-256 哈希")
    totp: str = Field(..., min_length=6, max_length=6, regex=r'^\d{6}$', description="TOTP 动态码")

class StrategyStatus(BaseModel):
    name: str
    enabled: bool
    description: str
    active_positions: int = 0
    daily_pnl: float = 0.0
    win_rate: float = 0.0

class StrategyListResponse(BaseModel):
    strategies: List[StrategyStatus]

class StrategyParamUpdate(BaseModel):
    strategy_name: str = Field(..., description="策略名称")
    param: str = Field(..., description="参数路径，如 'entry.threshold'")
    value: float
    password_hash: str
    totp: str = Field(..., min_length=6, max_length=6, regex=r'^\d{6}$')


# ======================== 辅助函数 ========================
def _authenticate_operation(password_hash: str, totp: str):
    """合并密码与TOTP验证"""
    if not verify_static_password(password_hash):
        raise HTTPException(status_code=401, detail="静态密码错误")
    if not verify_totp_code(totp):
        raise HTTPException(status_code=401, detail="TOTP动态码无效")


# ======================== 端点实现 ========================
@router.get("/mode", response_model=StrategyModeResponse)
async def get_strategy_mode(user: dict = Depends(get_current_user)):
    """获取当前策略模式（需登录）"""
    config = get_config()
    mode = config.get("system.strategy_mode", "moderate")
    return StrategyModeResponse(mode=mode, available_modes=["aggressive", "moderate"])


@router.post("/mode", response_model=StrategyModeResponse)
async def set_strategy_mode(request: ModeSwitchRequest, user: dict = Depends(get_current_user)):
    """
    切换策略模式（激进/稳健）
    需要静态密码 + TOTP 二次验证
    """
    _authenticate_operation(request.password_hash, request.totp)

    config = get_config()
    current_mode = config.get("system.strategy_mode", "moderate")
    if request.mode == current_mode:
        return StrategyModeResponse(mode=current_mode, available_modes=["aggressive", "moderate"])

    # 通过引擎执行热切换
    engine = get_engine()
    try:
        # 调用引擎的模式切换方法（若不存在则模拟）
        if hasattr(engine, 'set_strategy_mode'):
            engine.set_strategy_mode(request.mode)
        else:
            # 直接修改配置并热重载
            config.set("system.strategy_mode", request.mode)
            config.save()
            # 通知引擎重新加载配置
            if hasattr(engine, 'reload_config'):
                engine.reload_config()

        engine.behavior_log.log(
            EventType.STRATEGY, "ModeSwitch",
            f"策略模式切换: {current_mode} -> {request.mode}"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"模式切换失败: {str(e)}")

    return StrategyModeResponse(mode=request.mode, available_modes=["aggressive", "moderate"])


@router.get("/status", response_model=StrategyListResponse)
async def get_strategy_status(user: dict = Depends(get_current_user)):
    """获取所有已加载策略的运行状态"""
    engine = get_engine()
    strategies = []
    # 遍历插件管理器中的策略
    for name, instance in engine.plugin_mgr.strategies.items():
        strategies.append(StrategyStatus(
            name=name,
            enabled=True,
            description=getattr(instance, 'description', ''),
            active_positions=instance.get_active_position_count() if hasattr(instance, 'get_active_position_count') else 0,
            daily_pnl=instance.get_daily_pnl() if hasattr(instance, 'get_daily_pnl') else 0.0,
            win_rate=instance.get_win_rate() if hasattr(instance, 'get_win_rate') else 0.0
        ))
    return StrategyListResponse(strategies=strategies)


@router.get("/params")
async def get_strategy_params(strategy_name: str = "trend_1m", user: dict = Depends(get_current_user)):
    """获取指定策略的当前参数（需登录）"""
    config = get_config()
    params = config.get(f"strategy.{strategy_name}", None)
    if params is None:
        raise HTTPException(status_code=404, detail=f"策略 '{strategy_name}' 未找到")
    return {"strategy": strategy_name, "params": params}


@router.post("/params")
async def update_strategy_param(request: StrategyParamUpdate, user: dict = Depends(get_current_user)):
    """
    修改指定策略的参数（需密码+TOTP）
    参数路径示例: 'trend_1m.entry.resonance_prob_threshold'
    """
    _authenticate_operation(request.password_hash, request.totp)

    config = get_config()
    full_path = f"strategy.{request.strategy_name}.{request.param}"
    if not config.exists(full_path):
        raise HTTPException(status_code=404, detail=f"参数路径 {full_path} 不存在")

    old_val = config.get(full_path)
    config.set(full_path, request.value)
    config.save()

    engine = get_engine()
    engine.behavior_log.log(
        EventType.STRATEGY, "ParamUpdate",
        f"策略参数 {full_path}: {old_val} -> {request.value}"
    )
    # 通知策略热重载参数（引擎根据策略名重载对应模块）
    if hasattr(engine, 'reload_strategy_params'):
        engine.reload_strategy_params(request.strategy_name)

    return {"success": True, "param": full_path, "old_value": old_val, "new_value": request.value}
