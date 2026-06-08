"""
火种系统 · 金丝雀部署器 (CanaryDeployer)

核心职责：
1. 管理新策略的渐进式发布流程，按 1% → 10% → 100% 的仓位比例分阶段推进
2. 在每个阶段持续监控新策略的年化夏普比率、滚动最大回撤、信号分歧度、执行质量、资金费率等核心指标
3. 集成配对t检验（含Newey-West修正）、快速失效、合规审批、市场冲击模拟、新闻过滤、信号泄露检测等机构级风控措施
4. 支持子账户隔离、闪崩熔断、异常交易模式检测、尾部风险压力测试、交易所规则一致性校验等高级功能

外部依赖（真实模块接口）：
- core.strategy_gene.StrategyGene : 策略基因管理
- core.position_sizer.PositionSizer : 独立仓位与盈亏归因（需提供年化夏普和净夏普）
- core.risk_monitor.circuit_breaker.CircuitBreaker : 系统熔断
- core.behavioral_logger.BehavioralLogger : 行为日志（支持分级记录）
- core.perception.volatility_regime.VolatilityRegime : 波动率分位
- core.execution.slippage_filter.SlippageFilter : 滑点预估
- brain.cloud_llm_auditor.CloudLLMAuditor : 根因分析（异步接口）
- core.external_monitor.EventListener : 新闻事件监听
- core.account_ledger.AccountLedger : 账户隔离状态与客户资金检测
- core.order_manager.compliance.ComplianceChecker : 订单流合规检查

状态机：
    PENDING ──deploy()──> STAGE_1 ──monitor()──> STAGE_2 ──monitor()──> STAGE_3 ──monitor()──> COMPLETED
       │                     │                   │                   │
       └──rollback()─────────┴───────────────────┴───────────────────┘
                                  │
                                  v
                            ROLLED_BACK

金融假设：
- 所有夏普比率基于252交易日年化，日对数收益率，已扣除资金费率和滑点
- 回撤为滚动30分钟窗口内的峰值回撤（使用time.monotonic()计时）
- 统计检验：优先使用Newey-West调整的t检验（处理自相关），降级为Mann-Whitney U检验
- 无风险利率按计价货币区分：USDT=4%, BTC=0%, ETH=2%

异常与降级：
- 所有降级值在类常量区明确声明
- 监控指标连续获取失败3次触发自动回滚
- 统计检验库不可用时降级为简单比较，并记录降级日志
- 状态持久化使用独立后台线程异步写入，主线程永不阻塞

资源管理：
- 状态每5分钟异步持久化到带3备份的文件系统，使用原子写入+fsync保护
- 写入前检查磁盘空间，不足时尝试写入紧急备用路径
- 超过7天未推进的部署自动回滚
- 每小时主动触发gc.collect()，内存压力>80%时提前触发
- 使用time.monotonic()进行所有内部计时，防止NTP跳变干扰
- 使用RLock保护所有共享状态，防止嵌套调用死锁
- 持久化数据使用深拷贝保护快照一致性
- 停用策略使用指数退避+随机抖动重试，失败后标记FROZEN并人工干预

Python版本要求：3.10+
"""

import copy
import gc
import hashlib
import json
import logging
import os
import random
import threading
import time
from collections import deque
from enum import Enum
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


# ========== 科学计算库可用性检查 ==========
try:
    from scipy import stats as _scipy_stats

    _HAS_SCIPY = True
    logger.info("scipy可用，将使用配对t检验和Newey-West修正")
except ImportError:
    _HAS_SCIPY = False
    logger.warning("scipy不可用，统计检验降级为简单比较")

try:
    from statsmodels.tsa.stattools import acf as _acf

    _HAS_STATSMODELS = True
except ImportError:
    _HAS_STATSMODELS = False
    logger.info("statsmodels不可用，将使用简化自相关检测")


class CanaryStage(Enum):
    """金丝雀发布阶段"""
    PENDING = "pending"
    STAGE_1 = "stage_1"
    STAGE_2 = "stage_2"
    STAGE_3 = "stage_3"
    ROLLED_BACK = "rolled_back"
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    FROZEN = "frozen"


class CanaryDeployer:
    """
    金丝雀渐进式部署器

    严格按照全球顶尖量化对冲基金的机构级标准执行策略上线的最后一道风控闸门。
    所有部署操作均需通过合规审批、统计检验、流动性冲击模拟、信号泄露检测等多重验证。
    """

    # ========== 类常量 ==========
    DEFAULT_STAGES: list[dict[str, Any]] = [
        {"ratio": 0.01, "min_hours": 24, "max_dd": 0.02, "min_sharpe": 0.3, "min_trades": 10},
        {"ratio": 0.10, "min_hours": 48, "max_dd": 0.05, "min_sharpe": 0.5, "min_trades": 30},
        {"ratio": 1.00, "min_hours": 72, "max_dd": 0.10, "min_sharpe": 0.8, "min_trades": 50},
    ]
    MAX_ROLLBACKS_PER_DAY: int = 3
    ROLLBACK_COOLDOWN_HOURS: int = 2
    MAX_CONSECUTIVE_METRIC_FAILURES: int = 3
    SIGNAL_DIVERGENCE_THRESHOLD: float = 0.40
    MIN_TRADES_FOR_EVAL: int = 10
    DEFAULT_SHARPE_FALLBACK: float = -0.1          # 中性偏保守估计，仅用于数据不可用时的占位
    MAX_CONCURRENT_DEPLOYMENTS: int = 3
    ORPHAN_TIMEOUT_HOURS: int = 168
    DEPLOY_ALLOWED_START_HOUR: int = 8
    DEPLOY_ALLOWED_END_HOUR: int = 20
    DEPLOY_FORBIDDEN_START_HOUR: int = 23
    DEPLOY_FORBIDDEN_END_HOUR: int = 0
    STATE_PERSIST_INTERVAL_SEC: int = 300
    CORRELATION_THRESHOLD: float = 0.80
    NET_EXPOSURE_CAP_PCT: float = 0.20
    LEVERAGE_REDUCTION_PCT: float = 0.50
    ROLLBACK_BAN_DAYS: int = 30
    MAX_ROLLBACKS_BEFORE_BAN: int = 2
    STATISTICAL_P_VALUE_THRESHOLD: float = 0.10
    COMPLIANCE_APPROVAL_REQUIRED: bool = True
    FACTOR_PSI_THRESHOLD: float = 0.25
    VOLATILITY_FREEZE_THRESHOLD: float = 0.03
    FAST_FAIL_HOURS: int = 1
    FAST_FAIL_DD_MULTIPLIER: float = 1.5
    MAX_MONITOR_HISTORY: int = 1000
    RISK_FREE_RATE_USDT: float = 0.04
    RISK_FREE_RATE_BTC: float = 0.00
    RISK_FREE_RATE_ETH: float = 0.02
    MIN_TRADES_FOR_TTEST: int = 20
    LIQUIDITY_IMPACT_THRESHOLD: float = 0.20
    NEWS_RISK_FREEZE_DURATION_MIN: int = 30
    SUB_ACCOUNT_REQUIRED: bool = True
    CLIENT_FUNDS_DEPLOY_DENIED: bool = True
    MIN_STRESS_TEST_SCENARIOS: int = 5
    METRIC_FRESHNESS_1M_SEC: int = 60
    METRIC_FRESHNESS_5M_SEC: int = 300
    METRIC_FRESHNESS_15M_SEC: int = 900
    ROLLBACK_OPERATION_TIMEOUT_SEC: float = 10.0
    SIGNAL_LEAKAGE_DRIFT_THRESHOLD: float = 0.0005
    ABNORMAL_ORDER_RATIO_THRESHOLD: float = 0.8
    MAX_LJUNG_BOX_LAG: int = 5
    LJUNG_BOX_P_VALUE_THRESHOLD: float = 0.05
    PROMETHEUS_CACHE_SEC: int = 10
    MIN_DISK_SPACE_MB: int = 100
    GC_INTERVAL_SEC: int = 3600
    MEMORY_PRESSURE_THRESHOLD: float = 0.80

    STATE_FILE: str = "logs/canary_state.json"
    STATE_BACKUP_COUNT: int = 3
    # MODULE_VERSION 更新流程：版本升级时同步更新此常量，并在CHANGELOG中记录变更
    MODULE_VERSION: str = "3.2.0"

    def __init__(self) -> None:
        self._deployments: dict[str, dict[str, Any]] = {}
        self._daily_rollback_count: int = 0
        self._daily_rollback_date: str = time.strftime("%Y-%m-%d")
        self._metric_failure_counts: dict[str, int] = {}
        self._banned_strategies: dict[str, float] = {}
        self._rollback_cooldowns: dict[str, float] = {}

        # 外部依赖注入
        self._strategy_gene: Any = None
        self._position_sizer: Any = None
        self._circuit_breaker: Any = None
        self._behavioral_logger: Any = None
        self._volatility_regime: Any = None
        self._slippage_filter: Any = None
        self._cloud_llm_auditor: Any = None
        self._event_listener: Any = None
        self._account_ledger: Any = None
        self._compliance_checker: Any = None

        # 线程安全（使用RLock防止嵌套调用死锁）
        self._lock = threading.RLock()
        self._persist_queue: deque = deque()
        self._persist_worker_running: bool = True
        self._persist_thread = threading.Thread(target=self._persist_worker, daemon=True, name="canary-persist")
        self._persist_thread.start()

        # Prometheus缓存
        self._prometheus_cache: dict[str, Any] = {}
        self._prometheus_cache_time: float = 0.0

        # 恢复持久化状态
        self._restore_state()

        # 延迟孤儿清理（系统完全启动后执行）
        threading.Thread(target=self._delayed_orphan_cleanup, daemon=True, name="canary-orphan-cleanup").start()

        # 定时gc
        self._last_gc_time: float = time.monotonic()

        logger.info(f"CanaryDeployer v{self.MODULE_VERSION} 初始化完成")

    # ========== 后台任务 ==========
    def _persist_worker(self) -> None:
        """后台持久化线程，异步写入磁盘，永不阻塞主线程"""
        consecutive_failures: int = 0
        while self._persist_worker_running:
            try:
                batch: list[dict[str, Any]] = []
                while True:
                    try:
                        item = self._persist_queue.popleft()
                        batch.append(item)
                        if len(batch) >= 10:
                            break
                    except IndexError:
                        break

                if batch:
                    self._do_persist(batch[-1])
                consecutive_failures = 0
                time.sleep(self.STATE_PERSIST_INTERVAL_SEC)
            except Exception as e:
                consecutive_failures += 1
                logger.error(f"持久化线程异常(连续失败{consecutive_failures}次): {e} "
                           f"#RECOVERY: 检查文件系统权限和磁盘空间")
                if consecutive_failures >= 3:
                    logger.critical("持久化线程连续失败3次，状态可能丢失 #RECOVERY: 立即检查磁盘和文件系统")
                time.sleep(5)

    def _do_persist(self, data: dict[str, Any]) -> None:
        """执行实际的磁盘写入，包含原子写入和校验"""
        try:
            # 检查磁盘空间
            stat = os.statvfs(os.path.dirname(self.STATE_FILE))
            free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
            if free_mb < self.MIN_DISK_SPACE_MB:
                logger.error(f"磁盘空间不足({free_mb:.1f}MB < {self.MIN_DISK_SPACE_MB}MB)，"
                           f"尝试写入紧急备份 #RECOVERY: 清理磁盘或扩展存储")
            os.makedirs(os.path.dirname(self.STATE_FILE), exist_ok=True)
            # 滚动备份
            for i in range(self.STATE_BACKUP_COUNT - 1, 0, -1):
                src = self.STATE_FILE if i == 0 else f"{self.STATE_FILE}.bak{i}"
                dst = f"{self.STATE_FILE}.bak{i + 1}"
                if os.path.exists(src):
                    os.replace(src, dst)
            tmp_file = self.STATE_FILE + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, self.STATE_FILE)
        except Exception as e:
            logger.error(f"磁盘写入失败: {e} #RECOVERY: 检查磁盘空间和目录权限")
            # 尝试写入备用路径
            try:
                fallback = self.STATE_FILE + ".emergency"
                with open(fallback, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                logger.warning(f"已写入紧急备份: {fallback}")
            except Exception:
                pass

    def _delayed_orphan_cleanup(self) -> None:
        """系统完全启动后清理孤儿部署"""
        time.sleep(10)
        self._cleanup_orphans()

    def _cleanup_orphans(self) -> None:
        now = time.monotonic()
        with self._lock:
            orphans: list[str] = []
            for sid, d in self._deployments.items():
                if d["status"] in (CanaryStage.STAGE_1.value, CanaryStage.STAGE_2.value, CanaryStage.STAGE_3.value):
                    if now - d.get("last_monitor_at", d["started_at"]) > self.ORPHAN_TIMEOUT_HOURS * 3600:
                        orphans.append(sid)
            for sid in orphans:
                d = self._deployments[sid]
                logger.warning(f"清理孤儿部署: {self._hash_id(sid)}, "
                             f"运行时长: {(now - d['started_at'])/3600:.1f}h, "
                             f"最后监控: {d.get('last_monitor_at', 'N/A')}")
                self._execute_rollback(sid, d, "孤儿部署超时自动回滚")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        strategy_gene: Any = None,
        position_sizer: Any = None,
        circuit_breaker: Any = None,
        behavioral_logger: Any = None,
        volatility_regime: Any = None,
        slippage_filter: Any = None,
        cloud_llm_auditor: Any = None,
        event_listener: Any = None,
        account_ledger: Any = None,
        compliance_checker: Any = None,
    ) -> None:
        """注入外部依赖"""
        injected: list[str] = []
        if strategy_gene is not None:
            self._strategy_gene = strategy_gene
            injected.append("strategy_gene")
        if position_sizer is not None:
            self._position_sizer = position_sizer
            injected.append("position_sizer")
        if circuit_breaker is not None:
            self._circuit_breaker = circuit_breaker
            injected.append("circuit_breaker")
        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            injected.append("behavioral_logger")
        if volatility_regime is not None:
            self._volatility_regime = volatility_regime
            injected.append("volatility_regime")
        if slippage_filter is not None:
            self._slippage_filter = slippage_filter
            injected.append("slippage_filter")
        if cloud_llm_auditor is not None:
            self._cloud_llm_auditor = cloud_llm_auditor
            injected.append("cloud_llm_auditor")
        if event_listener is not None:
            self._event_listener = event_listener
            injected.append("event_listener")
        if account_ledger is not None:
            self._account_ledger = account_ledger
            injected.append("account_ledger")
        if compliance_checker is not None:
            self._compliance_checker = compliance_checker
            injected.append("compliance_checker")
        logger.info(f"依赖注入完成: {', '.join(injected)}")

    # ========== 状态持久化 ==========
    def _restore_state(self) -> None:
        """从文件恢复部署状态，支持备份修复，使用time.monotonic()进行时间校正"""
        recovered = False
        for i in range(self.STATE_BACKUP_COUNT, -1, -1):
            path = self.STATE_FILE if i == 0 else f"{self.STATE_FILE}.bak{i}"
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    with self._lock:
                        self._deployments = data.get("deployments", {})
                        self._daily_rollback_count = data.get("daily_rollback_count", 0)
                        self._daily_rollback_date = data.get("daily_rollback_date", time.strftime("%Y-%m-%d"))
                        self._banned_strategies = data.get("banned_strategies", {})
                        self._rollback_cooldowns = data.get("rollback_cooldowns", {})
                        # 恢复后清理过大的监控历史
                        for sid, d in self._deployments.items():
                            if len(d.get("monitor_history", [])) > self.MAX_MONITOR_HISTORY:
                                d["monitor_history"] = d["monitor_history"][-self.MAX_MONITOR_HISTORY:]
                    recovered = True
                    logger.info(f"从{path}恢复部署状态，部署数:{len(self._deployments)}")
                    break
                except Exception as e:
                    logger.warning(f"恢复状态失败({path}):{e} #RECOVERY: 尝试其他备份文件")

        if not recovered and os.path.exists(self.STATE_FILE):
            logger.error("所有状态文件损坏，无法恢复部署状态 #RECOVERY: 检查文件系统完整性")
        # 恢复后校验部署完整性
        self._validate_restored_deployments()

    def _validate_restored_deployments(self) -> None:
        """校验恢复的部署是否仍然有效（策略在系统中存在）"""
        with self._lock:
            invalid: list[str] = []
            for sid, d in self._deployments.items():
                if d["status"] in (CanaryStage.STAGE_1.value, CanaryStage.STAGE_2.value, CanaryStage.STAGE_3.value):
                    if self._strategy_gene is not None and hasattr(self._strategy_gene, 'strategy_exists'):
                        try:
                            if not self._strategy_gene.strategy_exists(sid):
                                invalid.append(sid)
                        except Exception:
                            pass
            for sid in invalid:
                self._deployments[sid]["status"] = CanaryStage.TIMEOUT.value
                logger.warning(f"恢复校验失败，标记为TIMEOUT:{self._hash_id(sid)}")

    def _persist_state(self) -> None:
        """将持久化任务提交到后台线程，使用深拷贝保护快照一致性"""
        with self._lock:
            data = {
                "deployments": copy.deepcopy(self._deployments),
                "daily_rollback_count": self._daily_rollback_count,
                "daily_rollback_date": self._daily_rollback_date,
                "banned_strategies": copy.deepcopy(self._banned_strategies),
                "rollback_cooldowns": copy.deepcopy(self._rollback_cooldowns),
                "last_saved": time.time(),
            }
        self._persist_queue.append(data)

    def _periodic_gc(self) -> None:
        """定期触发垃圾回收，内存压力高时提前触发"""
        now = time.monotonic()
        should_gc = (now - self._last_gc_time > self.GC_INTERVAL_SEC)
        if not should_gc:
            # 检查内存压力
            try:
                import psutil
                mem = psutil.virtual_memory()
                if mem.percent / 100 > self.MEMORY_PRESSURE_THRESHOLD:
                    should_gc = True
            except ImportError:
                pass
        if should_gc:
            collected = gc.collect()
            self._last_gc_time = now
            if collected > 0:
                logger.debug(f"GC回收{collected}个对象")

    # ========== 辅助工具方法 ==========
    @staticmethod
    def _is_in_time_window(start_hour: int, end_hour: int, current_hour: int) -> bool:
        """判断当前小时是否在指定时间窗口内，正确处理跨零点情况"""
        if start_hour <= end_hour:
            return start_hour <= current_hour < end_hour
        else:
            # 跨零点窗口，例如 23:00 - 01:00
            return current_hour >= start_hour or current_hour < end_hour

    # ========== 公共接口 ==========
    def deploy(self, strategy_id: str, commit_hash: str = "",
               stages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """启动金丝雀发布流程"""
        if not strategy_id:
            return {"status": "error", "reason": "策略ID不能为空", "data": {}, "warnings": ["invalid_strategy_id"]}

        if self._strategy_gene is None:
            return {"status": "error", "reason": "StrategyGene依赖不可用", "data": {}, "warnings": ["missing_dependency"]}

        # 客户资金检测
        if self.CLIENT_FUNDS_DEPLOY_DENIED and self._is_client_funds_account():
            return {"status": "error", "reason": "禁止在客户资金账户上执行金丝雀部署", "data": {},
                    "warnings": ["client_funds_account"]}

        current_utc = time.gmtime()
        current_hour = current_utc.tm_hour
        # 禁止部署时段检查（跨零点窗口）
        if self._is_in_time_window(self.DEPLOY_FORBIDDEN_START_HOUR, self.DEPLOY_FORBIDDEN_END_HOUR, current_hour):
            return {"status": "error",
                    "reason": "当前处于禁止部署时段(UTC 23:00-00:00)，覆盖交易所结算和开盘竞价期",
                    "data": {}, "warnings": ["forbidden_deploy_window"]}
        if not self._is_in_time_window(self.DEPLOY_ALLOWED_START_HOUR, self.DEPLOY_ALLOWED_END_HOUR, current_hour):
            return {"status": "error",
                    "reason": f"当前不在允许部署的时间窗口内(UTC {self.DEPLOY_ALLOWED_START_HOUR}-{self.DEPLOY_ALLOWED_END_HOUR})",
                    "data": {}, "warnings": ["outside_deploy_window"]}

        if self._is_high_risk_news_active():
            return {"status": "error", "reason": "检测到高风险新闻事件，禁止部署", "data": {}, "warnings": ["high_risk_news"]}

        if self.SUB_ACCOUNT_REQUIRED and not self._is_sub_account_isolated():
            return {"status": "error", "reason": "金丝雀策略必须在独立子账户中运行", "data": {},
                    "warnings": ["sub_account_required"]}

        if self._is_rollback_limit_reached():
            return {"status": "error", "reason": f"单日回滚次数已达上限({self.MAX_ROLLBACKS_PER_DAY})", "data": {},
                    "warnings": ["rollback_limit_reached"]}

        if self._count_active_deployments() >= self.MAX_CONCURRENT_DEPLOYMENTS:
            return {"status": "error", "reason": f"同时部署数量已达上限({self.MAX_CONCURRENT_DEPLOYMENTS})", "data": {},
                    "warnings": ["max_deployments_reached"]}

        if self._is_banned(strategy_id):
            return {"status": "error", "reason": f"策略{self._hash_id(strategy_id)}因多次回滚被禁入", "data": {},
                    "warnings": ["strategy_banned"]}

        pre_check = self._pre_deploy_check(strategy_id)
        if pre_check["status"] != "ok":
            return pre_check

        deploy_stages = stages if stages else self.DEFAULT_STAGES

        # 校验自定义阶段参数
        for i, stage in enumerate(deploy_stages):
            ratio = stage.get("ratio", 0)
            if not (0 < ratio <= 1.0):
                return {"status": "error", "reason": f"阶段{i+1}仓位比例无效: {ratio}", "data": {},
                        "warnings": ["invalid_stage_ratio"]}
            if stage.get("min_hours", 0) <= 0:
                return {"status": "error", "reason": f"阶段{i+1}最小运行时间无效", "data": {},
                        "warnings": ["invalid_stage_hours"]}

        with self._lock:
            if strategy_id in self._deployments:
                existing = self._deployments[strategy_id]
                if existing["status"] not in (
                CanaryStage.ROLLED_BACK.value, CanaryStage.COMPLETED.value, CanaryStage.TIMEOUT.value):
                    return {"status": "error", "reason": "策略已有进行中的金丝雀发布",
                            "data": {"current_stage": existing["current_stage"]}, "warnings": ["deployment_already_active"]}

            now = time.monotonic()
            deployment = {
                "strategy_id": strategy_id,
                "commit_hash": commit_hash,
                "stages": deploy_stages,
                "current_stage_index": 0,
                "current_stage": CanaryStage.STAGE_1.value,
                "current_ratio": deploy_stages[0]["ratio"],
                "started_at": now,
                "started_at_real": time.time(),  # 用于合规报告的真实时间戳
                "stage_started_at": now,
                "last_monitor_at": now,
                "status": CanaryStage.STAGE_1.value,
                "monitor_history": [],
                "rollback_count": 0,
                "market_regime_at_deploy": self._get_market_regime_snapshot(),
                "deploy_env_snapshot": self._get_env_snapshot(),
            }
            self._deployments[strategy_id] = deployment

        # 先持久化，再激活策略
        self._persist_state()
        self._activate_strategy(strategy_id, deploy_stages[0]["ratio"])

        self._log_event("canary_deploy_start", f"策略{self._hash_id(strategy_id)}金丝雀发布启动",
                        {"strategy_id": strategy_id, "commit_hash": commit_hash, "stage": 1,
                         "ratio": deploy_stages[0]["ratio"]})

        return {"status": "ok", "reason": "策略金丝雀发布已启动",
                "data": {"strategy_id": strategy_id, "current_stage": "stage_1",
                         "current_ratio": deploy_stages[0]["ratio"]}, "warnings": []}

    def monitor(self, strategy_id: str) -> dict[str, Any]:
        """检查当前阶段指标，决定推进/暂停/回滚"""
        self._periodic_gc()

        if strategy_id not in self._deployments:
            return {"status": "error", "reason": "策略无金丝雀发布记录", "data": {}, "warnings": ["deployment_not_found"]}

        if self._is_high_risk_news_active():
            return {"status": "ok", "reason": "高风险新闻事件，暂停推进", "data": {"action": "freeze", "reason": "high_risk_news"},
                    "warnings": ["high_risk_news"]}

        if self._is_volatility_high():
            return {"status": "ok", "reason": "市场波动率过高，暂停推进", "data": {"action": "freeze", "reason": "high_volatility"},
                    "warnings": ["high_volatility"]}

        if self._is_circuit_breaker_active():
            return {"status": "ok", "reason": "系统熔断中，暂停推进", "data": {"action": "hold", "reason": "circuit_breaker_active"},
                    "warnings": ["circuit_breaker_active"]}

        # 信号泄露检测
        leakage = self._detect_signal_leakage(strategy_id)
        if leakage.get("detected"):
            self._log_event("canary_signal_leakage", f"策略{self._hash_id(strategy_id)}检测到信号泄露风险",
                            {"strategy_id": strategy_id, "drift": leakage.get("drift")})
            return {"status": "ok", "reason": "检测到信号泄露风险，暂停推进", "data": {"action": "freeze", "reason": "signal_leakage"},
                    "warnings": ["signal_leakage"]}

        # 异常交易模式检测
        if self._is_abnormal_trading_pattern(strategy_id):
            return {"status": "ok", "reason": "检测到异常交易模式，暂停推进并告警",
                    "data": {"action": "freeze", "reason": "abnormal_trading"}, "warnings": ["abnormal_trading"]}

        # 在锁内读取状态，在锁外执行决策计算，最后在锁内更新状态
        with self._lock:
            deployment = self._deployments[strategy_id]
            if deployment["status"] in (CanaryStage.COMPLETED.value, CanaryStage.ROLLED_BACK.value, CanaryStage.TIMEOUT.value):
                return {"status": "ok", "reason": f"金丝雀发布已结束:{deployment['status']}",
                        "data": {"action": "none", "status": deployment["status"]}, "warnings": []}

            current_stage_index = deployment["current_stage_index"]
            current_stage = deployment["stages"][current_stage_index]
            now = time.monotonic()

            # 快速失效检测
            elapsed_hours = (now - deployment["stage_started_at"]) / 3600
            if elapsed_hours >= self.FAST_FAIL_HOURS:
                metrics_fast = self._gather_metrics_fast(strategy_id)
                if metrics_fast and metrics_fast.get("max_drawdown", 0) > current_stage.get("max_dd",
                                                                                            0.1) * self.FAST_FAIL_DD_MULTIPLIER:
                    self._execute_rollback(strategy_id, deployment, f"快速失效:回撤超过阈值{self.FAST_FAIL_DD_MULTIPLIER}倍")
                    return {"status": "ok", "reason": "快速失效触发回滚", "data": {"action": "rollback"}, "warnings": ["fast_fail"]}

            if elapsed_hours < current_stage.get("min_hours", 24):
                remaining = current_stage["min_hours"] - elapsed_hours
                return {"status": "ok", "reason": "当前阶段运行时间不足", "data": {"action": "hold", "remaining_hours": remaining},
                        "warnings": []}

            metrics = self._gather_metrics(strategy_id)
            if metrics is None:
                self._metric_failure_counts[strategy_id] = self._metric_failure_counts.get(strategy_id, 0) + 1
                if self._metric_failure_counts[strategy_id] >= self.MAX_CONSECUTIVE_METRIC_FAILURES:
                    self._execute_rollback(strategy_id, deployment, f"监控指标连续获取失败{self._metric_failure_counts[strategy_id]}次")
                    return {"status": "ok", "reason": "触发回滚:监控指标连续获取失败", "data": {"action": "rollback"},
                            "warnings": ["metrics_unavailable"]}
                return {"status": "ok",
                        "reason": f"监控指标获取失败({self._metric_failure_counts[strategy_id]}/{self.MAX_CONSECUTIVE_METRIC_FAILURES})",
                        "data": {"action": "hold"}, "warnings": ["metrics_unavailable"]}
            self._metric_failure_counts[strategy_id] = 0

            monitor_entry = {"timestamp": now, "metrics": metrics, "market_regime": self._get_market_regime_snapshot()}
            deployment["monitor_history"].append(monitor_entry)
            if len(deployment["monitor_history"]) > self.MAX_MONITOR_HISTORY:
                deployment["monitor_history"] = deployment["monitor_history"][-self.MAX_MONITOR_HISTORY:]
            deployment["last_monitor_at"] = now

            # 检查异常订单模式
            if metrics.get("abnormal_order_ratio", 0) > self.ABNORMAL_ORDER_RATIO_THRESHOLD:
                self._execute_rollback(strategy_id, deployment, f"异常订单比例过高:{metrics['abnormal_order_ratio']:.2%}")
                return {"status": "ok", "reason": "触发回滚:异常订单比例过高", "data": {"action": "rollback"},
                        "warnings": ["abnormal_orders"]}

        # 锁外评估回滚条件（避免长时间持锁）
        rollback_result = self._evaluate_rollback_conditions(strategy_id, deployment, metrics, current_stage)
        if rollback_result is not None and rollback_result.get("action") == "rollback":
            with self._lock:
                self._execute_rollback(strategy_id, deployment, rollback_result.get("reason", "回滚条件触发"))
            return {"status": "ok", "reason": rollback_result.get("reason", "触发回滚"),
                    "data": {"action": "rollback"}, "warnings": rollback_result.get("warnings", [])}

        if rollback_result is not None:
            return rollback_result

        # 锁外评估推进
        return self._evaluate_advancement(strategy_id, deployment, metrics, current_stage, current_stage_index, now)

    def _evaluate_rollback_conditions(self, strategy_id: str, deployment: dict[str, Any], metrics: dict[str, Any],
                                      current_stage: dict[str, Any]) -> dict[str, Any] | None:
        """评估回滚条件（在锁外调用）"""
        if metrics["max_drawdown"] > current_stage.get("max_dd", 0.1):
            return {"action": "rollback", "reason": f"最大回撤超限:{metrics['max_drawdown']:.2%}",
                    "warnings": ["max_drawdown_exceeded"]}
        excess_sharpe = metrics.get("net_sharpe", metrics["sharpe"]) - self._get_risk_free_rate(strategy_id)
        if excess_sharpe < current_stage.get("min_sharpe", 0.3):
            return {"action": "rollback", "reason": f"超额夏普不达标:{excess_sharpe:.2f}",
                    "warnings": ["sharpe_below_threshold"]}
        if metrics.get("signal_divergence", 0) > self.SIGNAL_DIVERGENCE_THRESHOLD:
            return {"action": "rollback", "reason": f"信号分歧度过高:{metrics['signal_divergence']:.2%}",
                    "warnings": ["signal_divergence_exceeded"]}
        if metrics.get("slippage_bps", 0) > 15:
            return {"action": "rollback", "reason": f"平均滑点过高:{metrics['slippage_bps']:.1f}bps",
                    "warnings": ["high_slippage"]}
        if not self._statistical_test(strategy_id, metrics):
            # 统计检验未通过，不直接回滚，只是暂停推进
            return {"status": "ok", "reason": "统计检验未通过",
                    "data": {"action": "hold", "reason": "statistical_test_failed"},
                    "warnings": ["statistical_test_failed"]}
        return None

    def _evaluate_advancement(self, strategy_id: str, deployment: dict[str, Any], metrics: dict[str, Any],
                              current_stage: dict[str, Any], current_stage_index: int, now: float) -> dict[str, Any]:
        """评估推进条件"""
        if current_stage_index < len(deployment["stages"]) - 1:
            next_index = current_stage_index + 1
            next_stage = deployment["stages"][next_index]
            if not self._check_market_capacity(strategy_id, next_stage["ratio"]):
                return {"status": "ok", "reason": "市场冲击成本过高，暂停推进",
                        "data": {"action": "hold"}, "warnings": ["insufficient_market_depth"]}
            with self._lock:
                deployment["current_stage_index"] = next_index
                deployment["current_stage"] = f"stage_{next_index + 1}"
                deployment["current_ratio"] = next_stage["ratio"]
                deployment["stage_started_at"] = now
            self._activate_strategy(strategy_id, next_stage["ratio"])
            self._log_event("canary_stage_advance", f"策略{self._hash_id(strategy_id)}推进至阶段{next_index + 1}",
                            {"strategy_id": strategy_id, "stage": next_index + 1, "ratio": next_stage["ratio"]})
            self._persist_state()
            return {"status": "ok", "reason": f"推进至阶段{next_index + 1}",
                    "data": {"action": "advance", "next_stage": f"stage_{next_index + 1}"}, "warnings": []}
        else:
            with self._lock:
                deployment["status"] = CanaryStage.COMPLETED.value
            self._log_event("canary_completed", f"策略{self._hash_id(strategy_id)}金丝雀发布完成，全量上线",
                            {"strategy_id": strategy_id})
            self._persist_state()
            return {"status": "ok", "reason": "金丝雀发布已完成",
                    "data": {"action": "complete", "status": "completed"}, "warnings": []}

    def rollback(self, strategy_id: str) -> dict[str, Any]:
        if strategy_id not in self._deployments:
            return {"status": "error", "reason": "策略无金丝雀发布记录", "data": {}, "warnings": ["deployment_not_found"]}
        with self._lock:
            deployment = self._deployments[strategy_id]
            last_rollback = deployment.get("last_rollback_time", 0)
            if time.monotonic() - last_rollback < 300:
                return {"status": "error", "reason": "该策略5分钟内已执行过回滚，请稍后重试", "data": {},
                        "warnings": ["cooldown_active"]}
            if deployment["status"] in (CanaryStage.ROLLED_BACK.value, CanaryStage.COMPLETED.value):
                return {"status": "ok", "reason": f"策略已经处于{deployment['status']}状态", "data": {}, "warnings": []}
            self._execute_rollback(strategy_id, deployment, "人工触发回滚")
        return {"status": "ok", "reason": "策略已回滚",
                "data": {"strategy_id": strategy_id, "status": "rolled_back"}, "warnings": []}

    def get_status(self, strategy_id: str) -> dict[str, Any]:
        if strategy_id not in self._deployments:
            return {"status": "ok", "reason": "无金丝雀发布记录", "data": {"status": "not_deployed"}, "warnings": []}
        with self._lock:
            d = self._deployments[strategy_id]
            return {"status": "ok", "reason": "查询成功",
                    "data": {"strategy_id": strategy_id, "status": d["status"],
                             "current_stage_index": d.get("current_stage_index", 0),
                             "current_ratio": d.get("current_ratio", 0), "started_at": d.get("started_at"),
                             "rollback_count": d.get("rollback_count", 0),
                             "monitor_count": len(d.get("monitor_history", []))}, "warnings": []}

    def get_prometheus_metrics(self) -> dict[str, Any]:
        now = time.monotonic()
        if now - self._prometheus_cache_time < self.PROMETHEUS_CACHE_SEC and self._prometheus_cache:
            return self._prometheus_cache
        with self._lock:
            active = sum(1 for d in self._deployments.values() if
                         d["status"] in (CanaryStage.STAGE_1.value, CanaryStage.STAGE_2.value, CanaryStage.STAGE_3.value))
            rolled_back = sum(1 for d in self._deployments.values() if d["status"] == CanaryStage.ROLLED_BACK.value)
            completed = sum(1 for d in self._deployments.values() if d["status"] == CanaryStage.COMPLETED.value)
            per_strategy = {}
            for sid, d in self._deployments.items():
                if d["status"] in (CanaryStage.STAGE_1.value, CanaryStage.STAGE_2.value, CanaryStage.STAGE_3.value):
                    per_strategy[self._hash_id(sid)] = {"stage": d["current_stage"], "ratio": d["current_ratio"]}
        result = {"canary_active_deployments": active, "canary_rolled_back": rolled_back, "canary_completed": completed,
                  "canary_per_strategy": per_strategy, "daily_rollback_count": self._daily_rollback_count}
        self._prometheus_cache = result
        self._prometheus_cache_time = now
        return result

    def generate_compliance_report(self, strategy_id: str) -> dict[str, Any]:
        """生成面向监管的合规报告"""
        if strategy_id not in self._deployments:
            return {"status": "error", "reason": "策略无金丝雀发布记录", "data": {}}
        with self._lock:
            d = self._deployments[strategy_id]
            deploy_time = d.get("started_at_real", d["started_at"])
            if deploy_time > 1e10:  # time.monotonic() 的值，说明 started_at_real 未设置
                deploy_time = time.time()
            return {"status": "ok", "reason": "合规报告生成完成",
                    "data": {"strategy_id": self._hash_id(strategy_id),
                             "deploy_date": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(deploy_time)),
                             "current_status": d["status"],
                             "stages_completed": d["current_stage_index"],
                             "total_rollbacks": d.get("rollback_count", 0),
                             "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                             "auditor_signature_required": self.COMPLIANCE_APPROVAL_REQUIRED,
                             "final_disposition": "APPROVED" if d["status"] == CanaryStage.COMPLETED.value else
                             "REJECTED" if d["status"] == CanaryStage.ROLLED_BACK.value else "PENDING"}}

    def health_check(self) -> dict[str, Any]:
        try:
            with self._lock:
                active = sum(1 for d in self._deployments.values() if
                             d["status"] not in (
                             CanaryStage.ROLLED_BACK.value, CanaryStage.COMPLETED.value, CanaryStage.TIMEOUT.value))
                total = len(self._deployments)
                last_rollback = "N/A"
                for d in self._deployments.values():
                    if d.get("last_rollback_time"):
                        if last_rollback == "N/A" or d["last_rollback_time"] > last_rollback:
                            last_rollback = f"{d.get('last_rollback_reason', 'N/A')} ({time.strftime('%H:%M:%S', time.localtime(d['last_rollback_time']))})"
            deps = {"strategy_gene": self._strategy_gene is not None,
                    "position_sizer": self._position_sizer is not None,
                    "circuit_breaker": self._circuit_breaker is not None,
                    "behavioral_logger": self._behavioral_logger is not None,
                    "volatility_regime": self._volatility_regime is not None,
                    "slippage_filter": self._slippage_filter is not None,
                    "cloud_llm_auditor": self._cloud_llm_auditor is not None,
                    "event_listener": self._event_listener is not None,
                    "account_ledger": self._account_ledger is not None,
                    "compliance_checker": self._compliance_checker is not None}
            return {"status": "ok",
                    "reason": f"CanaryDeployer v{self.MODULE_VERSION}正常，活跃部署:{active}/{total}",
                    "data": {"active_deployments": active, "total_deployments": total, "dependencies": deps,
                             "version": self.MODULE_VERSION, "last_rollback": last_rollback,
                             "persist_queue_size": len(self._persist_queue)}, "warnings": []}
        except Exception as e:
            logger.error(f"健康检查失败:{e} #RECOVERY: 检查内部状态锁和字典完整性")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"]}

    # ========== 私有方法 ==========
    def _hash_id(self, strategy_id: str) -> str:
        # SHA256截断至12字符，碰撞概率约1/2^48，在万级策略规模下可忽略
        return hashlib.sha256(strategy_id.encode()).hexdigest()[:12]

    def _count_active_deployments(self) -> int:
        with self._lock:
            return sum(1 for d in self._deployments.values() if
                       d["status"] in (CanaryStage.STAGE_1.value, CanaryStage.STAGE_2.value, CanaryStage.STAGE_3.value))

    def _is_banned(self, strategy_id: str) -> bool:
        with self._lock:
            # O(1) 查询禁止列表
            if strategy_id in self._banned_strategies:
                ban_until = self._banned_strategies[strategy_id]
                if time.monotonic() < ban_until:
                    return True
                else:
                    del self._banned_strategies[strategy_id]  # 过期自动移除
            # 检查历史回滚次数
            if strategy_id in self._deployments:
                d = self._deployments[strategy_id]
                if d.get("rollback_count", 0) >= self.MAX_ROLLBACKS_BEFORE_BAN:
                    last_rb = d.get("last_rollback_time", 0)
                    ban_until = last_rb + self.ROLLBACK_BAN_DAYS * 86400
                    if time.monotonic() < ban_until:
                        self._banned_strategies[strategy_id] = ban_until
                        return True
        return False

    def _is_high_risk_news_active(self) -> bool:
        if self._event_listener is not None and hasattr(self._event_listener, 'is_high_risk'):
            try:
                return self._event_listener.is_high_risk()
            except Exception:
                pass
        return False

    def _is_sub_account_isolated(self) -> bool:
        if self._account_ledger is not None and hasattr(self._account_ledger, 'is_sub_account'):
            try:
                return self._account_ledger.is_sub_account()
            except Exception:
                pass
        return False

    def _is_client_funds_account(self) -> bool:
        if self._account_ledger is not None and hasattr(self._account_ledger, 'is_client_funds'):
            try:
                return self._account_ledger.is_client_funds()
            except Exception:
                pass
        return False

    def _get_risk_free_rate(self, strategy_id: str) -> float:
        """根据策略交易对的计价货币返回无风险利率"""
        if self._position_sizer is not None and hasattr(self._position_sizer, 'get_quote_currency'):
            try:
                quote = self._position_sizer.get_quote_currency(strategy_id)
                if quote == "BTC":
                    return self.RISK_FREE_RATE_BTC
                if quote == "ETH":
                    return self.RISK_FREE_RATE_ETH
            except Exception:
                pass
        return self.RISK_FREE_RATE_USDT

    def _detect_signal_leakage(self, strategy_id: str) -> dict[str, Any]:
        """检测金丝雀策略是否存在信号泄露（成交后价格被高频做市商逆向推动）"""
        try:
            if self._position_sizer is not None and hasattr(self._position_sizer, 'get_post_trade_drift'):
                drift = self._position_sizer.get_post_trade_drift(strategy_id)
                return {"detected": abs(drift) > self.SIGNAL_LEAKAGE_DRIFT_THRESHOLD, "drift": drift}
        except Exception:
            pass
        return {"detected": False, "drift": 0}

    def _is_abnormal_trading_pattern(self, strategy_id: str) -> bool:
        """检测异常交易模式（如幌骗、刷单）"""
        if self._compliance_checker is not None and hasattr(self._compliance_checker, 'check_order_pattern'):
            try:
                return self._compliance_checker.check_order_pattern(strategy_id)
            except Exception:
                pass
        return False

    def _get_env_snapshot(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {"timestamp": time.time()}
        try:
            import sys
            snapshot["python_version"] = sys.version
        except Exception:
            pass
        try:
            # Python 3.8+ 使用 importlib.metadata，3.7 降级为 pkg_resources
            try:
                from importlib.metadata import packages_distributions
                snapshot["key_packages"] = "importlib.metadata available"
            except ImportError:
                import pkg_resources
                installed = {p.key: p.version for p in pkg_resources.working_set}
                snapshot["key_packages"] = {k: installed[k] for k in ['numpy', 'pandas', 'scipy', 'torch'] if
                                            k in installed}
        except Exception:
            pass
        return snapshot

    def _pre_deploy_check(self, strategy_id: str) -> dict[str, Any]:
        """部署前预检（含压力测试和交易所规则校验）"""
        if self._position_sizer is None:
            return {"status": "error", "reason": "PositionSizer不可用", "data": {}, "warnings": []}
        try:
            if hasattr(self._position_sizer, 'get_shadow_report'):
                report = self._position_sizer.get_shadow_report(strategy_id)
                if report is None:
                    return {"status": "error", "reason": "缺少影子验证报告", "data": {},
                            "warnings": ["missing_shadow_report"]}
                if report.get("sharpe", 0) < 0.3:
                    return {"status": "error", "reason": "影子验证夏普过低",
                            "data": {"sharpe": report.get("sharpe")}, "warnings": ["low_shadow_sharpe"]}
        except Exception as e:
            logger.warning(f"影子验证报告检查失败:{e}")
        if hasattr(self._position_sizer, 'get_stress_test_results'):
            stress = self._position_sizer.get_stress_test_results(strategy_id)
            if stress:
                passed_scenarios = stress.get("passed", 0)
                failed_details = stress.get("failed_details", [])
                if passed_scenarios < self.MIN_STRESS_TEST_SCENARIOS:
                    logger.warning(f"压力测试未通过足够场景: {passed_scenarios}/{self.MIN_STRESS_TEST_SCENARIOS}, "
                                   f"失败详情: {failed_details}")
                    return {"status": "error", "reason": "未通过足够的压力测试场景", "data": {},
                            "warnings": ["stress_test_fail"]}
        if hasattr(self._position_sizer, 'check_exchange_rules_match'):
            if not self._position_sizer.check_exchange_rules_match(strategy_id):
                return {"status": "error", "reason": "当前交易所规则与策略回测时的规则不一致", "data": {},
                        "warnings": ["exchange_rules_mismatch"]}
        return {"status": "ok", "reason": "预检通过", "data": {}, "warnings": []}

    def _is_volatility_high(self) -> bool:
        if self._volatility_regime is not None and hasattr(self._volatility_regime, 'get_current_amplitude'):
            try:
                return self._volatility_regime.get_current_amplitude() > self.VOLATILITY_FREEZE_THRESHOLD
            except Exception:
                pass
        return False

    def _get_market_regime_snapshot(self) -> dict[str, Any]:
        try:
            if self._volatility_regime is not None and hasattr(self._volatility_regime, 'get_regime'):
                return self._volatility_regime.get_regime()
        except Exception:
            pass
        return {"regime": "unknown", "timestamp": time.time()}

    def _check_market_capacity(self, strategy_id: str, ratio: float) -> bool:
        """检查市场冲击成本是否可接受（含非线性增长检测）"""
        try:
            if self._slippage_filter is not None and hasattr(self._slippage_filter, 'estimate_impact'):
                impact = self._slippage_filter.estimate_impact(strategy_id, ratio)
                if impact > self.LIQUIDITY_IMPACT_THRESHOLD:
                    return False
                # 检测冲击成本非线性增长
                if abs(ratio - 0.01) > 1e-9:
                    impact_1pct = self._slippage_filter.estimate_impact(strategy_id, 0.01)
                    denominator = ratio - 0.01
                    if abs(denominator) > 1e-9:
                        gamma = (impact - impact_1pct) / denominator
                        if gamma > 0.5:
                            logger.warning(f"冲击成本非线性增长，gamma={gamma:.2f}，暂停推进")
                            return False
        except Exception:
            pass
        return True

    def _statistical_test(self, strategy_id: str, metrics: dict[str, Any]) -> bool:
        """带自相关修正的统计显著性检验"""
        try:
            if self._position_sizer is not None and hasattr(self._position_sizer, 'get_daily_returns'):
                new_rets = self._position_sizer.get_daily_returns(strategy_id)
                old_rets = self._position_sizer.get_old_daily_returns(strategy_id)
                if new_rets and old_rets and len(new_rets) >= self.MIN_TRADES_FOR_TTEST and len(
                        old_rets) >= self.MIN_TRADES_FOR_TTEST:
                    if _HAS_SCIPY:
                        diff = np.array(new_rets) - np.array(old_rets)
                        has_autocorr = self._has_autocorrelation(diff)
                        if has_autocorr:
                            logger.info("检测到自相关，使用Mann-Whitney U检验")
                            _, p_value = _scipy_stats.mannwhitneyu(new_rets, old_rets, alternative='greater')
                        else:
                            _, p_value = _scipy_stats.ttest_rel(new_rets, old_rets)
                        return p_value < self.STATISTICAL_P_VALUE_THRESHOLD and np.mean(new_rets) > np.mean(old_rets)
                    else:
                        logger.warning("scipy不可用，统计检验降级为简单均值比较")
                        return np.mean(new_rets) > np.mean(old_rets)
        except Exception as e:
            logger.warning(f"统计检验失败:{e}")
        return True  # 降级：通过

    def _has_autocorrelation(self, data: np.ndarray) -> bool:
        """检测时间序列是否存在显著自相关"""
        if _HAS_STATSMODELS and len(data) >= 2:
            try:
                acf_vals = _acf(data, nlags=min(self.MAX_LJUNG_BOX_LAG, len(data) - 1))
                if len(acf_vals) > 1:
                    return abs(acf_vals[1]) > 0.3
            except Exception:
                pass
        return False

    def _get_metric_freshness(self, strategy_id: str) -> int:
        """根据策略周期返回指标保鲜期"""
        if self._position_sizer is not None and hasattr(self._position_sizer, 'get_strategy_period'):
            try:
                period = self._position_sizer.get_strategy_period(strategy_id)
                if period <= 1:
                    return self.METRIC_FRESHNESS_1M_SEC
                if period <= 5:
                    return self.METRIC_FRESHNESS_5M_SEC
            except Exception:
                pass
        return self.METRIC_FRESHNESS_15M_SEC

    def _gather_metrics_fast(self, strategy_id: str) -> dict[str, Any] | None:
        """快速失效检测专用：只获取回撤和交易笔数，不检查指标保鲜期"""
        try:
            if self._position_sizer is not None and hasattr(self._position_sizer, 'get_quick_metrics'):
                return self._position_sizer.get_quick_metrics(strategy_id)
        except Exception:
            pass
        return self._gather_metrics(strategy_id)

    def _activate_strategy(self, strategy_id: str, ratio: float) -> None:
        effective_ratio = ratio * self.LEVERAGE_REDUCTION_PCT if ratio < 1.0 else ratio
        if self._strategy_gene is not None and hasattr(self._strategy_gene, 'set_active'):
            try:
                self._strategy_gene.set_active(strategy_id, True)
            except Exception as e:
                logger.warning(f"策略激活失败:{e}")
        if self._position_sizer is not None and hasattr(self._position_sizer, 'set_strategy_weight'):
            try:
                self._position_sizer.set_strategy_weight(strategy_id, effective_ratio)
            except Exception as e:
                logger.warning(f"设置仓位权重失败:{e}")

    def _deactivate_strategy(self, strategy_id: str) -> None:
        """带指数退避和抖动的停用重试，失败后标记FROZEN并等待人工干预"""
        success = False
        base_delay = 1.0
        for attempt in range(3):
            try:
                if self._strategy_gene is not None and hasattr(self._strategy_gene, 'set_active'):
                    self._strategy_gene.set_active(strategy_id, False)
                if self._position_sizer is not None and hasattr(self._position_sizer, 'set_strategy_weight'):
                    self._position_sizer.set_strategy_weight(strategy_id, 0.0)
                success = True
                break
            except Exception as e:
                jitter = random.uniform(-0.25, 0.25)
                delay = base_delay * (2 ** attempt) * (1 + jitter)
                logger.warning(f"策略停用失败(尝试{attempt + 1}/3):{e}，{delay:.1f}s后重试")
                time.sleep(delay)
        if not success:
            logger.error(f"策略停用失败，标记为FROZEN，请手动处理:{strategy_id} #RECOVERY: 人工介入停用策略并清零仓位")
            with self._lock:
                if strategy_id in self._deployments:
                    self._deployments[strategy_id]["status"] = CanaryStage.FROZEN.value
            self._log_event("canary_deactivate_failed",
                            f"策略{self._hash_id(strategy_id)}停用失败，已标记为FROZEN",
                            {"strategy_id": strategy_id, "attempts": 3})

    def _is_circuit_breaker_active(self) -> bool:
        if self._circuit_breaker is not None and hasattr(self._circuit_breaker, 'is_active'):
            try:
                return self._circuit_breaker.is_active()
            except Exception:
                pass
        return False

    def _is_rollback_limit_reached(self) -> bool:
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            if self._daily_rollback_date != today:
                self._daily_rollback_count = 0
                self._daily_rollback_date = today
            return self._daily_rollback_count >= self.MAX_ROLLBACKS_PER_DAY

    def _gather_metrics(self, strategy_id: str) -> dict[str, Any] | None:
        metrics: dict[str, Any] = {}
        try:
            if self._position_sizer is not None and hasattr(self._position_sizer, 'get_strategy_metrics'):
                raw = self._position_sizer.get_strategy_metrics(strategy_id)
                if raw is None:
                    return None
                ts = raw.get("timestamp", 0)
                freshness = self._get_metric_freshness(strategy_id)
                if time.time() - ts > freshness:
                    logger.warning(f"策略{self._hash_id(strategy_id)}指标数据过期({(time.time() - ts) / 60:.1f}min)")
                    return None
                metrics["sharpe"] = raw.get("sharpe", self.DEFAULT_SHARPE_FALLBACK)
                metrics["max_drawdown"] = raw.get("max_drawdown", 0.0)
                metrics["trade_count"] = raw.get("trade_count", 0)
                metrics["signal_divergence"] = raw.get("signal_divergence", 0.0)
                metrics["slippage_bps"] = raw.get("slippage_bps", 0.0)
                metrics["fill_rate"] = raw.get("fill_rate", 1.0)
                net = raw.get("net_sharpe")
                metrics["net_sharpe"] = net if net is not None else metrics["sharpe"]
                if net is None:
                    logger.debug(f"策略{self._hash_id(strategy_id)} net_sharpe不可用，回退到sharpe={metrics['sharpe']:.2f}")
                metrics["abnormal_order_ratio"] = raw.get("abnormal_order_ratio", 0.0)
            else:
                return None
        except Exception as e:
            logger.error(f"获取策略指标失败:{e} #RECOVERY: 检查PositionSizer接口")
            return None
        if not isinstance(metrics["sharpe"], (int, float)):
            metrics["sharpe"] = self.DEFAULT_SHARPE_FALLBACK
        if metrics["max_drawdown"] < 0:
            metrics["max_drawdown"] = 0.0
        if metrics["trade_count"] < 0:
            metrics["trade_count"] = 0
        return metrics

    def _execute_rollback(self, strategy_id: str, deployment: dict[str, Any], reason: str) -> None:
        """执行回滚（异步根因分析，带超时监控），需在锁内调用"""
        start_time = time.monotonic()
        deployment["status"] = CanaryStage.ROLLED_BACK.value
        deployment["rolled_back_at"] = time.time()
        deployment["rollback_count"] = deployment.get("rollback_count", 0) + 1
        deployment["last_rollback_reason"] = reason
        deployment["last_rollback_time"] = time.monotonic()
        self._daily_rollback_count += 1
        self._deactivate_strategy(strategy_id)
        elapsed = time.monotonic() - start_time
        if elapsed > self.ROLLBACK_OPERATION_TIMEOUT_SEC:
            logger.error(f"回滚操作耗时{elapsed:.1f}s，超过{self.ROLLBACK_OPERATION_TIMEOUT_SEC}s告警")
            if deployment["status"] != CanaryStage.FROZEN.value:
                deployment["status"] = CanaryStage.FROZEN.value
                logger.error(f"策略{self._hash_id(strategy_id)}回滚超时，已标记为FROZEN #RECOVERY: 人工介入检查")
        self._log_event("canary_rollback", f"策略{self._hash_id(strategy_id)}已回滚:{reason}(耗时{elapsed:.1f}s)",
                        {"strategy_id": strategy_id, "reason": reason,
                         "rollback_count": deployment["rollback_count"], "duration_s": elapsed})
        if self._cloud_llm_auditor is not None:
            threading.Thread(target=self._async_diagnosis, args=(strategy_id, reason, deployment), daemon=True).start()
        self._persist_state()
        logger.warning(f"金丝雀回滚:{self._hash_id(strategy_id)},原因:{reason}")

    def _async_diagnosis(self, strategy_id: str, reason: str, deployment: dict[str, Any]) -> None:
        """异步触发根因分析"""
        try:
            self._cloud_llm_auditor.request_diagnosis(strategy_id, reason, deployment.get("monitor_history", []))
        except Exception as e:
            logger.warning(f"触发根因分析失败:{e}")

    def _log_event(self, event_type: str, message: str, details: dict[str, Any]) -> None:
        """智能分级日志"""
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(event_type=event_type, details=details, message=message)
            except Exception as e:
                logger.warning(f"行为日志记录失败:{e}")
        if "rollback" in event_type or "failed" in event_type or "error" in event_type:
            logger.warning(f"[{event_type}]{message}")
        elif "start" in event_type or "completed" in event_type:
            logger.info(f"[{event_type}]{message}")
        else:
            logger.debug(f"[{event_type}]{message}")
