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
- core.account_ledger.AccountLedger : 查询账户保证金率、可用保证金与强平价格
- core.negotiation_bus.NegotiationBus : 发出加仓协商请求，获取风控与执行模块的约束反馈
- core.behavioral_logger.BehavioralLogger : 记录加仓决策与异常事件

接口契约：
- evaluate_add_position(symbol: str, direction: int, current_position: float, avg_entry: float,
    current_price: float, current_atr: float, timestamp: float, bar_duration_seconds: int = 60) -> Dict[str, Any]
  主入口：执行完整加仓评估流程，返回是否可加仓、仓位倍数与决策依据
- evaluate_post_add_behavior(...) -> Dict[str, Any]：加仓后行为评估（反向速裁 + 僵持防护）
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 ProfitCompression 不可用时，跳过紧缩利润校验，并标记 "degraded" 状态
- 当 AccountLedger 不可用时，使用类常量 DEFAULT_MARGIN_SAFETY_FACTOR 进行保守估算
- 当 VisualCortex 不可用时，使用保守趋势评分 DEFAULT_CONSERVATIVE_TREND_SCORE
- 所有降级值在类常量区明确声明

资源管理：
- 本模块不持有任何需要手动释放的资源
- 依赖的外部模块由调用方管理生命周期
- 冷却时间戳字典定期清理过期记录，防止内存泄漏
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional

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

    # 反向速裁参数
    DEFAULT_REVERSAL_MICRO_SEC = 3         # 微反向判定时间窗口，秒，[1, 5]
    DEFAULT_REVERSAL_MEDIUM_SEC = 10       # 中反向判定时间窗口，秒，[5, 15]
    DEFAULT_REVERSAL_STRONG_SEC = 20       # 强反向判定时间窗口，秒，[15, 30]
    DEFAULT_MICRO_PRICE_PCT = 0.0015       # 微反向价格阈值（%），无量纲，[0.001, 0.003]
    DEFAULT_MEDIUM_PRICE_PCT = 0.003       # 中反向价格阈值（%），无量纲，[0.002, 0.005]
    DEFAULT_STRONG_PRICE_PCT = 0.005       # 强反向价格阈值（%），无量纲，[0.004, 0.008]

    # 僵持防护参数
    DEFAULT_STAGNANT_WINDOW_SEC = 10       # 僵持判定窗口，秒，[5, 20]
    DEFAULT_STAGNANT_RANGE_PCT = 0.0005    # 僵持价格波动范围（%），无量纲，[0.0002, 0.001]
    DEFAULT_STAGNANT_REDUCE_PCT = 0.5      # 僵持超时后减仓比例，无量纲，[0.3, 0.7]

    # 降级默认值
    DEFAULT_MARGIN_SAFETY_FACTOR = 0.8     # 保证金安全系数（当 AccountLedger 不可用时），无量纲，[0.7, 0.9]
    DEFAULT_CONSERVATIVE_TREND_SCORE = 0.4 # 保守趋势评分（当感知模块不可用时），无量纲，[0.2, 0.5]
    DEFAULT_RISK_BUDGET_FALLBACK = 5000.0  # 降级风险预算（USD），当 AccountLedger 不可用时，[1000, 50000]
    DEFAULT_MAX_COOLDOWN_AGE_SEC = 86400   # 冷却记录最大保留时间，秒，[3600, 172800]
    DEFAULT_MIN_QTY_STEP = 0.0             # 最小合约变动单位，默认 0 表示不裁剪

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
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 线程安全（保护加仓冷却时间戳等共享状态）
        self._lock = threading.Lock()
        # 记录每个品种和方向的最后加仓时间，用于冷却校验
        self._last_add_timestamps: Dict[str, float] = {}
        # K 线周期秒数（默认 60，由外部注入）
        self._bar_duration_seconds = 60
        # 最小变动单位（由 symbol_mapper 或合约规格注入）
        self._min_qty_step = 0.0

        logger.info("[AddPosition] AddPositionManager 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        profit_compression: Optional[Any] = None,
        visual_cortex: Optional[Any] = None,
        circuit_breaker: Optional[Any] = None,
        fragility_calculator: Optional[Any] = None,
        account_ledger: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        bar_duration_seconds: int = 60,
        min_qty_step: float = 0.0,
    ) -> None:
        """注入外部依赖（可选注入，未注入时对应功能降级）"""
        self._profit_compression = profit_compression
        self._visual_cortex = visual_cortex
        self._circuit_breaker = circuit_breaker
        self._fragility_calculator = fragility_calculator
        self._account_ledger = account_ledger
        self._negotiation_bus = negotiation_bus
        self._behavioral_logger = behavioral_logger
        self._bar_duration_seconds = bar_duration_seconds
        self._min_qty_step = min_qty_step

        if not profit_compression:
            logger.warning("[AddPosition] ProfitCompression 未注入，紧缩利润校验降级")
        if not visual_cortex:
            logger.warning("[AddPosition] VisualCortex 未注入，M12 与趋势校验降级")
        if not account_ledger:
            logger.warning("[AddPosition] AccountLedger 未注入，保证金预检降级为保守估算")
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

        # 使用传入的 K 线周期或已注入的值
        effective_bar_seconds = bar_duration_seconds if bar_duration_seconds is not None else self._bar_duration_seconds

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
            current_atr, timestamp, effective_bar_seconds,
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

        # 3. 四维仓位计算
        add_multiplier = self._calc_add_multiplier(current_position, avg_entry, current_price, current_atr)

        # 4. 保证金预检
        if not self._check_margin(symbol, direction, current_position * add_multiplier, current_price):
            data["add_multiplier"] = add_multiplier
            return {
                "status": "ok",
                "reason": "保证金不足，禁止加仓",
                "data": data,
                "warnings": warnings + ["margin_insufficient"],
            }

        # 5. 清理过期冷却记录
        self._purge_stale_timestamps(timestamp)

        # 6. 更新冷却时间
        with self._lock:
            self._last_add_timestamps[self._get_key(symbol, direction)] = timestamp

        add_size = current_position * add_multiplier
        # 7. 合约精度裁剪
        if self._min_qty_step > 0:
            add_size = round(add_size / self._min_qty_step) * self._min_qty_step
            if add_size <= 0:
                return {
                    "status": "ok",
                    "reason": "加仓量经精度裁剪后为零，取消加仓",
                    "data": data,
                    "warnings": warnings + ["qty_step_too_small"],
                }

        data["add_allowed"] = True
        data["add_multiplier"] = add_multiplier
        data["add_size"] = add_size

        logger.info(
            "[AddPosition] 加仓通过: symbol=%s, dir=%d, multiplier=%.3f, size=%.4f, price=%.2f",
            symbol, direction, add_multiplier, add_size, current_price
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
        加仓后行为评估（反向速裁 + 僵持防护）

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

        # 微反向
        if elapsed <= self.DEFAULT_REVERSAL_MICRO_SEC and price_change_pct >= self.DEFAULT_MICRO_PRICE_PCT:
            return {
                "status": "ok",
                "reason": "加仓后微反向，收紧止损",
                "data": {"action": self.ACTION_TIGHTEN_STOP, "atr_mult": 0.3},
                "warnings": ["micro_reversal"],
            }
        # 中反向
        if elapsed <= self.DEFAULT_REVERSAL_MEDIUM_SEC and price_change_pct >= self.DEFAULT_MEDIUM_PRICE_PCT:
            return {
                "status": "ok",
                "reason": "加仓后中反向，平掉加仓部分",
                "data": {"action": self.ACTION_CLOSE_ADDED_ONLY},
                "warnings": ["medium_reversal"],
            }
        # 强反向
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
        """模块自检"""
        try:
            deps_health = {
                "profit_compression": self._profit_compression is not None,
                "visual_cortex": self._visual_cortex is not None,
                "account_ledger": self._account_ledger is not None,
                "circuit_breaker": self._circuit_breaker is not None,
            }
            # 对 AccountLedger 进行端到端验证
            if self._account_ledger is not None and hasattr(self._account_ledger, 'health_check'):
                try:
                    ledger_hc = self._account_ledger.health_check()
                    if ledger_hc.get("status") != "ok":
                        deps_health["account_ledger"] = f"unhealthy: {ledger_hc.get('reason', 'unknown')}"
                except Exception as e:
                    deps_health["account_ledger"] = f"health_check 异常: {str(e)}"

            with self._lock:
                cooldown_count = len(self._last_add_timestamps)

            return {
                "status": "ok",
                "reason": f"AddPositionManager 正常，活跃冷却记录 {cooldown_count} 条",
                "data": {
                    "dependencies": deps_health,
                    "cooldown_record_count": cooldown_count,
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error("[AddPosition] 健康检查失败: %s #RECOVERY: 检查锁状态和依赖注入", e)
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"]}

    # ========== 私有方法 ==========
    def _run_seven_dimension_checks(
        self, symbol, direction, current_position, avg_entry,
        current_price, current_atr, timestamp, bar_seconds,
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

        # 4. 加仓冷却计时
        with self._lock:
            last_add = self._last_add_timestamps.get(self._get_key(symbol, direction), 0)
        required_cooldown = self.DEFAULT_COOLDOWN_BARS * bar_seconds
        cooldown_ok = (timestamp - last_add) >= required_cooldown
        checks["cooldown"] = {
            "passed": cooldown_ok,
            "value": f"距上次加仓 {timestamp - last_add:.0f}s, 需要 {required_cooldown}s"
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
        risk_ok = current_position * current_price * 0.02 <= self._calc_risk_budget()
        checks["total_risk"] = {"passed": risk_ok, "value": "通过" if risk_ok else "超限"}

        return checks

    def _calc_add_multiplier(self, current_position, avg_entry, current_price, current_atr) -> float:
        """四维加权计算加仓倍数"""
        # 基础序列
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
        """保证金预检"""
        if self._account_ledger is not None and hasattr(self._account_ledger, 'get_margin_info'):
            try:
                margin_info = self._account_ledger.get_margin_info(symbol, direction, position_size, price)
                return margin_info.get("sufficient", False)
            except Exception as e:
                logger.error("[AddPosition] 保证金查询异常: %s #RECOVERY: 检查 AccountLedger 服务", e)

        # 保守估算
        estimated_margin = position_size * price * (1.0 - self.DEFAULT_MARGIN_SAFETY_FACTOR)
        budget = self._calc_risk_budget()
        sufficient = estimated_margin <= budget
        logger.debug(
            "[AddPosition] 保证金估算: required=%.2f, budget=%.2f, sufficient=%s",
            estimated_margin, budget, sufficient
        )
        return sufficient

    def _calc_risk_budget(self) -> float:
        """计算当前风险预算，若 AccountLedger 不可用则返回保守降级值"""
        if self._account_ledger is not None and hasattr(self._account_ledger, 'get_risk_budget'):
            try:
                return self._account_ledger.get_risk_budget()
            except Exception as e:
                logger.warning("[AddPosition] 风险预算查询异常: %s，使用降级值", e)
        return self.DEFAULT_RISK_BUDGET_FALLBACK

    def _purge_stale_timestamps(self, now: float) -> None:
        """清理过期的冷却记录，防止内存泄漏"""
        with self._lock:
            stale_keys = [
                k for k, v in self._last_add_timestamps.items()
                if now - v > self.DEFAULT_MAX_COOLDOWN_AGE_SEC
            ]
            for k in stale_keys:
                del self._last_add_timestamps[k]
        if stale_keys:
            logger.debug("[AddPosition] 清理过期冷却记录: %d 条", len(stale_keys))

    def _get_key(self, symbol: str, direction: int) -> str:
        return f"{symbol}:{direction}"
