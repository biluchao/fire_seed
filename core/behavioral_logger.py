#!/usr/bin/env python3
"""
火种系统 (FireSeed) 行为日志模块
==================================
提供统一的事件记录与管理：
- 事件分级：INFO / WARN / ERROR / CRITICAL
- 丰富的事件类型（涵盖市场、信号、订单、风控、进化、智能体、议会等）
- 内存缓存：最近 N 条日志，支持快速查询
- 持久化：写入 SQLite 数据库，按日期分片
- 实时推送：通过 WebSocket 广播至前端面板
- 自动化维护：每日归档、过期清理
"""

import json
import logging
import sqlite3
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# 配置日志
logger = logging.getLogger("fire_seed.behavior")


class EventType(Enum):
    """行为事件类型枚举"""
    MARKET_STATE = "市场状态识别"
    SIGNAL_EVAL = "信号评估"
    ORDER_ACTION = "订单动作"
    ORDER_CONFIRM = "订单确认"
    POSITION_CHANGE = "仓位变更"
    RISK_ACTION = "风控干预"
    STRATEGY_SWITCH = "策略切换"
    SYSTEM = "系统事件"
    AGENT = "智能体"
    EVOLUTION = "进化"
    OTA = "OTA更新"
    AUTH = "认证操作"
    # 议会抗辩相关
    PARLIAMENT_PROPOSE = "议会提议"
    PARLIAMENT_CHALLENGE = "议会挑战"
    PARLIAMENT_VERDICT = "议会裁决"


class EventLevel(Enum):
    """事件严重级别"""
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass
class LogEntry:
    """单条日志记录"""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    ts: float = field(default_factory=time.time)
    level: EventLevel = EventLevel.INFO
    event_type: EventType = EventType.SYSTEM
    module: str = ""
    content: str = ""
    snapshot: Dict[str, Any] = field(default_factory=dict)

    @property
    def ts_str(self) -> str:
        return datetime.fromtimestamp(self.ts).strftime("%H:%M:%S")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "ts_str": self.ts_str,
            "level": self.level.value,
            "type": self.event_type.value,
            "module": self.module,
            "content": self.content,
        }


class BehavioralLogger:
    """
    全系统行为日志管理器。
    所有模块通过此单例记录运行时事件，支持：
    - 最近 500 条内存缓存（按需扩展）
    - SQLite 持久化（按日期分片，保留 30 天）
    - WebSocket 推送（通过回调函数注入）
    """

    def __init__(self,
                 max_memory: int = 500,
                 db_dir: str = "data/logs",
                 retention_days: int = 30):
        self.max_memory = max_memory
        self.db_dir = Path(db_dir)
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days

        # 内存环形队列
        self._memory: deque = deque(maxlen=max_memory)

        # 前端推送回调（由 WebSocket 管理器注入）
        self._ws_callbacks: List[Callable[[Dict], None]] = []

        # 初始化数据库
        self._init_db()

        logger.info(f"行为日志初始化完成，容量: {max_memory}")

    # ======================== 数据库管理 ========================
    def _db_path(self, date_str: Optional[str] = None) -> Path:
        """获取指定日期的数据库文件路径"""
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")
        return self.db_dir / f"behavior_{date_str}.db"

    def _init_db(self) -> None:
        """创建当前日期的数据库表"""
        db_path = self._db_path()
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    ts REAL NOT NULL,
                    level TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    module TEXT NOT NULL,
                    content TEXT NOT NULL,
                    snapshot TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_level ON events(level)
            """)

    # ======================== 日志记录 ========================
    def log(self,
            event_type: EventType,
            module: str,
            content: str,
            level: EventLevel = EventLevel.INFO,
            snapshot: Optional[Dict[str, Any]] = None) -> None:
        """
        记录一条行为日志。
        :param event_type: 事件类型
        :param module: 产生事件的模块名
        :param content: 事件描述
        :param level: 严重级别（默认INFO）
        :param snapshot: 可选的上下文数据快照
        """
        entry = LogEntry(
            ts=time.time(),
            level=level,
            event_type=event_type,
            module=module,
            content=content,
            snapshot=snapshot or {},
        )
        # 写入内存
        self._memory.append(entry)

        # 持久化
        self._persist(entry)

        # 推送到前端
        self._push(entry)

        # Python 日志系统同步
        log_level = getattr(logging, level.value, logging.INFO)
        logger.log(log_level, f"[{event_type.value}] [{module}] {content}")

    def info(self, event_type: EventType, module: str, content: str, snapshot: dict = None) -> None:
        self.log(event_type, module, content, EventLevel.INFO, snapshot)

    def warn(self, event_type: EventType, module: str, content: str, snapshot: dict = None) -> None:
        self.log(event_type, module, content, EventLevel.WARN, snapshot)

    def error(self, event_type: EventType, module: str, content: str, snapshot: dict = None) -> None:
        self.log(event_type, module, content, EventLevel.ERROR, snapshot)

    def critical(self, event_type: EventType, module: str, content: str, snapshot: dict = None) -> None:
        self.log(event_type, module, content, EventLevel.CRITICAL, snapshot)

    # ======================== 持久化 ========================
    def _persist(self, entry: LogEntry) -> None:
        """将日志写入 SQLite"""
        try:
            db_path = self._db_path()
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    """INSERT INTO events
                       (id, ts, level, event_type, module, content, snapshot)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (entry.id, entry.ts, entry.level.value,
                     entry.event_type.value, entry.module,
                     entry.content, json.dumps(entry.snapshot, default=str))
                )
        except Exception as e:
            logger.error(f"日志持久化失败: {e}")

    # ======================== 前端推送 ========================
    def subscribe(self, callback: Callable[[Dict], None]) -> None:
        """注册前端推送回调"""
        self._ws_callbacks.append(callback)

    def _push(self, entry: LogEntry) -> None:
        """将日志推送给所有已注册的前端连接"""
        data = entry.to_dict()
        for cb in self._ws_callbacks:
            try:
                cb(data)
            except Exception as e:
                logger.warning(f"日志推送失败: {e}")

    def should_flush(self) -> bool:
        """判断是否需要主动向数据库刷新（通常不需要，此方法为兼容旧接口）"""
        return False

    async def flush_to_frontend(self) -> None:
        """显式将所有待推送日志发给前端（兼容旧接口）"""
        pass

    def flush_to_db(self) -> None:
        """强制将内存日志写入数据库（关闭时调用，当前已实时写入）"""
        pass

    # ======================== 查询接口 ========================
    def get_recent(self, limit: int = 50,
                   level: Optional[str] = None,
                   module: Optional[str] = None) -> List[LogEntry]:
        """从内存中获取最近 N 条日志，支持过滤"""
        result = []
        for entry in reversed(self._memory):
            if len(result) >= limit:
                break
            if level and entry.level.value != level:
                continue
            if module and entry.module != module:
                continue
            result.append(entry)
        return list(reversed(result))

    def query_db(self,
                 start_time: Optional[datetime] = None,
                 end_time: Optional[datetime] = None,
                 level: Optional[str] = None,
                 event_type: Optional[str] = None,
                 limit: int = 200) -> List[Dict]:
        """从数据库中查询历史日志（支持时间范围和过滤）"""
        if start_time is None:
            start_time = datetime.now() - timedelta(days=7)
        if end_time is None:
            end_time = datetime.now()

        results = []
        current = start_time.date()
        while current <= end_time.date():
            db_path = self._db_path(current.strftime("%Y%m%d"))
            if db_path.exists():
                try:
                    with sqlite3.connect(str(db_path)) as conn:
                        sql = "SELECT * FROM events WHERE ts BETWEEN ? AND ?"
                        params: List[Any] = [start_time.timestamp(), end_time.timestamp()]
                        if level:
                            sql += " AND level = ?"
                            params.append(level)
                        if event_type:
                            sql += " AND event_type = ?"
                            params.append(event_type)
                        sql += " ORDER BY ts DESC LIMIT ?"
                        params.append(limit)
                        cursor = conn.execute(sql, params)
                        for row in cursor.fetchall():
                            results.append({
                                "id": row[0],
                                "ts": row[1],
                                "level": row[2],
                                "event_type": row[3],
                                "module": row[4],
                                "content": row[5],
                                "snapshot": row[6],
                            })
                except Exception as e:
                    logger.warning(f"查询数据库 {db_path} 失败: {e}")
            current += timedelta(days=1)

        # 按时间排序并限制数量
        results.sort(key=lambda x: x["ts"], reverse=True)
        return results[:limit]

    # ======================== 自动化维护 ========================
    async def archive_expired(self) -> None:
        """清理超过保留期限的历史数据库文件"""
        cutoff = datetime.now() - timedelta(days=self.retention_days)
        cutoff_str = cutoff.strftime("%Y%m%d")
        for db_file in self.db_dir.glob("behavior_*.db"):
            date_str = db_file.stem.replace("behavior_", "")
            if date_str < cutoff_str:
                try:
                    import gzip
                    compressed_path = db_file.with_suffix('.db.gz')
                    with open(db_file, 'rb') as f_in:
                        with gzip.open(compressed_path, 'wb') as f_out:
                            f_out.write(f_in.read())
                    db_file.unlink()
                    logger.info(f"档案已压缩: {compressed_path}")
                except Exception as e:
                    logger.warning(f"日志归档失败 ({db_file}): {e}")
