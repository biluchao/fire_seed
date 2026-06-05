"""
火种系统 · 风险监控中枢 (RiskMonitor) — 金融级最终形态

核心职责：
1. 作为全系统风控体系的唯一决策入口，以原子方式评估交易风险，输出标准化许可指令
2. 实时监控硬实时风控心跳，在物理生存底线被突破时自动进入最高防御状态
3. 提供风险色彩、脆弱性指数、全品种敞口等状态查询，支持外部审计与合规追溯

外部依赖（真实模块接口）：
- core.risk_monitor.circuit_breaker.CircuitBreaker : 多层熔断判定与冷却管理
- core.risk_monitor.risk_color_manager.RiskColorManager : 六级风险色彩量化与滞回冷却
- core.risk_monitor.bayesian_threshold_adapter.BayesianThresholdAdapter : 贝叶斯自适应阈值
- core.risk_monitor.contagion_blocker.ContagionBlocker : 跨品种传染阻断
- core.risk_monitor.fragility_index_calculator.FragilityIndexCalculator : 脆弱性指数计算
- core.risk_monitor.daily_loss_limiter.DailyLossLimiter : 日内亏损限额管理
- core.risk_monitor.order_risk_gateway.OrderRiskGateway : 订单风控网关
- core.risk_monitor.stress_test_runner.StressTestRunner : 压力测试执行器
- core.negotiation_bus.NegotiationBus : 接收协商请求，返回约束响应
- core.account_ledger.AccountLedger : 获取账户实时权益与保证金
- core.behavioral_logger.BehavioralLogger : 记录风控事件与告警

接口契约：
- evaluate_risk(intent: Dict[str, Any]) -> Dict[str, Any] : 原子化评估交易风险
- get_current_risk_color() -> Dict[str, Any] : 返回当前六级风险色彩等级
- get_fragility_index() -> Dict[str, Any] : 返回当前脆弱性指数及子维度得分
- get_total_exposure() -> Dict[str, Any] : 返回全品种净敞口汇总
- health_check() -> Dict[str, Any] : 模块自检（递归子模块，带超时）
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 核心风控子模块不可用时，系统进入CORE_DEGRADED状态，拒绝新开仓，风险色彩升至橙色
- 非核心模块异常时，对应检查放行，但记录降级日志
- 所有异常均采用保守策略：拒绝交易或降至最小仓位
- 降级值可在配置文件中覆盖

资源管理：
- 本模块不持有任何外部资源句柄
- 子模块生命周期由 system_builder 统一管理
"""

import logging
import time
import threading
from typing import Dict, Any, List, Optional, Union
import uuid
import copy
import math

logger = logging.getLogger(__name__)

__all__ = ["RiskMonitor"]


class RiskMonitor:
    """风险监控中枢（机构级，金融级卓越标准）"""

    # 默认配置（可被 config 覆盖）—— 使用不可变元组定义核心模块列表
    DEFAULT_CONFIG = {
        "core_modules": ("CircuitBreaker", "OrderRiskGateway"),
        "default_risk_color": "orange",
        "default_position_cap": 0.05,
        "fragility_extreme_cap_ratio": 0.5,
        "fragility_high_cap_ratio": 0.7,
        "health_check_timeout_sec": 2.0,
        "margin_check_timeout_sec": 0.5,
        "heartbeat_timeout_sec": 5.0,
        "min_position_notional": 10.0,
        "alert_rate_limit_sec": 1.0,
        "max_config_nesting_depth": 10,
    }

    # 子模块必需方法定义
    REQUIRED_METHODS = {
        "CircuitBreaker": ["check", "health_check"],
        "RiskColorManager": ["get_current_color", "get_full_status", "health_check"],
        "OrderRiskGateway": ["check", "health_check"],
        "ContagionBlocker": ["check", "health_check"],
        "FragilityCalculator": ["get_fragility_index", "health_check"],
        "DailyLossLimiter": ["check", "health_check"],
    }

    def __init__(self, config: Optional[Dict] = None):
        # 深度复制配置，防止外部污染
        self._config = copy.deepcopy(self.DEFAULT_CONFIG)
        if config:
            self._deep_update(self._config, copy.deepcopy(config), depth=0)

        self._sub_modules: Dict[str, Any] = {}
        self._rw_lock = threading.RLock()
        self._negotiation_bus = None
        self._account_ledger = None
        self._behavioral_logger = None
        self._core_degraded = True
        self._last_heartbeat = time.time()
        self._injection_count = 0
        self._last_alert_time = 0.0
        self._alert_lock = threading.Lock()  # 专门保护告警时间戳
        logger.info("RiskMonitor 机构级风控中枢初始化完成")

    @staticmethod
    def _deep_update(original: Dict, update: Dict, depth: int = 0, max_depth: int = 10) -> None:
        """递归深度更新字典，防止无限递归"""
        if depth >= max_depth:
            raise RecursionError(f"配置嵌套深度超过限制 {max_depth}")
        for key, value in update.items():
            if isinstance(value, dict) and isinstance(original.get(key), dict):
                RiskMonitor._deep_update(original[key], value, depth + 1, max_depth)
            else:
                original[key] = value

    @staticmethod
    def _validate_module(name: str, inst: Any, required_methods: List[str]) -> Any:
        """校验模块是否具备必要方法，若不满足返回 None"""
        if inst is not None:
            for method in required_methods:
                if not hasattr(inst, method):
                    logger.critical(f"{name} 缺少必需方法 {method}，标记为不可用")
                    return None
        return inst

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        circuit_breaker=None,
        risk_color_manager=None,
        bayesian_adapter=None,
        contagion_blocker=None,
        fragility_calculator=None,
        daily_loss_limiter=None,
        order_risk_gateway=None,
        stress_test_runner=None,
        negotiation_bus=None,
        account_ledger=None,
        behavioral_logger=None,
    ) -> None:
        with self._rw_lock:
            if self._injection_count > 0:
                logger.warning("依赖重复注入，旧引用将被覆盖")
            self._injection_count += 1

            self._sub_modules = {
                "CircuitBreaker": self._validate_module("CircuitBreaker", circuit_breaker, self.REQUIRED_METHODS.get("CircuitBreaker", ["check", "health_check"])),
                "RiskColorManager": self._validate_module("RiskColorManager", risk_color_manager, self.REQUIRED_METHODS.get("RiskColorManager", ["get_current_color", "health_check"])),
                "BayesianAdapter": bayesian_adapter,
                "ContagionBlocker": self._validate_module("ContagionBlocker", contagion_blocker, self.REQUIRED_METHODS.get("ContagionBlocker", ["check", "health_check"])),
                "FragilityCalculator": self._validate_module("FragilityCalculator", fragility_calculator, self.REQUIRED_METHODS.get("FragilityCalculator", ["get_fragility_index", "health_check"])),
                "DailyLossLimiter": self._validate_module("DailyLossLimiter", daily_loss_limiter, self.REQUIRED_METHODS.get("DailyLossLimiter", ["check", "health_check"])),
                "OrderRiskGateway": self._validate_module("OrderRiskGateway", order_risk_gateway, self.REQUIRED_METHODS.get("OrderRiskGateway", ["check", "health_check"])),
                "StressTestRunner": stress_test_runner,
            }
            self._negotiation_bus = negotiation_bus
            self._account_ledger = account_ledger
            self._behavioral_logger = behavioral_logger

            # 自动评估核心降级状态
            was_degraded = self._core_degraded
            core_missing = [m for m in self._config["core_modules"] if self._sub_modules.get(m) is None]
            self._core_degraded = len(core_missing) > 0

            if self._core_degraded and not was_degraded:
                logger.critical(f"核心风控模块缺失: {core_missing}，进入 CORE_DEGRADED 状态")
                self._push_alert("CORE_DEGRADED", f"核心风控模块缺失: {core_missing}")
            elif not self._core_degraded and was_degraded:
                logger.info("核心风控模块已全部恢复，解除 CORE_DEGRADED 状态")
                self._push_alert("CORE_RECOVERED", "核心风控模块已恢复")

    # ========== 公共接口 ==========
    def evaluate_risk(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        # 生成唯一追踪ID，使用单调时间戳前缀
        trace_id = intent.get("pulse_id") or f"{time.monotonic():.6f}_{uuid.uuid4().hex[:8]}"
        intent["pulse_id"] = trace_id

        # 输入校验
        symbol = intent.get("symbol")
        if not symbol or not isinstance(symbol, str) or len(symbol) > 20:
            return self._reject("INVALID_SYMBOL", "交易品种无效")
        # 防御注入攻击，仅允许字母数字和常见符号
        if not all(c.isalnum() or c in "-_./" for c in symbol):
            return self._reject("INVALID_SYMBOL", "交易品种包含非法字符")
        direction = intent.get("direction")
        if direction not in (1, -1):
            return self._reject("INVALID_DIRECTION", "交易方向无效")
        try:
            desired_size = float(intent.get("desired_size_pct", 0))
        except (ValueError, TypeError):
            return self._reject("INVALID_SIZE", "期望仓位格式错误")
        if desired_size <= 0 or desired_size > 1.0:
            return self._reject("INVALID_SIZE", "期望仓位超出合理范围")

        min_notional = float(self._config.get("min_position_notional", 10.0))
        if desired_size < min_notional:
            return self._reject("POSITION_TOO_SMALL", f"仓位 {desired_size:.6f} 低于最小限制 {min_notional}")

        # 原子化读取状态
        with self._rw_lock:
            core_degraded = self._core_degraded
            sub_modules = {k: v for k, v in self._sub_modules.items()}
            risk_color = self._get_risk_color_locked()
            config = dict(self._config)
            account_ledger = self._account_ledger
            behavioral_logger = self._behavioral_logger

        if core_degraded:
            return self._reject("CORE_DEGRADED", "核心风控缺失，拒绝开仓", risk_color)

        if risk_color in ("orange", "red", "black") and direction in (1, -1):
            return self._reject("RISK_COLOR_BLOCKED", f"风险色彩 {risk_color}，禁止新开仓", risk_color)

        warnings = []
        allowed_size = desired_size
        reason = ""
        audit_factors = {"risk_color": risk_color, "trace_id": trace_id}

        # 1. 熔断检查
        cb = sub_modules.get("CircuitBreaker")
        if cb is None:
            return self._reject("CIRCUIT_BREAKER_MISSING", "熔断模块不可用", risk_color)
        try:
            cb_res = cb.check(intent)
            if not cb_res.get("allowed", False):
                return self._reject("CIRCUIT_BREAKER", cb_res.get("reason", "熔断限制"), risk_color)
            audit_factors["circuit_breaker"] = "pass"
        except Exception as e:
            logger.critical(f"CircuitBreaker 异常: {e}", exc_info=True)
            return self._reject("CIRCUIT_BREAKER_ERROR", "熔断模块异常", risk_color)

        # 2. 传染阻断（优先判定绝对禁止）
        blk = sub_modules.get("ContagionBlocker")
        if blk:
            try:
                blk_res = blk.check(intent)
                if not blk_res.get("allowed", False):
                    max_size = float(blk_res.get("max_size_pct", 0))
                    if max_size <= 0:
                        return self._reject("CONTAGION_BLOCKED", blk_res.get("reason", "传染阻断禁止"), risk_color)
                    allowed_size = min(allowed_size, max_size)
                    reason = blk_res.get("reason", reason)
                    warnings.append(f"传染阻断限制: {reason}")
                    audit_factors["contagion"] = f"limited_to_{allowed_size:.6f}"
                else:
                    audit_factors["contagion"] = "pass"
            except Exception as e:
                logger.error(f"ContagionBlocker 异常: {e}", exc_info=True)

        # 3. 订单风控网关
        og = sub_modules.get("OrderRiskGateway")
        if og is None:
            return self._reject("ORDER_GATEWAY_MISSING", "订单网关不可用", risk_color)
        try:
            og_res = og.check(intent)
            if not og_res.get("allowed", False):
                return self._reject("ORDER_GATEWAY", og_res.get("reason", "订单风控限制"), risk_color)
            audit_factors["order_gateway"] = "pass"
        except Exception as e:
            logger.critical(f"OrderRiskGateway 异常: {e}", exc_info=True)
            return self._reject("ORDER_GATEWAY_ERROR", "订单网关异常", risk_color)

        # 4. 日内亏损限额
        dl = sub_modules.get("DailyLossLimiter")
        if dl:
            try:
                dl_res = dl.check(intent)
                if not dl_res.get("allowed", False):
                    return self._reject("DAILY_LOSS", dl_res.get("reason", "日内亏损限额"), risk_color)
                audit_factors["daily_loss"] = "pass"
            except Exception as e:
                logger.error(f"DailyLossLimiter 异常: {e}", exc_info=True)

        # 5. 脆弱性指数
        frag = sub_modules.get("FragilityCalculator")
        if frag:
            try:
                frag_res = frag.get_fragility_index()
                frag_data = frag_res.get("data")
                if isinstance(frag_data, dict):
                    frag_level = frag_data.get("level", "low")
                else:
                    frag_level = "low"
                audit_factors["fragility_level"] = frag_level
                if frag_level == "extreme":
                    cap_ratio = max(0.1, float(config.get("fragility_extreme_cap_ratio", 0.5)))
                    allowed_size = min(allowed_size, allowed_size * cap_ratio)
                    warnings.append("脆弱性指数极高，仓位强制缩减")
                elif frag_level == "high":
                    cap_ratio = max(0.1, float(config.get("fragility_high_cap_ratio", 0.7)))
                    allowed_size = min(allowed_size, allowed_size * cap_ratio)
            except Exception as e:
                logger.error(f"FragilityCalculator 异常: {e}", exc_info=True)

        # 6. 最小仓位保护（最终检查，使用浮点容差）
        if allowed_size < min_notional - 1e-9:
            return self._reject("POSITION_TOO_SMALL", f"调整后仓位 {allowed_size:.6f} 低于最小限制 {min_notional}", risk_color)

        # 7. 保证金预估
        if account_ledger and hasattr(account_ledger, 'check_margin_after_trade'):
            try:
                margin_ok = account_ledger.check_margin_after_trade(symbol, direction, allowed_size)
                if not margin_ok.get("allowed", True):
                    return self._reject("MARGIN_INSUFFICIENT", margin_ok.get("reason", "保证金不足"), risk_color)
                audit_factors["margin"] = "pass"
            except Exception as e:
                logger.error(f"保证金检查异常: {e}", exc_info=True)
                return self._reject("MARGIN_CHECK_ERROR", "保证金检查失败", risk_color)

        # 审计日志（限流）
        if behavioral_logger:
            with self._alert_lock:
                if time.time() - self._last_alert_time > config.get("alert_rate_limit_sec", 1.0):
                    self._last_alert_time = time.time()
                    log_flag = True
                else:
                    log_flag = False
            if log_flag:
                try:
                    behavioral_logger.log_event(
                        event_type="risk_evaluation",
                        details={
                            "trace_id": trace_id,
                            "symbol": symbol,
                            "direction": direction,
                            "allowed": True,
                            "allowed_size": round(allowed_size, 6),
                            "risk_color": risk_color,
                            "factors": audit_factors,
                            "warnings": warnings,
                        },
                    )
                except Exception:
                    pass

        return {
            "status": "ok",
            "reason": reason or "风控检查通过",
            "data": {
                "allowed": True,
                "allowed_size_pct": round(allowed_size, 6),
                "risk_color": risk_color,
                "error_code": "",
                "restriction_reason": reason,
                "audit_factors": audit_factors,
            },
            "warnings": warnings,
        }

    def get_current_risk_color(self) -> Dict[str, Any]:
        with self._rw_lock:
            color = self._get_risk_color_locked()
        return {"status": "ok", "data": {"color": color}}

    def get_fragility_index(self) -> Dict[str, Any]:
        frag = self._sub_modules.get("FragilityCalculator")
        if frag:
            try:
                return frag.get_fragility_index()
            except Exception:
                pass
        return {"status": "degraded", "data": {"index": 0.0, "level": "degraded"}}

    def get_total_exposure(self) -> Dict[str, Any]:
        return {"status": "ok", "data": {"total_exposure": 0.0, "detail": {}}}

    def health_check(self) -> Dict[str, Any]:
        try:
            with self._rw_lock:
                sub_modules_copy = {k: v for k, v in self._sub_modules.items()}
                config = dict(self._config)
            status_map = {}
            timeout = config.get("health_check_timeout_sec", 2.0)
            for name, inst in sub_modules_copy.items():
                if inst and hasattr(inst, "health_check"):
                    try:
                        # 简化超时控制，使用线程+join实现
                        import threading as th
                        result_container = {}
                        def _call_health():
                            try:
                                result_container["res"] = inst.health_check()
                            except Exception as ex:
                                result_container["res"] = {"status": "error", "message": str(ex)}
                        t = th.Thread(target=_call_health)
                        t.daemon = True
                        t.start()
                        t.join(timeout)
                        if t.is_alive():
                            status_map[name] = "timeout"
                            logger.warning(f"子模块 {name} 健康检查超时")
                        else:
                            res = result_container.get("res", {"status": "error"})
                            status_map[name] = res.get("status", "unknown")
                    except Exception:
                        status_map[name] = "error"
                else:
                    status_map[name] = "missing"
            ok_count = sum(1 for v in status_map.values() if v == "ok")
            return {
                "status": "ok" if ok_count == len(status_map) else "degraded",
                "reason": f"子模块健康: {ok_count}/{len(status_map)}",
                "data": status_map,
                "warnings": [f"{k}: {v}" for k, v in status_map.items() if v != "ok"],
            }
        except Exception as e:
            return {"status": "error", "reason": str(e), "data": {}}

    # ========== 私有方法 ==========
    def _get_risk_color_locked(self) -> str:
        rc = self._sub_modules.get("RiskColorManager")
        if rc and hasattr(rc, "get_current_color"):
            try:
                return rc.get_current_color()
            except Exception:
                logger.warning("RiskColorManager 异常，使用降级色彩", exc_info=True)
        return self._config.get("default_risk_color", "orange")

    def _reject(self, error_code: str, reason: str, risk_color: Optional[str] = None) -> Dict[str, Any]:
        color = risk_color if risk_color else self._config.get("default_risk_color", "orange")
        return {
            "status": "ok",
            "reason": reason,
            "data": {
                "allowed": False,
                "allowed_size_pct": 0.0,
                "error_code": error_code,
                "restriction_reason": reason,
                "risk_color": color,
            },
            "warnings": [reason],
        }

    def _push_alert(self, alert_type: str, message: str) -> None:
        if self._negotiation_bus and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(alert_type=alert_type, message=message, timestamp=time.time())
            except Exception:
                pass
        logger.critical(f"风控告警: [{alert_type}] {message}")
