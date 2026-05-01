#!/usr/bin/env python3
"""
火种系统 (FireSeed) 火种助手路由
==================================
提供对话式交互的 API 接口，支持：
- 本地规则引擎快速响应
- 云端大语言模型 (LLM) 接入
- 自动降级与上下文管理
- 对话历史记录
"""

from typing import Optional, List, Dict, Any
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Body
from pydantic import BaseModel, Field

from api.server import get_engine, get_config
from api.routes.auth import get_current_user
from core.behavioral_logger import EventType

router = APIRouter()


# ======================== 数据模型 ========================
class ChatMessage(BaseModel):
    role: str = Field(..., regex="^(user|assistant|system)$", description="消息角色")
    content: str = Field(..., min_length=1, description="消息内容")

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="用户输入")
    history: List[ChatMessage] = Field(default=[], description="对话历史，可选")
    use_cloud: Optional[bool] = Field(False, description="是否强制使用云端模型")
    context: Optional[Dict[str, Any]] = Field(default={}, description="额外上下文信息")

class ChatResponse(BaseModel):
    reply: str = Field(..., description="助手回复")
    source: str = Field(..., description="回复来源: local / cloud")
    timestamp: str = Field(..., description="回复时间 ISO8601")


# ======================== 本地规则引擎 ========================
# 与前端 assistant.js 保持一致的本地关键词匹配逻辑
LOCAL_RULES = [
    {
        "keywords": ["状态", "系统状态", "运行状态", "健康", "是否正常"],
        "handler": "get_system_status"
    },
    {
        "keywords": ["持仓", "持有", "仓位", "头寸", "敞口"],
        "handler": "get_position_summary"
    },
    {
        "keywords": ["订单", "成交", "交易记录", "最近交易"],
        "handler": "get_recent_trades"
    },
    {
        "keywords": ["盈亏", "利润", "盈利", "亏损", "赚了", "亏了"],
        "handler": "get_pnl_summary"
    },
    {
        "keywords": ["风险", "回撤", "熔断", "VaR"],
        "handler": "get_risk_summary"
    },
    {
        "keywords": ["模式", "切换", "激进", "稳健", "虚拟", "实盘"],
        "handler": "get_mode_info"
    },
    {
        "keywords": ["帮助", "你能做什么", "功能", "命令"],
        "handler": "get_help"
    },
]


def match_local_rules(message: str) -> Optional[str]:
    """尝试匹配本地规则，返回处理函数名称或None"""
    lower_msg = message.lower()
    for rule in LOCAL_RULES:
        for kw in rule["keywords"]:
            if kw in lower_msg:
                return rule["handler"]
    return None


def execute_local_handler(handler_name: str, message: str) -> str:
    """执行本地处理函数生成回复"""
    engine = get_engine()

    if handler_name == "get_system_status":
        health = engine.self_check.run()
        return (
            f"系统运行正常。\n"
            f"健康评分: {health.score}/100\n"
            f"CPU温度: {health.cpu_temp}°C\n"
            f"磁盘IO: {health.disk_io}ms\n"
            f"运行时间: {health.uptime}\n"
            f"当前模式: {engine.mode}"
        )

    elif handler_name == "get_position_summary":
        pos = engine.order_mgr.get_position_summary()
        if pos and pos.size > 0:
            return f"当前持仓：{pos.side} {pos.size}，开仓均价 {pos.entry_price}，浮动盈亏 {pos.unrealized_pnl:.2f} USDT"
        return "当前无持仓。"

    elif handler_name == "get_recent_trades":
        trades = engine.order_mgr.get_recent_orders(limit=5)
        if not trades:
            return "暂无最近的交易记录。"
        lines = []
        for t in trades:
            pnl_str = f"盈亏: {t.pnl:.2f}" if t.pnl is not None else ""
            lines.append(f"{t.side} {t.symbol} {t.quantity} @ {t.price} {pnl_str}")
        return "最近5笔交易：\n" + "\n".join(lines)

    elif handler_name == "get_pnl_summary":
        acc = engine.order_mgr.get_account()
        return (
            f"总权益: ${acc.equity:,.0f}\n"
            f"浮动盈亏: ${acc.unrealized_pnl:,.0f}\n"
            f"今日已实现盈亏: ${acc.realized_pnl_today:,.0f}"
        )

    elif handler_name == "get_risk_summary":
        risk = engine.risk_monitor.get_snapshot()
        return (
            f"回撤: {risk.drawdown_pct:.1f}% / 预警 {risk.drawdown_warn_pct:.1f}%\n"
            f"日亏损: {risk.daily_loss_pct:.1f}% / 熔断 {risk.daily_loss_limit_pct:.1f}%\n"
            f"VaR(99): ${risk.var_99:,.0f}\n"
            f"CVaR: ${risk.cvar:,.0f}"
        )

    elif handler_name == "get_mode_info":
        mode = engine.mode
        strategy_mode = get_config().get("system.strategy_mode", "moderate")
        if any(kw in message for kw in ["切换", "改", "设置"]):
            return "模式切换需要通过前端面板操作，并需要操作密码和TOTP验证。"
        return f"当前运行模式：{mode}，策略风格：{strategy_mode}"

    elif handler_name == "get_help":
        return (
            "我是火种助手，您可以询问：\n"
            "• 系统状态 / 健康\n"
            "• 当前持仓\n"
            "• 订单 / 交易记录\n"
            "• 盈亏情况\n"
            "• 风险评估\n"
            "• 模式信息\n\n"
            "复杂问题我会调用云端模型进行分析。"
        )

    return "抱歉，我暂时无法处理这个请求。"


# ======================== 云端 LLM 调用 ========================
async def call_cloud_llm(message: str, history: List[Dict[str, str]], context: Dict[str, Any]) -> str:
    """调用配置的云端大语言模型"""
    config = get_config()
    engine = get_engine()

    # 获取LLM配置
    llm_config = config.get("llm", {})
    provider = llm_config.get("provider", "deepseek")

    # 构建上下文
    system_context = {
        "mode": engine.mode,
        "strategy_mode": config.get("system.strategy_mode", "moderate"),
        "time": datetime.now().isoformat(),
    }
    system_context.update(context)

    try:
        if hasattr(engine, 'llm_gateway'):
            reply = await engine.llm_gateway.chat(
                message=message,
                history=history,
                context=system_context,
                provider=provider
            )
            return reply
        else:
            return "云端模型未配置，请检查 llm_gateway 模块。"
    except Exception as e:
        engine.behavior_log.log(EventType.SYSTEM, "Assistant", f"云端调用失败: {str(e)}")
        return f"调用云端模型时出错: {str(e)}。请稍后重试或使用本地命令。"


# ======================== API 端点 ========================
@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, user: dict = Depends(get_current_user)):
    """
    与火种助手对话。
    优先使用本地规则引擎快速响应，若未命中且允许/强制则调用云端模型。
    """
    engine = get_engine()
    message = request.message.strip()
    history = [h.dict() for h in request.history] if request.history else []
    use_cloud = request.use_cloud

    reply = None
    source = "local"

    # 1. 如果未强制云端，先尝试本地规则
    if not use_cloud:
        handler_name = match_local_rules(message)
        if handler_name:
            reply = execute_local_handler(handler_name, message)

    # 2. 本地未命中时，或强制云端时，调用云端模型
    if reply is None:
        source = "cloud"
        reply = await call_cloud_llm(message, history, request.context)

    # 记录行为日志
    engine.behavior_log.log(
        EventType.SYSTEM, "Assistant",
        f"用户: {message[:100]}... → 回复来源: {source}",
        {"message": message[:200], "source": source}
    )

    return ChatResponse(
        reply=reply or "抱歉，助手暂未生成回复。",
        source=source,
        timestamp=datetime.now().isoformat()
    )


@router.get("/providers")
async def list_providers(user: dict = Depends(get_current_user)):
    """列出可用的云端LLM提供商及其状态"""
    config = get_config()
    llm_config = config.get("llm", {})
    providers = llm_config.get("providers", {})
    result = {}
    for name, cfg in providers.items():
        result[name] = {
            "enabled": cfg.get("enabled", False),
            "model": cfg.get("model", "unknown"),
            "endpoint": cfg.get("endpoint", "N/A")
        }
    return {"providers": result}
