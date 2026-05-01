#!/usr/bin/env python3
"""
火种系统 (FireSeed) 权重校准师智能体 (WeightCalibrator)
============================================================
定期执行以下审计与校验任务：
- 因子权重自洽性检查（总和是否为1，负权重合法性）
- 智能体议会投票权重归一化检查
- 模型融合权重覆盖率检查
- 信号泄漏检测（同一信号被多处计入）
- 敏感性分析（微小扰动对评分的影响）
- 异常结果自动写入行为日志，必要时触发告警
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

logger = logging.getLogger("fire_seed.weight_calibrator")


class WeightCalibrator:
    """
    权重校准师智能体。
    负责对系统中所有涉及“权重”的部分进行自洽性验证与敏感性分析。
    """

    def __init__(self,
                 config_path: str = "config",
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None):
        self.config_dir = Path(config_path)
        self.behavior_log = behavior_log
        self.notifier = notifier
        self.issues: List[str] = []
        self.sensitivity_report: Dict[str, Any] = {}

    async def run_full_audit(self) -> Dict[str, Any]:
        """
        执行完整的权重审计。
        返回结果字典，包含 issues 列表和总体状态。
        """
        self.issues = []

        # 1. 评分卡权重自洽性
        self._check_scorecard_weights()

        # 2. 议会投票权重
        self._check_council_weights()

        # 3. 信号泄漏检测
        await self._detect_weight_leakage()

        # 4. 敏感性分析（若引擎可用）
        await self._sensitivity_analysis()

        # 5. 写入行为日志
        total_issues = len(self.issues)
        status = "OK" if total_issues == 0 else "WARNING"
        if self.behavior_log:
            self.behavior_log.log(
                EventType.AGENT,
                "WeightCalibrator",
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

    # ==================== 评分卡权重检查 ====================
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

        # 总和检查
        if abs(total_weight - 1.0) > 0.01:
            self.issues.append(f"因子权重总和 {total_weight:.4f} 偏离 1.0")

        # 负权重合法性：仅允许标记为 penalty 的因子为负
        for name, w in all_factors.items():
            if w < 0 and not name.startswith("penalty_") and name not in ("penalty_signal", "risk_penalty"):
                self.issues.append(f"因子 {name} 权重为负 ({w})，但未标记为扣分项")

        # 极端权重检测（单个因子超过50%）
        for name, w in all_factors.items():
            if w > 0.5:
                self.issues.append(f"因子 {name} 权重过高 ({w})，存在单一依赖风险")

    # ==================== 议会投票权重检查 ====================
    def _check_council_weights(self) -> None:
        """检查 agent_rewards.yaml 中的投票权重是否合理"""
        reward_file = self.config_dir / "agent_rewards.yaml"
        if not reward_file.exists():
            return

        try:
            with open(reward_file, "r") as f:
                data = yaml.safe_load(f)
        except Exception:
            return

        agents = data.get("agents", {})
        if not agents:
            return

        # 检查各个智能体的权重定义是否在合理范围
        for name, cfg in agents.items():
            components = cfg.get("reward_components", [])
            total_w = sum(c.get("weight", 0) for c in components)
            if total_w > 1.5:
                self.issues.append(f"智能体 {name} 的奖励权重总和 {total_w:.2f} 过高")

    # ==================== 信号泄漏检测 ====================
    async def _detect_weight_leakage(self) -> None:
        """
        检测同一个底层信号是否通过多个因子路径被重复计入最终评分。
        需要从引擎获取当前活跃的信号定义表。
        """
        try:
            engine = get_engine()
            if engine is None:
                return
            # 从因子惰性求值器获取当前因子到原始特征的映射
            if not hasattr(engine, 'lazy_evaluator'):
                return
            factor_calc = engine.lazy_evaluator._factor_calculator
            if factor_calc is None:
                return
            # 假设因子计算器提供 get_factor_dependencies 方法，返回 {factor: [base_feature]}
            deps = getattr(factor_calc, 'get_factor_dependencies', lambda: {})()
            if not deps:
                return

            feature_to_factors = defaultdict(list)
            for factor, features in deps.items():
                for feat in features:
                    feature_to_factors[feat].append(factor)

            for feat, factors in feature_to_factors.items():
                if len(factors) > 1:
                    # 检查这些因子是否同时具有非零权重
                    weight_file = self.config_dir / "weights.yaml"
                    if weight_file.exists():
                        with open(weight_file, "r") as f:
                            data = yaml.safe_load(f)
                        active = [
                            fac for fac in factors
                            if data.get("core_factors", {}).get(fac, data.get("auxiliary_factors", {}).get(fac, 0)) > 0
                        ]
                        if len(active) > 1:
                            self.issues.append(
                                f"信号泄漏: 特征 '{feat}' 被多个活跃因子计入: {active}"
                            )
        except Exception as e:
            logger.debug(f"信号泄漏检测跳过: {e}")

    # ==================== 敏感性分析 ====================
    async def _sensitivity_analysis(self) -> None:
        """
        对评分卡进行参数敏感性分析。
        若引擎不可用则跳过。
        """
        try:
            engine = get_engine()
            if engine is None or not hasattr(engine, 'scorecard'):
                return

            sc = engine.scorecard
            # 获取当前权重和基准输入
            weights = sc._weights if hasattr(sc, '_weights') else {}
            if not weights:
                return

            # 基准评分（使用一组中性的因子得分）
            factor_names = list(weights.keys())
            base_scores = {name: 0.0 for name in factor_names}  # 假设0表示中性
            base_output = sc.compute(base_scores)

            sensitivities = {}
            for name in factor_names:
                if weights.get(name, 0) == 0:
                    continue
                # 扰动该因子的权重 ±10%
                perturbed_weights = weights.copy()
                delta = max(0.01, weights[name] * 0.1)
                for sign, label in [(1, "up"), (-1, "down")]:
                    perturbed_weights[name] = weights[name] + sign * delta
                    # 重新归一化
                    total = sum(perturbed_weights.values())
                    if total > 0:
                        perturbed_weights = {k: v / total for k, v in perturbed_weights.items()}
                    # 计算扰动后的输出（使用相同的中性得分）
                    perturbed_output = sc.compute(base_scores, external_weights=perturbed_weights)
                    key = f"{name}_{label}"
                    sensitivities[key] = round(abs(perturbed_output - base_output), 4)

            # 标记高敏感因子
            for name in factor_names:
                up_key = f"{name}_up"
                down_key = f"{name}_down"
                up_val = sensitivities.get(up_key, 0)
                down_val = sensitivities.get(down_key, 0)
                if max(up_val, down_val) > 3.0:  # 评分变化超过3点
                    self.issues.append(
                        f"高敏感因子: {name} (扰动±10% 评分变化 {max(up_val, down_val):.2f})"
                    )

            self.sensitivity_report = sensitivities
        except Exception as e:
            logger.warning(f"敏感性分析失败: {e}")

    # ==================== 对外接口 ====================
    def get_status(self) -> Dict[str, Any]:
        return {
            "last_issues": self.issues[-10:],
            "total_issues": len(self.issues),
            "sensitivity_available": bool(self.sensitivity_report),
        }
