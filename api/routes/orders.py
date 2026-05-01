#!/usr/bin/env python3
"""
火种系统 (FireSeed) 订单管理路由
================================
提供订单的完整生命周期管理：
- GET  /recent           获取最近订单列表
- GET  /active           获取当前活动订单
- POST /create           手动创建订单 (需授权)
- POST /cancel/{order_id} 撤销指定订单
- POST /emergency-close  紧急一键平仓 (需二次确认)
- GET  /history          按条件查询历史订单
"""

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Path
from pydantic import BaseModel, Field, validator

from api.server import get_engine, get_config
from api.routes.auth import get_current_user
from core.behavioral_logger import EventType

router = APIRouter()


# ======================== 数据模型 ========================
class OrderResponse(BaseModel):
    order_id: str = Field(..., description="订单ID")
    symbol: str = Field(..., description="交易对")
    side: str = Field(..., description="方向: buy/sell")
    order_type: str = Field(..., description="订单类型: LIMIT/MARKET/STOP")
    price: float = Field(..., description="委托价格")
    quantity: float = Field(..., description="委托数量")
    filled_quantity: float = Field(0, description="已成交数量")
    status: str = Field(..., description="状态: OPEN/FILLED/CANCELLED/REJECTED")
    created_at: str = Field(..., description="创建时间 ISO8601")
    updated_at: Optional[str] = Field(None, description="更新时间")
    pnl: Optional[float] = Field(None, description="平仓盈亏 (仅已成交)")
    slippage_bps: Optional[float] = Field(None, description="滑点 (基点)")

class CreateOrderRequest(BaseModel):
    symbol: str = Field(..., description="交易对")
    side: str = Field(..., regex="^(buy|sell)$")
    order_type: str = Field("LIMIT", regex="^(LIMIT|MARKET|STOP)$")
    price: Optional[float] = Field(None, description="限价单价格 (市价单可空)")
    quantity: float = Field(..., gt=0, description="委托数量")
    take_profit: Optional[float] = Field(None, description="止盈价")
    stop_loss: Optional[float] = Field(None, description="止损价")

class CancelOrderResponse(BaseModel):
    order_id: str
    status: str
    message: str

class EmergencyCloseRequest(BaseModel):
    confirm: bool = Field(True, description="二次确认标志")
    totp: Optional[str] = Field(None, min_length=6, max_length=6, regex=r'^\d{6}$')

class TradeRecord(BaseModel):
    trade_id: str
    order_id: str
    symbol: str
    side: str
    price: float
    quantity: float
    pnl: float
    fee: float
    timestamp: str

# ======================== 查询端点 ========================
@router.get("/recent", response_model=List[OrderResponse])
async def get_recent_orders(
    limit: int = Query(12, ge=1, le=100),
    symbol: Optional[str] = Query(None),
    user: dict = Depends(get_current_user)
):
    """获取最近N笔订单 (默认12笔)"""
    engine = get_engine()
    try:
        orders = engine.order_mgr.get_recent_orders(limit=limit, symbol=symbol)
        return [
            OrderResponse(
                order_id=o.id,
                symbol=o.symbol,
                side=o.side,
                order_type=o.type,
                price=o.price,
                quantity=o.quantity,
                filled_quantity=o.filled,
                status=o.status,
                created_at=o.created_at.isoformat(),
                updated_at=o.updated_at.isoformat() if o.updated_at else None,
                pnl=o.pnl if o.status == "FILLED" else None,
                slippage_bps=o.slippage_bps
            )
            for o in orders
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取订单列表失败: {str(e)}")


@router.get("/active", response_model=List[OrderResponse])
async def get_active_orders(user: dict = Depends(get_current_user)):
    """获取当前活动订单 (未成交的挂单)"""
    engine = get_engine()
    try:
        orders = engine.order_mgr.get_active_orders()
        return [
            OrderResponse(
                order_id=o.id,
                symbol=o.symbol,
                side=o.side,
                order_type=o.type,
                price=o.price,
                quantity=o.quantity,
                filled_quantity=o.filled,
                status=o.status,
                created_at=o.created_at.isoformat(),
                updated_at=o.updated_at.isoformat() if o.updated_at else None,
                pnl=None,
                slippage_bps=None
            )
            for o in orders
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取活动订单失败: {str(e)}")


@router.get("/history", response_model=List[TradeRecord])
async def get_trade_history(
    days: int = Query(7, ge=1, le=90),
    symbol: Optional[str] = Query(None),
    user: dict = Depends(get_current_user)
):
    """按时间范围查询成交记录"""
    engine = get_engine()
    try:
        trades = engine.order_mgr.get_trade_history(days=days, symbol=symbol)
        return [
            TradeRecord(
                trade_id=t.id,
                order_id=t.order_id,
                symbol=t.symbol,
                side=t.side,
                price=t.price,
                quantity=t.quantity,
                pnl=t.pnl,
                fee=t.fee,
                timestamp=t.timestamp.isoformat()
            )
            for t in trades
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取交易历史失败: {str(e)}")


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order_detail(
    order_id: str = Path(..., description="订单ID"),
    user: dict = Depends(get_current_user)
):
    """获取指定订单详情"""
    engine = get_engine()
    order = engine.order_mgr.get_order_by_id(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单未找到")
    return OrderResponse(
        order_id=order.id,
        symbol=order.symbol,
        side=order.side,
        order_type=order.type,
        price=order.price,
        quantity=order.quantity,
        filled_quantity=order.filled,
        status=order.status,
        created_at=order.created_at.isoformat(),
        updated_at=order.updated_at.isoformat() if order.updated_at else None,
        pnl=order.pnl if order.status == "FILLED" else None,
        slippage_bps=order.slippage_bps
    )


# ======================== 操作端点 ========================
@router.post("/create", response_model=OrderResponse, status_code=201)
async def create_order(
    request: CreateOrderRequest,
    user: dict = Depends(get_current_user)
):
    """手动创建订单 (需要高级权限)"""
    engine = get_engine()
    try:
        order = engine.execution.create_order(
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            price=request.price,
            quantity=request.quantity,
            take_profit=request.take_profit,
            stop_loss=request.stop_loss
        )
        # 记录行为日志
        engine.behavior_log.log(
            EventType.ORDER, "ManualCreate", 
            f"手动下单: {request.side} {request.quantity} @ {request.price}",
            {"order_id": order.id}
        )
        return OrderResponse(
            order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            order_type=order.type,
            price=order.price,
            quantity=order.quantity,
            filled_quantity=order.filled,
            status=order.status,
            created_at=order.created_at.isoformat()
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建订单失败: {str(e)}")


@router.post("/cancel/{order_id}", response_model=CancelOrderResponse)
async def cancel_order(
    order_id: str = Path(..., description="要撤销的订单ID"),
    user: dict = Depends(get_current_user)
):
    """撤销指定订单"""
    engine = get_engine()
    success, message = engine.execution.cancel_order(order_id)
    if not success:
        raise HTTPException(status_code=400, detail=message)
    
    engine.behavior_log.log(
        EventType.ORDER, "Cancel",
        f"撤销订单: {order_id}",
        {"order_id": order_id}
    )
    return CancelOrderResponse(order_id=order_id, status="CANCELLED", message=message)


@router.post("/emergency-close")
async def emergency_close(
    request: EmergencyCloseRequest,
    user: dict = Depends(get_current_user)
):
    """
    紧急一键平仓所有持仓。
    需要：
    1. 二次确认 (confirm=True)
    2. TOTP二次验证 (如果启用)
    """
    if not request.confirm:
        raise HTTPException(status_code=400, detail="需要二次确认")
    
    engine = get_engine()
    
    # 验证TOTP (如果系统启用)
    auth_config = get_config().data.get("auth", {})
    if auth_config.get("totp_enabled", False):
        if not request.totp:
            raise HTTPException(status_code=400, detail="需要TOTP动态码")
        from api.routes.auth import verify_totp_code
        if not verify_totp_code(request.totp):
            raise HTTPException(status_code=401, detail="动态码无效")
    
    try:
        result = await engine.execution.close_all_positions()
        engine.behavior_log.log(
            EventType.ORDER, "EmergencyClose",
            f"紧急平仓完成: {result['closed_count']} 个仓位",
            {"result": result}
        )
        return {
            "status": "success",
            "message": f"已平仓 {result['closed_count']} 个仓位",
            "details": result
        }
    except Exception as e:
        engine.behavior_log.log(EventType.RISK, "EmergencyClose", f"平仓失败: {e}")
        raise HTTPException(status_code=500, detail=f"紧急平仓失败: {str(e)}")


@router.get("/stats/summary")
async def trading_stats_summary(user: dict = Depends(get_current_user)):
    """获取交易统计摘要 (今日)"""
    engine = get_engine()
    stats = engine.order_mgr.get_daily_trading_stats()
    return {
        "today_trades": stats.count,
        "win_rate": round(stats.win_rate * 100, 1),
        "profit": stats.realized_pnl,
        "best_trade": stats.best_trade,
        "worst_trade": stats.worst_trade,
        "average_holding_minutes": stats.avg_holding_time
      }
