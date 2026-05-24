"""
火种系统 · 感官快照标准化接口 (SensorySnapshot) — 深度精细化

核心职责：
1. 将视觉、听觉、触觉、嗅觉、味觉五感皮层的原始输出统一封装为标准化快照对象，保证下游决策模块无论感官模块是否降级，均可安全读取所有字段。
2. 对输入数据进行完整性校验、边界值钳制与缺失值填充，并在检测到数据质量问题时发出分级告警。

外部依赖（真实模块接口）：
- 无外部模块依赖。本模块仅定义数据容器与校验逻辑，所有输入由调用方通过工厂方法传入。

接口契约：
- create_snapshot(raw_data: Dict[str, Any]) -> Dict[str, Any]
  输出字典固定包含 "status" (str), "snapshot" (Dict[str, Any]), "reason" (str),
  "missing_fields" (List[str]), "warnings" (List[str])
- validate_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]
  输出字典固定包含 "status" (str), "snapshot" (Dict[str, Any]), "reason" (str),
  "clamped_fields" (List[str]), "warnings" (List[str])
- health_check() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "message" (str)

异常与降级：
- 当原始数据中某个感官字段缺失时，使用预定义的保守默认值填充，并在 missing_fields 中标记，不抛出异常。
- 当所有感官数据全部缺失时，返回一个全量默认快照，状态标记为 "degraded"。
- 边界值校验失败时，将异常值钳制到合法范围内，并在 clamped_fields 中记录，不影响整体状态。

资源管理：
- 本模块为纯工具类，无状态，不持有任何外部资源，无需显式释放。
"""

import logging
from typing import Dict, Any, List, Optional, Union

logger = logging.getLogger(__name__)


class SensorySnapshot:
    """感官快照标准化接口"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 各感官字段的默认安全值（用于降级填充）
    DEFAULT_VISUAL: Dict[str, Any] = {
        "candlestick_pattern": "none",       # K线形态，可选值见形态枚举
        "pattern_confidence": 0.0,           # 形态置信度，[0.0, 1.0]
        "ma12_position": "on_line",          # 价格相对M12位置，on_line/near/far/extreme
        "orderbook_slope": 0.0,             # 订单簿斜率，无量纲，[-5.0, 5.0]
        "orderbook_concentration": 0.0,      # 挂单集中度，[0.0, 1.0]
        "wall_resilience": "unknown",        # 挂单墙韧性，true_wall/paper_wall/uncertain/unknown
    }
    DEFAULT_AUDITORY: Dict[str, Any] = {
        "macro_alert_level": "none",         # 宏观事件等级，none/level1/level2/level3
        "time_to_next_event_sec": 999999.0,  # 距离下一事件的时间，秒
        "sentiment_score": 0.0,              # 情绪得分，[-1.0, 1.0]，0 表示中性
        "sentiment_momentum": 0.0,           # 情绪变化率，[-1.0, 1.0]，0 表示持平
    }
    DEFAULT_TACTILE: Dict[str, Any] = {
        "liquidity_level": "L1",             # 流动性等级，L1(极度稀薄)~L5(极度充裕)
        "depth_decay_speed": 10.0,           # 深度衰减速率，bps/s
        "trade_pulse_cv": 1.5,               # 成交脉搏变异系数，无量纲，>0.7 杂乱，<0.3 算法
        "volatility_regime": "expanding",    # 波动率结构，expanding/normal/contracting
    }
    DEFAULT_OLFACTORY: Dict[str, Any] = {
        "paper_wall_flag": False,            # 纸墙检测标记
        "spread_manipulation_flag": False,   # 价差操纵标记
        "contagion_risk_index": 0.0,         # 传染风险指数，[0.0, 1.0]
        "order_toxicity_active": False,      # 订单流毒性标记
    }
    DEFAULT_GUSTATORY: Dict[str, Any] = {
        "similar_win_rate": 0.5,             # 相似历史环境胜率，[0.0, 1.0]
        "bitter_similarity": 0.0,            # 失败记忆相似度，[0.0, 1.0]
        "sweet_memory_count": 0,             # 甜味记忆数量，个
        "bitter_memory_count": 0,            # 苦味记忆数量，个
        "total_similarity": 0.0,             # 总相似度累计值
    }

    # 所有必需感官字段列表
    REQUIRED_SENSES = ["visual", "auditory", "tactile", "olfactory", "gustatory"]

    # 各感官的合法值域与边界
    FIELD_CONSTRAINTS: Dict[str, Dict[str, tuple]] = {
        "visual": {
            "pattern_confidence": (0.0, 1.0),
            "orderbook_slope": (-5.0, 5.0),
            "orderbook_concentration": (0.0, 1.0),
        },
        "auditory": {
            "sentiment_score": (-1.0, 1.0),
            "sentiment_momentum": (-1.0, 1.0),
        },
        "tactile": {
            "depth_decay_speed": (0.0, 100.0),
            "trade_pulse_cv": (0.0, 10.0),
        },
        "olfactory": {
            "contagion_risk_index": (0.0, 1.0),
        },
        "gustatory": {
            "similar_win_rate": (0.0, 1.0),
            "bitter_similarity": (0.0, 1.0),
        },
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """可接受配置覆盖默认值（保留扩展性）。"""
        self._config = config or {}
        logger.info("SensorySnapshot 初始化完成")

    # ────────────────────────── 公共接口 ──────────────────────────
    def create_snapshot(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        基于五感原始数据创建标准化快照。
        
        Args:
            raw_data: 包含五个感官子字典的原始数据，格式如 {"visual": {...}, "auditory": {...}, ...}
        
        Returns:
            标准化快照字典，包含完整字段和元数据
        """
        snapshot: Dict[str, Any] = {}
        missing_fields: List[str] = []
        warnings: List[str] = []

        for sense in self.REQUIRED_SENSES:
            defaults = self._get_defaults(sense)
            if sense in raw_data and isinstance(raw_data[sense], dict):
                # 使用默认值作为基底，再用传入数据覆盖
                merged = {**defaults, **raw_data[sense]}
                snapshot[sense] = merged
            else:
                # 感官数据缺失，使用全量默认值并标记
                snapshot[sense] = defaults
                missing_fields.append(sense)
                warnings.append(f"感官 {sense} 数据缺失，使用降级默认值")

        # 生成状态与原因
        if len(missing_fields) == len(self.REQUIRED_SENSES):
            status = "degraded"
            reason = "所有感官数据缺失，使用全量降级快照"
        elif missing_fields:
            status = "partial"
            reason = f"部分感官数据缺失: {', '.join(missing_fields)}"
        else:
            status = "ok"
            reason = "感官快照创建成功，所有字段完整"

        return {
            "status": status,
            "snapshot": snapshot,
            "reason": reason,
            "missing_fields": missing_fields,
            "warnings": warnings,
        }

    def validate_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """
        验证一个已创建的感官快照是否包含所有必需字段，并对异常值进行边界钳制。
        
        Args:
            snapshot: 待验证的感官快照字典
        
        Returns:
            验证后的快照和状态信息
        """
        if not isinstance(snapshot, dict):
            return {
                "status": "error",
                "snapshot": {},
                "reason": "输入快照不是字典",
                "clamped_fields": [],
                "warnings": ["输入类型错误，不可恢复"],
            }

        missing: List[str] = []
        clamped_fields: List[str] = []
        warnings: List[str] = []

        # 补全缺失感官
        for sense in self.REQUIRED_SENSES:
            if sense not in snapshot or not isinstance(snapshot[sense], dict):
                snapshot[sense] = self._get_defaults(sense)
                missing.append(sense)
            else:
                # 对每个感官内的数值字段进行边界钳制
                constraints = self.FIELD_CONSTRAINTS.get(sense, {})
                for field, (low, high) in constraints.items():
                    if field in snapshot[sense]:
                        try:
                            val = float(snapshot[sense][field])
                            if val < low or val > high:
                                snapshot[sense][field] = max(low, min(high, val))
                                clamped_fields.append(f"{sense}.{field}")
                        except (ValueError, TypeError):
                            # 无法转为数值的字段，保留原值并告警
                            warnings.append(f"字段 {sense}.{field} 类型异常，保留原值")

        # 额外校验：流动性等级必须在合法集合内
        valid_liquidity = {"L1", "L2", "L3", "L4", "L5"}
        tactile = snapshot.get("tactile", {})
        if tactile.get("liquidity_level") not in valid_liquidity:
            tactile["liquidity_level"] = self.DEFAULT_TACTILE["liquidity_level"]
            clamped_fields.append("tactile.liquidity_level")
            warnings.append("流动性等级非法，已重置")

        # 额外校验：MA12位置
        valid_ma12_positions = {"on_line", "near", "far", "extreme"}
        visual = snapshot.get("visual", {})
        if visual.get("ma12_position") not in valid_ma12_positions:
            visual["ma12_position"] = self.DEFAULT_VISUAL["ma12_position"]
            clamped_fields.append("visual.ma12_position")

        status = "ok" if not missing and not clamped_fields else ("partial" if missing else "warning")
        reason = (
            "快照验证完成"
            if status == "ok"
            else f"修复字段: missing={missing}, clamped={clamped_fields}"
        )

        return {
            "status": status,
            "snapshot": snapshot,
            "reason": reason,
            "clamped_fields": clamped_fields,
            "warnings": warnings + ([f"补全缺失感官: {missing}"] if missing else []),
        }

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检：用全量默认值和部分缺失数据测试核心逻辑。"""
        try:
            instance = cls()

            # 测试1：全量默认快照（所有感官缺失）
            raw_empty = {}
            result = instance.create_snapshot(raw_empty)
            if result["status"] != "degraded":
                return {"status": "error", "message": "全量默认快照应返回 degraded 状态"}
            if len(result["missing_fields"]) != len(cls.REQUIRED_SENSES):
                return {"status": "error", "message": "缺失字段列表长度不正确"}

            # 测试2：部分缺失（提供视觉和触觉，缺少其他）
            raw_partial = {
                "visual": {"ma12_position": "near", "orderbook_slope": -1.2},
                "tactile": {"liquidity_level": "L4", "trade_pulse_cv": 0.5},
            }
            result = instance.create_snapshot(raw_partial)
            if result["status"] != "partial":
                return {"status": "error", "message": "部分缺失快照应返回 partial 状态"}
            if len(result["missing_fields"]) != 3:  # 缺少听觉、嗅觉、味觉
                return {"status": "error", "message": f"缺失字段数不符: {result['missing_fields']}"}

            # 测试3：验证边界钳制
            raw_with_outlier = {
                "visual": {"pattern_confidence": 1.5, "orderbook_slope": 10.0},
                "auditory": {"sentiment_score": -2.0},
                "tactile": {"liquidity_level": "L6"},
                "olfactory": {},
                "gustatory": {},
            }
            snapshot_result = instance.create_snapshot(raw_with_outlier)
            validated = instance.validate_snapshot(snapshot_result["snapshot"])
            if len(validated["clamped_fields"]) < 3:
                return {"status": "error", "message": f"边界钳制字段数不足: {validated['clamped_fields']}"}

            return {"status": "ok", "message": "所有测试通过"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    @classmethod
    def _get_defaults(cls, sense: str) -> Dict[str, Any]:
        """获取指定感官的完整默认值字典（返回副本，防止外部修改类常量）"""
        defaults_map = {
            "visual": cls.DEFAULT_VISUAL,
            "auditory": cls.DEFAULT_AUDITORY,
            "tactile": cls.DEFAULT_TACTILE,
            "olfactory": cls.DEFAULT_OLFACTORY,
            "gustatory": cls.DEFAULT_GUSTATORY,
        }
        return dict(defaults_map.get(sense, {}))

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        """数值边界钳制"""
        return max(lower, min(upper, value))
