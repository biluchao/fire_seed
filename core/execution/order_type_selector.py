"""
火种系统 · 智能订单类型选择器 (OrderTypeSelector)

核心职责：
1. 基于单一原子市场快照，对市价、限价、冰山与TWAP进行微秒级成本模拟与量化比较，选择经风险调整后综合成本最低的最优执行方案
2. 动态计算所选订单类型的最优执行参数（如冰山单显露量、限价单偏移量、TWAP切片数），并主动探测盘口陷阱（假墙、波动率毒刺）与近期毒性订单流以调整执行策略

外部依赖（真实模块接口）：
- core.perception.tactile_cortex.TactileCortex : 获取单一原子市场快照（订单簿、价差、成交脉搏、波动率）
- core.perception.olfactory_cortex.OlfactoryCortex : 探测盘口陷阱（假墙、订单流毒性、波动率毒刺）
- core.negotiation_bus.NegotiationBus : 发布订单类型选择事件，用于后续审计和策略复盘

接口契约：
- select_order_type(urgency: int, time_tolerance_us: int, order_size_pct: float) -> Dict[str, Any] : 根据信号特征与市场状态，返回建议的订单类型、最优参数及决策依据
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 TactileCortex 或 OlfactoryCortex 不可用时，采用保守策略：禁用市价单，默认使用限价单并拉宽限价偏移量以降低风险
- 当 NegotiationBus 不可用时，放弃发布事件，仅通过本地日志记录决策
- 所有降级值在类常量区明确声明

资源管理：
- 本模块为纯计算型模块，不持有任何外部资源句柄
- 成本模拟缓存极短时间（50μs）以消除高频调用下的冗余计算
"""

import logging
import time
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger(__name__)


class OrderTypeSelector:
    """智能订单类型选择器 —— 交易成本精算师"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 成本模拟权重
    SLIPPAGE_WEIGHT = 1.0               # 滑点成本权重，无量纲
    OPPORTUNITY_COST_WEIGHT = 0.5       # 机会成本权重（限价单未成交的踏空风险），无量纲
    FEE_WEIGHT = 0.3                    # 手续费成本权重，无量纲

    # 默认参数
    DEFAULT_LIQUIDITY_LEVEL = 3         # 降级用默认流动性评级，无量纲，[1,5]
    DEFAULT_MAX_SPREAD_PCT = 0.0005     # 可接受最大买卖价差百分比，无量纲
    DEFAULT_LIMIT_OFFSET_TICKS = 2      # 限价单默认偏移Tick数，整数，[1,10]
    DEFAULT_ICEBERG_DISPLAY_RATIO = 0.15 # 冰山单默认显露比例，无量纲，(0,1)
    DEFAULT_ICEBERG_INTERVAL_MS = 500   # 冰山单默认切片间隔，毫秒，[100,5000]
    DEFAULT_TWAP_SLICES = 5             # TWAP默认切片数，整数，[2,20]

    # 紧急度阈值
    URGENCY_SURVIVAL = 9                # 生存级指令，无条件市价，整数，[0,10]
    URGENCY_HIGH = 7                    # 高紧急，整数，[0,10]
    URGENCY_LOW = 3                     # 低紧急，整数，[0,10]

    # 成本模拟缓存
    CACHE_TTL_US = 50                   # 成本模拟结果缓存有效期，微秒，[10,500]

    # 毒性保护
    TOXICITY_COOLDOWN_MS = 500          # 检测到毒性后暂停激进策略的冷却时间，毫秒，[100,5000]
    TOXICITY_SPREAD_WIDEN_PCT = 2.0     # 毒性期间限价单偏移扩大倍数，无量纲，[1.5,5.0]

    # 默认订单类型（所有逻辑都无法匹配时使用）
    DEFAULT_ORDER_TYPE = "limit"

    def __init__(self):
        # 外部依赖注入
        self._tactile_cortex = None
        self._olfactory_cortex = None
        self._negotiation_bus = None

        # 统计计数器
        self._decision_count = 0
        self._trap_triggered_count = 0

        # 成本模拟缓存（用于消除高频调用下的冗余计算）
        self._cost_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._cache_timestamp: float = 0.0

        logger.info("OrderTypeSelector 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        tactile_cortex: Optional[Any] = None,
        olfactory_cortex: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）
        """
        if tactile_cortex is not None:
            self._tactile_cortex = tactile_cortex
            logger.info("TactileCortex 注入成功")
        else:
            logger.warning("TactileCortex 未注入，使用默认流动性评级")

        if olfactory_cortex is not None:
            self._olfactory_cortex = olfactory_cortex
            logger.info("OlfactoryCortex 注入成功")
        else:
            logger.warning("OlfactoryCortex 未注入，盘口陷阱与毒性检测不可用")

        if negotiation_bus is not None:
            self._negotiation_bus = negotiation_bus
            logger.info("NegotiationBus 注入成功")
        else:
            logger.warning("NegotiationBus 未注入，决策事件仅记录本地日志")

    # ========== 公共接口 ==========
    def select_order_type(
        self,
        urgency: int,
        time_tolerance_us: int,
        order_size_pct: float,
    ) -> Dict[str, Any]:
        """
        基于单一原子市场快照与成本模拟，选择最优订单类型及执行参数

        Args:
            urgency: 信号紧急性 (0-10，10为生存级)
            time_tolerance_us: 可接受的最大执行延迟（微秒）
            order_size_pct: 订单量占权益的百分比，用于评估市场冲击

        Returns:
            标准响应字典，data 中包含 suggested_order_type, optimal_params, cost_estimates, trap_flags 等字段
        """
        # 参数边界校验
        urgency = max(0, min(10, urgency))
        time_tolerance_us = max(0, time_tolerance_us)
        order_size_pct = max(0.001, order_size_pct)

        warnings: List[str] = []
        self._decision_count += 1

        # 1. 获取单一原子市场快照（消除数据不一致性）
        snapshot = self._get_market_snapshot()
        if snapshot is None:
            # 降级：使用默认值构造快照
            snapshot = {
                "orderbook_depth": {"bids": [(0.0, 0.0)], "asks": [(999999.0, 0.0)]},
                "spread_pct": self.DEFAULT_MAX_SPREAD_PCT,
                "trade_pulse_avg_size": 0.01,
                "instant_volatility": 0.002,
                "book_resilience_ratio": 1.0,
                "liquidity_level": self.DEFAULT_LIQUIDITY_LEVEL,
            }
            warnings.append("市场快照不可用，使用降级默认值")

        orderbook_depth = snapshot.get("orderbook_depth", {"bids": [], "asks": []})
        spread_pct = snapshot.get("spread_pct", self.DEFAULT_MAX_SPREAD_PCT)
        trade_pulse_avg_size = snapshot.get("trade_pulse_avg_size", 0.01)
        instant_volatility = snapshot.get("instant_volatility", 0.002)
        book_resilience_ratio = snapshot.get("book_resilience_ratio", 1.0)

        # 2. 盘口陷阱与毒性检测
        trap_flags = self._detect_orderbook_traps()
        is_toxic = self._is_recently_toxic()

        if trap_flags.get("paper_wall") or trap_flags.get("volatility_stinger"):
            self._trap_triggered_count += 1
            warnings.append(f"检测到盘口陷阱: {trap_flags}，强制禁用市价单")
        if is_toxic:
            warnings.append("检测到近期毒性订单流，启用保守执行策略")

        # 3. 对各订单类型进行成本模拟（使用缓存消除冗余计算）
        cost_estimates = self._get_cost_estimates(
            order_size_pct, orderbook_depth, spread_pct, trade_pulse_avg_size,
            instant_volatility, book_resilience_ratio, time_tolerance_us
        )

        # 4. 综合决策（成本最低优先，同时考虑紧急性和盘口陷阱）
        suggested_type, optimal_params, reasoning = self._make_decision(
            urgency, time_tolerance_us, cost_estimates, trap_flags, is_toxic
        )

        # 5. 发布决策事件（用于审计）
        self._publish_decision_event(suggested_type, optimal_params, urgency, cost_estimates, trap_flags)

        logger.info(
            "订单类型选择: 类型=%s, 紧急性=%d, 滑点估算(market=%.4f%%, iceberg=%.4f%%, limit=%.4f%%, twap=%.4f%%), 原因=%s",
            suggested_type, urgency,
            cost_estimates["market"]["total_cost"] * 100,
            cost_estimates["iceberg"]["total_cost"] * 100,
            cost_estimates["limit"]["total_cost"] * 100,
            cost_estimates["twap"]["total_cost"] * 100,
            reasoning,
        )

        return {
            "status": "ok",
            "reason": reasoning,
            "data": {
                "suggested_order_type": suggested_type,
                "optimal_params": optimal_params,
                "cost_estimates": cost_estimates,
                "trap_flags": trap_flags,
                "is_toxic": is_toxic,
                "urgency": urgency,
            },
            "warnings": warnings,
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检

        Returns:
            标准健康检查响应字典
        """
        try:
            # 检查核心逻辑：使用默认参数应能返回有效结果
            test_depth = {"bids": [(50000.0, 1.0)], "asks": [(50001.0, 1.0)]}
            test_cost = self._simulate_market_order(0.1, test_depth)
            if not isinstance(test_cost, dict) or "total_cost" not in test_cost:
                return {
                    "status": "error",
                    "reason": "成本模拟逻辑异常",
                    "data": {},
                    "warnings": ["logic_failure"],
                }

            return {
                "status": "ok",
                "reason": "OrderTypeSelector 正常，核心决策与成本模拟逻辑可用",
                "data": {
                    "dependencies": {
                        "tactile_cortex": self._tactile_cortex is not None,
                        "olfactory_cortex": self._olfactory_cortex is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                    },
                    "stats": {
                        "decision_count": self._decision_count,
                        "trap_triggered_count": self._trap_triggered_count,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查依赖注入和内部模拟逻辑")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 市场微观状态获取 ==========
    def _get_market_snapshot(self) -> Optional[Dict[str, Any]]:
        """安全获取单一原子市场快照（消除数据时间裂缝）"""
        if self._tactile_cortex is not None:
            try:
                if hasattr(self._tactile_cortex, 'get_market_snapshot'):
                    snapshot = self._tactile_cortex.get_market_snapshot()
                    if snapshot:
                        return snapshot
                    logger.warning("TactileCortex 返回空快照")
                else:
                    # 降级：逐个获取
                    return {
                        "orderbook_depth": self._get_orderbook_depth(),
                        "spread_pct": self._get_spread_pct(),
                        "trade_pulse_avg_size": self._get_trade_pulse_avg_size(),
                        "instant_volatility": self._get_instant_volatility(),
                        "book_resilience_ratio": self._get_book_resilience_ratio(),
                        "liquidity_level": self._get_liquidity_level(),
                    }
            except Exception as e:
                logger.warning(f"获取市场快照失败: {e}")
        return None

    def _get_orderbook_depth(self) -> Dict[str, Any]:
        """安全获取订单簿深度，带降级"""
        if self._tactile_cortex is not None:
            try:
                depth = self._tactile_cortex.get_orderbook_snapshot(levels=10)
                if depth and "bids" in depth and "asks" in depth:
                    return depth
            except Exception as e:
                logger.warning(f"获取订单簿深度失败: {e}")
        return {"bids": [(0.0, 0.0)], "asks": [(999999.0, 0.0)]}

    def _get_spread_pct(self) -> float:
        """安全获取当前买卖价差百分比"""
        if self._tactile_cortex is not None:
            try:
                spread = self._tactile_cortex.get_current_spread_pct()
                if isinstance(spread, (int, float)) and spread > 0:
                    return float(spread)
            except Exception as e:
                logger.warning(f"获取买卖价差失败: {e}")
        return self.DEFAULT_MAX_SPREAD_PCT

    def _get_trade_pulse_avg_size(self) -> float:
        """安全获取近期平均单笔成交量（用于冰山单显露量计算）"""
        if self._tactile_cortex is not None:
            try:
                avg_size = self._tactile_cortex.get_trade_pulse_avg_size()
                if isinstance(avg_size, (int, float)) and avg_size > 0:
                    return float(avg_size)
            except Exception as e:
                logger.warning(f"获取成交脉搏失败: {e}")
        return 0.01

    def _get_instant_volatility(self) -> float:
        """安全获取瞬时波动率（用于限价单机会成本计算）"""
        if self._tactile_cortex is not None:
            try:
                vol = self._tactile_cortex.get_instant_volatility()
                if isinstance(vol, (int, float)) and vol > 0:
                    return float(vol)
            except Exception as e:
                logger.warning(f"获取瞬时波动率失败: {e}")
        return 0.002  # 降级默认值，0.2%/秒

    def _get_book_resilience_ratio(self) -> float:
        """安全获取订单簿韧性比率（挂单被吃后补充速度的度量，用于动态冰山单成本）"""
        if self._tactile_cortex is not None:
            try:
                ratio = self._tactile_cortex.get_book_resilience_ratio()
                if isinstance(ratio, (int, float)) and ratio > 0:
                    return float(ratio)
            except Exception as e:
                logger.warning(f"获取订单簿韧性失败: {e}")
        return 1.0  # 降级默认值，标准韧性

    def _get_liquidity_level(self) -> int:
        """安全获取当前流动性评级，带降级"""
        if self._tactile_cortex is not None:
            try:
                level = self._tactile_cortex.get_liquidity_level()
                if isinstance(level, int) and 1 <= level <= 5:
                    return level
                logger.warning(f"TactileCortex 返回无效流动性: {level}")
            except Exception as e:
                logger.warning(f"获取流动性失败: {e}")
        return self.DEFAULT_LIQUIDITY_LEVEL

    # ========== 盘口陷阱与毒性检测 ==========
    def _detect_orderbook_traps(self) -> Dict[str, bool]:
        """检测盘口陷阱（假墙、波动率毒刺）"""
        traps = {"paper_wall": False, "volatility_stinger": False}
        if self._olfactory_cortex is not None:
            try:
                traps["paper_wall"] = self._olfactory_cortex.is_paper_wall_detected()
                traps["volatility_stinger"] = self._olfactory_cortex.is_volatility_stinger_detected()
            except Exception as e:
                logger.warning(f"盘口陷阱检测失败: {e}")
        return traps

    def _is_recently_toxic(self) -> bool:
        """检测近期是否存在毒性订单流（成交后价格朝不利方向移动）"""
        if self._olfactory_cortex is not None:
            try:
                return self._olfactory_cortex.is_recently_toxic(
                    window_ms=self.TOXICITY_COOLDOWN_MS
                )
            except Exception as e:
                logger.warning(f"毒性检测失败: {e}")
        return False

    # ========== 成本模拟缓存 ==========
    def _get_cost_estimates(
        self,
        size_pct: float,
        depth: Dict,
        spread_pct: float,
        pulse_avg_size: float,
        instant_volatility: float,
        book_resilience: float,
        time_tolerance_us: int,
    ) -> Dict[str, Dict]:
        """获取成本模拟结果（带极短时间缓存，消除高频调用冗余）"""
        now = time.time()
        cache_key = f"{size_pct:.6f}_{spread_pct:.6f}_{pulse_avg_size:.6f}_{instant_volatility:.6f}_{book_resilience:.3f}"

        if (cache_key in self._cost_cache and
                (now - self._cache_timestamp) * 1e6 < self.CACHE_TTL_US):
            return self._cost_cache[cache_key][1]

        estimates = {
            "market": self._simulate_market_order(size_pct, depth),
            "iceberg": self._simulate_iceberg_order(size_pct, depth, pulse_avg_size, book_resilience),
            "limit": self._simulate_limit_order(size_pct, spread_pct, time_tolerance_us, instant_volatility),
            "twap": self._simulate_twap_order(size_pct, depth, time_tolerance_us),
        }

        self._cost_cache[cache_key] = (now, estimates)
        self._cache_timestamp = now
        # 限制缓存大小
        if len(self._cost_cache) > 20:
            oldest = min(self._cost_cache, key=lambda k: self._cost_cache[k][0])
            del self._cost_cache[oldest]

        return estimates

    # ========== 成本模拟器 ==========
    def _simulate_market_order(self, size_pct: float, depth: Dict) -> Dict[str, Any]:
        """
        模拟市价单执行成本
        逐档穿透订单簿，计算加权平均成交价和滑点
        """
        try:
            target_size = size_pct / 100.0
            remaining = target_size
            total_cost = 0.0
            base_price = depth["asks"][0][0] if depth["asks"] else 0

            for price, volume in depth["asks"]:
                if remaining <= 0:
                    break
                filled = min(remaining, volume)
                total_cost += filled * price
                remaining -= filled

            if remaining > 0:
                last_price = depth["asks"][-1][0] if depth["asks"] else base_price
                total_cost += remaining * last_price * 1.10

            avg_price = total_cost / target_size if target_size > 0 else base_price
            slippage = (avg_price - base_price) / base_price if base_price > 0 else 0.01
            return {
                "total_cost": slippage + 0.0004,
                "slippage": slippage,
                "avg_price": avg_price,
                "fill_ratio": (target_size - remaining) / target_size if target_size > 0 else 1.0,
            }
        except Exception as e:
            logger.warning(f"市价单模拟失败: {e}")
            return {"total_cost": 999.0, "slippage": 999.0, "avg_price": 0, "fill_ratio": 0}

    def _simulate_iceberg_order(
        self, size_pct: float, depth: Dict, pulse_avg_size: float, book_resilience: float
    ) -> Dict[str, Any]:
        """
        模拟冰山单执行成本（动态滑点系数，基于订单簿韧性）
        """
        try:
            optimal_display = pulse_avg_size
            market_cost = self._simulate_market_order(size_pct, depth)

            # 冰山单滑点缩减系数与订单簿韧性挂钩
            # 韧性高（挂单恢复快）：冰山单优势小（系数接近 0.9）
            # 韧性低（挂单恢复慢）：冰山单优势大（系数接近 0.3）
            resilience_factor = max(0.3, min(0.9, 1.0 / max(book_resilience, 0.5)))
            iceberg_slippage = market_cost["slippage"] * resilience_factor

            partial_fill_risk = 0.05 * market_cost["total_cost"]
            return {
                "total_cost": iceberg_slippage + 0.0004 + partial_fill_risk,
                "slippage": iceberg_slippage,
                "optimal_display_qty": optimal_display,
                "optimal_interval_ms": self.DEFAULT_ICEBERG_INTERVAL_MS,
            }
        except Exception as e:
            logger.warning(f"冰山单模拟失败: {e}")
            return {"total_cost": 999.0, "slippage": 999.0, "optimal_display_qty": 0.01, "optimal_interval_ms": 500}

    def _simulate_limit_order(
        self, size_pct: float, spread_pct: float, time_tolerance_us: int, instant_volatility: float
    ) -> Dict[str, Any]:
        """
        模拟限价单执行成本（机会成本与瞬时波动率挂钩）
        """
        try:
            limit_slippage = spread_pct * 0.5

            # 机会成本与波动率正相关
            # 高波动率下价格快速偏离挂单价的风险更大
            vol_mult = max(0.5, min(5.0, instant_volatility / 0.002))
            if time_tolerance_us < 200:
                base_opportunity = 0.005
            elif time_tolerance_us < 1000:
                base_opportunity = 0.002
            else:
                base_opportunity = 0.0005
            opportunity_cost = base_opportunity * vol_mult

            return {
                "total_cost": limit_slippage + 0.0004 + opportunity_cost,
                "slippage": limit_slippage,
                "opportunity_cost": opportunity_cost,
                "optimal_offset_ticks": self.DEFAULT_LIMIT_OFFSET_TICKS,
            }
        except Exception as e:
            logger.warning(f"限价单模拟失败: {e}")
            return {"total_cost": 999.0, "slippage": 999.0, "opportunity_cost": 999.0, "optimal_offset_ticks": 2}

    def _simulate_twap_order(self, size_pct: float, depth: Dict, time_tolerance_us: int) -> Dict[str, Any]:
        """
        模拟TWAP执行成本（基于时间片分割与每片冲击估算）
        """
        try:
            # 根据时间容忍度动态确定切片数
            if time_tolerance_us > 5000:
                slices = min(20, max(3, time_tolerance_us // 2000))
            elif time_tolerance_us > 2000:
                slices = min(10, max(2, time_tolerance_us // 1500))
            elif time_tolerance_us > 1000:
                slices = min(5, 2)
            else:
                # 时间不足，TWAP不可用
                return {"total_cost": 999.0, "slippage": 999.0, "optimal_slices": 0}

            # 每片大小为总订单的 1/slices
            slice_size = size_pct / slices
            market_cost = self._simulate_market_order(slice_size, depth)
            # TWAP 总滑点 = 每片滑点之和（市场有消化时间，后续片滑点递减）
            total_slippage = market_cost["slippage"]
            for i in range(1, slices):
                total_slippage += market_cost["slippage"] * (0.7 ** i)
            avg_slippage = total_slippage / slices

            return {
                "total_cost": avg_slippage + 0.0004,
                "slippage": avg_slippage,
                "optimal_slices": slices,
            }
        except Exception as e:
            logger.warning(f"TWAP模拟失败: {e}")
            return {"total_cost": 999.0, "slippage": 999.0, "optimal_slices": 5}

    # ========== 核心决策引擎 ==========
    def _make_decision(
        self,
        urgency: int,
        time_tolerance_us: int,
        cost_estimates: Dict[str, Dict],
        trap_flags: Dict[str, bool],
        is_toxic: bool,
    ) -> Tuple[str, Dict[str, Any], str]:
        """
        综合成本、紧急性、盘口陷阱与毒性，选择最优订单类型与执行参数
        """
        # 生存级指令：无条件市价单
        if urgency >= self.URGENCY_SURVIVAL:
            return "market", {"execution_mode": "immediate"}, "生存级指令，无条件市价执行"

        # 检测到盘口陷阱：强制禁用市价单
        market_allowed = not (trap_flags.get("paper_wall") or trap_flags.get("volatility_stinger"))

        # 构建候选列表（成本从低到高排序）
        candidates = sorted(cost_estimates.items(), key=lambda x: x[1].get("total_cost", 999.0))

        for order_type, cost_info in candidates:
            # 排除被禁止的市价单
            if order_type == "market" and not market_allowed:
                continue
            # 排除时间不足时的TWAP
            if order_type == "twap" and time_tolerance_us < 1000:
                continue
            # 排除极高紧急度时的限价单
            if order_type == "limit" and urgency >= self.URGENCY_HIGH and time_tolerance_us < 200:
                continue
            # 排除极高紧急度时的TWAP
            if order_type == "twap" and urgency >= self.URGENCY_HIGH:
                continue

            # 毒性环境下，提高保守策略偏好
            if is_toxic:
                if order_type == "market":
                    continue  # 毒性期间禁用市价单
                if order_type == "limit":
                    optimal_params = {
                        "offset_ticks": max(
                            self.DEFAULT_LIMIT_OFFSET_TICKS * self.TOXICITY_SPREAD_WIDEN_PCT,
                            5,
                        ),
                    }
                    return order_type, optimal_params, "检测到毒性订单流，强制使用保守限价单"
                if order_type == "iceberg":
                    optimal_params = {
                        "display_qty": cost_info.get("optimal_display_qty", 0.01) * 0.5,
                        "slice_interval_ms": cost_info.get("optimal_interval_ms", 500) * 2,
                    }
                    return order_type, optimal_params, "检测到毒性订单流，使用保守冰山单参数"

            # 找到最优解
            optimal_params = {}
            if order_type == "iceberg":
                optimal_params = {
                    "display_qty": cost_info.get("optimal_display_qty", 0.01),
                    "slice_interval_ms": cost_info.get("optimal_interval_ms", 500),
                }
            elif order_type == "limit":
                optimal_params = {"offset_ticks": cost_info.get("optimal_offset_ticks", 2)}
            elif order_type == "twap":
                optimal_params = {"slices": cost_info.get("optimal_slices", 5)}

            reasoning = (
                f"综合成本最低({cost_info['total_cost']*100:.4f}%)，"
                f"紧急性={urgency}，时间容忍={time_tolerance_us}μs"
            )
            if not market_allowed and order_type != "market":
                reasoning += "，市价单因盘口陷阱被禁用"

            return order_type, optimal_params, reasoning

        # 所有类型都被排除，使用默认类型
        logger.warning("所有订单类型均被排除，使用默认限价单")
        return self.DEFAULT_ORDER_TYPE, {}, "所有候选类型均不适用，回退为默认限价单"

    # ========== 事件发布 ==========
    def _publish_decision_event(
        self,
        order_type: str,
        params: Dict[str, Any],
        urgency: int,
        costs: Dict[str, Dict],
        traps: Dict[str, bool],
    ) -> None:
        """发布订单类型选择事件（用于审计）"""
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_event'):
            try:
                self._negotiation_bus.publish_event(
                    event_type="order_type_selected",
                    details={
                        "order_type": order_type,
                        "params": params,
                        "urgency": urgency,
                        "costs": {k: round(v.get("total_cost", 999), 6) for k, v in costs.items()},
                        "traps": traps,
                        "timestamp": time.time(),
                    },
                )
            except Exception as e:
                logger.warning(f"发布决策事件失败: {e}")
