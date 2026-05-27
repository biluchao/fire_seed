"""
火种系统 · 紧缩利润阶梯 (ProfitCompression)

核心职责：
1. 根据持仓浮盈幅度与市场波动率，计算非线性紧缩的止损价格，实现利润阶梯式锁定
2. 提供当前持仓所处的紧缩阶段查询，供加仓管理、移动止损等下游模块协同决策

外部依赖（真实模块接口）：
- 无。本模块为纯计算模块，所有必要数据（ATR、波动率分位、熔断状态、M12方向等）均通过方法参数传入，
  不持有任何外部模块引用。调用方需自行从感知模块或风控模块获取这些数据后传入。

接口契约：
- calculate_new_stop(entry_price, current_price, direction, lifecycle_stage, compression_count, atr,
                     vol_percentile, is_circuit_breaker_active, m12_direction)
  -> Dict[str, Any]
  输出字典固定包含 "new_stop_candidate" (float), "compression_stage" (str),
  "profit_atr_ratio" (float), "stop_atr_mult" (float), "reason" (str), "warnings" (List[str])
- get_compression_stage(current_price, entry_price, direction, atr, lifecycle_stage)
  -> Dict[str, Any]
  输出字典固定包含 "stage" (str), "profit_atr_ratio" (float), "reason" (str), "warnings" (List[str])
- health_check() -> Dict[str, Any] : 模块自检

异常与降级：
- 本模块不依赖外部模块，所有降级值均为类常量。当传入的 ATR 无效时自动使用 DEFAULT_ATR。
- 当 direction 无效时直接返回错误，拒绝执行，保证风控安全。
- 当 lifecycle_stage 未知时，降级为 "maturity" 并在 warnings 中记录。
- 所有错误返回均包含安全的止损候选值（保本价）和明确的警告信息，调用方可安全使用。
- 止损倍数有硬性下限保护（MIN_STOP_ATR = 0.05），防止极端叠加场景下出现负数或零止损距离。

资源管理：
- 纯计算模块，无状态，无需资源管理。
"""

import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class ProfitCompression:
    """紧缩利润阶梯：基于浮盈幅度和波动率的非线性止损计算"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 紧缩阶梯阈值（浮盈/ATR 比例）
    THRESHOLD_MICRO_PROFIT = 0.5      # 微盈阈值，浮盈/ATR，取值范围 [0.3, 0.8]
    THRESHOLD_MEDIUM_PROFIT = 1.0     # 中盈阈值，浮盈/ATR，取值范围 [0.8, 1.5]
    THRESHOLD_LARGE_PROFIT = 2.0      # 大盈阈值，浮盈/ATR，取值范围 [1.5, 3.0]
    THRESHOLD_EXTREME_PROFIT = 3.0    # 极端盈利阈值，浮盈/ATR，取值范围 [2.5, 5.0]

    # 浮点比较容差，防止边界抖动导致阶段误判
    EPSILON = 1e-9                    # 无量纲，用于 >= 比较的微容差

    # 各阶段止损 ATR 倍数
    STOP_ATR_MICRO = 0.6              # 微盈阶段，取值范围 [0.3, 0.8]
    STOP_ATR_MEDIUM = 0.8             # 中盈阶段，取值范围 [0.6, 1.0]
    STOP_ATR_LARGE = 0.4              # 大盈阶段，取值范围 [0.2, 0.5]
    STOP_ATR_EXTREME = 0.15           # 极端盈利阶段，取值范围 [0.1, 0.25]

    # 止损倍数硬性下限，防止 M12 逆势 + 熔断叠加后出现负数或零
    MIN_STOP_ATR = 0.05               # 最低止损 ATR 倍数，取值范围 [0.02, 0.10]

    # 加仓次数额外收紧系数（加仓次数越多，止损越紧）
    # 0-3 次使用精确映射，4 次及以上统一使用 MAX_TIGHTEN
    COMPRESSION_COUNT_TIGHTEN = {
        0: 1.00,   # 无加仓，不额外收紧
        1: 1.00,   # 1次加仓，暂不额外收紧
        2: 0.95,   # 2次加仓，收紧5%
        3: 0.88,   # 3次加仓，收紧12%
    }
    COMPRESSION_COUNT_MAX_TIGHTEN = 0.80  # 4次及以上统一收紧20%

    # 熔断时加速紧缩系数
    CIRCUIT_BREAKER_ACCELERATION = 1.5  # 无量纲，取值范围 [1.2, 2.0]

    # 降级默认值
    DEFAULT_ATR = 50.0                # 默认 ATR（价格单位），用于无法获取真实 ATR 时的安全回退
    DEFAULT_VOL_PERCENTILE = 50       # 默认波动率分位，无量纲，取值范围 [0, 100]

    # 已知的合法生命周期阶段
    VALID_LIFECYCLE_STAGES = (
        "incubation", "acceleration", "maturity", "decline", "termination"
    )

    # M12 协同调整系数（趋势越强，紧缩越宽松）
    M12_STRONG_TREND_DELAY = 0.3      # 强趋势下阈值延迟（ATR倍数），取值范围 [0.1, 0.5]
    M12_FLAT_ACCELERATE = 0.2         # 震荡下阈值提前（ATR倍数），取值范围 [0.1, 0.4]
    M12_COUNTER_TREND_ACCELERATE = 1.5  # 逆势加速系数，无量纲，取值范围 [1.2, 2.0]

    # ========== 公共接口 ==========
    @classmethod
    def calculate_new_stop(
        cls,
        entry_price: float,
        current_price: float,
        direction: int,
        lifecycle_stage: str = "maturity",
        compression_count: int = 0,
        atr: Optional[float] = None,
        vol_percentile: Optional[float] = None,
        is_circuit_breaker_active: bool = False,
        m12_direction: str = "flat",
    ) -> Dict[str, Any]:
        """
        计算紧缩利润后的新止损价格

        Args:
            entry_price: 开仓均价
            current_price: 当前市价
            direction: 持仓方向 (1=多头, -1=空头)，其他值直接返回错误
            lifecycle_stage: 当前生命周期阶段
            compression_count: 已加仓次数，用于分层紧缩加速与额外止损收紧
            atr: 当前 ATR 值，若为 None 则使用默认值
            vol_percentile: 当前波动率分位 (0-100)，若为 None 则使用默认值
            is_circuit_breaker_active: 熔断是否激活
            m12_direction: M12 均线方向 (strong_up/weak_up/flat/weak_down/strong_down)

        Returns:
            标准响应字典，包含 new_stop_candidate, compression_stage, reason, warnings
        """
        warnings: List[str] = []

        # 参数校验：direction 无效直接拒绝
        if direction not in (1, -1):
            logger.warning(f"无效方向: {direction}，必须为 1(多头) 或 -1(空头)")
            return {
                "status": "error",
                "reason": f"无效方向: {direction}，必须为 1(多头) 或 -1(空头)",
                "data": {
                    "new_stop_candidate": entry_price if entry_price > 0 else 0.0,
                    "compression_stage": "micro",
                    "profit_atr_ratio": 0.0,
                    "stop_atr_mult": cls.STOP_ATR_MICRO,
                },
                "warnings": ["invalid_direction", "fallback_values_used"],
            }

        if entry_price <= 0 or current_price <= 0:
            logger.warning(f"价格参数无效: entry={entry_price}, current={current_price}")
            return {
                "status": "error",
                "reason": "价格参数必须为正数",
                "data": {
                    "new_stop_candidate": entry_price if entry_price > 0 else 0.0,
                    "compression_stage": "micro",
                    "profit_atr_ratio": 0.0,
                    "stop_atr_mult": cls.STOP_ATR_MICRO,
                },
                "warnings": ["invalid_price", "fallback_values_used"],
            }

        # lifecycle_stage 校验
        if lifecycle_stage not in cls.VALID_LIFECYCLE_STAGES:
            logger.warning(f"未知生命周期阶段: {lifecycle_stage}，降级为 maturity")
            warnings.append(f"unknown_lifecycle_stage:{lifecycle_stage}")
            lifecycle_stage = "maturity"

        effective_atr = atr if atr and atr > 0 else cls.DEFAULT_ATR
        effective_vol_pct = vol_percentile if vol_percentile is not None else cls.DEFAULT_VOL_PERCENTILE

        # 计算浮盈（统一为正数表示盈利幅度）
        profit_atr_ratio = abs(current_price - entry_price) / effective_atr

        # 确定紧缩阶段（加仓次数影响阶段加速）
        stage = cls._determine_stage(profit_atr_ratio, lifecycle_stage, compression_count)
        base_stop_atr = cls._get_base_stop_atr(stage)

        # 加仓次数额外收紧止损倍数（仓位越大止损越紧）
        tighten = cls.COMPRESSION_COUNT_TIGHTEN.get(
            compression_count, cls.COMPRESSION_COUNT_MAX_TIGHTEN
        )
        base_stop_atr *= tighten

        # M12 协同调整
        base_stop_atr += cls._get_m12_adjustment(m12_direction, direction, profit_atr_ratio)

        # 熔断加速
        if is_circuit_breaker_active:
            base_stop_atr *= cls.CIRCUIT_BREAKER_ACCELERATION
            logger.debug("熔断激活，紧缩加速 %.1f 倍", cls.CIRCUIT_BREAKER_ACCELERATION)

        # 波动率自适应：高波动稍宽松，低波动更敏感
        if effective_vol_pct > 70:
            base_stop_atr *= 1.15
        elif effective_vol_pct < 30:
            base_stop_atr *= 0.85

        # 硬性下限保护：防止 M12 逆势 + 熔断叠加后止损倍数变为负数或零
        base_stop_atr = max(cls.MIN_STOP_ATR, base_stop_atr)

        # 计算新止损价（方向对称）
        if direction == 1:
            new_stop = current_price - effective_atr * base_stop_atr
            new_stop = min(new_stop, entry_price)  # 多头止损不高于入场价
        else:
            new_stop = current_price + effective_atr * base_stop_atr
            new_stop = max(new_stop, entry_price)  # 空头止损不低于入场价

        reason = (
            f"紧缩阶段: {stage}, 浮盈/ATR={profit_atr_ratio:.2f}, "
            f"止损ATR倍数={base_stop_atr:.2f}, 新止损={new_stop:.4f}"
        )
        logger.info(
            "紧缩计算: entry=%.4f current=%.4f dir=%d stage=%s new_stop=%.4f profit_atr=%.2f count=%d",
            entry_price, current_price, direction, stage, new_stop, profit_atr_ratio, compression_count
        )

        return {
            "status": "ok",
            "reason": reason,
            "data": {
                "new_stop_candidate": round(new_stop, 8),
                "compression_stage": stage,
                "profit_atr_ratio": round(profit_atr_ratio, 4),
                "stop_atr_mult": round(base_stop_atr, 2),
            },
            "warnings": warnings,
        }

    @classmethod
    def get_compression_stage(
        cls,
        current_price: float,
        entry_price: float,
        direction: int,
        atr: Optional[float] = None,
        lifecycle_stage: str = "maturity",
    ) -> Dict[str, Any]:
        """
        查询当前持仓所处的紧缩阶段（不计算新止损）

        Args:
            current_price: 当前市价
            entry_price: 开仓均价
            direction: 持仓方向 (1=多头, -1=空头)
            atr: 当前 ATR 值
            lifecycle_stage: 当前生命周期阶段

        Returns:
            标准响应字典，data 包含 stage, profit_atr_ratio
        """
        warnings: List[str] = []

        if direction not in (1, -1):
            return {
                "status": "error",
                "reason": f"无效方向: {direction}",
                "data": {"stage": "micro", "profit_atr_ratio": 0.0},
                "warnings": ["invalid_direction", "fallback_values_used"],
            }

        if entry_price <= 0 or current_price <= 0:
            return {
                "status": "error",
                "reason": "价格参数无效",
                "data": {"stage": "micro", "profit_atr_ratio": 0.0},
                "warnings": ["invalid_price", "fallback_values_used"],
            }

        if lifecycle_stage not in cls.VALID_LIFECYCLE_STAGES:
            logger.warning(f"未知生命周期阶段: {lifecycle_stage}，降级为 maturity")
            warnings.append(f"unknown_lifecycle_stage:{lifecycle_stage}")
            lifecycle_stage = "maturity"

        effective_atr = atr if atr and atr > 0 else cls.DEFAULT_ATR
        profit_atr_ratio = abs(current_price - entry_price) / effective_atr
        stage = cls._determine_stage(profit_atr_ratio, lifecycle_stage, 0)

        return {
            "status": "ok",
            "reason": f"当前紧缩阶段: {stage}",
            "data": {
                "stage": stage,
                "profit_atr_ratio": round(profit_atr_ratio, 4),
            },
            "warnings": warnings,
        }

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """
        模块自检：验证核心计算逻辑、常量完整性、降级路径、加仓加速逻辑、
        浮点边界鲁棒性、极端叠加场景下限保护
        """
        try:
            # 1. 正常多头
            res = cls.calculate_new_stop(100.0, 102.0, 1, atr=2.0)
            assert res["status"] == "ok", f"多头计算失败: {res['reason']}"

            # 2. 正常空头
            res = cls.calculate_new_stop(100.0, 98.0, -1, atr=2.0)
            assert res["status"] == "ok", f"空头计算失败: {res['reason']}"

            # 3. 降级路径：ATR 缺失
            res = cls.calculate_new_stop(100.0, 102.0, 1, atr=None)
            assert res["status"] == "ok", f"降级路径失败: {res['reason']}"

            # 4. 错误输入：价格无效
            res = cls.calculate_new_stop(-1.0, 102.0, 1, atr=2.0)
            assert res["status"] == "error", "错误输入应返回error"
            assert "fallback_values_used" in res.get("warnings", []), "缺少降级标记"

            # 5. 错误输入：方向无效
            res = cls.calculate_new_stop(100.0, 102.0, 0, atr=2.0)
            assert res["status"] == "error", "无效方向应返回error"
            assert "invalid_direction" in res.get("warnings", []), "缺少invalid_direction标记"

            # 6. 加仓加速阶段跳跃验证
            stage_low = cls._determine_stage(1.0, "maturity", 0)   # 无加仓 → medium
            stage_high = cls._determine_stage(1.0, "maturity", 4)  # 4次加仓 → large
            assert stage_low == "medium", f"0次加仓应为medium，实际{stage_low}"
            assert stage_high == "large", f"4次加仓应跳至large，实际{stage_high}"

            # 7. 同一阶段下加仓收紧系数精度验证（micro 阶段，profit_atr=0.6）
            res_no_add = cls.calculate_new_stop(
                100.0, 100.6, 1, atr=1.0, compression_count=0
            )
            res_add2 = cls.calculate_new_stop(
                100.0, 100.6, 1, atr=1.0, compression_count=2
            )
            res_add3 = cls.calculate_new_stop(
                100.0, 100.6, 1, atr=1.0, compression_count=3
            )
            res_add4 = cls.calculate_new_stop(
                100.0, 100.6, 1, atr=1.0, compression_count=4
            )
            # 同一阶段（micro）下，基础倍数相同（0.6），但收紧系数不同
            base = cls.STOP_ATR_MICRO  # 0.6
            assert abs(res_no_add["data"]["stop_atr_mult"] - base * 1.00) < 0.01, \
                "0次加仓收紧系数应为1.00"
            assert abs(res_add2["data"]["stop_atr_mult"] - base * 0.95) < 0.01, \
                "2次加仓收紧系数应为0.95"
            assert abs(res_add3["data"]["stop_atr_mult"] - base * 0.88) < 0.01, \
                "3次加仓收紧系数应为0.88"
            assert abs(res_add4["data"]["stop_atr_mult"] - base * 0.80) < 0.01, \
                "4次加仓收紧系数应为0.80"

            # 8. 浮点边界鲁棒性验证（EPSILON 容差）
            exact_at_threshold = cls.THRESHOLD_MEDIUM_PROFIT
            slightly_below = exact_at_threshold - cls.EPSILON * 0.5
            stage_exact = cls._determine_stage(exact_at_threshold, "maturity", 0)
            stage_slightly_below = cls._determine_stage(slightly_below, "maturity", 0)
            assert stage_exact == "medium", \
                f"恰好等于阈值应进入medium，实际{stage_exact}"
            assert stage_slightly_below == "medium", \
                f"略低于阈值（在EPSILON容差内）应仍为medium，实际{stage_slightly_below}"

            # 9. 极端叠加场景下限保护验证
            # 模拟逆势持仓 + 熔断 + 低浮盈的极端场景
            res_extreme = cls.calculate_new_stop(
                100.0, 100.5, 1,
                atr=2.0,
                m12_direction="strong_down",  # 多头但M12强向下
                is_circuit_breaker_active=True,  # 熔断激活
                compression_count=0,
            )
            assert res_extreme["data"]["stop_atr_mult"] >= cls.MIN_STOP_ATR, \
                f"止损倍数({res_extreme['data']['stop_atr_mult']})应不低于MIN_STOP_ATR({cls.MIN_STOP_ATR})"

            # 10. 常量顺序验证
            assert (cls.THRESHOLD_MICRO_PROFIT < cls.THRESHOLD_MEDIUM_PROFIT
                    < cls.THRESHOLD_LARGE_PROFIT < cls.THRESHOLD_EXTREME_PROFIT), \
                "紧缩阈值必须递增"
            assert (0 < cls.STOP_ATR_EXTREME < cls.STOP_ATR_LARGE
                    < cls.STOP_ATR_MEDIUM), "止损倍数必须递减"
            assert cls.MIN_STOP_ATR > 0, "MIN_STOP_ATR 必须为正数"
            assert cls.EPSILON > 0, "EPSILON 必须为正数"

            return {
                "status": "ok",
                "reason": "ProfitCompression 全部自检通过（含浮点鲁棒性、系数精度、极端叠加验证）",
                "data": {
                    "thresholds": {
                        "micro": cls.THRESHOLD_MICRO_PROFIT,
                        "medium": cls.THRESHOLD_MEDIUM_PROFIT,
                        "large": cls.THRESHOLD_LARGE_PROFIT,
                        "extreme": cls.THRESHOLD_EXTREME_PROFIT,
                    },
                    "stop_atr_multipliers": {
                        "micro": cls.STOP_ATR_MICRO,
                        "medium": cls.STOP_ATR_MEDIUM,
                        "large": cls.STOP_ATR_LARGE,
                        "extreme": cls.STOP_ATR_EXTREME,
                    },
                    "compression_tighten": cls.COMPRESSION_COUNT_TIGHTEN,
                    "max_tighten": cls.COMPRESSION_COUNT_MAX_TIGHTEN,
                    "min_stop_atr": cls.MIN_STOP_ATR,
                    "epsilon": cls.EPSILON,
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查类常量与核心计算逻辑")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    @classmethod
    def _determine_stage(
        cls, profit_atr_ratio: float, lifecycle_stage: str, compression_count: int
    ) -> str:
        """
        根据浮盈幅度、生命周期阶段和加仓次数确定紧缩阶段

        使用 EPSILON 容差防止浮点精度导致的边界抖动。

        Args:
            profit_atr_ratio: 浮盈 / ATR
            lifecycle_stage: 生命周期阶段
            compression_count: 已加仓次数

        Returns:
            紧缩阶段字符串: micro/medium/large/extreme
        """
        eps = cls.EPSILON

        # 孵化期始终为 micro，不因加仓加速
        if lifecycle_stage == "incubation":
            return "micro"

        # 加仓次数越多，跳过微盈阶段，更快进入激进紧缩
        if compression_count >= 4:
            if profit_atr_ratio >= cls.THRESHOLD_LARGE_PROFIT - eps:
                return "extreme"
            if profit_atr_ratio >= cls.THRESHOLD_MEDIUM_PROFIT - eps:
                return "large"
            return "medium"
        if compression_count >= 2:
            if profit_atr_ratio >= cls.THRESHOLD_LARGE_PROFIT - eps:
                return "extreme"
            if profit_atr_ratio >= cls.THRESHOLD_MEDIUM_PROFIT - eps:
                return "large"
            if profit_atr_ratio >= cls.THRESHOLD_MICRO_PROFIT - eps:
                return "medium"
            return "micro"

        # 标准逻辑（无加仓或仅1次加仓）
        if profit_atr_ratio >= cls.THRESHOLD_EXTREME_PROFIT - eps:
            return "extreme"
        if profit_atr_ratio >= cls.THRESHOLD_LARGE_PROFIT - eps:
            return "large"
        if profit_atr_ratio >= cls.THRESHOLD_MEDIUM_PROFIT - eps:
            return "medium"
        if profit_atr_ratio >= cls.THRESHOLD_MICRO_PROFIT - eps:
            return "micro"
        return "micro"

    @classmethod
    def _get_base_stop_atr(cls, stage: str) -> float:
        """根据紧缩阶段返回基础止损 ATR 倍数"""
        stage_map = {
            "micro": cls.STOP_ATR_MICRO,
            "medium": cls.STOP_ATR_MEDIUM,
            "large": cls.STOP_ATR_LARGE,
            "extreme": cls.STOP_ATR_EXTREME,
        }
        return stage_map.get(stage, cls.STOP_ATR_MICRO)

    @classmethod
    def _get_m12_adjustment(
        cls, m12_direction: str, position_direction: int, profit_atr_ratio: float
    ) -> float:
        """
        根据 M12 方向计算紧缩阈值的调整量

        Args:
            m12_direction: M12 方向 (strong_up/weak_up/flat/weak_down/strong_down)
            position_direction: 持仓方向 (1=多头, -1=空头)
            profit_atr_ratio: 浮盈/ATR

        Returns:
            ATR 倍数调整量（正数为延迟紧缩，负数为加速紧缩）
        """
        is_aligned = (
            (position_direction == 1 and m12_direction in ("strong_up", "weak_up"))
            or (position_direction == -1 and m12_direction in ("strong_down", "weak_down"))
        )
        is_counter = (
            (position_direction == 1 and m12_direction in ("strong_down", "weak_down"))
            or (position_direction == -1 and m12_direction in ("strong_up", "weak_up"))
        )

        if is_counter:
            return -cls.M12_COUNTER_TREND_ACCELERATE * cls.STOP_ATR_MEDIUM
        if is_aligned and m12_direction in ("strong_up", "strong_down"):
            return cls.M12_STRONG_TREND_DELAY
        if m12_direction == "flat":
            return -cls.M12_FLAT_ACCELERATE
        return 0.0
