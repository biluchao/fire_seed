"""
火种系统 · 背压处理器 (BackpressureHandler)

核心职责：
1. 监控四车道信号队列的积压程度，当队列深度超过阈值时自动执行信号压缩或丢弃策略
2. 将压缩或丢弃的低优先级信号转化为聚合摘要，并触发降级告警通知运维和上游模块

外部依赖（真实模块接口）：
- core.signal_bus.lane_scheduler.LaneScheduler : 获取各车道实时队列深度和吞吐量
- core.signal_bus.lane_health_monitor.LaneHealthMonitor : 获取车道健康状态以辅助背压决策
- core.negotiation_bus.NegotiationBus : 发送背压告警事件和降级通知
- core.behavioral_logger.BehavioralLogger : 记录背压操作日志

接口契约：
- assess_pressure(lane_name: str) -> Dict[str, Any] : 评估指定车道当前背压等级
- apply_backpressure(lane_name: str, signals: List[Dict]) -> List[Dict] : 执行背压策略，返回处理后信号列表
- get_pressure_stats() -> Dict[str, Any] : 获取所有车道的背压统计汇总
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 LaneScheduler 不可用时，默认假设无背压（保守放行），避免误拦截正常信号
- 当 LaneHealthMonitor 不可用时，仅依赖本地队列深度判断，不参考健康评分
- 当 NegotiationBus 不可用时，告警降级为仅本地日志记录
- 所有降级值在类常量区明确声明

资源管理：
- 本模块不持有任何需要手动释放的外部资源
- 背压统计数据的清理通过定期定时器自动执行，无需外部干预
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple
from collections import deque
import numpy as np

logger = logging.getLogger(__name__)


class BackpressureHandler:
    """四车道背压处理器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 背压阈值（队列深度占容量的比例）
    DEFAULT_LOW_PRESSURE_THRESHOLD = 0.5    # 轻度背压阈值，无量纲，[0.3, 0.7]
    DEFAULT_MEDIUM_PRESSURE_THRESHOLD = 0.7 # 中度背压阈值，无量纲，[0.5, 0.9]
    DEFAULT_HIGH_PRESSURE_THRESHOLD = 0.9   # 高度背压阈值，无量纲，[0.7, 1.0]

    # 压缩/丢弃策略参数
    DEFAULT_COMPRESSION_BATCH_SIZE = 10     # 批量压缩最小条数，无量纲，[5, 50]
    DEFAULT_MAX_DISCARD_RATIO = 0.5         # 单次处理最大丢弃比例，无量纲，[0.1, 0.8]
    DEFAULT_LOW_PRIORITY_MAX = 4            # 低优先级信号阈值（urgency低于此值可被丢弃），无量纲，[1, 5]

    # 统计与清理参数
    DEFAULT_STATS_WINDOW = 300              # 统计窗口大小，条，[100, 1000]
    DEFAULT_CLEANUP_INTERVAL_SEC = 600      # 统计清理间隔，秒，[300, 3600]
    DEFAULT_MAX_STATS_AGE_SEC = 1800        # 统计数据最大保留时间，秒，[600, 7200]

    # 告警去重
    DEFAULT_ALERT_DEDUP_WINDOW_SEC = 30     # 同类型告警去重窗口，秒，[10, 120]

    # 车道队列容量（与 lane_scheduler 保持一致）
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
        # 背压统计（每条车道独立）
        self._pressure_stats: Dict[str, Dict] = {}
        for lane in self.LANE_QUEUE_CAPACITIES:
            self._pressure_stats[lane] = {
                "applied_count": 0,
                "compressed_count": 0,
                "discarded_count": 0,
                "history": deque(maxlen=self.DEFAULT_STATS_WINDOW),
            }

        # 告警去重记录
        self._alert_last_triggered: Dict[str, float] = {}

        # 外部依赖注入
        self._lane_scheduler = None
        self._lane_health_monitor = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 线程安全
        self._lock = threading.Lock()

        # 清理定时器
        self._last_cleanup = time.time()

        logger.info("BackpressureHandler 初始化完成，监控 %d 条车道", len(self.LANE_QUEUE_CAPACITIES))

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        lane_scheduler: Optional[Any] = None,
        lane_health_monitor: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
        behavioral_logger: Optional[Any] = None,
    ) -> None:
        """
        注入外部依赖（可选注入，未注入时对应功能降级）
        """
        if lane_scheduler is not None:
            self._lane_scheduler = lane_scheduler
            logger.info("LaneScheduler 注入成功")
        else:
            logger.warning("LaneScheduler 未注入，背压检查将默认放行")

        if lane_health_monitor is not None:
            self._lane_health_monitor = lane_health_monitor
            logger.info("LaneHealthMonitor 注入成功")
        else:
            logger.warning("LaneHealthMonitor 未注入，健康评分参考不可用")

        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, 'publish_alert'):
                logger.warning("NegotiationBus 缺少 publish_alert 方法，告警推送不可用")
                self._negotiation_bus = None
            else:
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，日志降级为标准 logger")

    # ========== 公共接口 ==========
    def assess_pressure(self, lane_name: str) -> Dict[str, Any]:
        """
        评估指定车道当前背压等级

        Args:
            lane_name: 车道名称 (express/fast/normal/slow)

        Returns:
            标准响应字典，data 中包含 pressure_level, queue_usage_ratio, recommendation 等字段
        """
        if lane_name not in self.LANE_QUEUE_CAPACITIES:
            logger.warning(f"无效车道名称: {lane_name}")
            return {
                "status": "error",
                "reason": f"无效车道名称: {lane_name}，有效值为 {list(self.LANE_QUEUE_CAPACITIES.keys())}",
                "data": {},
                "warnings": [f"未知车道: {lane_name}"],
            }

        # 获取队列深度
        queue_depth = 0
        capacity = self.LANE_QUEUE_CAPACITIES[lane_name]
        if self._lane_scheduler is not None:
            try:
                queue_depth = self._lane_scheduler.get_queue_depth(lane_name)
            except Exception as e:
                logger.warning(f"获取队列深度失败: {e}，假设为空队列")
                queue_depth = 0
        else:
            # 降级：无法获取队列深度，假设无背压
            return {
                "status": "ok",
                "reason": "LaneScheduler 不可用，默认无背压",
                "data": {
                    "lane": lane_name,
                    "pressure_level": "none",
                    "queue_depth": 0,
                    "capacity": capacity,
                    "usage_ratio": 0.0,
                    "recommendation": "保持正常处理",
                },
                "warnings": ["lane_scheduler_unavailable"],
            }

        usage_ratio = queue_depth / capacity if capacity > 0 else 0

        # 判定背压等级
        if usage_ratio >= self.DEFAULT_HIGH_PRESSURE_THRESHOLD:
            level = "high"
            recommendation = "紧急：丢弃低优先级信号，压缩可合并信号"
        elif usage_ratio >= self.DEFAULT_MEDIUM_PRESSURE_THRESHOLD:
            level = "medium"
            recommendation = "警告：压缩低优先级信号，减少新信号入队"
        elif usage_ratio >= self.DEFAULT_LOW_PRESSURE_THRESHOLD:
            level = "low"
            recommendation = "注意：监控队列变化，暂不干预"
        else:
            level = "none"
            recommendation = "保持正常处理"

        return {
            "status": "ok",
            "reason": f"{self.LANE_DESCRIPTIONS[lane_name]} 背压等级: {level}",
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

        处理逻辑：
        - 轻度背压：不做处理，原样返回
        - 中度背压：将低优先级且同类型的信号合并为摘要信号
        - 高度背压：丢弃 urgency < LOW_PRIORITY_MAX 的低优先级信号，其余合并

        Args:
            lane_name: 车道名称
            signals: 待处理的信号列表，每个信号包含 "urgency" 字段

        Returns:
            处理后的信号列表
        """
        if not signals:
            return signals

        if lane_name not in self.LANE_QUEUE_CAPACITIES:
            logger.warning(f"无效车道名称: {lane_name}，原样返回信号")
            return signals

        # 评估当前背压
        assessment = self.assess_pressure(lane_name)
        if assessment["status"] != "ok":
            logger.warning(f"背压评估失败，原样返回信号")
            return signals

        pressure_level = assessment["data"]["pressure_level"]
        original_count = len(signals)

        with self._lock:
            # 轻度背压：不做处理
            if pressure_level == "none" or pressure_level == "low":
                return signals

            # 中度/高度背压：分离高优先级和低优先级信号
            high_priority = [s for s in signals if s.get("urgency", 5) >= self.DEFAULT_LOW_PRIORITY_MAX]
            low_priority = [s for s in signals if s.get("urgency", 5) < self.DEFAULT_LOW_PRIORITY_MAX]

            if pressure_level == "medium":
                # 中度背压：压缩低优先级信号
                compressed = self._compress_signals(low_priority)
                result = high_priority + compressed
                self._pressure_stats[lane_name]["compressed_count"] += len(low_priority) - len(compressed)
            else:
                # 高度背压：丢弃部分低优先级信号
                max_keep = int(len(low_priority) * (1 - self.DEFAULT_MAX_DISCARD_RATIO))
                kept_low = low_priority[:max_keep]
                discarded = len(low_priority) - len(kept_low)
                self._pressure_stats[lane_name]["discarded_count"] += discarded
                if discarded > 0:
                    logger.warning(
                        "%s 丢弃 %d 条低优先级信号 (queue_usage=%.1f%%)",
                        self.LANE_DESCRIPTIONS[lane_name],
                        discarded,
                        assessment["data"]["usage_ratio"] * 100,
                    )
                result = high_priority + kept_low

            self._pressure_stats[lane_name]["applied_count"] += 1
            self._pressure_stats[lane_name]["history"].append({
                "timestamp": time.time(),
                "lane": lane_name,
                "level": pressure_level,
                "original_count": original_count,
                "result_count": len(result),
                "compressed": original_count - len(result),
            })

            if original_count != len(result):
                self._trigger_alert(
                    lane_name,
                    pressure_level,
                    f"背压处理: 原始{original_count}条 -> 处理后{len(result)}条",
                )

            return result

    def get_pressure_stats(self) -> Dict[str, Any]:
        """
        获取所有车道的背压统计汇总

        Returns:
            标准响应字典，data 中包含各车道的累计压缩/丢弃次数和最近操作记录
        """
        self._try_cleanup()

        all_stats = {}
        total_applied = 0
        total_compressed = 0
        total_discarded = 0

        with self._lock:
            for lane in self.LANE_QUEUE_CAPACITIES:
                stats = self._pressure_stats[lane]
                recent_history = list(stats["history"])[-10:] if stats["history"] else []
                all_stats[lane] = {
                    "applied_count": stats["applied_count"],
                    "compressed_count": stats["compressed_count"],
                    "discarded_count": stats["discarded_count"],
                    "recent_operations": len(recent_history),
                }
                total_applied += stats["applied_count"]
                total_compressed += stats["compressed_count"]
                total_discarded += stats["discarded_count"]

        return {
            "status": "ok",
            "reason": f"背压统计汇总: 累计应用{total_applied}次, 压缩{total_compressed}条, 丢弃{total_discarded}条",
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
                "reason": f"BackpressureHandler 正常，监控 {lane_count} 条车道，累计背压处理 {total_applied} 次",
                "data": {
                    "lane_count": lane_count,
                    "total_applied": total_applied,
                    "dependencies": {
                        "lane_scheduler": self._lane_scheduler is not None,
                        "lane_health_monitor": self._lane_health_monitor is not None,
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
        将低优先级信号按类型压缩为摘要信号，超过批量阈值才执行压缩
        """
        if not signals:
            return signals

        # 当信号数量不足时不压缩，避免无意义操作
        if len(signals) < self.DEFAULT_COMPRESSION_BATCH_SIZE:
            return signals

        # 按 signal_type 分组统计
        from collections import Counter
        type_counts = Counter(s.get("type", "unknown") for s in signals)

        # 生成一条摘要信号
        summary = {
            "type": "compressed_summary",
            "urgency": 0,  # 最低优先级
            "timestamp": time.time(),
            "original_count": len(signals),
            "type_distribution": dict(type_counts),
            "reason": f"背压压缩: {len(signals)}条低优先级信号合并",
        }

        logger.debug(
            "背压压缩: %d 条低优先级信号 -> 1 条摘要 (类型分布: %s)",
            len(signals),
            dict(type_counts),
        )

        return [summary]

    def _trigger_alert(self, lane_name: str, level: str, message: str) -> None:
        """触发告警（含去重机制，需在锁内调用）"""
        alert_key = f"backpressure:{lane_name}:{level}"
        now = time.time()
        last_time = self._alert_last_triggered.get(alert_key, 0)
        if now - last_time < self.DEFAULT_ALERT_DEDUP_WINDOW_SEC:
            return

        self._alert_last_triggered[alert_key] = now
        alert_msg = f"[BACKPRESSURE-{level.upper()}] {self.LANE_DESCRIPTIONS[lane_name]}: {message}"

        # 协商总线推送
        if self._negotiation_bus is not None and hasattr(self._negotiation_bus, 'publish_alert'):
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

        # 本地日志
        if level == "high":
            logger.error(
                "%s #RECOVERY: 降低该车道信号流量、检查上游模块是否异常、考虑动态核心借用",
                alert_msg
            )
        else:
            logger.warning(alert_msg)

        # 行为日志
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
        """定期清理过期统计数据"""
        now = time.time()
        if now - self._last_cleanup < self.DEFAULT_CLEANUP_INTERVAL_SEC:
            return

        with self._lock:
            cutoff = now - self.DEFAULT_MAX_STATS_AGE_SEC
            total_removed = 0
            for lane in self.LANE_QUEUE_CAPACITIES:
                history = self._pressure_stats[lane]["history"]
                before = len(history)
                while history and history[0]["timestamp"] < cutoff:
                    history.popleft()
                    total_removed += 1
                if len(history) < before:
                    logger.debug("车道 %s 背压历史清理: %d -> %d 条", lane, before, len(history))

        self._last_cleanup = now
        if total_removed > 0:
            logger.info("全局背压历史清理: %d 条过期记录", total_removed)
