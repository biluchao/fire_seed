#!/usr/bin/env python3
"""
火种系统 (FireSeed) 跟单协调官智能体 (CopyTradeCoordinator)
================================================================
监控多账户跟单系统的同步状态：
- 子账户连接状态
- 跟单延迟与成功率
- 异常账户自动暂停/恢复
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict

from api.server import get_engine
from core.behavioral_logger import BehavioralLogger, EventType

logger = logging.getLogger("fire_seed.copy_trade_coordinator")


class CopyTradeCoordinator:
    """跟单协调官"""

    def __init__(self, behavior_log=None, notifier=None):
        self.behavior_log = behavior_log
        self.notifier = notifier

    async def evaluate(self) -> Dict[str, Any]:
        engine = get_engine()
        if engine and hasattr(engine, 'copy_trading'):
            subs = engine.copy_trading.list_sub_accounts()
            offline = [s for s in subs if s.get('status') != 'online']
            return {
                "total": len(subs),
                "offline": len(offline),
                "timestamp": datetime.now().isoformat(),
            }
        return {"status": "no_data"}
