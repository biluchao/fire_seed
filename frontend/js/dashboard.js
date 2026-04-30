/* =============================================================================
   火种系统 (FireSeed) 仪表盘实时数据管理
   功能：
     - WebSocket 连接管理 (自动重连、心跳)
     - REST API 轮询备选
     - 各卡片数据精准更新 (账户、风险、交易、系统状态等)
     - 行为日志实时推送
     - 迷你收益曲线动态更新
   ============================================================================= */

(function (global) {
  'use strict';

  // 配置常量
  const CONFIG = {
    WS_URL: `ws${location.protocol === 'https:' ? 's' : ''}://${location.host}/ws/status`,
    REST_BASE: '/api',
    HEARTBEAT_INTERVAL: 10000,   // 10 秒心跳
    RECONNECT_DELAY: 2000,       // 重连延迟
    MAX_RECONNECT_ATTEMPTS: 5,
    POLL_INTERVAL: 3000,         // 轮询间隔 (毫秒，仅在 WS 不可用时)
    LOG_MAX_ITEMS: 20,           // 行为日志最大展示条数
  };

  // 内部状态
  let ws = null;
  let reconnectAttempts = 0;
  let heartbeatTimer = null;
  let pollTimer = null;
  let usePolling = false;
  let miniChart = null;
  let miniChartData = [];
  const logBuffer = [];

  // ======================== 核心初始化 ========================
  function init() {
    console.log('[Dashboard] Initializing real-time data stream...');
    initMiniChart();
    connectWebSocket();
    // 如果 WebSocket 连接失败，自动切换轮询
    setTimeout(() => {
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        enablePolling();
      }
    }, 5000);
  }

  // ======================== WebSocket 管理 ========================
  function connectWebSocket() {
    if (ws) {
      ws.close();
      ws = null;
    }
    try {
      ws = new WebSocket(CONFIG.WS_URL);
    } catch (e) {
      console.error('[Dashboard] WebSocket creation failed:', e);
      enablePolling();
      return;
    }

    ws.onopen = () => {
      console.log('[Dashboard] WebSocket connected');
      reconnectAttempts = 0;
      startHeartbeat();
      if (usePolling) {
        disablePolling();
      }
    };

    ws.onmessage = (event) => {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (e) {
        console.warn('[Dashboard] Failed to parse WS message:', e);
        return;
      }
      handleServerData(data);
    };

    ws.onerror = (err) => {
      console.error('[Dashboard] WebSocket error:', err);
      // 错误时不清除连接，等待 onclose 处理
    };

    ws.onclose = (event) => {
      console.warn(`[Dashboard] WebSocket closed: code=${event.code}, reason=${event.reason}`);
      clearInterval(heartbeatTimer);
      if (reconnectAttempts < CONFIG.MAX_RECONNECT_ATTEMPTS) {
        reconnectAttempts++;
        console.log(`[Dashboard] Attempting reconnect ${reconnectAttempts}/${CONFIG.MAX_RECONNECT_ATTEMPTS}...`);
        setTimeout(connectWebSocket, CONFIG.RECONNECT_DELAY);
      } else {
        console.error('[Dashboard] Max reconnect attempts reached. Switching to polling.');
        enablePolling();
      }
    };
  }

  function startHeartbeat() {
    clearInterval(heartbeatTimer);
    heartbeatTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'ping' }));
      }
    }, CONFIG.HEARTBEAT_INTERVAL);
  }

  // ======================== 消息处理分发 ========================
  function handleServerData(data) {
    if (!data || !data.type) return;

    switch (data.type) {
      case 'account_update':
        updateAccountDisplay(data.payload);
        break;
      case 'risk_update':
        updateRiskDisplay(data.payload);
        break;
      case 'trade_update':
        updateTradeDisplay(data.payload);
        break;
      case 'system_status':
        updateSystemStatus(data.payload);
        break;
      case 'behavior_log':
        appendBehaviorLog(data.payload);
        break;
      case 'equity_curve':
        updateMiniChart(data.payload);
        break;
      case 'order_update':
        updateOrderList(data.payload);
        break;
      case 'pong':
        // 心跳响应
        break;
      default:
        console.log('[Dashboard] Unhandled data type:', data.type);
    }
  }

  // ======================== 卡片更新函数 ========================
  function updateAccountDisplay(payload) {
    // 精准更新账户仪表盘的值
    safeUpdateText('accountEquity', `$${(payload.equity / 1000).toFixed(1)}k`);
    safeUpdateText('accountAvailable', `$${(payload.available / 1000).toFixed(1)}k`);
    safeUpdateText('accountPnl', `+$${(payload.pnl / 1000).toFixed(1)}k`);
    safeUpdateText('accountMargin', payload.marginRate + '%');
  }

  function updateRiskDisplay(payload) {
    // 回撤进度条
    const ddFill = document.querySelector('#riskDrawdownBar .progress-fill');
    if (ddFill) {
      ddFill.style.width = Math.min(100, (payload.drawdown / payload.drawdownWarn) * 100) + '%';
    }
    safeUpdateText('riskDrawdownVal', `${payload.drawdown}%`);
    safeUpdateText('riskDrawdownWarn', `${payload.drawdownWarn}%`);

    // 日亏损进度条
    const lossFill = document.querySelector('#riskLossBar .progress-fill');
    if (lossFill) {
      lossFill.style.width = Math.min(100, (payload.dailyLoss / payload.dailyLimit) * 100) + '%';
    }
    safeUpdateText('riskDailyLoss', `${payload.dailyLoss}%`);
    safeUpdateText('riskDailyLimit', `${payload.dailyLimit}%`);

    safeUpdateText('riskVaR', `$${payload.var99.toLocaleString()}`);
  }

  function updateTradeDisplay(payload) {
    safeUpdateText('tradeTodayCount', `${payload.today}笔`);
    safeUpdateText('tradeWinRate', `${payload.winRate}%`);
    safeUpdateText('tradeProfit', `+$${payload.profit.toLocaleString()}`);
  }

  function updateSystemStatus(payload) {
    // 顶栏状态
    safeUpdateText('latText', `${payload.latency}ms`);
    safeUpdateText('lossText', `${payload.packetLoss.toFixed(2)}%`);
    safeUpdateText('cpuVal', `${payload.cpu}%`);
    safeUpdateText('memVal', `${payload.memory.toFixed(1)}/8G`);
    safeUpdateText('diskVal', `${payload.disk}%`);

    // 延迟指示灯
    const led = document.getElementById('latLed');
    if (led) {
      led.className = 'led ' + (payload.latency < 30 ? 'green' : payload.latency < 60 ? 'yellow' : 'red');
    }

    // 系统健康度
    if (payload.healthScore !== undefined) {
      safeUpdateText('healthScore', `${payload.healthScore}/100`);
      const healthBar = document.querySelector('#healthBar .progress-fill');
      if (healthBar) healthBar.style.width = `${payload.healthScore}%`;
    }
    if (payload.cpuTemp) safeUpdateText('cpuTemp', payload.cpuTemp);
    if (payload.diskIO) safeUpdateText('diskIO', payload.diskIO);
    if (payload.uptime) safeUpdateText('uptime', payload.uptime);
  }

  function updateOrderList(payload) {
    // payload: { recentTrades: [...] }
    if (!payload.recentTrades || !Array.isArray(payload.recentTrades)) return;
    const container = document.getElementById('orderListContainer');
    if (!container) return;

    // 只更新最近12条
    const trades = payload.recentTrades.slice(0, 12);
    container.innerHTML = trades.map(t => `
      <div class="row-flex" style="font-size:0.7rem; padding:3px 0;">
        <span>${t.time}</span>
        <span style="color:${t.side === '多' ? '#10b981' : '#ef4444'};">${t.side}</span>
        <span>${t.pair}</span>
        <span style="color:${t.pnl.startsWith('+') ? '#10b981' : '#ef4444'};">${t.pnl}</span>
      </div>
    `).join('');
  }

  // ======================== 行为日志 ========================
  function appendBehaviorLog(payload) {
    // payload: { time, dot, msg }
    if (!payload.msg) return;
    logBuffer.push(payload);
    // 保持最多 MAX_LOG_ITEMS 条
    while (logBuffer.length > CONFIG.LOG_MAX_ITEMS) {
      logBuffer.shift();
    }
    renderBehaviorLog();
  }

  function renderBehaviorLog() {
    const logList = document.getElementById('logList');
    if (!logList) return;
    // 倒序显示
    const items = [...logBuffer].reverse();
    logList.innerHTML = items.map(l => `
      <div class="log-item">
        <span class="log-time">${l.time}</span>
        <span class="log-dot ${l.dot || 'dot-system'}"></span>
        <span>${l.msg}</span>
      </div>
    `).join('');
  }

  // ======================== 迷你收益曲线 ========================
  function initMiniChart() {
    const canvas = document.getElementById('miniChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    miniChartData = [];
    miniChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: Array.from({ length: 50 }, () => ''),
        datasets: [{
          data: miniChartData,
          borderColor: '#2563eb',
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.3,
          fill: false,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 200 },
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: { x: { display: false }, y: { display: false } }
      }
    });
  }

  function updateMiniChart(payload) {
    // payload: { equity: number }
    if (!miniChart || !payload || payload.equity === undefined) return;
    miniChartData.push(payload.equity);
    if (miniChartData.length > 50) {
      miniChartData.shift();
    }
    miniChart.update();
  }

  // ======================== 轮询回退模式 ========================
  function enablePolling() {
    if (usePolling) return;
    console.warn('[Dashboard] Enabling REST polling fallback');
    usePolling = true;
    pollTimer = setInterval(fetchAllData, CONFIG.POLL_INTERVAL);
    fetchAllData(); // 立即获取一次
  }

  function disablePolling() {
    if (!usePolling) return;
    console.log('[Dashboard] Disabling polling, WebSocket active');
    usePolling = false;
    clearInterval(pollTimer);
    pollTimer = null;
  }

  async function fetchAllData() {
    try {
      const [statusRes, orderRes] = await Promise.all([
        fetch(`${CONFIG.REST_BASE}/status`).then(r => r.json()),
        fetch(`${CONFIG.REST_BASE}/orders?limit=12`).then(r => r.json()),
      ]);
      if (statusRes) {
        handleServerData({ type: 'system_status', payload: statusRes.system });
        handleServerData({ type: 'account_update', payload: statusRes.account });
        handleServerData({ type: 'risk_update', payload: statusRes.risk });
        handleServerData({ type: 'trade_update', payload: statusRes.trades });
        if (statusRes.equity) handleServerData({ type: 'equity_curve', payload: { equity: statusRes.equity } });
      }
      if (orderRes && orderRes.trades) {
        handleServerData({ type: 'order_update', payload: { recentTrades: orderRes.trades } });
      }
    } catch (err) {
      console.error('[Dashboard] Polling fetch error:', err);
    }
  }

  // ======================== 辅助函数 ========================
  function safeUpdateText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  // 暴露接口给 app.js 设置初始数据
  global.Dashboard = {
    init,
    updateMiniChart,
    appendBehaviorLog,
    safeUpdateText,
  };

})(window);

// 当 DOM 和 app.js 就绪后启动
document.addEventListener('DOMContentLoaded', () => {
  // 延迟启动，确保 app.js 的渲染已完成
  setTimeout(() => {
    if (!window.Dashboard || !window.Dashboard.init) return;
    window.Dashboard.init();
  }, 500);
});
