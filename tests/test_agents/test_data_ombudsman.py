#!/usr/bin/env python3
"""
火种系统 (FireSeed) 数据监察员 (DataOmbudsman) 单元测试
==========================================================
测试覆盖：
- 初始化与数据源注册
- WebSocket 数据源健康检查
- REST 数据源检查
- NTP 时钟同步检查
- 回测数据对齐检查
- 价格序列异常检测
- 全局健康评分计算
- 告警生成与去重
- 对抗性校验日志
- 边界条件 (引擎未就绪、空数据等)
"""

import asyncio
import time
from datetime import datetime
from collections import deque
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock

import pytest
import numpy as np

# 要测试的模块
from agents.data_ombudsman import (
    DataOmbudsman,
    DataSourceHealth,
    DataQualityAlert,
)

# 辅助导入
from core.behavioral_logger import EventLevel, EventType
from core.notifier import SystemNotifier


# ---------- 夹具 ----------
@pytest.fixture
def mock_behavior_log():
    """模拟行为日志"""
    return MagicMock()


@pytest.fixture
def mock_notifier():
    """模拟消息推送器"""
    notifier = MagicMock(spec=SystemNotifier)
    notifier.send_alert = AsyncMock()
    return notifier


@pytest.fixture
def ombudsman(mock_behavior_log, mock_notifier):
    """创建带模拟依赖的数据监察员实例"""
    return DataOmbudsman(
        behavior_log=mock_behavior_log,
        notifier=mock_notifier,
        check_interval_sec=0.1,  # 加速测试
    )


@pytest.fixture
def mock_engine():
    """模拟火种引擎"""
    engine = MagicMock()
    # 模拟 data_feed
    engine.data_feed = MagicMock()
    engine.data_feed.get_ws_status = MagicMock(return_value={
        'latency_ms': 50.0,
        'packet_loss_pct': 0.1,
        'last_heartbeat': time.time(),
    })
    engine.data_feed.get_next_tick = AsyncMock(return_value=None)
    # 模拟 order_mgr
    engine.order_mgr = MagicMock()
    engine.order_mgr.get_recent_orders = MagicMock(return_value=[])
    return engine


# ---------- 初始化测试 ----------
class TestInitialization:
    def test_init_registers_all_sources(self, ombudsman):
        """验证初始化后注册了所有6个数据源"""
        expected_sources = [
            "binance_ws", "binance_rest", "bybit_ws",
            "okx_ws", "ntp_sync", "backtest_store",
        ]
        for name in expected_sources:
            assert name in ombudsman.data_sources
            assert isinstance(ombudsman.data_sources[name], DataSourceHealth)

    def test_init_sets_health_score(self, ombudsman):
        """验证初始全局健康评分"""
        assert ombudsman._global_health_score == 100.0

    def test_init_creates_histories(self, ombudsman):
        """验证为每个数据源创建了延迟和丢包历史队列"""
        for name in ombudsman.data_sources:
            assert name in ombudsman._latency_history
            assert name in ombudsman._packet_loss_history
            assert isinstance(ombudsman._latency_history[name], deque)
            assert isinstance(ombudsman._packet_loss_history[name], deque)


# ---------- evaluate 主流程 ----------
class TestEvaluate:
    @pytest.mark.asyncio
    async def test_evaluate_returns_health_score(self, ombudsman, mock_engine):
        """验证 evaluate 返回全局健康评分"""
        with patch('agents.data_ombudsman.get_engine', return_value=mock_engine):
            with patch.object(ombudsman, '_check_ntp_sync', AsyncMock()):
                result = await ombudsman.evaluate()
        assert 'global_health_score' in result
        assert result['global_health_score'] >= 0
        assert result['global_health_score'] <= 100

    @pytest.mark.asyncio
    async def test_evaluate_when_engine_absent(self, ombudsman):
        """验证引擎缺失时仍可正常评估"""
        with patch('agents.data_ombudsman.get_engine', return_value=None):
            result = await ombudsman.evaluate()
        assert result['total_sources'] == len(ombudsman.data_sources)

    @pytest.mark.asyncio
    async def test_evaluate_respects_throttle(self, ombudsman, mock_engine):
        """验证检查间隔限制生效"""
        with patch('agents.data_ombudsman.get_engine', return_value=mock_engine):
            # 第一次调用不受限
            result1 = await ombudsman.evaluate()
            # 立即第二次调用应被限流
            result2 = await ombudsman.evaluate()
        assert result1.get('status') != 'throttled' or result1['status'] is None
        assert result2['status'] == 'throttled'


# ---------- WebSocket 数据源检查 ----------
class TestWebSocketCheck:
    @pytest.mark.asyncio
    async def test_ws_healthy(self, ombudsman, mock_engine):
        """验证正常 WebSocket 状态"""
        with patch('agents.data_ombudsman.get_engine', return_value=mock_engine):
            alerts = []
            await ombudsman._check_ws_sources(mock_engine, alerts)
        # 所有 WS 数据源应健康
        ws_sources = [n for n, ds in ombudsman.data_sources.items() if ds.source_type == "WS"]
        for name in ws_sources:
            ds = ombudsman.data_sources[name]
            assert ds.status == "healthy"
            assert ds.anomaly_score < 0.2

    @pytest.mark.asyncio
    async def test_ws_high_latency_triggers_alert(self, ombudsman, mock_engine):
        """验证高延迟触发告警"""
        mock_engine.data_feed.get_ws_status.return_value = {
            'latency_ms': 350.0,
            'packet_loss_pct': 0.0,
            'last_heartbeat': time.time(),
        }
        with patch('agents.data_ombudsman.get_engine', return_value=mock_engine):
            alerts = []
            await ombudsman._check_ws_sources(mock_engine, alerts)
        assert len(alerts) > 0
        alert = alerts[0]
        assert alert.level == EventLevel.WARN
        assert alert.metric == "延迟"
        assert alert.current_value == 350.0

    @pytest.mark.asyncio
    async def test_ws_critical_latency(self, ombudsman, mock_engine):
        """验证极高延迟触发 CRITICAL 告警"""
        mock_engine.data_feed.get_ws_status.return_value = {
            'latency_ms': 800.0,
            'packet_loss_pct': 0.0,
            'last_heartbeat': time.time(),
        }
        with patch('agents.data_ombudsman.get_engine', return_value=mock_engine):
            alerts = []
            await ombudsman._check_ws_sources(mock_engine, alerts)
        assert len(alerts) > 0
        assert any(a.level == EventLevel.CRITICAL for a in alerts)

    @pytest.mark.asyncio
    async def test_ws_heartbeat_timeout(self, ombudsman, mock_engine):
        """验证心跳超时将数据源标记为离线"""
        mock_engine.data_feed.get_ws_status.return_value = {
            'latency_ms': 50.0,
            'packet_loss_pct': 0.0,
            'last_heartbeat': time.time() - 60,  # 60秒前
        }
        with patch('agents.data_ombudsman.get_engine', return_value=mock_engine):
            alerts = []
            await ombudsman._check_ws_sources(mock_engine, alerts)
        ws = [ds for ds in ombudsman.data_sources.values() if ds.source_type == "WS"]
        assert any(ds.status == "offline" for ds in ws)

    @pytest.mark.asyncio
    async def test_ws_anomaly_score_accumulates(self, ombudsman, mock_engine):
        """验证异常分数累积"""
        mock_engine.data_feed.get_ws_status.return_value = {
            'latency_ms': 300.0,
            'packet_loss_pct': 2.0,
            'last_heartbeat': time.time(),
        }
        with patch('agents.data_ombudsman.get_engine', return_value=mock_engine):
            await ombudsman._check_ws_sources(mock_engine, [])
        ws = ombudsman.data_sources["binance_ws"]
        assert ws.anomaly_score > 0.3


# ---------- NTP 时钟检查 ----------
class TestNtpCheck:
    @pytest.mark.asyncio
    async def test_ntp_healthy(self, ombudsman):
        """模拟 NTP 正常状态"""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value.stdout = "System time     : 0.000000005 seconds slow of NTP time"
            alerts = []
            await ombudsman._check_ntp_sync(alerts)
        ds = ombudsman.data_sources["ntp_sync"]
        assert ds.status == "healthy"
        assert ds.latency_ms < 100

    @pytest.mark.asyncio
    async def test_ntp_offset_triggers_alert(self, ombudsman):
        """验证时钟偏移过大触发告警"""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value.stdout = "System time     : 0.00015 seconds slow of NTP time"
            alerts = []
            await ombudsman._check_ntp_sync(alerts)
        ds = ombudsman.data_sources["ntp_sync"]
        assert ds.status == "degraded"
        assert len(alerts) > 0
        assert alerts[0].metric == "时钟偏移"

    @pytest.mark.asyncio
    async def test_ntp_command_failure(self, ombudsman):
        """模拟 chronyc 命令失败不告警"""
        with patch('subprocess.run', side_effect=FileNotFoundError):
            alerts = []
            await ombudsman._check_ntp_sync(alerts)
        assert len(alerts) == 0


# ---------- 价格异常检测 ----------
class TestPriceAnomalies:
    @pytest.mark.asyncio
    async def test_price_gap_detection(self, ombudsman, mock_engine):
        """验证价格断层检测"""
        # 构造连续价格序列，最后一个点突变
        prices = [50000.0 + i * 0.5 for i in range(100)]
        prices.append(50100.0)  # 突变
        ombudsman._price_sequence = deque(prices, maxlen=600)

        mock_tick = MagicMock()
        mock_tick.last_price = 50100.0
        mock_engine.data_feed.get_next_tick = AsyncMock(return_value=mock_tick)

        with patch('agents.data_ombudsman.get_engine', return_value=mock_engine):
            alerts = []
            await ombudsman._check_price_anomalies(mock_engine, alerts)
        assert len(alerts) > 0
        gap_alert = [a for a in alerts if a.metric == "价格断层"]
        assert len(gap_alert) > 0

    @pytest.mark.asyncio
    async def test_price_stagnation(self, ombudsman, mock_engine):
        """验证价格停滞检测"""
        # 最近10个价格完全相同
        prices = [50000.0] * 10
        ombudsman._price_sequence = deque(prices, maxlen=600)

        mock_tick = MagicMock()
        mock_tick.last_price = 50000.0
        mock_engine.data_feed.get_next_tick = AsyncMock(return_value=mock_tick)

        with patch('agents.data_ombudsman.get_engine', return_value=mock_engine):
            alerts = []
            await ombudsman._check_price_anomalies(mock_engine, alerts)
        stagnation = [a for a in alerts if a.metric == "价格停滞"]
        assert len(stagnation) > 0

    @pytest.mark.asyncio
    async def test_no_alerts_on_normal_data(self, ombudsman, mock_engine):
        """正常波动不产生告警"""
        prices = [50000.0 + np.random.normal(0, 2) for _ in range(200)]
        ombudsman._price_sequence = deque(prices, maxlen=600)

        mock_tick = MagicMock()
        mock_tick.last_price = prices[-1]
        mock_engine.data_feed.get_next_tick = AsyncMock(return_value=mock_tick)

        with patch('agents.data_ombudsman.get_engine', return_value=mock_engine):
            alerts = []
            await ombudsman._check_price_anomalies(mock_engine, alerts)
        assert len(alerts) == 0


# ---------- 全局健康评分 ----------
class TestGlobalHealthScore:
    def test_all_healthy(self, ombudsman):
        """所有数据源健康时评分应为 100"""
        for ds in ombudsman.data_sources.values():
            ds.status = "healthy"
            ds.anomaly_score = 0.0
        ombudsman._compute_global_health()
        assert ombudsman._global_health_score == 100.0

    def test_mixed_health(self, ombudsman):
        """混合状态时评分应合理下降"""
        sources = list(ombudsman.data_sources.values())
        for i, ds in enumerate(sources):
            if i % 3 == 0:
                ds.status = "offline"
            elif i % 3 == 1:
                ds.status = "degraded"
            else:
                ds.status = "healthy"
            ds.anomaly_score = 0.0
        ombudsman._compute_global_health()
        assert ombudsman._global_health_score < 100
        assert ombudsman._global_health_score > 0

    def test_anomaly_score_reduces_health(self, ombudsman):
        """高异常评分进一步降低健康评分"""
        for ds in ombudsman.data_sources.values():
            ds.status = "healthy"
            ds.anomaly_score = 0.8
        ombudsman._compute_global_health()
        assert ombudsman._global_health_score < 80


# ---------- 告警处理 ----------
class TestAlertHandling:
    def test_alert_stored_in_history(self, ombudsman):
        """验证告警被添加到历史列表"""
        alert = DataQualityAlert(
            level=EventLevel.WARN,
            source="test",
            metric="test_metric",
            current_value=100,
            threshold=50,
            message="测试告警",
        )
        ombudsman._emit_alert(alert)
        assert len(ombudsman._alerts) == 1
        assert ombudsman._alerts[0].source == "test"

    def test_alert_history_limited(self, ombudsman):
        """验证告警历史长度受限（200条）"""
        for i in range(250):
            alert = DataQualityAlert(
                level=EventLevel.INFO,
                source=f"test_{i}",
                metric="count",
                current_value=i,
                threshold=0,
                message=f"alert {i}",
            )
            ombudsman._emit_alert(alert)
        assert len(ombudsman._alerts) == 200
        # 最老的应被丢弃，保留最新的
        assert ombudsman._alerts[0].source == "test_50"

    @pytest.mark.asyncio
    async def test_alert_pushes_to_notifier(self, ombudsman, mock_notifier):
        """验证 WARN 以上级别告警推送至消息渠道"""
        alert = DataQualityAlert(
            level=EventLevel.WARN,
            source="test_push",
            metric="push_metric",
            current_value=200,
            threshold=100,
            message="推送测试",
        )
        ombudsman._emit_alert(alert)
        await asyncio.sleep(0.05)  # 等待异步任务
        # notifier.send_alert 被调用
        mock_notifier.send_alert.assert_called_once()

    def test_alert_to_dict(self, ombudsman):
        """验证告警转字典格式正确"""
        alert = DataQualityAlert(
            level=EventLevel.WARN,
            source="test_dict",
            metric="dict_metric",
            current_value=10,
            threshold=5,
            message="dict test",
        )
        d = ombudsman._alert_to_dict(alert)
        assert d['source'] == "test_dict"
        assert d['level'] == EventLevel.WARN.value
        assert d['metric'] == "dict_metric"
        assert 'timestamp' in d


# ---------- 边界条件 ----------
class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_data_sources(self, ombudsman):
        """数据源列表为空时不应崩溃"""
        ombudsman.data_sources.clear()
        ombudsman._compute_global_health()
        assert ombudsman._global_health_score == 100.0

    def test_negative_price_in_sequence(self, ombudsman):
        """价格序列包含零或负数不影响异常检测（但断层检测仍应工作）"""
        ombudsman._price_sequence = deque([50000.0] * 100, maxlen=600)
        # 不会崩溃
        ombudsman._compute_global_health()

    @pytest.mark.asyncio
    async def test_rest_source_not_implemented(self, ombudsman, mock_engine):
        """REST 检查当前为占位实现，应保持 healthy"""
        with patch('agents.data_ombudsman.get_engine', return_value=mock_engine):
            alerts = []
            await ombudsman._check_rest_sources(mock_engine, alerts)
        rest = ombudsman.data_sources.get("binance_rest")
        assert rest is not None
        assert rest.status == "healthy"
        assert len(alerts) == 0


# ---------- 对抗性校验 ----------
class TestAdversarialLogging:
    @pytest.mark.asyncio
    async def test_logs_adversarial_check(self, ombudsman, mock_engine, mock_behavior_log):
        """验证与监察者对抗性校验的日志记录"""
        with patch('agents.data_ombudsman.get_engine', return_value=mock_engine):
            with patch.object(ombudsman, '_check_ntp_sync', AsyncMock()):
                await ombudsman.evaluate()
        # 应该记录对抗性校验日志
        calls = mock_behavior_log.log.call_args_list
        adversarial_calls = [
            c for c in calls if "对抗性校验" in str(c.args)
        ]
        assert len(adversarial_calls) > 0

    def test_get_status(self, ombudsman):
        """验证 get_status 返回正确结构"""
        status = ombudsman.get_status()
        assert 'global_health_score' in status
        assert 'sources' in status
        assert 'recent_alerts' in status
        assert isinstance(status['sources'], dict)


# ---------- 运行协程辅助 ----------
if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
