#!/usr/bin/env python3
"""
火种系统 (FireSeed) API 依赖注入模块
========================================
为 FastAPI 路由提供共享依赖（引擎实例、配置、日志记录器等）。
使用 FastAPI 的 Depends 机制，确保每个请求都能获得正确的单例对象。
"""

import logging
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request

from api.server import get_engine, get_config
from core.engine import FireSeedEngine
from config.loader import ConfigLoader

logger = logging.getLogger("fire_seed.api.deps")


# ---------- 引擎依赖 ----------
async def require_engine() -> FireSeedEngine:
    """
    注入交易引擎实例。
    若引擎尚未启动，抛出 503 服务不可用。
    """
    try:
        engine = get_engine()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=f"引擎不可用: {str(e)}")
    return engine


# ---------- 配置依赖 ----------
async def require_config() -> ConfigLoader:
    """
    注入系统配置实例。
    """
    try:
        config = get_config()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=f"配置未加载: {str(e)}")
    return config


# ---------- 可选的当前用户（无鉴权时返回 None） ----------
async def get_optional_user(
    authorization: Optional[str] = Header(None)
) -> Optional[dict]:
    """
    尝试从 Authorization 头中解析当前用户。
    不做强制鉴权，仅返回解析结果或 None。
    适用于同时支持公开与认证访问的端点。
    """
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.split(" ", 1)[1]
    try:
        # 延迟导入避免循环
        from api.routes.auth import verify_token
        payload = verify_token(token, "access")
        return payload
    except HTTPException:
        return None
    except Exception:
        return None
