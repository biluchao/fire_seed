#!/usr/bin/env python3
"""
火种系统 (FireSeed) 认证与授权路由
==================================
提供：
- POST /login      静态密码登录 (返回是否需要TOTP)
- POST /verify-totp TOTP二次验证 (返回JWT令牌)
- POST /refresh     令牌刷新
- POST /logout      登出
- POST /authorize-action 敏感操作授权 (需TOTP)
"""

import time
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
import jwt
import pyotp
from fastapi import APIRouter, Depends, HTTPException, Header, Request
from pydantic import BaseModel, Field

from api.server import get_engine, get_config
from core.behavioral_logger import EventType

router = APIRouter()

# ---------- 请求模型 ----------
class LoginRequest(BaseModel):
    password_hash: str = Field(..., description="SHA-256哈希后的密码")

class TOTPVerifyRequest(BaseModel):
    totp: str = Field(..., min_length=6, max_length=6, pattern=r'^\d{6}$')

class RefreshRequest(BaseModel):
    refresh_token: str

class AuthorizeActionRequest(BaseModel):
    totp: str = Field(..., min_length=6, max_length=6, pattern=r'^\d{6}$')
    action: str = Field(..., description="操作描述")

# ---------- 工具函数 ----------
def get_auth_config():
    """获取认证配置"""
    cfg = get_config()
    return cfg.data.get("auth", {})

def get_totp_config():
    """获取TOTP配置"""
    cfg = get_config()
    return cfg.data.get("totp", {})

def verify_static_password(password_hash: str) -> bool:
    """验证静态密码哈希"""
    auth_cfg = get_auth_config()
    stored_hash = os.getenv("AUTH_PASSWORD_HASH", auth_cfg.get("password_hash", ""))
    if not stored_hash:
        raise HTTPException(status_code=500, detail="密码未配置")
    return bcrypt.checkpw(password_hash.encode(), stored_hash.encode())

def verify_totp_code(totp_code: str) -> bool:
    """验证TOTP动态码"""
    totp_cfg = get_totp_config()
    if not totp_cfg.get("enabled", False):
        # 如果未启用TOTP，直接返回True
        return True
    
    # 解密TOTP密钥
    from cryptography.fernet import Fernet
    encrypted_key = os.getenv("TOTP_ENCRYPTED_KEY", totp_cfg.get("encrypted_key", ""))
    if not encrypted_key:
        raise HTTPException(status_code=500, detail="TOTP密钥未配置")
    
    # 使用系统主密钥解密 (这里简化，实际应从安全模块获取)
    master_key = os.getenv("MASTER_ENCRYPTION_KEY", "default-key-change-in-production").encode()
    # 确保密钥长度为32字节
    master_key = master_key[:32] if len(master_key) >= 32 else master_key.ljust(32, b'\0')
    cipher = Fernet(b'base64-encoded-key-here' if b'/' not in master_key else master_key)
    secret = cipher.decrypt(encrypted_key.encode()).decode()
    
    totp = pyotp.TOTP(secret, interval=totp_cfg.get("period", 30))
    # 允许前后一个时间步长的偏差
    return totp.verify(totp_code, valid_window=1)

def create_tokens() -> tuple:
    """生成访问令牌和刷新令牌"""
    jwt_cfg = get_config().data.get("jwt", {})
    secret = os.getenv("JWT_SECRET", jwt_cfg.get("secret", "change-me-in-production"))
    algorithm = jwt_cfg.get("algorithm", "HS256")
    
    now = datetime.utcnow()
    access_expiry = now + timedelta(minutes=jwt_cfg.get("access_expiry_minutes", 30))
    refresh_expiry = now + timedelta(days=jwt_cfg.get("refresh_expiry_days", 7))
    
    access_payload = {
        "sub": "fire_seed_admin",
        "iat": now,
        "exp": access_expiry,
        "type": "access"
    }
    refresh_payload = {
        "sub": "fire_seed_admin",
        "iat": now,
        "exp": refresh_expiry,
        "type": "refresh"
    }
    
    access_token = jwt.encode(access_payload, secret, algorithm=algorithm)
    refresh_token = jwt.encode(refresh_payload, secret, algorithm=algorithm)
    return access_token, refresh_token, int(access_expiry.timestamp()), int(refresh_expiry.timestamp())

def verify_token(token: str, token_type: str = "access") -> dict:
    """验证JWT令牌"""
    jwt_cfg = get_config().data.get("jwt", {})
    secret = os.getenv("JWT_SECRET", jwt_cfg.get("secret", "change-me-in-production"))
    algorithm = jwt_cfg.get("algorithm", "HS256")
    try:
        payload = jwt.decode(token, secret, algorithms=[algorithm])
        if payload.get("type") != token_type:
            raise HTTPException(status_code=401, detail="无效的令牌类型")
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="令牌已过期")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="无效的令牌")

# ---------- 依赖注入 ----------
def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    """从Authorization头获取当前用户信息"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.split(" ")[1]
    return verify_token(token, "access")

# ---------- 路由端点 ----------
@router.post("/login")
async def login(request: LoginRequest):
    """
    第一步：静态密码验证
    返回是否需要进行TOTP二次验证
    """
    # 验证静态密码
    if not verify_static_password(request.password_hash):
        raise HTTPException(status_code=401, detail="密码错误")
    
    # 检查是否需要TOTP
    require_totp = get_totp_config().get("enabled", False)
    
    # 记录审计日志
    engine = get_engine()
    engine.behavior_log.log(
        EventType.AUTH, "Auth", "密码验证成功"
    )
    
    return {"require_totp": require_totp}


@router.post("/verify-totp")
async def verify_totp(request: TOTPVerifyRequest):
    """
    第二步：TOTP二次验证
    返回JWT令牌
    """
    if not verify_totp_code(request.totp):
        raise HTTPException(status_code=401, detail="动态码无效")
    
    access_token, refresh_token, access_exp, refresh_exp = create_tokens()
    
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": access_exp - int(time.time()),
    }


@router.post("/refresh")
async def refresh_token(request: RefreshRequest):
    """
    使用刷新令牌获取新的访问令牌
    """
    payload = verify_token(request.refresh_token, "refresh")
    access_token, refresh_token, access_exp, refresh_exp = create_tokens()
    
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": access_exp - int(time.time()),
    }


@router.post("/logout")
async def logout(user: dict = Depends(get_current_user)):
    """
    登出 (前端清除令牌即可)
    """
    engine = get_engine()
    engine.behavior_log.log(EventType.AUTH, "Auth", "用户登出")
    return {"status": "ok"}


@router.post("/authorize-action")
async def authorize_action(
    request: AuthorizeActionRequest,
    user: dict = Depends(get_current_user)
):
    """
    敏感操作授权 (需要TOTP二次验证)
    """
    if not verify_totp_code(request.totp):
        raise HTTPException(status_code=401, detail="动态码无效")
    
    engine = get_engine()
    engine.behavior_log.log(
        EventType.AUTH, "Authorize", 
        f"授权操作: {request.action}",
        {"action": request.action}
    )
    
    return {"status": "authorized", "action": request.action}


# ---------- 健康检查 (无需认证) ----------
@router.get("/status")
async def auth_status():
    """检查认证模块状态"""
    totp_cfg = get_totp_config()
    return {
        "password_set": bool(os.getenv("AUTH_PASSWORD_HASH") or get_auth_config().get("password_hash")),
        "totp_enabled": totp_cfg.get("enabled", False),
  }
