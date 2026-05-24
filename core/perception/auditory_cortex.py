"""
火种系统 · 听觉皮层 (AuditoryCortex) — 深度精细化

核心职责：
1. 监听并解析外部宏观经济事件，输出事件等级、距离下一事件的时间以及预期冲击倍数。
2. 解析多语种社交媒体情绪数据，输出标准化的情绪得分(-1到1)与情绪变化动量，同时提供情绪稳定性评估。

外部依赖（真实模块接口）：
- (可选) openclaw.gateway.Gateway : 提供 get_macro_events() 和 get_sentiment_data() 接口，用于获取外部数据。
- 若无外部依赖注入，则所有数据由调用方通过 listen() 方法的参数传入。

接口契约：
- listen(macro_events: Optional[List[Dict[str, Any]]] = None,
         sentiment_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]
  输出字典固定包含 "status" (str), "macro_alert_level" (str), "time_to_next_event_sec" (float),
  "sentiment_score" (float), "sentiment_momentum" (float), "sentiment_stability" (float),
  "reason" (str), "warnings" (List[str])
- health_check() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "message" (str)

异常与降级：
- 当外部数据服务不可用且调用方未提供参数时，返回所有字段为安全中性默认值，状态标记为 "degraded"。
- 所有解析异常被内部捕获，不向外抛出，确保调用方稳定。

资源管理：
- 本模块为有状态工具，仅保留最近一次情绪得分和时间戳用于计算动量，无外部资源占用，无需显式释放。
"""

import time
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class AuditoryCortex:
    """听觉皮层：宏观事件监听与多语种情绪解析"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_MACRO_LEVEL = "none"           # 默认无宏观事件
    DEFAULT_SENTIMENT_SCORE = 0.0          # 默认情绪中性，[-1.0, 1.0]
    DEFAULT_SENTIMENT_MOMENTUM = 0.0       # 默认情绪动量，[-1.0, 1.0]
    DEFAULT_SENTIMENT_STABILITY = 1.0      # 情绪稳定性，值越小波动越大，[0.0, 1.0]
    DEFAULT_TIME_TO_EVENT = 999999.0       # 默认距离下一事件的时间，秒，极大值表示无事件
    # 事件等级阈值（基于历史波动率冲击倍数）
    EVENT_LEVEL1_MULTIPLIER = 3.0           # 一级事件（如非农、利率决议），波动率冲击 > 3倍
    EVENT_LEVEL2_MULTIPLIER = 2.0           # 二级事件（如CPI），波动率冲击 > 2倍
    EVENT_LEVEL3_MULTIPLIER = 1.5           # 三级事件，波动率冲击 > 1.5倍
    # 情绪动量计算参数
    SENTIMENT_MOMENTUM_WINDOW = 300.0       # 动量计算时间窗口，秒，[60, 600]
    SENTIMENT_STABILITY_WINDOW = 600.0      # 稳定性计算窗口，秒
    MAX_SENTIMENT_HISTORY = 120             # 历史情绪记录最大保留数

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """初始化听觉皮层，可接受配置字典覆盖默认阈值。"""
        self._gateway: Optional[Any] = None
        self._config = config or {}
        self._level1 = float(self._config.get("event_level1_multiplier", self.EVENT_LEVEL1_MULTIPLIER))
        self._level2 = float(self._config.get("event_level2_multiplier", self.EVENT_LEVEL2_MULTIPLIER))
        self._level3 = float(self._config.get("event_level3_multiplier", self.EVENT_LEVEL3_MULTIPLIER))
        self._momentum_window = float(self._config.get("sentiment_momentum_window", self.SENTIMENT_MOMENTUM_WINDOW))
        # 情绪历史（时间戳, 得分）
        self._sentiment_history: List[tuple] = []
        logger.info(f"AuditoryCortex 初始化完成，一级事件阈值: {self._level1}, 情绪动量窗口: {self._momentum_window}s")

    # ────────────────────────── 依赖注入 ──────────────────────────
    def inject_dependencies(self, gateway: Optional[Any] = None) -> None:
        """注入外部数据网关（可选），并进行鸭子类型校验。"""
        self._gateway = gateway
        if gateway is not None:
            if not hasattr(gateway, "get_macro_events"):
                logger.warning("网关缺少 get_macro_events 方法，宏观事件监听将降级")
            if not hasattr(gateway, "get_sentiment_data"):
                logger.warning("网关缺少 get_sentiment_data 方法，情绪解析将降级")

    # ────────────────────────── 公共接口 ──────────────────────────
    def listen(
        self,
        macro_events: Optional[List[Dict[str, Any]]] = None,
        sentiment_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        执行一次听觉感知，融合宏观事件与情绪数据。
        
        Args:
            macro_events: 可选，外部传入的宏观事件列表，每项包含 'name', 'time', 'impact_multiplier'。
            sentiment_data: 可选，外部传入的情绪数据，包含 'score' (float) 和 'timestamp' (float)。
        
        Returns:
            标准化听觉快照字典
        """
        warnings: List[str] = []
        reason_parts: List[str] = []

        # 1. 获取宏观事件数据
        events = macro_events
        if events is None and self._gateway:
            try:
                events = self._gateway.get_macro_events()
            except Exception as e:
                logger.warning(f"获取宏观事件失败: {e}")
                warnings.append("宏观事件数据源不可用，使用降级默认值")

        # 2. 分析宏观事件
        alert_level, time_to_next = self._analyze_macro_events(events, warnings)
        reason_parts.append(f"宏观事件等级: {alert_level}")

        # 3. 获取情绪数据
        sent_data = sentiment_data
        if sent_data is None and self._gateway:
            try:
                sent_data = self._gateway.get_sentiment_data()
            except Exception as e:
                logger.warning(f"获取情绪数据失败: {e}")
                warnings.append("情绪数据源不可用，使用降级默认值")

        # 4. 解析情绪
        score, momentum, stability = self._analyze_sentiment(sent_data, warnings)
        reason_parts.append(f"情绪得分: {score:.2f}, 动量: {momentum:.2f}, 稳定性: {stability:.2f}")

        reason = "听觉感知完成: " + ", ".join(reason_parts) if reason_parts else "无有效外部数据"

        return {
            "status": "ok" if not warnings else "warning",
            "macro_alert_level": alert_level,
            "time_to_next_event_sec": time_to_next,
            "sentiment_score": score,
            "sentiment_momentum": momentum,
            "sentiment_stability": stability,
            "reason": reason,
            "warnings": warnings,
        }

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检：使用模拟数据测试分析逻辑。"""
        try:
            instance = cls()
            now = time.time()
            # 模拟一级事件 + 情绪数据
            events = [
                {"name": "FOMC", "time": now + 600, "impact_multiplier": 3.5}
            ]
            sent = {"score": -0.6, "timestamp": now}
            result = instance.listen(events, sent)
            if result["macro_alert_level"] != "level1":
                return {"status": "error", "message": "一级事件判定失败"}
            if not (-1.0 <= result["sentiment_score"] <= 1.0):
                return {"status": "error", "message": "情绪得分超出范围"}

            # 模拟空数据降级
            degraded = instance.listen(None, None)
            if degraded["macro_alert_level"] != cls.DEFAULT_MACRO_LEVEL:
                return {"status": "error", "message": "降级时宏观事件等级错误"}
            if degraded["sentiment_score"] != cls.DEFAULT_SENTIMENT_SCORE:
                return {"status": "error", "message": "降级时情绪得分错误"}

            # 测试动量计算（连续两次不同得分）
            instance.listen(None, {"score": 0.2, "timestamp": now})
            time.sleep(0.1)  # 模拟微小时间差
            momentum_result = instance.listen(None, {"score": 0.5, "timestamp": now + 60})
            if momentum_result["sentiment_momentum"] <= 0:
                return {"status": "error", "message": "情绪动量应为正"}

            return {"status": "ok", "message": "所有测试通过"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _analyze_macro_events(
        self, events: Optional[List[Dict[str, Any]]], warnings: List[str]
    ) -> tuple:
        """分析宏观事件列表，返回当前最高等级和距离最近事件的时间。"""
        if not events:
            return self.DEFAULT_MACRO_LEVEL, self.DEFAULT_TIME_TO_EVENT

        now = time.time()
        max_impact = 0.0
        min_time_to_event = self.DEFAULT_TIME_TO_EVENT

        for ev in events:
            try:
                impact = float(ev.get("impact_multiplier", 1.0))
                ev_time = float(ev.get("time", now + self.DEFAULT_TIME_TO_EVENT))
                if ev_time <= now:
                    continue  # 已过期的事件跳过
                max_impact = max(max_impact, impact)
                time_diff = ev_time - now
                if time_diff < min_time_to_event:
                    min_time_to_event = time_diff
            except Exception as e:
                logger.debug(f"解析事件条目异常: {e}")
                warnings.append(f"无效的事件条目: {str(ev)[:50]}")
                continue

        # 根据冲击倍数分级
        if max_impact >= self._level1:
            level = "level1"
        elif max_impact >= self._level2:
            level = "level2"
        elif max_impact >= self._level3:
            level = "level3"
        else:
            level = "none"

        return level, max(0.0, min_time_to_event)

    def _analyze_sentiment(
        self, data: Optional[Dict[str, Any]], warnings: List[str]
    ) -> tuple:
        """解析情绪数据，返回当前得分、动量和稳定性。"""
        if not data or "score" not in data:
            return self.DEFAULT_SENTIMENT_SCORE, self.DEFAULT_SENTIMENT_MOMENTUM, self.DEFAULT_SENTIMENT_STABILITY

        try:
            score = float(data["score"])
            score = max(-1.0, min(1.0, score))  # 钳制到 [-1, 1]
            now = float(data.get("timestamp", time.time()))
        except (ValueError, TypeError) as e:
            logger.debug(f"情绪得分解析异常: {e}")
            warnings.append(f"无效的情绪得分: {data.get('score')}")
            return self.DEFAULT_SENTIMENT_SCORE, self.DEFAULT_SENTIMENT_MOMENTUM, self.DEFAULT_SENTIMENT_STABILITY

        # 记录历史
        self._sentiment_history.append((now, score))
        if len(self._sentiment_history) > self.MAX_SENTIMENT_HISTORY:
            self._sentiment_history = self._sentiment_history[-self.MAX_SENTIMENT_HISTORY:]

        # 计算动量（与上一有效记录的差异）
        momentum = 0.0
        if len(self._sentiment_history) >= 2:
            prev_time, prev_score = self._sentiment_history[-2]
            time_delta = now - prev_time
            if time_delta > 0 and time_delta < self._momentum_window * 2:
                momentum = (score - prev_score) / time_delta * 60.0  # 标准化到每分钟变化
            momentum = max(-1.0, min(1.0, momentum))

        # 计算稳定性（窗口内得分的方差）
        stability = self._calc_stability(now)
        return score, momentum, stability

    def _calc_stability(self, now: float) -> float:
        """计算情绪的稳定性（窗口内得分的变异系数反比）。"""
        cutoff = now - self.SENTIMENT_STABILITY_WINDOW
        recent = [s for t, s in self._sentiment_history if t >= cutoff]
        if len(recent) < 3:
            return 1.0  # 数据不足时假定稳定
        mean = sum(recent) / len(recent)
        var = sum((x - mean) ** 2 for x in recent) / len(recent)
        cv = (var ** 0.5) / (abs(mean) + 1e-10)
        # 变异系数越大，稳定性越低
        stability = max(0.0, min(1.0, 1.0 / (1.0 + cv)))
        return stability
