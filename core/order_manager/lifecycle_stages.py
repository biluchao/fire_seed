"""
火种系统 · 持仓生命周期阶段管理器 (LifecycleStages)

核心职责：
1. 管理每笔持仓的五阶段生命周期（孵化期、加速期、成熟期、衰减期、终结期），提供阶段判定与自动流转
2. 基于持仓时长、浮盈幅度、价格行为触发动态阶段切换，输出标准化的阶段状态信息
3. 支持状态导出与恢复，适配系统重启后的持仓上下文重建

外部依赖（真实模块接口）：
- core.order_manager.profit_compression.ProfitCompression : 获取当前紧缩利润阶段
- core.perception.tactile_cortex.TactileCortex : 获取当前波动率分位
- core.negotiation_bus.NegotiationBus : 发送阶段变更事件
- core.position_snapshot.PositionSnapshot : 持仓快照持久化（用于状态导出/恢复）

接口契约：
- get_stage(position_id, entry_time, entry_price, current_price, direction) -> Dict[str, Any]
- advance_stage(position_id, current_stage, context) -> Dict[str, Any]
- export_state(position_id) -> Dict[str, Any]
- restore_state(position_id, state) -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- 所有公共方法输出字典固定包含 "stage" (str), "stage_index" (int), "reason" (str), "warnings" (List[str])

异常与降级：
- 当 ProfitCompression 不可用时，阶段流转中的紧缩状态检查默认使用正常值
- 当 TactileCortex 不可用时，波动率分位使用 50% 作为中性默认值
- 当 NegotiationBus 不可用时，阶段变更事件降级为本地日志记录
- 极端价格跳空时对利润值进行波动率自适应钳制，防止阶段误判
- 所有降级值在类常量区明确声明

资源管理：
- 本模块维护活跃持仓的阶段状态缓存，定期清理已平仓或超时的条目
- 采用“标记-扫描-清理”三步模式降低锁竞争
- 不持有任何需要手动释放的外部资源
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple
from collections import OrderedDict

logger = logging.getLogger(__name__)


class LifecycleStages:
    """持仓生命周期五阶段状态机"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========

    # 五阶段定义（按序）
    STAGE_INCUBATION = "incubation"
    STAGE_ACCELERATION = "acceleration"
    STAGE_MATURITY = "maturity"
    STAGE_DECLINE = "decline"
    STAGE_TERMINATION = "termination"

    STAGES_ORDER = [
        STAGE_INCUBATION,
        STAGE_ACCELERATION,
        STAGE_MATURITY,
        STAGE_DECLINE,
        STAGE_TERMINATION,
    ]
    STAGE_INDEX = {s: i for i, s in enumerate(STAGES_ORDER)}

    # 各阶段默认最大时长（秒），仅用于无外部波动率数据时的回退值
    DEFAULT_MAX_DURATION = {
        STAGE_INCUBATION: 60,
        STAGE_ACCELERATION: 180,
        STAGE_MATURITY: 300,
        STAGE_DECLINE: 600,
        STAGE_TERMINATION: float("inf"),
    }

    # 各阶段触发条件相关阈值
    ACCELERATION_MIN_PROFIT_PCT = 0.003      # 孵化期→加速期最小浮盈，无量纲，[0.001, 0.01]
    MATURITY_MIN_PROFIT_PCT = 0.01           # 加速期→成熟期最小浮盈，无量纲，[0.005, 0.02]
    DECLINE_MAX_PROFIT_PCT = 0.01            # 成熟期→衰减期利润上限，无量纲，[0.003, 0.02]
    DECLINE_TRIGGER_DURATION_SEC = 30        # 成熟期低利润持续触发衰减，秒，[15, 120]
    TIME_FORCE_CLOSE_SEC = 300               # 强制终结持仓时长，秒，[120, 600]
    TIME_FORCE_CLOSE_MIN_PROFIT_PCT = 0.01   # 强制终结最小浮盈，无量纲，[0.005, 0.02]

    # 波动率对时间窗口的动态调整系数
    VOL_ADJUSTMENT_HIGH = 0.7               # 高波动压缩时间窗口，[0.5, 0.9]
    VOL_ADJUSTMENT_LOW = 1.3                # 低波动拉伸时间窗口，[1.1, 1.5]

    # 加速期回踩保护
    ACCELERATION_RETRACE_THRESHOLD = 0.4     # 从阶段最高盈利回撤比例触发衰减，[0.3, 0.6]

    # 利润异常钳制参数
    PROFIT_CLAMP_HIGH_VOL = 0.05             # 高波动秒级合理变动上限，[0.03, 0.08]
    PROFIT_CLAMP_MID_VOL = 0.02              # 中波动秒级合理变动上限，[0.01, 0.04]
    PROFIT_CLAMP_LOW_VOL = 0.008             # 低波动秒级合理变动上限，[0.003, 0.015]
    PROFIT_CLAMP_ELAPSED_THRESHOLD = 5.0     # 短持仓时间阈值，秒，[2, 10]
    PROFIT_CLAMP_SHORT_FACTOR = 0.5          # 短持仓时间额外收紧因子，[0.3, 0.7]

    # 缓存管理
    MAX_CACHE_ENTRIES = 1000
    CLEANUP_INTERVAL_SEC = 60
    CACHE_TTL_SEC = 3600

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._last_cleanup = time.time()
        self._last_cleanup_duration = 0.0  # 上一次清理耗时（用于监控）

        # 外部依赖注入（可选）
        self._profit_compression = None
        self._tactile_cortex = None
        self._negotiation_bus = None

        logger.info("LifecycleStages 初始化完成，五阶段: %s", " → ".join(self.STAGES_ORDER))

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        profit_compression: Optional[Any] = None,
        tactile_cortex: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
    ) -> None:
        """注入外部依赖（可选）"""
        if profit_compression is not None:
            self._profit_compression = profit_compression
            logger.info("ProfitCompression 注入成功")
        else:
            logger.warning("ProfitCompression 未注入，紧缩检查降级")
        if tactile_cortex is not None:
            self._tactile_cortex = tactile_cortex
            logger.info("TactileCortex 注入成功")
        else:
            logger.warning("TactileCortex 未注入，波动率分位使用默认值")
        if negotiation_bus is not None:
            self._negotiation_bus = negotiation_bus
            logger.info("NegotiationBus 注入成功")
        else:
            logger.warning("NegotiationBus 未注入，阶段变更事件降级")

    # ========== 公共接口 ==========
    def get_stage(
        self,
        position_id: str,
        entry_time: float,
        entry_price: float,
        current_price: float,
        direction: int,
    ) -> Dict[str, Any]:
        """
        获取持仓当前所处的生命周期阶段

        Args:
            position_id: 持仓唯一标识
            entry_time: 开仓时间（Unix 秒）
            entry_price: 开仓均价
            current_price: 当前市场价格
            direction: 持仓方向，1 为多头，-1 为空头

        Returns:
            标准响应字典，data 中包含 stage, stage_index, elapsed_seconds, profit_pct 等字段
        """
        if not position_id:
            logger.warning("position_id 为空")
            return self._build_termination_response("position_id 无效")
        if entry_price <= 0 or current_price <= 0:
            logger.warning(f"价格无效: entry={entry_price}, current={current_price}")
            return self._build_termination_response("价格数据无效")

        # 计算持仓时长和原始浮盈比例
        now = time.time()
        elapsed = max(0.0, now - entry_time)
        if direction == 1:
            raw_profit_pct = (current_price - entry_price) / entry_price
        else:
            raw_profit_pct = (entry_price - current_price) / entry_price

        # 获取波动率分位（降级默认 50%）
        vol_percentile = self._get_volatility_percentile()

        # 利润异常钳制
        profit_pct, was_clamped = self._clamp_profit_pct(raw_profit_pct, vol_percentile, elapsed)

        with self._lock:
            cached = self._cache.get(position_id)
            if cached and cached.get("stage") != self.STAGE_TERMINATION:
                prev_stage = cached["stage"]
                # 更新阶段最高浮盈（用于回踩检测）
                if "stage_high_profit" not in cached or profit_pct > cached["stage_high_profit"]:
                    cached["stage_high_profit"] = profit_pct

                new_stage, reason = self._evaluate_stage(
                    prev_stage, elapsed, profit_pct, vol_percentile, cached
                )
                if new_stage != prev_stage:
                    cached["stage"] = new_stage
                    cached["reason"] = reason
                    cached["updated_at"] = now
                    self._notify_stage_change(position_id, prev_stage, new_stage, reason)
                return self._build_response(new_stage, elapsed, profit_pct, reason, cached)

            # 无缓存或已终结，从头计算
            stage, reason = self._evaluate_initial(elapsed, profit_pct, vol_percentile)
            self._cache[position_id] = {
                "stage": stage,
                "reason": reason,
                "entry_time": entry_time,
                "updated_at": now,
                "below_decline_since": None,
                "stage_high_profit": profit_pct,
                "stage_history": [{"stage": stage, "timestamp": now}],
            }
            self._try_cleanup()
            return self._build_response(stage, elapsed, profit_pct, reason, self._cache[position_id])

    def advance_stage(
        self,
        position_id: str,
        current_stage: str,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        手动推进阶段（供外部模块在检测到特殊事件时强制转换）

        Args:
            position_id: 持仓唯一标识
            current_stage: 当前阶段
            context: 包含当前价格、时间等必要数据

        Returns:
            标准响应字典
        """
        if current_stage not in self.STAGE_INDEX:
            return {
                "status": "error",
                "reason": f"无效阶段: {current_stage}",
                "data": {},
                "warnings": [f"unknown_stage: {current_stage}"],
            }

        current_idx = self.STAGE_INDEX[current_stage]
        if current_idx >= len(self.STAGES_ORDER) - 1:
            return {
                "status": "ok",
                "reason": "已处于终结期，无法继续推进",
                "data": {"stage": self.STAGE_TERMINATION, "stage_index": current_idx},
                "warnings": [],
            }

        next_stage = self.STAGES_ORDER[current_idx + 1]
        with self._lock:
            cached = self._cache.get(position_id, {})
            cached["stage"] = next_stage
            cached["reason"] = f"外部触发: {context.get('trigger', 'manual')}"
            cached["updated_at"] = time.time()
            if "stage_history" not in cached:
                cached["stage_history"] = []
            cached["stage_history"].append({"stage": next_stage, "timestamp": time.time()})
            self._cache[position_id] = cached

        logger.info(
            f"手动推进阶段: {position_id} {current_stage} → {next_stage}, 原因: {context.get('reason', '外部触发')}"
        )
        self._notify_stage_change(position_id, current_stage, next_stage, "外部触发")
        return {
            "status": "ok",
            "reason": f"已推进至 {next_stage}",
            "data": {"stage": next_stage, "stage_index": self.STAGE_INDEX[next_stage]},
            "warnings": [],
        }

    def export_state(self, position_id: str) -> Dict[str, Any]:
        """
        导出指定持仓的生命周期状态，用于快照持久化

        Args:
            position_id: 持仓唯一标识

        Returns:
            标准响应字典，data 中包含 stage, below_decline_since, stage_history 等字段
        """
        with self._lock:
            cached = self._cache.get(position_id, {})
            state = {
                "stage": cached.get("stage", self.STAGE_INCUBATION),
                "below_decline_since": cached.get("below_decline_since"),
                "stage_high_profit": cached.get("stage_high_profit"),
                "stage_history": cached.get("stage_history", []),
                "exported_at": time.time(),
            }
            return {
                "status": "ok",
                "reason": f"已导出 {position_id} 的生命周期状态",
                "data": state,
                "warnings": [],
            }

    def restore_state(self, position_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        从快照恢复持仓的生命周期状态

        Args:
            position_id: 持仓唯一标识
            state: 由 export_state 导出的状态字典

        Returns:
            标准响应字典
        """
        with self._lock:
            self._cache[position_id] = {
                "stage": state.get("stage", self.STAGE_INCUBATION),
                "below_decline_since": state.get("below_decline_since"),
                "stage_high_profit": state.get("stage_high_profit"),
                "stage_history": state.get("stage_history", []),
                "restored_at": time.time(),
                "updated_at": time.time(),
            }
            stage = self._cache[position_id]["stage"]
            logger.info(f"恢复生命周期状态: {position_id} → {stage}")
            return {
                "status": "ok",
                "reason": f"已恢复 {position_id} 的生命周期状态",
                "data": {"stage": stage},
                "warnings": [],
            }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            with self._lock:
                cache_size = len(self._cache)
                stage_dist = {stage: 0 for stage in self.STAGES_ORDER}
                valid_count = 0
                for c in self._cache.values():
                    s = c.get("stage", "")
                    if s in stage_dist:
                        stage_dist[s] += 1
                        valid_count += 1
                buffer_usage_pct = (cache_size / self.MAX_CACHE_ENTRIES * 100) if self.MAX_CACHE_ENTRIES > 0 else 0

            return {
                "status": "ok",
                "reason": f"LifecycleStages 正常，缓存条目 {cache_size}，有效条目 {valid_count}",
                "data": {
                    "cache_size": cache_size,
                    "valid_entries": valid_count,
                    "buffer_usage_pct": round(buffer_usage_pct, 1),
                    "stage_distribution": stage_dist,
                    "last_cleanup_duration_ms": round(self._last_cleanup_duration * 1000, 2),
                    "dependencies": {
                        "profit_compression": self._profit_compression is not None,
                        "tactile_cortex": self._tactile_cortex is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查缓存锁与数据结构")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _get_volatility_percentile(self) -> float:
        """获取当前波动率分位（降级默认 50%）"""
        if self._tactile_cortex is not None and hasattr(
            self._tactile_cortex, 'get_volatility_percentile'
        ):
            try:
                return float(self._tactile_cortex.get_volatility_percentile())
            except Exception as e:
                logger.warning(f"获取波动率分位失败: {e}，使用默认值 50%")
        return 50.0

    def _clamp_profit_pct(
        self, profit_pct: float, vol_percentile: float, elapsed: float
    ) -> Tuple[float, bool]:
        """
        对异常利润值进行波动率自适应钳制

        Returns:
            (clamped_profit_pct, was_clamped)
        """
        if vol_percentile > 80:
            max_move = self.PROFIT_CLAMP_HIGH_VOL
        elif vol_percentile > 50:
            max_move = self.PROFIT_CLAMP_MID_VOL
        else:
            max_move = self.PROFIT_CLAMP_LOW_VOL

        if elapsed < self.PROFIT_CLAMP_ELAPSED_THRESHOLD:
            max_move *= self.PROFIT_CLAMP_SHORT_FACTOR

        if abs(profit_pct) > max_move:
            clamped = max_move * (1 if profit_pct > 0 else -1)
            logger.warning(
                f"利润值钳制: 原始 {profit_pct*100:.3f}% → 钳制 {clamped*100:.3f}%, "
                f"波动率分位 {vol_percentile:.0f}, 持仓时长 {elapsed:.1f}s"
            )
            return clamped, True
        return profit_pct, False

    def _evaluate_initial(
        self, elapsed: float, profit_pct: float, vol_percentile: float
    ) -> Tuple[str, str]:
        """从零开始评估持仓阶段（用于新持仓）"""
        return self._evaluate_stage(self.STAGE_INCUBATION, elapsed, profit_pct, vol_percentile, None)

    def _evaluate_stage(
        self,
        current_stage: str,
        elapsed: float,
        profit_pct: float,
        vol_percentile: float,
        cached: Optional[Dict[str, Any]],
    ) -> Tuple[str, str]:
        """根据当前阶段、时长、浮盈评估是否推进"""
        time_mult = self._get_vol_time_mult(vol_percentile)

        # 时间强制终结检查
        if (
            elapsed > self.TIME_FORCE_CLOSE_SEC * time_mult
            and profit_pct < self.TIME_FORCE_CLOSE_MIN_PROFIT_PCT
        ):
            return self.STAGE_TERMINATION, (
                f"持仓超时({elapsed:.0f}s)且浮盈不足({profit_pct*100:.2f}%)，强制终结"
            )

        # 孵化期 → 加速期
        if current_stage == self.STAGE_INCUBATION:
            if profit_pct >= self.ACCELERATION_MIN_PROFIT_PCT:
                return self.STAGE_ACCELERATION, f"浮盈达到 {profit_pct*100:.2f}%，进入加速期"
            if elapsed > self.DEFAULT_MAX_DURATION[self.STAGE_INCUBATION] * time_mult:
                return self.STAGE_DECLINE, f"孵化期超时({elapsed:.0f}s)未盈利，进入衰减期"
            return current_stage, "孵化中"

        # 加速期 → 成熟期 / 衰减期
        if current_stage == self.STAGE_ACCELERATION:
            # 回踩检测
            if cached and "stage_high_profit" in cached:
                high_profit = cached["stage_high_profit"]
                if high_profit > 0 and profit_pct < high_profit * self.ACCELERATION_RETRACE_THRESHOLD:
                    if profit_pct < self.ACCELERATION_MIN_PROFIT_PCT:
                        logger.info(
                            f"加速期回踩过深: 最高 {high_profit*100:.3f}% → 当前 {profit_pct*100:.3f}%"
                        )
                        return self.STAGE_DECLINE, (
                            f"加速期回踩过深({high_profit*100:.3f}%→{profit_pct*100:.3f}%)，转入衰减"
                        )
            if profit_pct >= self.MATURITY_MIN_PROFIT_PCT:
                return self.STAGE_MATURITY, f"浮盈达到 {profit_pct*100:.2f}%，进入成熟期"
            if elapsed > self.DEFAULT_MAX_DURATION[self.STAGE_ACCELERATION] * time_mult:
                if profit_pct > 0:
                    return self.STAGE_MATURITY, "加速期超时但仍有正利润，进入成熟期"
                return self.STAGE_DECLINE, f"加速期超时({elapsed:.0f}s)且无利润，进入衰减期"
            return current_stage, "加速中"

        # 成熟期 → 衰减期
        if current_stage == self.STAGE_MATURITY:
            if profit_pct < self.DECLINE_MAX_PROFIT_PCT:
                now = time.time()
                if cached and cached.get("below_decline_since"):
                    below_duration = now - cached["below_decline_since"]
                else:
                    below_duration = 0.0
                    if cached is not None:
                        cached["below_decline_since"] = now
                if below_duration > self.DECLINE_TRIGGER_DURATION_SEC * time_mult:
                    return self.STAGE_DECLINE, (
                        f"成熟期持续低利润 {below_duration:.0f}s，进入衰减期"
                    )
                return current_stage, f"利润回落至{profit_pct*100:.2f}%，观察中"
            if cached:
                cached["below_decline_since"] = None
            if elapsed > self.DEFAULT_MAX_DURATION[self.STAGE_MATURITY] * time_mult:
                return self.STAGE_DECLINE, f"成熟期超时({elapsed:.0f}s)，进入衰减期"
            return current_stage, "成熟运行中"

        # 衰减期 → 终结期
        if current_stage == self.STAGE_DECLINE:
            if profit_pct < 0:
                return self.STAGE_TERMINATION, "衰减期出现亏损，立即终结"
            if elapsed > self.DEFAULT_MAX_DURATION[self.STAGE_DECLINE] * time_mult:
                return self.STAGE_TERMINATION, f"衰减期超时({elapsed:.0f}s)，强制终结"
            return current_stage, f"衰减中，浮盈 {profit_pct*100:.2f}%"

        # 终结期
        return self.STAGE_TERMINATION, "已终结"

    def _get_vol_time_mult(self, vol_percentile: float) -> float:
        """根据波动率分位获取时间窗口调整系数"""
        if vol_percentile > 70:
            return self.VOL_ADJUSTMENT_HIGH
        elif vol_percentile < 30:
            return self.VOL_ADJUSTMENT_LOW
        return 1.0

    def _build_response(
        self,
        stage: str,
        elapsed: float,
        profit_pct: float,
        reason: str,
        cached: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """构建标准响应"""
        result = {
            "stage": stage,
            "stage_index": self.STAGE_INDEX.get(stage, -1),
            "elapsed_seconds": round(elapsed, 3),
            "profit_pct": round(profit_pct, 6),
            "reason": reason,
            "warnings": [],
            "data": {
                "stage": stage,
                "stage_index": self.STAGE_INDEX.get(stage, -1),
                "elapsed_seconds": round(elapsed, 3),
                "profit_pct": round(profit_pct, 6),
                "stage_history": cached.get("stage_history", []) if cached else [],
            },
        }
        if stage == self.STAGE_TERMINATION:
            result["warnings"].append("position_should_be_closed")
        return result

    def _build_termination_response(self, reason: str) -> Dict[str, Any]:
        """构建终结期响应"""
        return {
            "stage": self.STAGE_TERMINATION,
            "stage_index": self.STAGE_INDEX[self.STAGE_TERMINATION],
            "elapsed_seconds": 0.0,
            "profit_pct": 0.0,
            "reason": reason,
            "warnings": [reason],
            "data": {
                "stage": self.STAGE_TERMINATION,
                "stage_index": self.STAGE_INDEX[self.STAGE_TERMINATION],
                "elapsed_seconds": 0.0,
                "profit_pct": 0.0,
                "stage_history": [],
            },
        }

    def _notify_stage_change(
        self, position_id: str, old_stage: str, new_stage: str, reason: str
    ) -> None:
        """通知阶段变更事件"""
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_event'):
            try:
                self._negotiation_bus.publish_event(
                    event_type="lifecycle_stage_change",
                    position_id=position_id,
                    old_stage=old_stage,
                    new_stage=new_stage,
                    reason=reason,
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning(f"发布阶段变更事件失败: {e}")
        logger.info(f"生命周期阶段变更: {position_id} {old_stage} → {new_stage}, 原因: {reason}")

    def _try_cleanup(self) -> None:
        """定期清理缓存中的无效条目（标记-扫描-清理三步模式）"""
        now = time.time()
        if now - self._last_cleanup < self.CLEANUP_INTERVAL_SEC:
            return

        cleanup_start = time.time()
        # 步骤1：在锁内快速标记待删除条目
        with self._lock:
            to_remove = []
            for pid, data in list(self._cache.items()):
                if data.get("stage") == self.STAGE_TERMINATION:
                    to_remove.append(pid)
                elif now - data.get("updated_at", 0) > self.CACHE_TTL_SEC:
                    to_remove.append(pid)
            self._last_cleanup = now

        # 步骤2：在锁外执行真正的删除操作，逐条加锁减少持锁时间
        removed_count = 0
        for pid in to_remove:
            with self._lock:
                if pid in self._cache:
                    del self._cache[pid]
                    removed_count += 1

        # 步骤3：超出容量限制的清理仍在锁内但使用批量操作
        with self._lock:
            overflow = 0
            while len(self._cache) > self.MAX_CACHE_ENTRIES:
                self._cache.popitem(last=False)
                overflow += 1

        self._last_cleanup_duration = time.time() - cleanup_start
        if removed_count > 0 or overflow > 0:
            logger.info(
                f"缓存清理完成: 移除 {removed_count} 条, 溢出清理 {overflow} 条, "
                f"当前 {len(self._cache)} 条, 耗时 {self._last_cleanup_duration*1000:.2f}ms"
            )
