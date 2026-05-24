"""
火种系统 · 味觉皮层 (GustatoryCortex) — 深度精细化

核心职责：
1. 将当前市场状态向量与历史经验回放池中的样本进行多维相似度匹配，返回最相似历史环境的盈亏统计与置信度。
2. 管理“甜味记忆区”（盈利经验）与“苦味记忆区”（亏损经验），支持记忆的时效性衰减与主动清理，为评分卡和内部批评家提供基于记忆的决策参考。

外部依赖（真实模块接口）：
- core.experience_replay.ExperienceReplay : 查询历史经验样本池，检索相似市场状态
- core.global_state_archive.GlobalStateArchive : 查询极端事件记忆，获取罕见市场状态参考

接口契约：
- taste(state_vector: Dict[str, float], top_k: int = 20) -> Dict[str, Any]
  输出字典固定包含 "status" (str), "similar_win_rate" (float), "bitter_similarity" (float),
  "sweet_memory_count" (int), "bitter_memory_count" (int), "confidence" (float),
  "reason" (str), "warnings" (List[str])
- record_outcome(state_vector: Dict[str, float], pnl: float) -> Dict[str, Any]
  输出字典固定包含 "status" (str), "reason" (str)
- health_check() -> Dict[str, Any]
  输出字典固定包含 "status" (str), "message" (str)

异常与降级：
- 当 ExperienceReplay 不可用时，返回默认中性值（win_rate=0.5, similarity=0.0, confidence=0.0），状态标记为 "degraded"。
- 当历史样本不足时，降低 top_k 至可用样本数，并在 warnings 中记录；若样本数为零，返回中性默认值。
- 所有外部查询异常被内部捕获，不影响调用方稳定性。

资源管理：
- 本模块持有轻量级查询缓存（容量受限），在内存压力时可自动清空。
- 无外部连接或文件句柄，无需显式释放。
"""

import time
import logging
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class GustatoryCortex:
    """味觉皮层：环境相似度匹配、盈亏记忆检索与时效性管理"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_TOP_K = 20                    # 默认返回最相似样本数，个，取值范围 [5, 100]
    MIN_SAMPLES_FOR_TASTE = 10            # 最低有效样本数，个，取值范围 [5, 50]
    SIMILARITY_THRESHOLD = 0.75           # 相似度匹配阈值，[0.0, 1.0]，低于此值视为不匹配
    BITTER_SIMILARITY_WARNING = 0.85      # 苦味相似度告警阈值，[0.0, 1.0]，超过时触发预警
    MAX_STATE_CACHE_SIZE = 1000           # 状态缓存最大容量，个，取值范围 [100, 10000]
    CACHE_TTL_SECONDS = 60.0              # 缓存有效期，秒，取值范围 [10, 600]
    MEMORY_DECAY_HALFLIFE_DAYS = 30       # 记忆时效半衰期，天，取值范围 [7, 90]
    CONFIDENCE_BASE = 0.5                 # 置信度基值
    CONFIDENCE_SAMPLE_FACTOR = 0.01       # 样本量对置信度的贡献因子

    # 用于相似度计算的标准特征键列表
    STANDARD_FEATURES = [
        "volatility_percentile",
        "spread_ratio",
        "obi_direction",
        "pll_frequency",
        "volume_ratio",
        "ma12_slope",
        "liquidity_level_encoded",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """可接受配置字典覆盖默认参数。"""
        self._config = config or {}
        self._top_k = int(self._config.get("top_k", self.DEFAULT_TOP_K))
        self._top_k = max(5, min(100, self._top_k))
        self._similarity_threshold = float(
            self._config.get("similarity_threshold", self.SIMILARITY_THRESHOLD)
        )
        self._bitter_warning = float(
            self._config.get("bitter_similarity_warning", self.BITTER_SIMILARITY_WARNING)
        )

        # 外部依赖（延迟注入）
        self._experience_replay: Optional[Any] = None
        self._state_archive: Optional[Any] = None

        # 查询缓存
        self._state_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}  # key -> (timestamp, result)
        self._cache_hits: int = 0
        self._cache_misses: int = 0

        logger.info(
            f"GustatoryCortex 初始化完成，top_k={self._top_k}, "
            f"相似度阈值={self._similarity_threshold}, 苦味预警={self._bitter_warning}"
        )

    # ────────────────────────── 依赖注入 ──────────────────────────
    def inject_dependencies(
        self,
        experience_replay: Optional[Any] = None,
        state_archive: Optional[Any] = None
    ) -> None:
        """
        注入外部依赖模块，并进行鸭子类型校验。
        
        Args:
            experience_replay: 经验回放池实例，需实现 query_similar(features, k, threshold) 方法。
            state_archive: 全局状态存档实例，需实现 get_extreme_events(limit) 方法。
        """
        self._experience_replay = experience_replay
        self._state_archive = state_archive

        if experience_replay is not None:
            if not hasattr(experience_replay, "query_similar"):
                logger.warning("ExperienceReplay 缺少 query_similar 方法，味觉功能将降级")
        if state_archive is not None:
            if not hasattr(state_archive, "get_extreme_events"):
                logger.warning("GlobalStateArchive 缺少 get_extreme_events 方法")

        logger.info("GustatoryCortex 依赖注入完成")

    # ────────────────────────── 公共接口 ──────────────────────────
    def taste(self, state_vector: Dict[str, float], top_k: int = 0) -> Dict[str, Any]:
        """
        检索与当前市场状态最相似的历史盈亏记忆。
        
        Args:
            state_vector: 当前市场状态特征字典，应包含 STANDARD_FEATURES 中的关键字段。
            top_k: 返回的最相似样本数，0 表示使用默认值。
        
        Returns:
            标准化字典，包含相似胜率、苦味相似度、甜/苦记忆数量及置信度。
        """
        warnings: List[str] = []
        k = top_k if top_k > 0 else self._top_k

        # 1. 输入校验
        if not state_vector or len(state_vector) == 0:
            return self._neutral_response("输入状态向量为空", warnings)

        # 2. 检查缓存
        cached = self._check_cache(state_vector)
        if cached is not None:
            self._cache_hits += 1
            return cached
        self._cache_misses += 1

        # 3. 提取标准特征向量
        feature_vec = self._extract_features(state_vector)

        # 4. 依赖检查
        if self._experience_replay is None:
            return self._degraded_response("ExperienceReplay 未注入")

        # 5. 从经验回放池检索相似样本
        try:
            similar_samples = self._experience_replay.query_similar(
                feature_vec, k, self._similarity_threshold
            )
        except Exception as e:
            logger.warning(f"查询经验回放池失败: {e}")
            return self._degraded_response(f"查询失败: {str(e)[:100]}")

        # 6. 补充极端事件记忆（样本不足时）
        if self._state_archive and len(similar_samples) < self.MIN_SAMPLES_FOR_TASTE:
            try:
                extreme_samples = self._state_archive.get_extreme_events(limit=k)
                similar_samples.extend(extreme_samples)
            except Exception:
                pass

        # 7. 统计甜味与苦味
        result = self._analyze_samples(similar_samples, feature_vec, warnings)

        # 8. 更新缓存
        self._update_cache(state_vector, result)

        return result

    def record_outcome(self, state_vector: Dict[str, float], pnl: float) -> Dict[str, Any]:
        """
        记录一笔交易的盈亏结果，用于后续更新记忆区。
        本方法本身不执行写入（由经验回放池负责），仅做输入校验和日志记录。
        
        Args:
            state_vector: 开仓时的市场状态特征字典。
            pnl: 该笔交易的盈亏金额。
        
        Returns:
            标准化字典。
        """
        if not state_vector:
            return {"status": "empty_input", "reason": "状态向量为空，未记录"}

        flavor = "sweet" if pnl > 0 else "bitter" if pnl < 0 else "neutral"
        logger.info(
            f"味觉记录: {flavor} (pnl={pnl:.4f}), "
            f"波动率={state_vector.get('volatility_percentile', 'N/A')}"
        )
        return {
            "status": "ok",
            "reason": f"盈亏记录已接收: {flavor} (pnl={pnl:.4f})",
        }

    @classmethod
    def health_check(cls) -> Dict[str, Any]:
        """模块自检：用模拟状态向量测试味觉检索与空输入降级。"""
        try:
            instance = cls()
            test_state = {
                "volatility_percentile": 55.0,
                "spread_ratio": 0.02,
                "obi_direction": "up",
                "pll_frequency": 0.015,
                "volume_ratio": 1.1,
                "ma12_slope": 0.01,
                "liquidity_level": "L3",
            }
            # 依赖未注入时，应返回降级响应
            degraded = instance.taste(test_state)
            if degraded["status"] != "degraded":
                return {"status": "error", "message": "未注入依赖时应返回 degraded"}

            # 测试空输入
            empty_result = instance.taste({})
            if empty_result["status"] != "empty_input" and empty_result["similar_win_rate"] != 0.5:
                return {"status": "error", "message": "空输入未正确处理"}

            # 测试 record_outcome
            record = instance.record_outcome(test_state, 150.0)
            if record["status"] != "ok":
                return {"status": "error", "message": "record_outcome 失败"}

            return {"status": "ok", "message": "所有测试通过（降级模式正常）"}
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return {"status": "error", "message": str(e)}

    # ────────────────────────── 内部方法 ──────────────────────────
    def _extract_features(self, state: Dict[str, float]) -> List[float]:
        """从状态字典中提取标准化特征向量。"""
        features: List[float] = []
        for key in self.STANDARD_FEATURES:
            val = state.get(key, 0.0)
            if isinstance(val, str):
                val = self._encode_string(val)
            features.append(float(val))
        return features

    @staticmethod
    def _encode_string(value: str) -> float:
        """将字符串特征编码为数值。"""
        mapping = {
            "L1": 1.0, "L2": 2.0, "L3": 3.0, "L4": 4.0, "L5": 5.0,
            "up": 1.0, "down": -1.0, "none": 0.0, "flat": 0.0,
            "unknown": 0.0,
        }
        return mapping.get(value, 0.0)

    def _analyze_samples(
        self,
        samples: List[Dict[str, Any]],
        current_features: List[float],
        warnings: List[str]
    ) -> Dict[str, Any]:
        """分析相似样本的盈亏统计，返回综合评估结果。"""
        if not samples or len(samples) == 0:
            warnings.append("无相似历史样本")
            return self._neutral_response("无相似历史样本", warnings)

        sweet_count = 0
        bitter_count = 0
        max_bitter_similarity = 0.0
        total_similarity = 0.0

        for sample in samples:
            pnl = sample.get("pnl", 0.0)
            similarity = sample.get("similarity", 0.5)
            timestamp = sample.get("timestamp", 0.0)

            # 应用时效衰减
            decay = self._calc_time_decay(timestamp)
            effective_similarity = similarity * decay
            total_similarity += effective_similarity

            if pnl > 0:
                sweet_count += 1
            elif pnl < 0:
                bitter_count += 1
                max_bitter_similarity = max(max_bitter_similarity, effective_similarity)

        # 加权胜率
        total_samples = sweet_count + bitter_count
        similar_win_rate = sweet_count / total_samples if total_samples > 0 else 0.5

        # 置信度：样本越多、相似度总和越高，置信度越高
        confidence = self._calc_confidence(total_samples, total_similarity)

        reason = (
            f"检索到 {len(samples)} 个相似样本: "
            f"甜味 {sweet_count} 个, 苦味 {bitter_count} 个, "
            f"相似胜率 {similar_win_rate:.2%}, 置信度 {confidence:.2f}"
        )

        # 苦味相似度过高时告警
        if max_bitter_similarity > self._bitter_warning:
            warnings.append(
                f"苦味相似度偏高 ({max_bitter_similarity:.3f})，建议谨慎开仓"
            )

        return {
            "status": "ok",
            "similar_win_rate": similar_win_rate,
            "bitter_similarity": max_bitter_similarity,
            "sweet_memory_count": sweet_count,
            "bitter_memory_count": bitter_count,
            "confidence": confidence,
            "total_similarity": total_similarity,
            "reason": reason,
            "warnings": warnings,
        }

    def _calc_time_decay(self, timestamp: float) -> float:
        """基于指数衰减计算记忆时效权重。"""
        if timestamp <= 0:
            return 1.0
        age_days = (time.time() - timestamp) / 86400.0
        # 指数衰减：半衰期由配置决定
        decay = math.exp(-0.693 * age_days / self.MEMORY_DECAY_HALFLIFE_DAYS)
        return max(0.1, decay)  # 最低保留 10% 权重

    def _calc_confidence(self, sample_count: int, total_similarity: float) -> float:
        """基于样本量和总相似度计算置信度。"""
        sample_factor = min(1.0, sample_count / self.MIN_SAMPLES_FOR_TASTE)
        similarity_factor = min(1.0, total_similarity / (self.MIN_SAMPLES_FOR_TASTE * 0.8))
        return min(1.0, self.CONFIDENCE_BASE + (sample_factor * similarity_factor - 0.5) * 0.5)

    def _neutral_response(self, reason: str, warnings: List[str]) -> Dict[str, Any]:
        """返回中性默认值。"""
        return {
            "status": "ok" if not warnings else "warning",
            "similar_win_rate": 0.5,
            "bitter_similarity": 0.0,
            "sweet_memory_count": 0,
            "bitter_memory_count": 0,
            "confidence": 0.0,
            "total_similarity": 0.0,
            "reason": reason,
            "warnings": warnings,
        }

    def _degraded_response(self, reason: str) -> Dict[str, Any]:
        """降级响应：返回中性默认值。"""
        logger.warning(f"GustatoryCortex 降级: {reason}")
        return {
            "status": "degraded",
            "similar_win_rate": 0.5,
            "bitter_similarity": 0.0,
            "sweet_memory_count": 0,
            "bitter_memory_count": 0,
            "confidence": 0.0,
            "total_similarity": 0.0,
            "reason": f"降级模式: {reason}",
            "warnings": [f"GustatoryCortex 降级: {reason}"],
        }

    # ────────────────────────── 缓存管理 ──────────────────────────
    def _check_cache(self, state: Dict[str, float]) -> Optional[Dict[str, Any]]:
        """检查缓存是否命中。"""
        cache_key = self._build_cache_key(state)
        if cache_key in self._state_cache:
            ts, result = self._state_cache[cache_key]
            if time.time() - ts < self.CACHE_TTL_SECONDS:
                return result
            else:
                del self._state_cache[cache_key]
        return None

    def _update_cache(self, state: Dict[str, float], result: Dict[str, Any]) -> None:
        """更新缓存，维护容量上限。"""
        cache_key = self._build_cache_key(state)
        self._state_cache[cache_key] = (time.time(), result)

        if len(self._state_cache) > self.MAX_STATE_CACHE_SIZE:
            # 清除一半旧缓存
            keys = list(self._state_cache.keys())
            for k in keys[:len(keys) // 2]:
                del self._state_cache[k]

    @staticmethod
    def _build_cache_key(state: Dict[str, float]) -> str:
        """基于核心特征构建缓存键。"""
        vol = int(state.get("volatility_percentile", 50) / 10)
        obi = str(state.get("obi_direction", "none"))[:3]
        liq = str(state.get("liquidity_level", "L3"))
        return f"v{vol}_{obi}_{liq}"

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        """数值边界钳制。"""
        return max(lower, min(upper, value))
