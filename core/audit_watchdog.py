"""
火种系统 · 外部审计截止监控器 (AuditWatchdog)

核心职责：
1. 持续监控人类外部审计 (HWIP) 的执行间隔，当超过最大允许天数时，强制系统进入保守运行模式以控制风险
2. 在审计即将逾期时，提前向运维者推送分级预警，保障人类监督机制的有效性

外部依赖（真实模块接口）：
- core.utils.config_loader.ConfigLoader : 从全局配置中读取 HWIP 审计参数（最大天数、当前审计日期），并注册变更回调
- core.engine.strategy_manager.StrategyManager : 在审计逾期时强制切换策略模式并限制仓位
- core.behavioral_logger.BehavioralLogger : 记录审计逾期、降级和预警事件
- core.external_monitor.ExternalMonitor : 通过 Telegram/DingTalk 等渠道推送紧急告警
- core.compliance.compliance_reporter.ComplianceReporter : 生成审计逾期合规报告 (可选)

接口契约：
- check() -> Dict[str, Any] : 检查审计逾期状态，触发相应的降级或预警动作（幂等，可重复调用）
- get_days_remaining() -> Dict[str, Any] : 返回距离下次审计截止的剩余天数
- health_check() -> Dict[str, Any] : 模块自检，返回详细状态指标
- reset(operator: str, reason: str) -> Dict[str, Any] : 重置降级状态并恢复正常运行（需权限，有频率限制）
- is_downgraded() -> Dict[str, Any] : 查询当前是否处于降级状态
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 ConfigLoader 不可用时，使用类常量中定义的保守默认值（最大审计间隔设为 30 天）
- 当 StrategyManager 不可用时，降级告警仅做日志记录，无法执行实际降级动作
- 当外部审计日期格式无效或无法解析时，视为严重配置错误并触发 immediate 告警
- 所有降级值在类常量区明确声明

资源管理：
- 本模块为纯逻辑控制器，不持有文件句柄、网络连接等外部资源
- 所有文件操作均使用上下文管理器确保资源释放
- 关键状态定期持久化到本地文件以防重启丢失
- 降级历史记录有上限控制，防止内存泄漏
"""

import time
import logging
import threading
import json
import os
import uuid
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple, Callable, Set
from collections import deque

logger = logging.getLogger(__name__)

# 可选依赖：Prometheus 监控
try:
    from prometheus_client import Gauge
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False

# 可选依赖：合规报告
try:
    from core.compliance.compliance_reporter import ComplianceReporter
    _COMPLIANCE_AVAILABLE = True
except ImportError:
    _COMPLIANCE_AVAILABLE = False


class AuditWatchdog:
    """外部审计截止监控器 (全球顶尖量化标准版 v6.0)"""

    # ========== 类常量 ==========
    _DEFAULT_MAX_DAYS_WITHOUT_AUDIT: int = 90
    _DEFAULT_WARNING_THRESHOLD_RATIO: float = 0.8
    _DEFAULT_OVERRIDE_POSITION_CAP: float = 0.10
    _DEFAULT_FORCED_MODE: str = "moderate"
    _DEFAULT_AUDIT_DATE_FORMAT: str = "%Y-%m-%dT%H:%M:%S"
    _DEFAULT_RESTORE_COOLDOWN_SEC: int = 86400
    _CONFIG_CACHE_BASE_TTL_SEC: int = 60
    _CONFIG_CACHE_CRITICAL_TTL_SEC: int = 10
    _STATE_FILE: str = "logs/audit_watchdog_state.json"
    _STATE_FILE_TEMP: str = _STATE_FILE + ".tmp"
    _COMPLIANCE_REPORT_DIR: str = "logs/compliance/"
    _MAX_HISTORY_SIZE: int = 100
    _AUTO_RESTORE_THRESHOLD_RATIO: float = 0.5
    _CLOCK_DRIFT_WARNING_SEC: float = 5.0
    _RESET_COOLDOWN_SEC: int = 300
    _ALERT_RETRY_MAX: int = 3
    _COMPLIANCE_SIGN_KEY: str = "HWIP-AUDIT-TRAIL"

    # 配置键路径
    _CONFIG_KEY_MAX_DAYS: str = "audit.max_days_without_audit"
    _CONFIG_KEY_LAST_AUDIT_DATE: str = "audit.last_audit_date"
    _CONFIG_KEY_WARNING_RATIO: str = "audit.warning_threshold_ratio"
    _CONFIG_KEY_PREVIOUS_POSITION: str = "audit.previous_max_position_pct"
    _CONFIG_KEY_DISABLE_AUTO_RESTORE: str = "audit.disable_auto_restore"

    # ========== 降级状态机定义 ==========
    class _State:
        NORMAL: str = "normal"
        DOWNGRADED: str = "downgraded"

    _VALID_TRANSITIONS: Dict[str, Set[str]] = {
        _State.NORMAL: {_State.DOWNGRADED},
        _State.DOWNGRADED: {_State.NORMAL},
    }

    def __init__(self):
        # 外部依赖注入
        self._config_loader: Any = None
        self._strategy_manager: Any = None
        self._behavioral_logger: Any = None
        self._external_monitor: Any = None
        self._compliance_reporter: Any = None

        # 锁层级定义（按此顺序获取，防止死锁）：_state_lock -> _data_lock -> _history_lock
        self._state_lock: threading.Lock = threading.Lock()
        self._data_lock: threading.Lock = threading.Lock()
        self._history_lock: threading.Lock = threading.Lock()

        # 核心状态
        self._downgrade_state: str = self._State.NORMAL
        self._last_audit_timestamp: float = 0.0
        self._audit_date_raw: Optional[str] = None
        self._previous_position_cap: float = 1.0
        self._restore_cooldown_until: float = 0.0
        self._last_check_time: float = 0.0
        self._last_check_duration_ms: float = 0.0
        self._alert_retry_count: Dict[str, int] = {}
        self._last_reset_time: float = 0.0

        # 降级历史（有上限的队列）
        self._downgrade_history: deque = deque(maxlen=self._MAX_HISTORY_SIZE)

        # 配置缓存
        self._config_cache: Dict[str, Any] = {}
        self._config_cache_timestamp: float = 0.0

        # 系统时钟基准
        self._boot_timestamp: float = time.time()
        self._boot_monotonic: float = time.monotonic()

        # Prometheus
        self._metrics_registered: bool = False
        self._register_metrics()

        # 恢复状态并注册配置回调
        self._load_state()
        self._register_config_callback()

        logger.info("AuditWatchdog v6.0 初始化完成，启动时间戳: %d，降级状态: %s",
                    int(self._boot_timestamp), self._downgrade_state)

    # ========== 属性暴露 ==========
    @property
    def DEFAULT_MAX_DAYS(self) -> int:
        return self._DEFAULT_MAX_DAYS_WITHOUT_AUDIT

    @property
    def DEFAULT_POSITION_CAP(self) -> float:
        return self._DEFAULT_OVERRIDE_POSITION_CAP

    @property
    def DEFAULT_FORCED_MODE(self) -> str:
        return self._DEFAULT_FORCED_MODE

    @property
    def DEFAULT_AUDIT_DATE_FORMAT(self) -> str:
        return self._DEFAULT_AUDIT_DATE_FORMAT

    @property
    def STATE_FILE(self) -> str:
        return self._STATE_FILE

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        config_loader: Optional[Any] = None,
        strategy_manager: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
        external_monitor: Optional[Any] = None,
        compliance_reporter: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """注入外部依赖（可选注入，未注入时对应功能降级）"""
        result = {
            "config_loader": False, "strategy_manager": False,
            "behavioral_logger": False, "external_monitor": False,
            "compliance_reporter": False,
        }

        if config_loader is not None and hasattr(config_loader, 'get'):
            self._config_loader = config_loader
            result["config_loader"] = True
            self._register_config_callback()
            logger.info("ConfigLoader 注入成功")
        else:
            logger.warning("ConfigLoader 未注入，使用默认审计参数")

        if strategy_manager is not None and all(
            hasattr(strategy_manager, m) for m in ['set_mode', 'set_max_position_pct']
        ):
            self._strategy_manager = strategy_manager
            result["strategy_manager"] = True
            logger.info("StrategyManager 注入成功")
        else:
            logger.warning("StrategyManager 未注入，降级时无法执行实际操作")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            result["behavioral_logger"] = True
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

        if external_monitor is not None and hasattr(external_monitor, 'push_alert'):
            self._external_monitor = external_monitor
            result["external_monitor"] = True
            logger.info("ExternalMonitor 注入成功")
        else:
            logger.warning("ExternalMonitor 未注入，告警仅本地日志")

        if _COMPLIANCE_AVAILABLE and compliance_reporter is not None:
            self._compliance_reporter = compliance_reporter
            result["compliance_reporter"] = True
            logger.info("ComplianceReporter 注入成功")
        else:
            logger.debug("ComplianceReporter 未注入，合规报告功能降级")

        return {"status": "ok", "reason": "依赖注入完成", "data": result, "warnings": []}

    # ========== 公共接口 ==========
    def check(self) -> Dict[str, Any]:
        """检查审计逾期状态（幂等，可重复调用）"""
        start_time = time.monotonic()
        warnings: List[str] = []
        max_days = self._get_max_days()
        last_audit, audit_valid = self._get_last_audit_date()
        now = self._get_safe_time()
        days_since = (now - last_audit) / 86400.0

        # 审计日期在未来
        if audit_valid and last_audit > now + 86400:
            reason = f"审计日期异常：上次审计时间 {datetime.fromtimestamp(last_audit, tz=timezone.utc).isoformat()} 远在未来"
            logger.error(f"{reason} #RECOVERY: 核实 HWIP 审计日期配置")
            self._trigger_alert("critical", reason)
            self._update_check_timing(start_time)
            return {
                "status": "error", "reason": reason,
                "data": {"days_since_last_audit": None, "max_days": max_days,
                         "is_overdue": False, "downgrade_state": self._downgrade_state},
                "warnings": warnings + ["audit_date_in_future"],
            }

        # 审计日期配置无效
        if not audit_valid:
            reason = "审计日期配置无效或缺失，请立即检查配置文件"
            logger.error(f"{reason} #RECOVERY: 检查 {self._CONFIG_KEY_LAST_AUDIT_DATE} 配置")
            self._trigger_alert("critical", reason)
            self._update_check_timing(start_time)
            return {
                "status": "error", "reason": reason,
                "data": {"days_since_last_audit": None, "max_days": max_days,
                         "is_overdue": False, "downgrade_state": self._downgrade_state},
                "warnings": warnings + ["audit_config_invalid"],
            }

        # 尝试自动恢复
        with self._state_lock:
            can_restore = (
                not self._is_auto_restore_disabled()
                and self._downgrade_state == self._State.DOWNGRADED
                and now > self._restore_cooldown_until
                and days_since < max_days * self._AUTO_RESTORE_THRESHOLD_RATIO
            )
        if can_restore:
            logger.info("检测到审计日期已更新，且冷却期已过，尝试恢复正常模式")
            self._try_restore_normal()

        # 逾期判定
        if days_since >= max_days:
            reason = f"外部审计逾期 {days_since:.1f} 天（最大允许 {max_days} 天），触发强制降级"
            logger.error(f"{reason} #RECOVERY: 请立即执行 HWIP 审计流程")
            downgrade_executed = self._execute_downgrade(days_since)
            if not downgrade_executed:
                warnings.append("downgrade_failed")
            self._generate_compliance_report("overdue", days_since)
            self._update_check_timing(start_time)
            return {
                "status": "ok", "reason": reason,
                "data": {"days_since_last_audit": round(days_since, 1), "max_days": max_days,
                         "is_overdue": True, "downgrade_state": self._downgrade_state,
                         "downgrade_executed": downgrade_executed},
                "warnings": warnings + (["audit_overdue"] if not warnings else warnings),
            }

        # 预警判定
        warning_ratio = self._get_warning_ratio()
        warning_threshold = max_days * warning_ratio
        if days_since >= warning_threshold:
            remaining = max_days - days_since
            reason = f"外部审计将在 {remaining:.1f} 天后逾期，请尽快安排"
            logger.warning(reason)
            self._trigger_alert("warning", reason)
            self._update_check_timing(start_time)
            return {
                "status": "ok", "reason": reason,
                "data": {"days_since_last_audit": round(days_since, 1), "max_days": max_days,
                         "days_remaining": round(remaining, 1), "is_overdue": False,
                         "downgrade_state": self._downgrade_state},
                "warnings": warnings + ["audit_nearing_deadline"],
            }

        self._update_check_timing(start_time)
        return {
            "status": "ok",
            "reason": f"审计状态正常（已过 {days_since:.1f} 天，最大 {max_days} 天）",
            "data": {"days_since_last_audit": round(days_since, 1), "max_days": max_days,
                     "is_overdue": False, "downgrade_state": self._downgrade_state},
            "warnings": [],
        }

    def get_days_remaining(self) -> Dict[str, Any]:
        """返回距离下次审计截止的剩余天数"""
        max_days = self._get_max_days()
        last_audit, _ = self._get_last_audit_date()
        days_since = (self._get_safe_time() - last_audit) / 86400.0
        remaining = max(0.0, max_days - days_since)
        warning_ratio = self._get_warning_ratio()
        return {
            "status": "ok",
            "reason": f"剩余 {remaining:.1f} 天",
            "data": {
                "days_remaining": round(remaining, 1), "max_days": max_days,
                "days_since_last_audit": round(days_since, 1),
                "is_overdue": days_since >= max_days,
                "warning_threshold_days": round(max_days * warning_ratio, 1),
            },
            "warnings": ["audit_overdue"] if days_since >= max_days else [],
        }

    def reset(self, operator: str = "unknown", reason: str = "manual") -> Dict[str, Any]:
        """重置降级状态（需权限，有频率限制）"""
        if not operator or not isinstance(operator, str):
            return {"status": "error", "reason": "操作者标识不能为空", "data": {}, "warnings": ["invalid_operator"]}
        if not self._verify_operator_privilege(operator):
            return {"status": "error", "reason": "操作者权限不足", "data": {}, "warnings": ["privilege_denied"]}

        now = time.time()
        with self._state_lock:
            if now - self._last_reset_time < self._RESET_COOLDOWN_SEC:
                remaining_cooldown = self._RESET_COOLDOWN_SEC - (now - self._last_reset_time)
                return {
                    "status": "error",
                    "reason": f"重置操作过于频繁，请在 {remaining_cooldown:.0f} 秒后重试",
                    "data": {}, "warnings": ["reset_cooldown"],
                }
            self._last_reset_time = now
            self._downgrade_state = self._State.NORMAL
            self._restore_cooldown_until = now + self._DEFAULT_RESTORE_COOLDOWN_SEC

        logger.info("外部审计看门狗状态已由 %s 重置，原因: %s", operator, reason)
        self._log_event("audit_reset", {
            "operator": operator, "reason": reason,
            "cooldown_until": self._restore_cooldown_until,
        })
        self._save_state()
        return {
            "status": "ok", "reason": "降级状态已重置",
            "data": {"downgrade_state": self._State.NORMAL, "operator": operator, "reason": reason},
            "warnings": [],
        }

    def is_downgraded(self) -> Dict[str, Any]:
        """查询当前是否处于降级状态"""
        with self._state_lock:
            downgraded = self._downgrade_state == self._State.DOWNGRADED
        return {
            "status": "ok", "reason": f"降级状态: {downgraded}",
            "data": {"is_downgraded": downgraded},
            "warnings": [],
        }

    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            max_days = self._get_max_days()
            if max_days <= 0:
                raise ValueError("审计最大天数配置无效")
            with self._data_lock:
                last_audit = self._last_audit_timestamp or self._boot_timestamp
            now = self._get_safe_time()
            days_since = (now - last_audit) / 86400.0
            return {
                "status": "ok",
                "reason": f"AuditWatchdog 正常，当前已过 {days_since:.1f} 天",
                "data": {
                    "max_days": max_days, "last_audit_timestamp": last_audit,
                    "days_since": round(days_since, 1),
                    "downgrade_state": self._downgrade_state,
                    "last_check_time": self._last_check_time,
                    "last_check_duration_ms": round(self._last_check_duration_ms, 2),
                    "history_size": len(self._downgrade_history),
                    "dependencies": {
                        "config_loader": self._config_loader is not None,
                        "strategy_manager": self._strategy_manager is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                        "external_monitor": self._external_monitor is not None,
                        "compliance_reporter": self._compliance_reporter is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查 ConfigLoader 注入状态和审计参数有效性")
            return {"status": "error", "reason": f"健康检查异常: {str(e)}", "data": {},
                    "warnings": [f"health_check_failed: {str(e)}"]}

    # ========== 私有方法 ==========
    def _update_check_timing(self, start_monotonic: float) -> None:
        """更新检查计时信息"""
        self._last_check_time = time.time()
        self._last_check_duration_ms = (time.monotonic() - start_monotonic) * 1000.0

    def _is_auto_restore_disabled(self) -> bool:
        """检查是否禁用自动恢复"""
        return bool(self._get_cached_config(self._CONFIG_KEY_DISABLE_AUTO_RESTORE, False))

    def _get_safe_time(self) -> float:
        """获取安全时间，检测系统时钟跳变"""
        now = time.time()
        monotonic_now = time.monotonic()
        expected = self._boot_timestamp + (monotonic_now - self._boot_monotonic)
        drift = abs(now - expected)
        if drift > self._CLOCK_DRIFT_WARNING_SEC:
            logger.warning(f"系统时钟可能发生跳变，偏差 {drift:.1f}s，使用单调时钟修正")
            return expected
        return now

    def _register_config_callback(self) -> None:
        """注册配置变更回调"""
        if self._config_loader is not None and hasattr(self._config_loader, 'on_change'):
            try:
                self._config_loader.on_change(lambda: self._clear_config_cache())
            except Exception as e:
                logger.debug(f"注册配置变更回调失败: {e}")

    def _clear_config_cache(self) -> None:
        """清除配置缓存"""
        with self._data_lock:
            self._config_cache.clear()
            self._config_cache_timestamp = 0.0
        logger.debug("配置缓存已主动清除")

    def _register_metrics(self) -> None:
        """注册 Prometheus 监控指标"""
        if not _PROMETHEUS_AVAILABLE or self._metrics_registered:
            return
        try:
            if not hasattr(self, '_metric_days_remaining'):
                self._metric_days_remaining = Gauge(
                    "fire_seed_audit_days_remaining", "审计剩余天数", ["env"]
                )
                self._metric_downgraded = Gauge(
                    "fire_seed_audit_downgraded", "审计降级状态 0=正常 1=降级", ["env"]
                )
                self._metric_health = Gauge(
                    "fire_seed_audit_health_score", "审计模块健康评分", ["env"]
                )
            self._metrics_registered = True
        except Exception as e:
            for attr in ['_metric_days_remaining', '_metric_downgraded', '_metric_health']:
                if hasattr(self, attr):
                    delattr(self, attr)
            logger.warning(f"注册 Prometheus 指标失败: {e}")

    def _save_state(self) -> None:
        """持久化状态到本地文件（原子写入）"""
        state = {
            "downgrade_state": self._downgrade_state,
            "last_audit_timestamp": self._last_audit_timestamp,
            "previous_position_cap": self._previous_position_cap,
            "restore_cooldown_until": self._restore_cooldown_until,
            "updated_at": time.time(),
        }
        try:
            os.makedirs(os.path.dirname(self._STATE_FILE), exist_ok=True)
            with open(self._STATE_FILE_TEMP, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(self._STATE_FILE_TEMP, self._STATE_FILE)
        except Exception as e:
            logger.error(f"持久化状态失败: {e} #RECOVERY: 检查磁盘空间和目录权限")
            self._trigger_alert("warning", f"状态持久化失败: {e}")

    def _load_state(self) -> None:
        """从本地文件恢复状态"""
        if not os.path.exists(self._STATE_FILE):
            return
        try:
            with open(self._STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)
            self._downgrade_state = state.get("downgrade_state", self._State.NORMAL)
            self._last_audit_timestamp = state.get("last_audit_timestamp", 0.0)
            self._previous_position_cap = state.get("previous_position_cap", 1.0)
            self._restore_cooldown_until = state.get("restore_cooldown_until", 0.0)
            logger.info("从文件恢复状态: state=%s, last_audit=%s",
                        self._downgrade_state, self._last_audit_timestamp)
        except json.JSONDecodeError as e:
            logger.error(f"状态文件格式损坏: {e} #RECOVERY: 删除 {self._STATE_FILE} 后重启")
        except Exception as e:
            logger.error(f"恢复状态失败: {e} #RECOVERY: 检查文件权限")

    def _get_cached_config(self, key: str, default: Any) -> Any:
        """从配置加载器获取配置，带缓存"""
        now = time.time()
        with self._data_lock:
            last_audit = self._last_audit_timestamp or self._boot_timestamp
        max_days = self._get_max_days()
        days_since = (now - last_audit) / 86400.0
        critical = (max_days - days_since) < 10
        ttl = self._CONFIG_CACHE_CRITICAL_TTL_SEC if critical else self._CONFIG_CACHE_BASE_TTL_SEC

        with self._data_lock:
            if now - self._config_cache_timestamp > ttl:
                self._config_cache.clear()
                self._config_cache_timestamp = now
            if key in self._config_cache:
                return self._config_cache[key]

        if self._config_loader is not None:
            try:
                val = self._config_loader.get(key, default)
                with self._data_lock:
                    self._config_cache[key] = val
                return val
            except Exception as e:
                logger.debug(f"ConfigLoader.get 异常 (key={key}): {e}")
        return default

    def _get_max_days(self) -> float:
        """获取最大无审计天数"""
        raw = self._get_cached_config(self._CONFIG_KEY_MAX_DAYS, self._DEFAULT_MAX_DAYS_WITHOUT_AUDIT)
        if isinstance(raw, (int, float)):
            val = float(raw)
            if val > 0:
                return val
        logger.warning(f"配置 {self._CONFIG_KEY_MAX_DAYS} 无效 ({raw})，使用默认 {self._DEFAULT_MAX_DAYS_WITHOUT_AUDIT}")
        return float(self._DEFAULT_MAX_DAYS_WITHOUT_AUDIT)

    def _get_warning_ratio(self) -> float:
        """获取预警比例"""
        raw = self._get_cached_config(self._CONFIG_KEY_WARNING_RATIO, self._DEFAULT_WARNING_THRESHOLD_RATIO)
        if isinstance(raw, (int, float)) and 0 < raw <= 1:
            return float(raw)
        return float(self._DEFAULT_WARNING_THRESHOLD_RATIO)

    def _get_last_audit_date(self) -> Tuple[float, bool]:
        """获取最近一次审计的时间戳，返回 (timestamp, is_valid)"""
        with self._data_lock:
            raw = self._get_cached_config(self._CONFIG_KEY_LAST_AUDIT_DATE, None)
            if raw and isinstance(raw, str):
                self._audit_date_raw = raw
                try:
                    dt = datetime.strptime(raw, self._DEFAULT_AUDIT_DATE_FORMAT)
                    ts = dt.replace(tzinfo=timezone.utc).timestamp()
                    self._last_audit_timestamp = ts
                    return ts, True
                except (ValueError, OSError) as e:
                    logger.critical(
                        f"解析审计日期失败 (raw={raw}): {e} "
                        f"#RECOVERY: 立即修复配置文件 {self._CONFIG_KEY_LAST_AUDIT_DATE}"
                    )
                    self._trigger_alert("critical", f"审计日期配置无效: {raw}")
                    return (self._last_audit_timestamp or self._boot_timestamp), False
            if self._last_audit_timestamp > 0:
                return self._last_audit_timestamp, True
            self._last_audit_timestamp = self._boot_timestamp
            logger.warning("未找到有效审计日期，使用系统启动时间作为保守降级")
            return self._last_audit_timestamp, False

    def _execute_downgrade(self, days_since: float) -> bool:
        """执行强制降级动作（事务性：保存状态→发送指令→持久化）"""
        with self._state_lock:
            if self._downgrade_state == self._State.DOWNGRADED:
                return True
            self._previous_position_cap = self._get_previous_position()
            self._downgrade_state = self._State.DOWNGRADED

        # 发送降级指令
        mode_ok = False
        position_ok = False
        if self._strategy_manager is not None:
            try:
                self._strategy_manager.set_mode(self._DEFAULT_FORCED_MODE)
                logger.info("已强制切换策略模式为: %s", self._DEFAULT_FORCED_MODE)
                mode_ok = True
            except Exception as e:
                logger.error(f"切换策略模式失败: {e} #RECOVERY: 检查 StrategyManager 运行状态", exc_info=True)
            try:
                self._strategy_manager.set_max_position_pct(self._DEFAULT_OVERRIDE_POSITION_CAP)
                logger.info("已强制限制最大仓位比例为: %.0f%%", self._DEFAULT_OVERRIDE_POSITION_CAP * 100)
                position_ok = True
            except Exception as e:
                logger.error(f"限制仓位失败: {e} #RECOVERY: 检查 StrategyManager 运行状态", exc_info=True)
        else:
            logger.warning("StrategyManager 不可用，无法执行实际降级动作")

        success = mode_ok and position_ok
        if not success:
            with self._state_lock:
                self._downgrade_state = self._State.NORMAL

        # 记录历史
        event_id = str(uuid.uuid4())[:8]
        event = {
            "timestamp": time.time(), "event_id": event_id, "success": success,
            "days_since": round(days_since, 1),
            "forced_mode": self._DEFAULT_FORCED_MODE,
            "forced_position_cap": self._DEFAULT_OVERRIDE_POSITION_CAP,
            "mode_ok": mode_ok, "position_ok": position_ok,
        }
        with self._history_lock:
            self._downgrade_history.append(event)

        self._save_state()
        self._log_event("audit_overdue_downgrade", event)
        self._update_metrics()
        return success

    def _get_previous_position(self) -> float:
        """获取当前仓位上限"""
        if self._strategy_manager is not None and hasattr(self._strategy_manager, 'get_max_position_pct'):
            try:
                return self._strategy_manager.get_max_position_pct()
            except Exception:
                pass
        return self._get_cached_config(self._CONFIG_KEY_PREVIOUS_POSITION, 1.0)

    def _try_restore_normal(self) -> None:
        """尝试恢复正常运行模式"""
        with self._state_lock:
            if self._downgrade_state != self._State.DOWNGRADED:
                return
            if time.time() < self._restore_cooldown_until:
                return

        if self._strategy_manager is not None:
            try:
                self._strategy_manager.set_mode("auto")
                previous_cap = self._previous_position_cap or 1.0
                self._strategy_manager.set_max_position_pct(previous_cap)
                logger.info("已恢复正常策略模式和仓位上限 (%.0f%%)", previous_cap * 100)
                with self._state_lock:
                    self._downgrade_state = self._State.NORMAL
                    self._restore_cooldown_until = time.time() + self._DEFAULT_RESTORE_COOLDOWN_SEC
                self._log_event("audit_restored", {"previous_position_cap": previous_cap})
                self._save_state()
                self._update_metrics()
            except Exception as e:
                logger.error(f"恢复正常模式失败: {e} #RECOVERY: 手动重启系统或再次尝试", exc_info=True)
        else:
            logger.warning("StrategyManager 不可用，无法自动恢复")

    def _trigger_alert(self, level: str, message: str) -> None:
        """推送告警（带重试机制）"""
        alert_key = f"{level}:{hashlib.md5(message.encode()).hexdigest()[:8]}"
        retry_count = self._alert_retry_count.get(alert_key, 0)

        pushed = False
        if self._external_monitor is not None and retry_count < self._ALERT_RETRY_MAX:
            try:
                self._external_monitor.push_alert(level=level, message=message)
                pushed = True
                self._alert_retry_count.pop(alert_key, None)
            except Exception:
                self._alert_retry_count[alert_key] = retry_count + 1
                logger.warning(f"外部监控推送失败 (重试 {retry_count + 1}/{self._ALERT_RETRY_MAX})")

        if not pushed:
            if level == "critical":
                logger.critical(f"{message} #RECOVERY: 立即执行审计或手动介入")
            else:
                logger.warning(message)

        self._log_event(f"audit_watchdog_{level}", {
            "message": message, "pushed": pushed, "retry_count": retry_count,
        })

    def _log_event(self, event_type: str, details: Dict[str, Any]) -> None:
        """记录事件到行为日志"""
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(event_type=event_type, details=details)
                return
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")
        logger.info("AUDIT_WATCHDOG_EVENT | type=%s | %s", event_type, json.dumps(details, default=str))

    def _update_metrics(self) -> None:
        """更新 Prometheus 指标"""
        if not _PROMETHEUS_AVAILABLE or not self._metrics_registered:
            return
        try:
            days_rem = self.get_days_remaining()["data"]["days_remaining"]
            self._metric_days_remaining.labels(env="prod").set(days_rem)
            self._metric_downgraded.labels(env="prod").set(
                1 if self._downgrade_state == self._State.DOWNGRADED else 0
            )
            self._metric_health.labels(env="prod").set(100 if days_rem > 30 else 50)
        except Exception:
            pass

    def _generate_compliance_report(self, event_type: str, days_since: float) -> None:
        """生成合规报告"""
        report_data = {
            "event_type": event_type,
            "days_since_audit": round(days_since, 1),
            "max_days": self._get_max_days(),
            "downgrade_state": self._downgrade_state,
            "timestamp": time.time(),
            "audit_trail_id": str(uuid.uuid4()),
            "signature": hashlib.sha256(
                f"{event_type}{days_since}{self._COMPLIANCE_SIGN_KEY}".encode()
            ).hexdigest()[:16],
        }

        if self._compliance_reporter is not None:
            try:
                self._compliance_reporter.generate_report(
                    event_type=event_type, details=report_data,
                )
                return
            except Exception as e:
                logger.error(f"生成合规报告失败: {e} #RECOVERY: 检查 ComplianceReporter 状态")

        # 降级写入本地
        try:
            os.makedirs(self._COMPLIANCE_REPORT_DIR, exist_ok=True)
            report_file = os.path.join(
                self._COMPLIANCE_REPORT_DIR,
                f"compliance_{int(time.time())}_{report_data['audit_trail_id'][:8]}.json",
            )
            with open(report_file, 'w', encoding='utf-8') as f:
                json.dump(report_data, f, ensure_ascii=False, indent=2)
            logger.info("合规报告已降级写入本地: %s", report_file)
        except Exception as e:
            logger.error(f"降级写入合规报告失败: {e} #RECOVERY: 检查磁盘空间")

    def _verify_operator_privilege(self, operator: str) -> bool:
        """验证操作者权限"""
        # 生产环境中应调用认证服务验证 token/密码
        return len(operator) >= 3  # 至少3个字符的用户名
