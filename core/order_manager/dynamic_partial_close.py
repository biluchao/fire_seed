"""
火种系统 · 动态部分止盈管理器 (DynamicPartialClose)

核心职责：
1. 基于持仓健康度六维评分与当前浮盈幅度，通过交叉决策矩阵动态计算最优部分止盈比例
2. 实时更新持仓健康度评分，管理止盈后的剩余仓位止损收紧、时间衰减止盈单挂载与利润锁定

外部依赖（真实模块接口）：
- core.order_manager.position_health_scorer.PositionHealthScorer : 获取持仓六维健康度评分
- core.order_manager.profit_compression.ProfitCompression : 获取当前紧缩利润阶段，用于调整止盈策略
- core.order_manager.stop_loss_trajectory.StopLossTrajectory : 更新止盈后剩余仓位的止损轨迹
- core.negotiation_bus.NegotiationBus : 推送止盈决策事件供叙事官和决策溯源使用
- core.behavioral_logger.BehavioralLogger : 记录止盈决策与健康度评分日志

接口契约：
- evaluate(position_id: str, context: Dict[str, Any]) -> Dict[str, Any] : 评估持仓并返回止盈比例与动作
- update_score(position_id: str, score: float, dimensions: Dict[str, float]) -> Dict[str, Any] : 更新持仓健康度评分
- get_status(position_id: str) -> Dict[str, Any] : 返回当前持仓的止盈状态与健康度评分
- cleanup_position(position_id: str) -> Dict[str, Any] : 清理指定持仓的所有缓存数据（平仓后调用）
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 PositionHealthScorer 不可用时，使用过期缓存评分；若无缓存，使用保守默认值(40分)
- 当 ProfitCompression 不可用时，默认认为持仓处于"maturity"阶段
- 当 StopLossTrajectory 不可用时，止盈后剩余仓位止损更新降级为仅记录日志，不阻塞主流程
- 当 NegotiationBus 不可用时，决策事件降级为仅本地日志记录
- 当外部依赖返回的评分为 None 时，使用过期缓存或保守默认值，并记录 WARNING
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护持仓健康度评分的本地缓存，在评分过期后自动清理
- 提供 cleanup_position 方法供外部在平仓时主动释放资源，防止内存泄漏
- 不持有任何外部资源句柄，线程锁在模块销毁时自动释放
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional
from collections import deque

logger = logging.getLogger(__name__)


class DynamicPartialClose:
    """动态部分止盈管理器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 交叉决策矩阵：行=健康度分档，列=浮盈ATR分档，值=部分止盈比例
    # 分档说明：健康度 [80+, 60-80, 40-60, <40]，浮盈ATR [<0.5, 0.5-1.5, 1.5-3.0, >3.0]
    DEFAULT_DECISION_MATRIX = [
        [0.0, 0.0, 0.20, 0.30],   # 健康度 80+：趋势强劲，仅极高浮盈时小幅止盈
        [0.0, 0.20, 0.40, 0.50],   # 健康度 60-80：趋势可能尾声，适度止盈
        [0.30, 0.50, 0.70, 0.80],  # 健康度 40-60：趋势衰减，大幅止盈
        [0.60, 0.80, 1.0, 1.0],    # 健康度 <40：趋势反转，全部或接近全部平仓
    ]
    # 健康度分档边界
    HEALTH_BRACKETS = [80, 60, 40, 0]       # 无量纲，取值范围 [0, 100]
    # 浮盈ATR分档边界（基础值，运行时根据波动率动态调整）
    BASE_PROFIT_BRACKETS = [0.5, 1.5, 3.0, 999]  # ATR倍数，无量纲

    # 止盈后剩余仓位止损收紧倍数
    REMAINING_STOP_ATR_MULT = 0.3            # ATR倍数，取值范围 [0.1, 0.5]
    # 时间衰减止盈单衰减周期
    DECAY_PERIOD_SECONDS = 60               # 秒，取值范围 [30, 120]
    # 止盈后剩余仓位最大持有时间
    MAX_HOLD_AFTER_PARTIAL_CLOSE_SEC = 180  # 秒，取值范围 [60, 600]
    # 评分缓存过期时间
    SCORE_CACHE_TTL_SEC = 5                  # 秒，取值范围 [1, 30]
    # 默认保守健康度评分（依赖不可用时使用）
    CONSERVATIVE_DEFAULT_SCORE = 40.0        # 无量纲，取值范围 [0, 100]
    # 同一持仓最小决策间隔
    DECISION_MIN_INTERVAL_SEC = 10           # 秒，取值范围 [5, 30]
    # 健康度EMA平滑系数
    SCORE_EMA_ALPHA = 0.3                   # 无量纲，[0.1, 0.5]
    # 波动率分位阈值
    HIGH_VOL_PERCENTILE = 70                # 高波动阈值，百分位，[60, 90]
    LOW_VOL_PERCENTILE = 30                 # 低波动阈值，百分位，[10, 40]
    # 高波动期浮盈档位扩张系数
    HIGH_VOL_BRACKET_EXPANSION = 1.5        # 无量纲，[1.2, 2.0]
    # 低波动期浮盈档位收缩系数
    LOW_VOL_BRACKET_CONTRACTION = 0.8       # 无量纲，[0.5, 0.9]
    # 滑点预警阈值
    SLIPPAGE_WARNING_BPS = 5.0              # 基点，[3.0, 20.0]

    def __init__(self):
        # 持仓健康度评分缓存
        self._scores: Dict[str, float] = {}
        self._score_dimensions: Dict[str, Dict[str, float]] = {}
        self._score_timestamps: Dict[str, float] = {}
        # EMA平滑后的健康度评分
        self._smoothed_scores: Dict[str, float] = {}

        # 近期止盈历史（用于趋势分析）
        self._close_history: Dict[str, deque] = {}
        # 上次决策时间（用于最小决策间隔）
        self._last_decision_time: Dict[str, float] = {}

        # 外部依赖注入
        self._health_scorer = None
        self._profit_compression = None
        self._stop_loss_trajectory = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 线程安全
        self._lock = threading.Lock()

        logger.info("DynamicPartialClose 初始化完成，决策矩阵已加载")

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        health_scorer: Optional[Any] = None,
        profit_compression: Optional[Any] = None,
        stop_loss_trajectory: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）

        Args:
            health_scorer: 持仓健康度评分器
            profit_compression: 紧缩利润阶段查询
            stop_loss_trajectory: 止损轨迹管理器
            negotiation_bus: 协商总线
            behavioral_logger: 行为日志记录器
        """
        if health_scorer is not None:
            if not hasattr(health_scorer, 'evaluate'):
                logger.warning("PositionHealthScorer 缺少 evaluate 方法，降级处理")
            else:
                self._health_scorer = health_scorer
                logger.info("PositionHealthScorer 注入成功")

        if profit_compression is not None:
            self._profit_compression = profit_compression
            logger.info("ProfitCompression 注入成功")

        if stop_loss_trajectory is not None:
            if not hasattr(stop_loss_trajectory, 'update_stop'):
                logger.warning("StopLossTrajectory 缺少 update_stop 方法，降级处理")
            else:
                self._stop_loss_trajectory = stop_loss_trajectory
                logger.info("StopLossTrajectory 注入成功")

        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, 'publish_event'):
                logger.warning("NegotiationBus 缺少 publish_event 方法，降级处理")
            else:
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")

    # ========== 公共接口 ==========
    def evaluate(self, position_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        评估持仓并返回动态止盈比例与动作建议

        注意：本方法返回止盈建议，实际止盈执行由调用方（持仓生命周期管理模块）完成。
        事件推送中标注 status: "proposed"，待执行完成后调用方应通过 update_score 反馈执行结果。

        Args:
            position_id: 持仓唯一标识
            context: 持仓上下文，必须包含:
                - profit_atr_ratio: float, 当前浮盈ATR倍数
                - direction: int, 持仓方向 (1=多头, -1=空头)
                - volatility_percentile: float, 当前波动率分位 (0-100)，可选

        Returns:
            标准响应字典，data 中包含 close_pct, remaining_action, stop_update 等字段
        """
        # 参数校验
        profit_atr_ratio = context.get("profit_atr_ratio", 0.0)
        if not isinstance(profit_atr_ratio, (int, float)):
            logger.warning(f"无效 profit_atr_ratio: {profit_atr_ratio}，使用默认值 0.0")
            profit_atr_ratio = 0.0

        direction = context.get("direction", 0)
        if direction not in (1, -1):
            logger.warning(f"无效 direction: {direction}，使用默认值 1")
            direction = 1

        vol_percentile = context.get("volatility_percentile", 50)
        if not isinstance(vol_percentile, (int, float)):
            vol_percentile = 50

        # 最小决策间隔检查（避免高频反复触发止盈）
        now = time.time()
        with self._lock:
            last_decision = self._last_decision_time.get(position_id, 0)
        if now - last_decision < self.DECISION_MIN_INTERVAL_SEC:
            logger.debug(f"持仓 {position_id} 距上次决策仅 {now - last_decision:.1f} 秒，跳过评估")
            return {
                "status": "ok",
                "reason": f"距上次决策仅 {now - last_decision:.1f} 秒，跳过评估（最小间隔 {self.DECISION_MIN_INTERVAL_SEC} 秒）",
                "data": {"close_pct": 0.0, "skipped": True},
                "warnings": [],
            }

        # 获取健康度评分并应用EMA平滑（已在内部处理锁与外部依赖调用）
        raw_health_score = self._get_score(position_id)
        health_score = self._get_smoothed_score(position_id, raw_health_score)
        logger.debug("position=%s raw_health=%.1f smoothed_health=%.1f profit_atr=%.2f",
                     position_id, raw_health_score, health_score, profit_atr_ratio)

        # 查询紧缩利润阶段（锁外调用，避免死锁）
        compression_stage = self._get_compression_stage(position_id)

        # 获取波动率适配的浮盈分档边界
        profit_brackets = self._get_volatility_adapted_brackets(vol_percentile)

        # 交叉决策矩阵查表
        close_pct = self._lookup_matrix(health_score, profit_atr_ratio, profit_brackets)
        reason = (f"健康度={health_score:.1f}(原始={raw_health_score:.1f}), "
                  f"浮盈ATR={profit_atr_ratio:.2f}, 波动率分位={vol_percentile:.0f}, "
                  f"方向={'多' if direction == 1 else '空'}")

        # 紧缩阶段修正
        if compression_stage in ("large_profit", "extreme"):
            close_pct = max(0.0, min(1.0, close_pct + 0.10))
            reason += f", 紧缩阶段={compression_stage}(止盈比例+10%)"
            logger.info("紧缩阶段修正: position=%s, close_pct=%.2f", position_id, close_pct)

        # 生成剩余仓位管理建议
        remaining_action = {}
        if close_pct < 1.0:
            remaining_action = {
                "stop_tighten_atr": self.REMAINING_STOP_ATR_MULT,
                "time_decay_tp_enabled": True,
                "decay_period_seconds": self.DECAY_PERIOD_SECONDS,
                "max_hold_after_partial_close_seconds": self.MAX_HOLD_AFTER_PARTIAL_CLOSE_SEC,
            }
            # 预期滑点估算（大额止盈时的冲击预估）
            if close_pct > 0:
                estimated_slippage = self._estimate_slippage(close_pct, context)
                remaining_action["estimated_slippage_bps"] = estimated_slippage
                if estimated_slippage > self.SLIPPAGE_WARNING_BPS:
                    remaining_action["suggest_split"] = True
                    remaining_action["split_batches"] = max(2, int(1.0 / close_pct))
                    logger.warning(
                        f"预期滑点 {estimated_slippage:.1f} bps，建议分批止盈，"
                        f"批数={remaining_action['split_batches']}"
                    )

        # 记录本次决策时间
        with self._lock:
            self._last_decision_time[position_id] = now
            if position_id not in self._close_history:
                self._close_history[position_id] = deque(maxlen=20)
            self._close_history[position_id].append({
                "timestamp": now,
                "close_pct": close_pct,
                "health_score": health_score,
                "raw_health_score": raw_health_score,
                "profit_atr_ratio": profit_atr_ratio,
                "direction": direction,
            })

        # 推送决策事件（标注 proposed 状态）
        self._publish_decision(position_id, close_pct, health_score, profit_atr_ratio, direction, reason)

        return {
            "status": "ok",
            "reason": reason,
            "data": {
                "close_pct": close_pct,
                "health_score": health_score,
                "raw_health_score": raw_health_score,
                "profit_atr_ratio": profit_atr_ratio,
                "compression_stage": compression_stage,
                "remaining_action": remaining_action,
            },
            "warnings": [],
        }

    def update_score(self, position_id: str, score: float, dimensions: Dict[str, float]) -> Dict[str, Any]:
        """
        更新持仓健康度评分

        Args:
            position_id: 持仓唯一标识
            score: 健康度总分 (0-100)
            dimensions: 各维度得分，如 {"profit_state": 85, "duration": 60, ...}

        Returns:
            标准响应字典
        """
        if score < 0 or score > 100:
            logger.warning(f"无效健康度评分: {score}，截断至 [0, 100]")
            score = max(0.0, min(100.0, score))

        if not dimensions:
            logger.warning(f"持仓 {position_id} 收到空的维度字典，评分={score}")

        with self._lock:
            self._scores[position_id] = score
            self._score_dimensions[position_id] = dimensions if dimensions else {}
            self._score_timestamps[position_id] = time.time()

        logger.debug("更新健康度评分: position=%s, score=%.1f, dims=%d", position_id, score, len(dimensions or {}))

        return {
            "status": "ok",
            "reason": f"已更新持仓 {position_id} 的健康度评分: {score:.1f}",
            "data": {"position_id": position_id, "score": score},
            "warnings": [],
        }

    def get_status(self, position_id: str) -> Dict[str, Any]:
        """
        返回当前持仓的止盈状态与健康度评分

        Args:
            position_id: 持仓唯一标识

        Returns:
            标准响应字典
        """
        with self._lock:
            score = self._scores.get(position_id)
            smoothed = self._smoothed_scores.get(position_id)
            dimensions = self._score_dimensions.get(position_id, {})
            last_update = self._score_timestamps.get(position_id, 0)
            history = list(self._close_history.get(position_id, []))

        if score is None:
            return {
                "status": "ok",
                "reason": f"持仓 {position_id} 暂无健康度评分",
                "data": {"position_id": position_id, "has_score": False},
                "warnings": [],
            }

        return {
            "status": "ok",
            "reason": f"持仓 {position_id} 健康度评分: {score:.1f}",
            "data": {
                "position_id": position_id,
                "score": score,
                "smoothed_score": smoothed,
                "dimensions": dimensions,
                "last_update": last_update,
                "close_history": history[-5:],
            },
            "warnings": [],
        }

    def cleanup_position(self, position_id: str) -> Dict[str, Any]:
        """
        清理指定持仓的所有缓存数据（平仓后调用）

        Args:
            position_id: 持仓唯一标识

        Returns:
            标准响应字典
        """
        with self._lock:
            removed = False
            if position_id in self._scores:
                del self._scores[position_id]
                removed = True
            self._score_dimensions.pop(position_id, None)
            self._score_timestamps.pop(position_id, None)
            self._smoothed_scores.pop(position_id, None)
            self._close_history.pop(position_id, None)
            self._last_decision_time.pop(position_id, None)

        if removed:
            logger.debug("清理持仓缓存: position=%s", position_id)

        return {
            "status": "ok",
            "reason": f"已清理持仓 {position_id} 的缓存数据",
            "data": {"position_id": position_id, "cleaned": removed},
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检

        Returns:
            标准健康检查响应字典
        """
        try:
            with self._lock:
                active_positions = len(self._scores)
                total_history = sum(len(h) for h in self._close_history.values())
                # 已平仓但未清理的僵尸缓存数
                stale_caches = sum(
                    1 for pid, ts in self._score_timestamps.items()
                    if time.time() - ts > 3600 and pid not in self._last_decision_time
                )

            matrix_rows = len(self.DEFAULT_DECISION_MATRIX)
            matrix_cols = len(self.DEFAULT_DECISION_MATRIX[0]) if matrix_rows > 0 else 0

            return {
                "status": "ok",
                "reason": (f"DynamicPartialClose 正常，活跃持仓 {active_positions}，"
                           f"历史记录 {total_history} 条，僵尸缓存 {stale_caches}"),
                "data": {
                    "active_positions": active_positions,
                    "total_history": total_history,
                    "stale_caches": stale_caches,
                    "matrix_dims": f"{matrix_rows}x{matrix_cols}",
                    "dependencies": {
                        "health_scorer": self._health_scorer is not None,
                        "profit_compression": self._profit_compression is not None,
                        "stop_loss_trajectory": self._stop_loss_trajectory is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                    },
                },
                "warnings": [f"僵尸缓存: {stale_caches}"] if stale_caches > 10 else [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和数据字典完整性")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _get_score(self, position_id: str) -> float:
        """
        获取持仓健康度评分（含缓存过期检查，已避免死锁）

        Args:
            position_id: 持仓唯一标识

        Returns:
            健康度评分 (0-100)
        """
        with self._lock:
            cached_score = self._scores.get(position_id)
            cached_time = self._score_timestamps.get(position_id, 0)

        if cached_score is not None and (time.time() - cached_time) < self.SCORE_CACHE_TTL_SEC:
            return cached_score

        external_score = None
        external_dimensions = {}
        if self._health_scorer is not None and hasattr(self._health_scorer, 'evaluate'):
            try:
                result = self._health_scorer.evaluate(position_id)
                if result.get("status") == "ok":
                    data = result.get("data", {})
                    external_score = data.get("score")
                    external_dimensions = data.get("dimensions", {})
                    if external_score is None:
                        logger.warning(f"外部健康度评分返回 None: position={position_id}")
            except Exception as e:
                logger.warning(f"外部健康度评分调用失败: {e}，使用缓存或默认值")

        with self._lock:
            if external_score is not None:
                self._scores[position_id] = external_score
                self._score_dimensions[position_id] = external_dimensions
                self._score_timestamps[position_id] = time.time()
                return external_score
            if cached_score is not None:
                logger.debug(f"外部调用失败，使用过期缓存: position={position_id}, score={cached_score}")
                return cached_score

        logger.warning(f"持仓 {position_id} 无法获取健康度评分，使用保守默认值 {self.CONSERVATIVE_DEFAULT_SCORE}")
        return self.CONSERVATIVE_DEFAULT_SCORE

    def _get_smoothed_score(self, position_id: str, raw_score: float) -> float:
        """
        对健康度评分应用EMA平滑，避免因瞬时噪声触发错误止盈

        Args:
            position_id: 持仓唯一标识
            raw_score: 原始健康度评分

        Returns:
            EMA平滑后的评分
        """
        with self._lock:
            prev_smoothed = self._smoothed_scores.get(position_id)
            if prev_smoothed is None:
                smoothed = raw_score
            else:
                smoothed = self.SCORE_EMA_ALPHA * raw_score + (1 - self.SCORE_EMA_ALPHA) * prev_smoothed
            self._smoothed_scores[position_id] = smoothed
        return smoothed

    def _get_volatility_adapted_brackets(self, vol_percentile: float) -> List[float]:
        """
        根据当前波动率分位动态调整浮盈分档边界

        高波动期（vol_percentile > 70）：扩大分档边界，让利润有更多奔跑空间
        低波动期（vol_percentile < 30）：收缩分档边界，更快锁定微利

        Args:
            vol_percentile: 当前波动率分位 (0-100)

        Returns:
            调整后的浮盈分档边界列表
        """
        if vol_percentile > self.HIGH_VOL_PERCENTILE:
            factor = self.HIGH_VOL_BRACKET_EXPANSION
            logger.debug(f"高波动环境(分位={vol_percentile:.0f})，浮盈分档扩张 {factor:.1f} 倍")
        elif vol_percentile < self.LOW_VOL_PERCENTILE:
            factor = self.LOW_VOL_BRACKET_CONTRACTION
            logger.debug(f"低波动环境(分位={vol_percentile:.0f})，浮盈分档收缩 {factor:.1f} 倍")
        else:
            factor = 1.0

        return [b * factor for b in self.BASE_PROFIT_BRACKETS]

    def _get_compression_stage(self, position_id: str) -> str:
        """
        获取当前紧缩利润阶段

        Args:
            position_id: 持仓唯一标识

        Returns:
            紧缩阶段字符串，如 "incubation", "acceleration", "maturity", "large_profit", "extreme"
        """
        if self._profit_compression is not None and hasattr(self._profit_compression, 'get_stage'):
            try:
                result = self._profit_compression.get_stage(position_id)
                if result.get("status") == "ok":
                    return result.get("data", {}).get("stage", "maturity")
            except Exception as e:
                logger.warning(f"紧缩阶段查询失败: {e}")
        return "maturity"

    def _lookup_matrix(self, health_score: float, profit_atr_ratio: float,
                       profit_brackets: List[float]) -> float:
        """
        通过交叉决策矩阵查表获取止盈比例

        Args:
            health_score: 健康度评分（已平滑）
            profit_atr_ratio: 浮盈ATR倍数
            profit_brackets: 当前波动率环境下的浮盈分档边界

        Returns:
            止盈比例 (0.0 - 1.0)
        """
        # 边界裁剪，确保输入在合理范围内
        health_score = max(0.0, min(100.0, health_score))
        profit_atr_ratio = max(0.0, profit_atr_ratio)

        # 确定健康度档位
        health_idx = 0
        for i, bracket in enumerate(self.HEALTH_BRACKETS):
            if health_score >= bracket:
                health_idx = i
                break

        # 确定浮盈档位
        profit_idx = 0
        for i, bracket in enumerate(profit_brackets):
            if profit_atr_ratio <= bracket:
                profit_idx = i
                break

        try:
            close_pct = self.DEFAULT_DECISION_MATRIX[health_idx][profit_idx]
        except IndexError:
            logger.error(
                f"决策矩阵越界: health_idx={health_idx}, profit_idx={profit_idx} "
                f"#RECOVERY: 检查 HEALTH_BRACKETS 和 profit_brackets 配置是否正确"
            )
            close_pct = 0.5

        return close_pct

    def _estimate_slippage(self, close_pct: float, context: Dict[str, Any]) -> float:
        """
        预估止盈执行时的滑点（基于持仓规模与当前盘口深度）

        Args:
            close_pct: 计划止盈比例
            context: 持仓上下文，可包含 current_position_size, avg_daily_volume

        Returns:
            预估滑点（基点）
        """
        position_size = context.get("current_position_size", 0.0)
        avg_volume = context.get("avg_daily_volume", 1.0)

        if position_size <= 0 or avg_volume <= 0:
            return 0.0

        # 简化模型：滑点正比于 止盈量 / 日均成交量
        close_volume = position_size * close_pct
        volume_ratio = close_volume / avg_volume

        # 基础滑点 = volume_ratio * 10 bps（假设成交 1% 日均量产生 10 bps 滑点）
        estimated_bps = volume_ratio * 10.0
        return round(estimated_bps, 2)

    def _publish_decision(
        self,
        position_id: str,
        close_pct: float,
        health_score: float,
        profit_atr_ratio: float,
        direction: int,
        reason: str,
    ) -> None:
        """
        推送止盈决策事件供叙事官和决策溯源使用

        注意：此事件在止盈建议生成时推送，标记 status="proposed"。
        实际执行结果由调用方在止盈完成后通过 update_score 或其他机制反馈。
        """
        event_data = {
            "position_id": position_id,
            "close_pct": close_pct,
            "health_score": health_score,
            "profit_atr_ratio": profit_atr_ratio,
            "direction": direction,
            "reason": reason,
            "status": "proposed",
            "timestamp": time.time(),
        }

        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_event'):
            try:
                self._negotiation_bus.publish_event(
                    event_type="partial_close_decision",
                    data=event_data,
                )
            except Exception as e:
                logger.warning(f"协商总线事件推送失败: {e}")

        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="partial_close_decision",
                    details=event_data,
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

        logger.info(
            "止盈决策: position=%s, close_pct=%.0f%%, health=%.1f, profit_atr=%.2f, dir=%s, reason=%s",
            position_id,
            close_pct * 100,
            health_score,
            profit_atr_ratio,
            '多' if direction == 1 else '空',
            reason,
            )
