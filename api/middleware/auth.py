#!/usr/bin/env python3
"""
火种系统 (FireSeed) 认证中间件
================================
提供基于装饰器的分级鉴权：
- READONLY          无验证
- PASSWORD          静态密码
- TOTP              静态密码 + TOTP
- TOTP_PLUS_CONFIRM TOTP + 前端二次确认
- TOTP_PLUS_COUNCIL TOTP + 智能体议会投票通过
- THRESHOLD_SIGN    门限签名 + TOTP

所有凭证通过自定义 HTTP 头传递：
- X-Password        SHA-256 哈希后的静态密码
- X-TOTP            6 位动态码
- X-Confirm         二次确认标志 (任意非空)
- X-Threshold-Sig   门限签名 (暂存根)
"""

import os
from functools import wraps
from typing import Callable, Optional

from fastapi import HTTPException, Request
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN

# 导入底层验证函数（与 auth 路由共享）
from api.routes.auth import verify_static_password, verify_totp_code


class AuthLevel:
    READONLY           = "READONLY"
    PASSWORD           = "PASSWORD"
    TOTP               = "TOTP"
    TOTP_PLUS_CONFIRM  = "TOTP_PLUS_CONFIRM"
    TOTP_PLUS_COUNCIL  = "TOTP_PLUS_COUNCIL"
    THRESHOLD_SIGN     = "THRESHOLD_SIGN"


def require_auth(level: str):
    """
    装饰器工厂：为 FastAPI 端点添加指定级别的鉴权。

    用法：
        @router.post("/sensitive")
        @require_auth(AuthLevel.TOTP)
        async def endpoint(...):
            ...
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # 尝试从 kwargs 中获取 Request 对象（FastAPI 默认注入）
            request: Optional[Request] = kwargs.get("request")
            if request is None:
                # 从 args 中寻找
                for arg in args:
                    if isinstance(arg, Request):
                        request = arg
                        break
            if request is None:
                raise HTTPException(status_code=500, detail="无法获取请求对象")

            password = request.headers.get("X-Password", "")
            totp_token = request.headers.get("X-TOTP", "")
            confirm = request.headers.get("X-Confirm", "")
            threshold_sig = request.headers.get("X-Threshold-Sig", "")

            # 1. 静态密码（若要求）
            if level in (AuthLevel.PASSWORD, AuthLevel.TOTP,
                         AuthLevel.TOTP_PLUS_CONFIRM,
                         AuthLevel.TOTP_PLUS_COUNCIL,
                         AuthLevel.THRESHOLD_SIGN):
                if not password or not verify_static_password(password):
                    raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="密码错误")

            # 2. TOTP
            if level in (AuthLevel.TOTP, AuthLevel.TOTP_PLUS_CONFIRM,
                         AuthLevel.TOTP_PLUS_COUNCIL, AuthLevel.THRESHOLD_SIGN):
                if not totp_token or not verify_totp_code(totp_token):
                    raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="动态码无效")

            # 3. 二次确认
            if level == AuthLevel.TOTP_PLUS_CONFIRM:
                if not confirm or confirm.lower() != "yes":
                    raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="缺少二次确认")

            # 4. 议会投票 (OTA 等)
            if level == AuthLevel.TOTP_PLUS_COUNCIL:
                # 尝试从依赖注入获取 council，亦可通过全局引擎
                from api.server import get_engine
                engine = get_engine()
                council = engine.agent_council
                if not council.approved("sensitive_action"):
                    raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="智能体议会未通过")

            # 5. 门限签名 (API 密钥修改)
            if level == AuthLevel.THRESHOLD_SIGN:
                if not threshold_sig:
                    raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="缺少门限签名")
                # 验证签名（占位：实际需验签）
                if not _verify_threshold_signature(threshold_sig):
                    raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="门限签名无效")

            return await func(*args, **kwargs)
        return wrapper
    return decorator


def _verify_threshold_signature(signature: str) -> bool:
    """
    门限签名验证（存根）。
    实际应使用 Shamir 秘密共享或 ECDSA 门限验证。
    """
    # 简化实现：检查格式和长度
    return len(signature) >= 64 and all(c in "0123456789abcdef" for c in signature)
