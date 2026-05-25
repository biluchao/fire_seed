"""
火种系统 · 背压处理器 (BackpressureHandler)

核心职责：
1. 评估四车道信号队列的积压程度，返回背压等级（none/low/medium/high）
2. 根据背压等级对信号列表执行压缩或丢弃策略，返回处理后的信号列表

外部依赖（真实模块接口）：
- core.signal_bus.lane_scheduler.LaneScheduler : 获取各车道实时队列深度（调用 get_queue_depth 方法）
- core.negotiation_bus.NegotiationBus : 发送背压告警事件（调用 publish_alert 方法）
- core.behavioral_logger.BehavioralLogger : 记录背压操作日志（调用 log_event 方法）

接口契约：
- assess_pressure(lane_name: str) -> Dict[str, Any] : 评估指定车道当前背压等级
- apply_backpressure(lane_name: str, signals: List[Dict]) -> List[Dict] : 执行背压策略，返回处理后信号列表
- get_pressure_stats() -> Dict[str, Any] : 获取所有车道的背压统计汇总
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 LaneScheduler 不可用或调用失败时，默认假设队列为空（无背压），信号原样放行
- 当 NegotiationBus 不可用时，告警降级为仅本地日志记录
- 当 BehavioralLogger 不可用时，日志记录静默降级
- 所有降级值在类常量区明确声明

资源管理：
- 本模块不持有任何需要手动释放的外部资源
- 背压统计数据的清理通过定时器自动执行，线程锁在模块销毁时自动释放
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional
from collections import deque, Counter

logger = logging.getLogger(__name__)


class BackpressureHandler:
    """四车道背压处理器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 背压阈值（队列深度占容量的比例）
    LOW_PRESSURE_THRESHOLD = 0.5        # 轻度背压阈值，无量纲，取值范围 [0.3, 0.7]
    MEDIUM_PRESSURE_THRESHOLD = 0.7     # 中度背压阈值，无量纲，取值范围 [0.5, 0.9]
    HIGH_PRESSURE_THRESHOLD = 0.9       # 高度背压阈值，无量纲，取值范围 [0.7, 1.0]

    # 信号处理参数
    COMPRESSION_BATCH_SIZE = 10         # 批量压缩最小条数，无量纲，取值范围 [5, 50]
    MAX_DISCARD_RATIO = 0.5             # 单次处理最大丢弃比例，无量纲，取值范围 [0.1, 0.8]
    LOW_PRIORITY_URGENCY = 4            # 低优先级信号阈值（urgency低于此值可被丢弃），无量纲，[1, 5]

    # 统计与清理参数
    STATS_WINDOW_SIZE = 300             # 历史记录窗口大小，条，取值范围 [100, 1000]
    CLEANUP_INTERVAL_SEC = 600          # 统计清理间隔，秒，取值范围 [300, 3600]
    MAX_STATS_AGE_SEC = 1800            # 统计数据最大保留时间，秒，取值范围 [600, 7200]

    # 告警去重窗口
    ALERT_DEDUP_WINDOW_SEC = 30         # 同类型告警去重窗口，秒，取值范围 [10, 120]

    # 各车道队列容量（与 LaneScheduler 保持一致）
    LANE_QUEUE_CAPACITIES = {
        "express": 64,
        "fast": 256,
        "normal": 1024,
        "slow": 4096,
    }

    LANE_DESCRIPTIONS = {
        "express": "极速车道",
        "fast": "快速车道",
        "normal": "普通车道",
        "slow": "慢速车道",
    }

    def __init__(self):
        """初始化背压处理器，创建统计字典和线程锁"""
        self._pressure_stats: Dict[str, Dict[str, Any]] = {}
        for lane in self.LANE_QUEUE_CAPACITIES:
            self._pressure_stats[lane] = {
                "applied_count": 0,          # 执行背压处理的次数
                "compressed_count": 0,       # 被压缩的信号总数
                "discarded_count": 0,        # 被丢弃的信号总数
                "history": deque(maxlen=self.STATS_WINDOW_SIZE),  # 操作历史记录
            }

        # 告警去重记录
        self._alert_last_triggered: Dict[str, float] = {}

        # 外部依赖注入
        self._lane_scheduler = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 线程安全锁（保护所有共享状态）
        self._lock = threading.Lock()

        # 清理定时器
        self._last_cleanup = time.time()

        logger.info("BackpressureHandler 初始化完成，监控 %d 条车道", len(self.LANE_QUEUE_CAPACITIES))

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        lane_scheduler: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）

        Args:
            lane_scheduler: 车道调度器实例，需实现 get_queue_depth 方法
            negotiation_bus: 协商总线实例，需实现 publish_alert 方法
            behavioral_logger: 行为日志实例，需实现 log_event 方法
        """
        if lane_scheduler is not None:
            # 鸭子类型检查：确保注入的实例实现了必要的方法
            if not hasattr(lane_scheduler, 'get_queue_depth'):
                logger.warning("LaneScheduler 缺少 get_queue_depth 方法，该依赖将不可用")
            else:
                self._lane_scheduler = lane_scheduler
                logger.info("LaneScheduler 注入成功")

        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, 'publish_alert'):
                logger.warning("NegotiationBus 缺少 publish_alert 方法，告警推送不可用")
            else:
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")

    # ========== 公共接口 ==========
    def assess_pressure(self, lane_name: str) -> Dict[str, Any]:
        """
        评估指定车道当前背压等级

        根据队列深度与容量的比值确定背压等级。当依赖不可用时，采用保守默认值。

        Args:
            lane_name: 车道名称，必须是 LANE_QUEUE_CAPACITIES 中的键

        Returns:
            标准响应字典，data 中包含 pressure_level, queue_depth, usage_ratio, recommendation
        """
        # 参数校验
        if lane_name not in self.LANE_QUEUE_CAPACITIES:
            logger.warning(f"无效车道名称: {lane_name}")
            return {
                "status": "error",
                "reason": f"无效车道名称: {lane_name}，有效值为 {list(self.LANE_QUEUE_CAPACITIES.keys())}",
                "data": {},
                "warnings": [f"未知车道: {lane_name}"],
            }

        capacity = self.LANE_QUEUE_CAPACITIES[lane_name]
        queue_depth = 0

        # 获取队列深度（降级策略：依赖不可用时假设队列为空）
        if self._lane_scheduler is not None:
            try:
                queue_depth = self._lane_scheduler.get_queue_depth(lane_name)
                if not isinstance(queue_depth, (int, float)):
                    logger.warning(f"get_queue_depth 返回非数值类型，采用默认值 0")
                    queue_depth = 0
            except Exception as e:
                logger.warning(f"获取队列深度失败: {e} #RECOVERY: 检查 LaneScheduler 是否正常运行")
                queue_depth = 0
        else:
            logger.debug("LaneScheduler 不可用，假设队列为空")

        # 计算使用率
        usage_ratio = queue_depth / capacity if capacity > 0 else 0.0

        # 判定背压等级
        if usage_ratio >= self.HIGH_PRESSURE_THRESHOLD:
            level = "high"
            recommendation = "紧急：丢弃低优先级信号，压缩可合并信号"
        elif usage_ratio >= self.MEDIUM_PRESSURE_THRESHOLD:
            level = "medium"
            recommendation = "警告：压缩低优先级信号，减少新信号入队"
        elif usage_ratio >= self.LOW_PRESSURE_THRESHOLD:
            level = "low"
            recommendation = "注意：监控队列变化，暂不干预"
        else:
            level = "none"
            recommendation = "保持正常处理"

        return {
            "status": "ok",
            "reason": f"{self.LANE_DESCRIPTIONS.get(lane_name, lane_name)} 背压等级: {level}",
            "data": {
                "lane": lane_name,
                "pressure_level": level,
                "queue_depth": queue_depth,
                "capacity": capacity,
                "usage_ratio": round(usage_ratio, 4),
                "recommendation": recommendation,
            },
            "warnings": [],
        }

    def apply_backpressure(self, lane_name: str, signals: List[Dict]) -> List[Dict]:
        """
        对信号列表执行背压策略，返回处理后的信号列表

        策略：
        - none/low：原样返回
        - medium：将低优先级信号按类型合并为一条摘要信号
        - high：丢弃部分低优先级信号（最多丢弃 MAX_DISCARD_RATIO），其余保留

        Args:
            lane_name: 车道名称
            signals: 待处理的信号列表，每个信号字典需包含 "urgency" 字段

        Returns:
            处理后的信号列表
        """
        if not signals:
            return signals

        if lane_name not in self.LANE_QUEUE_CAPACITIES:
            logger.warning(f"无效车道名称: {lane_name}，原样返回信号")
            return signals

        # 评估背压（降级时默认无背压）
        assessment = self.assess_pressure(lane_name)
        if assessment["status"] != "ok":
            logger.warning("背压评估失败，原样返回信号")
            return signals

        pressure_level = assessment["data"]["pressure_level"]
        original_count = len(signals)

        # 轻度或无背压：直接返回
        if pressure_level in ("none", "low"):
            return signals

        with self._lock:
            # 分离高优先级和低优先级信号
            high_priority = []
            low_priority = []
            for sig in signals:
                urgency = sig.get("urgency", 5)
                if isinstance(urgency, (int, float)) and urgency >= self.LOW_PRIORITY_URGENCY:
                    high_priority.append(sig)
                else:
                    low_priority.append(sig)

            if pressure_level == "medium":
                # 中度背压：压缩低优先级信号
                if len(low_priority) >= self.COMPRESSION_BATCH_SIZE:
                    compressed = self._compress_signals(low_priority)
                    result = high_priority + compressed
                    self._pressure_stats[lane_name]["compressed_count"] += len(low_priority) - len(compressed)
                else:
                    result = high_priority + low_priority  # 数量不足不压缩
            else:  # high
                # 高度背压：丢弃部分低优先级信号
                max_keep = max(0, int(len(low_priority) * (1 - self.MAX_DISCARD_RATIO)))
                kept = low_priority[:max_keep]
                discarded = len(low_priority) - len(kept)
                result = high_priority + kept
                self._pressure_stats[lane_name]["discarded_count"] += discarded

                if discarded > 0:
                    logger.warning(
                        "%s 丢弃 %d 条低优先级信号 (使用率 %.1f%%)",
                        self.LANE_DESCRIPTIONS.get(lane_name, lane_name),
                        discarded,
                        assessment["data"]["usage_ratio"] * 100,
                    )

            # 记录操作历史
            self._pressure_stats[lane_name]["applied_count"] += 1
            self._pressure_stats[lane_name]["history"].append({
                "timestamp": time.time(),
                "lane": lane_name,
                "level": pressure_level,
                "original_count": original_count,
                "result_count": len(result),
                "reduced": original_count - len(result),
            })

            # 触发告警（如果信号数量发生了变化）
            if len(result) != original_count:
                self._trigger_alert(
                    lane_name,
                    pressure_level,
                    f"背压处理: {original_count}条 -> {len(result)}条",
                )

            return result

    def get_pressure_stats(self) -> Dict[str, Any]:
        """
        获取所有车道的背压统计汇总

        Returns:
            标准响应字典，data 中包含各车道累计操作次数和最近历史
        """
        self._try_cleanup()

        all_stats = {}
        total_applied = 0
        total_compressed = 0
        total_discarded = 0

        with self._lock:
            for lane in self.LANE_QUEUE_CAPACITIES:
                stats = self._pressure_stats[lane]
                history_list = list(stats["history"])
                recent_ops = len(history_list[-10:]) if history_list else 0
                all_stats[lane] = {
                    "applied_count": stats["applied_count"],
                    "compressed_count": stats["compressed_count"],
                    "discarded_count": stats["discarded_count"],
                    "recent_operations": recent_ops,
                }
                total_applied += stats["applied_count"]
                total_compressed += stats["compressed_count"]
                total_discarded += stats["discarded_count"]

        return {
            "status": "ok",
            "reason": f"背压统计汇总: 累计处理 {total_applied} 次, 压缩 {total_compressed} 条, 丢弃 {total_discarded} 条",
            "data": {
                "total_applied": total_applied,
                "total_compressed": total_compressed,
                "total_discarded": total_discarded,
                "lanes": all_stats,
            },
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
            if not hasattr(self, '_pressure_stats') or not self._pressure_stats:
                return {
                    "status": "degraded",
                    "reason": "背压统计数据结构未初始化",
                    "data": {},
                    "warnings": ["pressure_stats_not_initialized"],
                }

            with self._lock:
                lane_count = len(self._pressure_stats)
                total_applied = sum(s["applied_count"] for s in self._pressure_stats.values())

            return {
                "status": "ok",
                "reason": f"BackpressureHandler 正常，监控 {lane_count} 条车道，累计处理 {total_applied} 次",
                "data": {
                    "lane_count": lane_count,
                    "total_applied": total_applied,
                    "dependencies": {
                        "lane_scheduler": self._lane_scheduler is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                    },
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查锁状态和统计字典完整性")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _compress_signals(self, signals: List[Dict]) -> List[Dict]:
        """
        将低优先级信号按类型统计并压缩为一条摘要信号

        Args:
            signals: 待压缩的信号列表

        Returns:
            包含一条摘要信号的列表
        """
        if not signals or len(signals) < self.COMPRESSION_BATCH_SIZE:
            return signals

        # 按类型统计
        type_counts = Counter(sig.get("type", "unknown") for sig in signals)

        summary = {
            "type": "compressed_summary",
            "urgency": 0,
            "timestamp": time.time(),
            "original_count": len(signals),
            "type_distribution": dict(type_counts),
            "reason": f"背压压缩: {len(signals)}条低优先级信号合并为摘要",
        }

        logger.debug("背压压缩: %d 条信号 -> 1 条摘要", len(signals))
        return [summary]

    def _trigger_alert(self, lane_name: str, level: str, message: str) -> None:
        """
        触发告警（含去重机制，需在锁内调用以保证原子性）

        Args:
            lane_name: 车道名称
            level: 告警级别
            message: 告警消息
        """
        alert_key = f"backpressure:{lane_name}:{level}"
        now = time.time()
        last_time = self._alert_last_triggered.get(alert_key, 0)
        if now - last_time < self.ALERT_DEDUP_WINDOW_SEC:
            return

        self._alert_last_triggered[alert_key] = now
        alert_msg = f"[BACKPRESSURE-{level.upper()}] {self.LANE_DESCRIPTIONS.get(lane_name, lane_name)}: {message}"

        # 通过协商总线推送告警
        if self._negotiation_bus is not None:
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="backpressure",
                    lane=lane_name,
                    level=level,
                    message=message,
                    timestamp=now,
                )
            except Exception as e:
                logger.warning(f"协商总线告警推送失败: {e}")

        # 本地日志记录
        if level == "high":
            logger.error(
                "%s #RECOVERY: 降低该车道信号流量、检查上游模块、启用降级策略",
                alert_msg
            )
        else:
            logger.warning(alert_msg)

        # 行为日志记录
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="backpressure_action",
                    details={
                        "lane": lane_name,
                        "level": level,
                        "message": message,
                        "timestamp": now,
                    },
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

    def _try_cleanup(self) -> None:
        """定期清理过期的操作历史记录"""
        now = time.time()
        if now - self._last_cleanup < self.CLEANUP_INTERVAL_SEC:
            return

        with self._lock:
            cutoff = now - self.MAX_STATS_AGE_SEC
            total_removed = 0
            for lane in self.LANE_QUEUE_CAPACITIES:
                history = self._pressure_stats[lane]["history"]
                before = len(history)
                while history and history[0]["timestamp"] < cutoff:
                    history.popleft()
                    total_removed += 1
                if len(history) < before:
                    logger.debug("车道 %s 背压历史清理: %d -> %d", lane, before, len(history))

        self._last_cleanup = now
        if total_removed > 0:
            logger.info("全局背压历史清理: %d 条过期记录", total_removed)
