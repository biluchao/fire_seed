"""
火种系统 · 感知中枢 (PerceptionHub) — 深度精细化

核心职责：
1. 作为五感皮层（视觉、听觉、触觉、嗅觉、味觉）、因子预处理器和多频段锁相环的统一入口，为下游策略引擎提供一站式感知服务。
2. 管理所有子模块的依赖注入、生命周期，并对外暴露标准化的 perceive() 接口，返回融合后的感官快照。

外部依赖（真实模块接口）：
- core.perception.visual_cortex.VisualCortex : 视觉皮层
- core.perception.auditory_cortex.AuditoryCortex : 听觉皮层
- core.perception.tactile_cortex.TactileCortex : 触觉皮层
- core.perception.olfactory_cortex.OlfactoryCortex : 嗅觉皮层
- core.perception.gustatory_cortex.GustatoryCortex : 味觉皮层
- core.perception.factor_preprocessor.FactorPreprocessor : 因子预处理器
- core.perception.multi_band_pll.MultiBandPLL : 多频段锁相环
- core.perception.sensory_snapshot.SensorySnapshot : 感官快照标准化接口
- (外部注入) core.experience_replay.ExperienceReplay : 味觉皮层所需历史经验数据
- (外部注入) core.global_state_archive.GlobalStateArchive : 味觉皮层所需极端事件记忆

接口契约：
- perceive(market_data: Dict[str, Any]) -> Dict[str, Any]
  输出字典固定包含 "status" (str), "snapshot" (Dict[str, Any]), "reason" (str), "warnings" (List[str])
- inject_dependencies(experience_replay: Any = None, state_archive: Any = None) -> None
- health_check() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "message" (str)

异常与降级：
- 任何感官模块不可用时，对应感官字段使用安全默认值填充，并在 warnings 中记录，不影响其他感官运行。
- 输入数据缺失时，返回全量降级快照，状态标记为 "degraded"。
- 所有子模块初始化异常均被捕获，确保感知中枢始终可运行。

资源管理：
- 本模块不持有需要手动释放的资源。所有子模块为无状态或独立管理自身资源。
"""

import logging
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class PerceptionHub:
    """感知中枢：统一调度五感皮层与预处理模块"""

    # 类常量（默认配置）
    DEFAULT_CONFIG: Dict[str, Any] = {}

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        初始化所有子模块实例。
        
        Args:
            config: 可选的全局配置字典，用于传递给各子模块进行参数覆盖。
        """
        self._config = config if config is not None else self.DEFAULT_CONFIG

        # 按需创建各子模块，若导入失败则标记为不可用
        self._visual = self._safe_create("core.perception.visual_cortex", "VisualCortex", self._config)
        self._auditory = self._safe_create("core.perception.auditory_cortex", "AuditoryCortex", self._config)
        self._tactile = self._safe_create("core.perception.tactile_cortex", "TactileCortex", self._config)
        self._olfactory = self._safe_create("core.perception.olfactory_cortex", "OlfactoryCortex", self._config)
        self._gustatory = self._safe_create("core.perception.gustatory_cortex", "GustatoryCortex", self._config)
        self._preprocessor = self._safe_create("core.perception.factor_preprocessor", "FactorPreprocessor", self._config)
        self._pll = self._safe_create("core.perception.multi_band_pll", "MultiBandPLL", None)
        self._snapshot = self._safe_create("core.perception.sensory_snapshot", "SensorySnapshot", self._config)

        # 外部依赖（延迟注入）
        self._experience_replay: Optional[Any] = None
        self._state_archive: Optional[Any] = None

        # 输出子模块初始化状态
        logger.info(
            f"PerceptionHub 初始化完成: "
            f"视觉:{self._visual is not None}, 听觉:{self._auditory is not None}, "
            f"触觉:{self._tactile is not None}, 嗅觉:{self._olfactory is not None}, "
            f"味觉:{self._gustatory is not None}, 预处理器:{self._preprocessor is not None}, "
            f"PLL:{self._pll is not None}, 快照:{self._snapshot is not None}"
        )

    # ────────────────────────── 依赖注入 ──────────────────────────
    def inject_dependencies(
        self,
        experience_replay: Optional[Any] = None,
        state_archive: Optional[Any] = None,
        tactile: Optional[Any] = None
    ) -> None:
        """
        注入外部依赖，并传递给需要的子模块。
        
        Args:
            experience_replay: 经验回放池实例，传递给味觉皮层。
            state_archive: 全局状态存档实例，传递给味觉皮层。
            tactile: 外部触觉皮层实例（可选），若提供则覆盖内部创建的实例。
        """
        self._experience_replay = experience_replay
        self._state_archive = state_archive

        # 若外部提供了触觉皮层实例，则覆盖内部实例
        if tactile is not None:
            self._tactile = tactile
            logger.info("已注入外部触觉皮层实例")

        # 传递给味觉皮层
        if self._gustatory is not None:
            try:
                self._gustatory.inject_dependencies(
                    experience_replay=experience_replay,
                    state_archive=state_archive
                )
                logger.info("味觉皮层依赖注入完成")
            except Exception as e:
                logger.warning(f"味觉皮层依赖注入失败: {e}")

        # 若预处理器需要外部触觉皮层，也可在此注入
        if self._preprocessor is not None and self._tactile is not None:
            try:
                if hasattr(self._preprocessor, "inject_dependencies"):
                    self._preprocessor.inject_dependencies(tactile=self._tactile)
                    logger.info("因子预处理器已注入触觉皮层")
            except Exception as e:
                logger.warning(f"因子预处理器依赖注入失败: {e}")

    # ────────────────────────── 公共接口 ──────────────────────────
    def perceive(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行一次完整的多感官感知，返回融合后的标准化快照。
        
        Args:
            market_data: 包含各感官所需原始数据的字典，预期结构:
                {
                    "kline_sequence": List[Dict],       # K线序列
                    "orderbook_snapshot": Dict,         # 订单簿快照（需包含 ma12_value, atr_value）
                    "trade_stream": List[Dict],          # 逐笔成交流
                    "atr_short": float,                  # 短期ATR
                    "atr_long": float,                   # 长期ATR
                    "price_series": List[float],          # 价格序列（供PLL）
                    "factor_values": Dict[str, List[float]], # 因子原始值字典
                    "state_vector": Dict[str, float],    # 市场状态向量（供味觉）
                    "macro_events": Optional[List[Dict]],  # 宏观事件列表
                    "sentiment_data": Optional[Dict],     # 情绪数据
                    "correlation_matrix": Optional[Dict], # 多品种相关性矩阵（供嗅觉）
                }
        
        Returns:
            标准化感知快照字典，包含 "snapshot" 字段，其内为各感官子快照。
        """
        warnings: List[str] = []
        raw_sensory: Dict[str, Any] = {}

        # 1. 视觉感知
        raw_sensory["visual"] = self._safe_sense(
            self._visual, "perceive",
            kline_sequence=market_data.get("kline_sequence"),
            orderbook_snapshot=market_data.get("orderbook_snapshot"),
            warnings=warnings, sense_name="视觉",
        )

        # 2. 听觉感知
        raw_sensory["auditory"] = self._safe_sense(
            self._auditory, "listen",
            macro_events=market_data.get("macro_events"),
            sentiment_data=market_data.get("sentiment_data"),
            warnings=warnings, sense_name="听觉",
        )

        # 3. 触觉感知
        raw_sensory["tactile"] = self._safe_sense(
            self._tactile, "sense",
            orderbook_snapshot=market_data.get("orderbook_snapshot"),
            trade_stream=market_data.get("trade_stream"),
            atr_short=market_data.get("atr_short"),
            atr_long=market_data.get("atr_long"),
            warnings=warnings, sense_name="触觉",
        )

        # 4. 嗅觉感知
        raw_sensory["olfactory"] = self._safe_sense(
            self._olfactory, "smell",
            orderbook_snapshot=market_data.get("orderbook_snapshot"),
            trade_stream=market_data.get("trade_stream"),
            correlation_matrix=market_data.get("correlation_matrix"),
            warnings=warnings, sense_name="嗅觉",
        )

        # 5. 味觉感知
        raw_sensory["gustatory"] = self._safe_sense(
            self._gustatory, "taste",
            state_vector=market_data.get("state_vector", {}),
            warnings=warnings, sense_name="味觉",
        )

        # 6. 通过 SensorySnapshot 创建并验证快照
        snapshot_result = self._create_snapshot(raw_sensory, warnings)

        # 7. 附加 PLL 信号到快照
        pll_signal = self._get_pll_signal(market_data.get("price_series", []), warnings)
        snapshot_result["snapshot"]["pll_signal"] = pll_signal

        # 8. 附加因子质量信息（可选）
        factor_quality = self._get_factor_quality(market_data.get("factor_values"), warnings)
        snapshot_result["snapshot"]["factor_quality"] = factor_quality

        # 聚合状态
        status = snapshot_result.get("status", "ok")
        reason = snapshot_result.get("reason", "感知融合完成")
        warnings.extend(snapshot_result.get("warnings", []))

        return {
            "status": status,
            "snapshot": snapshot_result["snapshot"],
            "reason": reason,
            "warnings": warnings,
        }

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检：检查所有子模块是否可正常创建并执行各自健康检查。"""
        try:
            instance = cls()
            failures = []
            modules_map = {
                "visual": instance._visual,
                "auditory": instance._auditory,
                "tactile": instance._tactile,
                "olfactory": instance._olfactory,
                "gustatory": instance._gustatory,
                "preprocessor": instance._preprocessor,
                "pll": instance._pll,
                "snapshot": instance._snapshot,
            }
            for name, mod in modules_map.items():
                if mod is None:
                    failures.append(f"{name} 创建失败")
                else:
                    try:
                        hc = mod.health_check()
                        if hc.get("status") != "ok":
                            failures.append(f"{name}: {hc.get('message', '健康检查未通过')}")
                    except Exception as e:
                        failures.append(f"{name} 健康检查异常: {str(e)[:100]}")

            if failures:
                return {"status": "error", "message": "; ".join(failures)}
            return {"status": "ok", "message": "所有子模块自检通过"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    @staticmethod
    def _safe_create(module_path: str, class_name: str, config: Optional[Dict[str, Any]]) -> Optional[Any]:
        """安全创建模块实例，导入失败返回 None。"""
        try:
            import importlib
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
            return cls(config) if config else cls()
        except Exception as e:
            logger.warning(f"创建 {module_path}.{class_name} 失败: {e}")
            return None

    def _safe_sense(
        self,
        module: Optional[Any],
        method: str,
        warnings: List[str],
        sense_name: str,
        **kwargs
    ) -> Dict[str, Any]:
        """安全调用感官模块方法，失败时返回该感官的降级默认值。"""
        if module is None:
            warnings.append(f"{sense_name}皮层不可用，使用降级值")
            return self._get_default(sense_name)

        try:
            func = getattr(module, method)
            result = func(**kwargs)
            if isinstance(result, dict) and "warnings" in result:
                warnings.extend(result["warnings"])
            return result if isinstance(result, dict) else {}
        except Exception as e:
            logger.error(f"{sense_name}感知异常: {e}", exc_info=True)
            warnings.append(f"{sense_name}感知失败: {str(e)[:100]}")
            return self._get_default(sense_name)

    def _create_snapshot(self, raw_sensory: Dict[str, Any], warnings: List[str]) -> Dict[str, Any]:
        """调用 SensorySnapshot 创建并验证标准化快照。"""
        if self._snapshot is None:
            warnings.append("SensorySnapshot 不可用，使用原始数据")
            return {"status": "warning", "snapshot": raw_sensory, "reason": "快照接口不可用", "warnings": warnings}

        try:
            created = self._snapshot.create_snapshot(raw_sensory)
            validated = self._snapshot.validate_snapshot(created["snapshot"])
            warnings.extend(validated.get("warnings", []))
            return validated
        except Exception as e:
            logger.error(f"快照创建/验证异常: {e}", exc_info=True)
            warnings.append(f"快照处理失败: {str(e)[:100]}")
            return {"status": "warning", "snapshot": raw_sensory, "reason": "快照处理异常", "warnings": warnings}

    def _get_pll_signal(self, price_series: List[float], warnings: List[str]) -> Dict[str, Any]:
        """获取多频段PLL融合信号。"""
        if self._pll is None:
            warnings.append("PLL不可用，返回默认信号")
            return {"trend_direction": 0, "trend_strength": 0.0, "consensus_count": 0, "locked_count": 0}

        try:
            if price_series:
                for p in price_series[-max(1, self._pll.MIN_PRICES_FOR_LOCK):]:
                    self._pll.update(p)
                return self._pll.get_fusion_signal()
            else:
                return {"trend_direction": 0, "trend_strength": 0.0, "consensus_count": 0, "locked_count": 0}
        except Exception as e:
            logger.error(f"PLL 信号获取异常: {e}", exc_info=True)
            warnings.append(f"PLL 信号获取失败: {str(e)[:100]}")
            return {"trend_direction": 0, "trend_strength": 0.0, "consensus_count": 0, "locked_count": 0}

    def _get_factor_quality(self, factor_values: Optional[Dict[str, List[float]]], warnings: List[str]) -> Dict[str, Any]:
        """获取因子预处理质量报告。"""
        if self._preprocessor is None or not factor_values:
            return {"status": "unavailable", "reason": "预处理器不可用或无因子数据"}

        try:
            # 对每个因子执行预处理，返回质量标记
            quality = {}
            for name, values in factor_values.items():
                res = self._preprocessor.process(values, name)
                quality[name] = {"status": res["status"], "warnings": res.get("warnings", [])}
                warnings.extend(res.get("warnings", []))
            return {"status": "ok", "factors": quality}
        except Exception as e:
            logger.error(f"因子质量分析异常: {e}", exc_info=True)
            warnings.append(f"因子质量分析失败: {str(e)[:100]}")
            return {"status": "error", "reason": str(e)[:100]}

    @staticmethod
    def _get_default(sense: str) -> Dict[str, Any]:
        """获取指定感官的完整降级默认值（引用 SensorySnapshot 默认值）。"""
        # 引用 SensorySnapshot 的默认值，避免重复定义
        try:
            from core.perception.sensory_snapshot import SensorySnapshot
            return SensorySnapshot._get_defaults(sense)
        except Exception:
            return {}  # 极端降级
