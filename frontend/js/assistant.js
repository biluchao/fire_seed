/* =============================================================================
   火种系统 (FireSeed) 前端助手模块
   功能：
     - 本地规则引擎 (无需API调用)
     - 云端LLM路由 (对接后端 /api/assistant/chat)
     - 自动降级：本地未命中时调用云端
     - 状态感知：读取全局 AppState 或通过 REST 获取系统快照
   ============================================================================= */

(function (global) {
  'use strict';

  // ---------- 配置 ----------
  const CONFIG = {
    // 是否优先使用云端模型 (可通过后端 API 或前端配置切换)
    preferCloud: false,
    // 云端对话接口
    CLOUD_ENDPOINT: '/api/assistant/chat',
    // 本地对话超时后是否自动转云端
    fallbackToCloud: true,
    // 请求超时 (毫秒)
    requestTimeout: 10000,
  };

  // ---------- 本地规则库 ----------
  // 每条规则包含 keywords (数组), handler (函数，返回回复字符串或 Promise)
  // 匹配时忽略大小写，关键词按顺序匹配第一个即触发
  const LOCAL_RULES = [
    {
      keywords: ['状态', '系统状态', '运行状态', '健康', '是否正常'],
      handler: () => {
        try {
          const st = getSystemState();
          return `系统运行正常。\nCPU: ${st.cpu}% | 内存: ${st.mem} | 延迟: ${st.lat}ms\n健康评分: ${st.health}/100 | 运行时间: ${st.uptime}`;
        } catch (e) {
          return '系统状态数据暂时无法获取，请稍后再试。';
        }
      }
    },
    {
      keywords: ['持仓', '持有', '仓位', '头寸', '敞口'],
      handler: () => {
        try {
          const pos = getPositionSummary();
          return pos ? `当前持仓：${pos}` : '当前无持仓。';
        } catch (e) {
          return '持仓数据获取失败。';
        }
      }
    },
    {
      keywords: ['订单', '成交', '交易记录', '最近交易'],
      handler: () => {
        try {
          const trades = getRecentTradesSummary();
          return trades || '暂时没有最近的交易记录。';
        } catch (e) {
          return '交易记录获取失败。';
        }
      }
    },
    {
      keywords: ['盈亏', '利润', '盈利', '亏损', '赚了', '亏了'],
      handler: () => {
        try {
          const pnl = getPnlSummary();
          return pnl;
        } catch (e) {
          return '盈亏数据获取失败。';
        }
      }
    },
    {
      keywords: ['风险', '回撤', '熔断', 'VaR'],
      handler: () => {
        try {
          const risk = getRiskSummary();
          return risk;
        } catch (e) {
          return '风险数据获取失败。';
        }
      }
    },
    {
      keywords: ['模式', '切换', '激进', '稳健', '虚拟', '实盘'],
      handler: (msg) => {
        try {
          const mode = getCurrentMode();
          if (msg.includes('切换') || msg.includes('改为') || msg.includes('设置')) {
            return '模式切换需要操作密码和TOTP验证，请在前端面板操作。';
          }
          return `当前模式：${mode.broker === 'virtual' ? '虚拟' : '实盘'} / ${mode.strategy === 'aggressive' ? '激进' : '稳健'}`;
        } catch (e) {
          return '模式信息获取失败。';
        }
      }
    },
    {
      keywords: ['帮助', '你能做什么', '功能', '命令'],
      handler: () => {
        return `我是火种助手，可回答以下问题：\n• 系统状态/健康\n• 当前持仓\n• 订单记录\n• 盈亏统计\n• 风险评估\n• 模式查询\n\n你也可以让我分析市场或给出策略建议（需调用云端模型）。`;
      }
    },
  ];

  // ---------- 状态获取辅助函数 ----------
  // 优先从全局 AppState 获取，若不存在则尝试从 DOM 读取

  function getSystemState() {
    // 全局 AppState 由 app.js 维护
    if (global.AppState && global.AppState.health) {
      const h = global.AppState.health;
      const st = {
        cpu: document.getElementById('cpuVal')?.textContent || 'N/A',
        mem: document.getElementById('memVal')?.textContent || 'N/A',
        lat: document.getElementById('latText')?.textContent || 'N/A',
        health: h.score,
        uptime: h.uptime,
        diskIO: h.diskIO,
        cpuTemp: h.cpuTemp,
      };
      return st;
    }
    // 回退：从 DOM 抓取
    return {
      cpu: document.getElementById('cpuVal')?.textContent || 'N/A',
      mem: document.getElementById('memVal')?.textContent || 'N/A',
      lat: document.getElementById('latText')?.textContent || 'N/A',
      health: document.querySelector('.card[data-nav="health"] .value')?.textContent || 'N/A',
      uptime: 'N/A',
      diskIO: 'N/A',
      cpuTemp: 'N/A',
    };
  }

  function getPositionSummary() {
    if (global.AppState && global.AppState.account) {
      // 模拟持仓描述
      return 'BTCUSDT 多 0.26 (均价 63282), ETHUSDT 空 1.5';
    }
    return '暂无数据';
  }

  function getRecentTradesSummary() {
    if (global.AppState && global.AppState.recentTrades) {
      const trades = global.AppState.recentTrades.slice(0, 5);
      return trades.map(t => `${t.time} ${t.pair} ${t.side} ${t.pnl}`).join('\n');
    }
    return '暂无数据';
  }

  function getPnlSummary() {
    if (global.AppState && global.AppState.account) {
      const acc = global.AppState.account;
      return `总权益: $${(acc.equity/1000).toFixed(1)}k\n浮动盈亏: +$${(acc.pnl/1000).toFixed(1)}k\n今日盈利: $${(global.AppState.trades?.profit || 0).toLocaleString()}`;
    }
    return '暂无数据';
  }

  function getRiskSummary() {
    if (global.AppState && global.AppState.risk) {
      const r = global.AppState.risk;
      return `回撤: ${r.drawdown}% / 预警 ${r.drawdownWarn}%\n日亏损: ${r.dailyLoss}% / 熔断 ${r.dailyLimit}%\nVaR(99): $${r.var99.toLocaleString()}`;
    }
    return '暂无数据';
  }

  function getCurrentMode() {
    if (global.AppState && global.AppState.mode) {
      return global.AppState.mode;
    }
    return { broker: 'virtual', strategy: 'moderate' };
  }

  // ---------- 本地匹配引擎 ----------
  function matchLocalRules(message) {
    const lowerMsg = message.toLowerCase();
    for (const rule of LOCAL_RULES) {
      for (const kw of rule.keywords) {
        if (lowerMsg.includes(kw.toLowerCase())) {
          return rule.handler(message);
        }
      }
    }
    return null;
  }

  // ---------- 云端调用 ----------
  async function callCloudAPI(message, context) {
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), CONFIG.requestTimeout);
      const response = await fetch(CONFIG.CLOUD_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, context }),
        signal: controller.signal,
      });
      clearTimeout(timeoutId);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const data = await response.json();
      return data.reply || '云端助手暂未回复。';
    } catch (err) {
      console.error('[Assistant] Cloud API error:', err);
      return `调用云端模型失败: ${err.message}。请检查网络或稍后重试。`;
    }
  }

  // ---------- 主入口：sendToAssistant ----------
  /**
   * 向火种助手发送消息并接收回复
   * @param {string} message 用户输入
   * @param {function} callback 回调函数，参数为回复字符串
   * @param {Array} [history] 可选，对话历史上下文
   */
  async function sendToAssistant(message, callback, history = []) {
    if (!message || !callback) return;

    // 1. 若不强制云端，先尝试本地规则
    if (!CONFIG.preferCloud) {
      const localReply = matchLocalRules(message);
      if (localReply !== null) {
        callback(localReply);
        return;
      }
    }

    // 2. 本地未命中且允许云端
    if (CONFIG.fallbackToCloud || CONFIG.preferCloud) {
      callback('思考中...'); // 临时占位
      const reply = await callCloudAPI(message, history);
      callback(reply);
    } else {
      callback('抱歉，我暂时无法理解您的指令。请尝试输入"帮助"查看支持的命令。');
    }
  }

  // ---------- 公开 API ----------
  global.sendToAssistant = sendToAssistant;
  global.FireSeedAssistant = {
    sendToAssistant,
    matchLocalRules,
    callCloudAPI,
  };

  // 初始化时可用，自动检测云端是否可用 (可选)
  console.log('[Assistant] FireSeed Assistant ready. Cloud mode:', CONFIG.preferCloud ? 'enabled' : 'disabled');
})(window);
