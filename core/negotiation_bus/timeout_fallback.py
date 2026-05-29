"""
火种系统 · 超时回退处理器 (TimeoutFallback)

核心职责：
1. 为协商总线各模块提供超时后的安全回退值，确保任何模块响应超时时系统仍能以保守策略继续运行
2. 基于固定时间窗口监控各模块的超时频率，自动触发告警，并保护关键安全字段不可被配置覆盖

外部依赖（真实模块接口）：
- core.utils.config_loader.ConfigLoader : 加载模块超时与回退配置，支持热重载
- core.behavioral_logger.BehavioralLogger : 记录超时事件与统计信息

接口契约：
- get_fallback(module_name: str) -> Dict[str, Any] : 返回指定模块的超时安全回退值（深拷贝，调用方可安全修改）
- record_timeout(module_name: str, elapsed_us: float) -> Dict[str, Any] : 记录一次超时事件
- record_call(module_name: str) -> None : 记录一次对模块的正常调用（用于计算超时率）
- health_check() -> Dict[str, Any] : 模块自检，结果带缓存
- on_config_changed() -> None : 配置热重载回调
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 ConfigLoader 不可用时，使用内置硬编码安全默认值（最保守策略）
- 当 BehavioralLogger 不可用时，超时事件仅记录本地日志
- 所有回退值在类常量区声明，关键安全字段（如风控的 allowed）不可被配置覆盖

资源管理：
- 本模块持有各模块超时统计的固定时间窗口数据，定期清理过期时间戳
- 不持有外部资源句柄，两把锁分别保护回退值与统计数据，避免高频统计阻塞核心协商
"""

import time
import logging
import threading
import copy
from typing import Dict, Any, List, Optional
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


class TimeoutFallback:
    """协商总线超时回退处理器"""

    # ========== 类常量 ==========
    STATS_WINDOW_SEC = 600                 # 超时统计时间窗口，秒，[300, 900]
    HIGH_TIMEOUT_RATE_THRESHOLD = 0.2     # 超时率超过 20% 触发告警
    MIN_SAMPLES_FOR_ALERT = 20            # 最少总样本数才触发告警

    # 内存告警阈值（每个模块的单个时间戳队列）
    MAX_QUEUE_LENGTH_WARNING = 1_000_000  # 队列长度超过此值发出警告

    # 健康检查缓存有效期
    HEALTH_CHECK_CACHE_TTL_SEC = 10.0

    # 关键安全字段：这些字段的值在任何情况下都不可被配置文件覆盖
    IMMUTABLE_FIELDS = {
        "risk_monitor": {"allowed": False, "allow_close": True},
        "circuit_breaker": {"allowed": False},
    }

    # 内置硬编码回退值（配置不可用时的最后防线）
    HARD_FALLBACKS = {
        "risk_monitor": {
            "allowed": False,
            "allowed_size_pct": 0.0,
            "adjustment_reason": "风控模块超时，默认禁止开仓/加仓，允许平仓",
            "allow_close": True,
        },
        "execution_gateway": {
            "allowed": True,
            "preferred_method": "limit_order",
            "max_slippage_bps": 1.0,
            "adjustment_reason": "执行网关超时，默认使用保守限价单",
        },
        "profit_compression": {
            "stop_price": None,
            "adjustment_reason": "紧缩利润模块超时，使用上一帧有效止损",
        },
        "position_sizer": {
            "allowed": True,
            "allowed_size_pct": 0.0,          # 仅适用于 open/add，平仓不受此限制
            "allow_unlimited_for_close": True, # 平仓不限制仓位
            "adjustment_reason": "仓位计算超时，禁止新开仓，允许平仓",
        },
    }

    def __init__(self):
        # 从配置加载的超时参数缓存
        self._module_configs: Dict[str, Dict[str, Any]] = {}
        # 自定义回退值（从配置加载，可能被安全字段覆盖）
        self._custom_fallbacks: Dict[str, Dict[str, Any]] = {}

        # 超时统计：固定时间窗口，存储时间戳
        self._timeout_timestamps: Dict[str, deque] = defaultdict(deque)
        self._call_timestamps: Dict[str, deque] = defaultdict(deque)

        # 外部依赖
        self._config_loader = None
        self._behavioral_logger = None

        # 线程安全：分离低频回退值锁与高频统计锁
        self._lock_fallback = threading.Lock()  # 保护回退值、配置
        self._lock_stats = threading.Lock()     # 保护超时统计时间戳

        # 健康检查缓存
        self._health_cache: Optional[Dict[str, Any]] = None
        self._health_cache_time = 0.0

        logger.info("TimeoutFallback 初始化完成，已加载 %d 个硬编码回退值", len(self.HARD_FALLBACKS))

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        config_loader: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖，并立即加载配置与一致性校验
        """
        if config_loader is not None:
            self._config_loader = config_loader
            self._load_config_from_loader()
            logger.info("ConfigLoader 注入成功，已加载 %d 个模块配置", len(self._module_configs))
        else:
            logger.warning("ConfigLoader 未注入，将仅使用硬编码回退值")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，超时事件仅记录本地日志")

        # 校验回退值一致性
        self._validate_consistency()

    def on_config_changed(self) -> None:
        """配置热重载回调，由 ConfigLoader 在配置变更后调用"""
        loader = self._config_loader
        if loader is not None:
            self._load_config_from_loader(loader)
            self._validate_consistency()
            logger.info("配置热重载完成，已更新 %d 个模块配置", len(self._module_configs))

    # ========== 公共接口 ==========
    def get_fallback(self, module_name: str) -> Dict[str, Any]:
        """
        获取指定模块的超时安全回退值（返回深拷贝，调用方可安全修改）

        Args:
            module_name: 模块名称

        Returns:
            标准响应字典，data 中包含回退值字典的深拷贝
        """
        with self._lock_fallback:
            if module_name in self._custom_fallbacks:
                fallback = copy.deepcopy(self._custom_fallbacks[module_name])
            elif module_name in self.HARD_FALLBACKS:
                fallback = copy.deepcopy(self.HARD_FALLBACKS[module_name])
            else:
                logger.warning("未找到模块 %s 的回退值，使用通用默认值", module_name)
                fallback = {
                    "allowed": False,
                    "allowed_size_pct": 0.0,
                    "adjustment_reason": f"未配置 {module_name} 的回退值，默认拒绝",
                }

        return {
            "status": "ok",
            "reason": f"返回 {module_name} 的超时回退值（深拷贝）",
            "data": fallback,
            "warnings": [],
        }

    def record_timeout(self, module_name: str, elapsed_us: float) -> Dict[str, Any]:
        """
        记录一次模块响应超时事件

        Args:
            module_name: 模块名称
            elapsed_us: 实际耗时（微秒）

        Returns:
            标准响应字典
        """
        if elapsed_us < 0:
            logger.warning("无效耗时: %s elapsed_us=%s，使用 0 替代", module_name, elapsed_us)
            elapsed_us = 0.0

        now = time.time()
        with self._lock_stats:
            self._timeout_timestamps[module_name].append(now)
            self._prune_timestamps(self._timeout_timestamps[module_name])
            timeout_rate = self._get_timeout_rate(module_name)

        logger.debug("记录超时: %s, 耗时=%.1fμs, 超时率=%.2f%%", module_name, elapsed_us, timeout_rate * 100)

        warnings = []
        if timeout_rate > self.HIGH_TIMEOUT_RATE_THRESHOLD:
            msg = f"{module_name} 超时率过高 ({timeout_rate:.1%})，建议检查模块健康状态"
            warnings.append(msg)
            logger.error("%s #RECOVERY: 检查模块日志、增加超时阈值或重启该模块", msg)
            if self._behavioral_logger is not None:
                try:
                    self._behavioral_logger.log_event(
                        event_type="high_timeout_rate",
                        details={"module": module_name, "timeout_rate": timeout_rate},
                    )
                except Exception as e:
                    logger.warning("行为日志记录失败: %s", e)

        return {
            "status": "ok",
            "reason": f"已记录 {module_name} 的超时事件",
            "data": {
                "module": module_name,
                "elapsed_us": elapsed_us,
                "timeout_rate": round(timeout_rate, 4),
            },
            "warnings": warnings,
        }

    def record_call(self, module_name: str) -> None:
        """
        记录一次对模块的正常调用（用于计算超时率）。
        此方法使用独立锁，不会阻塞 get_fallback。
        """
        now = time.time()
        with self._lock_stats:
            dq = self._call_timestamps[module_name]
            dq.append(now)
            # 内存保护告警
            if len(dq) > self.MAX_QUEUE_LENGTH_WARNING and len(dq) % 100000 == 0:
                logger.warning(
                    "%s 调用队列长度=%d，检查 STATS_WINDOW_SEC 是否过大或剪枝是否正常",
                    module_name, len(dq)
                )
            self._prune_timestamps(dq)

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检（带结果缓存）"""
        now = time.time()
        if self._health_cache and (now - self._health_cache_time) < self.HEALTH_CHECK_CACHE_TTL_SEC:
            return self._health_cache

        try:
            with self._lock_stats:
                timeout_summary = {}
                for mod in list(self._timeout_timestamps.keys()):
                    rate = self._get_timeout_rate(mod)
                    timeout_summary[mod] = {
                        "timeout_count": len(self._timeout_timestamps[mod]),
                        "call_count": len(self._call_timestamps[mod]),
                        "timeout_rate": round(rate, 4),
                    }

            with self._lock_fallback:
                active_fallbacks = {}
                for mod in set(list(self.HARD_FALLBACKS.keys()) + list(self._custom_fallbacks.keys())):
                    if mod in self._custom_fallbacks:
                        active_fallbacks[mod] = {"source": "custom_config", "value": self._custom_fallbacks[mod]}
                    else:
                        active_fallbacks[mod] = {"source": "hard_coded", "value": self.HARD_FALLBACKS[mod]}

            result = {
                "status": "ok",
                "reason": f"TimeoutFallback 正常，监控 {len(timeout_summary)} 个活跃模块",
                "data": {
                    "active_modules": len(timeout_summary),
                    "timeout_summary": timeout_summary,
                    "active_fallbacks": active_fallbacks,
                    "hard_fallback_count": len(self.HARD_FALLBACKS),
                    "custom_fallback_count": len(self._custom_fallbacks),
                },
                "warnings": [],
            }
            self._health_cache = result
            self._health_cache_time = now
            return result
        except Exception as e:
            logger.error("健康检查失败: %s #RECOVERY: 检查锁状态及统计数据完整性", e)
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _load_config_from_loader(self, loader: Any = None) -> None:
        """从 ConfigLoader 加载超时与回退配置"""
        if loader is None:
            loader = self._config_loader
        if loader is None:
            return

        try:
            config = loader.get("module_timeout", {})
            modules = config.get("modules", {})
            with self._lock_fallback:
                for mod_name, mod_config in modules.items():
                    self._module_configs[mod_name] = mod_config
                    if "fallback" in mod_config:
                        fallback = self._validate_fallback(mod_config["fallback"])
                        # 强制覆盖不可变安全字段
                        if mod_name in self.IMMUTABLE_FIELDS:
                            fallback.update(self.IMMUTABLE_FIELDS[mod_name])
                            logger.debug("已对 %s 应用不可变安全字段", mod_name)
                        self._custom_fallbacks[mod_name] = fallback
        except Exception as e:
            logger.error("加载超时配置失败: %s #RECOVERY: 检查 config/system/module_timeout.yaml 语法", e)

    @staticmethod
    def _validate_fallback(fallback: Dict[str, Any]) -> Dict[str, Any]:
        """验证并补全回退字典的必要字段"""
        required = ["adjustment_reason"]
        for key in required:
            if key not in fallback:
                fallback[key] = "回退值缺少说明"
        if "allowed" not in fallback:
            fallback["allowed"] = False
        return fallback

    def _validate_consistency(self) -> None:
        """验证自定义回退值与硬编码回退值的关键安全字段一致性"""
        with self._lock_fallback:
            for mod_name in self._custom_fallbacks:
                if mod_name in self.HARD_FALLBACKS and mod_name in self.IMMUTABLE_FIELDS:
                    custom = self._custom_fallbacks[mod_name]
                    for field, expected in self.IMMUTABLE_FIELDS[mod_name].items():
                        actual = custom.get(field)
                        if actual != expected:
                            logger.error(
                                "安全字段冲突: %s.%s 期望=%s, 实际=%s, 已强制覆盖",
                                mod_name, field, expected, actual
                            )
            logger.info("回退值一致性校验完成")

    def _prune_timestamps(self, dq: deque) -> None:
        """清理时间戳队列中的过期数据（需在 _lock_stats 内调用）"""
        cutoff = time.time() - self.STATS_WINDOW_SEC
        while dq and dq[0] < cutoff:
            dq.popleft()

    def _get_timeout_rate(self, module_name: str) -> float:
        """
        计算窗口内超时率（需在 _lock_stats 内调用）。
        由于追加时已即时剪枝，队列长度即为窗口内有效计数，无需再次遍历。
        """
        timeout_count = len(self._timeout_timestamps[module_name])
        call_count = len(self._call_timestamps[module_name])
        if call_count < self.MIN_SAMPLES_FOR_ALERT:
            return 0.0
        return timeout_count / call_count if call_count > 0 else 0.0
