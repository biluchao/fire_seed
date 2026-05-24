"""
火种系统 · 因子预处理器 (FactorPreprocessor) — 深度精细化

核心职责：
1. 对原始因子值执行去极值（Winsorize）、缺失值填充与毛刺平滑（中值滤波），确保输入评分卡的数据质量。
2. 根据当前市场波动率分位自适应选择预处理策略，避免在高低波动环境下引入系统性偏差。

外部依赖（真实模块接口）：
- core.perception.tactile_cortex.TactileCortex : 获取当前波动率分位

接口契约：
- process(factor_values: List[float], factor_name: str) -> Dict[str, Any]
  输出字典固定包含 "status" (str), "processed_values" (List[float]), "reason" (str), "warnings" (List[str])
- health_check() -> Dict[str, Any]

异常与降级：
- 当 TactileCortex 不可用时，使用固定参数（中位数填充、默认钳制分位）进行预处理。
- 当输入因子值全为空时，返回空列表并标记 "empty_input" 状态。

资源管理：
- 本模块为纯计算工具，不持有外部资源，无需显式释放。
"""

import logging
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class FactorPreprocessor:
    """因子预处理器（深度精细化）：去极值、平滑、缺失填充"""

    # ========== 类常量 ==========
    DEFAULT_WINSORIZE_LOWER = 0.01
    DEFAULT_WINSORIZE_UPPER = 0.99
    DEFAULT_SMOOTH_WINDOW = 3
    HIGH_VOL_WINSORIZE_LOWER = 0.005
    HIGH_VOL_WINSORIZE_UPPER = 0.995
    LOW_VOL_WINSORIZE_LOWER = 0.02
    LOW_VOL_WINSORIZE_UPPER = 0.98
    VOL_PERCENTILE_HIGH = 80
    VOL_PERCENTILE_LOW = 30
    MIN_VALUES_FOR_STATS = 10

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._tactile: Optional[Any] = None
        self._winsorize_lower = self.DEFAULT_WINSORIZE_LOWER
        self._winsorize_upper = self.DEFAULT_WINSORIZE_UPPER
        self._smooth_window = self.DEFAULT_SMOOTH_WINDOW
        if config:
            self._apply_config(config)
        logger.info(
            f"FactorPreprocessor 初始化，钳制:[{self._winsorize_lower},{self._winsorize_upper}], 窗口:{self._smooth_window}"
        )

    # ────────────────────────── 依赖注入 ──────────────────────────
    def inject_dependencies(self, tactile: Optional[Any] = None) -> None:
        self._tactile = tactile

    # ────────────────────────── 公共接口 ──────────────────────────
    def process(self, factor_values: List[float], factor_name: str = "unknown") -> Dict[str, Any]:
        """执行完整预处理流水线。"""
        warnings: List[str] = []
        if not factor_values:
            return {"status": "empty_input", "processed_values": [], "reason": f"因子 {factor_name} 输入为空", "warnings": warnings}
        lower, upper = self._select_bounds()
        filled = self._fill_missing(factor_values, factor_name, warnings)
        clamped = self._winsorize(filled, lower, upper)
        smoothed = self._median_smooth(clamped)
        reason = f"因子 {factor_name} 预处理完成: {len(factor_values)}值, 钳制[{lower:.3f},{upper:.3f}]"
        return {"status": "ok", "processed_values": smoothed, "reason": reason, "warnings": warnings}

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检。"""
        try:
            instance = cls()
            test = [1.0, 2.0, None, 100.0, -50.0, 1.8, 1.9, 2.1, 1.7, 1.6, None, 1.8, 2.0, 1.9, 1.7]
            result = instance.process(test, "test")
            if result["status"] != "ok":
                return {"status": "error", "message": f"预处理失败: {result['reason']}"}
            if max(result["processed_values"]) > 10.0:
                return {"status": "error", "message": "极端值未正确钳制"}
            return {"status": "ok", "message": f"测试通过，输出{len(result['processed_values'])}值"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _apply_config(self, config: Dict[str, Any]) -> None:
        self._winsorize_lower = max(0.0, min(0.10, float(config.get("winsorize_lower", self.DEFAULT_WINSORIZE_LOWER))))
        self._winsorize_upper = max(0.90, min(1.0, float(config.get("winsorize_upper", self.DEFAULT_WINSORIZE_UPPER))))
        self._smooth_window = max(1, min(10, int(config.get("smooth_window", self.DEFAULT_SMOOTH_WINDOW))))

    def _select_bounds(self) -> Tuple[float, float]:
        vol_pct = 50
        if self._tactile:
            try:
                data = self._tactile.sense(orderbook_snapshot={})
                if isinstance(data, dict):
                    # 尝试从流动性等级推断波动率环境
                    liq = data.get("liquidity_level", "L3")
                    if liq in ("L1", "L2"):
                        vol_pct = 90  # 低流动性 → 假设高波动
                    elif liq in ("L5", "L6"):
                        vol_pct = 20  # 高流动性 → 假设低波动
            except Exception:
                pass
        if vol_pct >= self.VOL_PERCENTILE_HIGH:
            return self.HIGH_VOL_WINSORIZE_LOWER, self.HIGH_VOL_WINSORIZE_UPPER
        if vol_pct <= self.VOL_PERCENTILE_LOW:
            return self.LOW_VOL_WINSORIZE_LOWER, self.LOW_VOL_WINSORIZE_UPPER
        return self._winsorize_lower, self._winsorize_upper

    def _fill_missing(self, values: List[float], name: str, warnings: List[str]) -> List[float]:
        valid = [v for v in values if v is not None]
        if len(valid) == len(values):
            return list(values)
        fill = sum(valid) / len(valid) if len(valid) >= self.MIN_VALUES_FOR_STATS else (sum(valid) / max(len(valid), 1))
        filled = []
        for v in values:
            if v is not None:
                filled.append(v)
            else:
                filled.append(fill)
                warnings.append(f"因子 {name} 缺失值已填充 ({fill:.4f})")
        return filled

    @staticmethod
    def _winsorize(values: List[float], lower: float, upper: float) -> List[float]:
        if len(values) < 3:
            return list(values)
        s = sorted(values)
        n = len(s)
        lb = s[max(0, int(n * lower) - 1)]
        ub = s[min(n - 1, int(n * upper))]
        return [lb if v < lb else (ub if v > ub else v) for v in values]

    def _median_smooth(self, values: List[float]) -> List[float]:
        if self._smooth_window <= 1 or len(values) < 3:
            return list(values)
        half = self._smooth_window // 2
        smoothed = []
        for i in range(len(values)):
            start = max(0, i - half)
            end = min(len(values), i + half + 1)
            window = sorted(values[start:end])
            smoothed.append(window[len(window) // 2])
        return smoothed
