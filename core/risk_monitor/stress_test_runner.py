"""
火种系统 · 压力测试执行器 (StressTestRunner)

核心职责：
1. 加载预定义压力测试场景库（闪崩、流动性枯竭、级联清算等）及运维者自定义情景，在隔离的虚拟券商环境中批量执行
2. 对每个测试场景下的策略组合进行生存能力评估，计算最大回撤、夏普比率、保证金覆盖率等关键风险指标，并生成结构化测试报告

外部依赖（真实模块接口）：
- ghost.virtual_broker.VirtualBroker : 提供高保真撮合与合成极端行情生成能力，用于模拟压力测试环境
- core.order_manager.OrderManager : 获取当前活跃策略的持仓上下文，用于初始化测试前快照
- core.risk_monitor.circuit_breaker.CircuitBreaker : 查询当前熔断状态（仅日志参考，不阻止模拟测试）
- core.behavioral_logger.BehavioralLogger : 记录压力测试执行日志与测试报告

接口契约：
- load_scenarios(scenario_config: Dict[str, Any]) -> Dict[str, Any] : 加载并校验场景定义
- execute_stress_test(strategy_ids: List[str], scenarios: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any] : 执行压力测试，返回详细报告（含test_id、survival_summary、failure_code_summary等）
- get_latest_report() -> Dict[str, Any] : 获取最近一次压力测试的结构化报告（深拷贝，不可修改）
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 VirtualBroker 不可用或缺少必需方法时，所有压力测试请求返回降级状态，并提示无法创建模拟环境
- 当场景定义包含非法参数时，自动修正为保守默认值并记录原始值到日志，同时在返回的warnings中体现
- 当执行过程中虚拟券商异常中断，保留已完成场景的部分结果，并报告失败场景列表

资源管理：
- 每次压力测试在独立的虚拟券商会话中执行，使用 finally 确保会话正确关闭，并限制单场景内存使用
- 不持有任何持久的外部资源句柄，所有中间结果在报告生成后自动回收
"""

import time
import logging
import threading
import uuid
import hashlib
import copy
from typing import Dict, Any, List, Optional, Tuple, Union
from collections import deque

import numpy as np

logger = logging.getLogger(__name__)


class StressTestRunner:
    """压力测试执行器"""

    # 虚拟券商必须实现的方法
    REQUIRED_VB_METHODS = [
        "create_synthetic_session",
    ]

    # 配置常量（默认值，可从配置文件覆盖）
    DEFAULT_MAX_SCENARIOS = 20
    DEFAULT_TIMEOUT_SECONDS_PER_SCENARIO = 60
    DEFAULT_SINGLE_TICK_TIMEOUT_SECONDS = 10  # 单次 tick 最大允许执行时间
    DEFAULT_RECENT_REPORT_CACHE_SIZE = 5
    DEFAULT_MAX_SERIES_POINTS = 10_000
    DEFAULT_EXECUTION_LOCK_TIMEOUT = 2.0
    DEFAULT_WARMUP_TICKS = 5
    MAX_VOL_MULT = 20.0
    MIN_VOL_MULT = 1.0
    MAX_DEPTH_DECAY = 1.0
    MIN_DEPTH_DECAY = 0.0

    # 压力测试指标基线（单位已明确）
    SURVIVAL_THRESHOLDS = {
        "max_drawdown_pct": 30.0,           # 最大回撤百分比，相对初始权益
        "min_sharpe": -1.0,                 # 夏普比率（假设无风险利率为0）
        "min_margin_coverage_pct": 120.0,   # 保证金覆盖率百分比（权益 / 已用保证金 * 100）
    }

    # 预定义失败原因编码
    FAILURE_CODES = {
        "INSUFFICIENT_DATA": "数据不足 (样本 < 2)",
        "TIMEOUT": "场景执行超时",
        "MAX_DRAWDOWN_EXCEEDED": "最大回撤超标",
        "SHARPE_BELOW_THRESHOLD": "夏普比率低于阈值",
        "MARGIN_COVERAGE_LOW": "保证金覆盖率不足",
        "SESSION_CREATION_FAILED": "虚拟券商会话创建失败",
        "TICK_EXCEPTION": "tick 执行异常",
        "GENERAL_EXCEPTION": "场景整体异常",
    }

    def __init__(self):
        # 场景缓存
        self._scenarios: Dict[str, Dict[str, Any]] = {}
        # 报告缓存（深拷贝存储，防止外部修改）
        self._recent_reports: deque = deque(maxlen=self.DEFAULT_RECENT_REPORT_CACHE_SIZE)
        # 外部依赖
        self._virtual_broker = None
        self._order_manager = None
        self._circuit_breaker = None
        self._behavioral_logger = None
        # 线程安全
        self._lock = threading.Lock()
        self._execution_lock = threading.Lock()
        self._health_lock = threading.Lock()
        self._last_health_check_time = 0.0
        self._health_check_cache = {}
        # 正在运行的测试ID（用于中断）
        self._active_test_id: Optional[str] = None
        self._cancel_event = threading.Event()
        logger.info("StressTestRunner 初始化完成，最大场景数 %d", self.DEFAULT_MAX_SCENARIOS)

    def inject_dependencies(
        self,
        virtual_broker: Optional[Any] = None,
        order_manager: Optional[Any] = None,
        circuit_breaker: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """注入外部依赖，重新注入时会覆盖原有依赖。"""
        if virtual_broker is not None:
            missing = [m for m in self.REQUIRED_VB_METHODS if not hasattr(virtual_broker, m)]
            if missing:
                logger.warning("VirtualBroker 缺少必需方法: %s，压力测试功能将不可用", missing)
                self._virtual_broker = None
            else:
                self._virtual_broker = virtual_broker
                logger.info("VirtualBroker 注入成功")
        # 其他依赖可被覆盖
        if order_manager is not None:
            self._order_manager = order_manager
        if circuit_breaker is not None:
            self._circuit_breaker = circuit_breaker
        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger

    # --- 公共接口 ---
    def load_scenarios(self, scenario_config: Dict[str, Any]) -> Dict[str, Any]:
        """加载场景，返回包含warnings的标准化字典。"""
        if not isinstance(scenario_config, dict) or not scenario_config:
            return {"status": "error", "reason": "场景配置为空或格式无效", "data": {}, "warnings": ["invalid_scenario_config"]}
        loaded = 0
        warnings = []
        with self._lock:
            for name, params in scenario_config.items():
                if not isinstance(params, dict) or not str(name).strip():
                    warnings.append(f"跳过无效场景: {name}")
                    continue
                name = str(name).strip()
                if name in self._scenarios:
                    old_params = self._scenarios[name]
                    warnings.append(f"场景 '{name}' 已存在，将被覆盖。旧参数: vol_mult={old_params.get('volatility_multiplier')}, depth_decay={old_params.get('depth_decay_ratio')}")
                validated = self._validate_and_clamp_params(params, name)
                self._scenarios[name] = validated
                loaded += 1
        return {"status": "ok", "reason": f"成功加载 {loaded} 个场景", "data": {"loaded_count": loaded}, "warnings": warnings}

    def execute_stress_test(
        self,
        strategy_ids: List[str],
        scenarios: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        执行压力测试。同一时间只允许一个测试运行。
        """
        if self._virtual_broker is None:
            return {"status": "error", "reason": "VirtualBroker 不可用", "data": {}, "warnings": ["missing_dependency: virtual_broker"]}
        if not strategy_ids:
            return {"status": "error", "reason": "策略ID列表为空", "data": {}, "warnings": ["empty_strategy_list"]}

        # 准备场景
        if scenarios is None or len(scenarios) == 0:
            with self._lock:
                scenarios_to_run = copy.deepcopy(list(self._scenarios.values()))
            if not scenarios_to_run:
                return {"status": "error", "reason": "未加载任何场景且未传入场景列表", "data": {}, "warnings": ["no_scenarios_available"]}
        else:
            scenarios_to_run = copy.deepcopy(scenarios)

        if len(scenarios_to_run) > self.DEFAULT_MAX_SCENARIOS:
            scenarios_to_run = scenarios_to_run[: self.DEFAULT_MAX_SCENARIOS]
            logger.warning("场景数超限，已截断至 %d 个", self.DEFAULT_MAX_SCENARIOS)

        # 检查熔断状态（仅记录日志）
        if self._circuit_breaker is not None and hasattr(self._circuit_breaker, 'is_active') and self._circuit_breaker.is_active():
            logger.warning("实盘熔断激活中，压力测试仍在虚拟环境执行，不影响实盘")

        # 获取脱敏的持仓结构快照
        position_snapshot = self._capture_position_snapshot(strategy_ids)

        # 获取执行锁，防止并发执行
        acquired = self._execution_lock.acquire(timeout=self.DEFAULT_EXECUTION_LOCK_TIMEOUT)
        if not acquired:
            return {"status": "error", "reason": "获取执行锁超时，可能已有压力测试正在运行", "data": {}, "warnings": ["execution_lock_timeout"]}

        test_id = str(uuid.uuid4())[:8]
        self._active_test_id = test_id
        self._cancel_event.clear()
        results = []
        warnings = []
        start_time = time.time()

        try:
            for idx, scenario in enumerate(scenarios_to_run):
                if self._cancel_event.is_set():
                    logger.warning("[%s] 压力测试被外部中断", test_id)
                    warnings.append("测试被外部中断")
                    break

                name = scenario.get("name", f"scenario_{idx}")
                logger.info("[%s] %d/%d: %s 开始 (vol=%.1f, depth=%.2f)", test_id, idx + 1, len(scenarios_to_run), name,
                            scenario.get("volatility_multiplier", 3.0), scenario.get("depth_decay_ratio", 0.8))
                try:
                    result = self._run_single_scenario(test_id, name, scenario, strategy_ids, position_snapshot)
                    results.append(result)
                except Exception as e:
                    logger.error("[%s] 场景 %s 整体异常: %s", test_id, name, str(e), exc_info=True)
                    results.append({
                        "scenario_name": name,
                        "passed": False,
                        "failure_codes": ["GENERAL_EXCEPTION"],
                        "failure_details": [str(e)],
                        "duration_seconds": 0,
                        "max_drawdown_pct": 0,
                        "sharpe": 0,
                        "market_params": scenario,
                    })
                    warnings.append(f"场景 {name} 异常: {str(e)}")

            total_elapsed = time.time() - start_time
            report = {
                "test_id": test_id,
                "timestamp": time.time(),
                "strategy_ids": strategy_ids,
                "total_scenarios": len(scenarios_to_run),
                "results": results,
                "survival_summary": self._calculate_survival_summary(results),
                "total_duration_seconds": round(total_elapsed, 2),
                "initial_position_empty": not bool(position_snapshot),
            }

            # 缓存深拷贝，防止外部修改
            with self._lock:
                self._recent_reports.append(copy.deepcopy(report))

            # 行为日志
            if self._behavioral_logger:
                try:
                    self._behavioral_logger.log_event("stress_test_completed", details={
                        "test_id": test_id,
                        "total": len(scenarios_to_run),
                        "passed": report["survival_summary"]["passed"],
                        "survival_rate": report["survival_summary"]["survival_rate_pct"],
                    })
                except Exception:
                    logger.warning("行为日志记录失败")

            return {"status": "ok", "reason": f"测试完成 {report['survival_summary']['passed']}/{len(scenarios_to_run)}", "data": report, "warnings": warnings}
        finally:
            self._active_test_id = None
            self._execution_lock.release()

    def get_latest_report(self) -> Dict[str, Any]:
        """获取最近一次报告，返回深拷贝以确保不可修改。"""
        with self._lock:
            if not self._recent_reports:
                return {"status": "error", "reason": "无报告", "data": {}, "warnings": []}
            return {"status": "ok", "reason": "最新报告", "data": copy.deepcopy(self._recent_reports[-1]), "warnings": []}

    def cancel_active_test(self) -> Dict[str, Any]:
        """中断当前正在运行的压力测试。"""
        if self._active_test_id:
            self._cancel_event.set()
            logger.info("已请求中断压力测试: %s", self._active_test_id)
            return {"status": "ok", "reason": f"已发送中断信号给测试 {self._active_test_id}", "data": {}, "warnings": []}
        return {"status": "error", "reason": "当前没有正在运行的压力测试", "data": {}, "warnings": ["no_active_test"]}

    def health_check(self) -> Dict[str, Any]:
        """模块自检，包含虚拟券商功能验证。"""
        try:
            with self._health_lock:
                now = time.time()
                if now - self._last_health_check_time < 5.0 and self._health_check_cache:
                    return self._health_check_cache

                virtual_broker_available = False
                if self._virtual_broker:
                    session = None
                    try:
                        session = self._virtual_broker.create_synthetic_session(
                            {"volatility_multiplier": 1.0, "depth_decay_ratio": 0.0}, {}
                        )
                        if session and hasattr(session, 'tick') and hasattr(session, 'close'):
                            virtual_broker_available = True
                    except Exception as e:
                        logger.warning("虚拟券商功能测试失败: %s", e)
                    finally:
                        if session:
                            try:
                                session.close()
                            except Exception:
                                pass

                result = {
                    "status": "ok",
                    "reason": f"场景 {len(self._scenarios)} 个，报告 {len(self._recent_reports)} 个",
                    "data": {
                        "scenario_count": len(self._scenarios),
                        "report_count": len(self._recent_reports),
                        "active_test_id": self._active_test_id,
                        "dependencies": {
                            "virtual_broker": {"available": virtual_broker_available, "impact": "压力测试不可用"},
                            "order_manager": {"available": self._order_manager is not None, "impact": "快照为空"},
                            "circuit_breaker": {"available": self._circuit_breaker is not None, "impact": "无法感知熔断"},
                            "behavioral_logger": {"available": self._behavioral_logger is not None, "impact": "仅本地日志"},
                        },
                    },
                    "warnings": [],
                }
                self._last_health_check_time = now
                self._health_check_cache = result
                return result
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查对象完整性")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"]}

    # --- 私有方法 ---
    def _validate_and_clamp_params(self, params: Dict[str, Any], name: str) -> Dict[str, Any]:
        """校验并钳位场景参数，返回经过清理的参数字典。"""
        warnings = []
        vol = params.get("volatility_multiplier", 3.0)
        if not isinstance(vol, (int, float)) or vol < self.MIN_VOL_MULT or vol > self.MAX_VOL_MULT:
            original = repr(vol)
            vol = max(self.MIN_VOL_MULT, min(self.MAX_VOL_MULT, float(vol)))
            warnings.append(f"volatility_multiplier 修正: {original} -> {vol}")
            logger.warning("场景 %s volatility_multiplier 非法(%s)，已修正为 %.2f", name, original, vol)

        depth = params.get("depth_decay_ratio", 0.8)
        if not isinstance(depth, (int, float)) or depth < self.MIN_DEPTH_DECAY or depth > self.MAX_DEPTH_DECAY:
            original = repr(depth)
            depth = max(self.MIN_DEPTH_DECAY, min(self.MAX_DEPTH_DECAY, float(depth)))
            warnings.append(f"depth_decay_ratio 修正: {original} -> {depth}")
            logger.warning("场景 %s depth_decay_ratio 非法(%s)，已修正为 %.2f", name, original, depth)

        return {
            "name": name,
            "volatility_multiplier": float(vol),
            "depth_decay_ratio": float(depth),
            "description": str(params.get("description", "")),
            "_load_warnings": warnings,  # 内部使用，传递给场景加载的warnings
        }

    def _capture_position_snapshot(self, strategy_ids: List[str]) -> Dict[str, Any]:
        """获取脱敏的持仓结构快照（仅保留方向，不保留数量）。"""
        if not self._order_manager or not hasattr(self._order_manager, 'get_strategy_positions'):
            return {}
        snapshots = {}
        try:
            with self._lock:
                for sid in strategy_ids:
                    try:
                        raw = self._order_manager.get_strategy_positions(sid)
                        sanitized = {}
                        if isinstance(raw, dict):
                            for sym, pos in raw.items():
                                sanitized[sym] = {"side": pos.get("side", 0) if isinstance(pos, dict) else 0}
                        snapshots[sid] = sanitized
                    except Exception as e:
                        logger.warning("获取策略 %s 持仓失败: %s", sid, str(e))
        except Exception as e:
            logger.warning("持仓快照整体失败: %s", str(e))
        return snapshots

    def _run_single_scenario(self, test_id: str, name: str, scenario: Dict[str, Any],
                             strategy_ids: List[str], position_snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """在隔离的虚拟券商中执行单个压力测试场景。"""
        params = {
            "volatility_multiplier": scenario["volatility_multiplier"],
            "depth_decay_ratio": scenario["depth_decay_ratio"],
        }
        session = None
        start_time = time.time()
        equity_series = deque(maxlen=self.DEFAULT_MAX_SERIES_POINTS)
        margin_series = deque(maxlen=self.DEFAULT_MAX_SERIES_POINTS)
        peak_equity = None
        max_dd = 0.0
        termination_reason = "timeout"
        failure_codes = []
        failure_details = []

        result = {
            "scenario_name": name,
            "passed": False,
            "max_drawdown_pct": 0.0,
            "sharpe": 0.0,
            "min_margin_coverage_pct": None,
            "failure_codes": [],
            "failure_details": [],
            "duration_seconds": 0.0,
            "market_params": params,
        }

        try:
            session = self._virtual_broker.create_synthetic_session(params, position_snapshot)
            if session is None or not (hasattr(session, 'tick') and hasattr(session, 'close')):
                result["failure_codes"] = ["SESSION_CREATION_FAILED"]
                result["failure_details"] = ["虚拟券商返回无效会话"]
                return result

            # 设置可复现种子
            seed = int(hashlib.sha256(f"{test_id}:{name}".encode()).hexdigest(), 16) % (2**31)
            if hasattr(session, 'set_seed'):
                session.set_seed(seed)

            tick_start_time = time.time()
            while (time.time() - start_time) < self.DEFAULT_TIMEOUT_SECONDS_PER_SCENARIO:
                # 单次 tick 超时检查（基于最后 tick 完成时间）
                if time.time() - tick_start_time > self.DEFAULT_SINGLE_TICK_TIMEOUT_SECONDS:
                    failure_codes.append("TIMEOUT")
                    failure_details.append("单次 tick 超时")
                    termination_reason = "tick_timeout"
                    break

                try:
                    tick = session.tick()
                except Exception as e:
                    failure_codes.append("TICK_EXCEPTION")
                    failure_details.append(str(e))
                    break

                tick_start_time = time.time()  # 重置单次 tick 计时

                if tick is None or not isinstance(tick, dict):
                    failure_codes.append("TICK_EXCEPTION")
                    failure_details.append("虚拟券商返回无效数据")
                    break
                if tick.get("finished", False):
                    termination_reason = "finished"
                    break

                # 处理权益
                eq = tick.get("equity")
                if eq is not None and isinstance(eq, (int, float)):
                    eq = float(max(0.0, eq))
                    equity_series.append(eq)

                    # 初始权益作为峰值基线
                    if peak_equity is None:
                        peak_equity = eq
                    elif eq > peak_equity:
                        peak_equity = eq

                    if peak_equity and peak_equity > 0:
                        dd = (peak_equity - eq) / peak_equity * 100.0
                        if dd > max_dd:
                            max_dd = dd
                else:
                    continue  # 忽略无权益数据的tick

                # 处理保证金覆盖率
                mc = tick.get("margin_coverage_pct")
                if mc is not None and isinstance(mc, (int, float)):
                    margin_series.append(float(mc))
                elif "margin_used" in tick and tick.get("margin_used", 0.0) > 0 and eq is not None:
                    # 若虚拟券商未提供百分比，则自行计算（需确保 eq 已获取）
                    margin_series.append(eq / tick["margin_used"] * 100.0)

            elapsed = time.time() - start_time
            result["duration_seconds"] = round(elapsed, 2)

            if termination_reason == "timeout" or termination_reason == "tick_timeout":
                failure_codes.append("TIMEOUT")
                failure_details.append(f"超时 ({elapsed:.1f}s)")

            # 检查数据量
            if len(equity_series) < 2:
                failure_codes.append("INSUFFICIENT_DATA")
                failure_details.append(f"有效权益样本不足 ({len(equity_series)})")
                result["failure_codes"] = failure_codes
                result["failure_details"] = failure_details
                result["max_drawdown_pct"] = round(max_dd, 2)
                return result

            # 预热数据去除（可选）
            warmup = min(self.DEFAULT_WARMUP_TICKS, max(0, len(equity_series) - 10))
            if warmup > 0:
                equity_list = list(equity_series)[warmup:]
            else:
                equity_list = list(equity_series)

            if len(equity_list) < 2:
                failure_codes.append("INSUFFICIENT_DATA")
                failure_details.append("预热后样本不足")
                result["failure_codes"] = failure_codes
                result["failure_details"] = failure_details
                return result

            # 计算夏普比率（假设无风险利率为0）
            returns = np.diff(equity_list) / (np.array(equity_list[:-1]) + 1e-10)
            returns = returns[np.isfinite(returns)]
            sharpe = float(np.mean(returns) / (np.std(returns) + 1e-10)) if len(returns) > 0 else 0.0

            result["max_drawdown_pct"] = round(max_dd, 2)
            result["sharpe"] = round(sharpe, 4)

            # 保证金覆盖率最低值
            if margin_series:
                result["min_margin_coverage_pct"] = round(float(np.min(margin_series)), 2)

            # 生存判定
            if max_dd > self.SURVIVAL_THRESHOLDS["max_drawdown_pct"]:
                failure_codes.append("MAX_DRAWDOWN_EXCEEDED")
                failure_details.append(f"回撤 {max_dd:.2f}% > {self.SURVIVAL_THRESHOLDS['max_drawdown_pct']}%")
            if sharpe < self.SURVIVAL_THRESHOLDS["min_sharpe"]:
                failure_codes.append("SHARPE_BELOW_THRESHOLD")
                failure_details.append(f"夏普 {sharpe:.4f} < {self.SURVIVAL_THRESHOLDS['min_sharpe']}")
            min_margin = result.get("min_margin_coverage_pct")
            if min_margin is not None and min_margin < self.SURVIVAL_THRESHOLDS["min_margin_coverage_pct"]:
                failure_codes.append("MARGIN_COVERAGE_LOW")
                failure_details.append(f"保证金覆盖率 {min_margin:.2f}% < {self.SURVIVAL_THRESHOLDS['min_margin_coverage_pct']}%")

            result["passed"] = len(failure_codes) == 0
            result["failure_codes"] = failure_codes
            result["failure_details"] = failure_details

        except Exception as e:
            logger.error("[%s] 场景 %s 整体异常: %s", test_id, name, str(e), exc_info=True)
            result["failure_codes"] = ["GENERAL_EXCEPTION"]
            result["failure_details"] = [str(e)]
            result["passed"] = False
        finally:
            if session:
                try:
                    session.close()
                except Exception:
                    logger.warning("关闭虚拟券商会话异常")

        return result

    def _calculate_survival_summary(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """根据场景结果计算生存能力摘要。"""
        total = len(results)
        passed = sum(1 for r in results if r.get("passed", False))
        code_counts = {}
        for r in results:
            for code in r.get("failure_codes", []):
                code_counts[code] = code_counts.get(code, 0) + 1
        # 计算组合平均夏普和平均最大回撤（可选，用于报告）
        sharpes = [r.get("sharpe", 0) for r in results]
        drawdowns = [r.get("max_drawdown_pct", 0) for r in results]
        return {
            "total": total,
            "passed": passed,
            "survival_rate_pct": round(passed / total * 100, 1) if total > 0 else 0.0,
            "failure_code_summary": code_counts,
            "average_sharpe": round(float(np.mean(sharpes)), 4) if sharpes else None,
            "average_max_drawdown_pct": round(float(np.mean(drawdowns)), 2) if drawdowns else None,
      }
