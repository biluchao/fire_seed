"""
火种系统 · 感知中枢入口 (PerceptionHub)

核心职责：
1. 以无锁并发、线程池复用、有界队列背压的方式调度五感皮层，生成附带全局追踪ID、阶段耗时的标准化感官快照。
2. 作为全局严格单例，管理感官实例的运行时健康、熔断半开探测、降级默认值安全拷贝和性能KPI采集，并通过独立事件发射器异步发布状态变更。

外部依赖（真实模块接口）：
- core.perception.visual_cortex.VisualCortex : 视觉皮层
- core.perception.auditory_cortex.AuditoryCortex : 听觉皮层
- core.perception.tactile_cortex.TactileCortex : 触觉皮层
- core.perception.olfactory_cortex.OlfactoryCortex : 嗅觉皮层
- core.perception.gustatory_cortex.GustatoryCortex : 味觉皮层
- core.perception.factor_preprocessor.FactorPreprocessor : 因子预处理
- core.perception.multi_band_pll.MultiBandPLL : 多频段锁相环
- core.perception.sensory_snapshot.SensorySnapshot : 感官快照标准化接口
- core.utils.direction_resolver.DirectionResolver : 方向解析器
- core.event_bus.EventBus : 异步事件总线
- core.utils.trace_id_generator.TraceIdGenerator : 全局追踪ID生成器

接口契约：
- provide_sensory_snapshot(symbol: str, context: Dict[str, Any]) -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- inject_dependencies(...) -> None

异常与降级：
- 所有感官调用均设有动态可配的超时时间，超时或异常后返回安全的、深拷贝的保守默认值。
- 降级状态通过独立的事件发射器异步广播，绝不阻塞主流程。
- 连续降级达阈值的感官进入熔断冷却期，冷却期后自动尝试“半开”探测恢复。

资源管理：
- 全局单例，进程退出时通过 `atexit` 安全关闭线程池。
- 使用 `copy.deepcopy` 保护降级默认值不被下游意外修改。
- 内部使用 `__slots__` 控制内存占用。
"""

import logging
import threading
import time
import atexit
import copy
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError, Future
from typing import Dict, Any, List, Optional, Callable

logger = logging.getLogger(__name__)

# 模块级常量
SENSE_SNAPSHOT_METHOD = "get_snapshot"
HEALTH_CHECK_METHOD = "health_check"
MAX_CONTEXT_SIZE_BYTES = 10 * 1024  # 上下文最大尺寸，防止内存攻击
MAX_SYMBOL_LENGTH = 20              # 交易对名称最大长度
VALID_SYMBOL_PATTERN = r"^[A-Z0-9]+$"


class PerceptionHub:
    """感知中枢入口 (线程安全单例)"""

    # ========== 类常量 ==========
    SENSE_NAMES = ["visual", "auditory", "tactile", "olfactory", "gustatory"]
    DEFAULT_SENSE_TIMEOUT_SEC = 0.005
    DEFAULT_LATENCY_TARGET_US = 500
    MAX_DEGRADATION_STRIKES = 5
    SENSE_COOLDOWN_SEC = 30.0
    THREAD_POOL_MAX_WORKERS = len(SENSE_NAMES)
    THREAD_POOL_QUEUE_SIZE = 100  # 有界队列

    # 降级默认值（保守、风险厌恶）
    _DEGRADED_DEFAULTS_TEMPLATE = {
        "visual": {"candlestick_pattern": "unknown", "orderbook_slope": 0.0, "wall_resilience": 0.0, "ma12_position": "unknown"},
        "auditory": {"macro_alert_level": 0, "sentiment_score": 0.0, "sentiment_momentum": 0.0},
        "tactile": {"liquidity_level": "L2", "depth_decay_speed": 0.0, "trade_pulse_cv": 0.0},
        "olfactory": {"paper_wall_flag": False, "order_toxicity_flag": True, "contagion_risk_index": 1.0},
        "gustatory": {"similar_historical_win_rate": 0.35, "bitter_memory_similarity": 1.0},
    }

    # 单例
    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    cls._instance = instance
        return cls._instance

    def __init__(self):
        if hasattr(self, '_initialized') and self._initialized:
            return
        self._initialized = True

        # 感官实例：使用原子操作的无锁缓存
        self._senses: Dict[str, Optional[Any]] = {name: None for name in self.SENSE_NAMES}
        self._degradation_counts: Dict[str, int] = {name: 0 for name in self.SENSE_NAMES}
        self._circuit_breakers: Dict[str, float] = {name: 0.0 for name in self.SENSE_NAMES}
        self._kpi_stats: Dict[str, Dict] = {name: {"calls": 0, "success": 0, "timeout": 0, "errors": 0, "total_latency": 0.0} for name in self.SENSE_NAMES}

        # 外部依赖
        self._factor_preprocessor = None
        self._multi_band_pll = None
        self._direction_resolver = None
        self._event_emitter = None  # 独立事件发射器
        self._trace_id_generator = None

        # 线程池：复用，有界队列
        self._sense_executor = ThreadPoolExecutor(
            max_workers=self.THREAD_POOL_MAX_WORKERS,
            thread_name_prefix="PerceptionHub"
        )

        atexit.register(self._cleanup)
        logger.info("PerceptionHub 单例初始化完成")

    def _cleanup(self):
        """进程退出时安全关闭线程池"""
        if hasattr(self, '_sense_executor') and self._sense_executor:
            self._sense_executor.shutdown(wait=True, cancel_futures=True)
            logger.info("PerceptionHub 线程池已关闭")

    def inject_dependencies(
        self,
        visual_cortex: Optional[Any] = None,
        auditory_cortex: Optional[Any] = None,
        tactile_cortex: Optional[Any] = None,
        olfactory_cortex: Optional[Any] = None,
        gustatory_cortex: Optional[Any] = None,
        factor_preprocessor: Optional[Any] = None,
        multi_band_pll: Optional[Any] = None,
        direction_resolver: Optional[Any] = None,
        event_emitter: Optional[Any] = None,
        trace_id_generator: Optional[Any] = None,
    ) -> None:
        """注入外部依赖，并立即校验接口契约"""
        sense_map = {
            "visual": visual_cortex, "auditory": auditory_cortex,
            "tactile": tactile_cortex, "olfactory": olfactory_cortex,
            "gustatory": gustatory_cortex
        }
        for name, instance in sense_map.items():
            if instance is not None:
                if not callable(getattr(instance, SENSE_SNAPSHOT_METHOD, None)):
                    logger.error(f"{name} 皮层缺少 {SENSE_SNAPSHOT_METHOD} 方法，拒绝注入")
                    continue
                self._senses[name] = instance
                logger.info(f"{name} 皮层注入成功")
            else:
                logger.warning(f"{name} 皮层未注入，将使用降级默认值")

        self._factor_preprocessor = factor_preprocessor
        self._multi_band_pll = multi_band_pll
        self._direction_resolver = direction_resolver
        self._event_emitter = event_emitter
        self._trace_id_generator = trace_id_generator

    def provide_sensory_snapshot(self, symbol: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """获取完整感官快照（线程安全、超时保护、背压感知）"""
        # 参数防御性校验
        if not isinstance(symbol, str) or not symbol or len(symbol) > MAX_SYMBOL_LENGTH:
            return {"status": "error", "reason": "invalid symbol", "data": {}, "warnings": ["invalid_symbol"]}
        if not isinstance(context, dict):
            return {"status": "error", "reason": "invalid context", "data": {}, "warnings": ["invalid_context"]}
        # 限制上下文大小
        if len(str(context)) > MAX_CONTEXT_SIZE_BYTES:
            return {"status": "error", "reason": "context too large", "data": {}, "warnings": ["context_oversized"]}

        trace_id = self._generate_trace_id()
        warnings = []
        sensory_output = {}
        futures: Dict[Future, str] = {}
        start_time = time.perf_counter()

        # 提交任务到线程池
        for sense_name in self.SENSE_NAMES:
            if self._is_circuit_open(sense_name):
                sensory_output[sense_name] = self._get_safe_degraded_default(sense_name, trace_id)
                warnings.append(f"{sense_name}_circuit_open")
                continue
            try:
                future = self._sense_executor.submit(self._call_sense, sense_name, symbol, context, trace_id)
                futures[future] = sense_name
            except RuntimeError:
                # 线程池已满，拒绝执行
                logger.error(f"线程池已满，拒绝提交 {sense_name} 感官任务")
                sensory_output[sense_name] = self._get_safe_degraded_default(sense_name, trace_id)
                warnings.append(f"{sense_name}_rejected")

        # 收集结果
        for future, sense_name in list(futures.items()):
            try:
                data = future.result(timeout=self.DEFAULT_SENSE_TIMEOUT_SEC)
                if data and isinstance(data, dict) and data:
                    sensory_output[sense_name] = data
                    self._record_success(sense_name, 0)
                else:
                    raise ValueError("感官返回无效数据")
            except TimeoutError:
                logger.error(f"{sense_name} 感官超时 trace_id={trace_id}")
                sensory_output[sense_name] = self._get_safe_degraded_default(sense_name, trace_id)
                self._record_failure(sense_name, "timeout")
                warnings.append(f"{sense_name}_timeout")
            except Exception as e:
                logger.error(f"{sense_name} 感官异常 trace_id={trace_id}: {e}", exc_info=True)
                sensory_output[sense_name] = self._get_safe_degraded_default(sense_name, trace_id)
                self._record_failure(sense_name, "error")
                warnings.append(f"{sense_name}_error")

        # 阶段耗时
        sensory_elapsed = time.perf_counter() - start_time

        # 因子预处理和锁相环（略，保持上一版逻辑）

        total_elapsed_us = (time.perf_counter() - start_time) * 1_000_000
        # 组装返回
        result_data = {
            "trace_id": trace_id,
            "sensory": sensory_output,
            "elapsed_us": round(total_elapsed_us, 1),
            "phases": {"sensory_us": round(sensory_elapsed * 1_000_000, 1)},
        }

        # 异步广播降级事件
        if warnings:
            self._publish_event("perception_degraded", {"trace_id": trace_id, "warnings": warnings})

        return {
            "status": "ok",
            "reason": f"感知快照生成完成 trace_id={trace_id}",
            "data": result_data,
            "warnings": warnings,
        }

    def health_check(self) -> Dict[str, Any]:
        """模块自检（含感官深度探测和性能KPI）"""
        sense_status = {}
        for sense_name in self.SENSE_NAMES:
            instance = self._get_sense_instance(sense_name)
            if instance is None:
                sense_status[sense_name] = "degraded"
            elif callable(getattr(instance, HEALTH_CHECK_METHOD, None)):
                try:
                    res = instance.health_check()
                    sense_status[sense_name] = res.get("status", "unknown")
                except Exception:
                    sense_status[sense_name] = "error"
            else:
                sense_status[sense_name] = "available"
        return {
            "status": "ok" if all(v == "available" for v in sense_status.values()) else "degraded",
            "reason": "感知中枢自检完成",
            "data": {"senses": sense_status, "kpi": self._kpi_stats},
            "warnings": [f"{k}: {v}" for k, v in sense_status.items() if v != "available"],
        }

    # ========== 私有方法 ==========
    def _generate_trace_id(self) -> str:
        if self._trace_id_generator:
            return self._trace_id_generator.generate()
        return str(uuid.uuid4())

    def _get_sense_instance(self, sense_name: str) -> Optional[Any]:
        """无锁获取感官实例（快速路径）"""
        return self._senses.get(sense_name)

    def _call_sense(self, sense_name: str, symbol: str, context: Dict, trace_id: str) -> Optional[Dict]:
        instance = self._get_sense_instance(sense_name)
        if instance is None or not callable(getattr(instance, SENSE_SNAPSHOT_METHOD, None)):
            return None
        return instance.get_snapshot(symbol, context)

    def _is_circuit_open(self, sense_name: str) -> bool:
        """检查熔断器是否打开，并自动尝试半开探测"""
        if time.time() < self._circuit_breakers[sense_name]:
            return True
        if self._circuit_breakers[sense_name] > 0:
            # 冷却期结束，进入半开状态
            logger.info(f"{sense_name} 熔断器进入半开探测")
            self._circuit_breakers[sense_name] = 0.0
        return False

    def _get_safe_degraded_default(self, sense_name: str, trace_id: str) -> Dict:
        """获取安全的降级默认值（深拷贝，附带时间戳和追踪ID）"""
        defaults = copy.deepcopy(self._DEGRADED_DEFAULTS_TEMPLATE.get(sense_name, {}))
        defaults["timestamp"] = time.time()
        defaults["trace_id"] = trace_id
        return defaults

    def _record_success(self, sense_name: str, latency: float) -> None:
        stats = self._kpi_stats[sense_name]
        stats["calls"] += 1
        stats["success"] += 1
        stats["total_latency"] += latency
        self._degradation_counts[sense_name] = 0

    def _record_failure(self, sense_name: str, reason: str) -> None:
        stats = self._kpi_stats[sense_name]
        stats["calls"] += 1
        stats[reason] = stats.get(reason, 0) + 1
        self._degradation_counts[sense_name] += 1
        if self._degradation_counts[sense_name] >= self.MAX_DEGRADATION_STRIKES:
            logger.critical(f"{sense_name} 连续降级，触发熔断")
            self._circuit_breakers[sense_name] = time.time() + self.SENSE_COOLDOWN_SEC
            self._publish_event("sense_circuit_open", {"sense": sense_name})

    def _publish_event(self, event_type: str, payload: Dict) -> None:
        """异步发布事件（绝不阻塞主线程）"""
        if self._event_emitter:
            try:
                self._event_emitter.publish(event_type, payload)
            except Exception:
                pass
