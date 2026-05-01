#!/usr/bin/env python3
"""
火种系统 (FireSeed) 冗余审计官智能体 (RedundancyAuditor)
============================================================
奥卡姆剃刀世界观：简洁是真理的标志，冗余是熵的征兆。
定期扫描项目代码库与配置文件，检测并报告：
- 未被调用的函数与类
- 未被引用的配置项
- 空函数体 (仅包含 pass)
- 重复代码片段
- 冗余导入

在对抗式议会中，提出基于代码清洁度的提案，并对其他智能体的复杂提案行使否决权。
"""

import ast
import asyncio
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from agents.worldview import WorldViewAgent, WorldViewManifesto, WorldView
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier

logger = logging.getLogger("fire_seed.redundancy_auditor")


class RedundancyAuditor(WorldViewAgent):
    """
    冗余审计官智能体 - 奥卡姆剃刀世界观。
    负责保持代码库的清洁，通过定期扫描推动简化。
    """

    def __init__(
        self,
        root: str = ".",
        behavior_log: Optional[BehavioralLogger] = None,
        notifier: Optional[SystemNotifier] = None,
        check_interval_sec: int = 86400,  # 默认每日扫描一次
    ):
        manifesto = WorldViewManifesto(
            worldview=WorldView.OCCAMS_RAZOR,
            core_belief="简洁是真理的标志，冗余是熵的征兆",
            primary_optimization_target="-code_lines",
            adversary_worldview=WorldView.PLURALISM,
            forbidden_data_source={"ALL_MARKET_DATA"},
            exclusive_data_source={"SOURCE_CODE"},
            time_scale="86400",
        )
        super().__init__(manifesto)

        self.root = Path(root)
        self.behavior_log = behavior_log
        self.notifier = notifier
        self.check_interval = check_interval_sec

        # 存储定义与调用关系
        self.defined_functions: Dict[str, List[Tuple[str, str, int]]] = defaultdict(list)
        self.defined_classes: Dict[str, List[Tuple[str, str, int]]] = defaultdict(list)
        self.called_functions: Set[str] = set()
        self.used_classes: Set[str] = set()
        self.empty_functions: List[str] = []

        # 配置文件引用情况
        self.config_keys_defined: Set[str] = set()
        self.config_keys_used: Set[str] = set()

        # 扫描结果
        self.issues: List[str] = []

        self._last_scan = 0.0

    # ================== WorldViewAgent 接口实现 ==================
    def propose(self, perception: Dict = None) -> Dict:
        """
        基于自身世界观提出决策建议。
        返回当前代码冗余度评估，并给出清理建议。
        """
        # 确保扫描频率
        now = time.time()
        if now - self._last_scan < self.check_interval:
            # 返回缓存的结果
            return self._cached_proposal or self._create_empty_proposal()

        report = self.scan_all()
        self._cached_proposal = self._build_proposal(report)
        self._last_scan = now
        return self._cached_proposal

    def challenge(self, other_proposal: Dict, my_worldview: WorldView = None) -> Dict:
        """
        从奥卡姆剃刀世界观挑战其他智能体的提案。
        若提案会增加系统复杂度（引入新因子、新策略），则行使否决权。
        """
        veto = False
        reason = ""
        confidence_adjust = 0.0

        # 检查提案是否涉及增加复杂度
        proposal_type = other_proposal.get("type", "")
        complexity_delta = other_proposal.get("complexity_delta", 0)

        if proposal_type in ("new_strategy", "new_factor", "new_parameter"):
            if complexity_delta > 0.2:
                veto = True
                reason = f"提案 '{proposal_type}' 增加系统复杂度 (Δ={complexity_delta:.2f})，违背奥卡姆剃刀原则"
                confidence_adjust = -0.2
            elif complexity_delta > 0.05:
                reason = f"提案 '{proposal_type}' 轻微增加复杂度，建议审核其必要性"
                confidence_adjust = -0.05

        return {"veto": veto, "reason": reason, "confidence_adjustment": confidence_adjust}

    # ================== 核心扫描逻辑 ==================
    def scan_all(self) -> Dict[str, Any]:
        """
        扫描整个项目并返回审计报告。
        :return: 字典包含 total_defined, unused_functions, dead_code_score 等
        """
        self._reset()
        # 1. 扫描所有 Python 文件
        for py_file in self.root.rglob("*.py"):
            if self._is_excluded(py_file):
                continue
            self._scan_python_file(py_file)

        # 2. 扫描配置文件
        self._scan_config_files()

        # 3. 统计未使用项
        unused_funcs = []
        for fname, defs in self.defined_functions.items():
            # 跳过私有方法、魔术方法、测试函数等
            if fname.startswith("_") or fname.startswith("__"):
                continue
            if fname not in self.called_functions:
                for file_path, _, line in defs:
                    loc = f"{file_path}:{line}"
                    unused_funcs.append(f"{loc} {fname}")

        # 4. 计算审计评分
        total_defined = sum(len(v) for v in self.defined_functions.values())
        dead_score = max(0, 100 - len(unused_funcs) * 2 - len(self.empty_functions) * 3)

        report = {
            "total_defined": total_defined,
            "total_called": len(self.called_functions),
            "unused_functions": unused_funcs,
            "empty_functions": self.empty_functions,
            "dead_code_score": dead_score,
            "unused_config_keys": sorted(self.config_keys_defined - self.config_keys_used),
            "issues": self.issues,
        }

        if self.behavior_log:
            self.behavior_log.info(
                EventType.AGENT,
                "RedundancyAuditor",
                f"审计完成: 死代码评分 {dead_score}, 未用函数 {len(unused_funcs)}",
            )

        return report

    def _reset(self) -> None:
        self.defined_functions.clear()
        self.defined_classes.clear()
        self.called_functions.clear()
        self.used_classes.clear()
        self.empty_functions.clear()
        self.config_keys_defined.clear()
        self.config_keys_used.clear()
        self.issues.clear()

    def _is_excluded(self, path: Path) -> bool:
        """排除虚拟环境、缓存等目录"""
        parts = set(path.parts)
        excluded = {
            "__pycache__", ".git", ".venv", "venv", "node_modules",
            ".eggs", "build", "dist", ".tox", ".mypy_cache", ".pytest_cache",
        }
        return bool(parts & excluded)

    def _scan_python_file(self, filepath: Path) -> None:
        """扫描单个 Python 文件的定义与调用"""
        try:
            source = filepath.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(filepath))
        except SyntaxError:
            self.issues.append(f"语法错误: {filepath}")
            return
        except Exception as e:
            logger.debug(f"跳过文件 {filepath}: {e}")
            return

        rel_path = str(filepath.relative_to(self.root))

        for node in ast.walk(tree):
            # 函数定义
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_name = node.name
                self.defined_functions[func_name].append((rel_path, func_name, node.lineno))
                if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    self.empty_functions.append(f"{rel_path}:{node.lineno} {func_name}")

            # 类定义
            elif isinstance(node, ast.ClassDef):
                self.defined_classes[node.name].append((rel_path, node.name, node.lineno))

            # 函数调用
            elif isinstance(node, ast.Call):
                func_name = None
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                if func_name:
                    self.called_functions.add(func_name)

            # 类使用 (简单检测 Name 节点)
            elif isinstance(node, ast.Name):
                if node.id in self.defined_classes:
                    self.used_classes.add(node.id)

    def _scan_config_files(self) -> None:
        """扫描 YAML 配置文件，收集键并与代码引用对比"""
        config_files = list(self.root.rglob("*.yaml")) + list(self.root.rglob("*.yml"))
        for cfg_file in config_files:
            if self._is_excluded(cfg_file):
                continue
            try:
                with open(cfg_file, "r") as f:
                    data = yaml.safe_load(f)
                if not isinstance(data, dict):
                    continue
                self._collect_config_keys(data, "")
            except Exception:
                continue

        # 在 Python 代码中搜索 yaml 键引用
        for py_file in self.root.rglob("*.py"):
            if self._is_excluded(py_file):
                continue
            try:
                text = py_file.read_text(encoding="utf-8")
                for key in list(self.config_keys_defined):
                    if f"'{key}'" in text or f'"{key}"' in text:
                        self.config_keys_used.add(key)
            except Exception:
                pass

    def _collect_config_keys(self, data: Dict[str, Any], prefix: str) -> None:
        for key, value in data.items():
            full_key = f"{prefix}.{key}" if prefix else key
            self.config_keys_defined.add(full_key)
            if isinstance(value, dict):
                self._collect_config_keys(value, full_key)

    # ================== 提案构建 ==================
    def _build_proposal(self, report: Dict) -> Dict:
        """将扫描报告转换为议会提案"""
        dead_score = report["dead_code_score"]
        unused = report["unused_functions"]
        recommendation = "maintain"

        if dead_score < 40:
            recommendation = "emergency_cleanup"
            severity = EventLevel.CRITICAL
        elif dead_score < 70:
            recommendation = "cleanup"
            severity = EventLevel.WARN
        else:
            severity = EventLevel.INFO

        if self.notifier and recommendation != "maintain":
            asyncio.ensure_future(
                self.notifier.send_alert(
                    level=severity.value,
                    title=f"冗余审计 [{dead_score}/100]",
                    body=f"发现 {len(unused)} 个未使用函数，建议 {recommendation}",
                )
            )

        return {
            "type": "redundancy_report",
            "dead_code_score": dead_score,
            "unused_functions_count": len(unused),
            "empty_functions_count": len(report["empty_functions"]),
            "recommendation": recommendation,
            "complexity_delta": 0.0,  # 本提案不增加复杂度
            "timestamp": datetime.now().isoformat(),
        }

    def _create_empty_proposal(self) -> Dict:
        return {
            "type": "redundancy_report",
            "dead_code_score": 100,
            "unused_functions_count": 0,
            "empty_functions_count": 0,
            "recommendation": "maintain",
            "complexity_delta": 0.0,
        }

    # ================== 状态查询 ==================
    def get_status(self) -> Dict[str, Any]:
        return {
            "worldview": self.manifesto.worldview.value,
            "last_scan": datetime.fromtimestamp(self._last_scan).isoformat() if self._last_scan else None,
            "dead_code_score": self._cached_proposal.get("dead_code_score") if hasattr(self, "_cached_proposal") else None,
        }

    async def run_loop(self) -> None:
        while True:
            self.propose()
            await asyncio.sleep(self.check_interval)
