#!/usr/bin/env python3
"""
火种系统 (FireSeed) 仪表盘详细数据路由
======================================
为前端二级详情大卡片提供完整的账户历史与统计信息。
"""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.server import get_engine
from api.routes.auth import get_current_user

router = APIRouter()


# ======================== 响应模型 ========================
class AccountDetail(BaseModel):
    account_balance: float = Field(..., description="当前账户余额 (USDT)")
    position_value: float = Field(..., description="当前持仓总市值 (USDT)")
    historical_deposit: float = Field(..., description="历史累计存入额")
    historical_pnl: float = Field(..., description="历史累计总盈亏")
    historical_volume: float = Field(..., description="历史累计总手数")
    historical_win_rate: float = Field(..., description="历史总胜率 (0-100)")
    last_month_pnl: float = Field(..., description="最近一个月总盈亏")
    estimated_slippage: float = Field(..., description="总预估滑点成本 (USDT)")
    last_updated: str = Field(..., description="数据更新时间")


# ======================== 端点 ========================
@router.get("/detail", response_model=AccountDetail)
async def get_account_detail(user: dict = Depends(get_current_user)):
    """
    获取账户的详细统计信息，用于仪表盘二级大卡片展示。
    """
    engine = get_engine()
    try:
        # 获取账户核心数据
        acc = engine.order_mgr.get_account()
        pos = engine.order_mgr.get_position_summary()

        # 计算当前持仓市值（简化：持仓数量 * 标记价格）
        position_value = pos.position_value if pos else 0.0
        balance = acc.equity  # 总权益

        # 历史数据（从订单管理器的统计模块获取）
        stats = engine.order_mgr.get_historical_stats() if hasattr(engine.order_mgr, 'get_historical_stats') else {}

        # 若 get_historical_stats 未实现，则回退为模拟值
        historical_deposit = stats.get('total_deposits', acc.initial_deposit) if hasattr(acc, 'initial_deposit') else stats.get('total_deposits', 500_000.0)
        historical_pnl = stats.get('total_pnl', 0.0)
        historical_volume = stats.get('total_volume', 0.0)
        historical_win_rate = stats.get('win_rate', 0.0) * 100  # 假设存储的是小数

        # 最近一个月盈亏
        last_month_pnl = engine.order_mgr.get_pnl_for_period(days=30)

        # 预估滑点费用（从风控模块或执行网关获取）
        slippage = engine.risk_monitor.estimated_total_slippage if hasattr(engine.risk_monitor, 'estimated_total_slippage') else 0.0

        return AccountDetail(
            account_balance=round(balance, 2),
            position_value=round(position_value, 2),
            historical_deposit=round(historical_deposit, 2),
            historical_pnl=round(historical_pnl, 2),
            historical_volume=round(historical_volume, 4),
            historical_win_rate=round(historical_win_rate, 2),
            last_month_pnl=round(last_month_pnl, 2),
            estimated_slippage=round(slippage, 2),
            last_updated=datetime.now().isoformat()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取账户详情失败: {str(e)}")
