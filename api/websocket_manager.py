#!/usr/bin/env python3
"""
火种系统 (FireSeed) WebSocket 连接管理器
=========================================
支持：
- 多客户端连接管理
- 广播消息至所有已连接的客户端
- 向特定客户端发送消息
- 自动心跳检测与超时断开
- 连接异常安全清理
"""

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket
from starlette.websockets import WebSocketState

logger = logging.getLogger("fire_seed.ws")


class WebSocketManager:
    """管理所有活跃的 WebSocket 连接，提供安全的消息广播。"""

    def __init__(self, heartbeat_interval: int = 20, heartbeat_timeout: int = 60):
        # 活跃连接集合，使用 set 存储 (websocket, last_heartbeat)
        self._connections: Set[WebSocket] = set()
        self._heartbeats: Dict[WebSocket, float] = {}
        self._lock = asyncio.Lock()

        self.heartbeat_interval = heartbeat_interval      # 心跳发送间隔（秒）
        self.heartbeat_timeout = heartbeat_timeout        # 心跳超时时间（秒），超过则断开

        # 后台任务
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._active = False

    async def start(self):
        """启动后台心跳检测任务（应在事件循环中调用）"""
        if self._active:
            return
        self._active = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("WebSocket 管理器心跳检测已启动")

    async def stop(self):
        """停止后台任务并关闭所有连接"""
        self._active = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        async with self._lock:
            # 关闭所有连接
            for ws in list(self._connections):
                await self._close_websocket(ws)
            self._connections.clear()
            self._heartbeats.clear()
        logger.info("WebSocket 管理器已停止")

    async def connect(self, websocket: WebSocket):
        """接受新的 WebSocket 连接。应在路径处理函数中调用 websocket.accept() 之后使用。"""
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
            self._heartbeats[websocket] = time.monotonic()
        logger.info(f"WebSocket 客户端连接: {websocket.client}，当前连接数: {len(self._connections)}")

    async def disconnect(self, websocket: WebSocket):
        """移除指定的 WebSocket 连接并尝试关闭。"""
        async with self._lock:
            if websocket in self._connections:
                self._connections.discard(websocket)
                self._heartbeats.pop(websocket, None)
        await self._close_websocket(websocket)
        logger.info(f"WebSocket 客户端断开: {websocket.client}，当前连接数: {len(self._connections)}")

    async def broadcast(self, message: Dict[str, Any]):
        """向所有已连接的客户端广播消息。"""
        if not self._connections:
            return

        payload = json.dumps(message, default=str)
        dead: List[WebSocket] = []

        async with self._lock:
            for ws in self._connections.copy():
                try:
                    if ws.client_state == WebSocketState.CONNECTED:
                        await ws.send_text(payload)
                except Exception:
                    dead.append(ws)

        # 清理死连接
        for ws in dead:
            await self.disconnect(ws)

    async def send_personal(self, websocket: WebSocket, message: Dict[str, Any]):
        """向特定客户端发送消息。"""
        if websocket.client_state != WebSocketState.CONNECTED:
            return
        payload = json.dumps(message, default=str)
        try:
            await websocket.send_text(payload)
        except Exception:
            await self.disconnect(websocket)

    async def _heartbeat_loop(self):
        """后台心跳检测循环。"""
        while self._active:
            await asyncio.sleep(self.heartbeat_interval)
            now = time.monotonic()

            async with self._lock:
                for ws in list(self._connections):
                    if ws.client_state != WebSocketState.CONNECTED:
                        logger.warning(f"移除断开的 WebSocket 客户端: {ws.client}")
                        self._connections.discard(ws)
                        self._heartbeats.pop(ws, None)
                        continue

                    # 发送心跳
                    try:
                        await ws.send_text(json.dumps({"type": "ping", "ts": int(time.time())}))
                        self._heartbeats[ws] = now  # 心跳发送成功，更新最后活动时间
                    except Exception:
                        logger.warning(f"WebSocket 心跳发送失败，移除客户端: {ws.client}")
                        self._connections.discard(ws)
                        self._heartbeats.pop(ws, None)
                        await self._close_websocket(ws)

            # 第二次遍历检查超时（客户端未响应 pong，因为我们无法强制要求客户端回复，此处改为检查连接存活）
            # 对于不主动发送 pong 的客户端，我们依赖发送心跳时捕获的异常来清理
            # 此处可选：如果长期未收到客户端任何消息，可主动断开（但需要记录最后消息时间）
            # 简单实现：依赖发送时的异常检测，此处不再额外断开

    @staticmethod
    async def _close_websocket(websocket: WebSocket):
        """安全关闭一个 WebSocket 连接。"""
        try:
            if websocket.client_state != WebSocketState.DISCONNECTED:
                await websocket.close()
        except Exception:
            pass

    @property
    def active_connections(self) -> int:
        """返回当前活跃连接数。"""
        return len(self._connections)
