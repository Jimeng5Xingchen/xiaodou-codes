/* ==========================================================================
   管理页逻辑：系统邮箱授权 / 注册策略 / 用户组 / 用户治理
   作为独立模块挂载到 window.XDAdmin，页面在切换到「管理」页时按需初始化。
   ========================================================================== */
(function () {
  'use strict';

  var $ = function (s) { return document.querySelector(s); };
  var $$ = function (s) { return Array.prototype.slice.call(document.querySelectorAll(s)); };

  var state = {
    groups: [],
    mailAccounts: [],
    settings: {},
    editingMailId: 0,
    loaded: false
  };

  function token() {
    try { return localStorage.getItem('xd_token') || ''; } catch (e) { return ''; }
  }

  function isAdmin() {
    return !!(window.ME && window.ME.role === 'admin');
  }

  function toast(t) {
    var el = $('#toast');
    if (!el) return;
    el.textContent = t;
    el.classList.add('show');
    setTimeout(function () { el.classList.remove('show'); }, 1800);
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  async function req(path, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    opts.headers['X-User-Token'] = token();
    if (opts.body && typeof opts.body !== 'string') {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.body);
    }
    var r = await fetch(apiUrl(path), opts);
    var j = null;
    try { j = await r.json(); } catch (e) { j = { ok: false, error: '响应解析失败' }; }
    if (!j.ok && j.error) toast(j.error);
    return j;
  }

  function msg(id, text, cls) {
    var el = $(id);
    if (!el) return;
    el.textContent = text || '';
    el.className = 'mmsg ' + (cls || '');
  }

  // ------------------------------------------------------------------
  // 加载
  // ------------------------------------------------------------------

  async function load() {
    if (!isAdmin()) {
      $('#adminPanel').hidden = true;
      $('#adminGuest').hidden = false;
      return;
    }
    $('#adminPanel').hidden = false;
    $('#adminGuest').hidden = true;

    var j = await req('/api/admin/overview');
    if (!j.ok) return;
    state.settings = j.settings || {};
    state.groups = j.groups || [];
    state.mailAccounts = j.mailAccounts || [];

    renderSettings();
    renderGroups();
    renderMail();
    renderGroupFilter();
    await loadUsers();
    state.loaded = true;
  }

  // ------------------------------------------------------------------
  // 注册策略
  // ------------------------------------------------------------------

  function renderSettings() {
    var s = state.settings || {};
    $('#setAllowSpecial').checked = !!s.allowSpecialEmail;
    $('#setRegEnabled').checked = !!s.registrationEnabled;
    $('#setWhitelist').value = (s.emailWhitelist || []).join('\n');
  }

  async function saveSettings() {
    var btn = $('#setSaveBtn');
    btn.disabled = true;
    var j = await req('/api/admin/settings', {
      method: 'POST',
      body: {
        allowSpecialEmail: $('#setAllowSpecial').checked,
        registrationEnabled: $('#setRegEnabled').checked,
        emailWhitelist: $('#setWhitelist').value
      }
    });
    btn.disabled = false;
    if (j.ok) {
      state.settings = j.settings || state.settings;
      renderSettings();
      msg('#setMsg', '设置已保存', 'ok');
      toast('注册策略已保存');
    } else {
      msg('#setMsg', j.error || '保存失败', 'err');
    }
  }

  // ------------------------------------------------------------------
  // 用户组
  // ------------------------------------------------------------------

  function renderGroups() {
    var box = $('#groupList');
    $('#groupCount').textContent = state.groups.length;
    if (!state.groups.length) {
      box.innerHTML = '<div class="adm-empty">尚未创建用户组。</div>';
      return;
    }
    box.innerHTML = state.groups.map(function (g) {
      return '<div class="adm-group">' +
        '<div class="gname">' + esc(g.name) +
          (g.id === 0 ? '' : '') + '</div>' +
        (g.description ? '<div class="gdesc">' + esc(g.description) + '</div>' : '') +
        '<div class="gmeta">成员 ' + g.members + ' 人</div>' +
        '<div class="gops">' +
          '<button class="adm-btn" data-gedit="' + g.id + '">编辑</button>' +
          '<button class="adm-btn danger" data-gdel="' + g.id + '">删除</button>' +
        '</div>' +
      '</div>';
    }).join('');

    box.querySelectorAll('[data-gedit]').forEach(function (b) {
      b.onclick = function () { editGroup(b.dataset.gedit); };
    });
    box.querySelectorAll('[data-gdel]').forEach(function (b) {
      b.onclick = function () { delGroup(b.dataset.gdel); };
    });
  }

  function editGroup(id) {
    var g = state.groups.filter(function (x) { return String(x.id) === String(id); })[0];
    if (!g) return;
    $('#groupName').value = g.name;
    $('#groupDesc').value = g.description || '';
    $('#groupName').dataset.editId = g.id;
    $('#groupAddBtn').textContent = '保存修改';
    $('#groupName').focus();
  }

  function resetGroupForm() {
    $('#groupName').value = '';
    $('#groupDesc').value = '';
    delete $('#groupName').dataset.editId;
    $('#groupAddBtn').textContent = '创建';
  }

  async function addGroup() {
    var editId = $('#groupName').dataset.editId;
    var name = $('#groupName').value.trim();
    var desc = $('#groupDesc').value.trim();
    if (!name) { msg('#groupMsg', '请输入分组名称', 'err'); return; }
    var j = editId
      ? await req('/api/admin/groups/' + editId, { method: 'PUT', body: { name: name, description: desc } })
      : await req('/api/admin/groups', { method: 'POST', body: { name: name, description: desc } });
    if (j.ok) {
      state.groups = j.groups || state.groups;
      renderGroups();
      renderGroupFilter();
      resetGroupForm();
      msg('#groupMsg', j.message || '已保存', 'ok');
    } else {
      msg('#groupMsg', j.error || '操作失败', 'err');
    }
  }

  async function delGroup(id) {
    var g = state.groups.filter(function (x) { return String(x.id) === String(id); })[0];
    if (!g) return;
    if (!confirm('确认删除分组「' + g.name + '」？组内成员将回到「未分组」。')) return;
    var j = await req('/api/admin/groups/' + id, { method: 'DELETE' });
    if (j.ok) {
      state.groups = j.groups || [];
      renderGroups();
      renderGroupFilter();
      toast('分组已删除');
    }
  }

  function renderGroupFilter() {
    var sel = $('#userGroupFilter');
    if (!sel) return;
    var cur = sel.value;
    var html = '<option value="">全部分组</option>';
    state.groups.forEach(function (g) {
      html += '<option value="' + g.id + '">' + esc(g.name) + '</option>';
    });
    sel.innerHTML = html;
    sel.value = cur;
  }

  // ------------------------------------------------------------------
  // 用户列表
  // ------------------------------------------------------------------

  async function loadUsers() {
    var q = ($('#userSearch').value || '').trim();
    var g = $('#userGroupFilter').value;
    var st = $('#userStatusFilter').value;
    var path = '/api/admin/users?limit=300';
    if (q) path += '&q=' + encodeURIComponent(q);
    if (g) path += '&group=' + encodeURIComponent(g);
    if (st) path += '&status=' + encodeURIComponent(st);
    var box = $('#userTableBox');
    box.innerHTML = '<div class="adm-empty">加载中…</div>';
    var j = await req(path);
    if (!j.ok) { box.innerHTML = '<div class="adm-empty">加载失败</div>'; return; }
    renderUsers(j.users || [], j.total || 0);
  }

  function renderUsers(users, total) {
    $('#userCount').textContent = users.length;
    $('#userTotal').textContent = '共 ' + total + ' 位用户';
    var box = $('#userTableBox');
    if (!users.length) {
      box.innerHTML = '<div class="adm-empty">没有符合条件的用户。</div>';
      return;
    }
    var groupOptions = '<option value="0">未分组</option>' + state.groups.map(function (g) {
      return '<option value="' + g.id + '">' + esc(g.name) + '</option>';
    }).join('');

    var body = users.map(function (u) {
      var badges = [];
      badges.push('<span class="adm-badge ' + (u.status === 'banned' ? 'bad' : 'ok') + '">' +
        (u.status === 'banned' ? '已封禁' : '正常') + '</span>');
      if (u.role === 'admin') badges.push('<span class="adm-badge admin">管理员</span>');
      badges.push('<span class="adm-badge group">' + esc(u.groupName) + '</span>');
      return '<tr data-uid="' + u.id + '">' +
        '<td><div class="adm-email">' + esc(u.email) + '</div>' +
          '<div style="font-size:11px;color:var(--dim)">' + esc(u.username || '（未设置用户名）') +
          ' · 分享 ' + u.submissions + ' 条</div></td>' +
        '<td>' + badges.join(' ') + '</td>' +
        '<td><select class="adm-select" data-gsel="' + u.id + '" style="width:110px;padding:4px 7px;font-size:11.5px">' +
          groupOptions.replace('value="' + u.groupId + '"', 'value="' + u.groupId + '" selected') +
        '</select></td>' +
        '<td class="adm-time">' + fmtTime(u.lastLogin) + '</td>' +
        '<td class="adm-act">' +
          (u.status === 'banned'
            ? '<button class="adm-btn" data-unban="' + u.id + '">解封</button>'
            : '<button class="adm-btn warn" data-ban="' + u.id + '">封号</button>') +
          '<button class="adm-btn" data-pwd="' + u.id + '">改密</button>' +
          '<button class="adm-btn" data-role="' + u.id + '" data-nex="' + (u.role === 'admin' ? 'user' : 'admin') + '">' +
            (u.role === 'admin' ? '取消管理' : '设为管理') + '</button>' +
          '<button class="adm-btn danger" data-del="' + u.id + '">删除</button>' +
        '</td></tr>';
    }).join('');

    box.innerHTML = '<div class="adm-table-wrap"><table class="adm-table">' +
      '<thead><tr><th>用户</th><th>状态</th><th>分类</th><th>最近登录</th><th style="text-align:right">操作</th></tr></thead>' +
      '<tbody>' + body + '</tbody></table></div>';

    box.querySelectorAll('[data-gsel]').forEach(function (sel) {
      sel.onchange = async function () {
        var j = await req('/api/admin/users/' + sel.dataset.gsel + '/group', {
          method: 'POST', body: { groupId: parseInt(sel.value, 10) || 0 }
        });
        if (j.ok) { toast('分类已更新'); loadUsers(); }
        else { toast(j.error || '更新失败'); }
      };
    });
    box.querySelectorAll('[data-ban]').forEach(function (b) {
      b.onclick = function () { banUser(b.dataset.ban); };
    });
    box.querySelectorAll('[data-unban]').forEach(function (b) {
      b.onclick = async function () {
        var j = await req('/api/admin/users/' + b.dataset.unban + '/status', {
          method: 'POST', body: { status: 'active' }
        });
        if (j.ok) { toast('已解封'); loadUsers(); }
      };
    });
    box.querySelectorAll('[data-pwd]').forEach(function (b) {
      b.onclick = function () { resetPwd(b.dataset.pwd); };
    });
    box.querySelectorAll('[data-role]').forEach(function (b) {
      b.onclick = async function () {
        var j = await req('/api/admin/users/' + b.dataset.role + '/role', {
          method: 'POST', body: { role: b.dataset.nex }
        });
        if (j.ok) { toast('角色已更新'); loadUsers(); }
      };
    });
    box.querySelectorAll('[data-del]').forEach(function (b) {
      b.onclick = function () { delUser(b.dataset.del); };
    });
  }

  function fmtTime(ts) {
    if (!ts) return '—';
    var d = new Date(ts * 1000);
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
           p(d.getHours()) + ':' + p(d.getMinutes());
  }

  async function banUser(id) {
    var reason = prompt('请输入封禁原因（可留空）：', '');
    if (reason === null) return;
    var j = await req('/api/admin/users/' + id + '/status', {
      method: 'POST', body: { status: 'banned', reason: reason }
    });
    if (j.ok) { toast('已封禁'); loadUsers(); }
  }

  async function delUser(id) {
    if (!confirm('确认删除该用户？其分享记录与登录态将一并清除，且不可恢复。')) return;
    var j = await req('/api/admin/users/' + id, { method: 'DELETE' });
    if (j.ok) { toast('用户已删除'); loadUsers(); }
  }

  function resetPwd(id) {
    var pw = prompt('请输入新密码（6-32 位），该用户需重新登录：', '');
    if (pw === null) return;
    if (pw.length < 6 || pw.length > 32) { toast('密码需 6-32 位'); return; }
    req('/api/admin/users/' + id + '/password', { method: 'POST', body: { password: pw } })
      .then(function (j) { if (j.ok) toast('密码已重置'); });
  }

  // ------------------------------------------------------------------
  // 授权发件邮箱
  // ------------------------------------------------------------------

  function renderMail() {
    var box = $('#mailList');
    $('#mailCount').textContent = state.mailAccounts.length;
    if (!state.mailAccounts.length) {
      box.innerHTML = '<div class="adm-empty">尚未配置发件邮箱。未配置时验证码将以开发模式直接显示在登录页。</div>';
      return;
    }
    box.innerHTML = state.mailAccounts.map(function (m) {
      var tags = [];
      tags.push('<span class="adm-badge ' + (m.enabled ? 'ok' : 'bad') + '">' + (m.enabled ? '启用' : '停用') + '</span>');
      if (m.isDefault) tags.push('<span class="adm-badge admin">默认</span>');
      if (m.provider === 'dev') tags.push('<span class="adm-badge">开发模式</span>');
      else if (!m.hasPassword) tags.push('<span class="adm-badge bad">缺授权码</span>');
      return '<div class="adm-mail">' +
        '<div class="maddr">' + esc(m.address) + '</div>' +
        '<div class="mhost">' + esc(m.host || '—') + ':' + m.port +
          (m.useSsl ? ' SSL' : '') + '</div>' +
        '<div>' + tags.join(' ') + '</div>' +
        '<div class="mops">' +
          (m.note ? '<span style="font-size:11px;color:var(--dim)">' + esc(m.note) + '</span>' : '') +
          (m.isDefault ? '' : '<button class="adm-btn" data-mdef="' + m.id + '">设为默认</button>') +
          '<button class="adm-btn" data-medit="' + m.id + '">编辑</button>' +
          '<button class="adm-btn danger" data-mdel="' + m.id + '">删除</button>' +
        '</div>' +
      '</div>';
    }).join('');

    box.querySelectorAll('[data-mdef]').forEach(function (b) {
      b.onclick = async function () {
        var j = await req('/api/admin/mail-accounts/' + b.dataset.mdef + '/default', { method: 'POST' });
        if (j.ok) { state.mailAccounts = j.mailAccounts || []; renderMail(); toast('已设为默认'); }
      };
    });
    box.querySelectorAll('[data-medit]').forEach(function (b) {
      b.onclick = function () { editMail(b.dataset.medit); };
    });
    box.querySelectorAll('[data-mdel]').forEach(function (b) {
      b.onclick = async function () {
        if (!confirm('确认删除该发件邮箱？')) return;
        var j = await req('/api/admin/mail-accounts/' + b.dataset.mdel, { method: 'DELETE' });
        if (j.ok) { state.mailAccounts = j.mailAccounts || []; renderMail(); toast('已删除'); }
      };
    });
  }

  function editMail(id) {
    var m = state.mailAccounts.filter(function (x) { return String(x.id) === String(id); })[0];
    if (!m) return;
    state.editingMailId = m.id;
    $('#mailAddr').value = m.address;
    $('#mailProvider').value = m.provider;
    $('#mailHost').value = m.host;
    $('#mailPort').value = m.port;
    $('#mailPass').value = '';
    $('#mailNote').value = m.note || '';
    $('#mailSsl').checked = !!m.useSsl;
    $('#mailEnabled').checked = !!m.enabled;
    $('#mailDefault').checked = !!m.isDefault;
    $('#mailSaveBtn').textContent = '保存修改';
    $('#mailCancelBtn').hidden = false;
    $('#mailAddr').scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function resetMailForm() {
    state.editingMailId = 0;
    $('#mailAddr').value = '';
    $('#mailProvider').value = 'smtp';
    $('#mailHost').value = '';
    $('#mailPort').value = '465';
    $('#mailPass').value = '';
    $('#mailNote').value = '';
    $('#mailSsl').checked = true;
    $('#mailEnabled').checked = true;
    $('#mailDefault').checked = false;
    $('#mailSaveBtn').textContent = '保存发件邮箱';
    $('#mailCancelBtn').hidden = true;
  }

  async function saveMail() {
    var btn = $('#mailSaveBtn');
    var body = {
      id: state.editingMailId,
      address: $('#mailAddr').value.trim(),
      provider: $('#mailProvider').value,
      host: $('#mailHost').value.trim(),
      port: parseInt($('#mailPort').value, 10) || 465,
      useSsl: $('#mailSsl').checked,
      password: $('#mailPass').value,
      enabled: $('#mailEnabled').checked,
      isDefault: $('#mailDefault').checked,
      note: $('#mailNote').value.trim()
    };
    btn.disabled = true;
    var j = await req('/api/admin/mail-accounts', { method: 'POST', body: body });
    btn.disabled = false;
    if (j.ok) {
      state.mailAccounts = j.mailAccounts || [];
      renderMail();
      resetMailForm();
      msg('#mailMsg', '已保存', 'ok');
      toast('发件邮箱已保存');
    } else {
      msg('#mailMsg', j.error || '保存失败', 'err');
    }
  }

  async function testMail() {
    var to = $('#mailTestTo').value.trim();
    if (!to) { msg('#mailMsg', '请填写测试收件邮箱', 'err'); return; }
    var btn = $('#mailTestBtn');
    btn.disabled = true;
    btn.textContent = '发送中…';
    var j = await req('/api/admin/mail-accounts/test', {
      method: 'POST', body: { id: state.editingMailId, to: to }
    });
    btn.disabled = false;
    btn.textContent = '发送测试';
    if (j.ok) msg('#mailMsg', j.message || '测试邮件已发送', 'ok');
    else msg('#mailMsg', j.error || '发送失败', 'err');
  }

  // ------------------------------------------------------------------
  // 初始部署引导
  // ------------------------------------------------------------------

  async function checkSetup() {
    var j;
    try {
      j = await fetch(apiUrl('/api/setup/state')).then(function (r) { return r.json(); });
    } catch (e) { return; }
    if (j && j.ok && !j.initialized) {
      if (j.needKey) {
        var row = $('#suKeyRow');
        if (row) row.hidden = false;
      }
      $('#setupMask').classList.add('show');
    }
  }

  async function submitSetup() {
    var acc = $('#suAccount').value.trim();
    var mail = $('#suEmail').value.trim();
    var pwd = $('#suPwd').value;
    var pwd2 = $('#suPwd2').value;
    var msgEl = $('#suMsg');
    if (!acc) { msgEl.className = 'smsg err'; msgEl.textContent = '请填写登录账号'; return; }
    if (!mail) { msgEl.className = 'smsg err'; msgEl.textContent = '请填写绑定邮箱'; return; }
    if (pwd.length < 6 || pwd.length > 32) {
      msgEl.className = 'smsg err'; msgEl.textContent = '密码需 6-32 位'; return;
    }
    if (pwd !== pwd2) { msgEl.className = 'smsg err'; msgEl.textContent = '两次输入的密码不一致'; return; }
    var btn = $('#suSubmitBtn');
    btn.disabled = true;
    btn.textContent = '创建中…';
    try {
      var r = await fetch(apiUrl('/api/setup/init'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          account: acc, email: mail, password: pwd,
          setupKey: ($('#suKey') ? $('#suKey').value : '').trim()
        })
      });
      var j = await r.json();
      if (j.ok) {
        if (j.token) {
          try { localStorage.setItem('xd_token', j.token); } catch (e) {}
          if (window.XDUI) window.XDUI.set('theme', 'light');
        }
        msgEl.className = 'smsg ok';
        msgEl.textContent = j.message || '初始化完成';
        setTimeout(function () { location.reload(); }, 900);
      } else {
        msgEl.className = 'smsg err';
        msgEl.textContent = j.error || '初始化失败';
        btn.disabled = false;
        btn.textContent = '创建管理员并进入';
      }
    } catch (e) {
      msgEl.className = 'smsg err';
      msgEl.textContent = '请求失败，请重试';
      btn.disabled = false;
      btn.textContent = '创建管理员并进入';
    }
  }

  // ------------------------------------------------------------------
  // 绑定
  // ------------------------------------------------------------------

  function bind() {
    var setBtn = $('#setSaveBtn');
    if (setBtn) setBtn.onclick = saveSettings;
    var gAdd = $('#groupAddBtn');
    if (gAdd) gAdd.onclick = addGroup;
    var gCancel = $('#groupCancelBtn');
    if (gCancel) gCancel.onclick = resetGroupForm;
    var mSave = $('#mailSaveBtn');
    if (mSave) mSave.onclick = saveMail;
    var mCancel = $('#mailCancelBtn');
    if (mCancel) mCancel.onclick = resetMailForm;
    var mTest = $('#mailTestBtn');
    if (mTest) mTest.onclick = testMail;
    var uRefresh = $('#userRefreshBtn');
    if (uRefresh) uRefresh.onclick = loadUsers;
    var uSearch = $('#userSearch');
    if (uSearch) {
      uSearch.oninput = function () {
        clearTimeout(uSearch._t);
        uSearch._t = setTimeout(loadUsers, 320);
      };
    }
    var gf = $('#userGroupFilter');
    if (gf) gf.onchange = loadUsers;
    var sf = $('#userStatusFilter');
    if (sf) sf.onchange = loadUsers;
    var su = $('#suSubmitBtn');
    if (su) su.onclick = submitSetup;
  }

  // 暴露给页面
  window.XDAdmin = {
    load: load,
    checkSetup: checkSetup,
    isAdmin: isAdmin,
    refreshUsers: loadUsers
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
