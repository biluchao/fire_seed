/* =============================================================================
   火种系统 (FireSeed) 驾驶舱主脚本
   功能：
     - 全局状态管理
     - 主界面卡片渲染与更新
     - 订单列表、迷你曲线、行为日志
     - 二级页面导航
     - 多账户切换、模式切换
     - AI助手交互
     - 状态栏实时刷新
   依赖：
     - Chart.js (CDN)
     - Petite-Vue (可选，当前纯原生实现)
     - style.css (样式)
     - auth.js (提供密码验证函数 verifyPassword())
     - assistant.js (提供 AI 后端交互函数 sendToAssistant())
   ============================================================================= */

(function () {
  // ======================== 全局状态 ========================
  // 模拟数据模块 (实盘时将替换为API调用)
  const AppState = {
    account: {
      equity: 531200,
      available: 412000,
      pnl: 14200,
      marginRate: '245%',
      netRate: '92%',
      fundingCost: -320,
    },
    risk: {
      drawdown: 4.2,
      drawdownWarn: 8,
      dailyLoss: 1.8,
      dailyLimit: 5,
      var99: 12600,
      cvar: 15800,
    },
    trades: {
      today: 16,
      winRate: 74,
      profit: 14200,
    },
    mode: {
      broker: 'virtual',        // 'virtual' | 'live'
      strategy: 'moderate',     // 'aggressive' | 'moderate'
      currentAccount: 'main',
    },
    accounts: ['main', 'sub1', 'sub2', 'sub3'],
    health: {
      score: 88,
      cpuTemp: '52°C',
      diskIO: '12ms',
      uptime: '7d 3h',
      status: '良好',
    },
    entropy: {
      score: 42,
      redundantCode: '3.2%',
      unusedFactors: 5,
      evolveFailRate: '18%',
      shadowReject: '22%',
      agentAbstain: '4%',
    },
    ai: {
      upload: 78,
      decision: 82,
      learn: 45,
    },
    council: {
      dominant: '屯币者',
      bias: '偏多',
      consensus: 78,
      next: '03:12:45',
    },
    recentTrades: [
      { time: '14:35', pair: 'BTCUSDT', side: '多', price: 63250, qty: 0.10, pnl: '+$180' },
      { time: '14:32', pair: 'BTCUSDT', side: '多', price: 63200.5, qty: 0.12, pnl: '+$340' },
      { time: '14:28', pair: 'ETHUSDT', side: '空', price: 4210.2, qty: 1.5, pnl: '+$220' },
      { time: '14:15', pair: 'BTCUSDT', side: '多', price: 63150, qty: 0.14, pnl: '+$180' },
      { time: '13:50', pair: 'SOLUSDT', side: '多', price: 142.3, qty: 20, pnl: '-$45' },
      { time: '13:30', pair: 'BTCUSDT', side: '空', price: 63080, qty: 0.1, pnl: '+$120' },
      { time: '13:10', pair: 'ETHUSDT', side: '多', price: 4180, qty: 2.0, pnl: '+$300' },
      { time: '12:45', pair: 'BTCUSDT', side: '空', price: 62900, qty: 0.05, pnl: '+$80' },
      { time: '12:20', pair: 'SOLUSDT', side: '空', price: 140.5, qty: 15, pnl: '+$65' },
      { time: '11:55', pair: 'ETHUSDT', side: '多', price: 4150, qty: 1.8, pnl: '-$200' },
      { time: '11:30', pair: 'BTCUSDT', side: '多', price: 62800, qty: 0.2, pnl: '+$500' },
      { time: '10:50', pair: 'BTCUSDT', side: '空', price: 62750, qty: 0.08, pnl: '-$90' },
    ],
    // 行为日志池 (按类别)
    logPools: {
      order: [
        { time: '14:35', dot: 'dot-exec', msg: '订单成交 BTCUSDT 多 0.1 @63250' },
        { time: '14:32', dot: 'dot-exec', msg: '订单成交 BTCUSDT 多 0.12 @63200.5' },
      ],
      system: [
        { time: '14:30', dot: 'dot-system', msg: '系统自检通过 CNN/SHM/DB' },
      ],
      agent: [
        { time: '14:10', dot: 'dot-agent', msg: '监察者：OBI因子PSI异常 0.38' },
      ],
      risk: [
        { time: '14:22', dot: 'dot-risk', msg: '流动性监控: 深度下降 8%' },
      ],
      evolution: [
        { time: '03:15', dot: 'dot-strategy', msg: '新因子进入影子验证' },
      ],
      council: [
        { time: '02:30', dot: 'dot-agent', msg: '议会投票通过 风险预算上调' },
      ],
      shadow: [
        { time: '01:00', dot: 'dot-strategy', msg: '幽灵对比: 新版夏普 1.35 vs 旧版 1.28' },
      ],
      strategy: [
        { time: '14:32', dot: 'dot-strategy', msg: '四维共振评分71 触发做多' },
      ],
    },
    currentLogFilter: 'order',
    navStack: [],
  };

  // ======================== DOM 元素 ========================
  const $ = (id) => document.getElementById(id);
  const homePage = $('homePage');
  const detailPage = $('detailPage');
  const backBtn = $('backBtn');
  const aiToggleBtn = $('aiToggleBtn');
  const aiChatCard = $('aiChatCard');
  const aiChatBody = $('aiChatBody');
  const aiInput = $('aiInput');
  const logDropdown = document.querySelector('.log-dropdown'); // 动态创建

  // ======================== 初始化 ========================
  function init() {
    // 欢迎动画
    setTimeout(() => {
      const overlay = $('welcomeOverlay');
      if (overlay) overlay.classList.add('hidden');
    }, 2000);

    // 渲染首屏
    renderMainCards();
    renderBehaviorLog();

    // 状态栏定时更新
    setInterval(updateStatusBar, 3000);

    // 绑定全局事件
    bindGlobalEvents();
  }

  function bindGlobalEvents() {
    // 返回按钮
    backBtn.addEventListener('click', () => navigateTo('home'));

    // AI 助手
    aiToggleBtn.addEventListener('click', () => {
      aiChatCard.classList.toggle('active');
    });
    $('aiCloseBtn').addEventListener('click', () => {
      aiChatCard.classList.remove('active');
    });
    $('aiSendBtn').addEventListener('click', handleAiSend);
    aiInput.addEventListener('keypress', (e) => {
      if (e.key === 'Enter') handleAiSend();
    });

    // 关闭日志下拉 (点击外部)
    document.addEventListener('click', (e) => {
      if (!e.target.closest('.log-filter-btn')) {
        const dd = document.querySelector('.log-dropdown.show');
        if (dd) dd.classList.remove('show');
      }
    });
  }

  // ======================== 主界面渲染 ========================
  function renderMainCards() {
    const grid = $('mainCards');
    if (!grid) return;

    const { account, risk, trades, mode, health, entropy, ai, council } = AppState;

    grid.innerHTML = `
      <div class="card" data-nav="accountDetail">
        <div class="card-header"><span class="card-title">账户仪表盘</span><div class="card-icon"><i class="fas fa-wallet"></i></div></div>
        <div class="row-flex"><span class="label">总权益</span><span class="value">$${(account.equity/1000).toFixed(1)}k</span></div>
        <div class="row-flex"><span class="label">浮动盈亏</span><span class="value" style="color:#10b981;">+$${(account.pnl/1000).toFixed(1)}k</span></div>
        <div class="row-flex"><span class="label">保证金率</span><span class="value">${account.marginRate}</span></div>
        <div class="row-flex"><span class="label">当前账户</span><span class="value">${mode.currentAccount}</span></div>
        <button class="btn btn-switch" id="switchAccountBtn" style="margin-top:6px;width:100%;">切换账户</button>
      </div>
      <div class="card" data-nav="tradesStats">
        <div class="card-header"><span class="card-title">交易统计</span><div class="card-icon" style="background:linear-gradient(145deg,#ea580c,#c2410c);"><i class="fas fa-chart-simple"></i></div></div>
        <div class="row-flex"><span class="label">今日成交</span><span class="value">${trades.today}笔</span></div>
        <div class="row-flex"><span class="label">胜率</span><span class="value">${trades.winRate}%</span></div>
        <div class="row-flex"><span class="label">盈利</span><span class="value" style="color:#10b981;">+$${trades.profit.toLocaleString()}</span></div>
        <button class="btn btn-switch ${mode.broker==='live'?'live':''}" id="toggleBrokerBtn" style="margin-top:6px;width:100%;">${mode.broker==='virtual'?'虚拟':'实盘'}</button>
      </div>
      <div class="card" data-nav="risk">
        <div class="card-header"><span class="card-title">风险监控</span><div class="card-icon" style="background:linear-gradient(145deg,#f97316,#ea580c);"><i class="fas fa-shield-halved"></i></div></div>
        <div class="progress-wrap"><div class="progress-label"><span>回撤 ${risk.drawdown}%</span><span>预警 ${risk.drawdownWarn}%</span></div><div class="progress-bar"><div class="progress-fill" style="width:${(risk.drawdown/risk.drawdownWarn)*100}%; background:#f59e0b;"></div></div></div>
        <div class="progress-wrap"><div class="progress-label"><span>日亏损 ${risk.dailyLoss}%</span><span>熔断 ${risk.dailyLimit}%</span></div><div class="progress-bar"><div class="progress-fill" style="width:${(risk.dailyLoss/risk.dailyLimit)*100}%; background:#ef4444;"></div></div></div>
      </div>
      <div class="card card-emergency" id="emergencyCloseCard">
        <div class="card-header"><span class="card-title">紧急干预</span></div>
        <div class="row-flex"><span class="label">一键平仓</span><span class="value">全部</span></div>
      </div>
      <div class="card" style="cursor:default;">
        <div class="card-header"><span class="card-title">收益曲线</span></div>
        <div style="height:100px;"><canvas id="miniChart"></canvas></div>
      </div>
      <div class="card order-card-span" id="orderCard">
        <div class="card-header"><span class="card-title">订单管理</span><div class="card-icon" style="background:linear-gradient(145deg,#0ea5e9,#0284c7);"><i class="fas fa-list-check"></i></div></div>
        <div style="max-height:310px; overflow-y:auto;" id="orderListContainer"></div>
      </div>
      <div class="card" data-nav="aiEvolution">
        <div class="card-header"><span class="card-title">AI上进心</span><div class="card-icon" style="background:linear-gradient(145deg,#dc2626,#b91c1c);"><i class="fas fa-rocket"></i></div></div>
        <div class="progress-wrap"><div class="progress-label"><span>决策</span><span>${ai.decision}%</span></div><div class="progress-bar"><div class="progress-fill" style="width:${ai.decision}%;"></div></div></div>
        <div class="progress-wrap"><div class="progress-label"><span>学习</span><span>${ai.learn}%</span></div><div class="progress-bar"><div class="progress-fill" style="width:${ai.learn}%; background:#10b981;"></div></div></div>
      </div>
      <div class="card" data-nav="entropy">
        <div class="card-header"><span class="card-title">系统熵值</span><div class="card-icon" style="background:linear-gradient(145deg,#a855f7,#7c3aed);"><i class="fas fa-chaos"></i></div></div>
        <div class="row-flex"><span class="label">熵增指数</span><span class="value">${entropy.score}/100</span></div>
        <div class="progress-wrap"><div class="progress-bar"><div class="progress-fill" style="width:${entropy.score}%; background:#a855f7;"></div></div></div>
        <div class="row-flex"><span class="label">冗余代码</span><span class="value">${entropy.redundantCode}</span></div>
        <div class="row-flex"><span class="label">未用因子</span><span class="value">${entropy.unusedFactors}</span></div>
      </div>
      <div class="card" data-nav="health">
        <div class="card-header"><span class="card-title">系统健康度</span><div class="card-icon" style="background:linear-gradient(145deg,#10b981,#059669);"><i class="fas fa-heartbeat"></i></div></div>
        <div class="row-flex"><span class="label">健康评分</span><span class="value">${health.score}/100</span></div>
        <div class="progress-wrap"><div class="progress-bar"><div class="progress-fill" style="width:${health.score}%; background:#10b981;"></div></div></div>
        <div class="row-flex"><span class="label">CPU温度</span><span class="value">${health.cpuTemp}</span></div>
        <div class="row-flex"><span class="label">磁盘IO</span><span class="value">${health.diskIO}</span></div>
        <div class="row-flex"><span class="label">运行时间</span><span class="value">${health.uptime}</span></div>
      </div>
      <div class="card" style="grid-column: span 2;" data-nav="systemEnv">
        <div class="card-header"><span class="card-title">系统环境</span><div class="card-icon" style="background:#0f172a;"><i class="fas fa-microchip"></i></div></div>
        <div class="row-flex"><span class="label">版本 v3.0.0-spartan</span><span class="value">点击进入</span></div>
      </div>
    `;

    // 绑定卡片内部按钮事件
    const switchAccountBtn = $('switchAccountBtn');
    if (switchAccountBtn) switchAccountBtn.addEventListener('click', (e) => { e.stopPropagation(); switchAccount(); });
    const toggleBrokerBtn = $('toggleBrokerBtn');
    if (toggleBrokerBtn) toggleBrokerBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleBroker(); });
    const emergencyCard = $('emergencyCloseCard');
    if (emergencyCard) emergencyCard.addEventListener('click', emergencyClose);
    // 为所有带 data-nav 的卡片绑定导航
    document.querySelectorAll('.card[data-nav]').forEach(card => {
      card.addEventListener('click', () => {
        const navKey = card.getAttribute('data-nav');
        if (navKey) navigateTo(navKey);
      });
    });

    // 刷新子组件
    renderOrderList();
    setTimeout(renderMiniChart, 50);
  }

  function renderOrderList() {
    const container = $('orderListContainer');
    if (!container) return;
    const trades = AppState.recentTrades.slice(0, 12);
    container.innerHTML = trades.map(t => `
      <div class="row-flex" style="font-size:0.7rem; padding:3px 0;">
        <span>${t.time}</span>
        <span style="color:${t.side==='多'?'#10b981':'#ef4444'};">${t.side}</span>
        <span>${t.pair}</span>
        <span style="color:${t.pnl.startsWith('+')?'#10b981':'#ef4444'};">${t.pnl}</span>
      </div>
    `).join('');
  }

  function renderMiniChart() {
    const canvas = $('miniChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const data = [];
    for (let i = 30; i >= 0; i--) data.push(500000 + (Math.random() - 0.4) * 10000);
    if (canvas._chart) canvas._chart.destroy();
    canvas._chart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: Array.from({ length: 31 }, (_, i) => ''),
        datasets: [{
          data,
          borderColor: '#2563eb',
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.3,
          fill: false,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: { x: { display: false }, y: { display: false } },
      },
    });
  }

  // ======================== 行为日志 ========================
  function renderBehaviorLog() {
    const container = $('behaviorLogContainer');
    if (!container) return;
    const logs = getLogs(AppState.currentLogFilter);
    const filterLabel = getFilterLabel(AppState.currentLogFilter);
    container.innerHTML = `
      <h3 style="margin-bottom:8px;">📋 行为日志</h3>
      <button class="log-filter-btn" id="logFilterBtn">${filterLabel} <i class="fas fa-chevron-down"></i></button>
      <div class="log-dropdown" id="logDropdown">
        ${['order', 'system', 'agent', 'risk', 'evolution', 'council', 'shadow', 'strategy'].map(k => `
          <button data-filter="${k}">${getFilterLabel(k)}</button>
        `).join('')}
      </div>
      <div class="log-list" id="logList"></div>
    `;
    // 绑定下拉按钮
    const filterBtn = $('logFilterBtn');
    const dropdown = $('logDropdown');
    filterBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      dropdown.classList.toggle('show');
    });
    document.addEventListener('click', () => {
      dropdown.classList.remove('show');
    });
    // 绑定过滤选项
    dropdown.querySelectorAll('button').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const newFilter = btn.getAttribute('data-filter');
        AppState.currentLogFilter = newFilter;
        renderBehaviorLog();
      });
    });
    // 填充日志条目
    const list = $('logList');
    list.innerHTML = logs.map(l => `
      <div class="log-item">
        <span class="log-time">${l.time}</span>
        <span class="log-dot ${l.dot}"></span>
        <span>${l.msg}</span>
      </div>
    `).join('');
  }

  function getLogs(filter) {
    return AppState.logPools[filter] || [];
  }

  function getFilterLabel(key) {
    const labels = {
      order: '订单管理',
      system: '系统状态',
      agent: '智能体',
      risk: '风险评估',
      evolution: '进化管理',
      council: '议会状态',
      shadow: '影子模式',
      strategy: '策略状态',
    };
    return labels[key] || key;
  }

  // ======================== 导航与页面管理 ========================
  function navigateTo(key) {
    if (key === 'systemEnv') {
      homePage.style.display = 'none';
      detailPage.classList.add('active');
      backBtn.style.display = 'flex';
      renderSystemEnvPage();
      AppState.navStack.push(key);
    } else if (key === 'home') {
      homePage.style.display = 'block';
      detailPage.classList.remove('active');
      backBtn.style.display = 'none';
      AppState.navStack = [];
    } else if (key === 'health') {
      // 运行健康检查
      AppState.health.score = Math.floor(Math.random() * 15) + 80;
      AppState.health.cpuTemp = (45 + Math.random() * 20).toFixed(0) + '°C';
      renderMainCards();
    } else if (key === 'accountDetail') {
      alert(`账户详情：\n余额：$${(AppState.account.available/1000).toFixed(1)}k\n持仓市值：模拟数据\n历史总存额：$520k\n历史总盈亏：+$31.2k\n历史总手数：1,240\n历史总胜率：68%\n最近一个月盈亏：+$4.5k\n总预估滑点费：-$0.8k`);
    } else {
      // 通用二级页面
      alert(`进入 ${key} 页面 (开发中)`);
    }
  }

  function renderSystemEnvPage() {
    if (!detailPage) return;
    const programs = [
      { name: '系统日志中心', icon: 'fa-scroll', bg: '#0f172a', status: '正常运行' },
      { name: '多周期状态', icon: 'fa-wave-square', bg: '#8b5cf6', status: '1m多 3m多' },
      { name: 'AI上进心', icon: 'fa-rocket', bg: '#dc2626', status: '决策82%' },
      { name: '进化议会', icon: 'fa-users', bg: '#8b5cf6', status: '屯币者主导' },
      { name: '学习成果', icon: 'fa-moon', bg: '#059669', status: '2个新因子' },
      { name: '消息中心', icon: 'fa-bell', bg: '#f59e0b', status: '3条未读' },
      { name: '系统自检', icon: 'fa-check-circle', bg: '#10b981', status: '全部通过' },
      { name: '策略矩阵', icon: 'fa-chess-board', bg: '#2563eb', status: '4个运行中' },
      { name: '风险矩阵', icon: 'fa-shield-halved', bg: '#ef4444', status: 'VaR 12.6k' },
      { name: '测试中心', icon: 'fa-flask', bg: '#a855f7', status: '最后测试: 06:00' },
      { name: 'API管理', icon: 'fa-key', bg: '#f97316', status: '3密钥有效' },
      { name: '配置管理', icon: 'fa-sliders', bg: '#64748b', status: '已保存' },
      { name: '仓位管理', icon: 'fa-boxes', bg: '#0ea5e9', status: '总敞口 0.26BTC' },
      { name: '行为日志', icon: 'fa-scroll', bg: '#334155', status: '归档中' },
      { name: '外部审计', icon: 'fa-check-double', bg: '#d946ef', status: '62天后' },
      { name: '学习守卫', icon: 'fa-brain', bg: '#10b981', status: '休眠中' },
      { name: '云存储', icon: 'fa-cloud', bg: '#38bdf8', status: '已同步' },
      { name: '系统熵值', icon: 'fa-chaos', bg: '#7c3aed', status: '42/100' },
      { name: '版本与OTA', icon: 'fa-code-branch', bg: '#1e293b', status: 'v3.0.0-spartan' },
      { name: '订单管理', icon: 'fa-list-check', bg: '#0ea5e9', status: '12笔记录' },
    ];
    detailPage.innerHTML = `
      <h2 style="margin:16px 0;">系统环境 · 二级模块</h2>
      <div class="sub-cards-grid">
        ${programs.map(p => `
          <div class="card" data-nav="${p.name}">
            <div class="card-header">
              <span class="card-title">${p.name}</span>
              <div class="card-icon" style="background:${p.bg};"><i class="fas ${p.icon}"></i></div>
            </div>
            <div class="row-flex"><span class="label">最近状态</span><span class="value">${p.status}</span></div>
          </div>
        `).join('')}
      </div>
    `;
    // 为二级卡片添加导航
    detailPage.querySelectorAll('.card[data-nav]').forEach(card => {
      card.addEventListener('click', () => {
        const navKey = card.getAttribute('data-nav');
        alert(`进入三级页面: ${navKey} (开发中)`);
      });
    });
  }

  // ======================== 操作函数 ========================
  function switchAccount() {
    const idx = AppState.accounts.indexOf(AppState.mode.currentAccount);
    const nextIdx = (idx + 1) % AppState.accounts.length;
    AppState.mode.currentAccount = AppState.accounts[nextIdx];
    renderMainCards();
  }

  function toggleBroker() {
    if (typeof verifyPassword === 'function') {
      const pwd = prompt('请输入操作密码以切换交易模式');
      if (!verifyPassword(pwd)) {
        alert('密码错误');
        return;
      }
    }
    AppState.mode.broker = AppState.mode.broker === 'virtual' ? 'live' : 'virtual';
    renderMainCards();
  }

  function emergencyClose() {
    if (!confirm('⚠️ 确认一键平仓所有持仓？此操作不可撤销！')) return;
    if (typeof verifyPassword === 'function') {
      const pwd = prompt('请输入操作密码');
      if (!verifyPassword(pwd)) {
        alert('密码错误，操作取消');
        return;
      }
    }
    // 实际调用后端API (此处模拟)
    alert('✅ 平仓指令已发送');
  }

  // ======================== AI 助手 ========================
  function handleAiSend() {
    const msg = aiInput.value.trim();
    if (!msg) return;
    appendAiMessage('你', msg);
    aiInput.value = '';

    // 优先使用外部助手函数
    if (typeof sendToAssistant === 'function') {
      sendToAssistant(msg, (reply) => {
        appendAiMessage('火种', reply);
      });
    } else {
      // 简单本地模拟
      setTimeout(() => {
        let reply = `收到指令: "${msg}"。`;
        if (msg.includes('状态')) reply += ' 系统运行正常，夏普比率1.35。';
        else if (msg.includes('订单')) reply += ' 今日成交16笔，胜率74%。';
        else reply += ' 我会在正式集成LLM后提供详细分析。';
        appendAiMessage('火种', reply);
      }, 500);
    }
  }

  function appendAiMessage(sender, text) {
    const div = document.createElement('div');
    div.style.margin = '4px 0';
    div.innerHTML = `<strong>${sender}:</strong> ${text}`;
    aiChatBody.appendChild(div);
    aiChatBody.scrollTop = aiChatBody.scrollHeight;
  }

  // ======================== 状态栏 ========================
  function updateStatusBar() {
    const latText = $('latText');
    const lossText = $('lossText');
    const cpuVal = $('cpuVal');
    const memVal = $('memVal');
    const diskVal = $('diskVal');
    const led = $('ledLat');
    if (!latText) return;

    const latency = 10 + Math.floor(Math.random() * 20);
    latText.textContent = latency + 'ms';
    lossText.textContent = (Math.random() * 0.1).toFixed(2) + '%';
    cpuVal.textContent = (20 + Math.floor(Math.random() * 30)) + '%';
    memVal.textContent = (3.0 + Math.random() * 1.5).toFixed(1) + '/8G';
    diskVal.textContent = (40 + Math.floor(Math.random() * 10)) + '%';

    led.className = 'led ' + (latency < 30 ? 'green' : latency < 60 ? 'yellow' : 'red');
  }

  // ======================== 启动 ========================
  init();
})();
