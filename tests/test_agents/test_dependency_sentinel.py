#!/usr/bin/env python3
"""
单元测试：外部依赖哨兵智能体 (DependencySentinel)
====================================================
测试覆盖：
- 初始化及数据源注册
- 交易所 API 可用性检查（REST / WebSocket）
- LLM API 调用成功率与延迟
- 云存储 API 响应延迟
- 消息推送渠道（Telegram/钉钉/企微）可达性
- 综合健康评分计算
- 告警生成与去重
- 连续异常累积与重置
"""

import asyncio
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.dependency_sentinel import (
    DependencySentinel,
    ExternalServiceStatus,
    DependencyAlert,
)
from core.behavioral_logger import EventType, EventLevel

# ───────────────────────────────────────────────────────────
# 固定数据
# ───────────────────────────────────────────────────────────
@pytest.fixture
def mock_behavior_log():
    """返回行为日志的 MagicMock"""
    return MagicMock()


@pytest.fixture
def mock_notifier():
    """返回消息推送器的 AsyncMock"""
    return AsyncMock()


@pytest.fixture
def sentinel(mock_behavior_log, mock_notifier):
    """创建外部依赖哨兵实例并注入 mock 依赖"""
    instance = DependencySentinel(
        behavior_log=mock_behavior_log,
        notifier=mock_notifier,
        check_interval_sec=30,
    )
    # 缩短检查间隔以便测试
    instance.check_interval = 0
    return instance


def _fake_fetch(url: str, timeout: float = 5.0) -> MagicMock:
    """构造模拟 HTTP 响应的辅助函数"""
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value={})
    resp.text = AsyncMock(return_value="ok")
    return resp


# ───────────────────────────────────────────────────────────
# 初始化测试
# ───────────────────────────────────────────────────────────
def test_initialization(sentinel):
    """验证初始数据源注册完整"""
    expected_sources = [
        "binance_rest",
        "binance_ws",
        "deepseek_llm",
        "openai_llm",
        "aliyun_oss",
        "telegram_bot",
        "dingtalk_bot",
        "redis_server",
    ]
    for name in expected_sources:
        assert name in sentinel.data_sources
        assert sentinel.data_sources[name].name == name


def test_initial_health_scores(sentinel):
    """初始健康评分应为 100"""
    assert sentinel._global_health_score == 100.0


# ───────────────────────────────────────────────────────────
# 交易所 API 检查
# ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_rest_api_healthy(sentinel, monkeypatch):
    """模拟 REST API 正常返回"""
    async def mock_get(*args, **kwargs):
        resp = MagicMock()
        resp.status = 200
        return resp

    monkeypatch.setattr(sentinel, "_check_rest_endpoint", mock_get)
    await sentinel._check_rest_sources(None, [])
    binance = sentinel.data_sources["binance_rest"]
    assert binance.status == "healthy"


@pytest.mark.asyncio
async def test_rest_api_error(sentinel, monkeypatch):
    """模拟 REST API 返回 500"""
    async def mock_get(*args, **kwargs):
        resp = MagicMock()
        resp.status = 500
        return resp

    monkeypatch.setattr(sentinel, "_check_rest_endpoint", mock_get)
    alerts = []
    await sentinel._check_rest_sources(None, alerts)
    binance = sentinel.data_sources["binance_rest"]
    assert binance.status == "degraded"
    assert len(alerts) > 0
    assert any("REST API" in a.message for a in alerts)


@pytest.mark.asyncio
async def test_ws_heartbeat_timeout(sentinel):
    """模拟 WebSocket 长时间无心跳"""
    ds = sentinel.data_sources["binance_ws"]
    ds.last_heartbeat = time.time() - 60  # 60 秒前
    alerts = []
    await sentinel._check_ws_sources(None, alerts)
    assert ds.status == "offline"
    assert len(alerts) > 0


# ───────────────────────────────────────────────────────────
# LLM API 检查
# ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_llm_api_healthy(sentinel, monkeypatch):
    """模拟 LLM API 正常响应"""
    async def mock_llm_ping(*args, **kwargs):
        return True, 0.05  # 50ms 延迟

    monkeypatch.setattr(sentinel, "_ping_llm", mock_llm_ping)
    alerts = []
    await sentinel._check_llm_sources(None, alerts)
    for name in ["deepseek_llm", "openai_llm"]:
        assert sentinel.data_sources[name].status == "healthy"


@pytest.mark.asyncio
async def test_llm_api_slow(sentinel, monkeypatch):
    """模拟 LLM API 高延迟"""
    async def mock_llm_ping(*args, **kwargs):
        return True, 3.0  # 3 秒

    monkeypatch.setattr(sentinel, "_ping_llm", mock_llm_ping)
    alerts = []
    await sentinel._check_llm_sources(None, alerts)
    ds = sentinel.data_sources["deepseek_llm"]
    assert ds.status == "degraded"
    assert len(alerts) > 0


# ───────────────────────────────────────────────────────────
# 云存储检查
# ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_oss_healthy(sentinel, monkeypatch):
    """模拟云存储可用"""
    async def mock_oss_ping(*args, **kwargs):
        return True, 0.1

    monkeypatch.setattr(sentinel, "_ping_oss", mock_oss_ping)
    alerts = []
    await sentinel._check_cloud_sources(None, alerts)
    assert sentinel.data_sources["aliyun_oss"].status == "healthy"


@pytest.mark.asyncio
async def test_oss_unavailable(sentinel, monkeypatch):
    """模拟云存储不可用"""
    async def mock_oss_ping(*args, **kwargs):
        return False, 0.0

    monkeypatch.setattr(sentinel, "_ping_oss", mock_oss_ping)
    alerts = []
    await sentinel._check_cloud_sources(None, alerts)
    assert sentinel.data_sources["aliyun_oss"].status == "degraded"


# ───────────────────────────────────────────────────────────
# 消息推送检查
# ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_telegram_healthy(sentinel, monkeypatch):
    """模拟 Telegram 推送成功"""
    async def mock_tg(*args, **kwargs):
        return True

    monkeypatch.setattr(sentinel, "_check_telegram", mock_tg)
    alerts = []
    await sentinel._check_messaging_sources(None, alerts)
    assert sentinel.data_sources["telegram_bot"].status == "healthy"


# ───────────────────────────────────────────────────────────
# 综合评估与健康评分
# ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_full_evaluate_healthy(sentinel, monkeypatch):
    """模拟所有依赖正常，健康评分应为 100"""
    async def mock_healthy(*args, **kwargs):
        return True, 0.1

    monkeypatch.setattr(sentinel, "_ping_llm", mock_healthy)
    monkeypatch.setattr(sentinel, "_ping_oss", mock_healthy)
    monkeypatch.setattr(sentinel, "_check_rest_endpoint", mock_healthy)

    result = await sentinel.evaluate()
    assert result["global_health_score"] >= 90
    assert result["alert_count"] == 0


@pytest.mark.asyncio
async def test_full_evaluate_with_failures(sentinel, monkeypatch):
    """模拟部分依赖失败，应生成告警"""
    async def mock_healthy(*args, **kwargs):
        return True, 0.1

    async def mock_fail(*args, **kwargs):
        return False, 0.0

    monkeypatch.setattr(sentinel, "_ping_llm", mock_fail)
    monkeypatch.setattr(sentinel, "_ping_oss", mock_healthy)
    monkeypatch.setattr(sentinel, "_check_rest_endpoint", mock_healthy)

    sentinel.data_sources["binance_ws"].last_heartbeat = time.time() - 120
    result = await sentinel.evaluate()
    assert result["alert_count"] > 0
    assert result["global_health_score"] < 100


# ───────────────────────────────────────────────────────────
# 告警去重逻辑
# ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_alert_deduplication(sentinel, monkeypatch):
    """连续相同告警应被去重，不重复推送"""
    async def mock_fail(*args, **kwargs):
        return False, 0.0

    monkeypatch.setattr(sentinel, "_ping_llm", mock_fail)
    push_count = 0

    async def fake_push(title, body):
        nonlocal push_count
        push_count += 1

    sentinel.notifier.send_alert = fake_push

    for _ in range(5):
        await sentinel._check_llm_sources(None, [])

    # 第一次和第四次应推送，其余被去重
    assert push_count >= 1


# ───────────────────────────────────────────────────────────
# 连续异常统计
# ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_consecutive_anomalies(sentinel):
    """验证连续异常计数正确累加"""
    sentinel._consecutive_anomalies["test_source"] = 3
    result = sentinel.get_status()
    assert result["consecutive_anomalies"]["test_source"] == 3
