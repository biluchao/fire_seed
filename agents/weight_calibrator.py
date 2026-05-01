#!/usr/bin/env python3
"""
火种系统 (FireSeed) 权重校准师智能体 (WeightCalibrator) —— 贝叶斯主义
=========================================================================
继承 WorldViewAgent，携带“概率是信念的量化，需不断用证据更新”的世界观。
负责：
- 因子权重自洽性审计（总和、负权重、单一依赖）
- 议会投票权重重审核
- 信号泄漏检测（同一特征被多个因子复用）
- 敏感性分析（扰动权重观察评分波动）
- 基于贝叶斯动态线性模型的权重平滑建议

在对抗式议会中：
- propose()：基于近期样本内外夏普差距与因子IC稳定性，提议冻结或调整因子权重
- challenge()：从“过拟合风险”角度挑战其他提案（尤其是过度激进地提升某些因子权重的提案）
"""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import yaml

from api.server import get_engine
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier
from agents.worldview import WorldViewAgent, WorldViewManifesto, WorldView
from agents.extreme_rewards import ExtremeRewardFunctions

logger = logging.getLogger("fire_seed.weight_calibrator")


class WeightCalibrator(WorldViewAgent):
    """权重校准师·贝叶斯主义"""

    def __init__(self,
                 config_path: str = "config",
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None):
        # 构建贝叶斯主义世界观宣言
        manifesto = WorldViewManifesto(
            worldview=WorldView.BAYESIANISM,
            core_belief="概率是信念的量化，需不断用证据更新",
            primary_optimization_target="OOS_sharpe - IS_sharpe",
            adversary_worldview=WorldView.SKEPTICISM,
            forbidden_data_source={"RAW_PRICE", "RAW_ORDERBOOK"},
            exclusive_data_source={"IC_SERIES", "FACTOR_WEIGHTS", "CONFIG"},
            time_scale="86400",
        )
        super().__init__(manifesto)

        self.config_dir = Path(config_path)
        self.behavior_log = behavior_log
        self.notifier = notifier
        self.issues: List[str] = []
        self.sensitivity_report: Dict[str, Any] = {}

        # 奖励函数引用（用于计算自身偏差时参考）
        self.extreme_rewards = ExtremeRewardFunctions()

        # 内部状态
        self._last_full_audit_time: float = 0.0

    # ======================== 对抗式议会接口 ========================
    async def propose(self, perception: Dict[str, Any]) -> Dict[str, Any]:
        """
        基于自身世界观提出关于“是否应调整因子权重”的提案。
        感知数据中应包含:
        - 'oos_sharpe': 近期样本外夏普
        - 'is_sharpe': 近期样本内夏普
        - 'factor_ic_map': {因子名: 近期IC序列}
        如果样本外夏普显著低于样本内，或某个因子的IC持续衰减，本智能体将建议冻结或降权。
        """
        try:
            oos = perception.get("oos_sharpe", 0.0)
            ins = perception.get("is_sharpe", 0.0)
            ic_map = perception.get("factor_ic_map", {})

            proposal = {
                "action": "no_change",
                "target": None,
                "confidence": 0.0,
                "direction": 0,
                "reason": "当前模型表现稳健，无需调整",
            }

            # 贝叶斯主义的极端化行为：宁可过度保守也不放过过拟合嫌疑
            gap = ins - oos
            if gap > 0.3:  # 过拟合嫌疑
                proposal["action"] = "freeze_aggressive_factors"
                proposal["target"] = "high_ic_factors"
                proposal["confidence"] = min(0.9, gap)
                proposal["direction"] = -1  # 反对激进调整
                proposal["reason"] = f"样本内外夏普差距 {gap:.2f}，推测存在过拟合，建议冻结高IC因子权重"
                self._log("提出系统过拟合预警提案", proposal)
                return proposal

            # 检查是否有因子IC持续衰减
            for factor, ic_series in ic_map.items():
                if len(ic_series) < 10:
                    continue
                recent = ic_series[-10:]
                if np.mean(recent) < 0.02 and np.std(recent) < 0.02:
                    proposal["action"] = "freeze_factor"
                    proposal["target"] = factor
                    proposal["confidence"] = 0.65
                    proposal["direction"] = -1
                    proposal["reason"] = f"因子 {factor} 近期IC持续走低，建议冻结观察"
                    self._log(f"检测到因子 {factor} 失效", proposal)
                    return proposal

            self._log("权重校准师提案：维持现状", proposal)
            return proposal

        except Exception as e:
            logger.error(f"权重校准师提案生成失败: {e}")
            return {"action": "no_change", "confidence": 0.0, "reason": str(e)}

    async def challenge(self, other_proposal: Dict[str, Any], my_worldview: WorldView) -> Dict[str, Any]:
        """
        从贝叶斯主义的世界观挑战其他提案。
        主要挑战点：提案是否可能导致模型过拟合？是否忽略了样本外验证？
        """
        challenges = []
        veto = False

        # 如果提案涉及“增加某个因子的权重”或“新因子激活”
        action = other_proposal.get("action", "")
        if action in ("activate_factor", "increase_weight", "add_factor"):
            # 贝叶斯主义要求必须有样本外验证
            if not other_proposal.get("oos_validated", False):
                challenges.append("该提案未提供样本外验证结果，可能引入过拟合风险")
                veto = True
            else:
                oos_perf = other_proposal.get("oos_performance", 0.0)
                if oos_perf < 0.1:  # 要求样本外夏普至少0.1
                    challenges.append(f"样本外表现过低 ({oos_perf:.2f})，不应采纳")

        # 挑战过度频繁的调整
        if other_proposal.get("adjustment_interval_days", 30) < 7:
            challenges.append("调整频率过高，权重可能引入噪声而非信号")
            veto = True

        reason = "; ".join(challenges) if challenges else "未发现过拟合风险"
        result = {
            "veto": veto,
            "reason": reason,
            "challenges": challenges,
            "confidence_adjustment": -0.2 if veto else 0.0,
        }

        self._log(f"挑战提案: {other_proposal.get('action')} -> 否决={veto}", result)
        return result

    # ======================== 传统审计功能（保留） ========================
    async def run_full_audit(self) -> Dict[str, Any]:
        """
        执行完整的权重审计。返回结果字典，包含 issues 列表和总体状态。
        """
        self.issues = []

        self._check_scorecard_weights()
        self._check_council_weights()
        await self._detect_weight_leakage()
        await self._sensitivity_analysis()

        total_issues = len(self.issues)
        status = "OK" if total_issues == 0 else "WARNING"
        if self.behavior_log:
            self.behavior_log.log(
                EventType.AGENT, "WeightCalibrator",
                f"权重审计完成，问题数: {total_issues}",
                snapshot={"status": status, "issues_count": total_issues}
            )

        return {
            "status": status,
            "issues": self.issues,
            "sensitivity": self.sensitivity_report,
            "issue_count": total_issues,
            "timestamp": datetime.now().isoformat()
        }

    def _check_scorecard_weights(self) -> None:
        """从 weights.yaml 加载因子权重，检查自洽性"""
        weight_file = self.config_dir / "weights.yaml"
        if not weight_file.exists():
            self.issues.append("权重文件 config/weights.yaml 不存在")
            return

        try:
            with open(weight_file, "r") as f:
                data = yaml.safe_load(f)
        except Exception as e:
            self.issues.append(f"权重文件解析失败: {e}")
            return

        core = data.get("core_factors", {})
        aux = data.get("auxiliary_factors", {})
        all_factors = {**core, **aux}
        total_weight = sum(all_factors.values())

        if abs(total_weight - 1.0) > 0.01:
            self.issues.append(f"因子权重总和 {total_weight:.4f} 偏离 1.0")

        for name, w in all_factors.items():
            if w < 0 and "penalty" not in name.lower():
                self.issues.append(f"因子 {name} 权重为负 ({w})，但未标记为扣分项")
            if w > 0.5:
                self.issues.append(f"因子 {name} 权重过高 ({w})，存在单一依赖风险")

    def _check_council_weights(self) -> None:
        reward_file = self.config_dir / "agent_rewards.yaml"
        if not reward_file.exists():
            return
        try:
            with open(reward_file, "r") as f:
                data = yaml.safe_load(f)
        except Exception:
            return
        agents = data.get("agents", {})
        for name, cfg in agents.items():
            components = cfg.get("reward_components", [])
            total_w = sum(abs(c.get("weight", 0)) for c in components)
            if total_w > 1.5:
                self.issues.append(f"智能体 {name} 的奖励权重总和 {total_w:.2f} 过高")

    async def _detect_weight_leakage(self) -> None:
        try:
            engine = get_engine()
            if engine is None:
                return
            if not hasattr(engine, 'lazy_evaluator'):
                return
            factor_calc = engine.lazy_evaluator._factor_calculator
            if factor_calc is None:
                return
            deps = getattr(factor_calc, 'get_factor_dependencies', lambda: {})()
            if not deps:
                return
            feature_to_factors = defaultdict(list)
            for factor, features in deps.items():
                for feat in features:
                    feature_to_factors[feat].append(factor)
            weight_file = self.config_dir / "weights.yaml"
            if weight_file.exists():
                with open(weight_file, "r") as f:
                    data = yaml.safe_load(f)
                core = data.get("core_factors", {})
                aux = data.get("auxiliary_factors", {})
                active_weights = {k: v for k, v in {**core, **aux}.items() if v > 0}
                for feat, factors in feature_to_factors.items():
                    active = [f for f in factors if f in active_weights]
                    if len(active) > 1:
                        self.issues.append(
                            f"信号泄漏: 特征 '{feat}' 被多个活跃因子计入: {active}"
                        )
        except Exception as e:
            logger.debug(f"信号泄漏检测跳过: {e}")

    async def _sensitivity_analysis(self) -> None:
        try:
            engine = get_engine()
            if engine is None or not hasattr(engine, 'scorecard'):
                return
            sc = engine.scorecard
            weights = sc._weights if hasattr(sc, '_weights') else {}
            if not weights:
                return
            factor_names = list(weights.keys())
            base_scores = {name: 0.0 for name in factor_names}
            base_output = sc.compute(base_scores)

            sensitivities = {}
            for name in factor_names:
                w0 = weights.get(name, 0.0)
                if w0 == 0.0:
                    continue
                for sign, label in [(1, "up"), (-1, "down")]:
                    perturbed = weights.copy()
                    delta = max(0.01, w0 * 0.1)
                    perturbed[name] = w0 + sign * delta
                    total = sum(perturbed.values())
                    if total > 0:
                        perturbed = {k: v / total for k, v in perturbed.items()}
                    perturbed_output = sc.compute(base_scores, external_weights=perturbed)
                    key = f"{name}_{label}"
                    sensitivities[key] = round(abs(perturbed_output - base_output), 4)

            for name in factor_names:
                up = sensitivities.get(f"{name}_up", 0)
                down = sensitivities.get(f"{name}_down", 0)
                if max(up, down) > 3.0:
                    self.issues.append(
                        f"高敏感因子: {name} (扰动±10% 评分变化 {max(up, down):.2f})"
                    )
            self.sensitivity_report = sensitivities
        except Exception as e:
            logger.warning(f"敏感性分析失败: {e}")

    # ======================== 内部辅助 ========================
    def _log(self, message: str, snapshot: Optional[Dict] = None) -> None:
        if self.behavior_log:
            self.behavior_log.log(
                EventType.AGENT, "WeightCalibrator", message, snapshot or {}
            )
        logger.info(f"[Bayesian] {message}")

    def get_status(self) -> Dict[str, Any]:
        return {
            "worldview": self.manifesto.worldview.value,
            "last_issues": self.issues[-10:],
            "total_issues": len(self.issues),
            "sensitivity_available": bool(self.sensitivity_report),
        }
