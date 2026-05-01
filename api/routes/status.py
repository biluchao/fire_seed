#!/ SoSystem
"""
火种系统 (FireSeed) 状态监控路由
================================
提供系统状态、健康度、熵值、自检等信息的查询端点。
部分端点需要身份验证 (Bearer Token)。
"""

import os
import time
import platform
import psutil
from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.server import get_engine, get_config
from api.routes.auth import get_current_user
from core.self_check import SystemSelfCheck
from core.risk_monitor import RiskMonitor

router = APIRouter()


# ======================== 响应模型 ========================
class SystemStatusResponse(BaseModel):
    cpu_usage: float = Field(..., description="CPU使用率 (百分比)")
    memory_used_gb: float = Field(..., description="已用内存 (GB)")
    memory_total_gb: float = Field(..., description="总内存 (GB)")
    disk_usage_pct: float = Field(..., description="磁盘使用率 (百分比)")
    latency_ms: float = Field(..., description="当前网络延迟 (毫秒)")
    packet_loss_pct: float = Field(..., description="丢包率 (百分比)")
    uptime_seconds: int = Field(..., description="系统运行时间 (秒)")

class HealthCheckResponse(BaseModel):
    health_score: int = Field(..., description="健康评分 (0-100)")
    cpu_temp: str = Field(..., description="CPU温度")
    disk_io_latency: str = Field(..., description="磁盘IO延迟")
    status: str = Field(..., description="状态描述")
    last_check_time: str = Field(..., description="上次检查时间")

class EntropyResponse(BaseModel):
    entropy_index: int = Field(..., description="熵增指数 (0-100)")
    redundant_code_pct: str = Field(..., description="冗余代码占比")
    unused_factors: int = Field(..., description="未使用的因子数量")
    evolve_fail_rate: str = Field(..., description="进化失败率")
    shadow_reject_rate: str = Field(..., description="影子验证淘汰率")
    agent_abstain_rate: str = Field(..., description="智能体弃权率")

class SelfCheckResponse(BaseModel):
    passed: bool
    checks: List[Dict[str, Any]]
    timestamp: str

class AccountSummaryResponse(BaseModel):
    equity: float
    available: float
    pnl: float
    margin_rate: str
    net_rate: str
    funding_cost: float

class RiskSnapshotResponse(BaseModel):
    drawdown: float
    drawdown_warn: float
    daily_loss: float
    daily_limit: float
    var_99: float
    cvar: float

class EquityCurveResponse(BaseModel):
    timestamps: List[str]
    values: List[float]


# ======================== 辅助函数 ========================
def get_system_stats() -> SystemStatusResponse:
    """获取系统实时状态"""
    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    net_io = psutil.net_io_counters()
    
    # 模拟网络延迟和丢包 (生产环境应从data_feed获取真实值)
    latency = 12.0 + (hash(str(time.time())) % 30) * 0.1
    packet_loss = 0.02
    
    return SystemStatusResponse(
        cpu_usage=cpu,
        memory_used_gb=round(mem.used / (1024**3), 1),
        memory_total_gb=round(mem.total / (1024**3), 1),
        disk_usage_pct=disk.percent,
        latency_ms=round(latency, 1),
        packet_loss_pct=packet_loss,
        uptime_seconds=int(time.time() - psutil.boot_time())
    )


def get_health_status() -> HealthCheckResponse:
    """获取系统健康度信息"""
    engine = get_engine()
    # 从自检模块获取
    health = engine.self_check.run()  # 返回 HealthCheck 对象
    
    return HealthCheckResponse(
        health_score=health.score,
        cpu_temp=f"{health.cpu_temp}°C" if health.cpu_temp else "N/A",
        disk_io_latency=f"{health.disk_io}ms" if health.disk_io else "N/A",
        status=health.status,
        last_check_time=health.timestamp.isoformat() if health.timestamp else datetime.now().isoformat()
    )


def get_entropy() -> EntropyResponse:
    """获取系统熵值信息"""
    engine = get_engine()
    entropy = engine.self_check.get_entropy()
    
    return EntropyResponse(
        entropy_index=entropy.score,
        redundant_code_pct=f"{entropy.redundant_code_ratio:.1%}",
        unused_factors=entropy.unused_factors,
        evolve_fail_rate=f"{entropy.evolve_fail_rate:.1%}",
        shadow_reject_rate=f"{entropy.shadow_reject_rate:.1%}",
        agent_abstain_rate=f"{entropy.agent_abstain_rate:.1%}"
    )


# ======================== 公共端点 (无需认证) ========================
@router.get("/ping")
async def ping():
    """存活探测"""
    return {"pong": True, "timestamp": int(time.time())}


@router.get("/version")
async def version():
    """获取版本信息"""
    config = get_config()
    return {
        "version": "3.0.0-spartan",
        "build_time": "2026-04-30T00:00:00Z",
        "mode": config.get("system.mode", "virtual"),
        "python_version": platform.python_version(),
        "cpp_available": config.get("cpp.enabled", True)
    }


@router.get("/system-stats")
async def system_stats():
    """获取系统资源使用情况"""
    return get_system_stats().dict()


# ======================== 需要认证的端点 ========================
@router.get("/health", response_model=HealthCheckResponse)
async def health_check(user: dict = Depends(get_current_user)):
    """获取系统健康度评分 (需登录)"""
    return get_health_status()


@router.get("/entropy", response_model=EntropyResponse)
async def entropy(user: dict = Depends(get_current_user)):
    """获取系统熵值 (需登录)"""
    return get_entropy()


@router.get("/self-check", response_model=SelfCheckResponse)
async def run_self_check(user: dict = Depends(get_current_user)):
    """执行系统自检并返回结果"""
    engine = get_engine()
    result = engine.self_check.run_full_check()
    return SelfCheckResponse(
        passed=result.passed,
        checks=result.checks,
        timestamp=result.timestamp.isoformat()
    )


@router.get("/account", response_model=AccountSummaryResponse)
async def account_summary(user: dict = Depends(get_current_user)):
    """获取账户概览 (需认证)"""
    engine = get_engine()
    pos = engine.order_mgr.get_position_summary()
    acc = engine.order_mgr.get_account()
    
    return AccountSummaryResponse(
        equity=acc.equity,
        available=acc.available,
        pnl=acc.unrealized_pnl + acc.realized_pnl_today,
        margin_rate=f"{acc.margin_rate:.0f}%",
        net_rate=f"{acc.net_value_ratio:.0f}%",
        funding_cost=acc.total_funding_paid
    )


@router.get("/risk", response_model=RiskSnapshotResponse)
async def risk_snapshot(user: dict = Depends(get_current_user)):
    """获取风险指标快照"""
    engine = get_engine()
    risk = engine.risk_monitor.get_snapshot()
    return RiskSnapshotResponse(
        drawdown=risk.drawdown_pct,
        drawdown_warn=risk.drawdown_warn_pct,
        daily_loss=risk.daily_loss_pct,
        daily_limit=risk.daily_loss_limit_pct,
        var_99=risk.var_99,
        cvar=risk.cvar
    )


@router.get("/equity-curve", response_model=EquityCurveResponse)
async def equity_curve(days: int = Query(30, ge=1, le=365), user: dict = Depends(get_current_user)):
    """获取历史收益曲线数据"""
    engine = get_engine()
    curve = engine.order_mgr.get_equity_history(days)
    return EquityCurveResponse(
        timestamps=[p.timestamp.isoformat() for p in curve],
        values=[p.equity for p in curve]
    )


@router.get("/full-snapshot")
async def full_snapshot(user: dict = Depends(get_current_user)):
    """
    返回完整的仪表盘快照数据，供前端一次性加载。
    """
    try:
        system = get_system_stats()
        health = get_health_status()
        entropy = get_entropy()
        engine = get_engine()
        acc = engine.order_mgr.get_account()
        pos = engine.order_mgr.get_position_summary()
        risk = engine.risk_monitor.get_snapshot()
        trades = engine.order_mgr.get_recent_trades(7)

        return {
            "system": system.dict(),
            "health": health.dict(),
            "entropy": entropy.dict(),
            "account": {
                "equity": acc.equity,
                "available": acc.available,
                "pnl": acc.unrealized_pnl + acc.realized_pnl_today,
                "margin_rate": f"{acc.margin_rate:.0f}%",
                "net_rate": f"{acc.net_value_ratio:.0f}%",
                "funding_cost": acc.total_funding_paid
            },
            "risk": {
                "drawdown": risk.drawdown_pct,
                "drawdown_warn": risk.drawdown_warn_pct,
                "daily_loss": risk.daily_loss_pct,
                "daily_limit": risk.daily_loss_limit_pct,
                "var_99": risk.var_99,
                "cvar": risk.cvar
            },
            "trades": {"today": len(trades), "win_rate": risk.win_rate_today, "profit": risk.realized_pnl_today},
            "recent_trades": [t.as_dict() for t in trades[-12:]] if trades else [],
            "equity": acc.equity
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取快照失败: {str(e)}")
