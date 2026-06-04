"""
火种系统 · 压力测试执行器 (StressTestRunner)

核心职责：
1. 加载预定义压力测试场景库（闪崩、流动性枯竭、级联清算等）及运维者自定义情景，在隔离的虚拟券商环境中批量执行
2. 对每个测试场景下的策略组合进行生存能力评估，计算关键风险指标（最大回撤、夏普比率、保证金覆盖率等），并生成结构化测试报告
3. 支持多阶段复合场景、历史真实行情重放、预热期、恢复期评估及组合维度分析

外部依赖（真实模块接口）：
- ghost.virtual_broker.VirtualBroker : 提供高保真撮合、合成极端行情生成及历史盘口回放能力
- ghost.event_replay_engine.EventReplayEngine : 提供历史极端事件盘口快照注入能力
- core.order_manager.OrderManager : 获取当前活跃策略的持仓上下文及元数据（类型、版本等）
- core.risk_monitor.circuit_breaker.CircuitBreaker : 查询当前熔断状态，确保压力测试不会意外触发实盘风控
- core.behavioral_logger.BehavioralLogger : 记录压力测试执行日志与测试报告

接口契约：
- load_scenarios(scenario_config: Dict[str, Any]) -> Dict[str, Any] : 加载并校验场景定义
- load_historical_scenario(date_range: Tuple[str, str]) -> Dict[str, Any] : 加载历史真实极端行情
- execute_stress_test(strategy_ids, scenarios, mode, code_version, config_hash, summary_only) -> Dict[str, Any] : 执行压力测试
- get_test_status() -> Dict[str, Any] : 获取当前测试进度
- get_latest_report(summary_only: bool) -> Dict[str, Any] : 获取最近一次压力测试的结构化报告，支持摘要模式
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 VirtualBroker 不可用时，所有压力测试请求返回降级状态
- 当 EventReplayEngine 不可用时，历史场景加载功能降级为不可用
- 多阶段场景若虚拟券商不支持，自动标记为未通过并记录原因
- 所有降级值在类常量区明确声明

资源管理：
- 每次压力测试在独立的虚拟券商会话中执行，会话通过 try-finally 确保释放；超时会话由独立线程强制中断
- 同一时间仅允许一个压力测试执行（通过 _is_running 控制），避免资源竞争
- 不持有任何持久的外部资源句柄，所有中间结果在报告生成后自动回收
- 压力测试期间产生的虚拟订单日志携带统一前缀 [STRESS_TEST: test_id]
"""

import time
import logging
import threading
import copy
from typing import Dict, Any, List, Optional, Tuple, Union
from collections import deque
import uuid
import secrets
import numpy as np

logger = logging.getLogger(__name__)


class StressTestRunner:
    """压力测试执行器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_MAX_SCENARIOS = 20             # 单次测试最大场景数，无量纲，取值范围 [5, 50]
    DEFAULT_TIMEOUT_SECONDS_PER_SCENARIO = 60  # 单个场景执行超时，秒，[10, 300]
    DEFAULT_RECENT_REPORT_CACHE_SIZE = 5   # 最近报告缓存数量，无量纲，[1, 20]
    DEFAULT_PROGRESS_UPDATE_INTERVAL = 2   # 进度更新最小间隔（秒），[1, 10]
    DEFAULT_CIRCUIT_BREAKER_CHECK_INTERVAL = 10  # 压力测试期间熔断检查间隔，秒，[5, 30]
    DEFAULT_WARMUP_SECONDS = 10            # 默认预热时长，秒，[0, 60]
    DEFAULT_EQUITY_DOWNSAMPLE_MAX = 200    # 权益序列降采样最大点数，无量纲，[50, 500]
    DEFAULT_STAGNATION_TICKS = 20          # 连续相同 tick 判定为卡死的次数，无量纲，[5, 50]

    # 压力测试指标基线（默认值，可被策略自身配置覆盖）
    SURVIVAL_THRESHOLDS = {
        "max_drawdown_pct": 30.0,
        "min_sharpe": -1.0,
        "min_margin_coverage_pct": 120.0,
        "recovery_min_sharpe": 0.0,
    }

    # 策略类型默认阈值覆盖
    STRATEGY_TYPE_THRESHOLDS = {
        "market_making": {
            "max_drawdown_pct": 15.0,
            "min_sharpe": -0.5,
            "min_margin_coverage_pct": 150.0,
        },
        "trend_following": {
            "max_drawdown_pct": 35.0,
            "min_sharpe": -1.5,
            "min_margin_coverage_pct": 120.0,
        },
        "arbitrage": {
            "max_drawdown_pct": 10.0,
            "min_sharpe": 0.0,
            "min_margin_coverage_pct": 130.0,
        },
    }

    SCENARIO_SCHEMA = {
        "volatility_multiplier": 3.0,
        "depth_decay_ratio": 0.8,
        "spread_multiplier": 2.0,
        "duration_seconds": 60,
        "stages": None,
        "warmup_seconds": 10,
        "historical_warmup_seconds": 60,
        "recovery_phase": None,
    }

    CONSERVATIVE_DIRECTION = {
        "max_drawdown_pct": "min",
        "min_sharpe": "max",
        "min_margin_coverage_pct": "max",
        "recovery_min_sharpe": "max",
    }

    def __init__(self):
        self._scenarios: Dict[str, Dict[str, Any]] = {}
        self._recent_reports: deque = deque(maxlen=self.DEFAULT_RECENT_REPORT_CACHE_SIZE)
        self._is_running: bool = False
        self._progress: Dict[str, Any] = {}
        self._progress_lock = threading.Lock()
        self._current_test_id: Optional[str] = None
        self._virtual_broker = None
        self._event_replay_engine = None
        self._order_manager = None
        self._circuit_breaker = None
        self._behavioral_logger = None
        self._lock = threading.Lock()
        logger.info("StressTestRunner 初始化完成，最大场景数 %d", self.DEFAULT_MAX_SCENARIOS)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        virtual_broker: Optional[Any] = None,
        event_replay_engine: Optional[Any] = None,
        order_manager: Optional[Any] = None,
        circuit_breaker: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        if virtual_broker is not None:
            self._virtual_broker = virtual_broker
            logger.info("VirtualBroker 注入成功")
        else:
            logger.warning("VirtualBroker 未注入，压力测试功能将不可用")

        if event_replay_engine is not None:
            self._event_replay_engine = event_replay_engine
            logger.info("EventReplayEngine 注入成功")
        else:
            logger.warning("EventReplayEngine 未注入，历史场景回放功能降级")

        if order_manager is not None:
            self._order_manager = order_manager
            logger.info("OrderManager 注入成功")
        else:
            logger.warning("OrderManager 未注入，测试快照将使用空仓状态")

        if circuit_breaker is not None:
            self._circuit_breaker = circuit_breaker
            logger.info("CircuitBreaker 注入成功")
        else:
            logger.warning("CircuitBreaker 未注入，熔断状态检查将默认允许")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

    # ========== 公共接口 ==========
    def load_scenarios(self, scenario_config: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(scenario_config, dict) or not scenario_config:
            return {
                "status": "error",
                "reason": "场景配置为空或格式无效",
                "data": {},
                "warnings": ["invalid_scenario_config"],
            }
        loaded_count = 0
        warnings = []
        with self._lock:
            for scene_name, params in scenario_config.items():
                if not isinstance(params, dict):
                    warnings.append(f"场景 '{scene_name}' 参数无效，已跳过")
                    continue
                validated_params, param_warnings = self._validate_scenario_params(params)
                self._scenarios[scene_name] = validated_params
                loaded_count += 1
                warnings.extend(param_warnings)
                logger.debug("加载压力测试场景: %s", scene_name)
        return {
            "status": "ok",
            "reason": f"成功加载 {loaded_count} 个场景",
            "data": {"loaded_count": loaded_count},
            "warnings": warnings,
        }

    def load_historical_scenario(self, date_range: Tuple[str, str]) -> Dict[str, Any]:
        if self._event_replay_engine is None:
            return {
                "status": "error",
                "reason": "EventReplayEngine 未注入，无法加载历史场景",
                "data": {},
                "warnings": ["missing_dependency: event_replay_engine"],
            }
        try:
            start_date, end_date = date_range
            historical_data = self._event_replay_engine.load_historical_orderbook(
                start_date=start_date, end_date=end_date
            )
            required_fields = ["timestamps", "bids", "asks"]
            for field in required_fields:
                if field not in historical_data:
                    raise ValueError(f"历史数据缺少必要字段: {field}")
            if len(historical_data["timestamps"]) < 2:
                raise ValueError("历史数据时间序列过短（< 2 个采样点）")

            data_duration = historical_data["timestamps"][-1] - historical_data["timestamps"][0]
            if data_duration <= 0:
                raise ValueError(f"历史数据时间跨度为非正数 ({data_duration}s)")

            scene_name = f"historical_{start_date}_{end_date}"
            with self._lock:
                self._scenarios[scene_name] = {
                    "name": scene_name,
                    "type": "historical",
                    "data": historical_data,
                    "start_date": start_date,
                    "end_date": end_date,
                    "duration_seconds": int(data_duration),
                }
            logger.info("加载历史场景: %s，时长 %d 秒", scene_name, int(data_duration))
            return {
                "status": "ok",
                "reason": f"历史场景 {scene_name} 加载成功",
                "data": {"scene_name": scene_name},
                "warnings": [],
            }
        except Exception as e:
            logger.error("加载历史场景失败: %s #RECOVERY: 检查 EventReplayEngine 状态与日期格式", str(e))
            return {
                "status": "error",
                "reason": f"加载历史场景异常: {str(e)}",
                "data": {},
                "warnings": [f"historical_load_failed: {str(e)}"],
            }

    def execute_stress_test(
        self,
        strategy_ids: List[str],
        scenarios: Optional[List[Dict[str, Any]]] = None,
        mode: str = "single",
        code_version: Optional[str] = None,
        config_hash: Optional[str] = None,
        randomize_order: bool = True,
        summary_only: bool = False,
    ) -> Dict[str, Any]:
        with self._lock:
            if self._is_running:
                return {
                    "status": "busy",
                    "reason": "已有压力测试正在运行",
                    "data": {"current_test_id": self._current_test_id},
                    "warnings": ["test_already_running"],
                }
            self._is_running = True
            test_id = str(uuid.uuid4())[:8]
            self._current_test_id = test_id

        self._reset_progress(test_id)

        try:
            return self._execute_stress_test_impl(
                strategy_ids, scenarios, mode, test_id, code_version, config_hash, randomize_order, summary_only
            )
        finally:
            with self._lock:
                self._is_running = False
                self._current_test_id = None
            logger.info("压力测试 %s 结束，资源已释放", test_id)

    def _execute_stress_test_impl(
        self,
        strategy_ids: List[str],
        scenarios: Optional[List[Dict[str, Any]]],
        mode: str,
        test_id: str,
        code_version: Optional[str],
        config_hash: Optional[str],
        randomize_order: bool,
        summary_only: bool,
    ) -> Dict[str, Any]:
        if self._virtual_broker is None:
            return {
                "status": "error",
                "reason": "VirtualBroker 未注入，无法执行压力测试",
                "data": {},
                "warnings": ["missing_dependency: virtual_broker"],
            }
        if not strategy_ids:
            return {
                "status": "error",
                "reason": "策略ID列表为空",
                "data": {},
                "warnings": ["empty_strategy_list"],
            }
        if self._circuit_breaker is not None:
            if hasattr(self._circuit_breaker, 'is_active') and self._circuit_breaker.is_active():
                logger.warning("实盘熔断处于激活状态，压力测试将被拒绝")
                return {
                    "status": "error",
                    "reason": "实盘熔断激活中，压力测试已自动取消",
                    "data": {},
                    "warnings": ["circuit_breaker_active"],
                }

        scenarios_to_run = scenarios if scenarios else list(self._scenarios.values())
        if not scenarios_to_run:
            return {
                "status": "error",
                "reason": "无可用场景",
                "data": {},
                "warnings": ["no_scenarios_available"],
            }
        if len(scenarios_to_run) > self.DEFAULT_MAX_SCENARIOS:
            scenarios_to_run = scenarios_to_run[: self.DEFAULT_MAX_SCENARIOS]
            logger.warning("场景数超限，已截断至 %d 个", self.DEFAULT_MAX_SCENARIOS)

        # 使用 SystemRandom 进行可靠随机化
        if randomize_order:
            rng = random.SystemRandom()
            rng.shuffle(scenarios_to_run)
            logger.debug("场景执行顺序已随机化 (SystemRandom)")
        actual_order = [s.get("name", f"scenario_{i}") for i, s in enumerate(scenarios_to_run)]

        position_snapshot = self._capture_position_snapshot(strategy_ids)
        strategy_thresholds = self._load_strategy_thresholds(strategy_ids)

        if code_version is None and self._order_manager is not None:
            code_version = self._get_strategy_code_version(strategy_ids)
        if config_hash is None and self._order_manager is not None:
            config_hash = self._get_strategy_config_hash(strategy_ids)

        test_results = []
        warnings = []
        failed_scenarios = []
        portfolio_equity_series: List[float] = []
        total_scenarios = len(scenarios_to_run)

        for idx, scenario in enumerate(scenarios_to_run):
            scene_name = scenario.get("name", f"scenario_{idx}")
            self._update_progress(idx + 1, total_scenarios, scene_name)
            scenario_type = scenario.get("type", "synthetic")
            try:
                if scenario_type == "historical":
                    result = self._run_historical_scenario(
                        scene_name, scenario, strategy_ids, position_snapshot, strategy_thresholds
                    )
                else:
                    result = self._run_synthetic_scenario(
                        scene_name, scenario, strategy_ids, position_snapshot, strategy_thresholds, mode
                    )
                test_results.append(result)
                if "portfolio_equity" in result:
                    portfolio_equity_series.extend(result["portfolio_equity"])
                logger.info(
                    "场景 '%s' 测试完成: 通过=%s, 最大回撤=%.2f%%, 夏普=%.2f, 最低保证金=%.1f%%",
                    scene_name,
                    result.get("passed", False),
                    result.get("max_drawdown_pct", 0),
                    result.get("sharpe", 0),
                    result.get("min_margin_coverage_pct", 0),
                )
            except Exception as e:
                logger.error("场景 '%s' 执行失败: %s #RECOVERY: 检查虚拟券商日志与场景参数", scene_name, str(e))
                failed_scenarios.append(scene_name)
                warnings.append(f"场景 '{scene_name}' 执行失败: {str(e)}")

        portfolio_analysis = self._analyze_portfolio(portfolio_equity_series, strategy_thresholds)

        report = {
            "test_id": test_id,
            "timestamp": time.time(),
            "strategy_ids": strategy_ids,
            "mode": mode,
            "total_scenarios": total_scenarios,
            "passed_scenarios": len(test_results),
            "failed_scenarios": failed_scenarios,
            "results": test_results,
            "portfolio_analysis": portfolio_analysis,
            "survival_summary": self._calculate_survival_summary(test_results),
            "metadata": {
                "code_version": code_version or "unknown",
                "config_hash": config_hash or "unknown",
                "execution_order_randomized": randomize_order,
                "actual_execution_order": actual_order,
            },
        }

        if summary_only and "results" in report:
            for result in report["results"]:
                if "portfolio_equity" in result and len(result["portfolio_equity"]) > self.DEFAULT_EQUITY_DOWNSAMPLE_MAX:
                    step = len(result["portfolio_equity"]) // self.DEFAULT_EQUITY_DOWNSAMPLE_MAX
                    result["portfolio_equity_downsampled"] = result["portfolio_equity"][::step]
                    del result["portfolio_equity"]

        with self._lock:
            self._recent_reports.append(report)

        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="stress_test_completed",
                    details={
                        "test_id": test_id,
                        "total_scenarios": total_scenarios,
                        "passed": len(test_results),
                        "failed": len(failed_scenarios),
                    },
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

        return {
            "status": "ok",
            "reason": f"压力测试完成: {len(test_results)}/{total_scenarios} 场景通过",
            "data": report,
            "warnings": warnings,
        }

    def get_test_status(self) -> Dict[str, Any]:
        with self._progress_lock:
            progress = copy.deepcopy(self._progress)
        if not self._is_running:
            return {
                "status": "ok",
                "reason": "当前无运行中的压力测试",
                "data": {"running": False},
                "warnings": [],
            }
        return {
            "status": "ok",
            "reason": f"测试进行中: {progress.get('completed', 0)}/{progress.get('total', 0)}",
            "data": {"running": True, **progress},
            "warnings": [],
        }

    def get_latest_report(self, summary_only: bool = True) -> Dict[str, Any]:
        with self._lock:
            if not self._recent_reports:
                return {
                    "status": "error",
                    "reason": "尚无压力测试报告",
                    "data": {},
                    "warnings": ["no_report_available"],
                }
            latest = copy.deepcopy(self._recent_reports[-1])

        if summary_only and "results" in latest:
            for result in latest["results"]:
                if "portfolio_equity" in result and len(result["portfolio_equity"]) > self.DEFAULT_EQUITY_DOWNSAMPLE_MAX:
                    step = len(result["portfolio_equity"]) // self.DEFAULT_EQUITY_DOWNSAMPLE_MAX
                    result["portfolio_equity_downsampled"] = result["portfolio_equity"][::step]
                    del result["portfolio_equity"]

        return {
            "status": "ok",
            "reason": "返回最近一次压力测试报告",
            "data": latest,
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        try:
            dependencies_ok = {
                "virtual_broker": self._virtual_broker is not None,
                "event_replay_engine": self._event_replay_engine is not None,
                "order_manager": self._order_manager is not None,
                "circuit_breaker": self._circuit_breaker is not None,
                "behavioral_logger": self._behavioral_logger is not None,
            }

            broker_alive = False
            if self._virtual_broker is not None:
                try:
                    logger.debug("[SMOKE_TEST_START] 虚拟券商冒烟测试开始")
                    # 使用最小合法参数防止触发断言
                    min_params = {"volatility_multiplier": 1.0, "duration_seconds": 1}
                    if hasattr(self._virtual_broker, 'ping') and callable(self._virtual_broker.ping):
                        broker_alive = self._virtual_broker.ping()
                    else:
                        test_session = self._virtual_broker.create_synthetic_session(min_params, {})
                        test_session.close()
                        broker_alive = True
                    logger.debug("[SMOKE_TEST_END] 虚拟券商冒烟测试结束")
                except Exception as e:
                    logger.warning("虚拟券商冒烟测试失败: %s", str(e))

            return {
                "status": "ok" if broker_alive or not dependencies_ok["virtual_broker"] else "degraded",
                "reason": f"StressTestRunner 正常，场景缓存 {len(self._scenarios)} 个，虚拟券商存活={broker_alive}",
                "data": {
                    "scenario_count": len(self._scenarios),
                    "report_count": len(self._recent_reports),
                    "dependencies": dependencies_ok,
                    "virtual_broker_alive": broker_alive,
                    "is_running": self._is_running,
                },
                "warnings": [] if broker_alive else ["virtual_broker_unreachable"],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查对象完整性")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"]}

    # ========== 私有方法 ==========
    def _validate_scenario_params(self, params: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
        validated = {}
        warnings = []
        for key, default in self.SCENARIO_SCHEMA.items():
            value = params.get(key, default)
            if key == "volatility_multiplier":
                if not isinstance(value, (int, float)) or value < 1.0:
                    warnings.append(f"volatility_multiplier 非法({value})，已修正为 {default}")
                    value = default
                validated[key] = float(value)
            elif key == "depth_decay_ratio":
                if not isinstance(value, (int, float)) or value < 0.0 or value > 1.0:
                    warnings.append(f"depth_decay_ratio 非法({value})，已修正为 {default}")
                    value = default
                validated[key] = float(value)
            elif key == "spread_multiplier":
                if not isinstance(value, (int, float)) or value < 1.0:
                    warnings.append(f"spread_multiplier 非法({value})，已修正为 {default}")
                    value = default
                validated[key] = float(value)
            elif key == "duration_seconds":
                if not isinstance(value, (int, float)) or value < 1:
                    warnings.append(f"duration_seconds 非法({value})，已修正为 {default}")
                    value = default
                validated[key] = int(value)
            elif key == "stages":
                if value is not None:
                    if not isinstance(value, list):
                        warnings.append("stages 必须是列表类型，已忽略")
                        value = None
                    else:
                        for i, stage in enumerate(value):
                            if not isinstance(stage, dict):
                                warnings.append(f"stages[{i}] 格式无效，已忽略整个 stages")
                                value = None
                                break
                validated[key] = value
            elif key in ("warmup_seconds", "historical_warmup_seconds"):
                if not isinstance(value, (int, float)) or value < 0:
                    warnings.append(f"{key} 非法({value})，已修正为 {default}")
                    value = default
                validated[key] = int(value)
            elif key == "recovery_phase":
                validated[key] = value
            else:
                validated[key] = value

        for key in params:
            if key not in self.SCENARIO_SCHEMA and key != "name" and key != "type":
                warnings.append(f"未知场景参数 '{key}'，将保留但可能不被使用")
                validated[key] = params[key]

        validated["name"] = str(params.get("name", "unnamed_scenario"))
        return validated, warnings

    def _capture_position_snapshot(self, strategy_ids: List[str]) -> Dict[str, Any]:
        if self._order_manager is None:
            return {}
        try:
            if not hasattr(self._order_manager, 'get_strategy_positions'):
                logger.warning("OrderManager 缺少 get_strategy_positions 方法")
                return {}
            snapshots = {}
            for sid in strategy_ids:
                try:
                    result = self._order_manager.get_strategy_positions(sid)
                    if isinstance(result, dict):
                        snapshots[sid] = result
                    else:
                        logger.warning("策略 %s 持仓快照返回格式无效，使用空快照", sid)
                        snapshots[sid] = {}
                except Exception as e:
                    logger.warning("获取策略 %s 持仓快照失败: %s，使用空快照", sid, str(e))
                    snapshots[sid] = {}
            return snapshots
        except Exception as e:
            logger.warning("整体获取持仓快照异常: %s，返回空快照", str(e))
            return {}

    def _load_strategy_thresholds(self, strategy_ids: List[str]) -> Dict[str, Dict[str, float]]:
        thresholds = {}
        for sid in strategy_ids:
            strategy_type = self._get_strategy_type(sid)
            base = copy.deepcopy(self.SURVIVAL_THRESHOLDS)
            if strategy_type in self.STRATEGY_TYPE_THRESHOLDS:
                for key, value in self.STRATEGY_TYPE_THRESHOLDS[strategy_type].items():
                    base[key] = value
            thresholds[sid] = base
        return thresholds

    def _get_strategy_type(self, strategy_id: str) -> str:
        if self._order_manager is not None and hasattr(self._order_manager, 'get_strategy_meta'):
            try:
                meta = self._order_manager.get_strategy_meta(strategy_id)
                return meta.get("type", "unknown")
            except Exception:
                pass
        return "unknown"

    def _get_strategy_code_version(self, strategy_ids: List[str]) -> str:
        if self._order_manager is not None and hasattr(self._order_manager, 'get_strategy_code_version'):
            try:
                versions = [self._order_manager.get_strategy_code_version(sid) for sid in strategy_ids]
                return ",".join(versions)
            except Exception:
                pass
        return "unknown"

    def _get_strategy_config_hash(self, strategy_ids: List[str]) -> str:
        if self._order_manager is not None and hasattr(self._order_manager, 'get_strategy_config_hash'):
            try:
                hashes = [self._order_manager.get_strategy_config_hash(sid) for sid in strategy_ids]
                return ",".join(hashes)
            except Exception:
                pass
        return "unknown"

    def _run_synthetic_scenario(
        self,
        name: str,
        scenario: Dict[str, Any],
        strategy_ids: List[str],
        position_snapshot: Dict[str, Any],
        strategy_thresholds: Dict[str, Dict[str, float]],
        mode: str,
    ) -> Dict[str, Any]:
        stages = scenario.get("stages")
        if stages is not None and len(stages) > 0:
            if not hasattr(self._virtual_broker, 'supports_multi_stage') or not self._virtual_broker.supports_multi_stage():
                logger.warning("场景 '%s' 包含多阶段定义但虚拟券商不支持，场景标记为未通过", name)
                return {
                    "scenario_name": name,
                    "scenario_type": "synthetic",
                    "passed": False,
                    "max_drawdown_pct": 0.0,
                    "sharpe": 0.0,
                    "min_margin_coverage_pct": 0.0,
                    "failure_reasons": ["multi_stage_not_supported"],
                    "critical_moments": {},
                    "simulated_duration_sec": 0.0,
                    "portfolio_equity": [],
                    "per_strategy_results": {},
                }

        market_params = {
            "volatility_multiplier": scenario.get("volatility_multiplier", 3.0),
            "depth_decay_ratio": scenario.get("depth_decay_ratio", 0.8),
            "spread_multiplier": scenario.get("spread_multiplier", 2.0),
            "stages": stages,
        }
        return self._run_scenario_common(
            name, market_params, strategy_ids, position_snapshot, strategy_thresholds, mode, "synthetic", scenario
        )

    def _run_historical_scenario(
        self,
        name: str,
        scenario: Dict[str, Any],
        strategy_ids: List[str],
        position_snapshot: Dict[str, Any],
        strategy_thresholds: Dict[str, Dict[str, float]],
    ) -> Dict[str, Any]:
        market_params = scenario.get("data", {})
        return self._run_scenario_common(
            name, market_params, strategy_ids, position_snapshot, strategy_thresholds, "single", "historical", scenario
        )

    def _run_scenario_common(
        self,
        name: str,
        market_params: Dict[str, Any],
        strategy_ids: List[str],
        position_snapshot: Dict[str, Any],
        strategy_thresholds: Dict[str, Dict[str, float]],
        mode: str,
        scenario_type: str,
        full_scenario: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        start_time = time.time()
        session = None
        result = {
            "scenario_name": name,
            "scenario_type": scenario_type,
            "passed": False,
            "max_drawdown_pct": 0.0,
            "sharpe": 0.0,
            "min_margin_coverage_pct": 0.0,
            "recovery_sharpe": None,
            "failure_reasons": [],
            "critical_moments": {},
            "simulated_duration_sec": 0.0,
            "portfolio_equity": [],
            "per_strategy_results": {},
        }

        logger.info("[STRESS_TEST:%s] 场景 '%s' 开始执行，参数: %s", self._current_test_id or "?", name, market_params)

        stop_event = threading.Event()
        result_container = {"result": result, "error": None, "completed": False}
        result_lock = threading.Lock()

        def _run_in_thread():
            nonlocal session
            try:
                warmup = full_scenario.get("historical_warmup_seconds", 60) if full_scenario else 60
                session = self._virtual_broker.create_synthetic_session(
                    market_params, position_snapshot, warmup_seconds=warmup
                )
                local_result = self._simulate_session(
                    session, strategy_ids, strategy_thresholds, mode, start_time, full_scenario, stop_event
                )
                with result_lock:
                    result_container["result"] = local_result
                    result_container["completed"] = True
            except Exception as e:
                with result_lock:
                    result_container["error"] = str(e)
            finally:
                if session is not None:
                    try:
                        session.close()
                    except Exception as close_err:
                        logger.warning("会话关闭异常: %s (原始: %s)", str(close_err),
                                       result_container.get("error", "未知"))

        thread = threading.Thread(target=_run_in_thread, daemon=True)
        thread.start()
        thread.join(timeout=self.DEFAULT_TIMEOUT_SECONDS_PER_SCENARIO)

        if thread.is_alive():
            logger.error("[STRESS_TEST:%s] 场景 '%s' 执行超时 (%.1fs)，强制终止",
                         self._current_test_id or "?", name, time.time() - start_time)
            stop_event.set()  # 通知子线程停止
            result["failure_reasons"].append("timeout")
            result["simulated_duration_sec"] = time.time() - start_time
            return result

        with result_lock:
            if result_container["error"]:
                logger.error("[STRESS_TEST:%s] 场景 '%s' 执行异常: %s",
                             self._current_test_id or "?", name, result_container["error"])
                result["failure_reasons"].append(f"runtime_error: {result_container['error']}")
                return result
            return result_container["result"]

    def _simulate_session(
        self,
        session: Any,
        strategy_ids: List[str],
        strategy_thresholds: Dict[str, Dict[str, float]],
        mode: str,
        start_time: float,
        full_scenario: Optional[Dict[str, Any]],
        stop_event: threading.Event,
    ) -> Dict[str, Any]:
        max_drawdown = 0.0
        min_margin_coverage = float("inf")
        equity_series: List[float] = []
        margin_series: List[float] = []
        per_strategy_equity: Dict[str, List[float]] = {sid: [] for sid in strategy_ids}

        worst_drawdown_time = 0.0
        worst_margin_time = 0.0
        worst_drawdown_margin = 0.0
        worst_margin_drawdown = 0.0

        warmup_seconds = full_scenario.get("warmup_seconds", self.DEFAULT_WARMUP_SECONDS) if full_scenario else self.DEFAULT_WARMUP_SECONDS
        recovery_config = full_scenario.get("recovery_phase") if full_scenario else None
        in_recovery = False
        recovery_equity_series: List[float] = []
        stress_equity_series: List[float] = []

        last_circuit_check = 0.0
        stagnant_ticks = 0
        last_equity = None

        scenario_type = full_scenario.get("type", "synthetic") if full_scenario else "synthetic"
        if scenario_type == "historical":
            pressure_duration = full_scenario.get("duration_seconds", 3600) if full_scenario else 3600
        else:
            pressure_duration = full_scenario.get("duration_seconds", 60) if full_scenario else 60

        while not stop_event.is_set():
            tick_result = session.tick()
            # 检查 tick 返回的错误状态
            if tick_result.get("error"):
                logger.error("[STRESS_TEST:%s] 虚拟券商 tick 返回错误: %s",
                             self._current_test_id or "?", tick_result["error"])
                break

            if tick_result.get("finished", False):
                break

            current_time = time.time() - start_time
            equity = tick_result.get("equity", 0.0)
            margin = tick_result.get("margin_coverage_pct", 0.0)

            # 停滞检测
            if equity == last_equity:
                stagnant_ticks += 1
                if stagnant_ticks >= self.DEFAULT_STAGNATION_TICKS:
                    logger.error("[STRESS_TEST:%s] 虚拟券商疑似卡死，连续 %d 个 tick 无变化，强制退出",
                                 self._current_test_id or "?", stagnant_ticks)
                    break
            else:
                stagnant_ticks = 0
                last_equity = equity

            if current_time < warmup_seconds:
                continue

            if recovery_config is not None and not in_recovery:
                if current_time >= pressure_duration:
                    in_recovery = True
                    if recovery_config.get("normalize_market", True) and hasattr(session, 'normalize_market'):
                        session.normalize_market()

            if in_recovery:
                recovery_equity_series.append(equity)
            else:
                stress_equity_series.append(equity)

            equity_series.append(equity)
            margin_series.append(margin)

            if len(equity_series) > 1:
                peak = max(equity_series)
                dd = (peak - equity) / peak * 100 if peak > 0 else 0
                if dd > max_drawdown:
                    max_drawdown = dd
                    worst_drawdown_time = current_time
                    worst_drawdown_margin = margin
                if margin < min_margin_coverage:
                    min_margin_coverage = margin
                    worst_margin_time = current_time
                    worst_margin_drawdown = dd

            if "per_strategy_equity" in tick_result:
                for sid in strategy_ids:
                    per_strategy_equity[sid].append(tick_result["per_strategy_equity"].get(sid, 0.0))

            if self._circuit_breaker is not None and (current_time - last_circuit_check) > self.DEFAULT_CIRCUIT_BREAKER_CHECK_INTERVAL:
                if hasattr(self._circuit_breaker, 'is_active') and self._circuit_breaker.is_active():
                    logger.error("[STRESS_TEST:%s] 压力测试期间实盘熔断触发，场景 '%s' 被中断",
                                 self._current_test_id or "?", full_scenario.get("name", "") if full_scenario else "")
                    break
                last_circuit_check = current_time

        aborted_by_circuit = False
        if self._circuit_breaker is not None and hasattr(self._circuit_breaker, 'is_active') and self._circuit_breaker.is_active():
            aborted_by_circuit = True

        stress_eq = stress_equity_series if stress_equity_series else equity_series
        returns = np.diff(stress_eq) / (np.array(stress_eq[:-1]) + 1e-10) if len(stress_eq) > 1 else []
        sharpe = float(np.mean(returns) / (np.std(returns) + 1e-10) * np.sqrt(252)) if len(returns) > 1 else 0.0

        recovery_sharpe = None
        if recovery_equity_series and len(recovery_equity_series) > 1:
            rec_returns = np.diff(recovery_equity_series) / (np.array(recovery_equity_series[:-1]) + 1e-10)
            recovery_sharpe = float(np.mean(rec_returns) / (np.std(rec_returns) + 1e-10) * np.sqrt(252))

        failure_reasons = []
        threshold = self._get_combined_threshold(strategy_thresholds)
        if max_drawdown > threshold["max_drawdown_pct"]:
            failure_reasons.append(f"max_drawdown_exceeded: {max_drawdown:.1f}% > {threshold['max_drawdown_pct']}%")
        if sharpe < threshold["min_sharpe"]:
            failure_reasons.append(f"sharpe_too_low: {sharpe:.2f} < {threshold['min_sharpe']}")
        if min_margin_coverage < threshold["min_margin_coverage_pct"] and min_margin_coverage != float("inf"):
            failure_reasons.append(
                f"margin_coverage_low: {min_margin_coverage:.1f}% < {threshold['min_margin_coverage_pct']}%"
            )
        if recovery_sharpe is not None and recovery_sharpe < threshold.get("recovery_min_sharpe", 0.0):
            failure_reasons.append(
                f"recovery_sharpe_low: {recovery_sharpe:.2f} < {threshold['recovery_min_sharpe']}"
            )
        if aborted_by_circuit:
            failure_reasons.append("aborted_due_to_live_circuit_breaker")

        passed = len(failure_reasons) == 0

        return {
            "scenario_name": "",
            "scenario_type": "",
            "passed": passed,
            "max_drawdown_pct": round(max_drawdown, 2),
            "sharpe": round(sharpe, 2),
            "recovery_sharpe": round(recovery_sharpe, 2) if recovery_sharpe is not None else None,
            "min_margin_coverage_pct": round(min_margin_coverage, 2) if min_margin_coverage != float("inf") else 0.0,
            "failure_reasons": failure_reasons,
            "critical_moments": {
                "worst_drawdown_time_sec": round(worst_drawdown_time, 1),
                "worst_drawdown_value_pct": round(max_drawdown, 2),
                "worst_drawdown_margin_pct": round(worst_drawdown_margin, 2),
                "worst_margin_time_sec": round(worst_margin_time, 1),
                "worst_margin_value_pct": round(min_margin_coverage, 2),
                "worst_margin_drawdown_pct": round(worst_margin_drawdown, 2),
            },
            "simulated_duration_sec": round(time.time() - start_time, 1),
            "portfolio_equity": equity_series,
            "per_strategy_results": {},
        }

    def _get_combined_threshold(self, strategy_thresholds: Dict[str, Dict[str, float]]) -> Dict[str, float]:
        combined = {}
        agg: Dict[str, List[float]] = {key: [] for key in self.CONSERVATIVE_DIRECTION}
        for t in strategy_thresholds.values():
            for key, val in t.items():
                if key in agg:
                    agg[key].append(val)
                else:
                    agg[key] = [val]

        for key, direction in self.CONSERVATIVE_DIRECTION.items():
            if key in agg and agg[key]:
                if direction == "min":
                    combined[key] = min(agg[key])
                elif direction == "max":
                    combined[key] = max(agg[key])
                else:
                    combined[key] = agg[key][0]
            else:
                combined[key] = self.SURVIVAL_THRESHOLDS.get(key, 0.0)
        return combined

    def _analyze_portfolio(self, equity_series: List[float], strategy_thresholds: Dict[str, Dict[str, float]]) -> Dict[str, Any]:
        if len(equity_series) < 2:
            return {"total_points": len(equity_series), "max_drawdown_pct": 0.0, "sharpe": 0.0}
        peak = equity_series[0]
        max_dd = 0.0
        for v in equity_series:
            peak = max(peak, v)
            dd = (peak - v) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)
        returns = np.diff(equity_series) / (np.array(equity_series[:-1]) + 1e-10)
        sharpe = float(np.mean(returns) / (np.std(returns) + 1e-10) * np.sqrt(252)) if len(returns) > 1 else 0.0
        return {
            "total_points": len(equity_series),
            "max_drawdown_pct": round(max_dd, 2),
            "sharpe": round(sharpe, 2),
        }

    def _calculate_survival_summary(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not results:
            return {"total": 0, "passed": 0, "survival_rate_pct": 0.0}
        passed = sum(1 for r in results if r.get("passed", False))
        return {
            "total": len(results),
            "passed": passed,
            "survival_rate_pct": round(passed / len(results) * 100, 1),
        }

    def _reset_progress(self, test_id: str) -> None:
        with self._progress_lock:
            self._progress = {
                "test_id": test_id,
                "started_at": time.time(),
                "completed": 0,
                "total": 0,
                "current_scenario": "",
            }

    def _update_progress(self, completed: int, total: int, current_scenario: str) -> None:
        with self._progress_lock:
            now = time.time()
            if completed < total and (now - self._progress.get("_last_update", 0) < self.DEFAULT_PROGRESS_UPDATE_INTERVAL):
                return
            self._progress["completed"] = completed
            self._progress["total"] = total
            self._progress["current_scenario"] = current_scenario
            self._progress["_last_update"] = now
        logger.debug("压力测试进度: %d/%d - %s", completed, total, current_scenario)
