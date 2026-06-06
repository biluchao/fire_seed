"""
火种系统 · 多频段锁相环 (MultiBandPLL)

核心职责：
1. 基于同一价格序列，并行运行多个不同时间常数的锁相环（短/中/长周期），提取各自的瞬时频率与相位
2. 对各频段的锁定状态与频率方向进行综合投票，输出统一的趋势强度与方向信号，并附带各频段的独立状态

外部依赖（真实模块接口）：
- core.perception.pll_core.PLLCore : 单频段锁相环核心，提供 update(price) -> Dict 和 lock_status 查询
- core.behavioral_logger.BehavioralLogger : 记录异常事件与调试信息

接口契约：
- update(price: float, timestamp: Optional[float] = None) -> Dict[str, Any] : 输入最新价格，返回综合信号及频段状态
- get_lock_status() -> Dict[str, Any] : 返回所有频段的锁定状态和综合判定
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 若 PLLCore 不可用（注入失败），本模块将完全降级：update 返回默认空信号，并标记 "degraded" 状态
- 若某个频段的 PLLCore 实例更新过程中发生异常，则跳过该频段，其他频段继续工作，同时在 warning 中记录
- 若某个频段初始化失败，该频段被标记为禁用，其他频段正常工作（部分降级）
- 当任一频段样本不足（更新次数 < 最小预热周期）时，该频段信号置为无效，不参与综合投票
- 所有降级值在类常量区明确声明

资源管理：
- 本模块持有多个 PLLCore 实例，其生命周期与本模块一致，无额外资源需手动释放
- 不创建线程或文件句柄
"""

import time
import logging
import threading
import math
import copy
from typing import Dict, Any, List, Optional, Tuple, Type
import numpy as np

logger = logging.getLogger(__name__)


class MultiBandPLL:
    """多频段锁相环：通过短/中/长周期并行跟踪，提升趋势确认的可靠性"""

    # ========== 类常量（默认配置） ==========
    DEFAULT_BANDS = {
        "short":  {"tau": 8,   "label": "短周期"},
        "medium": {"tau": 21,  "label": "中周期"},
        "long":   {"tau": 55,  "label": "长周期"},
    }
    MIN_LOCKED_BANDS_FOR_TREND = 2          # 综合判定所需的最小锁定频段数
    TREND_FREQ_THRESHOLD = 0.005            # 弧度/样本，绝对值低于此视为无趋势
    MIN_UPDATES_FOR_WARMUP = 5              # 频段预热所需的最小更新次数
    MAX_TAU = 500                           # tau 上限，防止异常配置
    DEGRADED_SIGNAL_TEMPLATE = {            # 降级信号模板（每次使用时深拷贝）
        "direction": 0,
        "strength": 0.0,
        "locked_bands": [],
        "band_details": {},
    }

    def __init__(self):
        self._bands: Dict[str, Any] = {}
        self._band_configs = copy.deepcopy(self.DEFAULT_BANDS)
        self._disabled_bands: set = set()
        self._band_update_counts: Dict[str, int] = {}  # 各频段更新次数（用于预热）

        self._behavioral_logger = None
        self._pll_core_class: Optional[Type] = None

        self._update_count = 0
        self._degraded = False
        self._last_direction = 0  # 上一刻非零方向，用于检测翻转（零方向不更新）
        self._lock = threading.Lock()

        logger.info("MultiBandPLL 初始化完成，频段: %s", list(self._band_configs.keys()))

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        pll_core_class: Optional[Type] = None,
        behavioral_logger: Optional[Any] = None,
        custom_band_configs: Optional[Dict[str, Dict]] = None,
    ) -> None:
        """注入外部依赖。若 PLLCore 类或配置变更，将重建所有频段并保留配置。"""
        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")

        if custom_band_configs is not None:
            # 配置完整性校验
            for band_name, cfg in custom_band_configs.items():
                if "tau" not in cfg:
                    logger.error(f"自定义频段 {band_name} 缺少必需字段 'tau'")
                    return
                if not isinstance(cfg["tau"], (int, float)) or cfg["tau"] <= 0 or cfg["tau"] > self.MAX_TAU:
                    logger.error(f"频段 {band_name} 的 tau 值非法: {cfg['tau']}")
                    return
            if len(custom_band_configs) == 0:
                logger.error("自定义频段配置为空")
                return
            self._band_configs = copy.deepcopy(custom_band_configs)
            logger.info("自定义频段配置已应用: %s", list(self._band_configs.keys()))

        if pll_core_class is not None:
            if not (isinstance(pll_core_class, type) and callable(pll_core_class)):
                logger.error("注入的 pll_core_class 必须是可调用的类")
                self._degraded = True
                return
            # 校验类接口（创建临时实例，使用后立即清理）
            test_instance = None
            try:
                test_instance = pll_core_class(tau=10)
                if not hasattr(test_instance, 'update') or not callable(getattr(test_instance, 'update', None)):
                    logger.error("PLLCore 类缺少 update 方法")
                    self._degraded = True
                    return
            except Exception as e:
                logger.error(f"PLLCore 接口校验失败: {e}")
                self._degraded = True
                return
            finally:
                if test_instance is not None and hasattr(test_instance, 'close'):
                    try:
                        test_instance.close()
                    except Exception:
                        pass
                del test_instance

            self._pll_core_class = pll_core_class
            with self._lock:
                self._initialize_bands()
            logger.info("PLLCore 类注入并初始化完成")

    def _initialize_bands(self) -> None:
        """使用注入的 PLLCore 类及当前配置创建各频段实例，支持部分降级。需在锁内调用。"""
        if self._pll_core_class is None:
            self._degraded = True
            return
        # 清理旧实例（显式释放资源）
        for band_name, pll_instance in list(self._bands.items()):
            if hasattr(pll_instance, 'close'):
                try:
                    pll_instance.close()
                except Exception:
                    pass
        self._bands.clear()
        self._disabled_bands.clear()
        self._band_update_counts.clear()

        errors = []
        # 先验证所有配置，再批量创建（避免部分创建残留）
        for band_name, config in self._band_configs.items():
            if "tau" not in config or config["tau"] <= 0 or config["tau"] > self.MAX_TAU:
                errors.append(band_name)
                self._disabled_bands.add(band_name)
                logger.error(f"频段 {band_name} tau 非法: {config.get('tau')}")
        for band_name in errors:
            del self._band_configs[band_name]  # 从配置中移除非法频段

        for band_name, config in self._band_configs.items():
            try:
                self._bands[band_name] = self._pll_core_class(tau=config["tau"])
                self._band_update_counts[band_name] = 0
            except Exception as e:
                logger.error(f"频段 {band_name} 初始化失败: {e} #RECOVERY: 检查 tau 值")
                self._disabled_bands.add(band_name)

        if len(self._bands) == 0:
            self._degraded = True
            logger.error("所有频段初始化失败，模块降级")
        else:
            self._degraded = False
            logger.info("频段实例初始化完成: %s, 禁用: %s", list(self._bands.keys()), list(self._disabled_bands))

    # ========== 公共接口 ==========
    def update(self, price: float, timestamp: Optional[float] = None) -> Dict[str, Any]:
        if timestamp is None:
            timestamp = time.time()
        # 严格参数校验（价格允许极小的正数，拒绝零或负数）
        if not isinstance(price, (int, float)) or not (math.isfinite(price) and price > 1e-12):
            return {
                "status": "error",
                "reason": f"非法价格: {price}",
                "data": copy.deepcopy(self.DEGRADED_SIGNAL_TEMPLATE),
                "warnings": ["invalid_price"],
            }
        if self._degraded or not self._bands:
            return {
                "status": "degraded",
                "reason": "PLLCore 不可用或未初始化",
                "data": copy.deepcopy(self.DEGRADED_SIGNAL_TEMPLATE),
                "warnings": ["module_degraded"],
            }
        with self._lock:
            self._update_count += 1
            band_results: Dict[str, Any] = {}
            warnings = []
            for band_name, pll in self._bands.items():
                try:
                    res = pll.update(price)
                    if not isinstance(res, dict) or "frequency" not in res:
                        raise ValueError(f"PLLCore.update 返回无效格式: {res}")
                    freq = res.get("frequency")
                    if not isinstance(freq, (int, float)) or not math.isfinite(freq):
                        freq = 0.0
                    self._band_update_counts[band_name] += 1
                    band_results[band_name] = {
                        "frequency": freq,
                        "phase": res.get("phase", 0.0),
                        "locked": bool(res.get("locked", False)),
                        "label": self._band_configs[band_name]["label"],
                        "update_count": self._band_update_counts[band_name],
                    }
                except Exception as e:
                    logger.error(f"频段 {band_name} 更新异常: {e}")
                    warnings.append(f"{band_name}_update_failed")
                    band_results[band_name] = {
                        "frequency": 0.0,
                        "phase": 0.0,
                        "locked": False,
                        "label": self._band_configs[band_name]["label"],
                        "error": True,
                    }
            for disabled in self._disabled_bands:
                band_results[disabled] = {
                    "frequency": 0.0, "phase": 0.0, "locked": False,
                    "label": self._band_configs.get(disabled, {}).get("label", disabled),
                    "error": True, "disabled": True,
                }
            direction, strength, locked_bands = self._evaluate_bands(band_results)
            # 方向翻转检测（仅在非零方向之间翻转时记录）
            if direction != 0 and direction != self._last_direction and self._last_direction != 0:
                logger.info("多频段方向翻转: %d -> %d (强度: %.3f)", self._last_direction, direction, strength)
            if direction != 0:
                self._last_direction = direction
            # 注意：方向为 0 时保留 _last_direction 不变，避免噪音

            data = {
                "direction": direction,
                "strength": strength,
                "locked_bands": locked_bands,
                "band_details": band_results,
                "update_count": self._update_count,
            }
            reason = f"综合方向: {direction}, 强度: {strength:.3f}, 锁定: {locked_bands}"
            if direction != 0:
                logger.debug("多频段信号: 方向=%d, 强度=%.3f, 锁定=%s", direction, strength, locked_bands)
            return {"status": "ok", "reason": reason, "data": data, "warnings": warnings}

    def get_lock_status(self) -> Dict[str, Any]:
        if self._degraded or not self._bands:
            return {"status": "degraded", "reason": "模块降级", "data": {}, "warnings": ["module_degraded"]}
        with self._lock:
            band_status = {}
            for name, pll in self._bands.items():
                try:
                    band_status[name] = {
                        "locked": getattr(pll, 'locked', False),
                        "frequency": getattr(pll, 'frequency', 0.0),
                        "update_count": self._band_update_counts.get(name, 0),
                    }
                except Exception:
                    band_status[name] = {"locked": False, "frequency": 0.0, "update_count": 0}
            for disabled in self._disabled_bands:
                band_status[disabled] = {"locked": False, "frequency": 0.0, "update_count": 0, "disabled": True}
            # 传递完整的 band_results 以支持预热过滤
            direction, _, locked_bands = self._evaluate_bands(band_status)
            return {
                "status": "ok",
                "reason": f"锁定频段: {locked_bands}, 综合方向: {direction}",
                "data": {"band_status": band_status, "direction": direction, "locked_bands": locked_bands},
                "warnings": [],
            }

    def health_check(self) -> Dict[str, Any]:
        """模块自检，不修改任何内部状态。"""
        try:
            if self._degraded:
                return {"status": "degraded", "reason": "PLLCore 未注入，模块降级", "data": {}, "warnings": ["pll_core_missing"]}
            with self._lock:
                total = len(self._band_configs)
                active = len(self._bands)
                disabled = sorted(list(self._disabled_bands))
                if active == 0:
                    return {"status": "degraded", "reason": "所有频段均不可用", "data": {"disabled_bands": disabled}, "warnings": ["all_bands_dead"]}
                # 静态接口校验：不实际调用 update
                try:
                    sample_band = list(self._bands.values())[0]
                    if not hasattr(sample_band, 'update') or not callable(getattr(sample_band, 'update', None)):
                        return {"status": "degraded", "reason": "PLLCore 实例缺少 update 方法", "data": {}, "warnings": ["pll_core_method_missing"]}
                except Exception as e:
                    logger.error(f"PLLCore 实例检查失败: {e}")
                    return {"status": "degraded", "reason": str(e), "data": {}, "warnings": ["instance_check_failed"]}
                return {
                    "status": "ok",
                    "reason": f"MultiBandPLL 正常，{active}/{total} 频段活跃，禁用: {disabled}",
                    "data": {"active_bands": active, "total_configured": total, "disabled_bands": disabled, "update_count": self._update_count},
                    "warnings": [],
                }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查内部状态")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_exception"]}

    # ========== 私有方法 ==========
    def _evaluate_bands(self, band_results: Dict[str, Dict]) -> Tuple[int, float, List[str]]:
        """综合各频段结果，考虑预热与禁用频段。"""
        locked = []
        frequencies = []
        total_configured = len(self._band_configs)
        if total_configured == 0:
            return 0, 0.0, []
        for name, res in band_results.items():
            # 跳过禁用或异常的频段
            if res.get("disabled") or res.get("error"):
                continue
            # 预热检查
            update_cnt = res.get("update_count", 0)
            if update_cnt < self.MIN_UPDATES_FOR_WARMUP:
                continue  # 未预热完成，即使 locked 也忽略
            if res.get("locked", False):
                freq = res.get("frequency", 0.0)
                if isinstance(freq, (int, float)) and math.isfinite(freq) and abs(freq) > 1e-12:
                    locked.append(name)
                    frequencies.append(freq)

        if len(locked) < self.MIN_LOCKED_BANDS_FOR_TREND:
            return 0, 0.0, locked

        median_freq = float(np.median(frequencies))
        if abs(median_freq) < self.TREND_FREQ_THRESHOLD:
            return 0, 0.0, locked

        direction = 1 if median_freq > 0 else -1
        lock_ratio = len(locked) / total_configured  # 使用总配置数，避免分母偏差
        same_sign = sum(1 for f in frequencies if (f > 0 and direction == 1) or (f < 0 and direction == -1))
        consistency = same_sign / len(frequencies) if frequencies else 0
        strength = min(1.0, lock_ratio * 0.6 + consistency * 0.4)
        return direction, strength, locked
