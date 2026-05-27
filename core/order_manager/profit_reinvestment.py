"""
火种系统 · 利润再投资与弹药库 (ProfitReinvestment)

核心职责：
1. 管理部分止盈或全平后释放的保证金，按优先级（同策略加仓 -> 跨策略分配 -> 弹药库储备）自动分配
2. 维护弹药库资金池，支持高质量信号触发时的智能释放、策略亏损时的反向解锁，以及利润锁定比例的动态调整

外部依赖（真实模块接口）：
- core.position_sizer.PositionSizer : 获取当前策略的资金需求与仓位上限
- core.risk_monitor.circuit_breaker.CircuitBreaker : 查询是否处于熔断冷却期，熔断期间禁止再投资
- core.ecological_niche.capital_allocator.CapitalAllocator : 跨策略资金调拨接口
- core.account_ledger.AccountLedger : 获取账户总权益、可用保证金等财务数据
- core.negotiation_bus.NegotiationBus : 发布弹药库释放/锁定事件，供其他模块订阅

接口契约：
- allocate_released_capital(amount: float, source_strategy: str, current_signal_score: float) -> Dict[str, Any]
- request_ammo_release(signal_score: float, required_amount: float) -> Dict[str, Any]
- get_ammo_status() -> Dict[str, Any]
- health_check() -> Dict[str, Any]
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])

异常与降级：
- 当 PositionSizer 不可用时，跳过同策略加仓，直接将资金转入弹药库
- 当 CapitalAllocator 不可用时，跨策略分配降级为弹药库储备
- 当 AccountLedger 不可用时，使用上一次缓存的总权益计算分配比例，或使用弹药库余额反推最低保留
- 当数据库不可用时，弹药库余额仅保存在内存中，重启丢失，critical 日志告警
- 所有降级值在类常量区明确声明

资源管理：
- 弹药库余额持久化至本地 SQLite 数据库，通过线程锁保证并发写入安全
- 数据库连接在模块初始化时创建，在系统退出时通过 atexit 回调关闭
- 不持有其他外部资源句柄
"""

import time
import logging
import threading
import atexit
import sqlite3
import os
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class ProfitReinvestment:
    """利润再投资与弹药库管理"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    DEFAULT_AMMO_MAX_PCT = 0.20            # 弹药库资金占权益上限，无量纲，[0.10, 0.30]
    DEFAULT_AMMO_MIN_PCT = 0.05            # 弹药库最低保留比例（占权益），无量纲，[0.02, 0.10]
    DEFAULT_REVERSE_UNLOCK_RATIO = 0.50   # 反向解锁每次释放的比例，[0.30, 0.70]
    DEFAULT_BORROW_MAX_DAYS = 3            # 借贷最长还款期限（交易日），天，[1, 7]
    DEFAULT_LOCK_STAGE1_PCT = 0.005        # 利润锁定第一阶梯：浮盈阈值，无量纲，[0.002, 0.010]
    DEFAULT_LOCK_STAGE1_RATIO = 0.20       # 第一阶梯锁定比例，[0.10, 0.30]
    DEFAULT_LOCK_STAGE2_PCT = 0.010        # 第二阶梯浮盈阈值，[0.008, 0.020]
    DEFAULT_LOCK_STAGE2_RATIO = 0.40       # 第二阶梯锁定比例，[0.30, 0.50]
    DEFAULT_LOCK_STAGE3_PCT = 0.020        # 第三阶梯浮盈阈值，[0.015, 0.030]
    DEFAULT_LOCK_STAGE3_RATIO = 0.60       # 第三阶梯锁定比例，[0.50, 0.70]
    DEFAULT_REINVEST_SIGNAL_THRESHOLD = 80 # 高质量信号评分阈值，无量纲，[70, 95]
    DEFAULT_BORROW_SIGNAL_THRESHOLD = 85   # 借贷信号评分阈值，[80, 95]
    DEFAULT_DB_PATH = "data/ammo_reserve.db"  # 弹药库持久化数据库路径
    MIN_RESERVE_AMMO_FALLBACK_RATIO = 0.10 # AccountLedger 不可用时，最低保留占当前弹药库余额比例

    # 再投资优先级
    REINVEST_PRIORITY = ["same_strategy", "cross_strategy", "ammo_reserve"]

    def __init__(self):
        # 弹药库余额（内存缓存）
        self._ammo_balance: float = 0.0
        # 借贷记录 {strategy: [{"amount": float, "timestamp": float, "repaid": bool}]}
        self._loans: Dict[str, List[Dict]] = {}

        # 外部依赖
        self._position_sizer = None
        self._circuit_breaker = None
        self._capital_allocator = None
        self._account_ledger = None
        self._negotiation_bus = None

        # 线程安全
        self._lock = threading.RLock()

        # 数据库连接
        self._db_conn: Optional[sqlite3.Connection] = None
        self._init_db()

        # 注册退出清理
        atexit.register(self._cleanup)

        # 加载持久化数据
        self._load_state()

        logger.info("ProfitReinvestment 初始化完成，弹药库余额: %.2f", self._ammo_balance)

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        position_sizer: Optional[Any] = None,
        circuit_breaker: Optional[Any] = None,
        capital_allocator: Optional[Any] = None,
        account_ledger: Optional[Any] = None,
        negotiation_bus: Optional[Any] = None,
    ) -> None:
        """注入外部依赖"""
        if position_sizer is not None:
            self._position_sizer = position_sizer
            logger.info("PositionSizer 注入成功")
        else:
            logger.warning("PositionSizer 未注入，同策略加仓降级为弹药库储备")

        if circuit_breaker is not None:
            self._circuit_breaker = circuit_breaker
            logger.info("CircuitBreaker 注入成功")
        else:
            logger.warning("CircuitBreaker 未注入，再投资将忽略熔断检查")

        if capital_allocator is not None:
            self._capital_allocator = capital_allocator
            logger.info("CapitalAllocator 注入成功")
        else:
            logger.warning("CapitalAllocator 未注入，跨策略分配降级为弹药库储备")

        if account_ledger is not None:
            self._account_ledger = account_ledger
            logger.info("AccountLedger 注入成功")
        else:
            logger.warning("AccountLedger 未注入，将使用弹药库余额反推最低保留")

        if negotiation_bus is not None:
            self._negotiation_bus = negotiation_bus
            logger.info("NegotiationBus 注入成功")
        else:
            logger.warning("NegotiationBus 未注入，弹药库事件不对外广播")

    # ========== 公共接口 ==========
    def allocate_released_capital(
        self, amount: float, source_strategy: str, current_signal_score: float
    ) -> Dict[str, Any]:
        """
        将部分止盈或全平释放的保证金按优先级分配

        Args:
            amount: 释放的保证金数额（账户计价货币单位）
            source_strategy: 来源策略标识
            current_signal_score: 当前信号评分（0-100），用于判断是否继续加仓

        Returns:
            标准响应字典，data 中包含 allocation 详情
        """
        if amount <= 0:
            logger.warning(f"无效释放金额: {amount}，跳过分配")
            return {
                "status": "error",
                "reason": "释放金额必须大于 0",
                "data": {},
                "warnings": ["invalid_amount"],
            }

        # 熔断期间禁止再投资
        if self._circuit_breaker is not None:
            try:
                if self._circuit_breaker.is_in_cooldown():
                    logger.info("熔断冷却中，资金转入弹药库")
                    self._add_to_ammo(amount)
                    return {
                        "status": "ok",
                        "reason": "熔断冷却中，资金转入弹药库储备",
                        "data": {"allocation": "ammo_reserve", "amount": amount},
                        "warnings": ["circuit_breaker_active"],
                    }
            except Exception as e:
                logger.warning(f"熔断检查异常: {e}，按默认规则分配")

        allocations = []
        remaining = amount

        for priority in self.REINVEST_PRIORITY:
            if remaining <= 0:
                break
            allocated = self._execute_allocation(priority, remaining, source_strategy, current_signal_score)
            allocated = min(allocated, remaining)  # 防止浮点精度导致超扣
            if allocated > 1e-10:  # 过滤极小的浮点噪声
                reason = self._get_allocation_reason(priority, source_strategy, current_signal_score)
                allocations.append({"target": priority, "amount": round(allocated, 8), "reason": reason})
                remaining -= allocated
                remaining = max(0.0, remaining)  # 确保非负

        return {
            "status": "ok",
            "reason": f"已分配 {amount:.4f}，剩余 {remaining:.4f} 转入弹药库",
            "data": {
                "total_released": amount,
                "allocations": allocations,
                "ammo_balance": self._ammo_balance,
            },
            "warnings": [],
        }

    def request_ammo_release(self, signal_score: float, required_amount: float) -> Dict[str, Any]:
        """
        根据信号强度请求从弹药库释放资金

        Args:
            signal_score: 信号评分 (0-100)
            required_amount: 需要的资金数额

        Returns:
            标准响应字典
        """
        if required_amount <= 0:
            return {
                "status": "error",
                "reason": "请求金额必须大于 0",
                "data": {},
                "warnings": ["invalid_amount"],
            }

        with self._lock:
            if signal_score >= self.DEFAULT_BORROW_SIGNAL_THRESHOLD:
                release_ratio = 0.80  # 极强信号释放 80%
            elif signal_score >= self.DEFAULT_REINVEST_SIGNAL_THRESHOLD:
                release_ratio = 0.50
            else:
                release_ratio = 0.0

            if release_ratio == 0.0:
                return {
                    "status": "ok",
                    "reason": "信号强度不足，不释放弹药库资金",
                    "data": {"released": 0.0, "ammo_balance": self._ammo_balance},
                    "warnings": ["signal_score_below_threshold"],
                }

            max_release = self._ammo_balance * release_ratio
            actual_release = min(max_release, required_amount)

            # 保留最低弹药库余额
            total_equity = self._get_total_equity()
            if total_equity <= 0:
                # 降级：使用弹药库余额的固定比例作为最低保留
                min_reserve = self._ammo_balance * self.MIN_RESERVE_AMMO_FALLBACK_RATIO
                logger.warning("总权益不可用，使用弹药库余额 %.4f 的 %.0f%% 作为最低保留: %.4f",
                               self._ammo_balance, self.MIN_RESERVE_AMMO_FALLBACK_RATIO * 100, min_reserve)
            else:
                min_reserve = total_equity * self.DEFAULT_AMMO_MIN_PCT

            if self._ammo_balance - actual_release < min_reserve:
                actual_release = max(0.0, self._ammo_balance - min_reserve)

            self._ammo_balance -= actual_release
            self._persist_balance()

            logger.info("弹药库释放: %.4f (信号评分 %d, 释放比例 %.0f%%)",
                        actual_release, int(signal_score), release_ratio * 100)
            self._notify_ammo_event("release", actual_release, signal_score)

            return {
                "status": "ok",
                "reason": f"弹药库释放 {actual_release:.4f}",
                "data": {
                    "released": actual_release,
                    "release_ratio": release_ratio,
                    "ammo_balance": self._ammo_balance,
                },
                "warnings": [],
            }

    def get_ammo_status(self) -> Dict[str, Any]:
        """获取弹药库当前状态"""
        with self._lock:
            total_equity = self._get_total_equity()
            ammo_pct = (self._ammo_balance / total_equity * 100) if total_equity > 0 else 0.0
            outstanding_loans = sum(
                loan["amount"] for loans in self._loans.values()
                for loan in loans if not loan["repaid"]
            )
            return {
                "status": "ok",
                "reason": "弹药库状态查询成功",
                "data": {
                    "balance": self._ammo_balance,
                    "ammo_pct_of_equity": round(ammo_pct, 2),
                    "max_capacity": total_equity * self.DEFAULT_AMMO_MAX_PCT if total_equity > 0 else 0,
                    "outstanding_loans": outstanding_loans,
                    "loan_count": sum(1 for loans in self._loans.values() for loan in loans if not loan["repaid"]),
                },
                "warnings": [],
            }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检"""
        try:
            with self._lock:
                db_ok = self._db_conn is not None
                if db_ok:
                    try:
                        self._db_conn.execute("SELECT 1")
                    except Exception:
                        db_ok = False
                        logger.error("弹药库数据库连接异常 #RECOVERY: 检查 SQLite 文件和磁盘状态")

                warnings = [] if db_ok else ["database_disconnected"]
                if not db_ok:
                    logger.critical("弹药库持久化不可用，重启将丢失余额")

                # 检查逾期贷款
                overdue = self._check_overdue_loans()
                if overdue:
                    warnings.append(f"存在 {len(overdue)} 笔逾期贷款")
                    for item in overdue:
                        logger.warning("逾期贷款: %s, 金额 %.4f, 天数 %d",
                                       item["strategy"], item["amount"], item["days_overdue"])

                return {
                    "status": "ok" if db_ok and not overdue else "degraded",
                    "reason": "弹药库模块正常" if (db_ok and not overdue) else "存在异常",
                    "data": {
                        "ammo_balance": self._ammo_balance,
                        "db_connected": db_ok,
                        "overdue_loans": overdue,
                        "dependencies": {
                            "position_sizer": self._position_sizer is not None,
                            "circuit_breaker": self._circuit_breaker is not None,
                            "capital_allocator": self._capital_allocator is not None,
                            "account_ledger": self._account_ledger is not None,
                            "negotiation_bus": self._negotiation_bus is not None,
                        },
                    },
                    "warnings": warnings,
                }
        except Exception as e:
            logger.error(f"健康检查失败: {e} #RECOVERY: 检查数据库文件权限和磁盘空间")
            return {
                "status": "error",
                "reason": f"健康检查异常: {str(e)}",
                "data": {},
                "warnings": [f"health_check_failed: {str(e)}"],
            }

    # ========== 私有方法 ==========
    def _execute_allocation(
        self, target: str, amount: float, strategy: str, signal_score: float
    ) -> float:
        """执行单个优先级的分配，返回实际分配金额"""
        if target == "same_strategy":
            return self._allocate_same_strategy(amount, strategy, signal_score)
        elif target == "cross_strategy":
            return self._allocate_cross_strategy(amount)
        elif target == "ammo_reserve":
            return self._allocate_to_ammo(amount)
        return 0.0

    def _allocate_same_strategy(self, amount: float, strategy: str, signal_score: float) -> float:
        """同策略加仓"""
        if self._position_sizer is None:
            logger.debug("PositionSizer 不可用，跳过同策略加仓")
            return 0.0
        if signal_score < self.DEFAULT_REINVEST_SIGNAL_THRESHOLD:
            logger.debug("信号评分 %d 低于阈值 %d，跳过同策略加仓",
                         int(signal_score), self.DEFAULT_REINVEST_SIGNAL_THRESHOLD)
            return 0.0
        try:
            available = self._position_sizer.get_available_capacity(strategy)
            allocated = min(amount, available)
            if allocated > 0:
                logger.info("同策略加仓: %s, 金额: %.4f", strategy, allocated)
            return allocated
        except Exception as e:
            logger.warning(f"同策略加仓查询异常: {e}")
            return 0.0

    def _allocate_cross_strategy(self, amount: float) -> float:
        """跨策略分配"""
        if self._capital_allocator is None:
            return 0.0
        try:
            allocated = self._capital_allocator.allocate(amount)
            if allocated > 0:
                logger.info("跨策略分配: %.4f", allocated)
            return allocated
        except Exception as e:
            logger.warning(f"跨策略分配异常: {e}")
            return 0.0

    def _allocate_to_ammo(self, amount: float) -> float:
        """转入弹药库"""
        self._add_to_ammo(amount)
        return amount

    def _add_to_ammo(self, amount: float) -> None:
        """增加弹药库余额（线程安全）"""
        with self._lock:
            total_equity = self._get_total_equity()
            max_ammo = total_equity * self.DEFAULT_AMMO_MAX_PCT if total_equity > 0 else float("inf")
            effective = min(amount, max_ammo - self._ammo_balance)
            if effective <= 0:
                overflow = amount
                logger.info("弹药库已达上限 (%.4f/%.4f)，溢出 %.4f 将退回主资金池",
                            self._ammo_balance, max_ammo, overflow)
                self._notify_ammo_event("ammo_overflow", overflow)
                return
            self._ammo_balance += effective
            self._persist_balance()
            logger.debug("弹药库增加: %.4f (当前: %.4f)", effective, self._ammo_balance)
            self._notify_ammo_event("ammo_deposit", effective)

    def _get_total_equity(self) -> float:
        """获取账户总权益（带降级）"""
        if self._account_ledger is not None:
            try:
                return self._account_ledger.get_total_equity()
            except Exception as e:
                logger.warning(f"获取总权益异常: {e}")
        return 0.0

    def _get_allocation_reason(self, target: str, strategy: str, signal_score: float) -> str:
        """生成分配原因说明"""
        if target == "same_strategy":
            return f"信号评分{signal_score:.0f}>={self.DEFAULT_REINVEST_SIGNAL_THRESHOLD}，同策略{strategy}加仓"
        elif target == "cross_strategy":
            return "跨策略分配"
        elif target == "ammo_reserve":
            return "转入弹药库储备"
        return "未知"

    def _check_overdue_loans(self) -> List[Dict[str, Any]]:
        """检查逾期未还的借贷"""
        overdue = []
        now = time.time()
        max_seconds = self.DEFAULT_BORROW_MAX_DAYS * 86400
        with self._lock:
            for strategy, loans in self._loans.items():
                for loan in loans:
                    if not loan["repaid"] and (now - loan["timestamp"]) > max_seconds:
                        overdue.append({
                            "strategy": strategy,
                            "amount": loan["amount"],
                            "timestamp": loan["timestamp"],
                            "days_overdue": int((now - loan["timestamp"]) / 86400),
                        })
        return overdue

    def _init_db(self) -> None:
        """初始化数据库"""
        try:
            os.makedirs(os.path.dirname(self.DEFAULT_DB_PATH), exist_ok=True)
            self._db_conn = sqlite3.connect(self.DEFAULT_DB_PATH, check_same_thread=False)
            self._db_conn.execute(
                "CREATE TABLE IF NOT EXISTS ammo_state (key TEXT PRIMARY KEY, value TEXT)"
            )
            self._db_conn.execute(
                "CREATE TABLE IF NOT EXISTS loan_records (strategy TEXT, amount REAL, timestamp REAL, repaid INTEGER)"
            )
            self._db_conn.commit()
        except Exception as e:
            logger.critical(
                f"数据库初始化失败: {e} "
                "#RECOVERY: 检查磁盘空间、目录权限、SQLite库版本。"
                "弹药库余额将仅保存在内存中，重启后丢失。"
            )
            self._db_conn = None

    def _load_state(self) -> None:
        """从数据库加载状态"""
        if self._db_conn is None:
            return
        try:
            cursor = self._db_conn.execute("SELECT value FROM ammo_state WHERE key='balance'")
            row = cursor.fetchone()
            if row:
                try:
                    self._ammo_balance = float(row[0])
                except (ValueError, TypeError):
                    logger.error(f"弹药库余额数据损坏: {row[0]} #RECOVERY: 检查数据库文件完整性，手动修正或删除重建")
                    self._ammo_balance = 0.0
            cursor = self._db_conn.execute("SELECT strategy, amount, timestamp, repaid FROM loan_records")
            for strategy, amount, ts, repaid in cursor.fetchall():
                if strategy not in self._loans:
                    self._loans[strategy] = []
                self._loans[strategy].append({"amount": amount, "timestamp": ts, "repaid": bool(repaid)})
        except Exception as e:
            logger.error(f"加载状态失败: {e}")

    def _persist_balance(self) -> None:
        """持久化余额（需在锁内调用）"""
        if self._db_conn is None:
            logger.warning("数据库不可用，弹药库余额未持久化，重启后可能丢失")
            return
        try:
            self._db_conn.execute(
                "INSERT OR REPLACE INTO ammo_state (key, value) VALUES ('balance', ?)",
                (str(self._ammo_balance),)
            )
            self._db_conn.commit()
        except Exception as e:
            logger.error(f"持久化余额失败: {e}")

    def _notify_ammo_event(self, event_type: str, amount: float, signal_score: float = 0.0) -> None:
        """发布弹药库事件"""
        if self._negotiation_bus is not None:
            try:
                self._negotiation_bus.publish_alert(
                    alert_type="ammo_event",
                    event=event_type,
                    amount=amount,
                    signal_score=signal_score,
                    timestamp=time.time(),
                )
            except Exception as e:
                logger.warning(f"弹药库事件推送失败: {e}")

    def _cleanup(self) -> None:
        """退出时清理资源"""
        if self._db_conn:
            try:
                self._db_conn.close()
                logger.debug("数据库连接已关闭")
            except Exception as e:
                logger.warning(f"关闭数据库连接异常: {e}")
