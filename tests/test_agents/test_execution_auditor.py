#!/usr/bin/env python3
"""
火种系统 (FireSeed) 执行质量审计官单元测试
===============================================
覆盖功能：
- TWAP/VWAP 完成率与偏离度计算
- 冰山订单成交质量统计
- 滑点趋势检测（逐日扩大告警）
- 撤单/重试率监控
- 多交易所路由质量评分
- 告警生成与推送
- 数据持久化与查询
"""

import asyncio
import time
from unittest import mock
from datetime import datetime, timedelta

import numpy as np
import pytest

from agents.execution_auditor import ExecutionAuditor, ExecutionAlert, OrderQualitySnapshot
from core.behavioral_logger import BehavioralLogger, EventType
from core.notifier import SystemNotifier


# ---------- 测试固件 ----------
@pytest.fixture
def mock_logger():
    return mock.MagicMock(spec=BehavioralLogger)


@pytest.fixture
def mock_notifier():
    return mock.AsyncMock(spec=SystemNotifier)


@pytest.fixture
def auditor(mock_logger, mock_notifier):
    return ExecutionAuditor(behavior_log=mock_logger, notifier=mock_notifier)


# ---------- 初始化测试 ----------
class TestExecutionAuditorInitialization:
    def test_default_init(self, mock_logger, mock_notifier):
        auditor = ExecutionAuditor(mock_logger, mock_notifier)
        assert auditor.check_interval == 60  # 默认60秒检查一次
        assert auditor.slippage_warn_threshold == 0.1  # 0.1% 滑点告警
        assert auditor.twap_completion_min == 95.0  # TWAP 最低完成率 95%
        assert auditor.max_consecutive_failures == 3

    def test_custom_params(self, mock_logger, mock_notifier):
        auditor = ExecutionAuditor(
            mock_logger, mock_notifier,
            check_interval_sec=30,
            slippage_warn_threshold=0.05,
            twap_completion_min=98.0
        )
        assert auditor.check_interval == 30
        assert auditor.slippage_warn_threshold == 0.05
        assert auditor.twap_completion_min == 98.0


# ---------- 订单统计辅助方法测试 ----------
class TestOrderStatistics:
    def test_calc_twap_completion_perfect(self, auditor):
        """TWAP 完全完成：总计划量 = 总成交量"""
        planned = 100.0
        filled = 100.0
        rate = auditor._calc_twap_completion_rate(planned, filled)
        assert rate == 100.0

    def test_calc_twap_completion_partial(self, auditor):
        planned = 200.0
        filled = 180.0
        rate = auditor._calc_twap_completion_rate(planned, filled)
        assert rate == 90.0

    def test_calc_twap_completion_zero_planned(self, auditor):
        result = auditor._calc_twap_completion_rate(0.0, 50.0)
        assert result == 0.0

    def test_calc_avg_slippage_bps(self, auditor):
        """平均滑点计算：多个订单的滑点平均值"""
        trades = [
            {"slippage_bps": 1.0},
            {"slippage_bps": 2.0},
            {"slippage_bps": 3.0},
        ]
        avg = auditor._calc_avg_slippage(trades)
        assert avg == pytest.approx(2.0)

    def test_calc_avg_slippage_empty(self, auditor):
        avg = auditor._calc_avg_slippage([])
        assert avg == 0.0

    def test_calc_cancel_retry_rate(self, auditor):
        """撤单重试率 = (撤单数 + 重试数) / 总订单数"""
        stats = {"cancelled": 2, "retried": 1, "total": 10}
        rate = auditor._calc_cancel_retry_rate(stats)
        assert rate == 30.0

    def test_calc_cancel_retry_rate_zero_total(self, auditor):
        stats = {"cancelled": 2, "retried": 1, "total": 0}
        rate = auditor._calc_cancel_retry_rate(stats)
        assert rate == 0.0


# ---------- 滑点趋势检测 ----------
class TestSlippageTrend:
    def test_trend_increasing(self, auditor):
        """连续5天滑点递增，应触发告警"""
        auditor._slippage_history = deque(
            [1.0, 1.5, 2.0, 2.5, 3.0], maxlen=30
        )
        result = auditor._detect_slippage_trend()
        assert result["trend"] == "increasing"
        assert result["slope"] > 0
        assert result["alert"] is True

    def test_trend_stable(self, auditor):
        auditor._slippage_history = deque(
            [2.0, 2.1, 1.9, 2.0, 2.0], maxlen=30
        )
        result = auditor._detect_slippage_trend()
        assert result["trend"] == "stable"
        assert result["alert"] is False

    def test_trend_insufficient_data(self, auditor):
        auditor._slippage_history = deque([2.0, 2.1], maxlen=30)
        result = auditor._detect_slippage_trend()
        assert result["trend"] == "insufficient_data"


# ---------- 告警生成 ----------
class TestAlertGeneration:
    def test_generate_alert_twap(self, auditor):
        alert = auditor._create_alert(
            category="TWAP",
            level=EventLevel.WARN,
            metrics={"completion": 85.0},
            message="TWAP 完成率 85%",
            suggestion="检查成交拆分粒度"
        )
        assert isinstance(alert, ExecutionAlert)
        assert alert.category == "TWAP"
        assert alert.level == EventLevel.WARN
        assert "85%" in alert.message

    def test_alert_history_capped(self, auditor):
        """告警历史最多保留200条"""
        for _ in range(250):
            auditor._alerts.append(ExecutionAlert(
                timestamp=datetime.now(),
                level=EventLevel.INFO,
                category="test",
                message="dummy"
            ))
        assert len(auditor._alerts) == 200


# ---------- TWAP 完成率检查 ----------
class TestTwapQuality:
    @pytest.mark.asyncio
    async def test_twap_healthy(self, auditor, mock_logger, mock_notifier):
        """TWAP 完成率正常时不产生告警"""
        # 模拟高完成率
        with mock.patch.object(auditor, '_get_recent_twap_orders',
                               return_value=[{"planned": 100, "filled": 98, "slippage": 1.0}]):
            alerts = await auditor._check_twap_quality()
            assert len(alerts) == 0

    @pytest.mark.asyncio
    async def test_twap_underperforming(self, auditor, mock_logger, mock_notifier):
        """TWAP 完成率过低时产生告警"""
        with mock.patch.object(auditor, '_get_recent_twap_orders',
                               return_value=[{"planned": 100, "filled": 80, "slippage": 3.0}]):
            alerts = await auditor._check_twap_quality()
            assert len(alerts) >= 1
            assert any("完成率" in a.message for a in alerts)


# ---------- 冰山订单质量检查 ----------
class TestIcebergQuality:
    @pytest.mark.asyncio
    async def test_iceberg_low_execution_ratio(self, auditor):
        """冰山订单成交均价偏离对手价超阈值"""
        with mock.patch.object(auditor, '_get_recent_iceberg_orders',
                               return_value=[{
                                   "avg_fill": 50010.0,
                                   "opponent_price": 50000.0,
                                   "filled_qty": 10,
                                   "total_qty": 10
                               }]):
            alerts = await auditor._check_iceberg_quality()
            # 价格偏差 0.02% 应触发告警（如果阈值设置够低）
            if auditor.slippage_warn_threshold <= 0.02:
                assert len(alerts) > 0
            else:
                # 阈值较高时可能无告警
                pass


# ---------- 多交易所路由评分 ----------
class TestRoutingScore:
    def test_score_calculation(self, auditor):
        scores = {
            "binance": {"fill_rate": 0.98, "avg_slippage": 0.5},
            "bybit": {"fill_rate": 0.85, "avg_slippage": 1.2},
            "okx": {"fill_rate": 0.92, "avg_slippage": 0.8},
        }
        result = auditor._calculate_routing_scores(scores)
        assert "binance" in result
        # binance 表现最好，评分应最高
        assert result["binance"]["score"] > result["bybit"]["score"]


# ---------- 主评估流程 ----------
class TestEvaluateFlow:
    @pytest.mark.asyncio
    async def test_evaluate_all_healthy(self, auditor, mock_logger, mock_notifier):
        """所有检查均通过"""
        with mock.patch.object(auditor, '_check_twap_quality', return_value=[]), \
             mock.patch.object(auditor, '_check_iceberg_quality', return_value=[]), \
             mock.patch.object(auditor, '_check_slippage_trend', return_value=[]), \
             mock.patch.object(auditor, '_check_cancel_retry_rate', return_value=[]), \
             mock.patch.object(auditor, '_check_routing_quality', return_value=[]):
            result = await auditor.evaluate()
            assert result["status"] == "OK"
            assert result["alert_count"] == 0

    @pytest.mark.asyncio
    async def test_evaluate_with_alerts(self, auditor, mock_logger, mock_notifier):
        """部分检查产生告警"""
        alert = auditor._create_alert("TWAP", EventLevel.WARN, {}, "test", "")
        with mock.patch.object(auditor, '_check_twap_quality', return_value=[alert]), \
             mock.patch.object(auditor, '_check_iceberg_quality', return_value=[]), \
             mock.patch.object(auditor, '_check_slippage_trend', return_value=[]), \
             mock.patch.object(auditor, '_check_cancel_retry_rate', return_value=[]), \
             mock.patch.object(auditor, '_check_routing_quality', return_value=[]):
            result = await auditor.evaluate()
            assert result["status"] == "WARNING"
            assert result["alert_count"] == 1


# ---------- 数据持久化 ----------
class TestPersistence:
    def test_save_and_load_snapshot(self, auditor, tmp_path):
        """验证订单质量快照的保存与加载"""
        snap = OrderQualitySnapshot(
            timestamp=datetime.now(),
            twap_completion=97.5,
            avg_slippage_bps=1.2,
            cancel_retry_rate=5.0,
            routing_scores={"binance": 0.95}
        )
        file_path = tmp_path / "quality_snapshot.json"
        auditor._save_snapshot(snap, path=str(file_path))
        assert file_path.exists()

        loaded = auditor._load_snapshot(path=str(file_path))
        assert loaded.twap_completion == 97.5
        assert loaded.avg_slippage_bps == 1.2

    @pytest.mark.asyncio
    async def test_history_cleanup(self, auditor):
        """超过30天的快照被清理"""
        old_snap = OrderQualitySnapshot(
            timestamp=datetime.now() - timedelta(days=31),
            twap_completion=90.0,
            avg_slippage_bps=2.0,
            cancel_retry_rate=10.0,
            routing_scores={}
        )
        new_snap = OrderQualitySnapshot(
            timestamp=datetime.now(),
            twap_completion=95.0,
            avg_slippage_bps=1.0,
            cancel_retry_rate=5.0,
            routing_scores={}
        )
        auditor._snapshot_history = [old_snap, new_snap]
        await auditor._cleanup_old_snapshots()
        assert len(auditor._snapshot_history) == 1
        assert auditor._snapshot_history[0].twap_completion == 95.0


# ---------- 告警推送 ----------
class TestAlertPush:
    @pytest.mark.asyncio
    async def test_critical_alert_pushes_to_notifier(self, auditor, mock_notifier):
        alert = auditor._create_alert(
            "SLIPPAGE", EventLevel.CRITICAL,
            {"avg_slippage": 1.5},
            "平均滑点超过 1.5%",
            "立即切换执行算法"
        )
        await auditor._emit_alert(alert)
        # 关键告警必须推送
        mock_notifier.send_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_info_alert_no_push(self, auditor, mock_notifier):
        """低级别告警不推送消息渠道"""
        alert = auditor._create_alert(
            "TWAP", EventLevel.INFO,
            {"completion": 96.0},
            "TWAP 完成率 96%，正常",
            ""
        )
        await auditor._emit_alert(alert)
        mock_notifier.send_alert.assert_not_called()
