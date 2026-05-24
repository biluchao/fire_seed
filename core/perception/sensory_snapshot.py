"""
火种系统 · 感官快照标准化接口 (SensorySnapshot)

核心职责：
1. 将五感皮层的原始输出统一封装为标准化快照对象，供下游决策模块使用。
2. 对输入数据进行完整性校验与缺失值填充，确保下游模块可安全读取所有字段。

外部依赖（真实模块接口）：
- 无外部模块依赖。本模块仅定义数据容器与校验逻辑。

接口契约：
- create_snapshot(raw_data: Dict[str, Any]) -> Dict[str, Any]
- validate_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]
- health_check() -> Dict[str, Any]

异常与降级：
- 感官字段缺失时使用保守默认值填充，状态标记为 "partial" 或 "degraded"。

资源管理：
- 本模块为纯工具类，不持有外部资源。
"""

import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class SensorySnapshot:
    """感官快照标准化接口"""

    DEFAULT_VISUAL = {"candlestick_pattern": "none", "ma12_position": "on_line", "orderbook_slope": 0.0, "wall_resilience": "unknown"}
    DEFAULT_AUDITORY = {"macro_alert_level": "none", "time_to_next_event_sec": 999999.0, "sentiment_score": 0.0, "sentiment_momentum": 0.0, "expected_impact_multiplier": 1.0, "sentiment_extreme": False}
    DEFAULT_TACTILE = {"liquidity_level": "L1", "depth_decay_speed": 10.0, "trade_pulse_cv": 1.5, "market_participant": "unknown", "volatility_regime": "expanding"}
    DEFAULT_OLFACTORY = {"paper_wall_flag": False, "spread_manipulation_flag": False, "contagion_risk_index": 0.0, "order_toxicity_active": False, "toxicity_score": 0.0}
    DEFAULT_GUSTATORY = {"similar_win_rate": 0.5, "bitter_similarity": 0.0, "sweet_memory_count": 0, "bitter_memory_count": 0}

    REQUIRED_SENSES = ["visual", "auditory", "tactile", "olfactory", "gustatory"]
    _DEFAULTS = {
        "visual": DEFAULT_VISUAL, "auditory": DEFAULT_AUDITORY, "tactile": DEFAULT_TACTILE,
        "olfactory": DEFAULT_OLFACTORY, "gustatory": DEFAULT_GUSTATORY,
    }

    @classmethod
    def create_snapshot(cls, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """基于五感原始数据创建标准化快照。"""
        snapshot = {}
        missing = []
        warnings = []
        for sense in cls.REQUIRED_SENSES:
            defaults = dict(cls._DEFAULTS.get(sense, {}))
            if sense in raw_data and isinstance(raw_data[sense], dict):
                snapshot[sense] = {**defaults, **raw_data[sense]}
            else:
                snapshot[sense] = defaults
                missing.append(sense)
                warnings.append(f"感官 {sense} 缺失，使用降级默认值")
        status = "degraded" if len(missing) == len(cls.REQUIRED_SENSES) else ("partial" if missing else "ok")
        reason = f"快照创建完成，缺失: {missing}" if missing else "快照创建完成，所有感官完整"
        return {"status": status, "snapshot": snapshot, "reason": reason, "missing_fields": missing, "warnings": warnings}

    @classmethod
    def validate_snapshot(cls, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """验证并修复一个已存在的感官快照。"""
        if not isinstance(snapshot, dict):
            return {"status": "error", "snapshot": {}, "reason": "输入不是字典", "missing_fields": cls.REQUIRED_SENSES, "warnings": []}
        missing = []
        for sense in cls.REQUIRED_SENSES:
            if sense not in snapshot or not isinstance(snapshot[sense], dict):
                missing.append(sense)
                snapshot[sense] = dict(cls._DEFAULTS.get(sense, {}))
        return {"status": "ok" if not missing else "partial", "snapshot": snapshot,
                "reason": f"验证完成，补全: {missing}" if missing else "快照验证通过", "missing_fields": missing, "warnings": []}

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检。"""
        try:
            r = cls.create_snapshot({})
            if r["status"] != "degraded":
                return {"status": "error", "message": "全量默认快照创建异常"}
            r2 = cls.create_snapshot({"visual": {"ma12_position": "near"}, "tactile": {"liquidity_level": "L4"}})
            if r2["status"] != "partial":
                return {"status": "error", "message": "部分缺失快照创建异常"}
            return {"status": "ok", "message": "所有测试通过"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}
