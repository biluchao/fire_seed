"""
火种系统 · 因子预处理器 (FactorPreprocessor) — 深度精细化

核心职责：
1. 对原始因子值序列执行去极值（Winsorize）、缺失值填充与毛刺平滑，确保输入评分卡的数据质量。
2. 根据当前市场波动率分位自适应选择预处理策略，避免在高低波动环境下引入系统性偏差。

外部依赖（真实模块接口）：
- 无外部模块依赖（本模块为无状态计算工具）。若需感知波动率分位，调用方通过 process() 的 vol_percentile 参数传入。

接口契约：
- process(factor_values: List[float], factor_name: str = "unknown", vol_percentile: Optional[float] = None) -> Dict[str, Any]
  输出字典固定包含 "status" (str), "processed_values" (List[float]), "reason" (str), "warnings" (List[str])
- health_check() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "message" (str)

异常与降级：
- 当输入因子值全为空或长度为零时，返回空列表并标记 "empty_input" 状态，reason 说明原因。
- 所有内部计算异常被捕获，返回原始值列表作为降级输出，并在 warnings 中记录异常详情。

资源管理：
- 本模块为纯计算工具，无状态，不持有任何外部资源，无需显式释放。
"""

import logging
import math
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class FactorPreprocessor:
    """因子预处理器：去极值、平滑与缺失值填充"""

    # 类常量（默认配置，附带单位与取值范围注释）
    DEFAULT_WINSORIZE_LOWER = 0.01         # 默认钳制下分位，[0.0, 0.10]
    DEFAULT_WINSORIZE_UPPER = 0.99         # 默认钳制上分位，[0.90, 1.0]
    DEFAULT_SMOOTH_WINDOW = 3              # 默认中值滤波窗口，Tick，[1, 10]
    HIGH_VOL_WINSORIZE_LOWER = 0.005       # 高波动钳制下分位，放宽以保留尾部信号
    HIGH_VOL_WINSORIZE_UPPER = 0.995       # 高波动钳制上分位
    LOW_VOL_WINSORIZE_LOWER = 0.02         # 低波动钳制下分位，收紧以过滤噪声
    LOW_VOL_WINSORIZE_UPPER = 0.98         # 低波动钳制上分位
    VOL_PERCENTILE_HIGH = 80               # 高波动分位阈值，[60, 95]
    VOL_PERCENTILE_LOW = 30                # 低波动分位阈值，[5, 50]
    MIN_VALUES_FOR_STATS = 10              # 计算统计量所需最小样本数，[5, 50]

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """可接受配置字典覆盖默认参数。"""
        self._config = config or {}
        # 从配置读取（如提供），否则使用类常量
        self._winsorize_lower = float(
            self._config.get("winsorize_lower", self.DEFAULT_WINSORIZE_LOWER)
        )
        self._winsorize_upper = float(
            self._config.get("winsorize_upper", self.DEFAULT_WINSORIZE_UPPER)
        )
        self._smooth_window = int(
            self._config.get("smooth_window", self.DEFAULT_SMOOTH_WINDOW)
        )
        # 边界保护
        self._winsorize_lower = max(0.0, min(0.10, self._winsorize_lower))
        self._winsorize_upper = max(0.90, min(1.0, self._winsorize_upper))
        self._smooth_window = max(1, min(10, self._smooth_window))
        logger.info(
            f"FactorPreprocessor 初始化完成，"
            f"钳制: [{self._winsorize_lower}, {self._winsorize_upper}], "
            f"平滑窗口: {self._smooth_window}"
        )

    # ────────────────────────── 公共接口 ──────────────────────────
    def process(
        self,
        factor_values: List[float],
        factor_name: str = "unknown",
        vol_percentile: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        对因子值序列执行完整的预处理流水线：去极值 → 缺失值填充 → 毛刺平滑。

        Args:
            factor_values: 原始因子值列表，可能包含 None。
            factor_name: 因子名称，用于日志追踪。
            vol_percentile: 当前波动率分位（0-100），用于自适应策略选择。若为 None 则使用默认参数。

        Returns:
            标准化字典，包含处理后的因子值列表。
        """
        warnings: List[str] = []

        # 1. 输入校验
        if not factor_values or len(factor_values) == 0:
            return {
                "status": "empty_input",
                "processed_values": [],
                "reason": f"因子 {factor_name} 输入为空",
                "warnings": ["输入序列长度为0"],
            }

        # 2. 选择钳制参数（根据波动率分位自适应）
        lower, upper = self._select_winsorize_bounds(vol_percentile)

        try:
            # 3. 缺失值填充
            filled, fill_warnings = self._fill_missing(factor_values, factor_name)
            warnings.extend(fill_warnings)

            # 4. 极端值钳制
            clamped = self._winsorize(filled, lower, upper)

            # 5. 毛刺平滑
            smoothed = self._median_smooth(clamped)

            # 6. 生成处理说明
            reason = (
                f"因子 {factor_name} 预处理完成: "
                f"输入 {len(factor_values)} 个值, "
                f"波动率分位 {vol_percentile if vol_percentile is not None else '默认'}, "
                f"钳制 [{lower:.3f}, {upper:.3f}], "
                f"平滑窗口 {self._smooth_window}"
            )

            return {
                "status": "ok",
                "processed_values": smoothed,
                "reason": reason,
                "warnings": warnings,
            }
        except Exception as e:
            logger.error(f"因子 {factor_name} 预处理异常: {e}", exc_info=True)
            warnings.append(f"预处理异常: {str(e)[:100]}，返回原始值")
            return {
                "status": "error",
                "processed_values": factor_values,
                "reason": f"预处理失败，返回原始值: {str(e)[:100]}",
                "warnings": warnings,
            }

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检：用含异常值和缺失值的模拟数据测试完整流水线。"""
        try:
            instance = cls()
            test_values = [
                1.0, 2.0, 1.5, None, 100.0, -50.0, 1.8, 1.9, 2.1, 1.7,
                1.6, None, 1.8, 2.0, 1.9, 1.7, 1.8, 1.6, 1.9, 2.0,
                1.5, 1.8, 200.0, 1.7, 1.9
            ]
            result = instance.process(test_values, "test_factor", vol_percentile=60.0)
            if result["status"] != "ok":
                return {"status": "error", "message": f"预处理失败: {result['reason']}"}
            if len(result["processed_values"]) != len(test_values):
                return {"status": "error", "message": "输出长度与输入不一致"}
            processed = result["processed_values"]
            # 验证极端值已被钳制（输出最大值不应超过正常范围的10倍）
            if max(processed) > 10.0:
                return {"status": "error", "message": "极端值未被正确钳制"}
            # 验证缺失值已填充（不应有 None）
            if any(v is None for v in processed):
                return {"status": "error", "message": "缺失值未填充"}

            # 空输入测试
            empty_result = instance.process([], "empty")
            if empty_result["status"] != "empty_input":
                return {"status": "error", "message": "空输入未正确处理"}

            return {"status": "ok", "message": "预处理测试通过"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _select_winsorize_bounds(self, vol_percentile: Optional[float]) -> Tuple[float, float]:
        """根据波动率分位选择最优钳制边界。"""
        if vol_percentile is None:
            return self._winsorize_lower, self._winsorize_upper
        if vol_percentile >= self.VOL_PERCENTILE_HIGH:
            return self.HIGH_VOL_WINSORIZE_LOWER, self.HIGH_VOL_WINSORIZE_UPPER
        elif vol_percentile <= self.VOL_PERCENTILE_LOW:
            return self.LOW_VOL_WINSORIZE_LOWER, self.LOW_VOL_WINSORIZE_UPPER
        else:
            return self._winsorize_lower, self._winsorize_upper

    def _fill_missing(
        self, values: List[float], name: str
    ) -> Tuple[List[float], List[str]]:
        """缺失值填充：前向填充 + 统计均值填充混合策略。"""
        warnings: List[str] = []
        filled: List[float] = []
        last_valid: Optional[float] = None
        # 先找一个有效的初始值
        for v in values:
            if v is not None:
                last_valid = v
                break
        if last_valid is None:
            # 所有值都缺失，用0.0填充
            warnings.append(f"因子 {name} 所有值缺失，填充0.0")
            return [0.0] * len(values), warnings

        for v in values:
            if v is not None:
                filled.append(v)
                last_valid = v
            else:
                filled.append(last_valid)
                warnings.append(f"因子 {name} 缺失值已前向填充")
        return filled, warnings

    @staticmethod
    def _winsorize(values: List[float], lower: float, upper: float) -> List[float]:
        """极端值钳制：将超出分位边界的值钳制到边界。"""
        if len(values) < 3:
            return list(values)

        sorted_vals = sorted(values)
        n = len(sorted_vals)
        lower_idx = max(0, int(n * lower) - 1)
        upper_idx = min(n - 1, int(n * upper))
        lower_bound = sorted_vals[lower_idx]
        upper_bound = sorted_vals[upper_idx]

        return [
            lower_bound if v < lower_bound else (upper_bound if v > upper_bound else v)
            for v in values
        ]

    def _median_smooth(self, values: List[float]) -> List[float]:
        """中值滤波平滑：消除单Tick毛刺。"""
        if self._smooth_window <= 1 or len(values) < 3:
            return list(values)

        half = self._smooth_window // 2
        smoothed: List[float] = []
        for i in range(len(values)):
            start = max(0, i - half)
            end = min(len(values), i + half + 1)
            window = values[start:end]
            window_sorted = sorted(window)
            smoothed.append(window_sorted[len(window_sorted) // 2])
        return smoothed

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        """数值边界钳制辅助函数。"""
        return max(lower, min(upper, value))
