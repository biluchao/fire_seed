"""
火种系统 · 区间时效衰减管理器 (ZoneDecayManager)

核心职责：
1. 管理多周期关键区间的四阶段时效衰减模型（新鲜期/活跃期/记忆期/消退期），根据单调时间自动调整约束力
2. 响应区间触及、突破等外部事件，动态调整衰减速率或重置阶段，并在角色翻转后重新初始化衰减时钟

外部依赖（真实模块接口）：
- 无直接外部模块依赖（仅使用标准库 time 的单调时钟、math 的指数函数、threading 的锁）
- 所有衰减参数均可通过构造函数注入，不依赖全局配置

接口契约：
- register_zone(zone_id: str, formation_time: float, formation_vol_ratio: float = 1.0, trend_strength: float = 1.0) -> Dict[str, Any]
  注册一个新的关键区间，返回初始衰减状态（如果 zone_id 已存在则覆盖并警告）
- get_current_strength(zone_id: str) -> Dict[str, Any]
  根据当前单调时间计算并返回该区间的即时约束力（0.0~1.0）及当前所处阶段（无副作用，线程安全）
- record_touch(zone_id: str) -> Dict[str, Any]
  记录一次区间被有效触及事件，适度降低衰减速率并返回更新后的约束力
- trigger_role_flip(zone_id: str) -> Dict[str, Any]
  区间被突破后触发角色翻转，重置衰减时钟并继承前序部分约束力
- schedule_cleanup() -> Dict[str, Any]
  显式触发过期失效区间清理，返回移除数量
- mark_inactive(zone_id: str) -> Dict[str, Any]
  手动将区间标记为失效
- health_check() -> Dict[str, Any]
  模块自检（只读快照，加锁）
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 zone_id 不存在时，返回错误状态，不抛出异常
- 当内部数据结构异常时，返回降级默认值（最低约束力 0.0），并记录 ERROR 日志
- 所有需要持有锁的操作均使用 with self._lock 保证原子性
- 所有降级值在类常量区明确声明

资源管理：
- 本模块仅维护一个内部字典存储各区间状态，不持有外部资源
- 通过 max_active_zones 限制最大活跃区间数量，防止内存无限增长
- 支持显式清理过期区间，避免热路径中触发清理
"""

import time
import math
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ZoneDecayManager:
    """多周期关键区间时效衰减管理器（金融级卓越标准）"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_STAGE_DURATIONS: Dict[str, float] = {
        "fresh": 300,       # 新鲜期: 5分钟
        "active": 1800,     # 活跃期: 30分钟
        "memory": 7200,     # 记忆期: 2小时
        "fading": 86400,    # 消退期: 24小时
    }

    # 阶段顺序（决定了从一个阶段进入下一阶段的先后关系，不可修改）
    _STAGE_ORDER: Tuple[str, ...] = ("fresh", "active", "memory", "fading")

    DEFAULT_MIN_STRENGTH: Dict[str, float] = {
        "fresh": 0.95,
        "active": 0.70,
        "memory": 0.40,
        "fading": 0.15,
    }

    DEFAULT_BASE_DECAY_RATE: float = 0.012          # 中等成交量时的基准衰减系数
    HIGH_VOL_DECAY_MULT: float = 0.6                # 高成交量形成 → 衰减更慢
    LOW_VOL_DECAY_MULT: float = 1.5                 # 低成交量形成 → 衰减更快

    TOUCH_DECAY_REDUCTION: float = 0.95             # 触及后衰减系数降低 5%
    ROLE_FLIP_INHERIT_STRENGTH: float = 0.8         # 角色翻转后继承的初始约束力比例

    DEFAULT_MAX_ACTIVE_ZONES: int = 200              # 最大活跃区间数量
    DEFAULT_CLEANUP_AGE_SEC: float = 86400 * 7       # 失效区间最长保留时间（7天）

    MIN_EFFECTIVE_STRENGTH: float = 0.05             # 有效约束力最低阈值
    MIN_DECAY_RATE: float = 0.0001                   # 衰减系数下限，防止除零或负值

    # 边界裁切魔法数字统一为常量
    VOL_RATIO_MIN: float = 0.01
    VOL_RATIO_MAX: float = 10.0
    TREND_STRENGTH_MIN: float = 0.1
    TREND_STRENGTH_MAX: float = 5.0
    FORMATION_TIME_FUTURE_TOLERANCE_SEC: float = 1.0

    def __init__(
        self,
        stage_durations: Optional[Dict[str, float]] = None,
        min_strength_map: Optional[Dict[str, float]] = None,
        base_decay_rate: Optional[float] = None,
        max_active_zones: Optional[int] = None,
        cleanup_age_sec: Optional[float] = None,
    ):
        self._stage_durations = dict(stage_durations) if stage_durations else self.DEFAULT_STAGE_DURATIONS.copy()
        self._min_strength_map = dict(min_strength_map) if min_strength_map else self.DEFAULT_MIN_STRENGTH.copy()
        self._base_decay_rate = base_decay_rate if base_decay_rate is not None else self.DEFAULT_BASE_DECAY_RATE
        self._max_active_zones = max_active_zones or self.DEFAULT_MAX_ACTIVE_ZONES
        self._cleanup_age_sec = cleanup_age_sec if cleanup_age_sec is not None else self.DEFAULT_CLEANUP_AGE_SEC

        self._zones: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

        self._last_cleanup_time: float = time.monotonic()
        self._total_cleanups: int = 0

        logger.info(
            "ZoneDecayManager 初始化完成，阶段=%s, 基准衰减=%.4f, 最大活跃区=%d",
            list(self._stage_durations.keys()),
            self._base_decay_rate,
            self._max_active_zones,
        )

    # ========== 公共接口 ==========
    def register_zone(
        self,
        zone_id: str,
        formation_time: float,
        formation_vol_ratio: float = 1.0,
        trend_strength: float = 1.0,
    ) -> Dict[str, Any]:
        """注册一个新的关键区间"""
        if not isinstance(zone_id, str) or not zone_id.strip():
            return {
                "status": "error",
                "reason": "zone_id 必须是非空字符串",
                "data": {},
                "warnings": ["invalid_zone_id"],
            }

        now_mono = time.monotonic()
        if formation_time <= 0:
            return {
                "status": "error",
                "reason": "formation_time 必须为正数",
                "data": {},
                "warnings": ["invalid_formation_time"],
            }
        if formation_time > now_mono + self.FORMATION_TIME_FUTURE_TOLERANCE_SEC:
            return {
                "status": "error",
                "reason": "formation_time 不能是未来时间",
                "data": {},
                "warnings": ["future_formation_time"],
            }

        # 边界裁切使用常量
        formation_vol_ratio = max(self.VOL_RATIO_MIN, min(self.VOL_RATIO_MAX, formation_vol_ratio))
        trend_strength = max(self.TREND_STRENGTH_MIN, min(self.TREND_STRENGTH_MAX, trend_strength))

        # 计算衰减系数
        if formation_vol_ratio >= 2.0:
            decay_rate = self._base_decay_rate * self.HIGH_VOL_DECAY_MULT
        elif formation_vol_ratio < 0.5:
            decay_rate = self._base_decay_rate * self.LOW_VOL_DECAY_MULT
        else:
            decay_rate = self._base_decay_rate

        decay_rate /= max(0.5, min(2.0, trend_strength))
        decay_rate = max(self.MIN_DECAY_RATE, decay_rate)

        new_state = {
            "formation_time": formation_time,
            "formation_vol_ratio": formation_vol_ratio,
            "trend_strength": trend_strength,
            "decay_rate": decay_rate,
            "current_stage": "fresh",
            "stage_start_time": formation_time,
            "last_touch_time": formation_time,
            "is_active": True,
            "start_strength_override": 1.0,
        }

        with self._lock:
            existed = zone_id in self._zones
            self._zones[zone_id] = new_state
            self._enforce_active_limit()

        if existed:
            logger.warning("区间 %s 已存在，被覆盖。新衰减系数=%.4f", zone_id, decay_rate)
        else:
            logger.info("注册新区间 %s: 形成时间=%.2f, 衰减系数=%.4f", zone_id, formation_time, decay_rate)

        return {
            "status": "ok",
            "reason": f"区间 {zone_id} 注册成功，初始阶段: fresh",
            "data": {"zone_id": zone_id, "current_stage": "fresh", "strength": 1.0, "decay_rate": decay_rate},
            "warnings": ["zone_overwritten"] if existed else [],
        }

    def get_current_strength(self, zone_id: str) -> Dict[str, Any]:
        """计算并返回指定区间的即时约束力（无副作用，不会更新 is_active）"""
        if zone_id not in self._zones:
            return {
                "status": "error",
                "reason": f"区间 {zone_id} 不存在",
                "data": {"zone_id": zone_id, "strength": 0.0, "stage": "unknown"},
                "warnings": [f"unknown_zone: {zone_id}"],
            }

        now_mono = time.monotonic()
        with self._lock:
            zone = self._zones[zone_id].copy()

        if not zone["is_active"]:
            return {
                "status": "ok",
                "reason": "区间已失效",
                "data": {
                    "zone_id": zone_id,
                    "strength": 0.0,
                    "stage": "inactive",
                    "total_elapsed_seconds": 0.0,
                    "decay_rate": zone["decay_rate"],
                    "formation_time": zone["formation_time"],
                },
                "warnings": [],
            }

        total_elapsed = max(0.0, now_mono - zone["formation_time"])
        current_stage = self._determine_stage(total_elapsed)
        stage_elapsed = max(0.0, now_mono - zone["stage_start_time"])

        start_strength = self._get_stage_start_strength(current_stage, zone)
        min_s = self._min_strength_map.get(current_stage, 0.1)
        decay = zone["decay_rate"]

        try:
            factor = math.exp(-decay * stage_elapsed)
        except OverflowError:
            factor = 0.0

        strength = min_s + (start_strength - min_s) * factor
        strength = max(0.0, min(1.0, strength))

        if math.isnan(strength):
            strength = min_s
            logger.warning("区间 %s 约束力计算结果为 NaN，已降级为最低值", zone_id)

        is_active = strength >= self.MIN_EFFECTIVE_STRENGTH

        return {
            "status": "ok",
            "reason": f"区间 {zone_id} 当前约束力: {strength:.6f}, 阶段: {current_stage}",
            "data": {
                "zone_id": zone_id,
                "strength": round(strength, 6),
                "stage": current_stage,
                "total_elapsed_seconds": round(total_elapsed, 3),
                "stage_elapsed_seconds": round(stage_elapsed, 3),
                "decay_rate": decay,
                "is_active": is_active,
                "formation_time": zone["formation_time"],
            },
            "warnings": [],
        }

    def mark_inactive(self, zone_id: str) -> Dict[str, Any]:
        """手动将区间标记为失效"""
        if zone_id not in self._zones:
            return {
                "status": "error",
                "reason": f"区间 {zone_id} 不存在",
                "data": {},
                "warnings": [f"unknown_zone: {zone_id}"],
            }
        with self._lock:
            self._zones[zone_id]["is_active"] = False
        logger.info("区间 %s 被手动标记为失效", zone_id)
        return {
            "status": "ok",
            "reason": f"区间 {zone_id} 已标记为失效",
            "data": {"zone_id": zone_id},
            "warnings": [],
        }

    def record_touch(self, zone_id: str) -> Dict[str, Any]:
        """记录一次区间被有效触及事件，并返回更新后的强度"""
        if zone_id not in self._zones:
            return {
                "status": "error",
                "reason": f"区间 {zone_id} 不存在",
                "data": {},
                "warnings": [f"unknown_zone: {zone_id}"],
            }

        with self._lock:
            zone = self._zones[zone_id]
            if not zone["is_active"]:
                return {
                    "status": "ok",
                    "reason": "区间已失效，触及无效",
                    "data": {},
                    "warnings": [],
                }
            old_rate = zone["decay_rate"]
            zone["decay_rate"] = max(self.MIN_DECAY_RATE, old_rate * self.TOUCH_DECAY_REDUCTION)
            zone["last_touch_time"] = time.monotonic()
            new_rate = zone["decay_rate"]

        logger.info("区间 %s 被触及，衰减系数 %.4f -> %.4f", zone_id, old_rate, new_rate)
        # 返回当前强度
        strength_resp = self.get_current_strength(zone_id)
        return {
            "status": "ok",
            "reason": f"区间 {zone_id} 触及记录成功，衰减放缓至 {new_rate:.4f}",
            "data": {
                "zone_id": zone_id,
                "new_decay_rate": new_rate,
                "strength": strength_resp["data"].get("strength", 0.0),
            },
            "warnings": [],
        }

    def trigger_role_flip(self, zone_id: str) -> Dict[str, Any]:
        """区间被有效突破后触发角色翻转"""
        if zone_id not in self._zones:
            return {
                "status": "error",
                "reason": f"区间 {zone_id} 不存在",
                "data": {},
                "warnings": [f"unknown_zone: {zone_id}"],
            }

        now_mono = time.monotonic()
        with self._lock:
            zone = self._zones[zone_id]
            if not zone["is_active"]:
                return {
                    "status": "ok",
                    "reason": "区间已失效，无法翻转",
                    "data": {},
                    "warnings": [],
                }

            # 计算翻转前强度
            total_elapsed = max(0.0, now_mono - zone["formation_time"])
            current_stage = self._determine_stage(total_elapsed)
            stage_elapsed = max(0.0, now_mono - zone["stage_start_time"])
            start_strength = self._get_stage_start_strength(current_stage, zone)
            min_s = self._min_strength_map.get(current_stage, 0.1)
            try:
                factor = math.exp(-zone["decay_rate"] * stage_elapsed)
            except OverflowError:
                factor = 0.0
            prev_strength = min_s + (start_strength - min_s) * factor
            prev_strength = max(0.0, min(1.0, prev_strength))

            inherited = prev_strength * self.ROLE_FLIP_INHERIT_STRENGTH

            # 重新计算衰减系数（使用注册时的成交量特征，保持不变）
            new_decay = self._base_decay_rate
            vol_ratio = zone["formation_vol_ratio"]
            trend = zone["trend_strength"]
            if vol_ratio >= 2.0:
                new_decay *= self.HIGH_VOL_DECAY_MULT
            elif vol_ratio < 0.5:
                new_decay *= self.LOW_VOL_DECAY_MULT
            new_decay /= max(0.5, min(2.0, trend))
            new_decay = max(self.MIN_DECAY_RATE, new_decay)

            # 更新状态
            zone["formation_time"] = now_mono
            zone["stage_start_time"] = now_mono
            zone["current_stage"] = "fresh"
            zone["is_active"] = True
            zone["decay_rate"] = new_decay
            zone["start_strength_override"] = inherited

        logger.info("区间 %s 角色翻转，继承约束力 %.4f，新衰减系数 %.4f", zone_id, inherited, new_decay)
        return {
            "status": "ok",
            "reason": f"区间 {zone_id} 角色翻转成功，继承约束力 {inherited:.4f}",
            "data": {
                "zone_id": zone_id,
                "inherited_strength": round(inherited, 4),
                "new_stage": "fresh",
                "new_decay_rate": round(new_decay, 6),
            },
            "warnings": [],
        }

    def schedule_cleanup(self) -> Dict[str, Any]:
        """显式触发过期区间清理"""
        now_mono = time.monotonic()
        removed = 0
        with self._lock:
            to_remove = [
                zid for zid, z in self._zones.items()
                if not z["is_active"] and (now_mono - z["formation_time"]) > self._cleanup_age_sec
            ]
            for zid in to_remove:
                del self._zones[zid]
                removed += 1
        if removed:
            logger.info("ZoneDecayManager 清理了 %d 个过期失效区间", removed)
        self._last_cleanup_time = now_mono
        self._total_cleanups += 1
        return {
            "status": "ok",
            "reason": f"清理完成，移除 {removed} 个区间",
            "data": {"removed": removed, "last_cleanup_time": self._last_cleanup_time},
            "warnings": [],
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            if not hasattr(self, '_zones'):
                return {
                    "status": "degraded",
                    "reason": "内部状态未初始化",
                    "data": {},
                    "warnings": ["uninitialized"],
                }
            with self._lock:
                total = len(self._zones)
                active = sum(1 for z in self._zones.values() if z["is_active"])
                stages = {"fresh": 0, "active": 0, "memory": 0, "fading": 0, "inactive": 0}
                for z in self._zones.values():
                    if z["is_active"]:
                        stages[z.get("current_stage", "fresh")] += 1
                    else:
                        stages["inactive"] += 1
            return {
                "status": "ok",
                "reason": f"ZoneDecayManager 正常，管理 {total} 个区间，活跃 {active}",
                "data": {
                    "total_zones": total,
                    "active_zones": active,
                    "stages_distribution": stages,
                    "last_cleanup_time": self._last_cleanup_time,
                    "total_cleanups": self._total_cleanups,
                },
                "warnings": [],
            }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查内部字典和锁状态")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _determine_stage(self, total_elapsed: float) -> str:
        """根据总经过时间确定当前衰减阶段"""
        if total_elapsed < self._stage_durations["fresh"]:
            return "fresh"
        if total_elapsed < self._stage_durations["active"]:
            return "active"
        if total_elapsed < self._stage_durations["memory"]:
            return "memory"
        return "fading"

    def _get_stage_start_strength(self, stage: str, zone: Dict[str, Any]) -> float:
        """获取进入指定阶段时的起始约束力"""
        if stage == "fresh":
            return zone.get("start_strength_override", 1.0) or 1.0
        prev = self._get_previous_stage(stage)
        if prev is None:
            return 1.0
        return self._min_strength_map.get(prev, 0.7)

    def _get_previous_stage(self, stage: str) -> Optional[str]:
        """获取前一个阶段名称，使用类常量顺序"""
        try:
            idx = self._STAGE_ORDER.index(stage)
            return self._STAGE_ORDER[idx - 1] if idx > 0 else None
        except ValueError:
            return None

    def _enforce_active_limit(self) -> None:
        """限制活跃区间总数，优先清理失效区间，若仍不足则淘汰最旧的活跃区间"""
        if len(self._zones) <= self._max_active_zones:
            return

        # 1. 先清理失效区间
        inactive = [(zid, z["formation_time"]) for zid, z in self._zones.items() if not z["is_active"]]
        inactive.sort(key=lambda x: x[1])
        to_remove = min(len(self._zones) - self._max_active_zones, len(inactive))
        for i in range(to_remove):
            del self._zones[inactive[i][0]]
        if len(self._zones) <= self._max_active_zones:
            if to_remove > 0:
                logger.info("活跃区间超限，清理了 %d 个失效区间", to_remove)
            return

        # 2. 仍然超限，淘汰最旧的活跃区间（降级策略）
        active_list = [(zid, z["formation_time"]) for zid, z in self._zones.items() if z["is_active"]]
        active_list.sort(key=lambda x: x[1])
        extra = len(self._zones) - self._max_active_zones
        for i in range(extra):
            del self._zones[active_list[i][0]]
        logger.warning(
            "活跃区间数量严重超限 (max=%d)，淘汰了 %d 个最旧活跃区间",
            self._max_active_zones,
            extra,
          )
