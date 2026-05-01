#!/usr/bin/env python3
"""
火种系统 (FireSeed) 策略竞技场 ELO 评分系统
===============================================
提供：
- 基于对抗性回测的策略 ELO 排名
- 持久化存储（SQLite）
- K因子动态调整（根据比赛场次）
- 排行榜查询
- 比赛历史记录
"""

import sqlite3
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple


class ELOSystem:
    """
    策略竞技场 ELO 评分系统。

    新策略初始评分 1500，K因子随比赛场次逐渐降低。
    通过与其它策略在相同历史数据上竞技，动态调整分数。
    """

    def __init__(self, db_path: str = "data/elo_rankings.db"):
        self.db_path = db_path
        self.lock = threading.Lock()

        # 初始化数据库
        self._init_db()

        # 基础 K 因子
        self.base_k = 32
        # K 因子衰减最低值
        self.min_k = 16
        # K 因子随比赛场次衰减的阈值（超过此值后K开始降低）
        self.k_decay_threshold = 30
        # K 因子衰减速率
        self.k_decay_rate = 0.5

    # ======================== 数据库初始化 ========================
    def _init_db(self) -> None:
        """创建评分表与比赛记录表"""
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                # 评分表
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS rankings (
                        strategy_id   TEXT PRIMARY KEY,
                        name          TEXT DEFAULT '',
                        elo           REAL DEFAULT 1500.0,
                        matches       INTEGER DEFAULT 0,
                        wins          INTEGER DEFAULT 0,
                        draws         INTEGER DEFAULT 0,
                        losses        INTEGER DEFAULT 0,
                        streak        INTEGER DEFAULT 0,
                        best_elo      REAL DEFAULT 1500.0,
                        last_updated  TEXT DEFAULT (datetime('now')),
                        created_at    TEXT DEFAULT (datetime('now'))
                    )
                """)

                # 比赛记录表
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS match_history (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        strategy_a    TEXT NOT NULL,
                        strategy_b    TEXT NOT NULL,
                        score_a       REAL NOT NULL,
                        score_b       REAL NOT NULL,
                        elo_change_a  REAL NOT NULL,
                        elo_change_b  REAL NOT NULL,
                        timestamp     TEXT DEFAULT (datetime('now'))
                    )
                """)

                # 索引
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_rankings_elo
                    ON rankings(elo DESC)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_match_timestamp
                    ON match_history(timestamp DESC)
                """)

    # ======================== 核心评分逻辑 ========================
    def _calc_k_factor(self, matches: int) -> float:
        """
        计算动态 K 因子。
        比赛场次越少 K 越大（允许快速收敛），
        比赛场次越多 K 越小（稳定排名）。
        """
        if matches <= self.k_decay_threshold:
            return self.base_k
        extra = matches - self.k_decay_threshold
        return max(self.min_k, self.base_k - extra * self.k_decay_rate)

    def _expected_score(self, rating_a: float, rating_b: float) -> Tuple[float, float]:
        """
        计算预期胜率（基于 ELO 公式）。
        E_A = 1 / (1 + 10^((R_B - R_A) / 400))
        """
        ea = 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))
        eb = 1.0 - ea
        return ea, eb

    def update_rating(self,
                      strategy_a: str,
                      strategy_b: str,
                      score_a: float,
                      score_b: float,
                      name_a: str = "",
                      name_b: str = "") -> Dict[str, float]:
        """
        根据双方得分更新 ELO 评分。
        输入得分需归一化到 [0, 1]，平局各 0.5，胜方为 1。

        :return: {"strategy_a": new_elo, "strategy_b": new_elo}
        """
        with self.lock:
            ra, matches_a = self._ensure_strategy(strategy_a, name_a)
            rb, matches_b = self._ensure_strategy(strategy_b, name_b)

            ea, eb = self._expected_score(ra, rb)
            ka = self._calc_k_factor(matches_a)
            kb = self._calc_k_factor(matches_b)

            delta_a = ka * (score_a - ea)
            delta_b = kb * (score_b - eb)

            new_ra = ra + delta_a
            new_rb = rb + delta_b

            # 更新数据库
            with sqlite3.connect(self.db_path) as conn:
                self._update_rankings_row(conn, strategy_a, new_ra, score_a, delta_a)
                self._update_rankings_row(conn, strategy_b, new_rb, score_b, delta_b)

                # 记录比赛历史
                conn.execute("""
                    INSERT INTO match_history
                    (strategy_a, strategy_b, score_a, score_b, elo_change_a, elo_change_b)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (strategy_a, strategy_b, score_a, score_b, delta_a, delta_b))

            return {strategy_a: new_ra, strategy_b: new_rb}

    def _ensure_strategy(self, strategy_id: str, name: str = "") -> Tuple[float, int]:
        """
        确保策略在评分表中存在，不存在则创建。
        返回 (当前elo, 当前比赛场次)
        """
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT elo, matches FROM rankings WHERE strategy_id = ?",
                (strategy_id,)
            )
            row = cur.fetchone()
            if row:
                return row[0], row[1]
            # 新策略注册
            conn.execute(
                "INSERT INTO rankings (strategy_id, name) VALUES (?, ?)",
                (strategy_id, name)
            )
            return 1500.0, 0

    def _update_rankings_row(self, conn, strategy_id: str,
                             new_elo: float, score: float,
                             elo_change: float) -> None:
        """更新单条评分记录"""
        # 判断胜负
        win = 1 if score > 0.55 else 0
        draw = 1 if 0.45 <= score <= 0.55 else 0
        loss = 1 if score < 0.45 else 0

        # 计算连胜/连败
        cur = conn.execute(
            "SELECT streak FROM rankings WHERE strategy_id = ?",
            (strategy_id,)
        )
        old_streak = cur.fetchone()[0]
        if win:
            new_streak = old_streak + 1 if old_streak > 0 else 1
        elif loss:
            new_streak = old_streak - 1 if old_streak < 0 else -1
        else:
            new_streak = 0  # 平局重置

        conn.execute("""
            UPDATE rankings
            SET elo = ?,
                best_elo = MAX(best_elo, ?),
                matches = matches + 1,
                wins = wins + ?,
                draws = draws + ?,
                losses = losses + ?,
                streak = ?,
                last_updated = datetime('now')
            WHERE strategy_id = ?
        """, (new_elo, new_elo, win, draw, loss, new_streak, strategy_id))

    # ======================== 查询接口 ========================
    def get_rating(self, strategy_id: str) -> Optional[Dict]:
        """查询单个策略的评分详情"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT * FROM rankings WHERE strategy_id = ?",
                (strategy_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "strategy_id": row[0],
                "name": row[1],
                "elo": row[2],
                "matches": row[3],
                "wins": row[4],
                "draws": row[5],
                "losses": row[6],
                "streak": row[7],
                "best_elo": row[8],
                "last_updated": row[9],
                "created_at": row[10],
            }

    def get_leaderboard(self, limit: int = 50) -> List[Dict]:
        """获取 ELO 排行榜（前 N 名）"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """SELECT strategy_id, name, elo, matches, wins, draws,
                          losses, streak, best_elo, last_updated
                   FROM rankings
                   ORDER BY elo DESC
                   LIMIT ?""",
                (limit,)
            )
            results = []
            for row in cur.fetchall():
                results.append({
                    "strategy_id": row[0],
                    "name": row[1],
                    "elo": round(row[2], 1),
                    "matches": row[3],
                    "wins": row[4],
                    "draws": row[5],
                    "losses": row[6],
                    "streak": row[7],
                    "best_elo": round(row[8], 1),
                    "last_updated": row[9],
                })
            return results

    def get_rank(self, strategy_id: str) -> int:
        """查询指定策略的当前排名（1-based）"""
        rating = self.get_rating(strategy_id)
        if not rating:
            return -1
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM rankings WHERE elo > ?",
                (rating["elo"],)
            )
            return cur.fetchone()[0] + 1

    def get_recent_matches(self, limit: int = 20,
                           strategy_id: Optional[str] = None) -> List[Dict]:
        """查询最近的比赛记录"""
        with sqlite3.connect(self.db_path) as conn:
            if strategy_id:
                cur = conn.execute(
                    """SELECT * FROM match_history
                       WHERE strategy_a = ? OR strategy_b = ?
                       ORDER BY timestamp DESC LIMIT ?""",
                    (strategy_id, strategy_id, limit)
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM match_history ORDER BY timestamp DESC LIMIT ?",
                    (limit,)
                )
            results = []
            for row in cur.fetchall():
                results.append({
                    "id": row[0],
                    "strategy_a": row[1],
                    "strategy_b": row[2],
                    "score_a": row[3],
                    "score_b": row[4],
                    "elo_change_a": round(row[5], 2),
                    "elo_change_b": round(row[6], 2),
                    "timestamp": row[7],
                })
            return results

    def get_rank_change(self, strategy_id: str, recent_n: int = 10) -> str:
        """
        判断排名近期变化趋势。
        返回: "up" / "down" / "stable"
        """
        matches = self.get_recent_matches(recent_n, strategy_id)
        if len(matches) < 2:
            return "stable"
        # 计算前一半和后一半的平均 ELO 变动
        mid = len(matches) // 2
        early_changes = []
        late_changes = []
        for i, m in enumerate(matches):
            if m["strategy_a"] == strategy_id:
                change = m["elo_change_a"]
            else:
                change = m["elo_change_b"]
            if i < mid:
                early_changes.append(change)
            else:
                late_changes.append(change)

        avg_early = sum(early_changes) / len(early_changes) if early_changes else 0
        avg_late = sum(late_changes) / len(late_changes) if late_changes else 0

        if avg_late > avg_early + 2:
            return "up"
        elif avg_late < avg_early - 2:
            return "down"
        return "stable"

    def predict_outcome(self, strategy_a: str, strategy_b: str) -> Dict[str, float]:
        """
        预测两策略的对战胜率。
        """
        ra_info = self.get_rating(strategy_a)
        rb_info = self.get_rating(strategy_b)
        ra = ra_info["elo"] if ra_info else 1500.0
        rb = rb_info["elo"] if rb_info else 1500.0
        ea, eb = self._expected_score(ra, rb)
        return {
            strategy_a: round(ea, 4),
            strategy_b: round(eb, 4)
        }

    # ======================== 维护操作 ========================
    def reset_strategy(self, strategy_id: str) -> bool:
        """重置指定策略的评分（回到1500，清空战绩）"""
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """UPDATE rankings
                       SET elo = 1500, matches = 0, wins = 0, draws = 0,
                           losses = 0, streak = 0, best_elo = 1500,
                           last_updated = datetime('now')
                       WHERE strategy_id = ?""",
                    (strategy_id,)
                )
                return conn.total_changes > 0

    def remove_strategy(self, strategy_id: str) -> bool:
        """从排行榜中彻底移除一个策略"""
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "DELETE FROM rankings WHERE strategy_id = ?",
                    (strategy_id,)
                )
                conn.execute(
                    "DELETE FROM match_history WHERE strategy_a = ? OR strategy_b = ?",
                    (strategy_id, strategy_id)
                )
                return conn.total_changes > 0

    def get_total_strategies(self) -> int:
        """获取注册策略总数"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT COUNT(*) FROM rankings")
            return cur.fetchone()[0]
