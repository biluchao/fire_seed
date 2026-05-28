"""
火种系统 · 流水线上下文传递器 (ContextPasser)

核心职责：
1. 将流水线阶段产生的处理结果封装为标准上下文数据包，附带硬件时间戳与校验和
2. 在下一阶段接收数据包时进行完整性校验，确保数据在传递过程中未被篡改或错位

外部依赖（真实模块接口）：
- 无外部自定义模块依赖，仅使用 Python 标准库 (hashlib, json, time, logging, pickle)

接口契约：
- create_packet(stage_name: str, payload: Dict[str, Any]) -> Dict[str, Any] : 创建标准化上下文数据包
- validate_packet(packet: Dict[str, Any]) -> Dict[str, Any] : 校验数据包完整性并返回原始载荷
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当输入的 payload 不是字典类型时，自动替换为空字典并记录 warning
- 当 payload 无法被序列化时，返回错误状态，不生成数据包
- 硬件时间戳获取失败时，降级使用 time.time() 并记录 warning
- 数据包缺少 algorithm 字段时，回退至类常量默认算法，并记录 warning
- 当数据包 algorithm 值不在已知合法列表中时，尝试使用默认算法重试，并记录 warning

资源管理：
- 本模块为纯工具类，不持有任何外部资源，无需手动释放
- 性能敏感路径支持原始字节模式，避免重复序列化开销
"""

import time
import hashlib
import json
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class ContextPasser:
    """流水线上下文传递器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_HASH_ALGORITHM = "sha256"       # 默认校验和算法，字符串，可选 "sha256", "sha1", "md5"

    # 当前系统接受的合法校验算法列表（向后兼容）
    VALID_ALGORITHMS: List[str] = ["sha256", "sha1", "md5"]

    # 性能优化：是否优先使用原始字节模式计算校验和（避免 JSON 序列化开销）
    # 仅当 payload 中的数据结构能够被 repr 或 pickle 稳定还原时启用
    USE_RAW_CHECKSUM: bool = True           # 布尔值，True 表示优先使用原始字节模式

    # 原始字节模式的序列化方法：可选 "repr" 或 "pickle"
    # "repr" 速度最快但要求 payload 中的数据均支持明确的 repr 输出
    # "pickle" 更通用但略慢于 repr，注意 pickle 在不同 Python 版本间可能不兼容
    RAW_CHECKSUM_METHOD: str = "repr"       # 字符串，可选 "repr", "pickle"

    # health_check 性能基准：100 次序列化/校验的平均耗时上限（微秒）
    HEALTH_PERF_THRESHOLD_US: float = 500.0

    # ========== 公共接口 ==========
    @classmethod
    def create_packet(cls, stage_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        将流水线阶段的处理结果封装为带时间戳与校验和的标准化数据包

        Args:
            stage_name: 当前阶段名称 (如 "S2_signal_confirm")
            payload: 阶段输出的数据字典，必须可被 JSON 序列化

        Returns:
            标准响应字典，data 中包含封装后的数据包
        """
        # 参数校验
        if not isinstance(payload, dict):
            logger.warning(f"payload 不是字典类型，将使用空字典代替")
            payload = {}

        # 尝试获取高精度硬件时间戳
        timestamp_ns: int
        timestamp_source: str
        try:
            timestamp_ns = time.perf_counter_ns()
            timestamp_source = "perf_counter_ns"
        except Exception:
            logger.warning("硬件时间戳获取失败，降级使用 time.time()")
            timestamp_ns = int(time.time() * 1e9)
            timestamp_source = "time_fallback"

        # 根据配置选择序列化方式用于校验和计算
        checksum_bytes: bytes
        checksum_method: str
        if cls.USE_RAW_CHECKSUM:
            try:
                checksum_bytes, checksum_method = cls._serialize_raw(payload)
            except Exception as e:
                logger.warning(
                    f"原始字节序列化失败: {e}，回退至 JSON 序列化 #RECOVERY: 检查 RAW_CHECKSUM_METHOD 配置"
                )
                # 回退到 JSON
                checksum_bytes, checksum_method = cls._serialize_json(payload)
        else:
            checksum_bytes, checksum_method = cls._serialize_json(payload)

        checksum = cls._compute_checksum(checksum_bytes)

        packet = {
            "stage": stage_name,
            "timestamp_ns": timestamp_ns,
            "timestamp_source": timestamp_source,
            "payload": payload,
            "checksum": checksum,
            "algorithm": cls.DEFAULT_HASH_ALGORITHM,
            "checksum_method": checksum_method,     # 记录序列化方法，便于调试
        }

        logger.debug(
            "创建上下文数据包: stage=%s, timestamp=%d ns, checksum=%s..., method=%s",
            stage_name, timestamp_ns, checksum[:8], checksum_method
        )

        return {
            "status": "ok",
            "reason": f"已创建阶段 {stage_name} 的上下文数据包",
            "data": {"packet": packet},
            "warnings": [],
        }

    @classmethod
    def validate_packet(cls, packet: Dict[str, Any]) -> Dict[str, Any]:
        """
        校验上下文数据包的完整性，提取原始载荷

        Args:
            packet: 由 create_packet 创建的数据包字典

        Returns:
            标准响应字典，data 中包含 "valid" (bool), "payload" (Dict), "stage" (str) 等字段
        """
        # 参数类型校验
        if not isinstance(packet, dict):
            logger.warning(f"传入参数不是字典类型: {type(packet).__name__}")
            return {
                "status": "error",
                "reason": f"数据包必须为字典类型，实际为 {type(packet).__name__}",
                "data": {"valid": False, "payload": {}, "stage": ""},
                "warnings": ["invalid_packet_type"],
            }

        required_keys = {"stage", "timestamp_ns", "payload", "checksum", "algorithm"}
        missing = required_keys - set(packet.keys())
        if missing:
            logger.warning(f"数据包缺少必需字段: {missing}")
            return {
                "status": "error",
                "reason": f"数据包格式无效，缺少字段: {missing}",
                "data": {"valid": False, "payload": {}, "stage": ""},
                "warnings": ["invalid_packet_format"],
            }

        algorithm = packet.get("algorithm")
        if algorithm is None:
            algorithm = cls.DEFAULT_HASH_ALGORITHM
            logger.warning("数据包缺少 algorithm 字段，使用默认算法 %s", algorithm)

        # 确定校验和计算方式：优先使用数据包记录的 checksum_method，否则根据当前配置推断
        checksum_method = packet.get("checksum_method", "json")
        checksum_bytes: bytes
        try:
            if checksum_method == "raw_repr":
                checksum_bytes = repr(packet["payload"]).encode("utf-8")
            elif checksum_method == "raw_pickle":
                import pickle
                checksum_bytes = pickle.dumps(packet["payload"])
            else:
                # 默认 JSON 方式
                checksum_bytes = json.dumps(
                    packet["payload"], sort_keys=True, ensure_ascii=False
                ).encode("utf-8")
        except Exception as e:
            logger.error(
                f"payload 序列化失败: {e} #RECOVERY: 检查 payload 内容是否可序列化"
            )
            return {
                "status": "error",
                "reason": f"payload 序列化失败: {str(e)}",
                "data": {"valid": False, "payload": {}, "stage": packet.get("stage", "")},
                "warnings": ["payload_serialization_failed"],
            }

        # 计算期望校验和
        expected_checksum = cls._compute_checksum(checksum_bytes, algorithm=algorithm)
        actual_checksum = packet.get("checksum", "")

        is_valid = (expected_checksum == actual_checksum)

        # 如果使用默认算法校验失败且数据包算法不在已知合法列表中，尝试使用默认算法重新校验
        if not is_valid and algorithm not in cls.VALID_ALGORITHMS:
            logger.warning(
                f"数据包算法 '{algorithm}' 不在已知合法列表 {cls.VALID_ALGORITHMS} 中，"
                f"尝试使用默认算法 {cls.DEFAULT_HASH_ALGORITHM} 重新校验"
            )
            retry_checksum = cls._compute_checksum(checksum_bytes, algorithm=cls.DEFAULT_HASH_ALGORITHM)
            if retry_checksum == actual_checksum:
                is_valid = True
                logger.info("使用默认算法重新校验通过，数据包有效")
            else:
                logger.error("默认算法重新校验失败，数据包确实无效")

        if is_valid:
            logger.debug(
                "数据包校验通过: stage=%s, checksum=%s...",
                packet["stage"], expected_checksum[:8]
            )
        else:
            logger.error(
                "数据包校验失败: stage=%s, 期望=%s..., 实际=%s... #RECOVERY: 检查上游模块是否完整传递了 create_packet 产生的数据包，排查中间是否有篡改",
                packet.get("stage", "unknown"),
                expected_checksum[:8],
                actual_checksum[:8] if actual_checksum else "null"
            )

        return {
            "status": "ok",
            "reason": "数据包校验完成" if is_valid else "数据包校验失败，完整性受损",
            "data": {
                "valid": is_valid,
                "payload": packet["payload"] if is_valid else {},
                "stage": packet.get("stage", ""),
                "timestamp_ns": packet.get("timestamp_ns", 0),
            },
            "warnings": [] if is_valid else ["checksum_mismatch"],
        }

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """
        模块自检：使用复合 payload 创建数据包、验证完整性、执行边界测试与性能基准

        Returns:
            标准健康检查响应字典
        """
        try:
            warnings: List[str] = []

            # 1. 基本功能测试（复合 payload 包含嵌套、Unicode、特殊浮点数）
            test_payload = {
                "bool": True,
                "int": 42,
                "float": 3.14159,
                "zero": 0.0,
                "string": "火种流水线",
                "nested": {"level2": [1, 2, 3], "empty": None},
            }
            create_result = cls.create_packet("health_check_stage", test_payload)
            if create_result["status"] != "ok":
                return {
                    "status": "error",
                    "reason": "create_packet 自检失败",
                    "data": {},
                    "warnings": ["create_packet_failed"],
                }

            packet = create_result["data"]["packet"]
            validate_result = cls.validate_packet(packet)
            if not validate_result["data"].get("valid"):
                return {
                    "status": "error",
                    "reason": "validate_packet 自检失败",
                    "data": {},
                    "warnings": ["validate_packet_failed"],
                }

            # 2. 边界值测试：极大浮点数、空结构、Unicode 特殊字符
            edge_payloads = [
                {"max_float": 1.7976931348623157e+308, "min_float": -1.7976931348623157e+308},
                {"empty_dict": {}, "empty_list": [], "empty_string": ""},
                {"unicode": "𝄞 😀 🚀\n\t", "escape_chars": "\\\"\0"},
            ]
            for i, ep in enumerate(edge_payloads):
                ep_result = cls.create_packet(f"health_edge_{i}", ep)
                if ep_result["status"] != "ok":
                    warnings.append(f"边界测试{i}创建失败")
                    continue
                ep_valid = cls.validate_packet(ep_result["data"]["packet"])
                if not ep_valid["data"].get("valid"):
                    warnings.append(f"边界测试{i}校验失败")

            # 3. 故障注入测试：手动篡改校验和，验证检测能力
            tampered_packet = packet.copy()
            tampered_packet["checksum"] = "0000000000000000deadbeef00000000"
            tamper_result = cls.validate_packet(tampered_packet)
            if tamper_result["data"].get("valid"):
                warnings.append("故障注入测试失败：篡改后的数据包被判定为有效")

            # 4. 性能基准测试：对中等复杂度 payload 执行 100 次序列化+校验
            perf_payload = {f"key_{i}": f"value_{i}" for i in range(100)}
            start_time = time.perf_counter_ns()
            for _ in range(100):
                pkt_res = cls.create_packet("perf_test", perf_payload)
                cls.validate_packet(pkt_res["data"]["packet"])
            end_time = time.perf_counter_ns()
            avg_time_us = (end_time - start_time) / 100 / 1000.0
            if avg_time_us > cls.HEALTH_PERF_THRESHOLD_US:
                warnings.append(
                    f"性能降级: 平均耗时 {avg_time_us:.1f}μs > 阈值 {cls.HEALTH_PERF_THRESHOLD_US}μs"
                )
            logger.info(f"性能基准: 100次操作平均耗时 {avg_time_us:.1f}μs")

            status = "ok" if not warnings else "degraded"
            return {
                "status": status,
                "reason": f"ContextPasser 自检完成，{len(warnings)} 个警告",
                "data": {
                    "warnings_count": len(warnings),
                    "performance_avg_us": round(avg_time_us, 1),
                },
                "warnings": warnings,
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查标准库 hashlib/json/pickle 是否可用")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    @classmethod
    def _compute_checksum(cls, data: bytes, algorithm: Optional[str] = None) -> str:
        """
        计算数据的校验和

        Args:
            data: 待计算校验和的字节串
            algorithm: 哈希算法名称，若为 None 则使用类常量默认值

        Returns:
            十六进制校验和字符串
        """
        algo = algorithm if algorithm else cls.DEFAULT_HASH_ALGORITHM
        if algo == "sha256":
            return hashlib.sha256(data).hexdigest()
        elif algo == "sha1":
            return hashlib.sha1(data).hexdigest()
        elif algo == "md5":
            return hashlib.md5(data).hexdigest()
        else:
            logger.warning(
                f"未知哈希算法: {algo}，回退为 sha256 #RECOVERY: 检查数据包 algorithm 字段或默认配置"
            )
            return hashlib.sha256(data).hexdigest()

    @classmethod
    def _serialize_json(cls, payload: Dict[str, Any]) -> tuple:
        """JSON 序列化方式，返回 (bytes, method_name)"""
        try:
            data = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            return data, "json"
        except (TypeError, ValueError) as e:
            logger.error(f"JSON 序列化失败: {e}")
            raise

    @classmethod
    def _serialize_raw(cls, payload: Dict[str, Any]) -> tuple:
        """原始字节序列化，返回 (bytes, method_name)"""
        method = cls.RAW_CHECKSUM_METHOD
        if method == "repr":
            return repr(payload).encode("utf-8"), "raw_repr"
        elif method == "pickle":
            import pickle
            return pickle.dumps(payload), "raw_pickle"
        else:
            # 回退 JSON
            logger.warning(f"未知的 RAW_CHECKSUM_METHOD: {method}，回退 JSON")
            return cls._serialize_json(payload)
