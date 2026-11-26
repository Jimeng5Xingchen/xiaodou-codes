/* ==========================================================================
   MCP / AI 授权模块（「我的账号」页）
   负责：展示连接地址与连接 Token、生成/撤销令牌、浏览器远端授权、
        复制 MCP 客户端配置、查看已授权客户端与可用工具清单。
   挂载为 window.XDMCP，由页面在切换到「我的账号」时按需初始化。
   ========================================================================== */
(function () {
  'use strict';

  var $ = function (s) { return document.querySelector(s); };

  var KEY = 'xd_mcp_creds_v1';   // 本机保存的连接令牌（明文仅存本机浏览器）

  var state = {
    cfg: null,
    loaded: false,
    showToken: false,
    busy: false
  };

  /* ---------------- 基础工具 ---------------- */

  function token() {
    try { return localStorage.getItem('xd_token') || ''; } catch (e) { return ''; }
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function toast(t) {
    var el = $('#toast');
    if (!el) return;
    el.textContent = t;
    el.classList.add('show');
    setTimeout(function () { el.classList.remove('show'); }, 1800);
  }

  function msg(text, cls) {
    var el = $('#mcpMsg');
    if (!el) return;
    el.textContent = text || '';
    el.className = 'mmsg ' + (cls || '');
  }

  /** 取后端 API 基址：优先页面注入的配置，其次运行时 config.js，最后同源。 */
  function apiBase() {
    try { if (typeof API_BASE !== 'undefined' && API_BASE) return String(API_BASE).replace(/\/+$/, ''); } catch (e) { }
    try {
      var c = window.__XD_CONFIG__ || {};
      if (c.api) return String(c.api).replace(/\/+$/, '');
    } catch (e) { }
    return location.origin.replace(/\/+$/, '');
  }

  /** 平台网关要求的 sharecode（部分部署需要随请求携带）。 */
  function sharecode() {
    try { if (typeof API_SHARECODE !== 'undefined' && API_SHARECODE) return String(API_SHARECODE); } catch (e) { }
    try {
      var c = window.__XD_CONFIG__ || {};
      if (c.sharecode) return String(c.sharecode);
    } catch (e) { }
    try { return new URLSearchParams(location.search).get('sharecode') || ''; } catch (e) { }
    return '';
  }

  /** MCP 连接地址（部分网关要求带 sharecode）。 */
  function endpointUrl() {
    var url = apiBase() + '/mcp';
    var sc = sharecode();
    if (sc) url += '?sharecode=' + encodeURIComponent(sc);
    return url;
  }

  function authorizeUrl() {
    var url = apiBase() + '/mcp/authorize';
    var sc = sharecode();
    if (sc) url += '?sharecode=' + encodeURIComponent(sc);
    return url;
  }

  /** 本模块内的接口地址拼接（不依赖主脚本的全局 apiUrl，避免加载顺序问题）。 */
  function apiPath(path) {
    var base = apiBase();
    var sc = sharecode();
    var url = base + path;
    if (sc) url += (path.indexOf('?') >= 0 ? '&' : '?') + 'sharecode=' + encodeURIComponent(sc);
    return url;
  }

  async function req(path, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    opts.headers['X-User-Token'] = token();
    if (opts.body && typeof opts.body !== 'string') {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.body);
    }
    var r = await fetch(apiPath(path), opts);
    var j = null;
    try { j = await r.json(); } catch (e) { j = { ok: false, error: '响应解析失败' }; }
    if (!j.ok && j.error) {
      msg(j.error, 'err');
      var mask = $('#mcpMask');
      if (!mask || !mask.classList.contains('show')) toast(j.error);
    }
    return j;
  }

  /* ---------------- 本机保存的令牌 ---------------- */

  function loadCreds() {
    try {
      var raw = localStorage.getItem(KEY);
      if (!raw) return null;
      var o = JSON.parse(raw);
      return (o && o.token) ? o : null;
    } catch (e) { return null; }
  }

  function saveCreds(o) {
    try { localStorage.setItem(KEY, JSON.stringify(o)); } catch (e) { }
  }

  function clearCreds() {
    try { localStorage.removeItem(KEY); } catch (e) { }
  }

  /** 取当前账号可用的连接令牌（不同账号不共用）。 */
  function currentToken(me) {
    var c = loadCreds();
    if (!c) return '';
    if (me && c.email && me.email && c.email !== me.email) return '';
    return c.token || '';
  }

  /* ---------------- 复制 ---------------- */

  async function copy(text, label) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } catch (_) { }
      document.body.removeChild(ta);
    }
    toast((label || '内容') + '已复制');
  }

  /* ---------------- 渲染 ---------------- */

  function fmtTime(ts) {
    if (!ts) return '—';
    var d = new Date(ts * 1000);
    function p(n) { return n < 10 ? '0' + n : '' + n; }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
      p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function scopeNames(scopes) {
    if (!scopes || !scopes.length) return '默认';
    return scopes.join(' ');
  }

  function renderState(me) {
    var el = $('#mcpState');
    var dot = $('#mcpEntryDot');
    var t = currentToken(me);
    var active = 0;
    if (state.cfg && state.cfg.tokens) {
      active = state.cfg.tokens.filter(function (x) { return x.active; }).length;
    }
    if (dot) dot.classList.toggle('on', !!(t || active));
    if (!el) return;
    if (t) {
      el.className = 'mcp-state on';
      el.innerHTML = '<span class="dot"></span>已授权';
    } else if (active) {
      el.className = 'mcp-state on';
      el.innerHTML = '<span class="dot"></span>' + active + ' 个有效令牌';
    } else {
      el.className = 'mcp-state';
      el.innerHTML = '<span class="dot"></span>未连接';
    }
  }

  function renderEndpoint() {
    var el = $('#mcpEndpoint');
    if (el) el.textContent = endpointUrl();
  }

  function renderToken(me) {
    var el = $('#mcpToken');
    var eye = $('#mcpTokenEye');
    if (!el) return;
    var t = currentToken(me);
    if (!t) {
      var active = state.cfg && state.cfg.tokens
        ? state.cfg.tokens.filter(function (x) { return x.active; }).length : 0;
      el.textContent = active
        ? '本机未保存明文（令牌仅在生成时显示一次）'
        : '尚未生成 —— 点「生成连接 Token」';
      el.style.color = 'var(--dim)';
      if (eye) eye.textContent = '显示';
      return;
    }
    el.style.color = '';
    el.textContent = state.showToken ? t : maskToken(t);
    if (eye) eye.textContent = state.showToken ? '隐藏' : '显示';
  }

  function maskToken(t) {
    if (!t) return '';
    if (t.length <= 16) return t;
    return t.slice(0, 14) + '••••••••' + t.slice(-4);
  }

  function renderTokens(me) {
    var box = $('#mcpTokenList');
    if (!box) return;
    var list = (state.cfg && state.cfg.tokens) || [];
    var cnt = $('#mcpTokenCount');
    if (cnt) cnt.textContent = list.length;
    if (!list.length) {
      box.innerHTML = '<div class="mcp-empty">还没有创建过连接令牌。点上面的「生成连接 Token」为某个 AI 客户端单独发一个，便于随时单独断开。</div>';
      return;
    }
    box.innerHTML = '<div class="mcp-list">' + list.map(function (t) {
      var life = t.revokedAt ? '<span class="tagline off">已撤销</span>'
        : (t.active ? '<span class="tagline live">有效</span>' : '<span class="tagline off">已过期</span>');
      return '<div class="mcp-item' + (t.active ? '' : ' dead') + '">'
        + '<div class="r1"><div class="nm">' + esc(t.name || 'AI 客户端') + life + '</div>'
        + '<div class="acts">'
        + (t.active ? '<button data-rot="' + t.id + '">重新生成</button>' : '')
        + (t.revokedAt ? '' : '<button data-rev="' + t.id + '">撤销</button>')
        + '</div></div>'
        + '<div class="meta"><code>' + esc(t.prefix || '') + '…</code> · 权限：' + esc(scopeNames(t.scopes))
        + ' · 创建 ' + fmtTime(t.createdAt)
        + ' · 到期 ' + fmtTime(t.expiresAt)
        + (t.lastUsedAt ? ' · 最近使用 ' + fmtTime(t.lastUsedAt) : ' · 从未使用')
        + (t.useCount ? ' · 调用 ' + t.useCount + ' 次' : '')
        + '</div></div>';
    }).join('') + '</div>';

    box.querySelectorAll('[data-rev]').forEach(function (b) {
      b.onclick = async function () {
        if (!confirm('确认撤销这个连接令牌？撤销后对应的 AI 客户端会立即失去访问能力。')) return;
        var j = await req('/api/mcp/tokens/' + b.getAttribute('data-rev'), { method: 'DELETE' });
        if (j.ok) { toast('已撤销'); await load(true); }
      };
    });
    box.querySelectorAll('[data-rot]').forEach(function (b) {
      b.onclick = async function () {
        if (!confirm('重新生成后旧令牌立即失效，需要重新配置客户端。确认继续？')) return;
        var j = await req('/api/mcp/tokens/' + b.getAttribute('data-rot') + '/rotate', { method: 'POST' });
        if (j.ok && j.token) {
          saveCreds({ token: j.token, email: (window.ME && window.ME.email) || '', at: Date.now() });
          state.showToken = true;
          toast('已重新生成');
          await load(true);
        }
      };
    });
  }

  function renderClients() {
    var box = $('#mcpClientList');
    if (!box) return;
    var list = (state.cfg && state.cfg.clients) || [];
    var cnt = $('#mcpClientCount');
    if (cnt) cnt.textContent = list.length;
    if (!list.length) {
      box.innerHTML = '<div class="mcp-empty">还没有通过浏览器授权页连接过 AI 客户端。使用上面的「浏览器远端授权」可以让 AI 客户端在你确认后完成连接。</div>';
      return;
    }
    box.innerHTML = '<div class="mcp-list">' + list.map(function (c) {
      return '<div class="mcp-item">'
        + '<div class="r1"><div class="nm">' + esc(c.name || c.clientId) + '</div>'
        + '<div class="acts"><button data-crv="' + esc(c.clientId) + '">解除授权</button></div></div>'
        + '<div class="meta">ID：<code>' + esc(c.clientId) + '</code>'
        + ' · 活跃令牌 ' + (c.tokenCount || 0) + ' 个'
        + ' · 授权时间 ' + fmtTime(c.approvedAt)
        + (c.redirectUris && c.redirectUris.length ? ' · 回调 ' + esc(c.redirectUris[0]) : '')
        + '</div></div>';
    }).join('') + '</div>';

    box.querySelectorAll('[data-crv]').forEach(function (b) {
      b.onclick = async function () {
        if (!confirm('解除该客户端的授权并撤销其全部令牌？')) return;
        var j = await req('/api/mcp/clients/' + encodeURIComponent(b.getAttribute('data-crv')), { method: 'DELETE' });
        if (j.ok) { toast('已解除授权'); await load(true); }
      };
    });
  }

  function renderScopes() {
    var box = $('#mcpScopePicker');
    if (!box || !state.cfg) return;
    var defs = state.cfg.scopes || [];
    var defaults = state.cfg.defaultScopes || [];
    box.innerHTML = defs.map(function (s) {
      var on = defaults.indexOf(s.name) >= 0;
      return '<label class="mcp-scope"><input type="checkbox" value="' + esc(s.name) + '"' + (on ? ' checked' : '') + '>'
        + '<span><b>' + esc(s.name) + '</b><small>' + esc(s.desc) + '</small></span></label>';
    }).join('');
  }

  function renderTools() {
    var box = $('#mcpTools');
    if (!box || !state.cfg) return;
    var tools = state.cfg.tools || [];
    box.innerHTML = tools.map(function (t) {
      return '<div class="mcp-tool"><b>' + esc(t.name) + '</b><span>' + esc(t.desc) + '</span></div>';
    }).join('');
  }

  function render(me) {
    renderEndpoint();
    renderToken(me);
    renderState(me);
    renderTokens(me);
    renderClients();
    renderTools();
  }

  /* ---------------- 交互 ---------------- */

  async function createToken() {
    if (state.busy) return;
    state.busy = true;
    var btn = $('#mcpNewToken');
    var old = btn ? btn.textContent : '';
    if (btn) { btn.textContent = '生成中…'; btn.disabled = true; }
    try {
      var name = ($('#mcpTokenName') && $('#mcpTokenName').value.trim()) || 'AI 客户端';
      var j = await req('/api/mcp/tokens', { method: 'POST', body: { name: name } });
      if (j.ok && j.token) {
        saveCreds({ token: j.token, email: (window.ME && window.ME.email) || '', at: Date.now() });
        state.showToken = true;
        msg('令牌已生成，明文只显示这一次，请立即复制保存。', 'ok');
        toast('连接令牌已生成');
        await load(true);
      }
    } catch (e) {
      msg('生成失败，请重试', 'err');
    } finally {
      state.busy = false;
      if (btn) { btn.textContent = old || '生成连接 Token'; btn.disabled = false; }
    }
  }

  function selectedScopes() {
    var out = [];
    document.querySelectorAll('#mcpScopePicker input:checked').forEach(function (i) {
      out.push(i.value);
    });
    return out;
  }

  function buildAuthUrl() {
    var base = authorizeUrl();
    var cid = ($('#mcpClientId') && $('#mcpClientId').value.trim()) || ('web-' + Math.random().toString(36).slice(2, 10));
    var ru = ($('#mcpRedirect') && $('#mcpRedirect').value.trim()) || 'http://localhost:8765/callback';
    var cn = ($('#mcpClientName') && $('#mcpClientName').value.trim()) || 'AI 客户端';
    var q = new URLSearchParams({
      response_type: 'code',
      client_id: cid,
      client_name: cn,
      redirect_uri: ru,
      scope: selectedScopes().join(' ') || 'codes:read codes:write account:read'
    });
    return base + '?' + q.toString();
  }

  function openAuth() {
    var url = buildAuthUrl();
    var w = window.open(url, '_blank', 'noopener');
    if (!w) {
      msg('浏览器拦截了新窗口，请允许弹窗，或用「复制授权链接」手动打开。', 'err');
    } else {
      msg('已打开授权页，确认后授权会回跳到客户端设置的回调地址。', 'ok');
    }
  }

  function clientConfig() {
    var me = window.ME || {};
    var t = currentToken(me);
    var cfg = {
      mcpServers: {
        '小豆邀请码': {
          url: endpointUrl(),
          headers: t ? { 'X-MCP-Token': t } : {},
          type: 'http'
        }
      }
    };
    if (!t) cfg.note = '尚未生成连接 Token，请先点「生成连接 Token」。';
    return JSON.stringify(cfg, null, 2);
  }

  /* ---------------- 弹窗开关 ---------------- */

  var lastFocus = null;

  function openModal() {
    var mask = $('#mcpMask');
    if (!mask) return;
    lastFocus = document.activeElement;
    mask.classList.add('show');
    document.body.style.overflow = 'hidden';
    if (!state.cfg) load();
    else render(window.ME);
    if (!$('#mcpScopePicker').children.length) renderScopes();
    var close = $('#mcpClose');
    if (close) close.focus();
  }

  function closeModal() {
    var mask = $('#mcpMask');
    if (!mask || !mask.classList.contains('show')) return;
    mask.classList.remove('show');
    document.body.style.overflow = '';
    if (lastFocus && lastFocus.focus) {
      try { lastFocus.focus(); } catch (e) { }
    }
  }

  /* ---------------- 加载 ---------------- */

  async function load(force) {
    if (!window.ME) return;
    if (!force && state.loaded && state.cfg) { render(window.ME); return; }
    var j = await req('/api/mcp/config');
    if (!j.ok) return;
    state.cfg = j;
    state.loaded = true;
    render(window.ME);
    if (!$('#mcpScopePicker').children.length) renderScopes();
  }

  function bind() {
    var ob = $('#mcpOpenBtn');
    if (ob) ob.onclick = openModal;

    var cb = $('#mcpClose');
    if (cb) cb.onclick = closeModal;

    var mask = $('#mcpMask');
    if (mask) mask.addEventListener('click', function (e) {
      if (e.target === mask) closeModal();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeModal();
    });

    var btn = $('#mcpNewToken');
    if (btn) btn.onclick = createToken;

    var eye = $('#mcpTokenEye');
    if (eye) eye.onclick = function () {
      state.showToken = !state.showToken;
      renderToken(window.ME);
    };

    var ce = $('#mcpCopyEndpoint');
    if (ce) ce.onclick = function () { copy(endpointUrl(), '连接地址'); };

    var ct = $('#mcpCopyToken');
    if (ct) ct.onclick = function () {
      var t = currentToken(window.ME);
      if (!t) { toast('还没有连接 Token'); return; }
      copy(t, '连接 Token');
    };

    var cc = $('#mcpCopyCfg');
    if (cc) cc.onclick = function () { copy(clientConfig(), '客户端配置'); };

    var ab = $('#mcpAuthBtn');
    if (ab) ab.onclick = function () {
      var box = $('#mcpAuthBox');
      if (box) box.hidden = !box.hidden;
      if (box && !box.hidden) renderScopes();
    };

    var ao = $('#mcpAuthOpen');
    if (ao) ao.onclick = openAuth;

    var ac = $('#mcpAuthCopy');
    if (ac) ac.onclick = function () { copy(buildAuthUrl(), '授权链接'); };

    var ra = $('#mcpRevokeAll');
    if (ra) ra.onclick = async function () {
      if (!confirm('断开全部 AI 客户端连接？所有连接令牌都会被撤销。')) return;
      var j = await req('/api/mcp/revoke-all', { method: 'POST' });
      if (j.ok) {
        clearCreds();
        toast('已断开全部连接');
        await load(true);
      }
    };
  }

  function init() {
    bind();
    if (window.ME) load();
  }

  window.XDMCP = {
    init: init,
    load: function () { return load(true); },
    refresh: function () { return load(true); },
    endpointUrl: endpointUrl,
    clientConfig: clientConfig
  };
})();
