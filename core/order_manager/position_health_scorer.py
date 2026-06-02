"""
火种系统 · 持仓健康度评分器 (PositionHealthScorer)

核心职责：
1. 基于六维加权模型为每笔活跃持仓实时计算健康度评分，浮盈使用ATR标准化（上限5倍ATR），
   OBI得分采用连续映射（方向一致时与OBI绝对值成正比，方向相反时最低10分）
2. 支持秒级快速评估与分钟级深度评估，输出标准化健康等级、诊断原因与可执行建议
3. 支持从配置文件动态加载权重与阈值，支持运行时热重载（要求提供完整权重集）
4. 内置缓存命中率、评分分布统计与最近评分历史，为运维提供完整的可观测性

外部依赖（真实模块接口）：
- core.perception.olfactory_cortex.OlfactoryCortex : get_current_obi() -> float
- core.perception.multi_band_pll.MultiBandPLL : is_locked, get_instantaneous_frequency() -> float
- core.perception.tactile_cortex.TactileCortex : get_trade_pulse() -> float (成交量分位数 0-1)
- core.order_manager.lifecycle_stages.LifecycleStages : get_stage(position_id) -> str
- core.account_ledger.AccountLedger : get_position(position_id) -> Dict
  (需包含: unrealized_pnl_pct(float,百分比), atr_at_entry_pct(float), direction(int), entry_timestamp(float))
- core.negotiation_bus.NegotiationBus : publish_alert(...)
- core.behavioral_logger.BehavioralLogger : log_event(...)
- core.utils.config_loader.ConfigLoader : get(section) -> Dict

接口契约：
- evaluate_health(position_id: str, mode: str = "fast") -> Dict[str, Any]
  输出: {"status","error_code","reason","data":{"score","level","dimensions","diagnosis","suggestion"}}
- get_all_unhealthy_positions(threshold: float = 40.0) -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- reload_config(config: Dict) -> None   # 必须包含全部六个权重键
  所有公共方法输出字典固定包含 "status", "reason", "data", "warnings"

异常与降级：
- ATR值异常时使用1.0作为默认值，标准化后上限5倍ATR
- 浮盈比例量级异常时降级为默认分并记录ERROR
- 当任何外部感知模块不可用时，对应维度返回中性分
- 当AccountLedger不可用时，评分器拒绝工作
- 权重总和偏离1.0超过0.01时，拒绝应用并保留原有权重
- 深度探测前二次校验依赖非空，防止热替换场景下的竞态

资源管理：
- 缓存采用OrderedDict实现LRU淘汰，最大条目1000
- 评分历史每持仓保留5条
- danger级别告警同时写入独立紧急日志文件
- 不持有外部资源句柄，线程锁自动回收
"""

import time
import logging
import threading
from collections import OrderedDict
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# 独立紧急告警文件路径
EMERGENCY_ALERT_FILE = "/var/log/fire_seed/emergency_alerts.log"


class PositionHealthScorer:
    """持仓健康度评分器"""

    # ========== 默认配置（可从配置文件覆盖） ==========
    WEIGHT_PROFIT = 0.25
    WEIGHT_DURATION = 0.20
    WEIGHT_OBI = 0.20
    WEIGHT_PLL = 0.20
    WEIGHT_VOLUME = 0.10
    WEIGHT_FRESHNESS = 0.05

    SCORE_HEALTHY = 80.0
    SCORE_DEGRADED = 60.0
    SCORE_CRITICAL = 40.0

    CACHE_TTL_FAST = 0.2
    CACHE_TTL_DEEP = 10.0
    MAX_CACHE_SIZE = 1000
    MAX_ALERT_TIMERS = 500
    MAX_SCORE_HISTORY = 5        # 每持仓保留最近N条评分
    DEEP_PROBE_INTERVAL = 60.0   # 深度探测间隔（秒）
    ALERT_COOLDOWN = 30.0
    DEFAULT_DIM_SCORE = 50.0
    ALL_WEIGHT_KEYS = {"profit", "duration", "obi", "pll", "volume", "freshness"}

    def __init__(self, config_loader=None):
        self._olfactory_cortex = None
        self._multi_band_pll = None
        self._tactile_cortex = None
        self._lifecycle_stages = None
        self._account_ledger = None
        self._negotiation_bus = None
        self._behavioral_logger = None
        self._config_loader = config_loader

        self._load_config()

        self._cache: OrderedDict[str, Dict] = OrderedDict()
        self._score_history: Dict[str, List[float]] = {}  # pid -> [scores]
        self._alert_timers: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

        # 统计计数器
        self._stats = {
            "cache_hits": 0,
            "cache_misses": 0,
            "level_counts": {"healthy": 0, "degraded": 0, "critical": 0, "danger": 0},
        }
        self._last_deep_probe = 0.0

        logger.info("PositionHealthScorer 初始化完成")

    # ---------- 配置管理 ----------
    def _load_config(self) -> None:
        if self._config_loader is None:
            return
        try:
            cfg = self._config_loader.get("position_health_scorer", {})
            if cfg:
                self._apply_config(cfg, source="initial")
        except Exception as e:
            logger.error(f"初始配置加载失败: {e}")

    def reload_config(self, config: Dict[str, Any]) -> None:
        try:
            self._apply_config(config, source="reload")
            logger.info("PositionHealthScorer 配置热重载成功")
        except Exception as e:
            logger.error(f"配置热重载失败: {e}")

    def _apply_config(self, cfg: Dict, source: str = "unknown") -> None:
        provided_keys = set(k for k in self.ALL_WEIGHT_KEYS if k in cfg)
        if provided_keys and provided_keys != self.ALL_WEIGHT_KEYS:
            logger.error(f"权重配置不完整，需要 {self.ALL_WEIGHT_KEYS}，实际 {provided_keys}，拒绝更新")
            return

        new_weights = {
            "profit": cfg.get("weight_profit", self.WEIGHT_PROFIT),
            "duration": cfg.get("weight_duration", self.WEIGHT_DURATION),
            "obi": cfg.get("weight_obi", self.WEIGHT_OBI),
            "pll": cfg.get("weight_pll", self.WEIGHT_PLL),
            "volume": cfg.get("weight_volume", self.WEIGHT_VOLUME),
            "freshness": cfg.get("weight_freshness", self.WEIGHT_FRESHNESS),
        }
        total = sum(new_weights.values())
        if abs(total - 1.0) > 0.01:
            logger.error(f"权重总和 {total:.4f} 偏离 1.0，拒绝更新")
            return

        self.WEIGHT_PROFIT = new_weights["profit"]
        self.WEIGHT_DURATION = new_weights["duration"]
        self.WEIGHT_OBI = new_weights["obi"]
        self.WEIGHT_PLL = new_weights["pll"]
        self.WEIGHT_VOLUME = new_weights["volume"]
        self.WEIGHT_FRESHNESS = new_weights["freshness"]
        self.SCORE_HEALTHY = cfg.get("score_healthy", self.SCORE_HEALTHY)
        self.SCORE_DEGRADED = cfg.get("score_degraded", self.SCORE_DEGRADED)
        self.SCORE_CRITICAL = cfg.get("score_critical", self.SCORE_CRITICAL)
        self.CACHE_TTL_FAST = cfg.get("cache_ttl_fast", self.CACHE_TTL_FAST)
        self.CACHE_TTL_DEEP = cfg.get("cache_ttl_deep", self.CACHE_TTL_DEEP)
        self.ALERT_COOLDOWN = cfg.get("alert_cooldown", self.ALERT_COOLDOWN)

    # ---------- 依赖注入 ----------
    def inject_dependencies(
        self, olfactory_cortex=None, multi_band_pll=None, tactile_cortex=None,
        lifecycle_stages=None, account_ledger=None, negotiation_bus=None,
        behavioral_logger=None,
    ) -> None:
        self._olfactory_cortex = olfactory_cortex
        self._multi_band_pll = multi_band_pll
        self._tactile_cortex = tactile_cortex
        self._lifecycle_stages = lifecycle_stages
        self._account_ledger = account_ledger
        self._negotiation_bus = negotiation_bus
        self._behavioral_logger = behavioral_logger
        if account_ledger is None:
            logger.error("AccountLedger 未注入，评分器无法工作")

    # ---------- 公共接口 ----------
    def evaluate_health(self, position_id: str, mode: str = "fast") -> Dict[str, Any]:
        if self._account_ledger is None:
            return {"status": "error", "error_code": "MISSING_DEPENDENCY",
                    "reason": "AccountLedger 未注入", "data": {}, "warnings": []}

        ttl = self.CACHE_TTL_FAST if mode == "fast" else self.CACHE_TTL_DEEP
        with self._lock:
            cached = self._cache.get(position_id)
            if cached and (time.time() - cached["timestamp"]) < ttl:
                self._stats["cache_hits"] += 1
                return self._build_ok(position_id, cached)
            self._stats["cache_misses"] += 1

        try:
            position = self._account_ledger.get_position(position_id)
        except Exception as e:
            return {"status": "error", "error_code": "POSITION_FETCH_FAILED",
                    "reason": str(e), "data": {}, "warnings": []}
        if position is None:
            return {"status": "error", "error_code": "POSITION_NOT_FOUND",
                    "reason": f"持仓不存在: {position_id}", "data": {}, "warnings": []}

        dims = {
            "profit": self._calc_profit(position),
            "duration": self._calc_duration(position),
            "obi": self._calc_obi(position),
            "pll": self._calc_pll(),
            "volume": self._calc_volume(),
            "freshness": self._calc_freshness(position),
        }

        weights = {
            "profit": self.WEIGHT_PROFIT, "duration": self.WEIGHT_DURATION,
            "obi": self.WEIGHT_OBI, "pll": self.WEIGHT_PLL,
            "volume": self.WEIGHT_VOLUME, "freshness": self.WEIGHT_FRESHNESS,
        }
        total = sum(weights[k] * dims[k] for k in dims)
        total = max(0.0, min(100.0, total))

        if total >= self.SCORE_HEALTHY:
            level, diag, sugg = "healthy", "持仓健康", "维持现有策略"
        elif total >= self.SCORE_DEGRADED:
            level, diag, sugg = "degraded", "部分维度走弱", "收紧止损至ATR×0.8"
        elif total >= self.SCORE_CRITICAL:
            level, diag, sugg = "critical", "多维度恶化", "立即减仓50%"
        else:
            level, diag, sugg = "danger", "极度危险", "全部平仓"

        min_dim = min(dims, key=dims.get)
        diag += f" | 最低维度:{min_dim}({dims[min_dim]:.1f})"

        entry = {"score": total, "level": level, "timestamp": time.time(),
                 "dimensions": dims, "diagnosis": diag, "suggestion": sugg}

        with self._lock:
            self._cache[position_id] = entry
            self._cache.move_to_end(position_id, last=True)
            while len(self._cache) > self.MAX_CACHE_SIZE:
                self._cache.popitem(last=False)

            if position_id not in self._score_history:
                self._score_history[position_id] = []
            self._score_history[position_id].append(total)
            if len(self._score_history[position_id]) > self.MAX_SCORE_HISTORY:
                self._score_history[position_id] = self._score_history[position_id][-self.MAX_SCORE_HISTORY:]

            self._stats["level_counts"][level] += 1

        if level in ("critical", "danger"):
            self._trigger_alert(position_id, level, diag, sugg)

        return self._build_ok(position_id, entry)

    def get_all_unhealthy_positions(self, threshold: float = 40.0) -> Dict[str, Any]:
        if self._account_ledger is None:
            return {"status": "error", "error_code": "MISSING_DEPENDENCY",
                    "reason": "AccountLedger 未注入", "data": {}, "warnings": []}
        try:
            all_pos = list(self._account_ledger.get_all_positions())
        except Exception as e:
            return {"status": "error", "error_code": "POSITION_LIST_FAILED",
                    "reason": str(e), "data": {}, "warnings": []}
        unhealthy = []
        for pid in all_pos:
            res = self.evaluate_health(pid, mode="fast")
            if res.get("status") == "ok" and res["data"]["score"] < threshold:
                unhealthy.append(res["data"])
        return {"status": "ok", "reason": f"{len(unhealthy)}个不健康",
                "data": {"unhealthy": unhealthy}, "warnings": []}

    def health_check(self) -> Dict[str, Any]:
        try:
            deps = {
                "account_ledger": self._account_ledger is not None,
                "olfactory_cortex": self._olfactory_cortex is not None,
                "multi_band_pll": self._multi_band_pll is not None,
                "tactile_cortex": self._tactile_cortex is not None,
                "lifecycle_stages": self._lifecycle_stages is not None,
            }

            now = time.time()
            alive = {
                "account_ledger": hasattr(self._account_ledger, 'get_position') if self._account_ledger else False,
                "olfactory_cortex": False,
                "multi_band_pll": False,
            }
            if now - self._last_deep_probe > self.DEEP_PROBE_INTERVAL:
                self._last_deep_probe = now
                # 深度探测前二次校验依赖非空
                if self._olfactory_cortex is not None and hasattr(self._olfactory_cortex, 'get_current_obi'):
                    try:
                        obi = float(self._olfactory_cortex.get_current_obi())
                        alive["olfactory_cortex"] = -1.0 <= obi <= 1.0
                    except Exception:
                        alive["olfactory_cortex"] = False
                else:
                    alive["olfactory_cortex"] = False
                if self._multi_band_pll is not None and hasattr(self._multi_band_pll, 'get_instantaneous_frequency'):
                    try:
                        freq = float(self._multi_band_pll.get_instantaneous_frequency())
                        alive["multi_band_pll"] = freq >= 0.0
                    except Exception:
                        alive["multi_band_pll"] = False
                else:
                    alive["multi_band_pll"] = False
            else:
                alive["olfactory_cortex"] = self._olfactory_cortex is not None
                alive["multi_band_pll"] = self._multi_band_pll is not None

            with self._lock:
                stats = dict(self._stats)
                cache_sz = len(self._cache)
                history_sz = sum(len(v) for v in self._score_history.values())

            return {
                "status": "ok" if all(deps.values()) else "degraded",
                "reason": f"缓存{cache_sz}条, 统计{stats}",
                "data": {
                    "dependencies": deps,
                    "alive": alive,
                    "stats": stats,
                    "cache_hit_rate": round(
                        stats["cache_hits"] / max(1, stats["cache_hits"] + stats["cache_misses"]), 3
                    ),
                    "cache_entries": cache_sz,
                    "history_entries": history_sz,
                },
                "warnings": [],
            }
        except Exception as e:
            return {"status": "error", "error_code": "HEALTH_CHECK_FAILED",
                    "reason": str(e), "data": {}, "warnings": []}

    # ---------- 维度计算 ----------
    def _calc_profit(self, pos: Dict) -> float:
        """浮盈状态得分，使用ATR标准化，上限5倍ATR"""
        try:
            pnl = float(pos.get("unrealized_pnl_pct", 0.0))
            atr = float(pos.get("atr_at_entry_pct", 1.0))
            if atr <= 0.1:  # ATR过小视为数据异常
                logger.warning(f"ATR={atr:.4f}异常偏小，使用默认值1.0")
                atr = 1.0
            if 0 < abs(pnl) < 0.1:
                logger.error(f"浮盈比例 {pnl:.4f} 过小，疑似单位错误，降级为默认分")
                return self.DEFAULT_DIM_SCORE
            normalized = min(pnl / atr, 5.0)  # 上限5倍ATR，防止极端值
            if normalized >= 2.0: return 100.0
            elif normalized >= 1.0: return 85.0
            elif normalized >= 0.5: return 70.0
            elif normalized >= 0.0: return 50.0
            elif normalized >= -0.5: return 30.0
            else: return 10.0
        except Exception:
            return self.DEFAULT_DIM_SCORE

    def _calc_duration(self, pos: Dict) -> float:
        """持仓时长得分，优先使用LifecycleStages，降级使用指数衰减模型"""
        if self._lifecycle_stages:
            try:
                stage = self._lifecycle_stages.get_stage(pos.get("position_id", ""))
                if stage == "maturity": return 90.0
                elif stage == "acceleration": return 80.0
                elif stage == "incubation": return 50.0
                elif stage == "decline": return 20.0
                elif stage == "termination": return 5.0
            except Exception:
                pass
        try:
            entry = float(pos.get("entry_timestamp", time.time()))
            age = time.time() - entry
            # 使用半衰期为600秒的指数衰减模型
            score = 85.0 * (0.5 ** (age / 600.0)) + 15.0
            return max(15.0, min(85.0, score))
        except Exception:
            return self.DEFAULT_DIM_SCORE

    def _calc_obi(self, pos: Dict) -> float:
        """
        OBI得分采用连续映射。
        方向一致时得分与OBI绝对值成正比（50-100分）。
        方向相反时得分随OBI强度递减，下限10分。
        """
        if not self._olfactory_cortex:
            return self.DEFAULT_DIM_SCORE
        try:
            obi = float(self._olfactory_cortex.get_current_obi())
            direction = int(pos.get("direction", 0))
            if (direction == 1 and obi > 0.0) or (direction == -1 and obi < 0.0):
                # 方向一致：OBI越强分越高
                base = 50.0 + min(50.0, abs(obi) * 80.0)
            elif abs(obi) < 0.05:
                base = 50.0
            else:
                # 方向相反：得分随OBI强度递减，下限10分
                # OBI=0.1 → 42分, OBI=0.3 → 26分, OBI=0.5 → 10分
                opposite_score = 50.0 - abs(obi) * 80.0
                base = max(10.0, opposite_score)

            # 斜率修正
            slope = 0.0
            slope_attr = getattr(self._olfactory_cortex, 'get_obi_slope', None)
            if callable(slope_attr):
                slope = float(slope_attr())
            elif isinstance(slope_attr, (int, float)):
                slope = float(slope_attr)
            if (direction == 1 and slope > 0.01) or (direction == -1 and slope < -0.01):
                base += 10.0
            elif (direction == 1 and slope < -0.01) or (direction == -1 and slope > 0.01):
                base -= 15.0
            return max(0.0, min(100.0, base))
        except Exception:
            return self.DEFAULT_DIM_SCORE

    def _calc_pll(self) -> float:
        if not self._multi_band_pll:
            return self.DEFAULT_DIM_SCORE
        try:
            locked = False
            lock_attr = getattr(self._multi_band_pll, 'is_locked', None)
            if callable(lock_attr):
                locked = lock_attr()
            elif lock_attr is not None:
                locked = bool(lock_attr)
            if not locked:
                return 30.0
            freq = float(self._multi_band_pll.get_instantaneous_frequency())
            if freq > 0.02: return 90.0
            elif freq > 0.01: return 70.0
            elif freq > 0.005: return 50.0
            else: return 30.0
        except Exception:
            return self.DEFAULT_DIM_SCORE

    def _calc_volume(self) -> float:
        if not self._tactile_cortex:
            return self.DEFAULT_DIM_SCORE
        try:
            pulse = float(self._tactile_cortex.get_trade_pulse())
            if pulse > 0.8: return 90.0
            elif pulse > 0.5: return 70.0
            else: return 40.0
        except Exception:
            return self.DEFAULT_DIM_SCORE

    def _calc_freshness(self, pos: Dict) -> float:
        try:
            entry = float(pos.get("entry_timestamp", 0.0))
            if entry <= 0: return self.DEFAULT_DIM_SCORE
            age = time.time() - entry
            if age < 30: return 100.0
            elif age < 120: return 75.0
            elif age < 300: return 50.0
            else: return 20.0
        except Exception:
            return self.DEFAULT_DIM_SCORE

    # ---------- 辅助 ----------
    def _build_ok(self, pid: str, entry: Dict) -> Dict:
        return {
            "status": "ok", "error_code": "SUCCESS",
            "reason": f"评分{entry['score']:.1f}",
            "data": {
                "position_id": pid, "score": entry["score"],
                "level": entry["level"], "dimensions": entry["dimensions"],
                "diagnosis": entry["diagnosis"], "suggestion": entry["suggestion"]
            }, "warnings": []
        }

    def _trigger_alert(self, pid: str, level: str, diag: str, sugg: str) -> None:
        now = time.time()
        with self._lock:
            self._cleanup_alert_timers()
            if pid in self._alert_timers:
                if now - self._alert_timers[pid] < self.ALERT_COOLDOWN:
                    return
            self._alert_timers[pid] = now
            while len(self._alert_timers) > self.MAX_ALERT_TIMERS:
                self._alert_timers.popitem(last=False)

        if self._negotiation_bus and hasattr(self._negotiation_bus, 'publish_alert'):
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="position_health", position_id=pid,
                    level=level, diagnosis=diag, suggestion=sugg)
            except Exception as e:
                logger.warning(f"协商总线告警失败: {e}")

        if level == "danger":
            try:
                with open(EMERGENCY_ALERT_FILE, "a") as f:
                    f.write(f"{time.time()} DANGER position_health {pid} diag={diag}\n")
            except Exception:
                pass

        log_msg = f"[{level.upper()}] 持仓 {pid}: {diag}. {sugg} #RECOVERY: 立即评估并执行"
        if level in ("critical", "danger"):
            logger.error(log_msg)
        else:
            logger.warning(log_msg)

    def _cleanup_alert_timers(self) -> None:
        cutoff = time.time() - self.ALERT_COOLDOWN * 2
        expired = [k for k, v in self._alert_timers.items() if v < cutoff]
        for k in expired:
            del self._alert_timers[k]
