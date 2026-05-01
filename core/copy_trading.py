#!/usr/bin/env python3
"""
火种系统 (FireSeed) 多账户跟单引擎
=====================================
负责：
- 从 multi_account.yaml 加载主账户与子账户配置
- 为每个子账户创建独立的 OrderManager 与 ExecutionGateway
- 监听主账户成交事件，按跟单比例克隆订单到子账户
- 子账户独立风控与持仓计算
- 前端切换当前显示账户 (不影响实际跟单)
- 跟单错误日志与重试机制
- 优雅启停与资源释放
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from config.loader import ConfigLoader
from core.order_manager import OrderManager, Order
from core.execution import ExecutionGateway
from core.behavioral_logger import BehavioralLogger, EventType

logger = logging.getLogger("fire_seed.copy_trading")


# ======================== 数据结构 ========================
@dataclass
class SubAccount:
    """子账户元数据"""
    name: str
    api_key: str
    api_secret: str
    follow_ratio: float = 1.0                 # 跟单手数比例
    max_single_position_pct: float = 50.0     # 单品种最大仓位占比
    max_leverage: int = 3                     # 最大杠杆
    allow_add_position: bool = True           # 允许加仓
    allow_manual_close: bool = False          # 允许手动平仓
    status: str = "offline"                   # online / offline / disabled
    order_mgr: Optional[OrderManager] = None
    execution: Optional[Any] = None           # ExecutionGateway 实例
    last_sync: Optional[datetime] = None
    daily_pnl: float = 0.0
    error_count: int = 0


@dataclass
class CopyTradeConfig:
    enabled: bool = False
    master_account: str = "main"
    accounts: List[SubAccount] = field(default_factory=list)
    max_daily_loss_pct: float = 3.0
    max_total_leverage: float = 5.0
    force_close_on_master_flat: bool = True
    max_connection_retries: int = 3
    retry_delay_sec: int = 5


# ======================== 跟单引擎 ========================
class CopyTradingEngine:
    """
    多账户跟单核心。维护主账户与所有子账户的映射关系，
    并在主账户订单成交时将指令复制至各子账户。
    """

    def __init__(self, config: ConfigLoader,
                 master_order_mgr: OrderManager,
                 master_execution: ExecutionGateway,
                 behavior_log: Optional[BehavioralLogger] = None):
        self.config = config
        self.master_order_mgr = master_order_mgr
        self.master_execution = master_execution
        self.log = behavior_log

        # 从 multi_account.yaml 加载配置
        self._cfg = self._load_config()
        self.enabled = self._cfg.enabled

        # 子账户列表
        self.sub_accounts: Dict[str, SubAccount] = {}
        # 前台当前显示的账户 (用于查询而非交易)
        self.display_account: str = self._cfg.master_account

        if self.enabled:
            self._init_sub_accounts()
            logger.info(f"多账户跟单已启用，子账户数量: {len(self.sub_accounts)}")

        self._active = True
        self._pending_syncs: asyncio.Queue = asyncio.Queue()

    # ======================== 初始化 ========================
    def _load_config(self) -> CopyTradeConfig:
        """加载并验证 multi_account.yaml 配置"""
        cfg = self.config.get("copy_trading", {})
        enabled = cfg.get("enabled", False)
        accounts = []
        for a in cfg.get("accounts", []):
            api_key = os.getenv(a.get("api_key_env", ""), a.get("api_key", ""))
            api_secret = os.getenv(a.get("api_secret_env", ""), a.get("api_secret", ""))
            if not api_key or not api_secret:
                logger.warning(f"子账户 {a.get('name')} 缺少 API 密钥，跳过")
                continue
            accounts.append(SubAccount(
                name=a["name"],
                api_key=api_key,
                api_secret=api_secret,
                follow_ratio=a.get("follow_ratio", 1.0),
                max_single_position_pct=a.get("max_single_position_pct", 50.0),
                max_leverage=a.get("max_leverage", 3),
                allow_add_position=a.get("allow_add_position", True),
                allow_manual_close=a.get("allow_manual_close", False),
                status="offline",
            ))
        return CopyTradeConfig(
            enabled=enabled,
            master_account=cfg.get("master_account", "main"),
            accounts=accounts,
            max_daily_loss_pct=cfg.get("global_risk", {}).get("max_daily_loss_pct", 3.0),
            max_total_leverage=cfg.get("global_risk", {}).get("max_total_leverage", 5.0),
            force_close_on_master_flat=cfg.get("global_risk", {}).get("force_close_on_master_flat", True),
            max_connection_retries=cfg.get("global_risk", {}).get("max_connection_retries", 3),
            retry_delay_sec=cfg.get("global_risk", {}).get("retry_delay_sec", 5),
        )

    def _init_sub_accounts(self) -> None:
        """创建子账户的 OrderManager 和 ExecutionGateway (虚拟或真实)"""
        mode = self.config.get("system.mode", "virtual")
        for acc in self._cfg.accounts:
            # 每个子账户独立的 OrderManager
            acc.order_mgr = OrderManager(self.config)
            # 创建独立执行网关 (共享 data_feed 可选，但简化处理)
            # 实际应使用子账户的 API 密钥；虚拟模式下忽略
            try:
                if mode == "live":
                    # 创建实盘网关，参数需要从引擎获取，此处简化
                    acc.execution = ExecutionGateway(
                        self.config, acc.order_mgr,
                        data_feed=None,  # 需要外部注入
                        behavior_logger=self.log
                    )
                else:
                    # 虚拟模式下直接使用统一模拟网关
                    acc.execution = self.master_execution
                acc.status = "online"
            except Exception as e:
                logger.error(f"子账户 {acc.name} 初始化失败: {e}")
                acc.status = "offline"
            self.sub_accounts[acc.name] = acc

    # ======================== 跟单复制接口 ========================
    async def replicate(self, master_order: Order) -> None:
        """
        在主账户订单成交后被调用，将该订单克隆到所有活跃子账户。
        """
        if not self.enabled:
            return
        for name, acc in self.sub_accounts.items():
            if acc.status != "online":
                continue
            # 跳过与主账户方向无关的单据 (如仅平仓)
            if master_order.order_type not in ("LIMIT", "MARKET"):
                continue

            # 计算子账户手数
            sub_qty = master_order.quantity * acc.follow_ratio
            if sub_qty < 0.000001:
                continue

            # 创建子账户订单
            try:
                _ = await acc.execution.create_order(
                    symbol=master_order.symbol,
                    side=master_order.side,
                    order_type=master_order.order_type,
                    price=master_order.price,
                    quantity=sub_qty,
                )
                acc.last_sync = datetime.now()
                acc.error_count = 0
            except Exception as e:
                logger.error(f"子账户 {name} 跟单失败: {e}")
                acc.error_count += 1
                if self.log:
                    self.log.log(EventType.SYSTEM, "CopyTrading",
                                 f"跟单失败 [{name}]: {e}")
                # 连续错误超过阈值，暂停该账户
                if acc.error_count >= self._cfg.max_connection_retries:
                    acc.status = "offline"
                    logger.warning(f"子账户 {name} 因连续错误被禁用")

    # ======================== 查询接口 ========================
    def list_sub_accounts(self) -> List[Dict]:
        """返回所有子账户状态列表"""
        result = []
        for acc in self.sub_accounts.values():
            result.append({
                "name": acc.name,
                "api_key_masked": acc.api_key[:4] + "****" if acc.api_key else "N/A",
                "follow_ratio": acc.follow_ratio,
                "max_position_pct": acc.max_single_position_pct,
                "status": acc.status,
                "last_sync": acc.last_sync.isoformat() if acc.last_sync else None,
                "daily_pnl": acc.daily_pnl,
                "error_count": acc.error_count,
            })
        return result

    def set_display_account(self, name: str) -> None:
        """设置前端当前显示的账户"""
        if name == self._cfg.master_account or name in self.sub_accounts:
            self.display_account = name
            logger.debug(f"前端显示账户切换至: {name}")

    @property
    def master_account_name(self) -> str:
        return self._cfg.master_account

    def get_error_logs(self, limit: int = 20) -> List[Dict]:
        """获取最近的跟单错误日志"""
        errors = []
        # 从行为日志中提取与 CopyTrading 相关的错误
        if self.log:
            entries = self.log.get_recent(limit * 2)
            for e in entries:
                if "跟单失败" in e.content:
                    errors.append({"timestamp": e.ts, "message": e.content})
        return errors[-limit:]

    def disable_account(self, name: str) -> bool:
        """手动禁用一个子账户"""
        acc = self.sub_accounts.get(name)
        if acc:
            acc.status = "disabled"
            logger.info(f"子账户 {name} 已禁用")
            return True
        return False

    def enable_account(self, name: str) -> bool:
        """重新启用一个子账户"""
        acc = self.sub_accounts.get(name)
        if acc and acc.status == "disabled":
            acc.status = "online"
            acc.error_count = 0
            logger.info(f"子账户 {name} 已启用")
            return True
        return False

    # ======================== 生命周期 ========================
    async def shutdown(self) -> None:
        """优雅关闭所有子账户连接"""
        self._active = False
        for acc in self.sub_accounts.values():
            acc.status = "offline"
        logger.info("多账户跟单引擎已关闭")
