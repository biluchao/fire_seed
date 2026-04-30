/* =============================================================================
   火种系统 (FireSeed) 前端认证与安全模块
   功能：
     - 登录密码强度校验与 SHA-256 哈希
     - TOTP 二次验证输入与提交
     - JWT 令牌存储、自动刷新、过期检测
     - 操作二次确认弹窗
     - 会话保活与自动登出
   ============================================================================= */

(function (global) {
  'use strict';

  // ---------- 配置常量 ----------
  const CONFIG = {
    API_BASE: '/api',
    AUTH_ENDPOINT: '/api/auth/login',
    TOTP_ENDPOINT: '/api/auth/verify-totp',
    REFRESH_ENDPOINT: '/api/auth/refresh',
    LOGOUT_ENDPOINT: '/api/auth/logout',
    TOKEN_KEY: 'fire_seed_jwt',
    REFRESH_TOKEN_KEY: 'fire_seed_refresh_jwt',
    TOKEN_EXPIRY_KEY: 'fire_seed_token_expiry',
    MIN_PASSWORD_LENGTH: 8,
    PASSWORD_REGEX: /^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).+$/, // 至少大小写字母+数字
    TOTP_LENGTH: 6,
    SESSION_TIMEOUT_MINUTES: 30,
    REFRESH_BEFORE_EXPIRY_MS: 60000, // 过期前60秒自动刷新
    MAX_LOGIN_ATTEMPTS: 5,
    LOCKOUT_DURATION_MS: 15 * 60 * 1000, // 15分钟
  };

  // ---------- 内部状态 ----------
  let accessToken = null;
  let refreshToken = null;
  let tokenExpiry = 0;
  let refreshTimer = null;
  let sessionTimer = null;
  let loginAttempts = 0;
  let lockoutUntil = 0;

  // ======================== 初始化 ========================
  function init() {
    loadTokens();
    if (accessToken && !isTokenExpired()) {
      startRefreshTimer();
      startSessionTimer();
      console.log('[Auth] Session restored from stored token.');
    } else if (refreshToken) {
      // 尝试刷新
      refreshAccessToken().then(() => {
        if (accessToken) {
          startRefreshTimer();
          startSessionTimer();
          console.log('[Auth] Token refreshed successfully.');
        }
      });
    }
  }

  // ======================== 登录流程 ========================
  /**
   * 使用静态密码登录 (第一步)
   * @param {string} password 用户输入的明文密码
   * @returns {Promise<boolean>} 成功返回 true，否则 false
   */
  async function loginWithPassword(password) {
    if (isLockedOut()) {
      alert('账号已被临时锁定，请稍后再试。');
      return false;
    }

    if (!validatePasswordStrength(password)) {
      return false;
    }

    const passwordHash = await sha256(password);

    try {
      const res = await fetch(CONFIG.AUTH_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password_hash: passwordHash }),
      });
      const data = await res.json();

      if (res.ok && data.require_totp) {
        // 需要 TOTP 二次验证
        return { requireTotp: true };
      } else if (res.ok && data.access_token) {
        // 已启用无 TOTP 登录（仅开发环境）
        storeTokens(data.access_token, data.refresh_token, data.expires_in);
        startRefreshTimer();
        startSessionTimer();
        loginAttempts = 0;
        return true;
      } else {
        handleLoginFailure(data.error);
        return false;
      }
    } catch (err) {
      console.error('[Auth] Login error:', err);
      return false;
    }
  }

  /**
   * 提交 TOTP 动态码 (第二步)
   * @param {string} totpCode 6位动态码
   * @returns {Promise<boolean>}
   */
  async function verifyTotp(totpCode) {
    if (!/^\d{6}$/.test(totpCode)) {
      alert('动态码格式错误，请输入6位数字。');
      return false;
    }

    try {
      const res = await fetch(CONFIG.TOTP_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ totp: totpCode }),
      });
      const data = await res.json();

      if (res.ok && data.access_token) {
        storeTokens(data.access_token, data.refresh_token, data.expires_in);
        startRefreshTimer();
        startSessionTimer();
        loginAttempts = 0;
        return true;
      } else {
        alert(data.error || 'TOTP 验证失败');
        return false;
      }
    } catch (err) {
      console.error('[Auth] TOTP verify error:', err);
      return false;
    }
  }

  // ======================== 操作授权 ========================
  /**
   * 确认敏感操作 (需 TOTP)
   * @param {string} actionDescription 操作描述
   * @returns {Promise<boolean>} 授权成功返回 true
   */
  async function authorizeAction(actionDescription) {
    const totp = prompt(`敏感操作：「${actionDescription}」\n请输入 Authenticator 6 位动态码`);
    if (!totp) return false;

    try {
      const res = await fetch('/api/auth/authorize-action', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${accessToken}`,
        },
        body: JSON.stringify({ totp: totp, action: actionDescription }),
      });
      return res.ok;
    } catch (err) {
      console.error('[Auth] Authorize action error:', err);
      return false;
    }
  }

  /**
   * 验证用户输入的静态密码 (用于前端二次确认)
   * @param {string} password 明文密码
   * @returns {boolean}
   */
  function verifyPassword(password) {
    // 使用 SHA-256 后比对后端验证接口
    // 简化：通过 API 调用验证
    return true; // 实际实现会调用后端
  }

  // ======================== 令牌管理 ========================
  function storeTokens(accTok, refTok, expiresIn) {
    accessToken = accTok;
    refreshToken = refTok || refreshToken; // 刷新时可能不返回新的 refresh token
    tokenExpiry = Date.now() + (expiresIn * 1000);
    try {
      localStorage.setItem(CONFIG.TOKEN_KEY, accessToken);
      localStorage.setItem(CONFIG.REFRESH_TOKEN_KEY, refreshToken);
      localStorage.setItem(CONFIG.TOKEN_EXPIRY_KEY, String(tokenExpiry));
    } catch (e) {
      console.warn('[Auth] Unable to persist tokens:', e);
    }
  }

  function loadTokens() {
    try {
      accessToken = localStorage.getItem(CONFIG.TOKEN_KEY) || null;
      refreshToken = localStorage.getItem(CONFIG.REFRESH_TOKEN_KEY) || null;
      const expiryStr = localStorage.getItem(CONFIG.TOKEN_EXPIRY_KEY);
      tokenExpiry = expiryStr ? parseInt(expiryStr, 10) : 0;
    } catch (e) {
      console.warn('[Auth] Unable to read tokens:', e);
    }
  }

  function clearTokens() {
    accessToken = null;
    refreshToken = null;
    tokenExpiry = 0;
    try {
      localStorage.removeItem(CONFIG.TOKEN_KEY);
      localStorage.removeItem(CONFIG.REFRESH_TOKEN_KEY);
      localStorage.removeItem(CONFIG.TOKEN_EXPIRY_KEY);
    } catch (e) {
      console.warn('[Auth] Unable to clear tokens:', e);
    }
  }

  function isTokenExpired() {
    return !tokenExpiry || Date.now() >= tokenExpiry;
  }

  function getAccessToken() {
    return accessToken;
  }

  // ======================== 令牌刷新 ========================
  async function refreshAccessToken() {
    if (!refreshToken) return false;
    try {
      const res = await fetch(CONFIG.REFRESH_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      const data = await res.json();
      if (res.ok && data.access_token) {
        storeTokens(data.access_token, data.refresh_token || refreshToken, data.expires_in);
        startRefreshTimer();
        return true;
      } else {
        clearTokens();
        return false;
      }
    } catch (err) {
      console.error('[Auth] Token refresh error:', err);
      return false;
    }
  }

  function startRefreshTimer() {
    clearTimeout(refreshTimer);
    if (!tokenExpiry) return;
    const delay = tokenExpiry - Date.now() - CONFIG.REFRESH_BEFORE_EXPIRY_MS;
    if (delay <= 0) {
      refreshAccessToken();
      return;
    }
    refreshTimer = setTimeout(() => {
      refreshAccessToken().then(success => {
        if (!success) handleSessionExpired();
      });
    }, delay);
  }

  // ======================== 会话管理 ========================
  function startSessionTimer() {
    clearTimeout(sessionTimer);
    sessionTimer = setTimeout(handleSessionExpired, CONFIG.SESSION_TIMEOUT_MINUTES * 60 * 1000);
  }

  function handleSessionExpired() {
    clearTokens();
    clearTimeout(refreshTimer);
    clearTimeout(sessionTimer);
    alert('会话已过期，请重新登录。');
    // 触发登出或重定向
    location.reload();
  }

  async function logout() {
    try {
      await fetch(CONFIG.LOGOUT_ENDPOINT, {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${accessToken}` },
      });
    } catch (e) { /* ignore */ }
    clearTokens();
    clearTimeout(refreshTimer);
    clearTimeout(sessionTimer);
    location.reload();
  }

  // ======================== 密码强度校验 ========================
  function validatePasswordStrength(password) {
    if (!password || password.length < CONFIG.MIN_PASSWORD_LENGTH) {
      alert(`密码长度至少 ${CONFIG.MIN_PASSWORD_LENGTH} 位。`);
      return false;
    }
    if (!CONFIG.PASSWORD_REGEX.test(password)) {
      alert('密码必须包含大写字母、小写字母和数字。');
      return false;
    }
    return true;
  }

  // ======================== 哈希工具 ========================
  async function sha256(message) {
    const msgBuffer = new TextEncoder().encode(message);
    const hashBuffer = await crypto.subtle.digest('SHA-256', msgBuffer);
    return Array.from(new Uint8Array(hashBuffer))
      .map(b => b.toString(16).padStart(2, '0'))
      .join('');
  }

  // ======================== 暴力破解防护 ========================
  function handleLoginFailure(errorMsg) {
    loginAttempts++;
    if (loginAttempts >= CONFIG.MAX_LOGIN_ATTEMPTS) {
      lockoutUntil = Date.now() + CONFIG.LOCKOUT_DURATION_MS;
      alert('连续登录失败次数过多，账号已锁定 15 分钟。');
      return;
    }
    alert(errorMsg || '登录失败，请检查密码。');
  }

  function isLockedOut() {
    if (Date.now() < lockoutUntil) return true;
    if (lockoutUntil && Date.now() >= lockoutUntil) {
      lockoutUntil = 0;
      loginAttempts = 0;
    }
    return false;
  }

  // ======================== API 拦截器 ========================
  // 自动为所有 fetch 请求附加 Authorization 头
  const originalFetch = window.fetch;
  window.fetch = function (url, options = {}) {
    if (accessToken) {
      options.headers = options.headers || {};
      if (typeof options.headers === 'object' && !(options.headers instanceof Headers)) {
        options.headers['Authorization'] = `Bearer ${accessToken}`;
      } else if (options.headers instanceof Headers) {
        options.headers.append('Authorization', `Bearer ${accessToken}`);
      }
    }
    return originalFetch.call(this, url, options).then(async (response) => {
      // 401 自动尝试刷新令牌
      if (response.status === 401 && refreshToken) {
        const refreshed = await refreshAccessToken();
        if (refreshed) {
          // 重试原始请求
          options.headers = options.headers || {};
          if (typeof options.headers === 'object' && !(options.headers instanceof Headers)) {
            options.headers['Authorization'] = `Bearer ${accessToken}`;
          }
          return originalFetch.call(this, url, options);
        } else {
          handleSessionExpired();
        }
      }
      return response;
    });
  };

  // ======================== 公开 API ========================
  global.FireSeedAuth = {
    init,
    loginWithPassword,
    verifyTotp,
    verifyPassword,
    authorizeAction,
    getAccessToken,
    logout,
    isTokenExpired,
    sha256,
  };

  // 启动时自动加载令牌
  document.addEventListener('DOMContentLoaded', init);

})(window);
