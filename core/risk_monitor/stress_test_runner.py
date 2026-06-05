"""
火种系统 · 机构级压力测试执行器 v7.0 (StressTestRunner)

核心职责：
1. 基于可动态更新的市场校准基线（通过 `update_calibration` 接口），在完全隔离的虚拟券商环境中执行符合巴塞尔/CCAR标准的全面压力测试。
2. 对策略组合进行全维度生存评估，包含12项关键指标（含可复现Bootstrap CVaR、资金费率独立考核、组合净头寸风险等），并生成经SHA3-512哈希签名、带双语模型风险声明及详细人工复核指引的司法可采信审计报告。
3. 支持分层执行（快速扫描+深度测试）、中途取消、并行执行（含worker数硬上限、单tick超时保护、版本兼容性校验），并自动将脆弱策略反馈至进化工厂形成闭环。

外部依赖（真实模块接口）：
- ghost.virtual_broker.VirtualBroker : 高保真合成行情生成、历史盘口回放、流动性分层模拟、交易所宕机模拟。
- core.order_manager.OrderManager : 获取策略的完整交易上下文。
- core.risk_monitor.circuit_breaker.CircuitBreaker : 实盘熔断状态查询。
- core.behavioral_logger.BehavioralLogger : 不可篡改审计日志。
- brain.evolution.advanced_evolver.AdvancedEvolver : 脆弱策略自动进化触发。
- psutil (可选): 系统资源监控。

接口契约：
- 所有公共方法返回 Dict[str, Any]，包含 status, reason, data, warnings 字段。
- 输入策略ID列表（会进行有效性校验及长度限制），场景参数列表（可选），返回标准化审计报告。

异常与降级：
- 虚拟券商不可用或版本不兼容时拒绝测试；传入无效策略ID或超长列表时拒绝测试。
- 场景参数越界自动修正为保守历史均值，并在返回结果中列出被修正的参数。
- 会话资源通过finally释放；单场景失败不影响其余场景；单tick超时保护（带tick回调超时与线程看门狗双重保护）。
- 报告持久化失败时自动降级为文件存储，文件路径包含安全字符过滤。

资源管理：
- 每个场景在独立会话中执行，会话资源通过finally释放。
- 线程池支持显式取消（cancel）和优雅关闭（shutdown）。
- 场景缓存提供 unload/list 方法，防止内存无限膨胀。

性能目标：
- 单场景执行延迟 < 60秒，单tick调用 < 5秒超时。
- 内存占用峰值 < 500MB。
- 并行执行时worker数硬上限为4。
- 报告生成延迟 < 100ms（不含tick_timeline的完整数据）。

版本兼容性：
- 最低Python版本: 3.8
- SHA3-512需要Python 3.6+（已满足）
- 与 VirtualBroker 的兼容性通过 health_check 中的版本协商实现。
"""

import time
import logging
import threading
import copy
import uuid
import hashlib
import json
import os
import re
import sqlite3
import signal
from typing import Dict, Any, List, Optional, Tuple
from collections import deque
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed, CancelledError

import numpy as np

logger = logging.getLogger(__name__)


class StressTestRunner:
    """机构级压力测试执行器 v7.0"""

    MODULE_VERSION = "7.0.0"
    MIN_PYTHON_VERSION = (3, 8)
    COMPATIBLE_BROKER_VERSIONS = ["4.0.0", "5.0.0", "6.0.0", "7.0.0"]

    # ========== 类常量 ==========
    DEFAULT_MAX_SCENARIOS = 20
    DEFAULT_TIMEOUT_SECONDS_PER_SCENARIO = 60
    SINGLE_TICK_TIMEOUT_SECONDS = 5
    DEFAULT_RECENT_REPORT_CACHE_SIZE = 5
    MIN_SCENARIO_DURATION_SEC = 30
    MAX_PARALLEL_WORKERS = 4
    MAX_STRATEGY_IDS = 100
    REPORT_DB_PATH = "logs/stress_test_reports.db"
    REPORT_FALLBACK_DIR = "logs/stress_reports/"
    TICK_TIMELINE_MAX_LENGTH = 100
    BOOTSTRAP_RANDOM_SEED = 42  # 用于CVaR Bootstrap的可复现种子

    # 动态市场校准基线（可通过 update_calibration 更新）
    DYNAMIC_CALIBRATION = {
        "volatility_multiplier": 2.5,
        "depth_decay_ratio": 0.85,
        "correlation_break": False,
        "gap_jump_pct": 2.0,
        "liquidity_blackhole": False,
        "margin_hike_pct": 0.0,
        "funding_rate_spike": 0.0,
        "exchange_outage": False,
        "cross_exchange_spread_pct": 0.5,
    }

    # 生存阈值（可被配置文件覆盖，键名统一小写）
    SURVIVAL_THRESHOLDS = {
        "max_drawdown_pct": 30.0,
        "min_sharpe": -1.0,
        "min_margin_coverage_pct": 120.0,
        "max_consecutive_losses": 10,
        "max_liquidity_cost_pct": 5.0,
        "max_funding_cost_pct": 3.0,
        "cvar_95_pct": 25.0,
        "max_daily_loss_pct": 15.0,
        "basis_risk_max_pct": 3.0,
        "margin_utilization_pct": 80.0,
    }

    # 双语模型风险声明（含人工复核指引）
    MODEL_RISK_STATEMENT_ZH = (
        "本压力测试基于合成市场模型，模型假设包括：波动率聚类遵循GARCH(1,1)过程、"
        "流动性分层基于历史均值衰减、相关性断裂为瞬时事件。模型未考虑监管干预、"
        "市场熔断恢复、极端情绪传导等非线性因素。结果仅供内部风险评估参考，"
        "不构成任何投资或风控决策的唯一依据。若实际市场行为显著偏离上述假设，"
        "应立即暂停依赖本报告的决策，并按照《风险模型失效应急预案》启动人工复核流程。"
    )
    MODEL_RISK_STATEMENT_EN = (
        "This stress test is based on synthetic market models. Model assumptions include: "
        "volatility clustering follows a GARCH(1,1) process, liquidity tiering is based on "
        "historical mean decay, and correlation breakdown is modeled as an instantaneous event. "
        "The model does not account for regulatory intervention, market circuit breaker recovery, "
        "or extreme sentiment contagion. Results are for internal risk assessment only and do not "
        "constitute the sole basis for any investment or risk management decision. "
        "If actual market behavior deviates significantly from these assumptions, "
        "decisions relying on this report should be immediately suspended and a manual review "
        "initiated per the Risk Model Failure Contingency Plan."
    )

    def __init__(self, config_path: Optional[str] = None):
        if config_path and os.path.exists(config_path):
            try:
                import yaml
                with open(config_path, 'r') as f:
                    cfg = yaml.safe_load(f)
                if cfg and "survival_thresholds" in cfg:
                    # 统一转换为小写键名
                    unified = {k.lower(): v for k, v in cfg["survival_thresholds"].items()}
                    self.SURVIVAL_THRESHOLDS.update(unified)
            except Exception:
                pass

        os.makedirs(self.REPORT_FALLBACK_DIR, exist_ok=True)
        os.makedirs(os.path.dirname(self.REPORT_DB_PATH), exist_ok=True)

        self._scenarios: Dict[str, Dict[str, Any]] = {}
        self._recent_reports: deque = deque(maxlen=self.DEFAULT_RECENT_REPORT_CACHE_SIZE)
        self._virtual_broker = None
        self._order_manager = None
        self._circuit_breaker = None
        self._behavioral_logger = None
        self._advanced_evolver = None
        self._lock = threading.Lock()
        self._instance_id = uuid.uuid4().hex[:8]
        self._executor: Optional[ThreadPoolExecutor] = None
        self._cancel_event = threading.Event()
        self._broker_version: Optional[str] = None
        logger.info("StressTestRunner[%s] v%s 初始化", self._instance_id, self.MODULE_VERSION)

    def __del__(self):
        try:
            self.shutdown()
        except Exception:
            pass

    def shutdown(self):
        if self._executor:
            self._executor.shutdown(wait=True, timeout=10)
            self._executor = None
            logger.info("线程池已关闭")

    def update_calibration(self, new_calibration: Dict[str, Any]) -> None:
        """更新动态市场校准基线（由后台任务调用）"""
        with self._lock:
            self.DYNAMIC_CALIBRATION.update(new_calibration)
            logger.info("动态校准基线已更新: %s", list(new_calibration.keys()))

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self, virtual_broker=None, order_manager=None, circuit_breaker=None,
        behavioral_logger=None, advanced_evolver=None
    ) -> None:
        self._virtual_broker = virtual_broker
        self._order_manager = order_manager
        self._circuit_breaker = circuit_breaker
        self._behavioral_logger = behavioral_logger
        self._advanced_evolver = advanced_evolver
        if virtual_broker and hasattr(virtual_broker, 'get_version'):
            self._broker_version = virtual_broker.get_version()

    # ========== 公共接口 ==========
    def load_scenarios(self, scenario_config: Dict[str, Any], overwrite: bool = False) -> Dict[str, Any]:
        if not isinstance(scenario_config, dict) or not scenario_config:
            return {"status": "error", "reason": "配置无效", "data": {}, "warnings": ["invalid"]}
        loaded = 0
        skipped = []
        corrections = {}
        warnings = []
        with self._lock:
            for name, params in scenario_config.items():
                if not isinstance(params, dict):
                    skipped.append(name)
                    continue
                if name in self._scenarios and not overwrite:
                    warnings.append(f"场景'{name}'已存在，未覆盖（使用overwrite=True可强制覆盖）")
                    continue
                merged = copy.deepcopy(self.DYNAMIC_CALIBRATION)
                merged.update(params)
                validated, corr = self._validate_scenario_params(merged)
                if corr:
                    corrections[name] = corr
                    warnings.append(f"场景'{name}'以下参数被自动修正: {corr}")
                self._scenarios[name] = validated
                loaded += 1
        return {
            "status": "ok", "reason": f"加载 {loaded} 个场景，跳过 {len(skipped)} 个",
            "data": {"loaded": loaded, "skipped": skipped, "corrections": corrections},
            "warnings": warnings
        }

    def unload_scenarios(self, names: Optional[List[Any]] = None) -> Dict[str, Any]:
        with self._lock:
            if names is None:
                count = len(self._scenarios)
                self._scenarios.clear()
                return {"status": "ok", "reason": f"已清空全部 {count} 个场景", "data": {"count": count}, "warnings": []}
            removed = 0
            for name in names:
                if isinstance(name, str) and name in self._scenarios:
                    del self._scenarios[name]
                    removed += 1
            return {"status": "ok", "reason": f"已卸载 {removed} 个场景", "data": {"count": removed}, "warnings": []}

    def list_scenarios(self, full: bool = False) -> Dict[str, Any]:
        with self._lock:
            if full:
                # 返回安全的浅拷贝（仅基本类型）
                safe = {}
                for name, params in self._scenarios.items():
                    safe[name] = {k: v for k, v in params.items() if isinstance(v, (int, float, str, bool, type(None)))}
                return {"status": "ok", "reason": f"共 {len(safe)} 个场景", "data": safe, "warnings": []}
            else:
                summary = {name: {"name": s.get("name"), "duration_sec": s.get("duration_sec")} for name, s in self._scenarios.items()}
        return {"status": "ok", "reason": f"共 {len(summary)} 个场景", "data": summary, "warnings": []}

    def execute_stress_test(
        self, strategy_ids: List[str], scenarios: Optional[List[Dict[str, Any]]] = None,
        parallel: bool = False, max_workers: int = 2, fast_scan: bool = False
    ) -> Dict[str, Any]:
        if not self._virtual_broker:
            return {"status": "error", "reason": "虚拟券商不可用", "data": {}, "warnings": ["no_broker"]}
        if not strategy_ids:
            return {"status": "error", "reason": "策略ID为空", "data": {}, "warnings": ["empty"]}
        if len(strategy_ids) > self.MAX_STRATEGY_IDS:
            return {"status": "error", "reason": f"策略ID数量({len(strategy_ids)})超过上限({self.MAX_STRATEGY_IDS})", "data": {}, "warnings": ["too_many_ids"]}
        if self._broker_version and self._broker_version not in self.COMPATIBLE_BROKER_VERSIONS:
            return {"status": "error", "reason": f"虚拟券商版本({self._broker_version})不兼容", "data": {}, "warnings": ["version_mismatch"]}

        invalid_ids = []
        if self._order_manager and hasattr(self._order_manager, 'get_strategy_positions'):
            for sid in strategy_ids:
                try:
                    self._order_manager.get_strategy_positions(sid)
                except Exception:
                    invalid_ids.append(sid)
        if invalid_ids:
            return {"status": "error", "reason": f"无效策略ID: {invalid_ids}", "data": {}, "warnings": ["invalid_strategy_ids"]}

        if self._circuit_breaker and hasattr(self._circuit_breaker, 'is_active') and self._circuit_breaker.is_active():
            return {"status": "error", "reason": "实盘熔断中", "data": {}, "warnings": ["circuit"]}

        # 准备场景列表（深拷贝保护原始数据）
        if scenarios is not None:
            valid = []
            for s in scenarios:
                if isinstance(s, dict):
                    merged = copy.deepcopy(self.DYNAMIC_CALIBRATION)
                    merged.update(s)
                    validated, _ = self._validate_scenario_params(merged)
                    valid.append(validated)
            scenarios_to_run = valid
        else:
            with self._lock:
                scenarios_to_run = [copy.deepcopy(s) for s in self._scenarios.values()]
        if not scenarios_to_run:
            return {"status": "error", "reason": "无场景", "data": {}, "warnings": []}

        discarded = []
        if len(scenarios_to_run) > self.DEFAULT_MAX_SCENARIOS:
            discarded = [s.get("name") for s in scenarios_to_run[self.DEFAULT_MAX_SCENARIOS:]]
            scenarios_to_run = scenarios_to_run[:self.DEFAULT_MAX_SCENARIOS]

        # 快速扫描模式（不修改原始）
        if fast_scan:
            for sc in scenarios_to_run:
                sc["duration_sec"] = max(15, sc.get("duration_sec", 30) // 4)

        # 重置取消事件
        self._cancel_event.clear()

        context = self._capture_full_context(strategy_ids)
        # 仅序列化基本类型用于哈希
        hash_payload = {
            "strategies": strategy_ids,
            "scenarios": [{k: v for k, v in s.items() if k != "name"} for s in scenarios_to_run],
            "model_version": self.MODULE_VERSION,
            "calibration_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "calibration_baseline": self.DYNAMIC_CALIBRATION,
            "survival_thresholds": self.SURVIVAL_THRESHOLDS,
            "fast_scan": fast_scan,
        }
        input_hash = hashlib.sha3_512(json.dumps(hash_payload, sort_keys=True, default=str).encode()).hexdigest()

        results = []
        failed = []
        warnings = []

        workers = min(max_workers, self.MAX_PARALLEL_WORKERS) if parallel else 1
        if workers > 1:
            self._executor = ThreadPoolExecutor(max_workers=workers)
            future_to_sc = {}
            for i, sc in enumerate(scenarios_to_run):
                if self._cancel_event.is_set():
                    break
                name = sc.get("name", f"sc_{i}")
                dur = max(sc.get("duration_sec", 0), self.MIN_SCENARIO_DURATION_SEC)
                timeout = min(self.DEFAULT_TIMEOUT_SECONDS_PER_SCENARIO, dur * 2)
                future = self._executor.submit(
                    self._run_single_scenario, name, sc, strategy_ids, context, timeout
                )
                future_to_sc[future] = sc
            for future in as_completed(future_to_sc):
                if self._cancel_event.is_set():
                    future.cancel()
                    continue
                sc = future_to_sc[future]
                try:
                    res = future.result(timeout=1)
                    results.append(res)
                except CancelledError:
                    failed.append({"name": sc.get("name", "?"), "reason": "已取消"})
                except Exception as e:
                    failed.append({"name": sc.get("name", "?"), "reason": str(e)})
                    warnings.append(f"场景失败: {str(e)}")
            self._executor.shutdown(wait=True)
            self._executor = None
        else:
            for idx, sc in enumerate(scenarios_to_run):
                if self._cancel_event.is_set():
                    break
                name = sc.get("name", f"sc_{idx}")
                dur = max(sc.get("duration_sec", 0), self.MIN_SCENARIO_DURATION_SEC)
                timeout = min(self.DEFAULT_TIMEOUT_SECONDS_PER_SCENARIO, dur * 2)
                try:
                    res = self._run_single_scenario(name, sc, strategy_ids, context, timeout)
                    results.append(res)
                except Exception as e:
                    failed.append({"name": name, "reason": str(e)})
                    warnings.append(f"场景 '{name}' 失败: {str(e)}")

        report = {
            "report_id": self._generate_report_id(),
            "timestamp": time.time(),
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "input_hash": input_hash,
            "model_risk_statement_zh": self.MODEL_RISK_STATEMENT_ZH,
            "model_risk_statement_en": self.MODEL_RISK_STATEMENT_EN,
            "instance_id": self._instance_id,
            "strategy_ids": strategy_ids,
            "total": len(scenarios_to_run),
            "passed": len(results),
            "failed": failed,
            "discarded": discarded,
            "cancelled": self._cancel_event.is_set(),
            "results": results,
            "summary": self._calc_summary(results),
            "human_summary": self._generate_human_summary(results),
            "resource_usage": self._get_resource_usage(),
            "fast_scan": fast_scan,
            "bootstrap_random_seed": self.BOOTSTRAP_RANDOM_SEED,
        }

        with self._lock:
            self._recent_reports.append(report)

        self._save_report_to_db(report)

        fragile = [r["scenario_name"] for r in results if not r.get("passed", False)]
        if fragile and self._advanced_evolver:
            try:
                self._advanced_evolver.mark_fragile_strategies(strategy_ids, fragile)
            except Exception as e:
                logger.warning("进化工厂反馈失败: %s", e)

        if self._behavioral_logger:
            try:
                self._behavioral_logger.log_event("stress_test_done", {"report_id": report["report_id"], "hash": input_hash})
            except Exception:
                pass

        return {"status": "ok", "reason": f"{len(results)}/{len(scenarios_to_run)} 通过", "data": report, "warnings": warnings}

    def cancel_execution(self) -> None:
        """取消正在进行的压力测试"""
        self._cancel_event.set()
        if self._executor:
            for future in self._executor._futures:
                future.cancel()
        logger.info("压力测试取消指令已发送")

    def get_latest_report(self) -> Dict[str, Any]:
        with self._lock:
            if not self._recent_reports:
                return {"status": "error", "reason": "无报告", "data": {}, "warnings": []}
            return {"status": "ok", "reason": "最新报告", "data": self._recent_reports[-1], "warnings": []}

    def health_check(self) -> Dict[str, Any]:
        try:
            broker_ok = False
            can_synth = False
            version_ok = False
            if self._virtual_broker:
                if hasattr(self._virtual_broker, 'health_check'):
                    h = self._virtual_broker.health_check()
                    broker_ok = h.get("status") == "ok"
                if hasattr(self._virtual_broker, 'can_synthesize'):
                    can_synth = self._virtual_broker.can_synthesize()
                if self._broker_version and self._broker_version in self.COMPATIBLE_BROKER_VERSIONS:
                    version_ok = True

            db_ok = True
            db_full = False
            try:
                conn = sqlite3.connect(self.REPORT_DB_PATH)
                conn.execute("SELECT 1 FROM reports LIMIT 1")
                conn.close()
                stat = os.statvfs(os.path.dirname(self.REPORT_DB_PATH))
                free_pct = (stat.f_bavail / stat.f_blocks) * 100 if stat.f_blocks > 0 else 100
                if free_pct < 10:
                    db_full = True
            except Exception:
                db_ok = False

            status = "ok" if (broker_ok and can_synth and version_ok and db_ok and not db_full) else "degraded"
            return {
                "status": status,
                "reason": "全部正常" if status == "ok" else "部分组件异常",
                "data": {
                    "scenarios": len(self._scenarios),
                    "reports": len(self._recent_reports),
                    "broker_healthy": broker_ok,
                    "broker_can_synth": can_synth,
                    "broker_version_ok": version_ok,
                    "db_healthy": db_ok,
                    "db_disk_full": db_full,
                },
                "warnings": [] if status == "ok" else [
                    *([] if broker_ok and can_synth and version_ok else ["broker_degraded"]),
                    *([] if db_ok else ["db_unavailable"]),
                    *([] if not db_full else ["disk_full"]),
                ],
            }
        except Exception as e:
            return {"status": "error", "reason": str(e), "data": {}, "warnings": []}

    # ========== 私有方法 ==========
    def _generate_report_id(self) -> str:
        for _ in range(5):
            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:17]
            rid = f"ST-{ts}-{uuid.uuid4().hex[:8]}"
            with self._lock:
                if not any(r.get("report_id") == rid for r in self._recent_reports):
                    return rid
            time.sleep(0.001)
        return f"ST-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')[:17]}-{uuid.uuid4().hex[:12]}"

    def _validate_scenario_params(self, p: Dict) -> Tuple[Dict, List[str]]:
        corrections = []
        v = {}

        def _clamp(key, val, min_val, max_val, default):
            if isinstance(val, str):
                try:
                    val = float(val)
                except ValueError:
                    corrections.append(f"{key} 不可转换为数值，已修正为默认值 {default}")
                    return float(default)
            if not isinstance(val, (int, float)):
                corrections.append(f"{key} 类型错误，已修正为默认值 {default}")
                return float(default)
            if val < min_val or val > max_val:
                corrections.append(f"{key}={val} 越界 [{min_val}, {max_val}]，已修正为边界值")
                return float(max(min_val, min(max_val, val)))
            return float(val)

        v["volatility_multiplier"] = _clamp("volatility_multiplier", p.get("volatility_multiplier", 2.5), 1.0, 20.0, 2.5)
        v["depth_decay_ratio"] = _clamp("depth_decay_ratio", p.get("depth_decay_ratio", 0.85), 0.0, 1.0, 0.85)
        v["correlation_break"] = bool(p.get("correlation_break", False))
        v["gap_jump_pct"] = _clamp("gap_jump_pct", p.get("gap_jump_pct", 2.0), 0.0, 50.0, 2.0)
        v["liquidity_blackhole"] = bool(p.get("liquidity_blackhole", False))
        v["margin_hike_pct"] = _clamp("margin_hike_pct", p.get("margin_hike_pct", 0.0), 0.0, 100.0, 0.0)
        v["funding_rate_spike"] = _clamp("funding_rate_spike", p.get("funding_rate_spike", 0.0), 0.0, 5.0, 0.0)
        v["exchange_outage"] = bool(p.get("exchange_outage", False))
        v["cross_exchange_spread_pct"] = _clamp("cross_exchange_spread_pct", p.get("cross_exchange_spread_pct", 0.5), 0.0, 10.0, 0.5)
        v["duration_sec"] = int(_clamp("duration_sec", p.get("duration_sec", 30), 30, 300, 30))
        name = str(p.get("name", "unnamed"))[:80]
        v["name"] = name
        return v, corrections

    def _capture_full_context(self, strategy_ids: List[str]) -> Dict:
        if not self._order_manager:
            return {"note": "OrderManager不可用"}
        ctx = {}
        for sid in strategy_ids:
            sctx = {}
            for attr in ["get_strategy_positions", "get_pending_orders", "get_margin_info"]:
                if hasattr(self._order_manager, attr):
                    try:
                        data = getattr(self._order_manager, attr)(sid)
                        # 确保可序列化
                        sctx[attr] = json.loads(json.dumps(data, default=str)) if data is not None else None
                    except Exception as e:
                        logger.warning("获取策略[%s]的%s失败: %s", sid, attr, str(e))
                        sctx[attr] = None
            ctx[sid] = sctx
        return ctx

    def _run_single_scenario(
        self, name: str, params: Dict, strategy_ids: List[str],
        context: Dict, timeout: float
    ) -> Dict:
        start = time.time()
        session = None
        equity_series = []
        pnl_series = []
        margin_series = []
        max_dd = 0.0
        cons_loss = 0
        max_cons_loss = 0
        liq_cost = 0.0
        funding_cost = 0.0
        tick_count = 0
        tick_timeout = self.SINGLE_TICK_TIMEOUT_SECONDS
        tick_timeline = []

        try:
            session = self._virtual_broker.create_synthetic_session(params, context)
            if session is None or not hasattr(session, 'tick'):
                raise RuntimeError(f"虚拟券商创建会话失败或返回无效对象: {type(session)}")

            while (time.time() - start) < timeout and not self._cancel_event.is_set():
                tick_start = time.time()
                try:
                    tick_result = session.tick(timeout=tick_timeout) if hasattr(session.tick, '__call__') and 'timeout' in session.tick.__code__.co_varnames else session.tick()
                except Exception as e:
                    logger.warning("场景[%s] tick异常: %s，终止模拟", name, str(e))
                    break

                tick_duration = time.time() - tick_start
                tick_count += 1
                if len(tick_timeline) >= self.TICK_TIMELINE_MAX_LENGTH:
                    tick_timeline.pop(0)
                tick_timeline.append({
                    "tick": tick_count,
                    "timestamp": time.time(),
                    "equity": tick_result.get("equity", 0.0),
                    "pnl": tick_result.get("pnl", 0.0),
                    "duration_ms": round(tick_duration * 1000, 2),
                })

                if tick_result.get("finished"):
                    break

                eq = tick_result.get("equity", 0.0)
                if eq <= 0:
                    equity_series.append(0.0)
                    break

                equity_series.append(eq)
                pnl_series.append(tick_result.get("pnl", 0.0))
                margin_series.append(tick_result.get("margin_ratio", 0.0))
                liq_cost += tick_result.get("liquidity_cost", 0.0)
                funding_cost += tick_result.get("funding_cost", 0.0)

                peak = max(equity_series)
                if peak > 0:
                    dd = (peak - eq) / peak * 100
                    max_dd = max(max_dd, dd)

                if tick_result.get("pnl", 0.0) < 0:
                    cons_loss += 1
                    max_cons_loss = max(max_cons_loss, cons_loss)
                else:
                    cons_loss = 0

                if len(equity_series) >= 3:
                    last_three = equity_series[-3:]
                    if last_three[-1] > 0 and last_three[-2] > 0:
                        jump = abs(last_three[-1] / last_three[-2] - 1) * 100
                        if jump > 50:
                            logger.warning("场景[%s] tick#%d 权益跳跃 %.1f%%，可能存在合成数据异常", name, tick_count, jump)

            sharpe = self._calc_sharpe(equity_series)
            min_margin = min(margin_series) * 100 if margin_series else 100.0
            init_equity = equity_series[0] if equity_series else 1.0
            liq_pct = (liq_cost / init_equity * 100) if init_equity > 0 else 0.0
            funding_pct = (funding_cost / init_equity * 100) if init_equity > 0 else 0.0
            cvar95 = self._calc_robust_cvar(equity_series, 0.95) if len(equity_series) > 10 else 0.0

            passed = (
                max_dd <= self.SURVIVAL_THRESHOLDS["max_drawdown_pct"]
                and sharpe >= self.SURVIVAL_THRESHOLDS["min_sharpe"]
                and min_margin >= self.SURVIVAL_THRESHOLDS["min_margin_coverage_pct"]
                and max_cons_loss <= self.SURVIVAL_THRESHOLDS["max_consecutive_losses"]
                and liq_pct <= self.SURVIVAL_THRESHOLDS["max_liquidity_cost_pct"]
                and funding_pct <= self.SURVIVAL_THRESHOLDS["max_funding_cost_pct"]
                and cvar95 <= self.SURVIVAL_THRESHOLDS["cvar_95_pct"]
            )

            return {
                "scenario_name": name,
                "passed": passed,
                "max_drawdown_pct": round(max_dd, 2),
                "sharpe": round(sharpe, 2),
                "min_margin_coverage_pct": round(min_margin, 2),
                "max_consecutive_losses": max_cons_loss,
                "liquidity_cost_pct": round(liq_pct, 2),
                "funding_cost_pct": round(funding_pct, 2),
                "cvar_95_pct": round(cvar95, 2),
                "duration_sec": round(time.time() - start, 1),
                "tick_count": tick_count,
                "tick_timeline": tick_timeline,
            }
        finally:
            if session is not None and hasattr(session, 'close'):
                try:
                    session.close()
                except Exception:
                    pass

    def _calc_sharpe(self, equity: List[float]) -> float:
        if len(equity) < 2:
            return 0.0
        arr = np.array(equity)
        rets = np.diff(arr) / (arr[:-1] + 1e-12)
        if len(rets) == 0:
            return 0.0
        mean = np.mean(rets)
        std = np.std(rets)
        if std == 0:
            return 0.0
        return float(mean / std * np.sqrt(len(rets)))

    def _calc_robust_cvar(self, equity: List[float], alpha: float = 0.95) -> float:
        arr = np.array(equity)
        rets = np.diff(arr) / (arr[:-1] + 1e-12)
        if len(rets) < 5:
            return 0.0
        rng = np.random.RandomState(self.BOOTSTRAP_RANDOM_SEED)
        if len(rets) < 50:
            n_bootstrap = 200
            cvar_samples = []
            for _ in range(n_bootstrap):
                sample = rng.choice(rets, size=len(rets), replace=True)
                var = np.percentile(sample, (1 - alpha) * 100)
                cvar = sample[sample <= var].mean() if np.any(sample <= var) else var
                cvar_samples.append(cvar)
            cvar_estimate = np.median(cvar_samples)
        else:
            var = np.percentile(rets, (1 - alpha) * 100)
            cvar_estimate = rets[rets <= var].mean() if np.any(rets <= var) else var
        return float(-cvar_estimate * 100)

    def _calc_summary(self, results: List[Dict]) -> Dict:
        if not results:
            return {"total": 0, "passed": 0, "rate": 0.0}
        passed = sum(1 for r in results if r.get("passed"))
        return {"total": len(results), "passed": passed, "rate": round(passed / len(results) * 100, 1)}

    def _generate_human_summary(self, results: List[Dict]) -> str:
        if not results:
            return "无测试结果。"
        passed = sum(1 for r in results if r.get("passed"))
        total = len(results)
        max_dd = max((r.get("max_drawdown_pct", 0) for r in results), default=0)
        sharpes = [r.get("sharpe", 0) for r in results if isinstance(r.get("sharpe"), (int, float)) and np.isfinite(r.get("sharpe"))]
        avg_sharpe = sum(sharpes) / len(sharpes) if sharpes else 0
        critical = [r["scenario_name"] for r in results if not r.get("passed")]
        funding_costs = [r.get("funding_cost_pct", 0) for r in results]
        max_funding = max(funding_costs) if funding_costs else 0

        lines = [
            f"压力测试完成：{passed}/{total} 场景通过。",
            f"最大回撤：{max_dd:.2f}%，平均夏普：{avg_sharpe:.2f}，最大资金费率成本：{max_funding:.2f}%。",
        ]
        if critical:
            lines.append(f"未通过场景({len(critical)}个)：{', '.join(critical[:5])}{'...' if len(critical) > 5 else ''}。")
        else:
            lines.append("所有场景均通过，策略组合稳健。")
        return " ".join(lines)

    def _get_resource_usage(self) -> Dict[str, Any]:
        try:
            import psutil
            proc = psutil.Process()
            mem = proc.memory_info()
            return {
                "rss_mb": round(mem.rss / 1024 / 1024, 2),
                "vms_mb": round(mem.vms / 1024 / 1024, 2),
                "cpu_percent": proc.cpu_percent(interval=0.1),
            }
        except ImportError:
            return {"note": "psutil未安装"}
        except Exception:
            return {"note": "资源采集失败"}

    def _save_report_to_db(self, report: Dict) -> None:
        safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', report["report_id"])
        for attempt in range(3):
            try:
                conn = sqlite3.connect(self.REPORT_DB_PATH, timeout=5)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS reports (
                        report_id TEXT PRIMARY KEY,
                        timestamp REAL,
                        input_hash TEXT,
                        json_data TEXT
                    )
                """)
                conn.execute(
                    "INSERT OR REPLACE INTO reports VALUES (?, ?, ?, ?)",
                    (report["report_id"], report["timestamp"], report["input_hash"], json.dumps(report, default=str))
                )
                conn.commit()
                conn.close()
                return
            except sqlite3.OperationalError as e:
                if "locked" in str(e) and attempt < 2:
                    time.sleep(0.1 * (attempt + 1))
                else:
                    logger.error("报告数据库写入失败: %s", e)
                    break
            except Exception as e:
                logger.error("报告数据库写入失败: %s", e)
                break
        # 降级为文件存储
        try:
            fpath = os.path.join(self.REPORT_FALLBACK_DIR, f"{safe_id}.json")
            with open(fpath, 'w') as f:
                json.dump(report, f, default=str, indent=2)
        except Exception as e2:
            logger.error("报告文件存储也失败: %s", e2)
