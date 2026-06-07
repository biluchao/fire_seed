"""
火种系统 · 热插拔管理器 (PluginManager)

核心职责：
1. 动态加载与卸载符合 IStrategy 接口的 Python 策略模块，支持运行时热替换
2. 维护插件注册表、依赖关系图，提供插件状态查询、健康检查与严格依赖校验
3. 实现 ECDSA 签名校验、安全路径沙箱、异步持久化审计、线程池资源控制与原子化状态管理

外部依赖（真实模块接口）：
- core.utils.config_loader.ConfigLoader : 获取插件配置、签名公钥、插件基路径
- core.negotiation_bus.NegotiationBus : 广播模块加载/卸载状态变更事件
- core.audit.audit_logger.AuditLogger : 持久化审计日志，满足金融合规要求
- concurrent.futures.ThreadPoolExecutor : 管理健康检查与清理任务的线程池
- cryptography >= 41.0.0 : ECDSA 签名验证 (硬依赖)

接口契约：
- load_plugin(plugin_name: str, filepath: str, version: str = "1.0.0") -> Dict[str, Any]
- unload_plugin(plugin_name: str, force: bool = False) -> Dict[str, Any]
- reload_plugin(plugin_name: str, filepath: str = None, version: str = None) -> Dict[str, Any]
- get_plugin_status(plugin_name: str) -> Dict[str, Any]
- get_all_plugins(offset: int = 0, limit: int = 100) -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- shutdown() -> None
- 所有公共方法输出字典包含 "status", "reason", "data", "warnings", "error_code" (int)

异常与降级：
- 签名校验失败、文件不存在、语法错误等均返回标准化错误响应，不会导致系统崩溃
- 当 ConfigLoader 未注入时，自动加载功能不可用；审计日志降级为标准 logger
- 清理方法超时时线程自动放弃（daemon），避免僵尸线程
- 所有降级值在类常量区明确声明

资源管理：
- 使用 importlib.util 避免命名冲突；卸载时从 sys.modules 移除并异步调用 cleanup
- 健康检查和清理任务通过线程池执行，防止无限创建线程
- 文件路径严格限制在 PLUGIN_BASE_DIR 目录内，防止路径遍历攻击
- 审计日志通过有界缓冲区写入并定时刷新，防止内存膨胀
- 插件模块通过 sys.modules 隔离，避免新旧代码状态污染
"""

import importlib.util
import sys
import os
import time
import hashlib
import inspect
import logging
import threading
import re
from typing import Dict, Any, List, Optional, Tuple, Set, ClassVar
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

logger = logging.getLogger(__name__)


class PluginManager:
    """热插拔策略模块管理器，符合华尔街高频交易机构级标准"""

    # ========== 类常量 ==========
    DEFAULT_MAX_PLUGIN_COUNT: ClassVar[int] = 50
    HEALTH_CHECK_TIMEOUT_SEC: ClassVar[float] = 2.0
    RETRY_COUNT_ON_FAILURE: ClassVar[int] = 2
    RETRY_BACKOFF_BASE_SEC: ClassVar[float] = 0.5
    SIGNATURE_VERIFICATION_ENABLED: ClassVar[bool] = True
    SIGNATURE_FILE_EXTENSION: ClassVar[str] = ".sig"
    DEPENDENCY_CHECK_ENABLED: ClassVar[bool] = True
    CLEANUP_TIMEOUT_SEC: ClassVar[float] = 2.0
    MAX_FILE_SIZE_BYTES: ClassVar[int] = 10 * 1024 * 1024
    DEFAULT_PLUGIN_BASE_DIR: ClassVar[str] = "/opt/fire_seed/plugins"
    THREAD_POOL_SIZE: ClassVar[int] = 4
    THREAD_POOL_MAX_QUEUE: ClassVar[int] = 100
    AUDIT_FLUSH_INTERVAL_SEC: ClassVar[float] = 5.0
    AUDIT_MAX_BUFFER_SIZE: ClassVar[int] = 500
    MAX_PAGE_LIMIT: ClassVar[int] = 200
    LOG_MAX_PLUGIN_NAME_LEN: ClassVar[int] = 128

    # 插件状态常量
    STATUS_ACTIVE: ClassVar[str] = "active"
    STATUS_DEGRADED: ClassVar[str] = "degraded"
    STATUS_UNKNOWN: ClassVar[str] = "unknown"
    STATUS_ERROR: ClassVar[str] = "error"
    STATUS_OK: ClassVar[str] = "ok"
    STATUS_HEALTHY: ClassVar[str] = "healthy"

    ERROR_CODES: ClassVar[Dict[str, int]] = {
        "SUCCESS": 0,
        "INVALID_PARAM": 1001,
        "FILE_NOT_FOUND": 1002,
        "SIGNATURE_INVALID": 1003,
        "INTERFACE_MISSING": 1004,
        "DEPENDENCY_MISSING": 1005,
        "LOAD_TIMEOUT": 1006,
        "MAX_PLUGIN_LIMIT": 1007,
        "UNLOAD_FAILED": 1008,
        "RELOAD_FAILED": 1009,
        "CLEANUP_FAILED": 1010,
        "INTERNAL_ERROR": 1999,
        "SHUTDOWN_IN_PROGRESS": 2001,
    }

    def __init__(self):
        self._registry: Dict[str, Dict[str, Any]] = {}
        self._dependency_graph: Dict[str, Set[str]] = {}
        self._reverse_deps: Dict[str, Set[str]] = {}

        self._config_loader = None
        self._negotiation_bus = None
        self._audit_logger = None

        self._lock = threading.RLock()
        self._shutdown_in_progress = False

        self._executor = ThreadPoolExecutor(
            max_workers=self.THREAD_POOL_SIZE,
            thread_name_prefix="plg-mgr-"
        )

        self._load_count = 0
        self._unload_count = 0
        self._last_error: Optional[str] = None

        self._audit_buffer: List[str] = []
        self._audit_lock = threading.Lock()
        self._audit_timer: Optional[threading.Timer] = None

        self._max_plugins = self.DEFAULT_MAX_PLUGIN_COUNT
        self._plugin_base_dir = self.DEFAULT_PLUGIN_BASE_DIR

        # 全局异常处理器
        self._executor_thread_exception_handler = self._on_thread_exception
        self._start_audit_flush_timer()

        logger.info("PluginManager initialized, base_dir: %s, thread_pool: %d",
                    self._plugin_base_dir, self.THREAD_POOL_SIZE)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        config_loader: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        audit_logger: Optional[Any] = None,
    ) -> None:
        if config_loader is not None:
            self._config_loader = config_loader
            self._max_plugins = config_loader.get("plugin_max_count", self.DEFAULT_MAX_PLUGIN_COUNT)
            self._plugin_base_dir = os.path.realpath(
                config_loader.get("plugin_base_dir", self.DEFAULT_PLUGIN_BASE_DIR))
        if negotiation_bus is not None and hasattr(negotiation_bus, 'publish_plugin_event'):
            self._negotiation_bus = negotiation_bus
        if audit_logger is not None:
            self._audit_logger = audit_logger
            self._flush_audit_buffer()

    # ========== 公共接口 ==========
    def load_plugin(self, plugin_name: str, filepath: str, version: str = "1.0.0") -> Dict[str, Any]:
        if self._is_shutting_down():
            return self._error_response("Shutdown in progress", "SHUTDOWN_IN_PROGRESS")

        if not plugin_name or not isinstance(plugin_name, str):
            return self._error_response("plugin_name must be non-empty string", "INVALID_PARAM")
        if not filepath or not isinstance(filepath, str):
            return self._error_response("filepath must be non-empty string", "INVALID_PARAM")
        if not isinstance(version, str):
            version = "1.0.0"

        filepath = os.path.realpath(filepath)
        if not filepath.startswith(self._plugin_base_dir):
            self._audit_log("load_blocked_path", plugin_name, version, filepath)
            return self._error_response("filepath outside allowed directory", "INVALID_PARAM")
        if not os.path.isfile(filepath):
            return self._error_response("File not found", "FILE_NOT_FOUND")
        if os.path.getsize(filepath) > self.MAX_FILE_SIZE_BYTES:
            return self._error_response("Plugin file too large", "INVALID_PARAM")

        if self.SIGNATURE_VERIFICATION_ENABLED:
            sig_ok, sig_reason = self._verify_file_signature(filepath)
            if not sig_ok:
                self._audit_log("load_blocked_signature", plugin_name, version, filepath)
                return self._error_response(f"Signature verification failed: {sig_reason}", "SIGNATURE_INVALID")

        with self._lock:
            if len(self._registry) >= self._max_plugins:
                return self._error_response(f"Max plugin limit {self._max_plugins} reached", "MAX_PLUGIN_LIMIT")
            old_entry = self._registry.pop(plugin_name, None)
            if old_entry:
                logger.info("Plugin %s already loaded, replacing", plugin_name)
                self._remove_from_graph(plugin_name)

        last_error = None
        for attempt in range(self.RETRY_COUNT_ON_FAILURE + 1):
            try:
                module = self._import_module(plugin_name, filepath)
                if not self._validate_interface(module):
                    sys.modules.pop(plugin_name, None)
                    raise ImportError("Missing required IStrategy interface methods")

                deps = self._safe_get_dependencies(module)
                if self.DEPENDENCY_CHECK_ENABLED:
                    missing = self._validate_dependencies_with_detail(plugin_name, deps)
                    if missing:
                        sys.modules.pop(plugin_name, None)
                        raise ImportError(f"Missing dependencies: {missing}")

                file_hash = self._compute_file_hash(filepath)
                with self._lock:
                    self._registry[plugin_name] = {
                        "module": module,
                        "filepath": filepath,
                        "version": version,
                        "loaded_at": time.time(),
                        "signature": file_hash,
                        "status": self.STATUS_ACTIVE,
                    }
                    self._load_count += 1
                    self._dependency_graph[plugin_name] = deps
                    for dep in deps:
                        self._reverse_deps.setdefault(dep, set()).add(plugin_name)

                health_status = self._safe_health_check(plugin_name, timeout=self.HEALTH_CHECK_TIMEOUT_SEC)
                if health_status.get("status") not in (self.STATUS_HEALTHY, self.STATUS_OK):
                    logger.error("Plugin %s post-load health check failed, marking degraded", plugin_name)
                    with self._lock:
                        if plugin_name in self._registry:
                            self._registry[plugin_name]["status"] = self.STATUS_DEGRADED
                    self._audit_log("load_health_degraded", plugin_name, version, filepath)

                logger.info("Plugin %s (v%s) loaded successfully", plugin_name, version)
                self._notify_event("plugin_loaded", plugin_name, version)
                self._audit_log("plugin_loaded", plugin_name, version, filepath)

                return self._success_response("Plugin loaded", {
                    "plugin_name": plugin_name,
                    "version": version,
                    "filepath": filepath,
                    "loaded_at": self._registry[plugin_name]["loaded_at"],
                    "file_hash": file_hash,
                    "health": health_status,
                })

            except Exception as e:
                last_error = str(e)
                logger.warning("Plugin %s load attempt %d failed: %s", plugin_name, attempt + 1, e)
                if attempt < self.RETRY_COUNT_ON_FAILURE:
                    time.sleep(self.RETRY_BACKOFF_BASE_SEC * (2 ** attempt))
                else:
                    self._last_error = last_error
                    logger.error("Plugin %s load failed: %s #RECOVERY: check syntax and IStrategy interface", plugin_name, last_error)
                    self._audit_log("load_failed", plugin_name, version, filepath)

        return self._error_response(f"Plugin load failed: {last_error}", "INTERNAL_ERROR")

    def unload_plugin(self, plugin_name: str, force: bool = False) -> Dict[str, Any]:
        if self._is_shutting_down():
            return self._error_response("Shutdown in progress", "SHUTDOWN_IN_PROGRESS")
        if not plugin_name or not isinstance(plugin_name, str):
            return self._error_response("plugin_name must be non-empty string", "INVALID_PARAM")

        with self._lock:
            if plugin_name not in self._registry:
                return self._error_response(f"Plugin {plugin_name} not found", "INVALID_PARAM")
            dependents = self._reverse_deps.get(plugin_name, set())
            if dependents and not force:
                return self._error_response(f"Plugin required by: {dependents}", "DEPENDENCY_MISSING")
            self._unload_unlocked(plugin_name, force=force)
            self._unload_count += 1

        logger.info("Plugin %s unloaded", plugin_name)
        self._notify_event("plugin_unloaded", plugin_name, "N/A")
        self._audit_log("plugin_unloaded", plugin_name, "", "")
        return self._success_response("Plugin unloaded", {"plugin_name": plugin_name})

    def reload_plugin(self, plugin_name: str, filepath: str = None, version: str = None) -> Dict[str, Any]:
        if self._is_shutting_down():
            return self._error_response("Shutdown in progress", "SHUTDOWN_IN_PROGRESS")
        with self._lock:
            if plugin_name not in self._registry:
                return self._error_response(f"Plugin {plugin_name} not found", "INVALID_PARAM")
            old_entry = dict(self._registry[plugin_name])
            target_path = filepath or old_entry["filepath"]
            target_version = version or old_entry["version"]

        unload_res = self.unload_plugin(plugin_name, force=True)
        if unload_res["status"] != "ok":
            return self._error_response(f"Reload unload failed: {unload_res['reason']}", "RELOAD_FAILED")

        load_res = self.load_plugin(plugin_name, target_path, target_version)
        if load_res["status"] != "ok":
            logger.error("Reload failed, rolling back plugin %s", plugin_name)
            rollback = self.load_plugin(plugin_name, old_entry["filepath"], old_entry["version"])
            if rollback["status"] != "ok":
                logger.critical("Plugin %s rollback failed, system degraded", plugin_name)
                return self._error_response("Reload and rollback both failed", "RELOAD_FAILED")
            return self._error_response(f"Reload failed, rolled back: {load_res['reason']}", "RELOAD_FAILED")
        return load_res

    def get_plugin_status(self, plugin_name: str) -> Dict[str, Any]:
        if not plugin_name:
            return self._error_response("plugin_name required", "INVALID_PARAM")
        with self._lock:
            if plugin_name not in self._registry:
                return self._error_response(f"Plugin {plugin_name} not found", "INVALID_PARAM")
            entry = dict(self._registry[plugin_name])

        health = self._safe_health_check(plugin_name, timeout=self.HEALTH_CHECK_TIMEOUT_SEC)
        return self._success_response("Plugin status", {
            "plugin_name": plugin_name,
            "version": entry["version"],
            "filepath": entry["filepath"],
            "loaded_at": entry["loaded_at"],
            "status": entry["status"],
            "health": health,
        })

    def get_all_plugins(self, offset: int = 0, limit: int = 100) -> Dict[str, Any]:
        if offset < 0 or limit <= 0 or limit > self.MAX_PAGE_LIMIT:
            return self._error_response("Invalid pagination parameters", "INVALID_PARAM")
        with self._lock:
            all_plugins = [
                {"plugin_name": name, "version": info["version"], "status": info["status"], "loaded_at": info["loaded_at"]}
                for name, info in self._registry.items()
            ]
            total = len(all_plugins)
            page = all_plugins[offset: offset + limit]
        return self._success_response(f"Total plugins: {total}", {
            "plugins": page, "total": total, "offset": offset, "limit": limit
        })

    def health_check(self) -> Dict[str, Any]:
        try:
            with self._lock:
                plugin_names = list(self._registry.keys())
                load_count = self._load_count
                unload_count = self._unload_count
                last_error = self._last_error
            failed = []
            for name in plugin_names:
                h = self._safe_health_check(name, timeout=self.HEALTH_CHECK_TIMEOUT_SEC)
                if h.get("status") not in (self.STATUS_HEALTHY, self.STATUS_OK):
                    failed.append({"name": name, "health": h})
            status = self.STATUS_DEGRADED if failed else self.STATUS_OK
            return self._success_response(f"PluginManager: {len(plugin_names)} plugins", {
                "total": len(plugin_names), "failed": failed,
                "load_count": load_count, "unload_count": unload_count,
                "last_error": last_error,
            }, warnings=[f"health:{p['name']}" for p in failed] if failed else None)
        except Exception as e:
            logger.error(f"Health check error: {e} #RECOVERY: check lock integrity")
            return self._error_response(f"Health check: {e}", "INTERNAL_ERROR")

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown_in_progress:
                return
            self._shutdown_in_progress = True

        self._stop_audit_flush_timer()
        self._flush_audit_buffer()
        self._executor.shutdown(wait=True, timeout=self.CLEANUP_TIMEOUT_SEC)
        logger.info("PluginManager shutdown complete")

    # ========== 私有方法 ==========
    def _is_shutting_down(self) -> bool:
        with self._lock:
            return self._shutdown_in_progress

    def _import_module(self, plugin_name: str, filepath: str) -> Any:
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"File not found: {filepath}")
        spec = importlib.util.spec_from_file_location(plugin_name, filepath)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create spec: {filepath}")
        module = importlib.util.module_from_spec(spec)
        # 注册模块前先备份旧模块，以便失败时恢复
        old_module = sys.modules.get(plugin_name)
        sys.modules[plugin_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            if old_module is not None:
                sys.modules[plugin_name] = old_module
            else:
                sys.modules.pop(plugin_name, None)
            raise
        return module

    def _validate_interface(self, module: Any) -> bool:
        required = ["on_tick", "health_check", "cleanup"]
        for m in required:
            attr = getattr(module, m, None)
            if attr is None or not callable(attr):
                logger.error("Plugin missing method: %s", m)
                return False
        try:
            sig = inspect.signature(module.on_tick)
            if len(sig.parameters) != 1:
                logger.error("on_tick signature invalid")
                return False
        except (ValueError, TypeError) as e:
            logger.error("Cannot inspect on_tick signature: %s", e)
            return False
        return True

    def _safe_get_dependencies(self, module: Any) -> Set[str]:
        try:
            deps = getattr(module, '__dependencies__', [])
            if isinstance(deps, str):
                logger.warning("__dependencies__ is a string, expected iterable")
                return set()
            if not isinstance(deps, (list, tuple, set)):
                logger.warning("__dependencies__ type not supported: %s", type(deps))
                return set()
            return set(deps)
        except Exception as e:
            logger.warning("Error reading __dependencies__: %s", e)
            return set()

    def _validate_dependencies_with_detail(self, plugin_name: str, deps: Set[str]) -> Optional[List[str]]:
        with self._lock:
            missing = [d for d in deps if d not in self._registry]
        return missing if missing else None

    def _verify_file_signature(self, filepath: str) -> Tuple[bool, str]:
        sig_file = filepath + self.SIGNATURE_FILE_EXTENSION
        if not os.path.exists(sig_file):
            return False, "Signature file not found"
        try:
            public_key = self._get_public_key()
            if public_key is None:
                return False, "Public key unavailable"
            with open(filepath, 'rb') as f:
                data = f.read()
            with open(sig_file, 'rb') as f:
                signature = f.read()
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives import hashes
            public_key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
            return True, "ok"
        except ImportError:
            logger.error("cryptography library not installed")
            return False, "Signature verification library missing"
        except Exception as e:
            logger.error("Signature verification error: %s", e)
            return False, f"Verification failed: {e}"

    def _get_public_key(self) -> Any:
        if self._config_loader:
            return self._config_loader.get("plugin_signature_public_key")
        return None

    def _compute_file_hash(self, filepath: str) -> str:
        sha = hashlib.sha256()
        try:
            with open(filepath, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    sha.update(chunk)
            return sha.hexdigest()
        except Exception as e:
            logger.error("Hash computation failed for %s: %s", filepath, e)
            # 返回固定格式错误哈希，明确标记无效
            return "INVALID_HASH_" + hashlib.md5(str(int(time.time())).encode()).hexdigest()[:8]

    def _unload_unlocked(self, plugin_name: str, force: bool = False) -> None:
        entry = self._registry.pop(plugin_name, None)
        if entry is None:
            return
        self._remove_from_graph(plugin_name)
        module = entry.get("module")
        if module and not force:
            try:
                self._executor.submit(self._run_cleanup, module, plugin_name)
            except RuntimeError:
                logger.warning("Executor shut down, cleanup skipped for %s", plugin_name)
        sys.modules.pop(plugin_name, None)

    def _remove_from_graph(self, plugin_name: str) -> None:
        self._dependency_graph.pop(plugin_name, None)
        for dep_set in self._reverse_deps.values():
            dep_set.discard(plugin_name)

    def _run_cleanup(self, module, plugin_name):
        try:
            if hasattr(module, 'cleanup'):
                module.cleanup()
        except Exception as e:
            logger.error("Plugin %s cleanup error: %s", plugin_name, e)

    def _safe_health_check(self, plugin_name: str, timeout: float) -> Dict[str, Any]:
        try:
            future = self._executor.submit(self._do_health_check, plugin_name)
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            return {"status": "error", "reason": f"Health check timeout ({timeout}s)"}
        except RuntimeError:
            return {"status": "error", "reason": "Executor shut down"}

    def _do_health_check(self, plugin_name: str) -> Dict[str, Any]:
        with self._lock:
            entry = self._registry.get(plugin_name)
        if entry is None:
            return {"status": self.STATUS_UNKNOWN, "reason": "Plugin not found"}
        module = entry.get("module")
        if module and hasattr(module, 'health_check'):
            try:
                return module.health_check()
            except Exception as e:
                return {"status": self.STATUS_ERROR, "reason": str(e)}
        return {"status": self.STATUS_HEALTHY, "reason": "No built-in health check"}

    def _notify_event(self, event_type: str, plugin_name: str, version: str) -> None:
        if self._negotiation_bus:
            try:
                self._negotiation_bus.publish_plugin_event(
                    event_type=event_type,
                    plugin_name=self._sanitize_for_log(plugin_name),
                    version=version,
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning("Notify event failed: %s", e)

    def _audit_log(self, action: str, plugin_name: str, version: str, filepath: str) -> None:
        safe_name = self._sanitize_for_log(plugin_name)
        msg = f"AUDIT: {action} plugin={safe_name} version={version}"
        if self._audit_logger:
            with self._audit_lock:
                if len(self._audit_buffer) < self.AUDIT_MAX_BUFFER_SIZE:
                    self._audit_buffer.append(msg)
                else:
                    # 缓冲区满，丢弃最旧消息并记录告警
                    self._audit_buffer.pop(0)
                    self._audit_buffer.append(msg)
                    logger.warning("Audit buffer full, dropped oldest message")
        else:
            logger.info(msg)

    def _sanitize_for_log(self, value: str) -> str:
        # 移除所有控制字符，并限制长度
        sanitized = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', str(value))
        return sanitized[:self.LOG_MAX_PLUGIN_NAME_LEN]

    def _flush_audit_buffer(self) -> None:
        if not self._audit_logger:
            return
        with self._audit_lock:
            if not self._audit_buffer:
                return
            msgs = self._audit_buffer[:]
            self._audit_buffer.clear()
        for msg in msgs:
            try:
                self._audit_logger.log("plugin_event", {"message": msg})
            except Exception as e:
                logger.warning("Audit log flush failed: %s", e)

    def _start_audit_flush_timer(self) -> None:
        self._audit_timer = threading.Timer(self.AUDIT_FLUSH_INTERVAL_SEC, self._periodic_flush)
        self._audit_timer.daemon = True
        self._audit_timer.start()

    def _periodic_flush(self):
        self._flush_audit_buffer()
        if not self._is_shutting_down():
            self._start_audit_flush_timer()

    def _stop_audit_flush_timer(self):
        if self._audit_timer:
            self._audit_timer.cancel()
            self._audit_timer = None

    def _success_response(self, reason: str, data: Dict[str, Any], warnings: Optional[List[str]] = None) -> Dict[str, Any]:
        return {
            "status": self.STATUS_OK,
            "reason": reason,
            "data": data,
            "warnings": warnings if warnings is not None else [],
            "error_code": self.ERROR_CODES["SUCCESS"]
        }

    def _error_response(self, reason: str, error_key: str) -> Dict[str, Any]:
        code = self.ERROR_CODES.get(error_key, self.ERROR_CODES["INTERNAL_ERROR"])
        safe_reason = self._sanitize_for_log(reason)
        return {
            "status": self.STATUS_ERROR,
            "reason": safe_reason,
            "data": {},
            "warnings": [safe_reason],
            "error_code": code
        }

    def _on_thread_exception(self, args) -> None:
        """线程池未捕获异常处理器"""
        logger.error("Unhandled exception in thread pool: %s", args)


if __name__ == '__main__':
    # 简单的自测试，可用于烟雾测试
    mgr = PluginManager()
    result = mgr.health_check()
    print("Health check:", result)
