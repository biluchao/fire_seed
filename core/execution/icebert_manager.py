"""
火种系统 · 冰山订单管理器 (IcebergManager)

核心职责：
1. 根据当前市场成交量脉搏、实时盘口深度（含挂单可信度评估）与流动性纹理，动态计算冰山订单的最优显露量，以最小化市场冲击并隐藏交易意图
2. 为批量订单生成符合泊松分布的随机切片方案，并返回动态执行规则与重新计算触发条件，供执行引擎自适应调整切片节奏

外部依赖（真实模块接口）：
- core.perception.tactile_cortex.TactileCortex : 获取成交量脉搏、盘口深度分布、挂单生存时间、波动率分位及趋势强度
- core.execution.order_type_selector.OrderTypeSelector : 接收订单执行方式建议，本模块不直接调用执行接口
- core.behavioral_logger.BehavioralLogger : 记录冰山订单切片的决策过程与最终执行参数

接口契约：
- calculate_display_quantity(symbol: str, total_quantity: float, side: str, urgency: int) -> Dict[str, Any]
  计算单次冰山订单的显露量，返回标准字典，data中包含 "display_quantity", "slice_count", "slice_interval_ms_initial", "dynamic_rules", "max_safe_display", "estimated_total_ms", "recalculation_triggers", "max_time_to_live_seconds"
- generate_disguised_slices(symbol: str, total_quantity: float, side: str) -> Dict[str, Any]
  生成伪装用的小额随机切片方案，返回标准字典，data中包含 "disguised_slices" (List[Dict])
- health_check() -> Dict[str, Any] : 模块自检，无副作用地探测外部依赖可用性、映射表完整性及配置一致性
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 TactileCortex 不可用或返回数据陈旧/无效时，使用预设的保守固定显露比例（总订单量的 5%），并标记 "degraded" 状态
- 数据陈旧（超过50ms）时自动应用折扣因子，防止基于过时信息做出激进决策
- 当无法获取波动率或趋势强度时，方向系数降级为预设的保守值
- 成交量脉搏单位声明为 trades_per_minute，依赖方应按此约定提供数据
- 波动率分位语义声明为 current_exceeds_history_pct（当前波动率超过历史百分之多少）
- 盘口深度约束连续失败超过阈值时自动升级告警级别，防止静默失效
- 所有降级值在类常量区明确声明

资源管理：
- 本模块为无状态计算模块，不持有任何外部资源句柄，不启动额外线程
- 所有中间计算数据在方法返回后自动回收
- 使用类实例复用的安全随机数生成器，避免频繁系统调用
- 本模块仅提供初始计算，执行引擎应在每次切片前根据实时状态重新调用以获取最新参数

版本兼容：
- 动态执行规则包含版本号，执行引擎应校验版本兼容性
"""

import logging
import secrets
import time
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class IcebergManager:
    """冰山订单管理器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_CONSERVATIVE_DISPLAY_RATIO = 0.05  # 降级时使用的保守显露比例，无量纲，[0.01, 0.10]
    DEFAULT_VOLUME_PULSE_FALLBACK = 1000.0     # 默认成交量脉搏（笔/分钟），用于降级，正数
    DEFAULT_MIN_DISPLAY_QTY = 0.001            # 最小显露量（BTC），用于防止过小订单，正数
    DEFAULT_MAX_DISPLAY_RATIO = 0.20           # 最大显露比例上限，无量纲，[0.10, 0.50]
    MAX_SLICE_COUNT = 20                       # 最大切片数量上限，防止触发交易所频率限制，[10, 50]
    MAX_ORDERBOOK_DEPTH_RATIO = 0.50           # 显露量不超过对手方前N档总深度的比例，无量纲，[0.30, 0.70]
    ORDERBOOK_DEPTH_LEVELS = 3                 # 用于深度约束的订单簿档位数，[2, 5]
    DISGUISED_SLICE_COUNT_MIN = 1              # 伪装切片最小数量，整数，>=1
    DISGUISED_SLICE_COUNT_MAX = 3              # 伪装切片最大数量，整数，<=5
    DISGUISED_QTY_RATIO = 0.02                 # 伪装切片相对于总订单量的比例上限，无量纲，[0.01, 0.05]
    POISSON_LAMBDA_SCALE = 0.5                 # 切片间隔的泊松分布λ缩放因子，无量纲
    URGENCY_HIGH_THRESHOLD = 7                 # 高紧迫度阈值，用于决定是否减少切片数量，[5, 9]
    SELL_SIDE_BASE_MULTIPLIER = 0.9           # 做空时显露比例的基础折扣系数，无量纲，[0.80, 0.95]
    SELL_SIDE_FALLBACK_MULTIPLIER = 0.6       # 做空方向降级时的保守系数（无法获取波动率/趋势时使用），[0.5, 0.7]
    MAX_DATA_STALENESS_MS = 50                # 数据最大允许陈旧度（毫秒），超过则应用陈旧折扣因子，[10, 200]
    STALE_DATA_DISCOUNT_FACTOR = 0.7          # 数据陈旧时的显露比例折扣，无量纲，[0.5, 0.9]
    GHOST_ORDER_DISCOUNT = 0.5                # 对疑似虚假挂单（存活时间短）的深度折扣，[0.3, 0.7]
    MIN_WALL_SURVIVAL_MS = 200                # 挂单最少存活时间（毫秒），低于此值视为可疑，[100, 500]
    DYNAMIC_RULES_VERSION = "1.0"             # 动态执行规则版本号，用于兼容性校验
    MIN_SLICE_COUNT_HIGH_URGENCY = 3          # 高紧迫度下的最小切片数量，保证基本隐蔽性，[2, 5]
    MAX_ICEBERG_EXECUTION_SEC = 120           # 最大执行时间（秒），超过则建议切换策略，[60, 300]
    MAX_ICEBERG_TTL_SEC = 300                 # 最大存活时间（秒），超过则建议取消剩余订单并切换策略，[120, 600]
    VOLUME_PULSE_UNIT = "trades_per_minute"   # 成交量脉搏数据源单位声明（笔/分钟），依赖方应按此提供
    VOLUME_PULSE_NORMALIZATION = 50000.0      # 标准化分母（BTC基准），无量纲，各品种应在流动性配置中覆盖此值
    VOLATILITY_PERCENTILE_SEMANTIC = "current_exceeds_history_pct"  # 波动率分位语义：当前波动率超过历史百分之多少
    DEPTH_ERROR_THRESHOLD = 10                # 盘口深度约束连续失败阈值，超过后升级告警级别，[5, 30]

    # 将订单方向映射到订单簿查询方向（buy 查询 asks，sell 查询 bids）
    BOOK_SIDE_MAP = {"buy": "asks", "sell": "bids"}

    # 默认品种流动性配置（当品种未在 SYMBOL_LIQUIDITY_PROFILES 中配置时使用，包含所有必需字段）
    DEFAULT_SYMBOL_PROFILE = {
        "fallback_pulse": 500.0,
        "max_display_ratio": 0.10,
        "pulse_normalization": 10000.0,
        "max_iceberg_ttl_sec": 600,           # 低流动性品种给更长的 TTL
        "max_disguised_qty": 0.005,            # 低流动性品种伪装量更小
        "strong_downtrend_threshold": -0.03,   # 强下跌趋势判定阈值
        "weak_downtrend_threshold": -0.01,     # 弱下跌趋势判定阈值
    }

    # 不同品种的流动性降级配置（当 TactileCortex 不可用时使用）
    # 各字段说明：
    #   fallback_pulse: 降级成交量脉搏（笔/分钟）
    #   max_display_ratio: 最大显露比例
    #   pulse_normalization: 该品种的成交量脉搏标准化分母
    #   max_iceberg_ttl_sec: 该品种冰山订单最大存活时间（秒）
    #   max_disguised_qty: 该品种伪装切片最大显露量（原始单位）
    #   strong_downtrend_threshold: 强下跌趋势判定阈值
    #   weak_downtrend_threshold: 弱下跌趋势判定阈值
    SYMBOL_LIQUIDITY_PROFILES = {
        "BTCUSDT": {
            "fallback_pulse": 5000.0,
            "max_display_ratio": 0.20,
            "pulse_normalization": 50000.0,
            "max_iceberg_ttl_sec": 180,
            "max_disguised_qty": 0.05,
            "strong_downtrend_threshold": -0.03,
            "weak_downtrend_threshold": -0.01,
        },
        "ETHUSDT": {
            "fallback_pulse": 3000.0,
            "max_display_ratio": 0.18,
            "pulse_normalization": 30000.0,
            "max_iceberg_ttl_sec": 240,
            "max_disguised_qty": 0.1,
            "strong_downtrend_threshold": -0.03,
            "weak_downtrend_threshold": -0.01,
        },
        "SOLUSDT": {
            "fallback_pulse": 2000.0,
            "max_display_ratio": 0.15,
            "pulse_normalization": 15000.0,
            "max_iceberg_ttl_sec": 300,
            "max_disguised_qty": 0.5,
            "strong_downtrend_threshold": -0.04,
            "weak_downtrend_threshold": -0.015,
        },
    }

    def __init__(self):
        # 外部依赖注入
        self._tactile_cortex = None
        self._behavioral_logger = None
        # 复用安全随机数生成器，避免重复系统调用
        self._secure_random = secrets.SystemRandom()
        # 盘口深度约束异常计数器（用于连续失败告警升级）
        self._depth_error_count = 0
        logger.info("IcebergManager 初始化完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        tactile_cortex: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）
        """
        if tactile_cortex is not None:
            required_methods = ['get_volume_pulse', 'get_orderbook_depth']
            missing = [m for m in required_methods if not hasattr(tactile_cortex, m)]
            if missing:
                logger.warning(f"TactileCortex 缺少方法: {missing}，相关功能降级")
                self._tactile_cortex = tactile_cortex
            else:
                self._tactile_cortex = tactile_cortex
                logger.info("TactileCortex 注入成功")
        else:
            logger.warning("TactileCortex 未注入，将使用保守的固定显露比例降级")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

    # ========== 公共接口 ==========
    def calculate_display_quantity(
        self,
        symbol: str,
        total_quantity: float,
        side: str = "buy",
        urgency: int = 5,
    ) -> Dict[str, Any]:
        """
        计算单次冰山订单的最优显露量及切片方案

        注意：本模块为无状态计算，执行引擎应在每次切片前根据实时成交进度重新调用以获取最新参数。
        返回的 recalculation_triggers 字段提供了明确的重新计算触发条件。
        """
        warnings: List[str] = []

        # 参数校验
        if total_quantity <= 0:
            logger.warning(f"无效总订单量: {total_quantity}")
            return {
                "status": "error",
                "reason": "总订单量必须为正数",
                "data": {},
                "warnings": ["invalid_total_quantity"],
            }
        if side not in ("buy", "sell"):
            logger.warning(f"无效订单方向: {side}，默认使用 buy")
            side = "buy"
            warnings.append("invalid_side_defaulted_to_buy")
        if not isinstance(urgency, int) or urgency < 0 or urgency > 10:
            urgency = max(0, min(10, int(urgency)))
            logger.warning(f"无效紧迫度，已钳位至 {urgency}")

        # 获取成交量脉搏（含数据新鲜度标记）
        volume_pulse, is_stale = self._get_volume_pulse(symbol)
        if is_stale:
            warnings.append("stale_volume_pulse_data")

        # 获取盘口深度约束（含挂单可信度评估，确保不低于最小显露量）
        max_safe_display = self._get_orderbook_depth_constraint(symbol, side)
        if max_safe_display > 0 and max_safe_display < self.DEFAULT_MIN_DISPLAY_QTY:
            logger.warning(
                "盘口安全深度 %.6f 低于最小显露量 %.6f，使用最小显露量",
                max_safe_display, self.DEFAULT_MIN_DISPLAY_QTY
            )
            max_safe_display = self.DEFAULT_MIN_DISPLAY_QTY

        # 获取品种流动性配置
        profile = IcebergManager._get_symbol_profile(symbol)
        max_ratio = profile.get("max_display_ratio", self.DEFAULT_MAX_DISPLAY_RATIO)
        pulse_norm = profile.get("pulse_normalization", self.VOLUME_PULSE_NORMALIZATION)

        # 计算基础显露比例（使用品种专属的标准化分母）
        base_ratio = min(
            max_ratio,
            max(
                self.DEFAULT_CONSERVATIVE_DISPLAY_RATIO,
                volume_pulse / pulse_norm
            )
        )

        # 数据陈旧折扣
        if is_stale:
            base_ratio *= self.STALE_DATA_DISCOUNT_FACTOR

        # 方向系数（做空时动态调整，降级时采用保守系数）
        side_multiplier = self._get_side_multiplier(side, symbol, profile)
        base_ratio *= side_multiplier

        # 紧迫度调整
        if urgency >= self.URGENCY_HIGH_THRESHOLD:
            base_ratio *= 1.3
        else:
            base_ratio *= 0.9

        # 安全随机抖动
        jitter = 0.9 + self._secure_random.random() * 0.2
        display_ratio = min(max_ratio, base_ratio * jitter)
        display_quantity = max(self.DEFAULT_MIN_DISPLAY_QTY, total_quantity * display_ratio)

        # 盘口深度硬约束
        if max_safe_display > 0:
            if display_quantity > max_safe_display:
                display_quantity = max_safe_display
        else:
            warnings.append("orderbook_depth_unavailable")

        # 确保显露量不低于最小下单量
        display_quantity = max(self.DEFAULT_MIN_DISPLAY_QTY, display_quantity)

        # 切片数量计算（高紧迫度下保持最小切片数以维持隐蔽性）
        if urgency >= self.URGENCY_HIGH_THRESHOLD:
            slice_count = max(
                self.MIN_SLICE_COUNT_HIGH_URGENCY,
                int(total_quantity / display_quantity) - 1
            )
        else:
            slice_count = max(2, int(total_quantity / display_quantity))

        # 切片上限约束
        if slice_count > self.MAX_SLICE_COUNT:
            slice_count = self.MAX_SLICE_COUNT
            display_quantity = total_quantity / slice_count
            display_quantity = max(self.DEFAULT_MIN_DISPLAY_QTY, display_quantity)

        # 初始切片间隔（执行引擎将根据动态规则调整）
        base_interval = 200 + (volume_pulse / 100)
        initial_interval_ms = self._secure_random.expovariate(
            1.0 / (base_interval * self.POISSON_LAMBDA_SCALE)
        )

        # 预估总执行时间
        estimated_total_ms = slice_count * initial_interval_ms
        if estimated_total_ms > self.MAX_ICEBERG_EXECUTION_SEC * 1000:
            warnings.append(
                f"预估执行时间 {estimated_total_ms/1000:.1f}s 超过上限 "
                f"{self.MAX_ICEBERG_EXECUTION_SEC}s，建议切换执行策略"
            )

        # 动态执行规则（附带版本号）
        dynamic_rules = {
            "version": self.DYNAMIC_RULES_VERSION,
            "accelerate_on_fill": 0.5,
            "decelerate_on_partial": 1.5,
            "pause_on_depth_drop": True,
            "max_interval_ms": 5000,
            "min_interval_ms": 100,
        }

        # 重新计算触发条件（语义明确化）
        recalculation_triggers = {
            "fill_ratio_exceed": 0.30,
            "depth_drop_ratio_exceed": {
                "value": 0.40,
                "baseline": "relative_to_entry_snapshot",
            },
            "price_deviation_bps_exceed": 15,
            "max_interval_seconds": 30,
        }

        # 获取品种级别的 TTL
        ttl = profile.get("max_iceberg_ttl_sec", self.MAX_ICEBERG_TTL_SEC)

        result = {
            "display_quantity": round(display_quantity, 6),
            "slice_count": slice_count,
            "slice_interval_ms_initial": round(initial_interval_ms, 1),
            "dynamic_rules": dynamic_rules,
            "max_safe_display": round(max_safe_display, 6),
            "estimated_total_ms": round(estimated_total_ms, 1),
            "recalculation_triggers": recalculation_triggers,
            "max_time_to_live_seconds": ttl,
        }

        logger.debug(
            "冰山订单计算: symbol=%s, side=%s, total=%.6f, display=%.6f, slices=%d, "
            "stale=%s, side_mult=%.2f, estimated_total=%.1fms, ttl=%ds",
            symbol, side, total_quantity, display_quantity, slice_count,
            is_stale, side_multiplier, estimated_total_ms, ttl
        )

        return {
            "status": "ok",
            "reason": f"显露量计算完成，显露比例 {display_ratio:.1%}",
            "data": result,
            "warnings": warnings,
        }

    def generate_disguised_slices(
        self,
        symbol: str,
        total_quantity: float,
        side: str = "buy",
    ) -> Dict[str, Any]:
        """
        生成伪装用的小额随机切片，建议仅在流动性充裕时由策略引擎显式触发

        Args:
            symbol: 交易对符号
            total_quantity: 实际订单总数量
            side: 订单方向，伪装切片方向应与实际订单一致
        """
        if total_quantity <= 0:
            return {
                "status": "error",
                "reason": "总订单量必须为正数",
                "data": {},
                "warnings": ["invalid_total_quantity"],
            }
        if side not in ("buy", "sell"):
            side = "buy"

        profile = IcebergManager._get_symbol_profile(symbol)
        max_disguised_qty = profile.get("max_disguised_qty", self.DEFAULT_SYMBOL_PROFILE["max_disguised_qty"])

        slice_count = self._secure_random.randint(
            self.DISGUISED_SLICE_COUNT_MIN, self.DISGUISED_SLICE_COUNT_MAX
        )
        max_qty_per_slice = min(
            total_quantity * self.DISGUISED_QTY_RATIO,
            max_disguised_qty
        )

        disguised_slices = []
        for _ in range(slice_count):
            qty = round(self._secure_random.uniform(0.0001, max_qty_per_slice), 6)
            delay_ms = round(self._secure_random.expovariate(1.0 / 500), 1)
            disguised_slices.append({"quantity": qty, "delay_ms": delay_ms, "side": side})

        return {
            "status": "ok",
            "reason": f"生成 {slice_count} 个伪装切片",
            "data": {"disguised_slices": disguised_slices},
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检，无副作用探测外部依赖可用性、映射表完整性及配置一致性
        """
        warnings = []
        try:
            _ = self.DEFAULT_CONSERVATIVE_DISPLAY_RATIO
        except Exception as e:
            return {"status": "error", "reason": f"常量异常: {e}", "data": {}, "warnings": [str(e)]}

        if not hasattr(self, '_secure_random') or self._secure_random is None:
            return {
                "status": "error",
                "reason": "安全随机数生成器未初始化",
                "data": {},
                "warnings": ["secure_random_unavailable"],
            }

        # 验证 BOOK_SIDE_MAP 完整性
        required_book_sides = ["buy", "sell"]
        for bs in required_book_sides:
            if bs not in self.BOOK_SIDE_MAP:
                warnings.append(f"BOOK_SIDE_MAP 缺少方向映射: {bs}")

        # 验证关键品种的流动性降级配置完整性
        required_symbols = ["BTCUSDT", "ETHUSDT"]
        for sym in required_symbols:
            if sym not in self.SYMBOL_LIQUIDITY_PROFILES:
                warnings.append(f"缺少品种 {sym} 的流动性降级配置")

        # 验证 DEFAULT_SYMBOL_PROFILE 包含所有必需字段
        required_profile_fields = [
            "fallback_pulse", "max_display_ratio", "pulse_normalization",
            "max_iceberg_ttl_sec", "max_disguised_qty",
            "strong_downtrend_threshold", "weak_downtrend_threshold"
        ]
        for field in required_profile_fields:
            if field not in self.DEFAULT_SYMBOL_PROFILE:
                warnings.append(f"DEFAULT_SYMBOL_PROFILE 缺少字段: {field}")

        # 验证所有已配置品种的 profile 字段完整性
        for sym, prof in self.SYMBOL_LIQUIDITY_PROFILES.items():
            for field in required_profile_fields:
                if field not in prof:
                    warnings.append(f"品种 {sym} 的流动性配置缺少字段: {field}")

        # 报告盘口深度异常状态
        if self._depth_error_count >= self.DEPTH_ERROR_THRESHOLD:
            warnings.append(f"盘口深度约束连续失败 {self._depth_error_count} 次，请检查 TactileCortex 连接状态")

        dependency_status = {}
        if self._tactile_cortex is not None:
            has_pulse = hasattr(self._tactile_cortex, 'get_volume_pulse')
            has_depth = hasattr(self._tactile_cortex, 'get_orderbook_depth')
            dependency_status["tactile_cortex"] = "ok" if (has_pulse and has_depth) else "missing_methods"
            if not (has_pulse and has_depth):
                warnings.append("tactile_cortex_missing_methods")
        else:
            dependency_status["tactile_cortex"] = "not_injected"
            warnings.append("tactile_cortex_not_injected")

        dependency_status["behavioral_logger"] = "injected" if self._behavioral_logger else "not_injected"

        overall = "ok" if not warnings else "degraded"
        return {
            "status": overall,
            "reason": f"依赖状态: {dependency_status}",
            "data": {"dependencies": dependency_status},
            "warnings": warnings,
        }

    # ========== 私有方法 ==========
    def _get_volume_pulse(self, symbol: str) -> Tuple[float, bool]:
        """获取成交量脉搏，返回 (值, 是否陈旧)"""
        pulse = None
        is_stale = False
        data_ts = 0.0
        if self._tactile_cortex and hasattr(self._tactile_cortex, 'get_volume_pulse'):
            try:
                result = self._tactile_cortex.get_volume_pulse(symbol)
                # 健壮的元组/序列/字典解包
                if isinstance(result, (tuple, list)) and len(result) == 2:
                    pulse, data_ts = float(result[0]), float(result[1])
                elif isinstance(result, dict):
                    pulse = float(result.get('value', result.get('pulse', 0)))
                    data_ts = float(result.get('timestamp', time.time()))
                else:
                    pulse = float(result)
                    data_ts = time.time()
                if pulse <= 0:
                    pulse = None
            except (TypeError, ValueError) as e:
                logger.warning(f"成交量脉搏数据解析失败: {e}")
                pulse = None
            except Exception as e:
                logger.warning(f"获取成交量脉搏异常: {e}")

        if pulse is None:
            profile = IcebergManager._get_symbol_profile(symbol)
            pulse = float(profile.get("fallback_pulse", self.DEFAULT_VOLUME_PULSE_FALLBACK))
            is_stale = True
        else:
            age_ms = (time.time() - data_ts) * 1000 if data_ts else 0
            if age_ms > self.MAX_DATA_STALENESS_MS:
                is_stale = True
                logger.debug(f"成交量脉搏数据陈旧 ({age_ms:.1f}ms)")

        return pulse, is_stale

    def _get_orderbook_depth_constraint(self, symbol: str, side: str) -> float:
        """
        基于对手方盘口深度（剔除可疑挂单）计算安全显露上限

        Args:
            symbol: 交易对符号
            side: 订单方向，"buy" 或 "sell"

        Returns:
            安全显露量上限（原始单位），若无法获取则返回 0（无约束）
        """
        if not self._tactile_cortex or not hasattr(self._tactile_cortex, 'get_orderbook_depth'):
            return 0.0

        # 将订单方向映射为订单簿查询方向
        book_side = self.BOOK_SIDE_MAP.get(side, "asks")

        try:
            depth_data = self._tactile_cortex.get_orderbook_depth(
                symbol, book_side, levels=self.ORDERBOOK_DEPTH_LEVELS
            )
            if isinstance(depth_data, (int, float)):
                total_depth = float(depth_data)
            elif isinstance(depth_data, (list, tuple)):
                total_depth = 0.0
                for level in depth_data:
                    if isinstance(level, dict) and 'volume' in level:
                        vol = float(level.get('volume', 0.0))
                        survival = float(level.get('survival_time_ms', self.MIN_WALL_SURVIVAL_MS))
                        if survival >= self.MIN_WALL_SURVIVAL_MS:
                            total_depth += vol
                        else:
                            total_depth += vol * self.GHOST_ORDER_DISCOUNT
                    elif isinstance(level, (int, float)):
                        total_depth += float(level)
            else:
                return 0.0

            if total_depth <= 0:
                return 0.0
            # 成功获取深度，重置连续失败计数器
            self._depth_error_count = max(0, self._depth_error_count - 1)
            return total_depth * self.MAX_ORDERBOOK_DEPTH_RATIO
        except Exception as e:
            self._depth_error_count += 1
            if self._depth_error_count >= self.DEPTH_ERROR_THRESHOLD:
                logger.error(
                    f"盘口深度约束连续失败 {self._depth_error_count} 次，"
                    f"请检查数据源 #RECOVERY: 检查 TactileCortex 连接状态"
                )
            logger.warning(f"盘口深度约束计算异常: {e}")
            return 0.0

    def _get_side_multiplier(self, side: str, symbol: str, profile: Optional[Dict[str, Any]] = None) -> float:
        """根据市场波动率与趋势动态计算方向系数（使用品种专属阈值）"""
        if side == "buy":
            return 1.0

        if profile is None:
            profile = IcebergManager._get_symbol_profile(symbol)

        vol_percentile = self._get_volatility_percentile(symbol)
        trend_strength = self._get_trend_strength(symbol)

        if trend_strength is None or vol_percentile is None:
            logger.debug("无法获取波动率或趋势强度，使用保守方向系数 %.2f", self.SELL_SIDE_FALLBACK_MULTIPLIER)
            return self.SELL_SIDE_FALLBACK_MULTIPLIER

        # 钳位并归一化波动率分位 (0-100)
        # 语义: current_exceeds_history_pct (当前波动率超过历史百分之多少)
        vol_pct = max(0.0, min(100.0, float(vol_percentile)))

        strong_dt = profile.get("strong_downtrend_threshold", self.DEFAULT_SYMBOL_PROFILE["strong_downtrend_threshold"])
        weak_dt = profile.get("weak_downtrend_threshold", self.DEFAULT_SYMBOL_PROFILE["weak_downtrend_threshold"])

        if trend_strength < strong_dt:
            # 强下跌趋势，波动率越高（分位越大）市场越恐慌，乘数越保守
            return max(0.5, 0.6 + (1.0 - vol_pct / 100.0) * 0.3)
        elif trend_strength < weak_dt:
            return 0.75
        else:
            return 0.85

    def _get_volatility_percentile(self, symbol: str) -> Optional[float]:
        """获取当前波动率分位，失败返回 None。
        语义: current_exceeds_history_pct (当前波动率超过历史百分之多少)"""
        if self._tactile_cortex and hasattr(self._tactile_cortex, 'get_volatility_percentile'):
            try:
                val = self._tactile_cortex.get_volatility_percentile(symbol)
                if isinstance(val, (int, float)):
                    return float(val)
            except Exception as e:
                logger.warning(f"获取波动率分位异常: {e}")
        return None

    def _get_trend_strength(self, symbol: str) -> Optional[float]:
        """获取当前趋势强度，失败返回 None"""
        if self._tactile_cortex and hasattr(self._tactile_cortex, 'get_trend_strength'):
            try:
                val = self._tactile_cortex.get_trend_strength(symbol)
                if isinstance(val, (int, float)):
                    return float(val)
            except Exception as e:
                logger.warning(f"获取趋势强度异常: {e}")
        return None

    @classmethod
    def _get_symbol_profile(cls, symbol: str) -> Dict[str, Any]:
        """获取品种流动性配置降级参数（类方法，确保访问类属性）。
        若品种不在 SYMBOL_LIQUIDITY_PROFILES 中，返回 DEFAULT_SYMBOL_PROFILE。"""
        return cls.SYMBOL_LIQUIDITY_PROFILES.get(symbol, cls.DEFAULT_SYMBOL_PROFILE)
