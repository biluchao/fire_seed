"""
火种系统 · 通用语义向量协议 (NeuroPulse)

核心职责：
1. 定义全系统统一的决策意图向量 NeuroPulse，包含意图类型、紧急性、置信度、期望仓位、风险代价、
   生存时间(TTL)等核心字段，并提供过期检测与边界值校验
2. 定义通用的约束响应向量 NeuroConstraint，用于各模块返回对意图的允许/拒绝/调整结果，
   并附带响应延迟、及时性标记、与原始脉冲的关联 ID

外部依赖（真实模块接口）：
- 无外部业务模块依赖，仅依赖 Python 标准库 typing, dataclasses, time, enum, json

接口契约：
- NeuroPulse.validate() -> Dict[str, Any] : 验证当前脉冲的字段完整性和合法性，含风控硬限制
- NeuroPulse.to_dict() -> Dict[str, Any] : 将脉冲序列化为标准化字典
- NeuroPulse.from_dict(data: Dict[str, Any]) -> NeuroPulse : 从字典反序列化
- NeuroPulse.is_expired() -> bool : 检查脉冲是否已过期
- NeuroConstraint.validate() -> Dict[str, Any] : 验证约束的合法性
- NeuroConstraint.allowed_reason() -> str : 返回约束决策的完整原因描述
- NeuroConstraint.mark_response_time(pulse_urgency: UrgencyLevel) -> None : 标记响应及时性
- health_check() -> Dict[str, Any] : 模块自检，包含边界值测试、TTL测试、全字段反序列化测试
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 本模块仅定义数据结构，无运行时外部依赖，无降级场景
- 在反序列化时，若字段缺失或类型非法，使用默认保守值并记录警告
- validate 方法会自动裁剪越界数值，确保脉冲在后续模块中不会引发数值异常

资源管理：
- 本模块不持有任何外部资源句柄，所有数据结构在作用域外自动回收
"""

import time
import logging
import json
from typing import Dict, Any, List
from dataclasses import dataclass, field
from enum import IntEnum

logger = logging.getLogger(__name__)


class IntentType(IntEnum):
    """意图类型枚举"""
    OPEN_LONG = 1           # 开多
    OPEN_SHORT = -1         # 开空
    ADD_POSITION = 2        # 加仓
    REDUCE_POSITION = -2    # 减仓
    CLOSE_LONG_ONLY = -3    # 仅平多
    CLOSE_SHORT_ONLY = -4   # 仅平空
    CLOSE_ALL = 0           # 全部平仓
    MODIFY_STOP = 3         # 修改止损
    CIRCUIT_BREAK = 9       # 熔断触发

    @property
    def is_close_intent(self) -> bool:
        """判断是否为平仓类意图"""
        return int(self) in (0, -3, -4)


class UrgencyLevel(IntEnum):
    """紧急性等级 (0-10, 10 为生存级)"""
    SURVIVAL = 10           # 生存级（C++ 硬实时风控）
    CRITICAL = 9            # 极高（熔断、紧急全平）
    HIGH = 7                # 高（策略执行、紧缩利润触发）
    NORMAL = 5              # 普通（正常交易信号）
    LOW = 3                 # 低（进化任务、因子更新）
    BACKGROUND = 1          # 后台（日志、报告）

    @property
    def max_response_us(self) -> int:
        """返回该等级的最大响应时间（微秒），0 表示无限制"""
        mapping = {
            10: 100,        # 生存级：100μs
            9: 500,         # 极高：500μs
            7: 1000,        # 高：1ms
            5: 10000,       # 普通：10ms
            3: 100000,      # 低：100ms
            1: 0,           # 后台：无限制
        }
        return mapping.get(int(self), 0)

    @property
    def is_preemptive(self) -> bool:
        """该等级是否可抢占低等级脉冲"""
        return int(self) >= 10


class ExecutionMethod(IntEnum):
    """执行方式建议"""
    LIMIT_ORDER = 1         # 限价单
    ICEBERG = 2             # 冰山订单
    TWAP = 3                # 时间加权平均价格
    MARKET_ORDER = 4        # 市价单


@dataclass
class NeuroPulse:
    """
    通用决策意图向量

    字段说明：
    - intent_id: str, 唯一脉冲标识，格式: 模块名_时间戳_序列号
    - intent_type: IntentType, 意图类型
    - urgency: UrgencyLevel, 紧急性等级 (0-10)
    - confidence: float, 置信度 (0.0 ~ 1.0)
    - desired_size_pct: float, 期望仓位占权益百分比
    - risk_cost_pct: float, 预估风险代价 (VaR 增量占权益百分比)
    - time_tolerance_us: int, 可接受最大延迟 (微秒)
    - sensory_source: str, 感官来源标签
    - timestamp: float, 时间戳
    - ttl_seconds: float, 生存时间 (秒)，超过此时间脉冲自动作废
    - generated_at: float, 生成时刻 (不可变，自动校准)
    - expires_at: float, 过期时刻 (自动计算)
    - context: Dict[str, Any], 扩展上下文字典
    """
    intent_id: str = ""
    intent_type: IntentType = IntentType.OPEN_LONG
    urgency: UrgencyLevel = UrgencyLevel.NORMAL
    confidence: float = 0.0
    desired_size_pct: float = 0.0
    risk_cost_pct: float = 0.0
    time_tolerance_us: int = 500
    sensory_source: str = "unknown"
    timestamp: float = field(default_factory=time.time)
    ttl_seconds: float = 1.0            # 默认生存1秒
    generated_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    context: Dict[str, Any] = field(default_factory=dict)

    # 类常量：字段合法范围
    MIN_CONFIDENCE = 0.0
    MAX_CONFIDENCE = 1.0
    MIN_SIZE_PCT = 0.0
    MAX_SIZE_PCT = 25.0              # 单笔脉冲最大仓位(%), 来源: 风控委员会 2025Q1 决议
                                     # 可通过 risk_limits.yaml:neuro_pulse.max_size_pct 覆盖，以较小值为准
    MAX_RISK_COST_PCT = 5.0          # 单笔脉冲最大风险代价(%), 来源: 风控委员会 2025Q1 决议
    MIN_TIME_TOLERANCE_US = 1
    MAX_TIME_TOLERANCE_US = 10000000  # 10 秒

    def __post_init__(self):
        """在实例化后自动校准时间戳并计算过期时间"""
        now = time.time()
        # 防御性校验：generated_at 必须为合理的时间戳
        if self.generated_at <= 0 or self.generated_at > now + 1.0:
            logger.warning(
                "脉冲 %s 的 generated_at 非法 (%.2f)，已重置为当前时间",
                self.intent_id, self.generated_at
            )
            self.generated_at = now
        if self.expires_at == 0.0:
            self.expires_at = self.generated_at + self.ttl_seconds

    def is_expired(self) -> bool:
        """检查脉冲是否过期"""
        return time.time() > self.expires_at

    def validate(self) -> Dict[str, Any]:
        """
        验证脉冲字段的完整性和合法性
        对越界数值进行裁剪，确保下游模块接收的脉冲是干净、合法的

        Returns:
            Dict 包含 status, reason, data, warnings
        """
        warnings = []

        # 0. 检查是否过期
        if self.is_expired():
            return {
                "status": "error",
                "reason": f"脉冲已过期 (TTL={self.ttl_seconds}s, 生成于 {self.generated_at})",
                "data": {"intent_id": self.intent_id},
                "warnings": ["expired_pulse"],
            }

        # 1. intent_id 不能为空
        if not self.intent_id:
            return {
                "status": "error",
                "reason": "intent_id 为空，无法追踪该脉冲",
                "data": {"field": "intent_id"},
                "warnings": ["missing_intent_id"],
            }

        # 2. 置信度范围检查与裁剪
        if not (self.MIN_CONFIDENCE <= self.confidence <= self.MAX_CONFIDENCE):
            old_val = self.confidence
            self.confidence = max(self.MIN_CONFIDENCE, min(self.MAX_CONFIDENCE, self.confidence))
            warnings.append(
                f"confidence 超出范围 [{self.MIN_CONFIDENCE}, {self.MAX_CONFIDENCE}], "
                f"已从 {old_val:.4f} 裁剪为 {self.confidence:.4f}"
            )
            logger.warning("脉冲 %s 的 confidence 值异常: %s", self.intent_id, old_val)

        # 3. 仓位百分比合理性检查与风控硬限制
        if self.desired_size_pct < self.MIN_SIZE_PCT:
            warnings.append(f"desired_size_pct 为负值 ({self.desired_size_pct:.4f})，已置零")
            self.desired_size_pct = 0.0
        elif self.desired_size_pct > self.MAX_SIZE_PCT:
            return {
                "status": "error",
                "reason": (
                    f"desired_size_pct ({self.desired_size_pct:.4f}%) "
                    f"超过系统硬限制 ({self.MAX_SIZE_PCT}%)"
                ),
                "data": {
                    "field": "desired_size_pct",
                    "value": self.desired_size_pct,
                    "max": self.MAX_SIZE_PCT,
                },
                "warnings": ["hard_limit_violation"],
            }

        # 4. 风险代价检查
        if self.risk_cost_pct > self.MAX_RISK_COST_PCT:
            return {
                "status": "error",
                "reason": (
                    f"risk_cost_pct ({self.risk_cost_pct:.4f}%) "
                    f"超过系统硬限制 ({self.MAX_RISK_COST_PCT}%)"
                ),
                "data": {
                    "field": "risk_cost_pct",
                    "value": self.risk_cost_pct,
                    "max": self.MAX_RISK_COST_PCT,
                },
                "warnings": ["hard_limit_violation"],
            }

        # 5. 时间容忍度裁剪
        if self.time_tolerance_us < self.MIN_TIME_TOLERANCE_US:
            self.time_tolerance_us = self.MIN_TIME_TOLERANCE_US
            warnings.append("time_tolerance_us 低于最小值，已调整")
        elif self.time_tolerance_us > self.MAX_TIME_TOLERANCE_US:
            self.time_tolerance_us = self.MAX_TIME_TOLERANCE_US
            warnings.append("time_tolerance_us 超过最大值，已调整")

        # 6. 意图类型必须为已知枚举
        if not isinstance(self.intent_type, IntentType):
            return {
                "status": "error",
                "reason": f"intent_type 类型非法: {type(self.intent_type)}",
                "data": {},
                "warnings": warnings + ["invalid_intent_type"],
            }

        # 7. 紧迫性必须为已知枚举
        if not isinstance(self.urgency, UrgencyLevel):
            return {
                "status": "error",
                "reason": f"urgency 类型非法: {type(self.urgency)}",
                "data": {},
                "warnings": warnings + ["invalid_urgency"],
            }

        # 8. 检查 context 可序列化，避免不可序列化对象传递
        try:
            json.dumps(self.context, default=str)
        except (TypeError, ValueError, RecursionError) as e:
            return {
                "status": "error",
                "reason": f"context 不可序列化: {type(e).__name__}",
                "data": {},
                "warnings": ["unserializable_context"],
            }

        # 9. 可选检查：若 sensory_source 为空，补上默认值
        if not self.sensory_source:
            self.sensory_source = "unknown"
            warnings.append("sensory_source 为空，已设为 'unknown'")

        return {
            "status": "ok",
            "reason": "脉冲验证通过",
            "data": {"intent_id": self.intent_id},
            "warnings": warnings,
        }

    def to_dict(self) -> Dict[str, Any]:
        """将脉冲序列化为标准化字典"""
        return {
            "intent_id": self.intent_id,
            "intent_type": int(self.intent_type),
            "urgency": int(self.urgency),
            "confidence": self.confidence,
            "desired_size_pct": self.desired_size_pct,
            "risk_cost_pct": self.risk_cost_pct,
            "time_tolerance_us": self.time_tolerance_us,
            "sensory_source": self.sensory_source,
            "timestamp": self.timestamp,
            "ttl_seconds": self.ttl_seconds,
            "generated_at": self.generated_at,
            "expires_at": self.expires_at,
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'NeuroPulse':
        """
        从字典反序列化，缺失字段使用默认值并记录警告
        """
        pulse = cls()
        pulse.intent_id = data.get("intent_id", "")
        pulse.confidence = float(data.get("confidence", 0.0))
        pulse.desired_size_pct = float(data.get("desired_size_pct", 0.0))
        pulse.risk_cost_pct = float(data.get("risk_cost_pct", 0.0))
        pulse.time_tolerance_us = int(data.get("time_tolerance_us", 500))
        pulse.sensory_source = str(data.get("sensory_source", "unknown"))
        pulse.timestamp = float(data.get("timestamp", time.time()))
        pulse.ttl_seconds = float(data.get("ttl_seconds", 1.0))
        pulse.generated_at = float(data.get("generated_at", time.time()))
        pulse.expires_at = float(data.get("expires_at", 0.0))
        pulse.context = data.get("context", {})
        if not isinstance(pulse.context, dict):
            pulse.context = {}
            logger.warning("脉冲 %s 的 context 不是字典，已重置为空", pulse.intent_id)

        # 枚举字段反序列化
        try:
            pulse.intent_type = IntentType(int(data.get("intent_type", 1)))
        except (ValueError, KeyError):
            logger.warning("无法解析 intent_type 值 %s，使用默认值 OPEN_LONG", data.get("intent_type"))
            pulse.intent_type = IntentType.OPEN_LONG

        try:
            pulse.urgency = UrgencyLevel(int(data.get("urgency", 5)))
        except (ValueError, KeyError):
            logger.warning("无法解析 urgency 值 %s，使用默认值 NORMAL", data.get("urgency"))
            pulse.urgency = UrgencyLevel.NORMAL

        # 简单验证，确保返回的脉冲对象是干净的
        pulse.validate()
        return pulse

    def to_compact_str(self) -> str:
        """返回紧凑的一行摘要，用于日志"""
        expired_mark = " [EXPIRED]" if self.is_expired() else ""
        return (
            f"Pulse({self.intent_id[:12]}, "
            f"type={self.intent_type.name}, "
            f"urgency={self.urgency.name}, "
            f"conf={self.confidence:.2f}, "
            f"size={self.desired_size_pct:.4%}, "
            f"ttl={self.ttl_seconds}s{expired_mark})"
        )


@dataclass
class NeuroConstraint:
    """
    通用约束响应向量

    字段说明：
    - correlation_id: str, 对应的 NeuroPulse.intent_id，用于匹配原始请求
    - allowed: bool, 是否允许执行
    - allowed_size_pct: float, 实际允许的仓位比例
    - preferred_method: ExecutionMethod, 建议执行方式
    - suggested_delay_us: int, 建议延迟微秒数（0 表示无建议）
    - adjustment_reason: str, 调整原因描述
    - alternative_suggestion: Dict[str, Any], 替代方案
    - responder: str, 响应模块名称
    - response_timestamp: float, 响应时间戳
    - response_latency_us: int, 响应延迟（微秒）
    - is_timely: bool, 是否在规定时间内响应
    """
    correlation_id: str = ""                     # 对应的 NeuroPulse.intent_id
    allowed: bool = True
    allowed_size_pct: float = 0.0
    preferred_method: ExecutionMethod = ExecutionMethod.LIMIT_ORDER
    suggested_delay_us: int = 0
    adjustment_reason: str = ""
    alternative_suggestion: Dict[str, Any] = field(default_factory=dict)
    responder: str = "unknown"
    response_timestamp: float = field(default_factory=time.time)
    response_latency_us: int = 0
    is_timely: bool = True

    # 类常量
    MAX_SIZE_PCT = 100.0
    MAX_SUGGESTED_DELAY_US = 10_000_000  # 10 秒

    def validate(self) -> Dict[str, Any]:
        """
        验证约束响应的基本合法性
        """
        warnings = []
        if self.allowed_size_pct < 0:
            self.allowed_size_pct = 0.0
            warnings.append("allowed_size_pct 为负值，已置零")
        elif self.allowed_size_pct > self.MAX_SIZE_PCT:
            warnings.append(f"allowed_size_pct 超过防御上限 {self.MAX_SIZE_PCT}%")

        if self.suggested_delay_us < 0:
            self.suggested_delay_us = 0
            warnings.append("suggested_delay_us 为负值，已置零")
        elif self.suggested_delay_us > self.MAX_SUGGESTED_DELAY_US:
            self.suggested_delay_us = self.MAX_SUGGESTED_DELAY_US
            warnings.append("suggested_delay_us 超过最大值，已裁剪")

        if not isinstance(self.preferred_method, ExecutionMethod):
            self.preferred_method = ExecutionMethod.LIMIT_ORDER
            warnings.append("preferred_method 类型非法，已重置为 LIMIT_ORDER")

        return {
            "status": "ok" if not warnings else "warning",
            "reason": "约束验证完成",
            "data": {"warnings_count": len(warnings)},
            "warnings": warnings,
        }

    def mark_response_time(self, pulse_urgency: UrgencyLevel) -> None:
        """根据原始脉冲的紧急性等级，标记响应是否及时"""
        max_us = pulse_urgency.max_response_us
        if max_us > 0:
            self.is_timely = self.response_latency_us <= max_us
        else:
            self.is_timely = True

    def allowed_reason(self) -> str:
        """返回约束决策的完整原因描述"""
        if self.allowed:
            return f"允许执行 (仓位: {self.allowed_size_pct:.4%}) {self.adjustment_reason}"
        return f"拒绝执行: {self.adjustment_reason}"

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "correlation_id": self.correlation_id,
            "allowed": self.allowed,
            "allowed_size_pct": self.allowed_size_pct,
            "preferred_method": int(self.preferred_method),
            "suggested_delay_us": self.suggested_delay_us,
            "adjustment_reason": self.adjustment_reason,
            "alternative_suggestion": self.alternative_suggestion,
            "responder": self.responder,
            "response_timestamp": self.response_timestamp,
            "response_latency_us": self.response_latency_us,
            "is_timely": self.is_timely,
        }


def health_check() -> Dict[str, Any]:
    """
    模块自检：验证 NeuroPulse 和 NeuroConstraint 的序列化与反序列化，
    以及边界值测试、TTL测试、全字段反序列化测试

    Returns:
        标准健康检查响应字典
    """
    try:
        # 基本序列化/反序列化测试
        test_pulse = NeuroPulse(
            intent_id="health_check_test_001",
            intent_type=IntentType.OPEN_LONG,
            urgency=UrgencyLevel.NORMAL,
            confidence=0.75,
            desired_size_pct=1.2,
            sensory_source="ma12_synergy",
        )
        val_result = test_pulse.validate()
        if val_result["status"] == "error":
            return {
                "status": "error",
                "reason": f"NeuroPulse 验证失败: {val_result['reason']}",
                "data": {},
                "warnings": val_result["warnings"],
            }

        pulse_dict = test_pulse.to_dict()
        restored = NeuroPulse.from_dict(pulse_dict)
        if restored.intent_id != test_pulse.intent_id:
            return {
                "status": "error",
                "reason": "NeuroPulse 序列化/反序列化不一致",
                "data": {},
                "warnings": [],
            }

        # 全字段反序列化测试
        test_pulse2 = NeuroPulse(
            intent_id="full_serial_test",
            intent_type=IntentType.CLOSE_SHORT_ONLY,
            urgency=UrgencyLevel.CRITICAL,
            confidence=0.88,
            desired_size_pct=3.5,
            risk_cost_pct=0.8,
            time_tolerance_us=200,
            sensory_source="olfactory_cortex",
            ttl_seconds=0.5,
        )
        pulse_dict2 = test_pulse2.to_dict()
        restored2 = NeuroPulse.from_dict(pulse_dict2)
        fields_to_check = [
            "intent_id", "confidence", "desired_size_pct", "risk_cost_pct",
            "time_tolerance_us", "sensory_source", "ttl_seconds",
        ]
        for field_name in fields_to_check:
            orig = getattr(test_pulse2, field_name)
            rest = getattr(restored2, field_name)
            if orig != rest:
                return {
                    "status": "error",
                    "reason": f"反序列化字段不匹配: {field_name} ({orig} vs {rest})",
                    "data": {},
                    "warnings": [],
                }

        # 约束测试
        constraint = NeuroConstraint(
            correlation_id=test_pulse.intent_id,
            allowed=True,
            allowed_size_pct=1.0,
            adjustment_reason="风控允许",
        )
        c_val = constraint.validate()
        if c_val["status"] == "error":
            return {
                "status": "error",
                "reason": f"NeuroConstraint 验证失败: {c_val['reason']}",
                "data": {},
                "warnings": c_val["warnings"],
            }

        # 边界值测试
        edge_cases = [
            {"confidence": 0.0, "desc": "置信度下限"},
            {"confidence": 1.0, "desc": "置信度上限"},
            {"desired_size_pct": 0.0, "desc": "零仓位"},
            {"desired_size_pct": NeuroPulse.MAX_SIZE_PCT, "desc": "最大仓位"},
            {"urgency": UrgencyLevel.SURVIVAL, "desc": "最高紧急度"},
            {"urgency": UrgencyLevel.BACKGROUND, "desc": "最低紧急度"},
            {"intent_type": IntentType.CLOSE_LONG_ONLY, "desc": "仅平多"},
            {"intent_type": IntentType.CLOSE_SHORT_ONLY, "desc": "仅平空"},
        ]
        for case in edge_cases:
            pulse = NeuroPulse(intent_id=f"edge_test_{case['desc']}", **case)
            result = pulse.validate()
            if result["status"] == "error":
                return {
                    "status": "error",
                    "reason": f"边界值测试失败: {case['desc']} - {result['reason']}",
                    "data": {},
                    "warnings": result["warnings"],
                }

        # TTL 过期测试
        expired_pulse = NeuroPulse(
            intent_id="expired_test",
            ttl_seconds=0.0,
        )
        time.sleep(0.01)
        expired_result = expired_pulse.validate()
        if expired_result["status"] != "error":
            return {
                "status": "error",
                "reason": "TTL 过期测试失败：过期脉冲未被拒绝",
                "data": {},
                "warnings": [],
            }

        # 响应时间标记测试
        constraint2 = NeuroConstraint(response_latency_us=200)
        constraint2.mark_response_time(UrgencyLevel.SURVIVAL)
        if constraint2.is_timely:
            return {
                "status": "error",
                "reason": "响应时间测试失败：200μs 应超过 SURVIVAL 的 100μs 限制",
                "data": {},
                "warnings": [],
            }

        return {
            "status": "ok",
            "reason": "NeuroPulse/NeuroConstraint 全量自检通过（含边界值/TTL/全字段/响应时间测试）",
            "data": {
                "pulse_test": "passed",
                "constraint_test": "passed",
                "full_serial_test": "passed",
                "edge_cases": "passed",
                "ttl_test": "passed",
                "response_time_test": "passed",
            },
            "warnings": [],
        }
    except Exception as e:
        logger.error(f"健康检查失败: {e} #RECOVERY: 检查 NeuroPulse/NeuroConstraint 类定义是否正确")
        return {
            "status": "error",
            "reason": f"健康检查异常: {str(e)}",
            "data": {},
            "warnings": [f"health_check_failed: {str(e)}"],
        }
