#!/usr/bin/env python3
"""
火种系统 (FireSeed) 上下文隔离模块
====================================
为每个交易周期提供严格隔离的数据视图，防止跨周期的信息污染。
所有传入数据均经过深拷贝，确保：
- 1 分钟引擎无法访问 15 分钟 K 线
- 不同周期的指标计算完全独立
- 冻结后的视图不可被意外修改
"""

import copy
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("fire_seed.context")


@dataclass
class Kline:
    """1 分钟 K 线数据结构"""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class OrderBookSnapshot:
    """订单簿快照"""
    timestamp: datetime
    bids: List[tuple]   # [(price, volume), ...]
    asks: List[tuple]
    spread: float = 0.0
    mid_price: float = 0.0


class IsolatedDataView:
    """
    单个周期的隔离数据视图。
    提供该周期专属的 K 线历史、订单簿快照与指标缓存。
    一旦冻结，所有写入操作将抛出异常，确保不因外部误操作修改数据。
    """

    def __init__(self, timeframe: str, max_klines: int = 500,
                 max_orderbooks: int = 1000):
        self.timeframe = timeframe
        self._max_klines = max_klines
        self._max_orderbooks = max_orderbooks

        # 内部存储（深拷贝保证不可变）
        self._klines: deque = deque(maxlen=max_klines)
        self._orderbooks: deque = deque(maxlen=max_orderbooks)
        self._indicators: Dict[str, Any] = {}

        self._frozen = False
        self._created_at = datetime.now()

    # ======================== 数据写入 ========================
    def add_kline(self, kline: Kline) -> None:
        """添加一根 K 线（深拷贝后存储）"""
        if self._frozen:
            raise RuntimeError(f"DataView({self.timeframe}) 已冻结，不可写入")
        self._klines.append(copy.deepcopy(kline))
        # 可在此触发因子更新的钩子，但通常由感知模块外置调用
        logger.debug(f"[{self.timeframe}] K线添加: {kline.timestamp} close={kline.close}")

    def add_orderbook(self, ob: OrderBookSnapshot) -> None:
        """添加一份订单簿快照（深拷贝存储）"""
        if self._frozen:
            raise RuntimeError(f"DataView({self.timeframe}) 已冻结，不可写入")
        self._orderbooks.append(copy.deepcopy(ob))

    def set_indicator(self, name: str, value: Any) -> None:
        """存储计算结果到指标缓存"""
        if self._frozen:
            raise RuntimeError(f"DataView({self.timeframe}) 已冻结，不可修改指标")
        self._indicators[name] = value

    # ======================== 数据读取 ========================
    def get_klines(self, n: Optional[int] = None) -> List[Kline]:
        """获取最近 N 根 K 线（深拷贝返回，防止外部篡改）"""
        if n is None:
            return [copy.deepcopy(k) for k in self._klines]
        return [copy.deepcopy(k) for k in list(self._klines)[-n:]]

    def get_orderbook(self) -> Optional[OrderBookSnapshot]:
        """获取最新订单簿快照（深拷贝返回）"""
        if self._orderbooks:
            return copy.deepcopy(self._orderbooks[-1])
        return None

    def get_indicator(self, name: str, default: Any = None) -> Any:
        """读取指标值"""
        return self._indicators.get(name, default)

    # ======================== 工具方法 ========================
    def freeze(self) -> None:
        """冻结视图，后续任何写入操作将抛出异常"""
        self._frozen = True
        logger.info(f"DataView({self.timeframe}) 已冻结")

    def unfreeze(self) -> None:
        """解冻视图，允许继续写入"""
        self._frozen = False
        logger.info(f"DataView({self.timeframe}) 已解冻")

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    @property
    def kline_count(self) -> int:
        return len(self._klines)

    @property
    def age_seconds(self) -> float:
        """该视图自创建以来经过的秒数"""
        return (datetime.now() - self._created_at).total_seconds()

    def count_ma_crosses(self, ma_period: int = 12, lookback: int = 10) -> int:
        """统计最近 N 根 K 线中穿越 MA 的次数"""
        closes = [k.close for k in list(self._klines)[-lookback - ma_period:]]
        if len(closes) < ma_period + 2:
            return 0
        crosses = 0
        for i in range(-lookback, -1):
            if i + ma_period > 0:
                break
            ma = sum(closes[i - ma_period:i]) / ma_period if i >= ma_period else 0
            ma_prev = sum(closes[i - 1 - ma_period:i - 1]) / ma_period if i - 1 >= ma_period else 0
            if (closes[i] - ma) * (closes[i - 1] - ma_prev) < 0:
                crosses += 1
        return crosses

    def avg_volume(self, n: int = 20) -> float:
        """最近 N 根 K 线的平均成交量"""
        klines = list(self._klines)[-n:]
        if not klines:
            return 0.0
        return sum(k.volume for k in klines) / len(klines)


class ContextFactory:
    """
    上下文工厂。
    为每个策略周期创建并管理完全隔离的 IsolatedDataView。
    提供隔离性自动化测试，确保跨周期无引用泄漏。
    """

    def __init__(self, config: Optional[Dict] = None):
        self._contexts: Dict[str, IsolatedDataView] = {}
        self.config = config or {}

    def create(self, timeframe: str) -> IsolatedDataView:
        """创建（或获取已有的）指定周期的数据视图"""
        if timeframe not in self._contexts:
            self._contexts[timeframe] = IsolatedDataView(timeframe)
            logger.info(f"创建隔离上下文: {timeframe}")
        return self._contexts[timeframe]

    def get(self, timeframe: str) -> Optional[IsolatedDataView]:
        """获取已有的上下文，不存在则返回 None"""
        return self._contexts.get(timeframe)

    def list_timeframes(self) -> List[str]:
        """列出所有已注册的周期"""
        return list(self._contexts.keys())

    def validate_isolation(self) -> tuple:
        """
        自动化隔离性测试。
        返回 (是否通过, 诊断信息列表)
        """
        errors = []
        tfs = list(self._contexts.keys())
        for i, tf1 in enumerate(tfs):
            for tf2 in tfs[i + 1:]:
                ctx1 = self._contexts[tf1]
                ctx2 = self._contexts[tf2]
                # 检查内部容器的对象标识是否不同
                if ctx1._klines is ctx2._klines:
                    errors.append(f"K线缓冲区共享: {tf1} ↔ {tf2}")
                if ctx1._orderbooks is ctx2._orderbooks:
                    errors.append(f"订单簿缓冲区共享: {tf1} ↔ {tf2}")
                if ctx1._indicators is ctx2._indicators:
                    errors.append(f"指标字典共享: {tf1} ↔ {tf2}")
        passed = len(errors) == 0
        if not passed:
            logger.error(f"上下文隔离验证失败: {errors}")
        return passed, errors

    def freeze_all(self) -> None:
        """冻结全部上下文（常用于快照备份或回放）"""
        for ctx in self._contexts.values():
            ctx.freeze()
        logger.info("所有上下文已冻结")

    def unfreeze_all(self) -> None:
        """解冻全部上下文"""
        for ctx in self._contexts.values():
            ctx.unfreeze()
        logger.info("所有上下文已解冻")

    def reset(self, timeframe: Optional[str] = None) -> None:
        """
        重置指定周期的上下文，或全部上下文。
        通常在检测到市场结构性断点时调用。
        """
        if timeframe:
            if timeframe in self._contexts:
                self._contexts[timeframe] = IsolatedDataView(timeframe)
                logger.info(f"已重置上下文: {timeframe}")
        else:
            self._contexts.clear()
            logger.info("所有上下文已重置")
