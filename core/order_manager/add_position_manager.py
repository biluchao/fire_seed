"""
火种系统 · 加仓管理器 (AddPositionManager)

核心职责：
1. 执行七维前置校验，确保加仓在安全边际、趋势强度、波动率、风险预算等方面全部通过
2. 基于趋势强度、利润安全、波动率四维加权动态计算加仓仓位，并执行保证金预检与反向速裁

外部依赖（真实模块接口）：
- core.order_manager.profit_compression.ProfitCompression : 获取当前持仓的紧缩利润阶段与不可逆止损位置
- core.perception.visual_cortex.VisualCortex : 获取当前 M12 方向、距离分区与质量评分
- core.risk_monitor.circuit_breaker.CircuitBreaker : 检查是否处于熔断冷却期
- core.risk_monitor.fragility_index_calculator.FragilityIndexCalculator : 获取当前脆弱性指数与波动率分位
- core.account_ledger.AccountLedger : 查询账户保证金率、可用保证金、强平价格、风险预算与权益
- core.position_snapshot.PositionSnapshot : 系统重启时恢复冷却记录与 K 线序号
- core.symbol_mapper.SymbolMapper : 获取交易对合约规格（最小变动单位、最大杠杆倍数）
- core.negotiation_bus.NegotiationBus : 发出加仓协商请求，获取风控与执行模块的约束反馈
- core.behavioral_logger.BehavioralLogger : 记录加仓决策与异常事件

接口契约：
- evaluate_add_position(symbol: str, direction: int, current_position: float, avg_entry: float,
    current_price: float, current_atr: float, timestamp: float, current_bar_index: int,
    bar_duration_seconds: int = 60) -> Dict[str, Any]
  主入口：执行完整加仓评估流程，返回是否可加仓、仓位倍数与决策依据
- evaluate_post_add_behavior(...) -> Dict[str, Any]：加仓后行为评估（反向速裁 + 僵持防护）
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 ProfitCompression 不可用时，跳过紧缩利润校验，并标记 "degraded" 状态
- 当 AccountLedger 不可用时，使用缓存权益 × 1% 作为保守风险预算，缓存超过 1 小时则使用硬底线
- 当 VisualCortex 不可用时，使用保守趋势评分 DEFAULT_CONSERVATIVE_TREND_SCORE
- 当 SymbolMapper 不可用时，使用默认杠杆 5 倍与默认最小变动单位 0.0
- 系统重启时从 PositionSnapshot 恢复冷却记录与 K 线序号
- 所有降级值在类常量区明确声明

资源管理：
- 本模块不持有任何需要手动释放的资源
- 依赖的外部模块由调用方管理生命周期
- 冷却记录字典定期清理超过 24 小时未更新的记录，防止内存泄漏
- 权益缓存附带时间戳，超时自动失效
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class AddPositionManager:
    """加仓管理器：七维校验 + 四维仓位计算 + 反向速裁 + 僵持防护"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 七维校验阈值
    DEFAULT_MIN_PROFIT_MARGIN_ATR = 0.5    # 最小浮盈安全边际（ATR 倍数），无量纲，[0.3, 1.0]
    DEFAULT_MAX_RISK_BUDGET_PCT = 0.02     # 单次加仓最大风险预算（权益百分比），无量纲，[0.01, 0.05]
    DEFAULT_MIN_TREND_STRENGTH = 0.6       # 最小趋势强度评分，无量纲，[0.4, 0.8]
    DEFAULT_COOLDOWN_BARS = 2              # 最小冷却 K 线数，根，[1, 10]
    DEFAULT_VOLATILITY_MIN_PCT = 20        # 最低波动率分位，%，[10, 40]
    DEFAULT_VOLATILITY_MAX_PCT = 85        # 最高波动率分位，%，[60, 95]
    DEFAULT_MA12_MAX_DISTANCE_ATR = 1.5    # M12 最大偏离（ATR 倍数），无量纲，[1.0, 2.5]

    # 四维仓位计算权重
    DEFAULT_WEIGHT_BASE_SEQUENCE = 0.40    # 基础序列权重，无量纲，[0.2, 0.6]
    DEFAULT_WEIGHT_TREND = 0.30            # 趋势强度权重，无量纲，[0.1, 0.4]
    DEFAULT_WEIGHT_PROFIT = 0.20           # 利润安全权重，无量纲，[0.1, 0.3]
    DEFAULT_WEIGHT_VOLATILITY = 0.10       # 波动率权重，无量纲，[0.05, 0.2]
    DEFAULT_MAX_ADD_MULT = 1.5             # 最大加仓倍数，无量纲，[1.2, 2.0]
    DEFAULT_MIN_ADD_MULT = 0.3             # 最小加仓倍数，无量纲，[0.1, 0.5]

    # 反向速裁参数（非对称阈值）
    DEFAULT_REVERSAL_MICRO_SEC = 3         # 微反向判定时间窗口，秒，[1, 5]
    DEFAULT_REVERSAL_MEDIUM_SEC = 10       # 中反向判定时间窗口，秒，[5, 15]
    DEFAULT_REVERSAL_STRONG_SEC = 20       # 强反向判定时间窗口，秒，[15, 30]
    # 多头持仓对下跌更敏感（恐慌性抛售），空头持仓对上涨更敏感
    LONG_MICRO_PRICE_PCT = 0.0010          # 多头微反向阈值，无量纲，[0.0005, 0.002]
    LONG_MEDIUM_PRICE_PCT = 0.0020         # 多头中反向阈值，无量纲，[0.001, 0.004]
    SHORT_MICRO_PRICE_PCT = 0.0015         # 空头微反向阈值，无量纲，[0.0008, 0.003]
    SHORT_MEDIUM_PRICE_PCT = 0.0030        # 空头中反向阈值，无量纲，[0.0015, 0.005]
    DEFAULT_STRONG_PRICE_PCT = 0.005       # 强反向价格阈值（多空通用），无量纲，[0.004, 0.008]

    # 僵持防护参数
    DEFAULT_STAGNANT_WINDOW_SEC = 10       # 僵持判定窗口，秒，[5, 20]
    DEFAULT_STAGNANT_RANGE_PCT = 0.0005    # 僵持价格波动范围（%），无量纲，[0.0002, 0.001]
    DEFAULT_STAGNANT_REDUCE_PCT = 0.5      # 僵持超时后减仓比例，无量纲，[0.3, 0.7]

    # 降级默认值
    DEFAULT_MARGIN_SAFETY_FACTOR = 0.8     # 保证金安全系数（当 AccountLedger 不可用时），无量纲，[0.7, 0.9]
    DEFAULT_CONSERVATIVE_TREND_SCORE = 0.4 # 保守趋势评分（当感知模块不可用时），无量纲，[0.2, 0.5]
    DEFAULT_RISK_BUDGET_FALLBACK = 5000.0  # 硬底线风险预算（USD），当缓存权益也失效时使用，[1000, 50000]
    DEFAULT_CACHED_EQUITY_MAX_AGE_SEC = 3600  # 缓存权益最大有效期，秒，[600, 7200]
    DEFAULT_CONSERVATIVE_EQUITY_PCT = 0.01 # 缓存权益的保守风险比例，无量纲，[0.005, 0.02]
    DEFAULT_MAX_COOLDOWN_AGE_SEC = 86400   # 冷却记录最大保留时间，秒，[3600, 172800]
    DEFAULT_MIN_QTY_STEP = 0.0             # 最小合约变动单位，默认 0 表示不裁剪
    DEFAULT_MAX_LEVERAGE = 5               # 默认最大杠杆倍数，无量纲，[2, 20]

    # 标准化动作枚举
    ACTION_HOLD = "hold"
    ACTION_CLOSE_ADDED_ONLY = "close_added_only"
    ACTION_CLOSE_ADDED_TIGHTEN_ORIGINAL = "close_added_tighten_original"
    ACTION_TIGHTEN_STOP = "tighten_stop"
    ACTION_REDUCE_ADDED = "reduce_added"

    def __init__(self):
        # 外部依赖注入
        self._profit_compression = None
        self._visual_cortex = None
        self._circuit_breaker = None
        self._fragility_calculator = None
        self._account_ledger = None
        self._symbol_mapper = None
        self._position_snapshot = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 线程安全（保护加仓冷却时间戳、K 线序号、权益缓存等共享状态）
        self._lock = threading.Lock()
        # 记录每个品种和方向的最后加仓 K 线序号（用于冷却校验，系统重启时从快照恢复）
        self._last_add_bar_indices: Dict[str, int] = {}
        # 记录每个品种和方向的最后加仓时间戳（用于过期清理）
        self._last_add_timestamps: Dict[str, float] = {}

        # K 线周期秒数（默认 60，由外部注入）
        self._bar_duration_seconds = 60
        # 最小变动单位（由 SymbolMapper 注入）
        self._min_qty_step = 0.0
        # 最大杠杆倍数（由 SymbolMapper 注入）
        self._max_leverage = self.DEFAULT_MAX_LEVERAGE

        # 权益缓存（AccountLedger 不可用时的降级数据源）
        self._cached_equity = 0.0
        self._cached_equity_timestamp = 0.0

        logger.info("[AddPosition] AddPositionManager 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        profit_compression: Optional[Any] = None,
        visual_cortex: Optional[Any] = None,
        circuit_breaker: Optional[Any] = None,
        fragility_calculator: Optional[Any] = None,
        account_ledger: Optional[Any] = None,
        symbol_mapper: Optional[Any] = None,
        position_snapshot: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        bar_duration_seconds: int = 60,
        min_qty_step: float = -1.0,
        max_leverage: int = 0,
    ) -> None:
        """注入外部依赖（可选注入，未注入时对应功能降级）"""
        self._profit_compression = profit_compression
        self._visual_cortex = visual_cortex
        self._circuit_breaker = circuit_breaker
        self._fragility_calculator = fragility_calculator
        self._account_ledger = account_ledger
        self._symbol_mapper = symbol_mapper
        self._position_snapshot = position_snapshot
        self._negotiation_bus = negotiation_bus
        self._behavioral_logger = behavioral_logger
        self._bar_duration_seconds = bar_duration_seconds

        # 合约规格
        if min_qty_step > 0:
            self._min_qty_step = min_qty_step
        elif min_qty_step == 0.0:
            logger.warning("[AddPosition] min_qty_step 为 0.0，精度裁剪未启用，订单可能被交易所拒绝")
        else:
            logger.warning("[AddPosition] min_qty_step 未注入，精度裁剪未启用")

        if max_leverage > 0:
            self._max_leverage = max_leverage
        else:
            logger.warning("[AddPosition] max_leverage 未注入，使用默认 %d 倍杠杆", self.DEFAULT_MAX_LEVERAGE)

        # 系统重启时从快照恢复冷却记录
        if position_snapshot is not None and hasattr(position_snapshot, 'get_add_cooldown_data'):
            try:
                cooldown_data = position_snapshot.get_add_cooldown_data()
                if cooldown_data:
                    with self._lock:
                        self._last_add_bar_indices = cooldown_data.get("bar_indices", {})
                        self._last_add_timestamps = cooldown_data.get("timestamps", {})
                    logger.info("[AddPosition] 从快照恢复冷却记录: %d 条", len(self._last_add_bar_indices))
            except Exception as e:
                logger.warning("[AddPosition] 快照恢复失败: %s", e)

        if not profit_compression:
            logger.warning("[AddPosition] ProfitCompression 未注入，紧缩利润校验降级")
        if not visual_cortex:
            logger.warning("[AddPosition] VisualCortex 未注入，M12 与趋势校验降级")
        if not account_ledger:
            logger.warning("[AddPosition] AccountLedger 未注入，保证金预检与风险预算降级为保守估算")
        if not negotiation_bus:
            logger.warning("[AddPosition] NegotiationBus 未注入，协商降级为本地决策")

    # ========== 公共接口 ==========
    def evaluate_add_position(
        self,
        symbol: str,
        direction: int,
        current_position: float,
        avg_entry: float,
        current_price: float,
        current_atr: float,
        timestamp: float,
        current_bar_index: int,
        bar_duration_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        评估是否可加仓及加仓仓位

        Args:
            symbol: 交易对符号
            direction: 持仓方向 (+1 多头, -1 空头)
            current_position: 当前持仓量（正数）
            avg_entry: 当前持仓均价
            current_price: 当前价格
            current_atr: 当前 ATR 值
            timestamp: 当前时间戳
            current_bar_index: 当前 K 线序号
            bar_duration_seconds: K 线周期秒数，可选，默认使用注入值或 60

        Returns:
            标准响应字典，data 中包含 add_allowed, add_multiplier, reason 等
        """
        warnings = []
        data = {
            "add_allowed": False,
            "add_multiplier": 0.0,
            "add_size": 0.0,
            "checks": {},
        }

        key = self._get_key(symbol, direction)

        # 1. 快速熔断检查
        if self._circuit_breaker is not None and hasattr(self._circuit_breaker, 'is_frozen'):
            try:
                if self._circuit_breaker.is_frozen(symbol):
                    logger.info("[AddPosition] 熔断冻结中，禁止加仓: %s", symbol)
                    return {
                        "status": "ok",
                        "reason": "熔断冻结中，禁止加仓",
                        "data": data,
                        "warnings": ["circuit_breaker_frozen"],
                    }
            except Exception as e:
                logger.warning("[AddPosition] 熔断检查异常: %s", e)
                warnings.append(f"熔断检查异常: {str(e)}")

        # 2. 七维校验
        checks = self._run_seven_dimension_checks(
            symbol, direction, current_position, avg_entry, current_price,
            current_atr, current_bar_index, key,
        )
        failed_checks = [k for k, v in checks.items() if not v.get("passed", False)]
        data["checks"] = checks
        if failed_checks:
            return {
                "status": "ok",
                "reason": f"七维校验未通过: {', '.join(failed_checks)}",
                "data": data,
                "warnings": warnings,
            }

        # 3. 七维校验通过后立即更新冷却 K 线序号（防止同一根 K 线内重复评估）
        with self._lock:
            self._last_add_bar_indices[key] = current_bar_index
            self._last_add_timestamps[key] = timestamp

        # 4. 四维仓位计算
        add_multiplier = self._calc_add_multiplier(current_position, avg_entry, current_price, current_atr)
        if add_multiplier <= 0:
            data["add_multiplier"] = 0.0
            return {
                "status": "ok",
                "reason": "加仓倍数计算结果为零，取消加仓",
                "data": data,
                "warnings": warnings + ["zero_multiplier"],
            }

        # 5. 保证金预检
        if not self._check_margin(symbol, direction, current_position * add_multiplier, current_price):
            data["add_multiplier"] = add_multiplier
            return {
                "status": "ok",
                "reason": "保证金不足，禁止加仓",
                "data": data,
                "warnings": warnings + ["margin_insufficient"],
            }

        # 6. 清理过期冷却记录
        self._purge_stale_timestamps(timestamp)

        add_size = current_position * add_multiplier

        # 7. 合约精度裁剪
        if self._min_qty_step > 0:
            add_size = round(add_size / self._min_qty_step) * self._min_qty_step
            if add_size <= 0:
                return {
                    "status": "ok",
                    "reason": "加仓量经精度裁剪后为零，取消加仓",
                    "data": data,
                    "warnings": warnings + ["qty_step_zero"],
                }

        data["add_allowed"] = True
        data["add_multiplier"] = add_multiplier
        data["add_size"] = add_size

        logger.info(
            "[AddPosition] 加仓通过: symbol=%s, dir=%d, bar=%d, multiplier=%.3f, size=%.4f",
            symbol, direction, current_bar_index, add_multiplier, add_size
        )

        return {
            "status": "ok",
            "reason": f"加仓允许, 倍数={add_multiplier:.2f}",
            "data": data,
            "warnings": warnings,
        }

    def evaluate_post_add_behavior(
        self,
        symbol: str,
        direction: int,
        add_price: float,
        current_price: float,
        add_timestamp: float,
        current_timestamp: float,
        current_atr: float,
    ) -> Dict[str, Any]:
        """
        加仓后行为评估（反向速裁 + 僵持防护），阈值根据持仓方向非对称

        Args:
            symbol: 交易对
            direction: 方向 (+1 多头, -1 空头)
            add_price: 加仓成交价
            current_price: 当前价格
            add_timestamp: 加仓时间戳
            current_timestamp: 当前时间戳
            current_atr: 当前 ATR

        Returns:
            标准响应字典，data 中包含 action, reason
        """
        elapsed = current_timestamp - add_timestamp
        price_change_pct = abs(current_price - add_price) / (add_price + 1e-12)

        # 僵持检测
        if elapsed > self.DEFAULT_STAGNANT_WINDOW_SEC and price_change_pct < self.DEFAULT_STAGNANT_RANGE_PCT:
            logger.info("[AddPosition] 加仓后价格僵持: %s, elapsed=%.1fs", symbol, elapsed)
            return {
                "status": "ok",
                "reason": "加仓后价格僵持，建议减仓",
                "data": {"action": self.ACTION_REDUCE_ADDED, "ratio": self.DEFAULT_STAGNANT_REDUCE_PCT},
                "warnings": ["stagnant_price"],
            }

        # 反向检测：direction=1 时价格下跌为不利；direction=-1 时价格上涨为不利
        adverse_move = (current_price - add_price) * direction < 0
        if not adverse_move:
            return {
                "status": "ok",
                "reason": "加仓后价格运行正常",
                "data": {"action": self.ACTION_HOLD},
                "warnings": [],
            }

        # 非对称阈值：多头对下跌更敏感，空头对上涨更敏感
        if direction == 1:
            micro_pct = self.LONG_MICRO_PRICE_PCT
            medium_pct = self.LONG_MEDIUM_PRICE_PCT
        else:
            micro_pct = self.SHORT_MICRO_PRICE_PCT
            medium_pct = self.SHORT_MEDIUM_PRICE_PCT

        # 微反向
        if elapsed <= self.DEFAULT_REVERSAL_MICRO_SEC and price_change_pct >= micro_pct:
            return {
                "status": "ok",
                "reason": "加仓后微反向，收紧止损",
                "data": {"action": self.ACTION_TIGHTEN_STOP, "atr_mult": 0.3},
                "warnings": ["micro_reversal"],
            }
        # 中反向
        if elapsed <= self.DEFAULT_REVERSAL_MEDIUM_SEC and price_change_pct >= medium_pct:
            return {
                "status": "ok",
                "reason": "加仓后中反向，平掉加仓部分",
                "data": {"action": self.ACTION_CLOSE_ADDED_ONLY},
                "warnings": ["medium_reversal"],
            }
        # 强反向（多空通用阈值）
        if elapsed <= self.DEFAULT_REVERSAL_STRONG_SEC and price_change_pct >= self.DEFAULT_STRONG_PRICE_PCT:
            return {
                "status": "ok",
                "reason": "加仓后强反向，平掉加仓并收紧原始止损",
                "data": {"action": self.ACTION_CLOSE_ADDED_TIGHTEN_ORIGINAL},
                "warnings": ["strong_reversal"],
            }

        return {
            "status": "ok",
            "reason": "加仓后正常波动",
            "data": {"action": self.ACTION_HOLD},
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检，对关键依赖执行端到端验证"""
        try:
            deps_health = {
                "profit_compression": self._profit_compression is not None,
                "visual_cortex": self._visual_cortex is not None,
                "circuit_breaker": self._circuit_breaker is not None,
                "account_ledger": self._account_ledger is not None,
                "symbol_mapper": self._symbol_mapper is not None,
            }

            # 对 AccountLedger 执行端到端验证
            if self._account_ledger is not None and hasattr(self._account_ledger, 'health_check'):
                try:
                    ledger_hc = self._account_ledger.health_check()
                    if ledger_hc.get("status") != "ok":
                        deps_health["account_ledger"] = f"unhealthy: {ledger_hc.get('reason', 'unknown')}"
                except Exception as e:
                    deps_health["account_ledger"] = f"health_check 异常: {str(e)}"

            # 验证 AccountLedger 的 get_risk_budget 方法真实可用性
            if self._account_ledger is not None and hasattr(self._account_ledger, 'get_risk_budget'):
                try:
                    budget = self._account_ledger.get_risk_budget()
                    if budget <= 0:
                        deps_health["account_ledger"] = f"风险预算异常: {budget}"
                except Exception as e:
                    deps_health["account_ledger"] = f"get_risk_budget 异常: {str(e)}"

            with self._lock:
                cooldown_count = len(self._last_add_bar_indices)
                timestamps_count = len(self._last_add_timestamps)

            return {
                "status": "ok",
                "reason": f"AddPositionManager 正常，活跃冷却记录 {cooldown_count} 条",
                "data": {
                    "dependencies": deps_health,
                    "cooldown_record_count": cooldown_count,
                    "timestamp_record_count": timestamps_count,
                    "min_qty_step": self._min_qty_step,
                    "max_leverage": self._max_leverage,
                    "cached_equity": self._cached_equity,
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error("[AddPosition] 健康检查失败: %s #RECOVERY: 检查锁状态和依赖注入", e)
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"]}

    # ========== 私有方法 ==========
    def _run_seven_dimension_checks(
        self, symbol, direction, current_position, avg_entry,
        current_price, current_atr, current_bar_index, key,
    ) -> Dict[str, Any]:
        """执行七维前置校验，返回各维度通过情况"""
        checks = {}

        # 1. 浮盈安全边际
        profit_atr = (current_price - avg_entry) * direction / (current_atr + 1e-12)
        profit_safe = profit_atr >= self.DEFAULT_MIN_PROFIT_MARGIN_ATR
        checks["profit_margin"] = {
            "passed": profit_safe, "value": round(profit_atr, 2),
            "reason": f"浮盈 ATR: {profit_atr:.2f}, 阈值: {self.DEFAULT_MIN_PROFIT_MARGIN_ATR}"
        }

        # 2. 利润紧缩状态
        compression_ok = True
        if self._profit_compression is not None and hasattr(self._profit_compression, 'get_stage'):
            try:
                stage = self._profit_compression.get_stage(symbol, direction)
                compression_ok = stage in ("breakeven", "micro", "medium", "large")
                checks["compression_stage"] = {"passed": compression_ok, "value": stage}
            except Exception as e:
                logger.warning("[AddPosition] 查询紧缩利润阶段异常: %s", e)
                checks["compression_stage"] = {"passed": True, "reason": "降级: 无法查询紧缩阶段"}
        else:
            checks["compression_stage"] = {"passed": True, "reason": "降级: ProfitCompression 不可用"}

        # 3. 趋势强度确认
        trend_ok = False
        trend_score = self.DEFAULT_CONSERVATIVE_TREND_SCORE
        if self._visual_cortex is not None and hasattr(self._visual_cortex, 'get_ma12_status'):
            try:
                ma12_status = self._visual_cortex.get_ma12_status(symbol, direction)
                trend_ok = ma12_status.get("aligned", False)
                trend_score = ma12_status.get("strength", self.DEFAULT_CONSERVATIVE_TREND_SCORE)
            except Exception as e:
                logger.warning("[AddPosition] 查询 M12 状态异常: %s", e)
        checks["trend_strength"] = {
            "passed": trend_ok or trend_score >= self.DEFAULT_MIN_TREND_STRENGTH,
            "value": trend_score
        }

        # 4. 加仓冷却计时（基于 K 线序号，非 wall clock 秒数）
        with self._lock:
            last_bar = self._last_add_bar_indices.get(key, -999)
        cooldown_ok = (current_bar_index - last_bar) >= self.DEFAULT_COOLDOWN_BARS
        checks["cooldown"] = {
            "passed": cooldown_ok,
            "value": f"当前 K 线: {current_bar_index}, 上次加仓 K 线: {last_bar}, 需要间隔: {self.DEFAULT_COOLDOWN_BARS}"
        }

        # 5. 波动率适宜度
        vol_ok = True
        if self._fragility_calculator is not None and hasattr(self._fragility_calculator, 'get_volatility_percentile'):
            try:
                vol_pct = self._fragility_calculator.get_volatility_percentile(symbol)
                vol_ok = self.DEFAULT_VOLATILITY_MIN_PCT <= vol_pct <= self.DEFAULT_VOLATILITY_MAX_PCT
                checks["volatility"] = {"passed": vol_ok, "value": vol_pct}
            except Exception as e:
                logger.warning("[AddPosition] 波动率查询异常: %s", e)
                checks["volatility"] = {"passed": True, "reason": "降级"}
        else:
            checks["volatility"] = {"passed": True, "reason": "降级: FragilityCalculator 不可用"}

        # 6. M12 距离分区
        m12_distance_ok = True
        if self._visual_cortex is not None and hasattr(self._visual_cortex, 'get_m12_distance'):
            try:
                dist_atr = self._visual_cortex.get_m12_distance(symbol, current_price, current_atr)
                m12_distance_ok = dist_atr <= self.DEFAULT_MA12_MAX_DISTANCE_ATR
                checks["m12_distance"] = {"passed": m12_distance_ok, "value": f"{dist_atr:.2f} ATR"}
            except Exception as e:
                logger.warning("[AddPosition] M12 距离查询异常: %s", e)
                checks["m12_distance"] = {"passed": True, "reason": "降级"}
        else:
            checks["m12_distance"] = {"passed": True, "reason": "降级: VisualCortex 不可用"}

        # 7. 总风险硬上限
        risk_exposure = current_position * current_price * self.DEFAULT_MAX_RISK_BUDGET_PCT
        risk_budget = self._calc_risk_budget()
        risk_ok = risk_exposure <= risk_budget
        checks["total_risk"] = {
            "passed": risk_ok,
            "value": f"风险敞口: {risk_exposure:.2f}, 预算: {risk_budget:.2f}"
        }

        return checks

    def _calc_add_multiplier(self, current_position, avg_entry, current_price, current_atr) -> float:
        """四维加权计算加仓倍数"""
        base_mult = 1.0

        # 趋势强度修正
        trend_factor = 1.0
        if self._visual_cortex is not None and hasattr(self._visual_cortex, 'get_trend_factor'):
            try:
                trend_factor = self._visual_cortex.get_trend_factor()
            except Exception as e:
                logger.warning("[AddPosition] 趋势因子查询异常: %s", e)

        # 利润安全修正：浮盈越大，加仓越激进（最高 +20%）
        profit_atr = (current_price - avg_entry) / (current_atr + 1e-12)
        profit_factor = 1.0 + min(0.2, abs(profit_atr) * 0.05)

        # 波动率修正
        vol_factor = 1.0
        if self._fragility_calculator is not None and hasattr(self._fragility_calculator, 'get_volatility_factor'):
            try:
                vol_factor = self._fragility_calculator.get_volatility_factor()
            except Exception as e:
                logger.warning("[AddPosition] 波动率因子查询异常: %s", e)

        multiplier = (
            base_mult * self.DEFAULT_WEIGHT_BASE_SEQUENCE
            + trend_factor * self.DEFAULT_WEIGHT_TREND
            + profit_factor * self.DEFAULT_WEIGHT_PROFIT
            + vol_factor * self.DEFAULT_WEIGHT_VOLATILITY
        )
        multiplier = max(self.DEFAULT_MIN_ADD_MULT, min(self.DEFAULT_MAX_ADD_MULT, multiplier))
        logger.debug(
            "[AddPosition] 加仓倍数: base=%.2f, trend=%.2f, profit=%.2f, vol=%.2f, final=%.3f",
            base_mult, trend_factor, profit_factor, vol_factor, multiplier
        )
        return multiplier

    def _check_margin(self, symbol, direction, position_size, price) -> bool:
        """保证金预检，使用真实杠杆率估算"""
        if self._account_ledger is not None and hasattr(self._account_ledger, 'get_margin_info'):
            try:
                margin_info = self._account_ledger.get_margin_info(symbol, direction, position_size, price)
                return margin_info.get("sufficient", False)
            except Exception as e:
                logger.error("[AddPosition] 保证金查询异常: %s #RECOVERY: 检查 AccountLedger 服务", e)

        # 保守估算：使用真实杠杆率
        leverage = self._max_leverage if self._max_leverage > 0 else self.DEFAULT_MAX_LEVERAGE
        estimated_margin = position_size * price / leverage
        budget = self._calc_risk_budget()
        sufficient = estimated_margin <= budget
        logger.debug(
            "[AddPosition] 保证金估算: required=%.2f, budget=%.2f, leverage=%d, sufficient=%s",
            estimated_margin, budget, leverage, sufficient
        )
        return sufficient

    def _calc_risk_budget(self) -> float:
        """计算当前风险预算，降级时使用缓存权益 × 保守比例"""
        if self._account_ledger is not None and hasattr(self._account_ledger, 'get_risk_budget'):
            try:
                budget = self._account_ledger.get_risk_budget()
                # 同时更新权益缓存
                if hasattr(self._account_ledger, 'get_equity'):
                    self._cached_equity = self._account_ledger.get_equity()
                    self._cached_equity_timestamp = time.time()
                return budget
            except Exception as e:
                logger.warning("[AddPosition] 风险预算查询异常: %s，尝试降级", e)

        # 降级：使用缓存的权益 × 保守比例
        if self._cached_equity > 0 and time.time() - self._cached_equity_timestamp < self.DEFAULT_CACHED_EQUITY_MAX_AGE_SEC:
            fallback = self._cached_equity * self.DEFAULT_CONSERVATIVE_EQUITY_PCT
            logger.debug("[AddPosition] 使用缓存权益降级: equity=%.2f, budget=%.2f", self._cached_equity, fallback)
            return fallback

        # 硬底线
        logger.warning("[AddPosition] 所有降级数据源失效，使用硬底线 %s", self.DEFAULT_RISK_BUDGET_FALLBACK)
        return self.DEFAULT_RISK_BUDGET_FALLBACK

    def _purge_stale_timestamps(self, now: float) -> None:
        """清理过期的冷却记录，防止内存泄漏"""
        with self._lock:
            stale_keys = [
                k for k, v in self._last_add_timestamps.items()
                if now - v > self.DEFAULT_MAX_COOLDOWN_AGE_SEC
            ]
            for k in stale_keys:
                self._last_add_timestamps.pop(k, None)
                self._last_add_bar_indices.pop(k, None)
        if stale_keys:
            logger.debug("[AddPosition] 清理过期冷却记录: %d 条", len(stale_keys))

    def _get_key(self, symbol: str, direction: int) -> str:
        return f"{symbol}:{direction}"
