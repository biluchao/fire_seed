"""
火种系统 · 通用语义向量协议 (NeuroPulse)

核心职责：
1. 定义全系统统一的决策意图向量 NeuroPulse，包含意图类型、紧急性、置信度、期望仓位等核心字段
2. 定义通用的约束响应向量 NeuroConstraint，用于各模块返回对意图的允许/拒绝/调整结果

外部依赖（真实模块接口）：
- 无外部业务模块依赖，仅依赖 Python 标准库 typing, dataclasses, time, enum

接口契约：
- NeuroPulse.validate() -> Dict[str, Any] : 验证当前脉冲的字段完整性和合法性
- NeuroPulse.to_dict() -> Dict[str, Any] : 将脉冲序列化为标准化字典
- NeuroPulse.from_dict(data: Dict[str, Any]) -> NeuroPulse : 从字典反序列化
- NeuroConstraint.allowed_reason() -> str : 返回约束决策的完整原因描述
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 本模块仅定义数据结构，无运行时外部依赖，无降级场景
- 在反序列化时，若字段缺失或类型非法，使用默认保守值并记录警告

资源管理：
- 本模块不持有任何外部资源句柄，所有数据结构在作用域外自动回收
"""

import time
import logging
from typing import Dict, Any, List, Optional, Union
from dataclasses import dataclass, field, asdict
from enum import IntEnum

logger = logging.getLogger(__name__)


class IntentType(IntEnum):
    """意图类型枚举"""
    OPEN_LONG = 1          # 开多
    OPEN_SHORT = -1        # 开空
    ADD_POSITION = 2       # 加仓
    REDUCE_POSITION = -2   # 减仓
    CLOSE_ALL = 0          # 全部平仓
    MODIFY_STOP = 3        # 修改止损
    CIRCUIT_BREAK = 9      # 熔断触发


class UrgencyLevel(IntEnum):
    """紧急性等级（0-10，10为生存级）"""
    SURVIVAL = 10          # 生存级（C++硬实时风控）
    CRITICAL = 9           # 极高（熔断、紧急全平）
    HIGH = 7               # 高（策略执行、紧缩利润触发）
    NORMAL = 5             # 普通（正常交易信号）
    LOW = 3                # 低（进化任务、因子更新）
    BACKGROUND = 1          # 后台（日志、报告）


class ExecutionMethod(IntEnum):
    """执行方式建议"""
    LIMIT_ORDER = 1        # 限价单
    ICEBERG = 2            # 冰山订单
    TWAP = 3               # 时间加权平均价格
    MARKET_ORDER = 4       # 市价单


@dataclass
class NeuroPulse:
    """
    通用决策意图向量
    
    字段说明：
    - intent_id: str, 唯一脉冲标识，格式: 模块名_时间戳_序列号
    - intent_type: IntentType, 意图类型（开多/开空/加仓/减仓等）
    - urgency: UrgencyLevel, 紧急性等级（0-10）
    - confidence: float, 置信度 (0.0-1.0)
    - desired_size_pct: float, 期望仓位占权益百分比
    - risk_cost_pct: float, 预估风险代价（VaR增量占权益百分比）
    - time_tolerance_us: int, 可接受最大延迟（微秒）
    - sensory_source: str, 感官来源标签（如 visual_cortex, ma12_synergy）
    - timestamp: float, 脉冲生成时间戳
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
    context: Dict[str, Any] = field(default_factory=dict)

    # 类常量：字段合法范围
    MIN_CONFIDENCE = 0.0
    MAX_CONFIDENCE = 1.0
    MIN_SIZE_PCT = 0.0
    MAX_SIZE_PCT = 100.0         # 百分比上限（极端情况下允许超过100%？实际上限应更小，此处为安全边界）
    MIN_TIME_TOLERANCE_US = 1
    MAX_TIME_TOLERANCE_US = 10000000  # 10秒

    def validate(self) -> Dict[str, Any]:
        """
        验证脉冲字段的完整性和合法性
        
        Returns:
            Dict 包含 status, reason, data, warnings
        """
        warnings = []
        
        # 1. intent_id 不能为空
        if not self.intent_id:
            return {
                "status": "error",
                "reason": "intent_id 为空",
                "data": {"field": "intent_id"},
                "warnings": ["missing_intent_id"],
            }
        
        # 2. 置信度范围检查
        if not (self.MIN_CONFIDENCE <= self.confidence <= self.MAX_CONFIDENCE):
            old_val = self.confidence
            self.confidence = max(self.MIN_CONFIDENCE, min(self.MAX_CONFIDENCE, self.confidence))
            warnings.append(f"confidence 超出范围 [{self.MIN_CONFIDENCE}, {self.MAX_CONFIDENCE}], 已从 {old_val} 裁剪为 {self.confidence}")
            logger.warning("脉冲 %s 的 confidence 值异常: %s", self.intent_id, old_val)
        
        # 3. 仓位百分比合理性检查
        if self.desired_size_pct < self.MIN_SIZE_PCT:
            warnings.append(f"desired_size_pct 为负值 ({self.desired_size_pct})，已置零")
            self.desired_size_pct = 0.0
        elif self.desired_size_pct > self.MAX_SIZE_PCT:
            old_val = self.desired_size_pct
            self.desired_size_pct = self.MAX_SIZE_PCT
            logger.warning("脉冲 %s 的 desired_size_pct 从 %s 裁剪至上限 %s", self.intent_id, old_val, self.MAX_SIZE_PCT)
            warnings.append(f"desired_size_pct 超过上限，已从 {old_val} 裁剪至 {self.MAX_SIZE_PCT}")
        
        # 4. 时间容忍度裁剪
        if self.time_tolerance_us < self.MIN_TIME_TOLERANCE_US:
            self.time_tolerance_us = self.MIN_TIME_TOLERANCE_US
            warnings.append("time_tolerance_us 低于最小值，已调整")
        elif self.time_tolerance_us > self.MAX_TIME_TOLERANCE_US:
            self.time_tolerance_us = self.MAX_TIME_TOLERANCE_US
            warnings.append("time_tolerance_us 超过最大值，已调整")
        
        # 5. 意图类型必须为已知枚举
        if not isinstance(self.intent_type, IntentType):
            return {
                "status": "error",
                "reason": f"intent_type 类型非法: {type(self.intent_type)}",
                "data": {},
                "warnings": warnings + ["invalid_intent_type"],
            }
        
        # 6. 紧迫性必须为已知枚举
        if not isinstance(self.urgency, UrgencyLevel):
            return {
                "status": "error",
                "reason": f"urgency 类型非法: {type(self.urgency)}",
                "data": {},
                "warnings": warnings + ["invalid_urgency"],
            }
        
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
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'NeuroPulse':
        """
        从字典反序列化，缺失字段使用默认值并记录警告
        """
        if not data:
            logger.warning("from_dict 收到空字典，返回默认 NeuroPulse")
        pulse = cls()
        pulse.intent_id = data.get("intent_id", "")
        pulse.confidence = data.get("confidence", 0.0)
        pulse.desired_size_pct = data.get("desired_size_pct", 0.0)
        pulse.risk_cost_pct = data.get("risk_cost_pct", 0.0)
        pulse.time_tolerance_us = data.get("time_tolerance_us", 500)
        pulse.sensory_source = data.get("sensory_source", "unknown")
        pulse.timestamp = data.get("timestamp", time.time())
        pulse.context = data.get("context", {})

        # 枚举字段反序列化
        try:
            pulse.intent_type = IntentType(data.get("intent_type", 1))
        except (ValueError, KeyError):
            logger.warning("无法解析 intent_type 值 %s，使用默认值 OPEN_LONG", data.get("intent_type"))
            pulse.intent_type = IntentType.OPEN_LONG

        try:
            pulse.urgency = UrgencyLevel(data.get("urgency", 5))
        except (ValueError, KeyError):
            logger.warning("无法解析 urgency 值 %s，使用默认值 NORMAL", data.get("urgency"))
            pulse.urgency = UrgencyLevel.NORMAL

        # 简单验证
        pulse.validate()
        return pulse

    def to_compact_str(self) -> str:
        """返回紧凑的一行摘要，用于日志"""
        return (f"Pulse({self.intent_id[:12]}, "
                f"type={self.intent_type.name}, "
                f"urgency={self.urgency.name}, "
                f"conf={self.confidence:.2f}, "
                f"size={self.desired_size_pct:.4%})")


@dataclass
class NeuroConstraint:
    """
    通用约束响应向量
    
    字段说明：
    - allowed: bool, 是否允许执行
    - allowed_size_pct: float, 实际允许的仓位比例
    - preferred_method: ExecutionMethod, 建议执行方式
    - suggested_delay_us: int, 建议延迟微秒数（0表示无建议）
    - adjustment_reason: str, 调整原因描述
    - alternative_suggestion: Dict[str, Any], 替代方案（如降级为更小仓位或限价单）
    - responder: str, 响应模块名称
    - response_timestamp: float, 响应时间戳
    - rejection_code: str, 拒绝/调整的标准化错误码（空字符串表示无异常）
    """
    allowed: bool = True
    allowed_size_pct: float = 0.0
    preferred_method: ExecutionMethod = ExecutionMethod.LIMIT_ORDER
    suggested_delay_us: int = 0
    adjustment_reason: str = ""
    alternative_suggestion: Dict[str, Any] = field(default_factory=dict)
    responder: str = "unknown"
    response_timestamp: float = field(default_factory=time.time)
    rejection_code: str = ""          # 标准化错误码，如 "RISK_LIMIT_EXCEEDED"

    # 类常量
    MAX_SIZE_PCT = 100.0
    MAX_SUGGESTED_DELAY_US = 10000000  # 10秒

    def validate(self) -> Dict[str, Any]:
        """
        验证约束响应的基本合法性
        """
        warnings = []
        if self.allowed_size_pct < 0:
            self.allowed_size_pct = 0.0
            warnings.append("allowed_size_pct 为负值，已置零")
        elif self.allowed_size_pct > self.MAX_SIZE_PCT:
            warnings.append(f"allowed_size_pct 超过上限 {self.MAX_SIZE_PCT}%")
        
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

    def allowed_reason(self) -> str:
        """返回约束决策的完整原因描述"""
        code_info = f" (code: {self.rejection_code})" if self.rejection_code else ""
        if self.allowed:
            return f"允许执行 (仓位: {self.allowed_size_pct:.4%}){code_info} {self.adjustment_reason}"
        return f"拒绝执行{code_info}: {self.adjustment_reason}"

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "allowed": self.allowed,
            "allowed_size_pct": self.allowed_size_pct,
            "preferred_method": int(self.preferred_method),
            "suggested_delay_us": self.suggested_delay_us,
            "adjustment_reason": self.adjustment_reason,
            "alternative_suggestion": self.alternative_suggestion,
            "responder": self.responder,
            "response_timestamp": self.response_timestamp,
            "rejection_code": self.rejection_code,
        }


def health_check() -> Dict[str, Any]:
    """
    模块自检：验证 NeuroPulse 和 NeuroConstraint 的序列化与反序列化
    
    Returns:
        标准健康检查响应字典
    """
    try:
        # 创建测试脉冲
        test_pulse = NeuroPulse(
            intent_id="test_001",
            intent_type=IntentType.OPEN_LONG,
            urgency=UrgencyLevel.NORMAL,
            confidence=0.75,
            desired_size_pct=1.2,
            sensory_source="ma12_synergy",
        )
        # 验证
        val_result = test_pulse.validate()
        if val_result["status"] == "error":
            return {
                "status": "error",
                "reason": f"NeuroPulse 验证失败: {val_result['reason']}",
                "data": {},
                "warnings": val_result["warnings"],
            }
        
        # 序列化 / 反序列化
        pulse_dict = test_pulse.to_dict()
        restored = NeuroPulse.from_dict(pulse_dict)
        if restored.intent_id != test_pulse.intent_id:
            return {
                "status": "error",
                "reason": "NeuroPulse 序列化/反序列化不一致",
                "data": {},
                "warnings": [],
            }
        
        # 测试约束
        constraint = NeuroConstraint(
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
        
        return {
            "status": "ok",
            "reason": "NeuroPulse/NeuroConstraint 自检通过",
            "data": {
                "pulse_test": "passed",
                "constraint_test": "passed",
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
