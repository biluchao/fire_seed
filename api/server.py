#!/usr/bin/env python3
"""
火种系统 (FireSeed) 后端API服务器
==============================
基于 FastAPI 的生产级 HTTP/WebSocket 服务。
提供：
- 前端静态页面托管
- RESTful API 接口
- WebSocket 实时数据推送
- 操作鉴权中间件
- 生命周期管理 (连接引擎、优雅关闭)
"""

import asyncio
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------- 导入路由 ----------
from api.routes.auth import router as auth_router
from api.routes.status import router as status_router
from api.routes.orders import router as orders_router
from api.routes.risk import router as risk_router
from api.routes.strategy import router as strategy_router
from api.routes.ota import router as ota_router
from api.routes.admin import router as admin_router
from api.routes.assistant import router as assistant_router
from api.routes.multi_account import router as multi_account_router
from api.routes.dashboard_detail import router as dashboard_router

# ---------- 项目内部模块 ----------
from config.loader import ConfigLoader
from core.engine import FireSeedEngine  # 主引擎
from api.websocket_manager import WebSocketManager
from utils.logger import setup_logging

# ---------- 全局状态 ----------
config: Optional[ConfigLoader] = None
engine: Optional[FireSeedEngine] = None
ws_manager: WebSocketManager = WebSocketManager()
logger = logging.getLogger("fire_seed.api")


# ======================== 生命周期管理 ========================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动/关闭时的资源管理"""
    global config, engine

    # 加载配置
    config_path = os.getenv("FIRE_SEED_CONFIG", "config/settings.yaml")
    config = ConfigLoader(config_path)
    setup_logging("api", config.get("system.log_level", "INFO"))

    logger.info("正在启动火种引擎...")
    engine = FireSeedEngine(config_path=config_path)
    # 引擎在后台运行
    loop = asyncio.get_event_loop()
    engine_task = loop.create_task(engine.run(), name="engine")

    logger.info("火种API服务器已就绪")
    yield  # 此处应用运行

    # ---- 关闭流程 ----
    logger.info("正在关闭火种系统...")
    if engine:
        engine._running = False
        await asyncio.sleep(0.5)
        # 给予引擎时间优雅退出
        try:
            await asyncio.wait_for(engine_task, timeout=10)
        except asyncio.TimeoutError:
            logger.warning("引擎退出超时，强制终止")
    logger.info("系统已关闭")


# ======================== FastAPI 实例 ========================
app = FastAPI(
    title="火种量化交易系统 API",
    version="3.0.0-spartan",
    description="FireSeed Cognitive Trading Engine",
    lifespan=lifespan,
    docs_url="/docs" if os.getenv("ENV") != "production" else None,
    redoc_url=None,
)

# ======================== 中间件 ========================
# CORS 跨域配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # 生产环境应限制为特定域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局异常捕获中间件
@app.middleware("http")
async def catch_exceptions(request: Request, call_next):
    try:
        response = await call_next(request)
        return response
    except Exception as e:
        logger.error(f"请求异常: {request.url} - {str(e)}")
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal Server Error", "error": str(e) if config and config.get("system.debug", False) else None}
        )

# 鉴权中间件 (简化版，实际由各路由自行调用 auth 模块)
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # 排除不需要鉴权的路径
    public_paths = ["/docs", "/openapi.json", "/static", "/api/auth/login", "/api/auth/verify-totp", "/health"]
    if any(request.url.path.startswith(p) for p in public_paths):
        return await call_next(request)

    # 这里可以加入全局JWT验证，但为了灵活性，由各路由自行处理
    return await call_next(request)


# ======================== 挂载静态前端文件 ========================
frontend_path = Path(__file__).parent.parent / "frontend"
if frontend_path.exists():
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")
    @app.get("/")
    async def root():
        from fastapi.responses import FileResponse
        return FileResponse(frontend_path / "index.html")
    logger.info(f"前端静态文件已挂载: {frontend_path}")
else:
    logger.warning(f"前端目录未找到: {frontend_path}，请检查部署路径")


# ======================== 注册路由 ========================
app.include_router(auth_router, prefix="/api/auth", tags=["认证"])
app.include_router(status_router, prefix="/api/status", tags=["系统状态"])
app.include_router(orders_router, prefix="/api/orders", tags=["订单管理"])
app.include_router(risk_router, prefix="/api/risk", tags=["风险控制"])
app.include_router(strategy_router, prefix="/api/strategy", tags=["策略管理"])
app.include_router(ota_router, prefix="/api/ota", tags=["OTA更新"])
app.include_router(admin_router, prefix="/api/admin", tags=["管理员"])
app.include_router(assistant_router, prefix="/api/assistant", tags=["火种助手"])
app.include_router(multi_account_router, prefix="/api/multi-account", tags=["多账户跟单"])
app.include_router(dashboard_router, prefix="/api/dashboard", tags=["仪表盘详情"])


# ======================== 健康检查端点 ========================
@app.get("/health")
async def health_check():
    """Kubernetes / 负载均衡器 健康探测"""
    return {"status": "ok", "timestamp": int(__import__("time").time())}


# ======================== WebSocket 实时推送 ========================
@app.websocket("/ws/status")
async def websocket_status(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            # 接收客户端心跳
            data = await websocket.receive_text()
            if data == 'ping':
                await websocket.send_text('pong')
            # 可扩展接收客户端指令
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket异常: {e}")
        ws_manager.disconnect(websocket)


# ======================== 全局引擎引用 (供路由使用) ========================
def get_engine() -> FireSeedEngine:
    """获取当前运行中的引擎实例"""
    if engine is None:
        raise RuntimeError("引擎尚未初始化")
    return engine


def get_config() -> ConfigLoader:
    """获取配置实例"""
    if config is None:
        raise RuntimeError("配置尚未加载")
    return config


# ======================== 启动入口 ========================
def run():
    """使用 uvicorn 启动服务"""
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))
    ssl_cert = os.getenv("SSL_CERT")
    ssl_key = os.getenv("SSL_KEY")

    uvicorn_config = {
        "app": "api.server:app",
        "host": host,
        "port": port,
        "log_config": None,
        "access_log": os.getenv("ENV") != "production",
        "reload": os.getenv("ENV") == "development",
    }
    if ssl_cert and ssl_key:
        uvicorn_config["ssl_certfile"] = ssl_cert
        uvicorn_config["ssl_keyfile"] = ssl_key

    uvicorn.run(**uvicorn_config)


if __name__ == "__main__":
    run()
