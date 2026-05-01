#!/usr/bin/env python3
"""
火种系统 (FireSeed) 灰度流量镜像模块
=========================================
在金丝雀发布或OTA更新期间，同时运行新旧版本策略引擎，
比较两者的交易信号，计算分歧程度，并在分歧超过阈值时
自动触发回滚或告警。

工作模式:
1. 镜像模式：主引擎执行旧版策略实盘下单，新版本仅记录信号
2. 对比模式：两个版本均运行在幽灵影子中，比较虚拟成交序列
"""

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("fire_seed.mirror_traffic")


@dataclass
class SignalSnapshot:
    """单个策略版本在某一时刻的信号快照"""
    version: str
    timestamp: datetime
    direction: int          # 1=多, -1=空, 0=中性
    confidence: float       # 0~1
    score: float            # 0~100
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DivergenceReport:
    """两版本信号分歧的统计报告"""
    total_samples: int = 0
    direction_mismatch: int = 0        # 方向相反的样本数
    mismatch_rate: float = 0.0         # 方向分歧率
    avg_score_diff: float = 0.0        # 平均评分差
    max_score_diff: float = 0.0
    correlation: float = 0.0           # 评分序列的相关系数
    recommendation: str = "continue"   # continue / rollback / investigate


class MirrorTraffic:
    """
    灰度流量镜像器。
    维护新旧两个版本的信号队列，定期生成分歧报告。
    """

    def __init__(self,
                 divergence_threshold: float = 0.40,
                 window_size: int = 100,
                 min_samples_for_report: int = 20):
        """
        :param divergence_threshold: 方向分歧率阈值，超过则建议回滚
        :param window_size: 滑动窗口大小（保存最近 N 个信号）
        :param min_samples_for_report: 最少积累多少样本后才可生成报告
        """
        self.divergence_threshold = divergence_threshold
        self.window_size = window_size
        self.min_samples = min_samples_for_report

        # 新版本的信号历史
        self._new_signals: List[SignalSnapshot] = []
        # 旧版本的信号历史
        self._old_signals: List[SignalSnapshot] = []

        # 最近一次分歧报告
        self._last_report: Optional[DivergenceReport] = None

    def feed_signal(self, version: str, direction: int,
                    confidence: float, score: float,
                    metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        输入一个版本的信号快照。
        通常在引擎产生信号后立即调用。
        """
        snap = SignalSnapshot(
            version=version,
            timestamp=datetime.now(),
            direction=direction,
            confidence=confidence,
            score=score,
            metadata=metadata or {}
        )

        if version == "new":
            self._new_signals.append(snap)
            while len(self._new_signals) > self.window_size:
                self._new_signals.pop(0)
        elif version == "old":
            self._old_signals.append(snap)
            while len(self._old_signals) > self.window_size:
                self._old_signals.pop(0)
        else:
            logger.warning(f"未知信号版本: {version}，已忽略")

    def evaluate(self) -> DivergenceReport:
        """
        计算当前窗口内两版本信号的分歧程度。
        """
        # 对齐时间戳，取两版本中共同出现的分钟（近似）
        # 简单对齐：取较短的序列长度，按尾部对齐
        n = min(len(self._new_signals), len(self._old_signals))
        if n < self.min_samples:
            return DivergenceReport(
                total_samples=n,
                recommendation="insufficient_data"
            )

        new_tail = self._new_signals[-n:]
        old_tail = self._old_signals[-n:]

        report = DivergenceReport(total_samples=n)

        # 方向分歧
        mismatch = 0
        score_diffs = []
        for ns, os in zip(new_tail, old_tail):
            if ns.direction != os.direction:
                mismatch += 1
            score_diffs.append(abs(ns.score - os.score))

        report.direction_mismatch = mismatch
        report.mismatch_rate = mismatch / n
        report.avg_score_diff = float(np.mean(score_diffs)) if score_diffs else 0.0
        report.max_score_diff = float(np.max(score_diffs)) if score_diffs else 0.0

        # 评分序列相关性
        new_scores = [s.score for s in new_tail]
        old_scores = [s.score for s in old_tail]
        if len(new_scores) >= 3:
            corr = np.corrcoef(new_scores, old_scores)[0, 1]
            if not np.isnan(corr):
                report.correlation = float(corr)

        # 建议
        if report.mismatch_rate > self.divergence_threshold:
            report.recommendation = "rollback"
        elif report.mismatch_rate > self.divergence_threshold * 0.7:
            report.recommendation = "investigate"
        else:
            report.recommendation = "continue"

        self._last_report = report
        if report.recommendation in ("rollback", "investigate"):
            logger.warning(
                f"镜像分歧告警: mismatch={mismatch}/{n} ({report.mismatch_rate*100:.1f}%), "
                f"建议={report.recommendation}"
            )

        return report

    def get_report(self) -> Optional[DivergenceReport]:
        """获取最近一次分歧报告"""
        return self._last_report

    def reset(self) -> None:
        """清空所有信号缓存"""
        self._new_signals.clear()
        self._old_signals.clear()
        self._last_report = None

    @property
    def sample_count(self) -> Tuple[int, int]:
        """返回 (新版本信号数, 旧版本信号数)"""
        return len(self._new_signals), len(self._old_signals)
