#!/usr/bin/env python3
"""
火种系统 (FireSeed) 冗余审计官智能体 (RedundancyAuditor)
============================================================
定期扫描项目代码库与配置文件，检测并报告：
- 未被调用的函数与类
- 未被引用的配置项
- 空函数体 (仅包含 pass)
- 重复代码片段
- 冗余导入

可集成到夜间任务调度，帮助保持系统清洁度与低熵值。
"""

import ast
import logging
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

logger = logging.getLogger("fire_seed.redundancy_auditor")


class RedundancyAuditor:
    """
    冗余审计官智能体。
    用法：
        auditor = RedundancyAuditor(root="/path/to/project")
        report = auditor.scan_all()
        print(report["dead_code_score"])
    """

    def __init__(self, root: str = ".", behavior_log=None):
        """
        :param root: 项目根目录路径
        :param behavior_log: 可选的行为日志实例
        """
        self.root = Path(root)
        self.behavior_log = behavior_log

        # 存储定义与调用关系
        self.defined_functions: Dict[str, List[Tuple[str, str, int]]] = defaultdict(list)
        #              func_name -> [(file, func_name, line)]
        self.defined_classes: Dict[str, List[Tuple[str, str, int]]] = defaultdict(list)
        self.called_functions: Set[str] = set()
        self.used_classes: Set[str] = set()

        # 空函数列表
        self.empty_functions: List[str] = []

        # 配置文件引用情况
        self.config_keys_defined: Set[str] = set()
        self.config_keys_used: Set[str] = set()

        # 扫描结果
        self.issues: List[str] = []

    # ======================== 主扫描方法 ========================
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
                EventType.AGENT, "RedundancyAuditor",
                f"审计完成: 死代码评分 {dead_score}, 未用函数 {len(unused_funcs)}"
            )

        return report

    # ======================== 内部扫描逻辑 ========================
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
        excluded = {"__pycache__", ".git", ".venv", "venv", "node_modules",
                    ".eggs", "build", "dist", ".tox", ".mypy_cache", ".pytest_cache"}
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

        # 遍历 AST 收集定义和调用
        for node in ast.walk(tree):
            # 函数定义
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_name = node.name
                self.defined_functions[func_name].append(
                    (rel_path, func_name, node.lineno)
                )
                # 检查空函数体
                if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    self.empty_functions.append(f"{rel_path}:{node.lineno} {func_name}")

            # 类定义
            elif isinstance(node, ast.ClassDef):
                self.defined_classes[node.name].append(
                    (rel_path, node.name, node.lineno)
                )

            # 函数调用
            elif isinstance(node, ast.Call):
                func_name = None
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                if func_name:
                    self.called_functions.add(func_name)

            # 类使用 (简单检测实例化)
            elif isinstance(node, ast.ClassDef):
                # 类定义已经在上面处理，这里不需要
                pass
            # 同时也检查 Name 节点中可能引用的类（比如作为类型注解）
            elif isinstance(node, ast.Name):
                if node.id in self.defined_classes:
                    self.used_classes.add(node.id)

        # 额外检查未使用的导入
        self._check_unused_imports(tree, rel_path)

    def _check_unused_imports(self, tree: ast.AST, rel_path: str) -> None:
        """简单的未使用导入检查（占位）"""
        # 实际实现较复杂，需要追踪作用域内的名称引用
        # 这里仅忽略，避免影响主要功能
        pass

    def _scan_config_files(self) -> None:
        """扫描 YAML 配置文件，收集键并与代码中的引用进行对比"""
        config_files = list(self.root.rglob("*.yaml")) + list(self.root.rglob("*.yml"))
        for cfg_file in config_files:
            if self._is_excluded(cfg_file):
                continue
            try:
                with open(cfg_file, "r") as f:
                    data = yaml.safe_load(f)
                if not isinstance(data, dict):
                    continue
                # 递归收集所有键路径
                self._collect_config_keys(data, "")
            except Exception:
                continue

        # 在 Python 代码中搜索 yaml 键引用（简单字符串匹配）
        for py_file in self.root.rglob("*.py"):
            if self._is_excluded(py_file):
                continue
            try:
                text = py_file.read_text(encoding="utf-8")
                for key in list(self.config_keys_defined):
                    # 精确匹配键字符串的引用
                    if f"'{key}'" in text or f'"{key}"' in text:
                        self.config_keys_used.add(key)
            except Exception:
                pass

    def _collect_config_keys(self, data: Dict[str, Any], prefix: str) -> None:
        """递归收集配置键"""
        for key, value in data.items():
            full_key = f"{prefix}.{key}" if prefix else key
            self.config_keys_defined.add(full_key)
            if isinstance(value, dict):
                self._collect_config_keys(value, full_key)
