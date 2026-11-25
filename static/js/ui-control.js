/* ============================================================
   ui-control.js —— UI 控制组件（外观 / 主题 / 毛玻璃 / 胶囊）
   独立模块：只负责"外观与交互偏好"，不碰业务数据。
   通过 window.XDUI 暴露，页面只需调用 XDUI.init()。
   ============================================================ */
(function (global) {
  'use strict';

  const KEY = 'xd_ui_v1';
  const THEME_KEY = 'xd_theme_v1';
  const ACCENT_KEY = 'xd_accent_v1';

  const ACCENT_PRESETS = [
    { c: '#f0883e', n: '橙' }, { c: '#58a6ff', n: '蓝' }, { c: '#3fb950', n: '绿' },
    { c: '#a371f7', n: '紫' }, { c: '#f85149', n: '红' }, { c: '#d29922', n: '黄' },
    { c: '#2dd4bf', n: '青' }
  ];
  const DEFAULT_ACCENT = { dark: '#f0883e', light: '#d2691e' };

  /* ---------- 状态 ---------- */
  const state = {
    theme: 'system',          // system | dark | light
    accent: '',               // 自定义主题色，空 = 用默认
    glass: true,              // 毛玻璃开关
    glassAlpha: 72,           // 表面不透明度 0~100
    glassBlur: 18,            // 模糊半径 0~40 px
    glassEdge: 45,            // 水晶高光强度 0~100
    capsule: false,           // 胶囊形状
    md: false,                // Android 11 / Material You 风格
    motion: true              // 动效
  };

  function load() {
    try {
      const raw = localStorage.getItem(KEY);
      if (raw) Object.assign(state, JSON.parse(raw) || {});
    } catch (e) { /* ignore */ }
    state.theme = localStorage.getItem(THEME_KEY) || state.theme || 'system';
    state.accent = localStorage.getItem(ACCENT_KEY) || state.accent || '';
  }

  function save() {
    try {
      localStorage.setItem(KEY, JSON.stringify(state));
      localStorage.setItem(THEME_KEY, state.theme);
      if (state.accent) localStorage.setItem(ACCENT_KEY, state.accent);
      else localStorage.removeItem(ACCENT_KEY);
    } catch (e) { /* ignore */ }
  }

  /* ---------- 工具 ---------- */
  function mixHex(a, b, ratio) {
    const pa = [1, 3, 5].map(i => parseInt(a.substr(i, 2), 16));
    const pb = [1, 3, 5].map(i => parseInt(b.substr(i, 2), 16));
    const p = pa.map((v, i) => Math.round(v * (1 - ratio) + pb[i] * ratio));
    return '#' + p.map(v => v.toString(16).padStart(2, '0')).join('');
  }
  // 相对亮度，用于决定主色上的文字颜色
  function lum(hex) {
    const c = [1, 3, 5].map(i => parseInt(hex.substr(i, 2), 16) / 255)
      .map(v => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)));
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  }
  function isLightMode() {
    if (state.theme === 'light') return true;
    if (state.theme === 'dark') return false;
    return !!(global.matchMedia && global.matchMedia('(prefers-color-scheme: light)').matches);
  }

  /* ---------- 应用到 DOM ---------- */
  function apply() {
    const root = document.documentElement;
    const light = isLightMode();

    // 主题
    if (state.theme === 'system') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', state.theme);

    // 主题色
    const base = state.accent || DEFAULT_ACCENT[light ? 'light' : 'dark'];
    root.style.setProperty('--accent', base);
    root.style.setProperty('--accent2', mixHex(base, '#58a6ff', 0.55));

    // Material You 派生色（安卓 11 动态取色的近似）
    // 遵循 Material 3 的色调规则：
    //   亮色模式 -> 容器是主色的极浅色调(90)，其上文字是主色的深色调(10)
    //   暗色模式 -> 容器是主色的深色调(30)，其上文字是主色的浅色调(90)
    const lightMode = light;
    const tone = c => mixHex(c, lightMode ? '#ffffff' : '#000000', lightMode ? 0.82 : 0.62);
    const onTone = c => mixHex(c, lightMode ? '#000000' : '#ffffff', lightMode ? 0.72 : 0.88);
    root.style.setProperty('--md-primary', base);
    root.style.setProperty('--md-on-primary', lum(base) < 0.45 ? '#ffffff' : '#1c1c1e');
    root.style.setProperty('--md-primary-container', tone(base));
    root.style.setProperty('--md-on-primary-container', onTone(base));
    root.style.setProperty('--md-secondary-container', tone(mixHex(base, '#58a6ff', 0.45)));
    root.style.setProperty('--md-on-secondary-container', onTone(mixHex(base, '#58a6ff', 0.45)));

    // 毛玻璃参数
    root.style.setProperty('--glass-alpha', state.glassAlpha + '%');
    root.style.setProperty('--glass-blur', state.glassBlur + 'px');
    root.style.setProperty('--glass-edge', state.glassEdge + '%');
    root.style.setProperty('--glass-sat', (140 + Math.round(state.glassAlpha * 0.4)) + '%');

    // 开关
    root.setAttribute('data-glass', state.glass ? 'on' : 'off');
    root.setAttribute('data-capsule', state.capsule ? 'on' : 'off');
    root.setAttribute('data-md', state.md ? 'on' : 'off');
    root.setAttribute('data-motion', state.motion ? 'on' : 'off');

    // 控件状态同步
    syncControls();
    save();
  }

  /* ---------- 控件渲染与绑定 ---------- */
  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }

  function buildSwatches() {
    const box = $('#swatches');
    if (!box) return;
    box.innerHTML = ACCENT_PRESETS.map(p =>
      `<button type="button" class="swatch-btn" data-c="${p.c}" title="${p.n}" aria-label="主题色 ${p.n}" style="background:${p.c}"></button>`
    ).join('');
    $$('#swatches .swatch-btn').forEach(b => b.onclick = () => {
      state.accent = b.dataset.c; apply();
    });
  }

  function syncControls() {
    const label = state.theme === 'system' ? '跟随系统' : (state.theme === 'dark' ? '暗黑' : '亮色');
    const tl = $('#themeLabel'); if (tl) tl.textContent = label;
    $$('#themeSeg button').forEach(b => b.classList.toggle('on', b.dataset.t === state.theme));
    $$('#swatches .swatch-btn').forEach(b =>
      b.classList.toggle('on', (b.dataset.c || '').toLowerCase() === state.accent.toLowerCase()));
    const pick = $('#accentPick');
    if (pick) pick.value = state.accent || (isLightMode() ? DEFAULT_ACCENT.light : DEFAULT_ACCENT.dark);

    const g = $('#glassSwitch'); if (g) g.checked = state.glass;
    const c = $('#capsuleSwitch'); if (c) c.checked = state.capsule;
    const m = $('#mdSwitch'); if (m) m.checked = state.md;
    const mo = $('#motionSwitch'); if (mo) mo.checked = state.motion;
    const ga = $('#glassAlpha'); if (ga) {
      ga.value = state.glassAlpha;
      const out = $('#glassAlphaVal'); if (out) out.textContent = state.glassAlpha + '%';
    }
    const gb = $('#glassBlur'); if (gb) {
      gb.value = state.glassBlur;
      const out = $('#glassBlurVal'); if (out) out.textContent = state.glassBlur + 'px';
    }
    const ge = $('#glassEdge'); if (ge) {
      ge.value = state.glassEdge;
      const out = $('#glassEdgeVal'); if (out) out.textContent = state.glassEdge + '%';
    }
    const gb2 = $('#glassBlock'); if (gb2) gb2.hidden = !state.glass;
  }

  function bindControls() {
    $$('#themeSeg button').forEach(b => b.onclick = () => { state.theme = b.dataset.t; apply(); });

    const pick = $('#accentPick');
    if (pick) pick.addEventListener('input', e => { state.accent = e.target.value; apply(); });
    const reset = $('#accentReset');
    if (reset) reset.onclick = () => { state.accent = ''; apply(); };

    const g = $('#glassSwitch');
    if (g) g.onchange = () => { state.glass = g.checked; apply(); };
    const c = $('#capsuleSwitch');
    if (c) c.onchange = () => { state.capsule = c.checked; apply(); };
    const m = $('#mdSwitch');
    if (m) m.onchange = () => { state.md = m.checked; apply(); };
    const mo = $('#motionSwitch');
    if (mo) mo.onchange = () => { state.motion = mo.checked; apply(); };

    const bindRange = (id, key) => {
      const el = $(id); if (!el) return;
      el.addEventListener('input', e => { state[key] = parseInt(e.target.value, 10) || 0; apply(); });
    };
    bindRange('#glassAlpha', 'glassAlpha');
    bindRange('#glassBlur', 'glassBlur');
    bindRange('#glassEdge', 'glassEdge');

    const preset = $('#glassPreset');
    if (preset) preset.onclick = () => {
      state.glassAlpha = 72; state.glassBlur = 18; state.glassEdge = 45; apply();
    };
    const off = $('#glassOff');
    if (off) off.onclick = () => { state.glass = false; apply(); };
    const all = $('#uiReset');
    if (all) all.onclick = () => {
      Object.assign(state, { theme: 'system', accent: '', glass: true, glassAlpha: 72,
        glassBlur: 18, glassEdge: 45, capsule: false, md: false, motion: true });
      apply(); toast('已恢复默认外观');
    };

    // 标题旁的外观按钮 + 点击外部关闭
    const btn = $('#themeBtn'), pop = $('#themePop');
    if (btn && pop) {
      btn.onclick = e => { e.stopPropagation(); pop.classList.toggle('show'); };
      document.addEventListener('click', e => {
        if (pop.classList.contains('show') && !pop.contains(e.target) && e.target !== btn) {
          pop.classList.remove('show');
        }
      });
      document.addEventListener('keydown', e => {
        if (e.key === 'Escape') pop.classList.remove('show');
      });
    }
  }

  /* ---------- Material 涟漪 ---------- */
  function mountRipple() {
    document.addEventListener('pointerdown', e => {
      if (!state.md || !state.motion) return;
      const t = e.target.closest('button,.btn,.nav-tab,.ccard');
      if (!t) return;
      const r = t.getBoundingClientRect();
      const size = Math.max(r.width, r.height);
      const span = document.createElement('span');
      span.className = 'ripple';
      span.style.width = span.style.height = size + 'px';
      span.style.left = (e.clientX - r.left - size / 2) + 'px';
      span.style.top = (e.clientY - r.top - size / 2) + 'px';
      t.appendChild(span);
      setTimeout(() => span.remove(), 650);
    }, { passive: true });
  }

  /* ---------- 系统主题跟随 ---------- */
  function watchSystem() {
    if (!global.matchMedia) return;
    const mq = global.matchMedia('(prefers-color-scheme: light)');
    const onChange = () => { if (state.theme === 'system') apply(); };
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);
  }

  function toast(t) {
    const el = $('#toast');
    if (!el) return;
    el.textContent = t; el.classList.add('show');
    setTimeout(() => el.classList.remove('show'), 1500);
  }

  function init(opts) {
    opts = opts || {};
    load();
    buildSwatches();
    bindControls();
    mountRipple();
    watchSystem();
    apply();
    if (opts.onReady) opts.onReady();
  }

  global.XDUI = {
    init, apply, state,
    get: k => state[k],
    set: (k, v) => { state[k] = v; apply(); },
    toggle: k => { state[k] = !state[k]; apply(); },
    reset: () => {
      Object.assign(state, { theme: 'system', accent: '', glass: true, glassAlpha: 72,
        glassBlur: 18, glassEdge: 45, capsule: false, md: false, motion: true });
      apply();
    },
    mixHex
  };
})(window);
