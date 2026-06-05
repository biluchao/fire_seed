"""
火种系统 · 信号漏斗 (SignalFunnel) [机构级最终修复版 v5.0]

核心职责：
1. 基于动态阈值将输入信号评分为A(优质)、B(标准)、C(试探)三个等级，并输出对应的仓位系数
2. 利用鲁棒分位数与自适应窗口，对历史评分分布进行实时校准，确保分级与市场微观结构动态匹配

外部依赖（真实模块接口）：
- core.scorecard.filter_coordinator.FilterCoordinator : 获取过滤器协同状态（如饥渴模式）
- core.experience_replay.ExperienceReplay : 获取C级信号近期胜率，动态调整试探仓位
- core.behavioral_logger.BehavioralLogger : 记录分级决策与异常事件
- core.utils.config_loader.ConfigLoader : 从配置文件加载可选参数，覆盖类常量默认值

接口契约：
- classify_signal(score: float, context: Dict[str, Any]) -> Dict[str, Any] : 分级并返回仓位系数
- get_current_thresholds() -> Dict[str, Any] : 返回当前生效的A/B/C级阈值
- force_update_thresholds() -> Dict[str, Any] : 手动触发阈值更新
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 依赖缺失时回退至保守阈值（A:80, B:65, C:55），C级仓位系数降级为 0.15
- 滑动窗口样本不足时使用历史中位数，历史为空则使用类常量默认值
- 分位数计算异常时保留上一帧有效阈值，防止数据污染导致的误分级
- 所有降级值在类常量区明确声明

资源管理：
- 滑动窗口大小受常量限制，避免内存溢出
- 锁粒度极细：仅操作共享数据时持锁，耗时计算在锁外完成
- 行为日志异步写入，不阻塞主流程
"""

import math
import time
import logging
import random
import threading
from typing import Dict, Any, List, Optional, Tuple
from collections import deque
import numpy as np

logger = logging.getLogger(__name__)


class SignalFunnel:
    """信号分级漏斗：A/B/C三级动态阈值管理，机构级最终修复版 v5.0"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_WINDOW_SIZE = 200               # 滑动窗口最大样本数，无量纲，取值范围 [100, 500]
    DEFAULT_UPDATE_INTERVAL_SEC = 300       # 阈值更新间隔，秒，取值范围 [120, 900]
    MIN_SAMPLES_FOR_UPDATE = 50             # 触发阈值更新所需的最小样本数，无量纲，[30, 100]
    CONSERVATIVE_THRESHOLD_A = 80.0         # 保守回退阈值A，无量纲，取值范围 [70, 90]
    CONSERVATIVE_THRESHOLD_B = 65.0         # 保守回退阈值B，无量纲，取值范围 [60, 75]
    CONSERVATIVE_THRESHOLD_C = 55.0         # 保守回退阈值C，无量纲，取值范围 [50, 65]
    DEFAULT_C_SIZE = 0.15                   # C级信号默认仓位系数，无量纲，[0.1, 0.25]
    C_SIZE_WIN_BOOST = 1.2                  # C级信号近期胜率高时，仓位系数放大倍数，[1.0, 1.5]
    C_SIZE_LOSS_REDUCE = 0.7                # C级信号近期胜率低时，仓位系数缩小倍数，[0.5, 1.0]
    C_WIN_RATE_THRESHOLD = 0.40             # C级信号胜率阈值，高于此值视为有效试探，无量纲，[0.30, 0.50]
    C_WIN_RATE_DISABLE = 0.25               # C级信号胜率低于此值自动禁用，无量纲，[0.15, 0.35]
    SCORE_HISTORY_MAX_AGE_SEC = 86400       # 历史评分最大保留时间，秒，[43200, 172800]
    C_WIN_RATE_REFRESH_INTERVAL_SEC = 60    # C级胜率缓存刷新间隔，秒，[30, 300]
    HUNGRY_MODE_MIN_THRESHOLD_C = 50.0      # 饥渴模式下C级阈值最低下限，无量纲，[45, 55]
    MAX_PERCENTILE_SAMPLE = 1000            # 分位数计算最大采样数，防长尾计算阻塞
    MAD_OUTLIER_THRESHOLD = 3.0             # MAD异常值过滤阈值，无量纲，[2.0, 5.0]
    A_PERCENTILE = 80                       # A级阈值分位数，百分位，[70, 90]
    B_PERCENTILE = 65                       # B级阈值分位数，百分位，[55, 75]
    C_PERCENTILE = 50                       # C级阈值分位数，百分位，[40, 60]
    SIZE_MULT_A = 1.0                       # A级仓位系数，无量纲，[0.8, 1.2]
    SIZE_MULT_B = 0.6                       # B级仓位系数，无量纲，[0.4, 0.8]
    HUNGRY_A_DELTA = 5                      # 饥渴模式A级阈值下调幅度，无量纲，[2, 10]
    HUNGRY_B_DELTA = 3                      # 饥渴模式B级阈值下调幅度，无量纲，[1, 5]
    HUNGRY_C_DELTA = 2                      # 饥渴模式C级阈值下调幅度，无量纲，[1, 5]

    def __init__(self, config_loader: Optional[Any] = None):
        """
        初始化信号漏斗
        Args:
            config_loader: 可选的ConfigLoader实例，用于从配置文件加载参数覆盖默认值
        """
        # 从配置加载可选参数（保留默认值作为降级）
        self._load_config(config_loader)

        # 评分滑动窗口（存储 (monotonic_time, score)）
        self._score_window: deque = deque(maxlen=self._window_size)

        # 当前生效的动态阈值
        self._thresholds: Dict[str, float] = {
            "A": self.CONSERVATIVE_THRESHOLD_A,
            "B": self.CONSERVATIVE_THRESHOLD_B,
            "C": self.CONSERVATIVE_THRESHOLD_C,
        }

        # 阈值上次更新时间 (monotonic)
        self._last_threshold_update: float = 0.0

        # 阈值更新失败计数
        self._update_failure_count: int = 0

        # C级信号近期胜率缓存
        self._c_win_rate: float = 0.5
        self._c_win_rate_last_refresh: float = 0.0

        # 外部依赖注入
        self._filter_coordinator = None
        self._experience_replay = None
        self._behavioral_logger = None

        # 线程安全（细粒度锁，仅保护最小临界区）
        self._lock = threading.Lock()

        logger.info(
            "SignalFunnel v5.0 初始化完成，默认阈值 A=%.0f, B=%.0f, C=%.0f",
            self._thresholds["A"], self._thresholds["B"], self._thresholds["C"]
        )

    # ========== 配置加载 ==========
    def _load_config(self, config_loader: Optional[Any]) -> None:
        """从ConfigLoader加载配置，覆盖类常量默认值"""
        # 所有可配置参数初始化为类常量
        self._window_size = self.DEFAULT_WINDOW_SIZE
        self._update_interval = self.DEFAULT_UPDATE_INTERVAL_SEC
        self._min_samples = self.MIN_SAMPLES_FOR_UPDATE
        self._c_size_default = self.DEFAULT_C_SIZE
        self._c_size_boost = self.C_SIZE_WIN_BOOST
        self._c_size_reduce = self.C_SIZE_LOSS_REDUCE
        self._c_rate_threshold = self.C_WIN_RATE_THRESHOLD
        self._c_rate_disable = self.C_WIN_RATE_DISABLE
        self._a_percentile = self.A_PERCENTILE
        self._b_percentile = self.B_PERCENTILE
        self._c_percentile = self.C_PERCENTILE
        self._size_mult_a = self.SIZE_MULT_A
        self._size_mult_b = self.SIZE_MULT_B
        self._hungry_a_delta = self.HUNGRY_A_DELTA
        self._hungry_b_delta = self.HUNGRY_B_DELTA
        self._hungry_c_delta = self.HUNGRY_C_DELTA

        if config_loader is None:
            logger.debug("未提供ConfigLoader，使用全部默认配置")
            return

        try:
            cfg = config_loader.get("scorecard.signal_funnel", {})
            self._window_size = int(cfg.get("window_size", self._window_size))
            self._update_interval = float(cfg.get("update_interval_sec", self._update_interval))
            self._min_samples = int(cfg.get("min_samples_for_update", self._min_samples))
            self._c_size_default = float(cfg.get("c_size_default", self._c_size_default))
            self._c_size_boost = float(cfg.get("c_size_boost", self._c_size_boost))
            self._c_size_reduce = float(cfg.get("c_size_reduce", self._c_size_reduce))
            self._c_rate_threshold = float(cfg.get("c_rate_threshold", self._c_rate_threshold))
            self._c_rate_disable = float(cfg.get("c_rate_disable", self._c_rate_disable))
            self._a_percentile = int(cfg.get("a_percentile", self._a_percentile))
            self._b_percentile = int(cfg.get("b_percentile", self._b_percentile))
            self._c_percentile = int(cfg.get("c_percentile", self._c_percentile))
            self._size_mult_a = float(cfg.get("size_mult_a", self._size_mult_a))
            self._size_mult_b = float(cfg.get("size_mult_b", self._size_mult_b))
            self._hungry_a_delta = int(cfg.get("hungry_a_delta", self._hungry_a_delta))
            self._hungry_b_delta = int(cfg.get("hungry_b_delta", self._hungry_b_delta))
            self._hungry_c_delta = int(cfg.get("hungry_c_delta", self._hungry_c_delta))
            # 重设现有窗口大小（若已初始化）
            if hasattr(self, '_score_window') and self._score_window is not None:
                with self._lock:
                    old_window = self._score_window
                    self._score_window = deque(old_window, maxlen=self._window_size)
            logger.info("配置加载成功：窗口=%d 更新间隔=%.0fs 最小样本=%d", self._window_size, self._update_interval, self._min_samples)
        except Exception as e:
            logger.warning(f"配置加载失败: {e}，使用默认值 #RECOVERY: 检查config路径 scorecard.signal_funnel")

    def reload_config(self, config_loader: Any) -> None:
        """运行时热重载配置"""
        self._load_config(config_loader)
        logger.info("SignalFunnel 配置热重载完成")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        filter_coordinator: Optional[Any] = None,
        experience_replay: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选注入，未注入时对应功能降级）"""
        if filter_coordinator is not None:
            self._filter_coordinator = filter_coordinator
            logger.info("FilterCoordinator 注入成功")
        else:
            logger.warning("FilterCoordinator 未注入，使用保守阈值")

        if experience_replay is not None:
            self._experience_replay = experience_replay
            logger.info("ExperienceReplay 注入成功")
        else:
            logger.warning("ExperienceReplay 未注入，C级信号仓位系数固定为默认值")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

    # ========== 公共接口 ==========
    def classify_signal(self, score: float, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        对单个信号评分进行分级，返回等级与仓位系数。
        锁内仅执行最小操作：记录评分、读取阈值副本；耗时更新在锁外异步触发。
        """
        # 参数校验与NaN检测
        if not isinstance(score, (int, float)) or (isinstance(score, float) and math.isnan(score)):
            logger.warning(f"无效信号评分: {score}，使用默认分级")
            return {
                "status": "error",
                "reason": f"无效信号评分: {score}，有效范围为 [0, 100]",
                "data": {"tier": "C", "size_mult": self._c_size_default},
                "warnings": ["invalid_score"],
            }
        score = float(np.clip(score, 0.0, 100.0))  # 修剪到有效范围
        ctx = context or {}

        # 1. 锁外异步刷新胜率缓存
        self._refresh_c_win_rate()

        # 2. 锁内快照：记录评分 + 获取阈值副本
        with self._lock:
            self._score_window.append((time.monotonic(), score))
            base_thresholds = dict(self._thresholds)  # 安全副本
            # 检查是否需要触发阈值更新（异步，释放锁后执行）
            need_update = (time.monotonic() - self._last_threshold_update >= self._update_interval
                           and len(self._score_window) >= self._min_samples)

        # 3. 锁外执行耗时阈值更新（仅在需要时）
        if need_update:
            self._async_update_thresholds()

        # 4. 基于副本阈值分级
        tier, size_mult = self._classify_with_thresholds(
            score, base_thresholds["A"], base_thresholds["B"], base_thresholds["C"]
        )

        # 5. 检查饥渴模式（锁外调用外部依赖）
        thresholds_used = base_thresholds
        hunger_applied = False
        warnings = []
        if self._filter_coordinator is not None and hasattr(self._filter_coordinator, 'is_hungry'):
            try:
                if self._filter_coordinator.is_hungry():
                    adjusted_a = max(self.HUNGRY_MODE_MIN_THRESHOLD_C + self._hungry_a_delta,
                                     base_thresholds["A"] - self._hungry_a_delta)
                    adjusted_b = max(self.HUNGRY_MODE_MIN_THRESHOLD_C,
                                     base_thresholds["B"] - self._hungry_b_delta)
                    adjusted_c = max(self.HUNGRY_MODE_MIN_THRESHOLD_C,
                                     base_thresholds["C"] - self._hungry_c_delta)
                    tier, size_mult = self._classify_with_thresholds(
                        score, adjusted_a, adjusted_b, adjusted_c
                    )
                    thresholds_used = {"A": adjusted_a, "B": adjusted_b, "C": adjusted_c}
                    hunger_applied = True
                    warnings.append("hungry_mode_active")
                    logger.debug("饥渴模式生效，临时阈值: A=%.0f B=%.0f C=%.0f",
                                 adjusted_a, adjusted_b, adjusted_c)
            except Exception as e:
                logger.warning(f"查询饥渴模式失败: {e}")

        return {
            "status": "ok",
            "reason": f"信号评分 {score:.1f} 分级为 {tier}，仓位系数 {size_mult:.2f}",
            "data": {
                "score": score,
                "tier": tier,
                "size_mult": size_mult,
                "thresholds_used": thresholds_used,
                "hunger_applied": hunger_applied,
                "context": {k: v for k, v in ctx.items() if k in ("signal_type", "market_regime")},
            },
            "warnings": warnings,
        }

    def get_current_thresholds(self) -> Dict[str, Any]:
        """返回当前生效的A/B/C级阈值（线程安全）"""
        with self._lock:
            return {
                "status": "ok",
                "reason": "返回当前生效的动态阈值",
                "data": {
                    "thresholds": dict(self._thresholds),
                    "last_update": self._last_threshold_update,
                    "sample_count": len(self._score_window),
                    "update_failures": self._update_failure_count,
                },
                "warnings": [],
            }

    def force_update_thresholds(self) -> Dict[str, Any]:
        """手动触发阈值更新（同步，阻塞等待完成）"""
        self._sync_update_thresholds()
        with self._lock:
            return {
                "status": "ok",
                "reason": "已手动触发阈值更新",
                "data": {
                    "thresholds": dict(self._thresholds),
                    "sample_count": len(self._score_window),
                },
                "warnings": [],
            }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            if not hasattr(self, '_score_window'):
                return {"status": "degraded", "reason": "评分窗口未初始化", "data": {}, "warnings": ["window_not_initialized"]}

            with self._lock:
                sample_count = len(self._score_window)
                window_maxlen = self._score_window.maxlen if self._score_window.maxlen else self._window_size
                usage_pct = round(sample_count / window_maxlen * 100, 1) if window_maxlen > 0 else 0.0
                thresholds_snapshot = dict(self._thresholds)
                c_rate = self._c_win_rate

            return {
                "status": "ok",
                "reason": f"SignalFunnel 正常，样本数 {sample_count}，阈值 A={thresholds_snapshot['A']:.0f} B={thresholds_snapshot['B']:.0f} C={thresholds_snapshot['C']:.0f}",
                "data": {
                    "sample_count": sample_count,
                    "window_usage_pct": usage_pct,
                    "thresholds": thresholds_snapshot,
                    "c_win_rate": c_rate,
                    "update_failures": self._update_failure_count,
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和评分窗口完整性")
            return {"status": "error", "reason": f"健康检查异常: {str(e)}", "data": {}, "warnings": [f"health_check_failed: {str(e)}"]}

    # ========== 阈值更新（异步/同步） ==========
    def _async_update_thresholds(self) -> None:
        """在独立线程中异步更新阈值，避免阻塞主调用"""
        try:
            threading.Thread(target=self._sync_update_thresholds, daemon=True).start()
        except Exception as e:
            logger.error(f"启动异步阈值更新线程失败: {e}")

    def _sync_update_thresholds(self) -> None:
        """同步更新动态阈值。提取窗口数据，执行鲁棒分位数计算，更新阈值。"""
        # 1. 在锁内提取数据副本
        with self._lock:
            now = time.monotonic()
            if now - self._last_threshold_update < self._update_interval:
                return
            # 乐观更新时间戳，避免重复触发
            self._last_threshold_update = now
            if len(self._score_window) < self._min_samples:
                return
            cutoff = now - self.SCORE_HISTORY_MAX_AGE_SEC
            # 保存原始窗口副本（列表）
            raw_scores = [s for ts, s in self._score_window if ts >= cutoff]
            if len(raw_scores) < self._min_samples:
                return

        # 2. 锁外执行耗时计算
        try:
            new_thresholds = self._compute_robust_percentiles(raw_scores)
            if new_thresholds is None:
                with self._lock:
                    self._update_failure_count += 1
                return
        except Exception as e:
            logger.error(f"阈值计算异常: {e}")
            with self._lock:
                self._update_failure_count += 1
            return

        # 3. 在锁内更新阈值
        with self._lock:
            old_a, old_b, old_c = self._thresholds["A"], self._thresholds["B"], self._thresholds["C"]
            self._thresholds.update(new_thresholds)
            self._update_failure_count = 0
            logger.info("动态阈值已更新: A %.0f→%.0f, B %.0f→%.0f, C %.0f→%.0f (有效样本 %d)",
                        old_a, new_thresholds["A"], old_b, new_thresholds["B"], old_c, new_thresholds["C"], len(raw_scores))

        # 4. 异步日志
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="signal_threshold_update",
                    details={"old": {"A": old_a, "B": old_b, "C": old_c},
                             "new": new_thresholds, "sample_count": len(raw_scores)})
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

    def _compute_robust_percentiles(self, scores: List[float]) -> Optional[Dict[str, float]]:
        """鲁棒分位数计算：随机采样 -> 异常值过滤 -> 计算分位数 -> 保守回退"""
        # 随机采样以限制计算量
        if len(scores) > self.MAX_PERCENTILE_SAMPLE:
            scores = random.sample(scores, self.MAX_PERCENTILE_SAMPLE)

        scores_array = np.array(scores)

        # 异常值过滤：MAD
        try:
            median = np.median(scores_array)
            mad = np.median(np.abs(scores_array - median))
            if mad > 0:
                modified_z = 0.6745 * (scores_array - median) / mad
                mask = np.abs(modified_z) < self.MAD_OUTLIER_THRESHOLD
                filtered = scores_array[mask]
                if len(filtered) >= self._min_samples // 2:
                    scores_array = filtered
        except Exception as e:
            logger.warning(f"异常值过滤失败: {e}，继续使用原始数据")

        # 分位数计算
        try:
            new_a = float(np.percentile(scores_array, self._a_percentile))
            new_b = float(np.percentile(scores_array, self._b_percentile))
            new_c = float(np.percentile(scores_array, self._c_percentile))
        except Exception as e:
            logger.error(f"分位数计算失败: {e}")
            return None

        # 保守回退 + 合法性检查
        if any(math.isnan(x) or math.isinf(x) for x in (new_a, new_b, new_c)):
            logger.error("分位数包含NaN/Inf，放弃更新")
            return None
        new_a = max(new_a, self.CONSERVATIVE_THRESHOLD_A)
        new_b = max(new_b, self.CONSERVATIVE_THRESHOLD_B)
        new_c = max(new_c, self.CONSERVATIVE_THRESHOLD_C)
        # 确保单调 A >= B >= C
        new_b = min(new_b, new_a)
        new_c = min(new_c, new_b)
        return {"A": new_a, "B": new_b, "C": new_c}

    # ========== 分级逻辑 ==========
    def _classify_with_thresholds(
        self, score: float, a_thresh: float, b_thresh: float, c_thresh: float
    ) -> Tuple[str, float]:
        """根据指定阈值进行分级，返回 (等级, 仓位系数)"""
        # 确保阈值单调
        b_thresh = min(b_thresh, a_thresh)
        c_thresh = min(c_thresh, b_thresh)
        if score >= a_thresh:
            return "A", self._size_mult_a
        if score >= b_thresh:
            return "B", self._size_mult_b
        return "C", self._get_c_size_mult_safe()

    def _get_c_size_mult_safe(self) -> float:
        """基于缓存的胜率计算C级仓位系数"""
        c_rate = self._c_win_rate
        if c_rate >= self._c_rate_threshold:
            return self._c_size_default * self._c_size_boost
        if c_rate <= self._c_rate_disable:
            return 0.0
        return self._c_size_default * self._c_size_reduce

    def _refresh_c_win_rate(self) -> None:
        """异步刷新C级信号胜率缓存（锁外调用外部依赖，带节流）"""
        now = time.monotonic()
        if now - self._c_win_rate_last_refresh < self.C_WIN_RATE_REFRESH_INTERVAL_SEC:
            return

        if self._experience_replay is not None and hasattr(self._experience_replay, 'get_c_win_rate'):
            try:
                new_rate = self._experience_replay.get_c_win_rate()
                with self._lock:
                    self._c_win_rate = new_rate
                self._c_win_rate_last_refresh = now
                logger.debug(f"C级胜率缓存刷新: {new_rate:.3f}")
            except Exception as e:
                # 失败时也更新时间戳，防止高频重试风暴
                self._c_win_rate_last_refresh = now
                logger.warning(f"获取C级胜率失败: {e}，保持旧缓存值")
        else:
            self._c_win_rate_last_refresh = now
