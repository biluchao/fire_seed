#!/usr/bin/env python3
"""
火种系统 (FireSeed) 策略锦标赛管理器
=========================================
负责：
- 组织定期策略对抗赛（循环赛、淘汰赛）
- 调用 ELO 系统记录比赛结果
- 调度比赛执行，支持异步并行
- 记录锦标赛历史与统计
"""

import asyncio
import logging
import random
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set, Tuple

from core.arena.elo_system import ELOSystem

logger = logging.getLogger("fire_seed.tournament")


class Tournament:
    """
    策略锦标赛管理器。

    提供两种赛事模式：
    1. 循环赛 (round_robin)：所有参赛策略两两对抗
    2. 淘汰赛 (knockout)：单败淘汰，随机种子

    比赛结果（得分）由外部匹配函数提供，该函数应对两个策略回测或模拟对抗，
    返回归一化得分 (score_a, score_b)，胜方得分应接近 1，败方接近 0，平局各 0.5。
    """

    def __init__(self,
                 elo_system: ELOSystem,
                 match_func: Optional[Callable[[str, str], Tuple[float, float]]] = None):
        """
        :param elo_system: 已初始化的 ELO 评分系统实例
        :param match_func: 异步或同步函数，签名为 (strategy_a_id, strategy_b_id) -> (score_a, score_b)
        """
        self.elo = elo_system
        self.match_func = match_func

        # 锦标赛历史记录（内存）
        self._history: List[Dict] = []

    async def _run_match(self, strategy_a: str, strategy_b: str) -> Tuple[float, float]:
        """
        执行一场比赛，返回 (score_a, score_b)。
        比赛函数必须返回在 [0, 1] 区间的得分，总和可以为任意值。
        """
        if self.match_func is None:
            raise RuntimeError("匹配函数未设置，无法执行比赛")

        try:
            # 支持同步或异步匹配函数
            if asyncio.iscoroutinefunction(self.match_func):
                score_a, score_b = await self.match_func(strategy_a, strategy_b)
            else:
                score_a, score_b = self.match_func(strategy_a, strategy_b)
        except Exception as e:
            logger.error(f"比赛 {strategy_a} vs {strategy_b} 异常: {e}")
            # 异常时双方得 0（视为均未得分）
            score_a, score_b = 0.0, 0.0

        # 确保得分在 [0, 1]
        score_a = max(0.0, min(1.0, score_a))
        score_b = max(0.0, min(1.0, score_b))
        return score_a, score_b

    async def run_round_robin(self,
                              strategies: List[str],
                              max_parallel: int = 4,
                              shuffle: bool = True) -> Dict:
        """
        循环赛：所有参赛策略两两对抗一次。
        :param strategies: 参赛策略 ID 列表
        :param max_parallel: 最大并行比赛数（避免资源耗尽）
        :param shuffle: 是否随机打乱比赛顺序
        :return: 赛事摘要
        """
        if len(strategies) < 2:
            return {"status": "skipped", "reason": "至少需要两个策略参赛"}

        pairs = []
        for i in range(len(strategies)):
            for j in range(i + 1, len(strategies)):
                pairs.append((strategies[i], strategies[j]))

        if shuffle:
            random.shuffle(pairs)

        total_matches = len(pairs)
        logger.info(f"循环赛开始，参赛策略: {len(strategies)}，总比赛: {total_matches}")

        semaphore = asyncio.Semaphore(max_parallel)
        start_time = datetime.now()

        async def match_wrapper(a, b):
            async with semaphore:
                score_a, score_b = await self._run_match(a, b)
                # 更新 ELO
                self.elo.update_rating(a, b, score_a, score_b)
                return (a, b, score_a, score_b)

        tasks = [match_wrapper(a, b) for a, b in pairs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        completed = 0
        for res in results:
            if isinstance(res, Exception):
                logger.error(f"比赛执行失败: {res}")
            else:
                completed += 1

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        summary = {
            "type": "round_robin",
            "players": len(strategies),
            "total_matches": total_matches,
            "completed": completed,
            "duration_sec": duration,
            "timestamp": end_time.isoformat(),
        }

        self._history.append(summary)
        logger.info(f"循环赛结束，完成 {completed}/{total_matches} 场，耗时 {duration:.1f}s")
        return summary

    async def run_knockout(self,
                           strategies: List[str],
                           seed: Optional[int] = None,
                           max_parallel: int = 4) -> Dict:
        """
        淘汰赛：随机抽签，单败淘汰，直至决出冠军。
        :param strategies: 参赛策略 ID 列表（数量最好为2的幂，可自动轮空）
        :param seed: 随机种子，用于重现
        :param max_parallel: 同一轮内的最大并行数
        :return: 赛事摘要，包含冠军及所有轮次结果
        """
        if len(strategies) < 2:
            return {"status": "skipped", "reason": "至少需要两个策略参赛"}

        if seed is not None:
            random.seed(seed)

        players = list(strategies)
        random.shuffle(players)

        # 补齐到2的幂（轮空）
        while len(players) & (len(players) - 1) != 0:
            players.append(None)  # None 表示轮空

        rounds = []
        current_round = 1
        start_time = datetime.now()

        while len(players) > 1:
            next_round = []
            # 本轮配对
            pairs = [(players[i], players[i + 1]) for i in range(0, len(players), 2)]
            logger.info(f"淘汰赛第 {current_round} 轮，{len(pairs)} 场比赛")

            semaphore = asyncio.Semaphore(max_parallel)
            round_results = []

            async def match_pair(a, b):
                async with semaphore:
                    if a is None:
                        return b  # 轮空
                    if b is None:
                        return a
                    score_a, score_b = await self._run_match(a, b)
                    # 归一化得分：得分高者胜出
                    winner = a if score_a >= score_b else b
                    self.elo.update_rating(a, b, score_a, score_b)
                    return winner

            tasks = [match_pair(p[0], p[1]) for p in pairs]
            winners = await asyncio.gather(*tasks, return_exceptions=True)

            for i, winner in enumerate(winners):
                if isinstance(winner, Exception):
                    logger.error(f"淘汰赛异常: {winner}")
                    # 异常时默认第一人晋级
                    winner = pairs[i][0] or pairs[i][1]
                next_round.append(winner)
                round_results.append({
                    "pair": pairs[i],
                    "winner": winner,
                })

            rounds.append({
                "round": current_round,
                "matches": round_results,
            })

            players = next_round
            current_round += 1

        champion = players[0] if players else None
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        summary = {
            "type": "knockout",
            "players": len(strategies),
            "rounds": len(rounds),
            "champion": champion,
            "duration_sec": duration,
            "timestamp": end_time.isoformat(),
            "details": rounds,
        }

        self._history.append(summary)
        if champion:
            logger.info(f"淘汰赛结束，冠军: {champion}，耗时 {duration:.1f}s")
        return summary

    def get_history(self, limit: int = 20) -> List[Dict]:
        """获取最近的锦标赛历史"""
        return self._history[-limit:]

    def get_statistics(self, strategy_id: str) -> Dict:
        """获取指定策略在锦标赛中的统计（基于历史）"""
        total = 0
        wins = 0
        for t in self._history:
            for match in t.get("details", []):
                # 简单处理：这里略，需要详细解析历史结构
                pass
        return {"total": 0, "wins": 0}
