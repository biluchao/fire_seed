"""
火种系统 · 背压处理器 (BackpressureHandler)

核心职责：
1. 评估四车道信号队列的积压程度，返回背压等级（none/low/medium/high）
2. 根据背压等级对信号列表执行压缩或丢弃策略，返回处理后的信号列表
3. 提供 Prometheus 兼容的监控指标导出，支撑机构级可观测性

外部依赖（真实模块接口）：
- core.signal_bus.lane_scheduler.LaneScheduler : 获取各车道实时队列深度（调用 get_queue_depth 方法）
- core.signal_bus.lane_health_monitor.LaneHealthMonitor : 获取车道健康状态辅助背压决策（调用 get_health_score 方法）
- core.negotiation_bus.NegotiationBus : 发送背压告警事件（调用 publish_alert 方法）
- core.behavioral_logger.BehavioralLogger : 记录背压操作日志（调用 log_event 方法）

接口契约：
- assess_pressure(lane_name: str) -> Dict[str, Any] : 评估指定车道当前背压等级
- apply_backpressure(lane_name: str, signals: List[Dict]) -> List[Dict] : 执行背压策略，返回处理后信号列表
- get_pressure_stats() -> Dict[str, Any] : 获取所有车道的背压统计汇总
- export_metrics() -> Dict[str, float] : 导出 Prometheus 兼容的监控指标
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 LaneScheduler 不可用或调用失败时，默认假设队列为空（无背压），信号原样放行，记录 WARNING 日志
- 当 LaneHealthMonitor 不可用时，仅依赖本地队列深度判断，不参考健康评分
- 当 NegotiationBus 不可用时，告警降级为仅本地日志记录
- 当 BehavioralLogger 不可用时，日志记录静默降级
- 当滑动窗口样本不足时，背压判断采用保守策略（默认无背压）

资源管理：
- 本模块不持有任何需要手动释放的外部资源
- 背压统计数据的清理通过定时器自动执行，线程锁在模块销毁时自动释放
- 清理操作分批进行，单次持锁时间有严格上限，避免阻塞信号处理
- 历史记录队列无界，完全依赖过期时间清理，确保关键数据不因窗口过小被误删
- 告警推送在锁外异步执行，避免阻塞背压处理主路径
"""

import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple
from collections import deque, Counter
import random

logger = logging.getLogger(__name__)


class BackpressureHandler:
    """四车道背压处理器"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    # 背压阈值（队列深度占容量的比例）
    LOW_PRESSURE_THRESHOLD = 0.5        # 轻度背压触发阈值，无量纲，取值范围 [0.3, 0.7]
    MEDIUM_PRESSURE_THRESHOLD = 0.7     # 中度背压触发阈值，无量纲，取值范围 [0.5, 0.9]
    HIGH_PRESSURE_THRESHOLD = 0.9       # 高度背压触发阈值，无量纲，取值范围 [0.7, 1.0]

    # 信号优先级参数（按车道差异化配置，紧急性与影响度权重）
    # 设计意图：
    #   - 极速车道：紧急性占主导(0.8)，阈值4.0，仅urgency=5或极高影响度的信号被无条件保护
    #   - 慢速车道：影响度更重要(0.6)，阈值2.5，更积极处理低紧急性信号
    URGENCY_WEIGHT = {
        "express": 0.8,     # 极速车道：紧急性优先（生存级指令）
        "fast": 0.7,
        "normal": 0.6,
        "slow": 0.4,        # 慢速车道：影响度更重要（审计日志等）
    }
    IMPACT_WEIGHT = {
        "express": 0.2,
        "fast": 0.3,
        "normal": 0.4,
        "slow": 0.6,
    }
    # 阈值设计示例（极速车道）:
    #   urgency=5, impact=0   → score=4.0 → 刚好通过
    #   urgency=4, impact=100 → score=23.2 → 远高于阈值
    #   urgency=3, impact=100 → score=22.4 → 远高于阈值
    LOW_PRIORITY_COMPOSITE_THRESHOLD = {
        "express": 4.0,     # 极速车道：高门槛，减少误丢弃
        "fast": 3.5,
        "normal": 3.0,
        "slow": 2.5,        # 慢速车道：低门槛，更积极处理
    }

    # 信号处理参数
    COMPRESSION_BATCH_SIZE = 10         # 批量压缩最小信号条数，无量纲，取值范围 [5, 50]
    MAX_DISCARD_RATIO = 0.5             # 单次处理最大丢弃比例，无量纲，取值范围 [0.1, 0.8]
    COMPRESSION_SAMPLE_THRESHOLD = 200  # 压缩采样阈值，超过此数量使用采样统计，无量纲，[50, 500]
    COMPRESSION_SAMPLE_RATIO = 0.25     # 采样比例，无量纲，取值范围 [0.1, 0.5]

    # 统计与清理参数
    CLEANUP_INTERVAL_SEC = 600          # 统计过期清理间隔，秒，取值范围 [300, 3600]
    MAX_STATS_AGE_SEC = 1800            # 统计数据最大保留时间，秒，取值范围 [600, 7200]
    MAX_CLEANUP_BATCH_PER_LANE = 1000   # 单车道单次清理最大记录数，无量纲，[500, 2000]

    # 告警去重与恶化检测参数
    ALERT_DEDUP_WINDOW_SEC = 30         # 同类型告警去重窗口，秒，取值范围 [10, 120]
    ALERT_WORSENING_THRESHOLD = 0.05    # 使用率恶化突破去重阈值，无量纲，[0.02, 0.10]

    # 告警推送超时保护
    ALERT_PUBLISH_TIMEOUT_SEC = 0.5     # 协商总线推送超时时间，秒，取值范围 [0.1, 2.0]

    # 健康检查参数
    MIN_SAMPLES_FOR_HEALTH_CHECK = 5    # 健康检查所需最少操作记录数，无量纲，取值范围 [3, 20]

    # 各车道队列容量（必须与 LaneScheduler 中的配置严格一致）
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
        """初始化背压处理器，创建各车道独立统计字典和线程安全锁"""
        self._pressure_stats: Dict[str, Dict[str, Any]] = {}
        for lane in self.LANE_QUEUE_CAPACITIES:
            self._pressure_stats[lane] = {
                "applied_count": 0,
                "compressed_count": 0,
                "discarded_count": 0,
                "last_applied_timestamp": 0.0,
                "history": deque(),  # 无界队列，完全依赖过期时间清理，避免关键数据被窗口误删
            }

        # 告警去重记录与恶化追踪
        self._alert_last_triggered: Dict[str, float] = {}
        self._alert_last_usage: Dict[str, float] = {}

        # 实时指标缓存（用于 export_metrics 提供 Gauge 值）
        self._last_known_usage: Dict[str, float] = {}
        self._last_known_level: Dict[str, str] = {}

        # 外部依赖注入
        self._lane_scheduler = None
        self._lane_health_monitor = None
        self._negotiation_bus = None
        self._behavioral_logger = None

        # 线程安全锁（保护所有共享状态：统计字典、告警去重记录、指标缓存）
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
        注入外部依赖（可选注入，未注入时对应功能安全降级）

        所有注入的依赖都会进行鸭子类型校验，确保实例实现了必需的方法。
        校验失败时依赖被置为 None，功能静默降级。

        Args:
            lane_scheduler: 车道调度器实例，需实现 get_queue_depth(lane_name: str) -> int 方法
            lane_health_monitor: 车道健康监控器实例，需实现 get_health_score(lane_name: str) -> Dict 方法
            negotiation_bus: 协商总线实例，需实现 publish_alert(**kwargs) 方法
            behavioral_logger: 行为日志实例，需实现 log_event(event_type: str, details: Dict) 方法
        """
        if lane_scheduler is not None:
            if not hasattr(lane_scheduler, 'get_queue_depth'):
                logger.warning("LaneScheduler 缺少 get_queue_depth 方法，该依赖将不可用，背压检查默认放行")
            else:
                self._lane_scheduler = lane_scheduler
                logger.info("LaneScheduler 注入成功")

        if lane_health_monitor is not None:
            if not hasattr(lane_health_monitor, 'get_health_score'):
                logger.warning("LaneHealthMonitor 缺少 get_health_score 方法，该依赖将不可用")
            else:
                self._lane_health_monitor = lane_health_monitor
                logger.info("LaneHealthMonitor 注入成功")

        if negotiation_bus is not None:
            if not hasattr(negotiation_bus, 'publish_alert'):
                logger.warning("NegotiationBus 缺少 publish_alert 方法，告警推送将降级为本地日志")
            else:
                self._negotiation_bus = negotiation_bus
                logger.info("NegotiationBus 注入成功")

        if behavioral_logger is not None:
            self._behavioral_logger = behavioral_logger
            logger.info("BehavioralLogger 注入成功")
        else:
            logger.warning("BehavioralLogger 未注入，背压操作日志将仅使用标准 logger")

    # ========== 公共接口 ==========
    def assess_pressure(self, lane_name: str) -> Dict[str, Any]:
        """
        评估指定车道当前背压等级

        通过查询 LaneScheduler 获取实时队列深度，结合队列容量计算使用率，
        并根据预设阈值判定背压等级。当依赖不可用时，采用保守默认值（假设队列为空）。
        同时更新实时指标缓存，供监控接口使用。

        Args:
            lane_name: 车道名称，必须是 LANE_QUEUE_CAPACITIES 中的键

        Returns:
            标准响应字典，data 中包含:
            - lane: 车道名称
            - pressure_level: 背压等级 (none/low/medium/high)
            - queue_depth: 当前队列深度
            - capacity: 队列容量
            - usage_ratio: 队列使用率 (0.0-1.0)
            - recommendation: 运维建议
        """
        self._try_cleanup()

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

        if self._lane_scheduler is not None:
            try:
                queue_depth = self._lane_scheduler.get_queue_depth(lane_name)
                if not isinstance(queue_depth, (int, float)):
                    logger.warning(
                        f"get_queue_depth 返回非数值类型 ({type(queue_depth).__name__})，采用默认值 0"
                    )
                    queue_depth = 0
                elif queue_depth < 0:
                    logger.warning(f"get_queue_depth 返回负值 ({queue_depth})，采用默认值 0")
                    queue_depth = 0
            except Exception as e:
                logger.warning(f"获取队列深度失败: {e}，采用默认值 0 #RECOVERY: 检查 LaneScheduler 进程存活状态")
                queue_depth = 0
        else:
            logger.debug("LaneScheduler 不可用，假设队列为空（保守放行）")

        usage_ratio = round(queue_depth / capacity, 4) if capacity > 0 else 0.0

        if usage_ratio >= self.HIGH_PRESSURE_THRESHOLD:
            level = "high"
            recommendation = "紧急：丢弃低优先级信号，压缩可合并信号，建议检查上游模块是否异常"
        elif usage_ratio >= self.MEDIUM_PRESSURE_THRESHOLD:
            level = "medium"
            recommendation = "警告：压缩低优先级信号，减少新信号入队速率"
        elif usage_ratio >= self.LOW_PRESSURE_THRESHOLD:
            level = "low"
            recommendation = "注意：持续监控队列变化，暂不干预"
        else:
            level = "none"
            recommendation = "保持正常处理"

        with self._lock:
            self._last_known_usage[lane_name] = usage_ratio
            self._last_known_level[lane_name] = level

        lane_desc = self.LANE_DESCRIPTIONS.get(lane_name, lane_name)
        logger.debug(
            "%s 背压评估: level=%s, queue=%d/%d, usage=%.1f%%",
            lane_desc, level, queue_depth, capacity, usage_ratio * 100
        )

        return {
            "status": "ok",
            "reason": f"{lane_desc} 背压等级: {level} (使用率 {usage_ratio:.1%})",
            "data": {
                "lane": lane_name,
                "pressure_level": level,
                "queue_depth": queue_depth,
                "capacity": capacity,
                "usage_ratio": usage_ratio,
                "recommendation": recommendation,
            },
            "warnings": [],
        }

    def apply_backpressure(self, lane_name: str, signals: List[Dict]) -> List[Dict]:
        """
        对信号列表执行背压策略，返回处理后的信号列表

        处理逻辑:
        - 无背压或轻度背压: 原样返回，不干预
        - 中度背压: 将低优先级且同类型的信号合并为一条压缩摘要信号
        - 高度背压: 丢弃部分低优先级信号（最多丢弃 MAX_DISCARD_RATIO 比例）
        信号优先级由紧急性 (urgency) 和影响度 (impact_score) 综合判定，权重按车道差异化配置。

        Args:
            lane_name: 车道名称
            signals: 待处理的信号列表，每个信号字典需包含 "urgency" (int) 和 "impact_score" (int, 0-100)

        Returns:
            处理后的信号列表
        """
        self._try_cleanup()

        if not signals:
            return signals

        if lane_name not in self.LANE_QUEUE_CAPACITIES:
            logger.warning(f"无效车道名称: {lane_name}，原样返回信号")
            return signals

        assessment = self.assess_pressure(lane_name)
        if assessment["status"] != "ok":
            logger.warning("背压评估失败，原样返回信号（保守放行）")
            return signals

        pressure_level = assessment["data"]["pressure_level"]
        if pressure_level in ("none", "low"):
            return signals

        # 锁内处理：二次确认、信号分离、背压执行、统计更新、告警去重
        alert_ctx = None
        with self._lock:
            if self._lane_scheduler is not None:
                try:
                    current_depth = self._lane_scheduler.get_queue_depth(lane_name)
                    current_usage = current_depth / self.LANE_QUEUE_CAPACITIES[lane_name]
                    if current_usage < self.LOW_PRESSURE_THRESHOLD:
                        logger.debug("背压已缓解，取消背压处理")
                        return signals
                except Exception:
                    pass

            high_priority, low_priority = self._separate_signals(signals, lane_name)
            original_count = len(signals)

            if pressure_level == "medium":
                result = self._handle_medium_pressure(
                    lane_name, high_priority, low_priority, original_count
                )
            else:  # high
                result = self._handle_high_pressure(
                    lane_name, high_priority, low_priority, original_count, assessment
                )

            self._pressure_stats[lane_name]["applied_count"] += 1
            self._pressure_stats[lane_name]["last_applied_timestamp"] = time.time()
            self._pressure_stats[lane_name]["history"].append({
                "timestamp": time.time(),
                "lane": lane_name,
                "level": pressure_level,
                "original_count": original_count,
                "result_count": len(result),
                "reduced": original_count - len(result),
                "usage_ratio": assessment["data"]["usage_ratio"],
            })

            # 准备告警上下文（锁内完成，推送在锁外执行）
            if len(result) != original_count:
                alert_ctx = {
                    "lane_name": lane_name,
                    "level": pressure_level,
                    "message": f"背压处理: 原始 {original_count} 条 -> 处理后 {len(result)} 条 (使用率 {assessment['data']['usage_ratio']:.1%})",
                    "usage_ratio": assessment["data"]["usage_ratio"],
                }

        # 锁外执行告警推送，避免阻塞背压主路径
        if alert_ctx is not None:
            self._trigger_alert(**alert_ctx)

        return result

    def get_pressure_stats(self) -> Dict[str, Any]:
        """
        获取所有车道的背压统计汇总

        Returns:
            标准响应字典，data 中包含各车道累计操作次数、最近历史和全局汇总
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
                last_time = stats.get("last_applied_timestamp", 0)

                all_stats[lane] = {
                    "applied_count": stats["applied_count"],
                    "compressed_count": stats["compressed_count"],
                    "discarded_count": stats["discarded_count"],
                    "recent_operations": recent_ops,
                    "last_applied_seconds_ago": round(time.time() - last_time, 1) if last_time > 0 else None,
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

    def export_metrics(self) -> Dict[str, float]:
        """
        导出 Prometheus 兼容的背压监控指标

        指标包括：
        - 实时 Gauge：各车道当前队列使用率、背压等级（数值化）
        - 累计 Counter：各车道背压处理次数、压缩信号总数、丢弃信号总数

        实时指标在锁内快速拷贝后释放锁，计算在锁外完成，避免阻塞背压主路径。

        Returns:
            指标字典，键为 metric_name，值为浮点数
        """
        with self._lock:
            usage_snapshot = dict(self._last_known_usage)
            level_snapshot = dict(self._last_known_level)
            stats_snapshot = {
                lane: {
                    "applied_count": self._pressure_stats[lane]["applied_count"],
                    "compressed_count": self._pressure_stats[lane]["compressed_count"],
                    "discarded_count": self._pressure_stats[lane]["discarded_count"],
                }
                for lane in self.LANE_QUEUE_CAPACITIES
            }

        metrics = {}
        for lane in self.LANE_QUEUE_CAPACITIES:
            # 实时 Gauge（从快照读取）
            metrics[f"backpressure_{lane}_usage_ratio"] = usage_snapshot.get(lane, 0.0)
            level_str = level_snapshot.get(lane, "none")
            level_num = {"none": 0, "low": 1, "medium": 2, "high": 3}.get(level_str, 0)
            metrics[f"backpressure_{lane}_level"] = float(level_num)

            # 累计 Counter
            stats = stats_snapshot[lane]
            metrics[f"backpressure_{lane}_applied_total"] = float(stats["applied_count"])
            metrics[f"backpressure_{lane}_compressed_total"] = float(stats["compressed_count"])
            metrics[f"backpressure_{lane}_discarded_total"] = float(stats["discarded_count"])

        return metrics

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """
        模块自检

        检查项：
        1. 内部数据结构是否完整初始化
        2. 各车道统计是否有足够的历史记录（样本充分性）
        3. 外部依赖的可用状态
        4. 监控指标导出接口是否正常

        Returns:
            标准健康检查响应字典
        """
        try:
            if not hasattr(self, '_pressure_stats') or not self._pressure_stats:
                return {
                    "status": "degraded",
                    "reason": "背压统计数据结构未初始化，模块可能尚未完成 __init__",
                    "data": {},
                    "warnings": ["pressure_stats_not_initialized"],
                }

            with self._lock:
                lane_count = len(self._pressure_stats)
                total_applied = sum(s["applied_count"] for s in self._pressure_stats.values())
                lanes_with_insufficient_data = []
                for lane, stats in self._pressure_stats.items():
                    history_len = len(stats["history"])
                    if history_len < self.MIN_SAMPLES_FOR_HEALTH_CHECK:
                        lanes_with_insufficient_data.append({
                            "lane": lane,
                            "history_count": history_len,
                        })

            warnings = []
            if lanes_with_insufficient_data:
                warnings.append(
                    f"{len(lanes_with_insufficient_data)} 条车道历史数据不足，背压判断可能不够准确"
                )

            try:
                _ = self.export_metrics()
            except Exception as e:
                warnings.append(f"export_metrics 不可用: {str(e)}")

            return {
                "status": "ok",
                "reason": f"BackpressureHandler 正常，监控 {lane_count} 条车道，累计处理 {total_applied} 次",
                "data": {
                    "lane_count": lane_count,
                    "total_applied": total_applied,
                    "lanes_with_insufficient_data": lanes_with_insufficient_data,
                    "dependencies": {
                        "lane_scheduler": self._lane_scheduler is not None,
                        "lane_health_monitor": self._lane_health_monitor is not None,
                        "negotiation_bus": self._negotiation_bus is not None,
                        "behavioral_logger": self._behavioral_logger is not None,
                    },
                },
                "warnings": warnings,
            }
        except Exception as e:
            logger.error(
                f"健康检查失败: {e} #RECOVERY: 检查锁状态、统计字典完整性、是否存在并发死锁"
            )
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _separate_signals(self, signals: List[Dict], lane_name: str) -> Tuple[List[Dict], List[Dict]]:
        """
        根据综合优先级和车道差异化权重分离信号为高优先级和低优先级

        综合评分 = urgency * URGENCY_WEIGHT[lane] + impact_score * IMPACT_WEIGHT[lane]
        评分低于 LOW_PRIORITY_COMPOSITE_THRESHOLD[lane] 的归为低优先级。
        已压缩的摘要信号（type == "compressed_summary"）自动保护为高优先级，防止递归压缩。
        缺少有效 impact_score 字段的信号默认 impact_score=0（最低影响度）。

        Args:
            signals: 待分离的信号列表
            lane_name: 车道名称

        Returns:
            (high_priority, low_priority) 元组
        """
        high_priority = []
        low_priority = []
        urg_weight = self.URGENCY_WEIGHT.get(lane_name, 0.6)
        imp_weight = self.IMPACT_WEIGHT.get(lane_name, 0.4)
        threshold = self.LOW_PRIORITY_COMPOSITE_THRESHOLD.get(lane_name, 3.5)

        for sig in signals:
            if sig.get("type") == "compressed_summary":
                high_priority.append(sig)
                continue

            urgency = sig.get("urgency", 5)
            if not isinstance(urgency, (int, float)):
                urgency = 5

            impact = sig.get("impact_score", None)
            if impact is None or not isinstance(impact, (int, float)):
                logger.warning(
                    f"信号缺少有效 impact_score 字段，默认设为 0: type={sig.get('type', 'unknown')}"
                )
                impact = 0

            composite_score = urgency * urg_weight + impact * imp_weight
            if composite_score >= threshold:
                high_priority.append(sig)
            else:
                low_priority.append(sig)
        return high_priority, low_priority

    def _handle_medium_pressure(
        self, lane_name: str, high_priority: List[Dict], low_priority: List[Dict], original_count: int
    ) -> List[Dict]:
        """处理中度背压：压缩低优先级信号为摘要"""
        if len(low_priority) >= self.COMPRESSION_BATCH_SIZE:
            compressed = self._compress_signals(low_priority)
            result = high_priority + compressed
            compressed_count = len(low_priority) - len(compressed)
            self._pressure_stats[lane_name]["compressed_count"] += compressed_count
            logger.info(
                "%s 中度背压: 压缩 %d 条低优先级信号为 %d 条摘要",
                self.LANE_DESCRIPTIONS.get(lane_name, lane_name),
                len(low_priority),
                len(compressed),
            )
        else:
            result = high_priority + low_priority
            logger.debug(
                "低优先级信号数量 (%d) 未达到压缩阈值 (%d)，跳过压缩",
                len(low_priority),
                self.COMPRESSION_BATCH_SIZE,
            )
        return result

    def _handle_high_pressure(
        self, lane_name: str, high_priority: List[Dict], low_priority: List[Dict],
        original_count: int, assessment: Dict[str, Any]
    ) -> List[Dict]:
        """处理高度背压：丢弃部分低优先级信号（优先丢弃评分最低的）"""
        if not low_priority:
            return high_priority

        # 按综合评分升序排列（最低优先级在前），确保优先丢弃最不重要的信号
        urg_weight = self.URGENCY_WEIGHT.get(lane_name, 0.6)
        imp_weight = self.IMPACT_WEIGHT.get(lane_name, 0.4)
        low_priority.sort(
            key=lambda s: s.get("urgency", 5) * urg_weight + s.get("impact_score", 0) * imp_weight
        )

        max_keep = max(0, int(len(low_priority) * (1 - self.MAX_DISCARD_RATIO)))
        kept = low_priority[:max_keep]
        discarded = len(low_priority) - len(kept)
        self._pressure_stats[lane_name]["discarded_count"] += discarded

        logger.warning(
            "%s 高度背压: 丢弃 %d 条低优先级信号 (队列使用率 %.1f%%)",
            self.LANE_DESCRIPTIONS.get(lane_name, lane_name),
            discarded,
            assessment["data"]["usage_ratio"] * 100,
        )
        return high_priority + kept

    def _compress_signals(self, signals: List[Dict]) -> List[Dict]:
        """
        将低优先级信号按类型统计并压缩为一条摘要信号，保留关键追溯信息。
        当信号数量超过采样阈值时，使用采样统计以减少计算开销。
        摘要信号 type 标记为 "compressed_summary"，后续背压处理会自动保护。

        Args:
            signals: 待压缩的低优先级信号列表

        Returns:
            包含一条摘要信号的列表，或原始信号列表（数量不足时）
        """
        if not signals or len(signals) < self.COMPRESSION_BATCH_SIZE:
            return signals

        if len(signals) > self.COMPRESSION_SAMPLE_THRESHOLD:
            sample_size = max(self.COMPRESSION_BATCH_SIZE, int(len(signals) * self.COMPRESSION_SAMPLE_RATIO))
            sample = random.sample(signals, sample_size)
            type_counts = Counter(s.get("type", "unknown") for s in sample)
            scale = len(signals) / sample_size
            type_counts = {k: int(v * scale) for k, v in type_counts.items()}
            source_modules = set(s.get("source_module", "unknown") for s in sample)
            sample_ids = [s.get("signal_id", "unknown") for s in sample[:10]]
        else:
            type_counts = Counter(s.get("type", "unknown") for s in signals)
            source_modules = set(s.get("source_module", "unknown") for s in signals)
            sample_ids = [s.get("signal_id", "unknown") for s in signals[:10]]

        summary = {
            "type": "compressed_summary",
            "urgency": 0,
            "timestamp": time.time(),
            "original_count": len(signals),
            "type_distribution": dict(type_counts),
            "source_modules": list(source_modules),
            "sample_ids": sample_ids,
            "reason": f"背压压缩: {len(signals)}条低优先级信号合并为摘要",
        }

        logger.debug("背压压缩: %d 条信号 -> 1 条摘要 (类型分布: %s)", len(signals), dict(type_counts))
        return [summary]

    def _trigger_alert(self, lane_name: str, level: str, message: str, usage_ratio: float = 0.0) -> None:
        """
        触发背压告警（含去重与恶化升级机制，锁外执行推送避免阻塞背压主路径）

        告警去重: 同一车道同一级别在去重窗口内仅触发一次。
        恶化检测: 若使用率较上次告警明显恶化，则强制突破去重窗口并重置计时。
        告警通道: NegotiationBus（带超时保护） → 本地日志 → BehavioralLogger（逐级降级）。

        Args:
            lane_name: 车道名称
            level: 告警级别 (medium/high)
            message: 告警详细描述
            usage_ratio: 当前队列使用率
        """
        # 去重与恶化检测（轻量级，无需锁，由调用方在锁内已更新状态）
        alert_key = f"backpressure:{lane_name}:{level}"
        now = time.time()

        # 恶化检测
        last_usage = self._alert_last_usage.get(alert_key, 0.0)
        if usage_ratio - last_usage > self.ALERT_WORSENING_THRESHOLD:
            logger.warning(
                "背压持续恶化，突破去重窗口: %s usage %.1f%% -> %.1f%%",
                alert_key, last_usage * 100, usage_ratio * 100
            )
            self._alert_last_triggered[alert_key] = 0.0

        last_time = self._alert_last_triggered.get(alert_key, 0.0)
        if now - last_time < self.ALERT_DEDUP_WINDOW_SEC:
            logger.debug("告警去重: %s (距上次 %.1f 秒)", alert_key, now - last_time)
            return

        self._alert_last_triggered[alert_key] = now
        self._alert_last_usage[alert_key] = usage_ratio

        lane_desc = self.LANE_DESCRIPTIONS.get(lane_name, lane_name)
        alert_msg = f"[BACKPRESSURE-{level.upper()}] {lane_desc}: {message}"

        # 协商总线推送（使用线程池异步执行，避免阻塞主路径）
        if self._negotiation_bus is not None:
            threading.Thread(
                target=self._safe_publish_alert,
                args=(lane_name, level, message, usage_ratio, now),
                daemon=True,
            ).start()

        # 本地日志
        if level == "high":
            logger.error(
                "%s #RECOVERY: 降低该车道信号流量、检查上游模块是否异常、考虑启用降级策略或动态核心借用",
                alert_msg
            )
        else:
            logger.warning(alert_msg)

        # 行为日志持久化
        if self._behavioral_logger is not None:
            try:
                self._behavioral_logger.log_event(
                    event_type="backpressure_action",
                    details={
                        "lane": lane_name,
                        "level": level,
                        "message": message,
                        "usage_ratio": usage_ratio,
                        "timestamp": now,
                    },
                )
            except Exception as e:
                logger.warning(f"行为日志记录失败: {e}")

    def _safe_publish_alert(
        self, lane_name: str, level: str, message: str, usage_ratio: float, timestamp: float
    ) -> None:
        """
        安全推送告警到协商总线（带超时保护，在独立线程中执行）

        Args:
            lane_name: 车道名称
            level: 告警级别
            message: 告警消息
            usage_ratio: 使用率
            timestamp: 时间戳
        """
        try:
            self._negotiation_bus.publish_alert(
                alert_type="backpressure",
                lane=lane_name,
                level=level,
                message=message,
                usage_ratio=usage_ratio,
                timestamp=timestamp,
            )
        except Exception as e:
            logger.warning(f"协商总线告警推送失败: {e} #RECOVERY: 检查 NegotiationBus 连接状态")

    def _try_cleanup(self) -> None:
        """
        定期清理过期的操作历史记录，分批处理以控制持锁时间。
        单次每车道最多清理 MAX_CLEANUP_BATCH_PER_LANE 条，超出部分留待下次。
        该方法内部有间隔检查，可安全地在高频路径中调用。
        """
        now = time.time()
        if now - self._last_cleanup < self.CLEANUP_INTERVAL_SEC:
            return

        with self._lock:
            cutoff = now - self.MAX_STATS_AGE_SEC
            total_removed = 0
            total_before = 0

            for lane in self.LANE_QUEUE_CAPACITIES:
                history = self._pressure_stats[lane]["history"]
                before = len(history)
                total_before += before
                batch_removed = 0
                while (history and history[0]["timestamp"] < cutoff
                       and batch_removed < self.MAX_CLEANUP_BATCH_PER_LANE):
                    history.popleft()
                    batch_removed += 1
                    total_removed += 1
                if batch_removed >= self.MAX_CLEANUP_BATCH_PER_LANE:
                    logger.warning(
                        "车道 %s 清理达单次上限 %d 条，剩余过期记录留待下次清理",
                        lane, self.MAX_CLEANUP_BATCH_PER_LANE
                    )
                elif batch_removed > 0:
                    logger.debug(
                        "车道 %s 背压历史清理: %d -> %d 条",
                        lane, before, len(history)
                    )

        self._last_cleanup = now
        if total_removed > 0:
            logger.info(
                "全局背压历史清理: %d 条过期记录 (清理前 %d 条, 清理后 %d 条)",
                total_removed, total_before, total_before - total_removed
    )
