"""
火种系统 · 流水线上下文传递器 (ContextPasser)

核心职责：
1. 将流水线阶段产生的处理结果封装为标准上下文数据包，附带硬件时间戳与校验和
2. 在下一阶段接收数据包时进行完整性校验，确保数据在传递过程中未被篡改或错位

外部依赖（真实模块接口）：
- 无外部自定义模块依赖，仅使用 Python 标准库 (hashlib, json, time, logging)

接口契约：
- create_packet(stage_name: str, payload: Dict[str, Any]) -> Dict[str, Any] : 创建标准化上下文数据包
- validate_packet(packet: Dict[str, Any]) -> Dict[str, Any] : 校验数据包完整性并返回原始载荷
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当输入的 payload 不是字典类型时，自动替换为空字典并记录 warning
- 当 payload 无法被 JSON 序列化时，返回错误状态，不生成数据包
- 硬件时间戳获取失败时，降级使用 time.time() 并记录 warning
- 数据包缺少 algorithm 字段时，回退至类常量默认算法，并记录 warning

资源管理：
- 本模块为纯工具类，不持有任何外部资源，无需手动释放
"""

import time
import hashlib
import json
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class ContextPasser:
    """流水线上下文传递器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_HASH_ALGORITHM = "sha256"       # 默认校验和算法，字符串，可选 "sha256", "sha1", "md5"

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

        # 序列化 payload 用于计算校验和
        try:
            payload_bytes = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as e:
            logger.error(f"payload 序列化失败: {e} #RECOVERY: 检查 payload 是否包含不可序列化对象")
            return {
                "status": "error",
                "reason": f"payload 序列化失败: {str(e)}",
                "data": {},
                "warnings": ["payload_serialization_failed"],
            }

        checksum = cls._compute_checksum(payload_bytes)

        packet = {
            "stage": stage_name,
            "timestamp_ns": timestamp_ns,
            "timestamp_source": timestamp_source,
            "payload": payload,
            "checksum": checksum,
            "algorithm": cls.DEFAULT_HASH_ALGORITHM,
        }

        logger.debug(
            "创建上下文数据包: stage=%s, timestamp=%d ns, checksum=%s...",
            stage_name, timestamp_ns, checksum[:8]
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

        try:
            payload_bytes = json.dumps(
                packet["payload"], sort_keys=True, ensure_ascii=False
            ).encode("utf-8")
        except (TypeError, ValueError) as e:
            logger.error(
                f"payload 反序列化验证失败: {e} #RECOVERY: 检查 payload 是否包含不可序列化对象"
            )
            return {
                "status": "error",
                "reason": f"payload 无法序列化用于校验: {str(e)}",
                "data": {"valid": False, "payload": {}, "stage": packet.get("stage", "")},
                "warnings": ["payload_serialization_failed"],
            }

        # 从数据包中读取哈希算法，实现向后兼容
        algorithm = packet.get("algorithm")
        if algorithm is None:
            algorithm = cls.DEFAULT_HASH_ALGORITHM
            logger.warning("数据包缺少 algorithm 字段，使用默认算法 %s", algorithm)

        expected_checksum = cls._compute_checksum(payload_bytes, algorithm=algorithm)
        actual_checksum = packet.get("checksum", "")

        is_valid = expected_checksum == actual_checksum

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
        模块自检：使用复合 payload 创建数据包并验证其完整性

        Returns:
            标准健康检查响应字典
        """
        try:
            # 使用包含嵌套结构、Unicode 字符串和特殊浮点数的复合 payload，覆盖更多边界条件
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

            return {
                "status": "ok",
                "reason": "ContextPasser 自检通过，数据包装与校验功能正常",
                "data": {},
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查标准库 hashlib/json 是否可用")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    @classmethod
    def _compute_checksum(cls, data: bytes, algorithm: str = None) -> str:
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
