#!/usr/bin/env python3
"""
火种系统 (FireSeed) 监察者智能体 (Sentinel)
=============================================
世界观：机械唯物主义
核心信仰：系统是可由独立组件分解的钟表，任何故障均可定位。
优化目标：-log(F1_score) 宁可误报不可漏报。
专属数据源：系统资源、进程状态、API 限频、数据库响应。
禁止数据源：K 线、订单簿、价格数据。
时间尺度：15 秒。

在议会中，监察者根据系统整体健康度提出风险预警建议，
并从资源可行性角度挑战其他智能体的激进提案。
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import psutil

from agents.worldview import WorldView, WorldViewManifesto, WorldViewAgent
from agents.extreme_rewards import ExtremeRewardFunctions
from core.behavioral_logger import BehavioralLogger, EventType, EventLevel
from core.notifier import SystemNotifier
from core.self_check import SystemSelfCheck

logger = logging.getLogger("fire_seed.sentinel")


class SentinelAgent(WorldViewAgent):
    """
    监察者智能体。
    继承 WorldViewAgent，以机械唯物主义视角监控系统组件状态。
    """

    def __init__(self,
                 behavior_log: Optional[BehavioralLogger] = None,
                 notifier: Optional[SystemNotifier] = None):
        # 构建世界观宣言
        manifesto = WorldViewManifesto(
            worldview=WorldView.MECHANICAL_MATERIALISM,
            core_belief="系统是可分解为独立组件的钟表，故障可定位",
            primary_optimization_target=ExtremeRewardFunctions.REWARD_MAP[
                WorldView.MECHANICAL_MATERIALISM
            ]["formula"],
            adversary_worldview=WorldView.HOLISM,  # 与整体论对立
            forbidden_data_source={"KLINE", "ORDERBOOK", "PRICE"},
            exclusive_data_source={"SYSTEM_METRICS", "PROCESS_STATE", "API_LIMIT", "DB_RESPONSE"},
            time_scale="15s"
        )
        super().__init__(manifesto)

        self.behavior_log = behavior_log
        self.notifier = notifier
        self._self_check = SystemSelfCheck()
        self._last_health_report = None

    # ======================== 议会接口 ========================
    def propose(self, perception: Dict) -> Dict:
        """
        基于当前系统组件状态生成提案。
        返回：{"direction": 1/-1/0, "confidence": 0-1, "reason": str}
        """
        # 运行系统自检
        health_report = self._self_check.run()
        self._last_health_report = health_report

        # 计算健康评分
        score = health_report.score

        # 根据健康度确定方向与置信度
        if score >= 80:
            direction = 1   # 系统健康，倾向正常交易
            confidence = 0.8
            reason = f"系统健康评分 {score}，各组件运行正常"
        elif score >= 60:
            direction = 0   # 中性，建议谨慎
            confidence = 0.6
            reason = f"系统健康评分 {score}，部分组件有轻微异常"
        elif score >= 40:
            direction = -1  # 建议减仓/防御
            confidence = 0.75
            reason = f"系统健康评分 {score}，多个组件告警，建议降低风险暴露"
        else:
            direction = -1
            confidence = 0.9
            reason = f"系统健康评分 {score}，严重故障风险，强烈建议暂停交易"

        return {
            "direction": direction,
            "confidence": confidence,
            "reason": reason,
            "source": "Sentinel",
            "health_score": score,
            "timestamp": datetime.now().isoformat()
        }

    def challenge(self, other_proposal: Dict, my_worldview: WorldView) -> Dict:
        """
        从机械唯物主义视角挑战其他智能体的提案。
        主要检查提案是否与当前系统资源状态相容。
        返回：{"veto": bool, "reason": str, "confidence_adjustment": float}
        """
        challenges = []
        veto = False

        # 获取当前系统资源状态
        cpu_pct = psutil.cpu_percent(interval=0.1)
        mem_pct = psutil.virtual_memory().percent
        disk_pct = psutil.disk_usage('/').percent

        proposed_direction = other_proposal.get("direction", 0)
        proposed_confidence = other_proposal.get("confidence", 0.5)

        # 如果提案是激进做多或做空，但系统资源紧张，则挑战
        if proposed_direction != 0 and proposed_confidence > 0.7:
            if cpu_pct > 85:
                challenges.append(f"CPU 使用率 {cpu_pct:.1f}%，可能影响交易延迟")
                veto = True
            if mem_pct > 90:
                challenges.append(f"内存使用率 {mem_pct:.1f}%，SWAP 风险升高")
                veto = True
            if disk_pct > 92:
                challenges.append(f"磁盘使用率 {disk_pct:.1f}%，日志写入可能阻塞")
                veto = True

        # 检查数据库连接（若可用）
        try:
            import sqlite3
            db_path = "data/fire_seed.db"
            if not __import__('os').path.exists(db_path):
                challenges.append("数据库文件缺失，订单记录可能不完整")
                veto = True
            else:
                conn = sqlite3.connect(db_path)
                conn.execute("SELECT 1")
                conn.close()
        except Exception as e:
            challenges.append(f"数据库连接异常: {str(e)[:50]}")
            veto = True

        # 如果提案来自非机械唯物主义者，监察者会从系统稳定性角度质疑
        if other_proposal.get("source") not in ("Sentinel",):
            if not self._is_system_stable():
                challenges.append("近期系统资源波动较大，不适合执行高风险提案")
                veto = True

        reason = "; ".join(challenges) if challenges else "系统资源充裕，提案可行"

        return {
            "veto": veto,
            "reason": reason,
            "challenges": challenges,
            "confidence_adjustment": -0.2 if veto else 0.0
        }

    # ======================== 内部判断方法 ========================
    def _is_system_stable(self) -> bool:
        """检查系统近期是否稳定（无大幅波动）"""
        try:
            # 获取1分钟平均负载
            load1 = psutil.getloadavg()[0]
            cpu_count = psutil.cpu_count()
            if cpu_count and load1 / cpu_count > 2.0:
                return False
            # 内存是否持续增长
            return True
        except Exception:
            return True

    # ======================== 供外部调用的健康评估 ========================
    async def evaluate(self) -> Dict[str, Any]:
        """
        执行一次传统的健康评估（兼容旧接口）。
        返回结构化报告。
        """
        health_report = self._self_check.run()
        return {
            "health_score": health_report.score,
            "status": health_report.status,
            "checks": [{"name": c.name, "status": c.status, "message": c.message}
                       for c in health_report.checks],
            "timestamp": datetime.now().isoformat()
        }

    # ======================== 生命周期 ========================
    def terminate(self) -> None:
        """优雅清理资源"""
        pass
