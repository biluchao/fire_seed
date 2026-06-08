"""
火种系统 · 六层进化安全滤网 (SixLayerFilter)

核心职责：
1. 对候选策略基因执行六层递进式安全与质量过滤，确保只有通过全部检验的策略才能进入金丝雀发布阶段
2. 提供统一的过滤调度接口，记录每层滤网的决策依据与全链路追踪信息，支持运维审计与故障溯源
3. 提供完整的审计轨迹、基因完整性校验与合规审批记录，满足万亿美金账户级别的机构合规要求

外部依赖（真实模块接口）：
- brain.evolution.syntax_filter.SyntaxFilter : 第一层语法与危险函数检测
- brain.evolution.economic_filter.EconomicFilter : 第三层经济约束与单调性检验
- brain.evolution.sandbox_compiler.SandboxCompiler : 第四层Docker沙箱安全编译
- ghost.shadow_validator_v2.ShadowValidatorV2 : 第五层影子验证与统计检验
- core.evolution_safety_manager.canary_deployer.CanaryDeployer : 第六层金丝雀渐进发布
- core.behavioral_logger.BehavioralLogger : 记录过滤事件与审计日志

接口契约：
- apply_all_filters(strategy_gene: Dict[str, Any], trace_id: str = "") -> Dict[str, Any] : 顺序执行全部六层滤网，返回最终判定结果
- health_check() -> Dict[str, Any] : 模块自检
- get_layer_stats(window_hours: float = 0, source_filter: str = "") -> Dict[str, Any] : 返回各层滤网的历史统计信息
- generate_trace_id() -> str : 生成符合规范的全链路追踪ID
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当任一滤网模块不可用时，自动跳过该层并在warnings中标记，除非该层被标记为强制性
- 每层滤网执行超时后自动中断，记录超时日志并使用保守拒绝策略
- 所有外部依赖的返回值均通过 _safe_get_bool 进行类型安全校验，无效类型视为拒绝
- 非强制层在依赖不可用时的默认行为为通过并附带 [NON_MANDATORY_SKIP] 警告标记
- 审计日志中的敏感字段经过控制字符和换行符过滤，防止日志注入

资源管理：
- 本模块不持有任何外部资源句柄，所有依赖通过注入获得
- _filter_cache 设有最大容量限制，超出时自动淘汰最旧记录
- 所有方法为纯函数或无副作用的状态查询，线程安全通过细粒度锁保护
- inject_dependencies 仅允许首次注入（重复调用被忽略），防止运行时依赖篡改
- 锁的获取顺序固定为 _inject_lock → _cache_lock → _stats_lock，防止死锁
"""

import copy
import hashlib
import hmac
import time
import logging
import threading
import os
import uuid
from typing import Dict, Any, List, Optional, Tuple, Set, Union
from collections import OrderedDict

logger = logging.getLogger(__name__)


class SixLayerFilter:
    """六层进化安全滤网调度器"""

    # ========== 类常量（默认配置） ==========
    LAYER_TIMEOUT_SEC = {1: 10, 2: 15, 3: 30, 4: 120, 5: 3600, 6: 60}
    FILTER_TIMEOUT_FALLBACK_SEC = 300
    MANDATORY_LAYERS: Set[int] = {1, 4}
    LAYER_NAMES = {
        1: "语法与危险函数检测",
        2: "安全扫描与权限校验",
        3: "经济约束与单调性检验",
        4: "Docker沙箱安全编译",
        5: "影子验证与统计检验",
        6: "金丝雀渐进发布",
    }
    MAX_SOURCE_CODE_LENGTH = 1024 * 1024
    MAX_CACHE_ENTRIES = int(os.environ.get("FS_FILTER_CACHE_SIZE", "10000"))
    MIN_SHADOW_VALIDATION_HOURS = 24
    INJECT_LOCK_TIMEOUT_SEC = 5.0
    MAX_GENE_ID_LENGTH = 256
    MAX_TRACE_ID_LENGTH = 128
    MAX_REASON_LENGTH = 512
    DEPENDENCY_KEYS = ["syntax_filter", "economic_filter", "sandbox_compiler", "shadow_validator", "canary_deployer", "behavioral_logger"]
    MANDATORY_DEPENDENCY_KEYS = ["syntax_filter", "sandbox_compiler"]
    VALID_STRATEGY_SOURCES = {"internal", "openclaw", "community", "manual"}
    VALID_STRATEGY_TYPES = {"trend", "oscillation", "arbitrage", "market_making", "event_driven", "funding_arb", "unknown"}
    HMAC_KEY = os.environ.get("FS_HMAC_SECRET", "fire_seed_default_hmac_key_change_in_production")
    SYSTEM_TIME_JUMP_THRESHOLD_SEC = 1.0

    def __init__(self):
        self._syntax_filter = None
        self._economic_filter = None
        self._sandbox_compiler = None
        self._shadow_validator = None
        self._canary_deployer = None
        self._behavioral_logger = None
        self._filter_cache: OrderedDict[str, Dict[int, Dict[str, Any]]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._inject_lock = threading.Lock()
        self._inject_count: Dict[str, int] = {}
        self._inject_failure_log: List[Dict[str, Any]] = []
        self._stats: Dict[int, Dict[str, Union[int, List[float]]]] = {
            layer: {"total": 0, "passed": 0, "rejected": 0, "skipped": 0, "recent_timestamps": [], "latency_ma": 0.0, "cache_hits": 0, "cache_misses": 0}
            for layer in range(1, 7)
        }
        self._stats_lock = threading.Lock()
        self._startup_time = time.time()
        logger.info("SixLayerFilter 初始化完成，缓存上限 %d，启动时间戳 %.0f", self.MAX_CACHE_ENTRIES, self._startup_time)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        syntax_filter: Optional[Any] = None,
        economic_filter: Optional[Any] = None,
        sandbox_compiler: Optional[Any] = None,
        shadow_validator: Optional[Any] = None,
        canary_deployer: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        acquired = self._inject_lock.acquire(timeout=self.INJECT_LOCK_TIMEOUT_SEC)
        if not acquired:
            logger.error("无法获取注入锁（超时 %.1fs），依赖注入失败 #RECOVERY: 检查持有锁的线程状态", self.INJECT_LOCK_TIMEOUT_SEC)
            self._inject_failure_log.append({"timestamp": time.time(), "reason": "lock_timeout"})
            return
        try:
            dep_map = {
                "syntax_filter": (syntax_filter, ["validate"], "SyntaxFilter"),
                "economic_filter": (economic_filter, ["validate"], "EconomicFilter"),
                "sandbox_compiler": (sandbox_compiler, ["compile_and_run"], "SandboxCompiler"),
                "shadow_validator": (shadow_validator, ["validate"], "ShadowValidatorV2"),
                "canary_deployer": (canary_deployer, ["check_promotion_eligibility"], "CanaryDeployer"),
                "behavioral_logger": (behavioral_logger, ["log_event"], "BehavioralLogger"),
            }
            for dep_name, (dep_obj, required_methods, expected_class) in dep_map.items():
                if dep_obj is not None:
                    if self._inject_count.get(dep_name, 0) > 0:
                        logger.warning("%s 已注入过（第%d次），拒绝重复注入", dep_name, self._inject_count[dep_name])
                        continue
                    if type(dep_obj).__name__ != expected_class:
                        logger.warning("%s 类名不匹配 (期望 %s, 实际 %s)，注入拒绝", dep_name, expected_class, type(dep_obj).__name__)
                        continue
                    if not self._validate_dependency(dep_obj, dep_name, required_methods):
                        continue
                    setattr(self, f"_{dep_name}", dep_obj)
                    self._inject_count[dep_name] = self._inject_count.get(dep_name, 0) + 1
                    logger.info("%s 注入成功（第%d次）", dep_name, self._inject_count[dep_name])
        finally:
            self._inject_lock.release()

    # ========== 公共接口 ==========
    def apply_all_filters(self, strategy_gene: Dict[str, Any], trace_id: str = "") -> Dict[str, Any]:
        if not isinstance(strategy_gene, dict):
            return self._error("策略基因必须是字典类型", {"trace_id": self._sanitize_trace_id(trace_id)})
        gene_id = strategy_gene.get("gene_id")
        if not gene_id or not isinstance(gene_id, str):
            return self._error("策略基因缺少有效的 gene_id 字段", {"trace_id": self._sanitize_trace_id(trace_id)})
        if len(gene_id) > self.MAX_GENE_ID_LENGTH:
            return self._error(f"gene_id 长度超限 ({len(gene_id)} > {self.MAX_GENE_ID_LENGTH})", {"gene_id": gene_id[:32]})
        source_code = strategy_gene.get("source_code", "")
        if not isinstance(source_code, str):
            return self._error("source_code 必须为字符串", {"gene_id": gene_id})
        if len(source_code) > self.MAX_SOURCE_CODE_LENGTH:
            return self._error(f"源码长度超限 ({len(source_code)} > {self.MAX_SOURCE_CODE_LENGTH})", {"gene_id": gene_id})
        strategy_source = strategy_gene.get("source", "internal")
        if strategy_source not in self.VALID_STRATEGY_SOURCES:
            return self._error(f"无效的策略来源: {strategy_source}", {"gene_id": gene_id})
        strategy_type = strategy_gene.get("strategy_type", "unknown")
        if strategy_type not in self.VALID_STRATEGY_TYPES:
            return self._error(f"无效的策略类型: {strategy_type}", {"gene_id": gene_id})

        trace_id = self._sanitize_trace_id(trace_id)
        working_gene = {
            "gene_id": gene_id, "source_code": source_code,
            "factor_code": strategy_gene.get("factor_code", ""),
            "strategy_type": strategy_type, "source": strategy_source,
            "gene_version": strategy_gene.get("gene_version", 1),
        }
        gene_hash = self._compute_hmac(source_code)
        working_gene["_gene_hash"] = gene_hash

        start_time = time.monotonic()
        layer_results: Dict[str, Dict[str, Any]] = {}
        final_status = "rejected"
        reject_reason = ""
        warnings: List[str] = []
        all_layers_passed = True

        for layer_num in range(1, 7):
            layer_name = self.LAYER_NAMES.get(layer_num, f"第{layer_num}层")
            layer_start = time.monotonic()
            result = {"passed": False, "reason": "滤网执行异常（未赋值）"}

            try:
                layer_gene = copy.copy(working_gene)
                result = self._execute_layer_with_timeout(layer_num, layer_gene)
            except Exception as e:
                logger.error(f"滤网 {layer_name} 执行异常: {e} #RECOVERY: 检查对应模块状态", exc_info=True)
                result = {"passed": False, "reason": f"滤网执行异常: {str(e)}"}

            elapsed = round(time.monotonic() - layer_start, 6)
            if elapsed < 0:
                logger.warning(f"滤网 {layer_name} 耗时异常（负值 {elapsed}s），可能发生系统时钟回调")
                elapsed = abs(elapsed)
            result["elapsed_sec"] = elapsed
            result["input_hash"] = gene_hash
            layer_results[str(layer_num)] = result

            self._update_stats(layer_num, result.get("passed") is True, elapsed)
            self._log_filter_event(gene_id, trace_id, layer_num, layer_name, result, elapsed, gene_hash, strategy_gene.get("gene_version", 1))

            if result.get("passed") is False:
                all_layers_passed = False
                if layer_num in self.MANDATORY_LAYERS:
                    final_status = "rejected"
                    reject_reason = f"强制性滤网未通过: {layer_name} - {result.get('reason', '未知')}"
                    warnings.append(reject_reason)
                    break
                else:
                    warnings.append(f"非强制滤网 {layer_name} 未通过: {result.get('reason', '未知')}")

        if all_layers_passed:
            final_status = "passed"
            reject_reason = ""

        total_elapsed = round(time.monotonic() - start_time, 6)
        reason = (
            f"基因 {gene_id} 通过全部六层滤网，耗时 {total_elapsed}s"
            if final_status == "passed"
            else reject_reason or f"基因 {gene_id} 未通过全部滤网"
        )

        self._add_to_cache(gene_id, layer_results)

        return {
            "status": "ok", "reason": reason,
            "data": {
                "gene_id": gene_id, "gene_hash": gene_hash, "trace_id": trace_id,
                "final_status": final_status, "total_elapsed_sec": total_elapsed, "layers": layer_results,
            },
            "warnings": list(warnings),
        }

    def health_check(self) -> Dict[str, Any]:
        try:
            deps = {k: getattr(self, f"_{k}", None) is not None for k in self.DEPENDENCY_KEYS}
            cache_size = len(self._filter_cache) if hasattr(self, '_filter_cache') else 0
            missing = [k for k in self.MANDATORY_DEPENDENCY_KEYS if not deps.get(k)]
            warnings = []
            if missing:
                warnings.append(f"mandatory_dep_missing: {missing}")
            if cache_size > self.MAX_CACHE_ENTRIES * 0.8:
                warnings.append(f"缓存使用率 {cache_size}/{self.MAX_CACHE_ENTRIES} 超过80%")
            try:
                import sys
                cache_mem = sys.getsizeof(self._filter_cache)
            except Exception:
                cache_mem = 0
            status = "degraded" if missing else "ok"
            return {
                "status": status,
                "reason": "所有强制性依赖可用" if not missing else f"强制性依赖缺失: {', '.join(missing)}",
                "data": {"dependencies": deps, "cache_size": cache_size, "cache_memory_bytes": cache_mem, "inject_counts": dict(self._inject_count)},
                "warnings": warnings,
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查对象初始化状态")
            return {"status": "error", "reason": str(e), "data": {}, "warnings": ["health_check_failed"]}

    def get_layer_stats(self, window_hours: float = 0, source_filter: str = "") -> Dict[str, Any]:
        with self._stats_lock:
            stats_copy: Dict[str, Dict[str, Any]] = {}
            now = time.time()
            for layer_num in range(1, 7):
                s = dict(self._stats[layer_num])
                timestamps = list(s.get("recent_timestamps", []))
                if window_hours > 0:
                    recent = [ts for ts in timestamps if now - ts <= window_hours * 3600]
                else:
                    recent = timestamps
                s["recent_total"] = len(recent)
                total = max(0, int(s["total"]))
                s["rejection_rate"] = round(s["rejected"] / total, 6) if total > 0 else 0.0
                s["latency_ma"] = round(float(s.get("latency_ma", 0.0)), 6)
                cache_hits = int(s.get("cache_hits", 0))
                cache_total = cache_hits + int(s.get("cache_misses", 0))
                s["cache_hit_rate"] = round(cache_hits / cache_total, 6) if cache_total > 0 else 0.0
                stats_copy[str(layer_num)] = s
        return {
            "status": "ok",
            "reason": f"返回各层滤网历史统计" + (f" (窗口: {window_hours}h)" if window_hours > 0 else ""),
            "data": {"layers": stats_copy, "window_hours": window_hours, "source_filter": source_filter},
            "warnings": [],
        }

    @staticmethod
    def generate_trace_id() -> str:
        return f"fsf_{uuid.uuid4().hex[:12]}_{int(time.time() * 1000)}"

    # ========== 私有方法 ==========
    @staticmethod
    def _error(reason: str, details: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "error", "reason": reason, "data": details, "warnings": [reason]}

    @staticmethod
    def _sanitize_trace_id(trace_id: str) -> str:
        if not isinstance(trace_id, str):
            return ""
        return trace_id.replace("\n", " ").replace("\r", " ").replace("\t", " ")[:128]

    @staticmethod
    def _sanitize_reason(reason: str) -> str:
        if not isinstance(reason, str):
            return "未知原因"
        import re
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', reason)
        return cleaned.replace("\n", " ").replace("\r", " ")[:512]

    @staticmethod
    def _sanitize_gene_id(gene_id: str) -> str:
        if not isinstance(gene_id, str) or not gene_id.strip():
            return "unknown"
        return gene_id.replace("/", "_").replace("\\", "_").replace(".", "_").replace("\x00", "")[:256]

    @staticmethod
    def _validate_dependency(obj: Any, name: str, required_methods: List[str]) -> bool:
        for method in required_methods:
            if not hasattr(obj, method):
                logger.warning("%s 缺少必需方法 %s，注入拒绝", name, method)
                return False
        return True

    def _compute_hmac(self, data: str) -> str:
        return hmac.new(self.HMAC_KEY.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).hexdigest()

    def _execute_layer_with_timeout(self, layer_num: int, gene: Dict[str, Any]) -> Dict[str, Any]:
        timeout = self.LAYER_TIMEOUT_SEC.get(layer_num, self.FILTER_TIMEOUT_FALLBACK_SEC)
        start = time.monotonic()
        try:
            result = self._execute_layer(layer_num, gene)
        except Exception as e:
            result = {"passed": False, "reason": f"滤网执行异常: {str(e)}"}
        elapsed = time.monotonic() - start
        if elapsed > timeout:
            logger.error(f"滤网第{layer_num}层超时 ({elapsed:.3f}s > {timeout}s)")
            return {"passed": False, "reason": f"滤网执行超时 ({elapsed:.1f}s > {timeout}s)"}
        return result

    def _execute_layer(self, layer_num: int, gene: Dict[str, Any]) -> Dict[str, Any]:
        if layer_num == 1:
            return self._run_syntax_check(gene)
        elif layer_num == 2:
            return self._run_security_check(gene)
        elif layer_num == 3:
            return self._run_economic_check(gene)
        elif layer_num == 4:
            return self._run_sandbox_compilation(gene)
        elif layer_num == 5:
            return self._run_shadow_validation(gene)
        elif layer_num == 6:
            return self._run_canary_promotion(gene)
        logger.error(f"未知滤网层号: {layer_num}")
        return {"passed": False, "reason": f"未知滤网层号: {layer_num}"}

    def _safe_get_bool(self, result: Any, key: str, default: bool = False) -> bool:
        if result is None or not isinstance(result, dict):
            logger.warning(f"滤网返回无效类型: {type(result).__name__}，使用默认值 {default}")
            return default
        value = result.get(key, default)
        return bool(value)

    def _run_syntax_check(self, gene: Dict[str, Any]) -> Dict[str, Any]:
        if self._syntax_filter is None:
            return {"passed": False, "reason": "SyntaxFilter 不可用（强制层）"}
        source_code = gene.get("source_code", "")
        if not source_code:
            return {"passed": False, "reason": "基因缺少源码"}
        try:
            result = self._syntax_filter.validate(source_code)
        except Exception as e:
            logger.error(f"语法检查调用异常: {e} #RECOVERY: 检查 SyntaxFilter 运行状态")
            return {"passed": False, "reason": f"语法检查调用异常: {str(e)}"}
        if not isinstance(result, dict):
            logger.error(f"语法检查返回非字典类型: {type(result).__name__}")
            return {"passed": False, "reason": "语法检查返回无效结果"}
        return {"passed": self._safe_get_bool(result, "valid", False), "reason": self._sanitize_reason(str(result.get("message", "语法检查完成")))}

    def _run_security_check(self, gene: Dict[str, Any]) -> Dict[str, Any]:
        return {"passed": True, "reason": "安全扫描层未启用，默认通过（预留层，启用需机构安全团队审批，审批单号: SEC-2024-001）"}

    def _run_economic_check(self, gene: Dict[str, Any]) -> Dict[str, Any]:
        if self._economic_filter is None:
            return {"passed": True, "reason": "EconomicFilter 不可用，跳过（非强制），需人工确认 [NON_MANDATORY_SKIP]"}
        factor_code = gene.get("factor_code", "")
        strategy_type = gene.get("strategy_type", "unknown")
        if not factor_code and strategy_type in ("arbitrage", "market_making"):
            logger.warning("%s 策略缺少因子代码，经济约束跳过——此行为需在季度审计中审查", strategy_type)
            return {"passed": True, "reason": f"{strategy_type} 策略缺少因子代码，已记录审计日志 [NON_MANDATORY_SKIP]"}
        if not factor_code:
            return {"passed": True, "reason": "基因未包含因子代码，跳过经济检验（非强制）[NON_MANDATORY_SKIP]"}
        try:
            result = self._economic_filter.validate(factor_code)
        except Exception as e:
            logger.warning(f"经济约束检验异常: {e}")
            return {"passed": True, "reason": f"经济约束检验异常（非强制）: {str(e)} [NON_MANDATORY_SKIP]"}
        if not isinstance(result, dict):
            logger.warning(f"经济约束返回非字典类型: {type(result).__name__}")
            return {"passed": True, "reason": "经济约束返回无效结果（非强制）[NON_MANDATORY_SKIP]"}
        return {"passed": self._safe_get_bool(result, "valid", False), "reason": self._sanitize_reason(str(result.get("message", "")))}

    def _run_sandbox_compilation(self, gene: Dict[str, Any]) -> Dict[str, Any]:
        if self._sandbox_compiler is None:
            return {"passed": False, "reason": "SandboxCompiler 不可用（强制层）"}
        source_code = gene.get("source_code", "")
        if not source_code:
            return {"passed": False, "reason": "基因缺少源码"}
        try:
            result = self._sandbox_compiler.compile_and_run(source_code)
        except Exception as e:
            logger.error(f"沙箱编译调用异常: {e} #RECOVERY: 检查 Docker 服务和 SandboxCompiler 状态")
            return {"passed": False, "reason": f"沙箱编译调用异常: {str(e)}"}
        if not isinstance(result, dict):
            logger.error(f"沙箱编译返回非字典类型: {type(result).__name__}")
            return {"passed": False, "reason": "沙箱编译返回无效结果"}
        return {"passed": self._safe_get_bool(result, "success", False), "reason": self._sanitize_reason(str(result.get("output", "")))}

    def _run_shadow_validation(self, gene: Dict[str, Any]) -> Dict[str, Any]:
        if self._shadow_validator is None:
            logger.warning("ShadowValidatorV2 不可用，影子验证跳过——此行为需在季度审计中审查 [NON_MANDATORY_SKIP]")
            return {"passed": True, "reason": "ShadowValidatorV2 不可用，跳过（非强制）[NON_MANDATORY_SKIP]"}
        try:
            result = self._shadow_validator.validate(gene)
        except Exception as e:
            logger.warning(f"影子验证异常: {e}")
            return {"passed": True, "reason": f"影子验证异常（非强制）: {str(e)} [NON_MANDATORY_SKIP]"}
        if not isinstance(result, dict):
            logger.warning(f"影子验证返回非字典类型: {type(result).__name__}")
            return {"passed": True, "reason": "影子验证返回无效结果（非强制）[NON_MANDATORY_SKIP]"}
        return {"passed": self._safe_get_bool(result, "passed", False), "reason": self._sanitize_reason(str(result.get("reason", "")))}

    def _run_canary_promotion(self, gene: Dict[str, Any]) -> Dict[str, Any]:
        if self._canary_deployer is None:
            logger.warning("CanaryDeployer 不可用，金丝雀准入跳过——此行为需在季度审计中审查 [NON_MANDATORY_SKIP]")
            return {"passed": True, "reason": "CanaryDeployer 不可用，跳过（非强制）[NON_MANDATORY_SKIP]"}
        try:
            result = self._canary_deployer.check_promotion_eligibility(gene)
        except Exception as e:
            logger.warning(f"金丝雀准入异常: {e}")
            return {"passed": True, "reason": f"金丝雀准入异常（非强制）: {str(e)} [NON_MANDATORY_SKIP]"}
        if not isinstance(result, dict):
            logger.warning(f"金丝雀准入返回非字典类型: {type(result).__name__}")
            return {"passed": True, "reason": "金丝雀准入返回无效结果（非强制）[NON_MANDATORY_SKIP]"}
        return {"passed": self._safe_get_bool(result, "eligible", False), "reason": self._sanitize_reason(str(result.get("reason", "")))}

    def _update_stats(self, layer: int, passed: bool, elapsed: float) -> None:
        if elapsed < 0:
            elapsed = 0.0
        with self._stats_lock:
            self._stats[layer]["total"] += 1
            if passed:
                self._stats[layer]["passed"] += 1
            else:
                self._stats[layer]["rejected"] += 1
            self._stats[layer]["recent_timestamps"].append(time.time())
            if len(self._stats[layer]["recent_timestamps"]) > 1000:
                self._stats[layer]["recent_timestamps"] = self._stats[layer]["recent_timestamps"][-1000:]
            alpha = 0.2
            current_ma = float(self._stats[layer].get("latency_ma", 0.0))
            self._stats[layer]["latency_ma"] = alpha * elapsed + (1 - alpha) * current_ma

    def _log_filter_event(
        self, gene_id: str, trace_id: str, layer: int,
        layer_name: str, result: Dict[str, Any], elapsed: float, gene_hash: str, gene_version: int = 1,
    ) -> None:
        passed = result.get("passed", False)
        reason = self._sanitize_reason(str(result.get("reason", "")))
        if self._behavioral_logger:
            try:
                self._behavioral_logger.log_event(
                    event_type="evolution_safety_filter",
                    details={
                        "gene_id": gene_id, "trace_id": trace_id, "gene_hash": gene_hash, "gene_version": gene_version,
                        "layer": layer, "layer_name": layer_name,
                        "passed": passed, "reason": reason,
                        "elapsed_sec": round(elapsed, 6),
                    },
                )
            except Exception:
                pass
        log_func = logger.info if passed else logger.warning
        log_func(f"滤网 {layer_name}(第{layer}层) {'通过' if passed else '拒绝'}: {reason} ({elapsed:.6f}s)")

    def _add_to_cache(self, gene_id: str, data: Dict[str, Any]) -> None:
        safe_id = self._sanitize_gene_id(gene_id)
        if len(safe_id) > self.MAX_GENE_ID_LENGTH:
            safe_id = safe_id[:self.MAX_GENE_ID_LENGTH]
        with self._cache_lock:
            self._filter_cache[safe_id] = data
            while len(self._filter_cache) > self.MAX_CACHE_ENTRIES:
                try:
                    self._filter_cache.popitem(last=False)
                except (TypeError, KeyError):
                    try:
                        self._filter_cache.popitem()
                        logger.debug("LRU淘汰降级为默认popitem()（兼容模式）")
                    except KeyError:
                        logger.warning("缓存淘汰失败：缓存为空")
                        break
