"""MCP 浏览器授权页（服务端渲染的自包含 HTML）。

设计目标
--------
- **自包含**：样式全部内联，不依赖站点 CSS 资源路径，
  在任何网关前缀 / 反向代理下都能正常显示。
- **视觉一致**：沿用站点的暗色玻璃拟态语言，与「我的账号」页观感统一。
- **信息透明**：授权前明确展示「哪个客户端 / 要哪些权限 / 回调到哪里」，
  避免用户盲授权。
"""

from __future__ import annotations

import html
import urllib.parse

from typing import Any

import mcp_store

_STYLE = """
:root{
  color-scheme: dark;
  --bg:#0e1116; --panel:rgba(28,35,44,.72); --panel2:rgba(22,27,34,.9);
  --line:rgba(255,255,255,.10); --fg:#e6edf3; --dim:#8b98a5;
  --accent:#f0883e; --accent2:#58a6ff; --ok:#3fb950; --bad:#f85149; --warn:#d29922;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  min-height:100vh;background:
    radial-gradient(900px 520px at 12% -10%, rgba(240,136,62,.20), transparent 62%),
    radial-gradient(760px 460px at 96% 4%, rgba(88,166,255,.16), transparent 60%),
    var(--bg);
  color:var(--fg);
  font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  display:flex;align-items:center;justify-content:center;padding:28px 16px;
}
.card{
  width:100%;max-width:520px;background:var(--panel);border:1px solid var(--line);
  border-radius:22px;padding:26px 24px;backdrop-filter:blur(18px);
  -webkit-backdrop-filter:blur(18px);box-shadow:0 20px 54px rgba(0,0,0,.42);
}
.eyebrow{font-size:12px;letter-spacing:.16em;color:var(--accent);font-weight:700;text-transform:uppercase}
h1{font-size:21px;margin:8px 0 6px;line-height:1.4}
.sub{color:var(--dim);font-size:13.5px;line-height:1.75;margin:0 0 18px}
.client{
  display:flex;align-items:center;gap:12px;background:var(--panel2);
  border:1px solid var(--line);border-radius:14px;padding:13px 14px;margin-bottom:16px;
}
.logo{
  width:40px;height:40px;flex:0 0 40px;border-radius:12px;display:flex;align-items:center;
  justify-content:center;font-size:19px;font-weight:800;color:#1b1207;
  background:linear-gradient(135deg,var(--accent),#ffb27a);
}
.cname{font-size:15px;font-weight:650;word-break:break-all}
.cid{font-size:11.5px;color:var(--dim);font-family:ui-monospace,Menlo,Consolas,monospace;
  word-break:break-all;margin-top:2px}
h2{font-size:13px;margin:0 0 9px;color:var(--dim);font-weight:600;letter-spacing:.04em}
.scopes{display:flex;flex-direction:column;gap:8px;margin-bottom:16px}
.scope{display:flex;gap:9px;align-items:flex-start;font-size:13px;line-height:1.6;
  background:var(--panel2);border:1px solid var(--line);border-radius:11px;padding:9px 12px}
.dot{width:6px;height:6px;border-radius:50%;background:var(--ok);margin-top:7px;flex:0 0 6px}
.kv{display:flex;gap:8px;font-size:12px;color:var(--dim);line-height:1.65;margin-bottom:6px;
  word-break:break-all}
.kv b{color:var(--fg);font-weight:500}
.ops{display:flex;gap:10px;margin-top:20px}
button{
  flex:1;border:1px solid var(--line);background:var(--panel2);color:var(--fg);
  border-radius:12px;padding:12px 16px;font-size:14px;font-weight:600;cursor:pointer;
  font-family:inherit;transition:transform .14s ease,filter .14s ease;
}
button:hover{transform:translateY(-1px)}
button.primary{background:linear-gradient(135deg,var(--accent),#ffab63);border-color:transparent;color:#1b1207}
.note{font-size:12px;color:var(--dim);line-height:1.75;margin:14px 0 0;
  border-top:1px solid var(--line);padding-top:13px}
.note b{color:var(--warn)}
.err{background:rgba(248,81,73,.12);border:1px solid rgba(248,81,73,.4);
  color:#ffb4ae;border-radius:12px;padding:12px 14px;font-size:13px;line-height:1.7;margin-bottom:16px}
.tip{background:rgba(88,166,255,.10);border:1px solid rgba(88,166,255,.32);
  color:#bcd9ff;border-radius:12px;padding:12px 14px;font-size:13px;line-height:1.7;margin-bottom:16px}
a{color:var(--accent2)}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11.5px;
  background:rgba(255,255,255,.06);padding:1px 5px;border-radius:5px}
@media(max-width:420px){
  .card{padding:20px 16px;border-radius:18px}
  .ops{flex-direction:column-reverse}
}
"""


def _esc(v: Any) -> str:
    return html.escape(str(v if v is not None else ""))


def _scope_items(scope: str) -> list[tuple[str, str]]:
    """把 scope 字符串拆成 (scope, 描述) 列表。"""
    out = []
    for s in (scope or "").split():
        out.append((s, mcp_store.SCOPES.get(s, "自定义权限")))
    return out


def _shell(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<meta name=\"referrer\" content=\"no-referrer\">"
        "<title>" + _esc(title) + "</title><style>" + _STYLE + "</style></head><body>"
        "<div class=\"card\">" + body + "</div></body></html>"
    )


def consent_page(
    *,
    client_name: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str,
    login_url: str,
    logged_in: bool,
    account_label: str = "",
    error: str = "",
) -> str:
    """渲染授权确认页。"""
    if error:
        err_block = '<div class="err">' + _esc(error) + "</div>"
    else:
        err_block = ""

    if not logged_in:
        body = (
            '<div class="eyebrow">MCP Authorization</div>'
            "<h1>请先登录后再授权</h1>"
            '<p class="sub">授权需要确认你的身份。点击下面的按钮打开登录页，'
            "登录完成后回到本页面再点一次「授权」即可。</p>"
            + err_block
            + '<div class="tip">登录页会自动在新标签打开，登录后切回本标签刷新即可。</div>'
            '<div class="ops">'
            '<a href="' + _esc(login_url) + '" target="_blank" rel="noopener" '
            'style="flex:1;text-decoration:none"><button class="primary" type="button">'
            "打开登录页</button></a>"
            '<button type="button" onclick="location.reload()">我已登录，刷新本页</button>'
            "</div>"
            '<p class="note">登录用的邮箱与本站账号一致。授权完成后，AI 客户端只能在'
            "<b>你授权的权限范围</b>内访问数据。</p>"
        )
        return _shell("请先登录 · 小豆邀请码 MCP 授权", body)

    scopes = _scope_items(scope) or [(s, mcp_store.SCOPES.get(s, "")) for s in mcp_store.DEFAULT_SCOPES]
    scope_html = "".join(
        '<div class="scope"><span class="dot"></span><span><b>' + _esc(name) + "</b> —— "
        + _esc(desc) + "</span></div>"
        for name, desc in scopes
    )

    redirect_html = ""
    if redirect_uri:
        redirect_html = (
            '<div class="kv"><b>回调地址</b><br><code>' + _esc(redirect_uri[:200]) + "</code></div>"
        )

    body = (
        '<div class="eyebrow">MCP Authorization</div>'
        "<h1>授权 AI 客户端连接</h1>"
        '<p class="sub">该客户端请求访问你的小豆邀请码账号。授权后它可以用你的身份'
        "读取邀请码、发布邀请码与分享链接。你随时可以在「我的账号」里解除授权。</p>"
        + err_block
        + '<div class="client"><div class="logo">AI</div><div style="min-width:0">'
        '<div class="cname">' + _esc(client_name or "未命名客户端") + "</div>"
        '<div class="cid">' + _esc(client_id) + "</div></div></div>"
        + "<h2>请求的权限</h2>"
        + '<div class="scopes">' + scope_html + "</div>"
        + '<div class="kv"><b>当前账号</b> ' + _esc(account_label or "") + "</div>"
        + redirect_html
        + '<form method="post" action="">'
        '<input type="hidden" name="client_id" value="' + _esc(client_id) + '">'
        '<input type="hidden" name="redirect_uri" value="' + _esc(redirect_uri) + '">'
        '<input type="hidden" name="scope" value="' + _esc(scope) + '">'
        '<input type="hidden" name="state" value="' + _esc(state) + '">'
        '<input type="hidden" name="code_challenge" value="' + _esc(code_challenge) + '">'
        '<input type="hidden" name="code_challenge_method" value="' + _esc(code_challenge_method) + '">'
        '<div class="ops">'
        '<button type="submit" name="decision" value="deny">拒绝</button>'
        '<button type="submit" name="decision" value="approve" class="primary">授权</button>'
        "</div></form>"
        + '<p class="note">注意：授权后该客户端<b>可以代替你发布邀请码</b>。'
        "若不认识此客户端，请选择「拒绝」。</p>"
    )
    return _shell("授权 · 小豆邀请码 MCP 授权", body)


def login_required_page(*, login_url: str, message: str = "") -> str:
    """未登录 / 登录态失效时的提示页。"""
    body = (
        '<div class="eyebrow">MCP Authorization</div>'
        "<h1>需要登录</h1>"
        '<p class="sub">' + _esc(message or "请先登录后再进行授权操作。") + "</p>"
        '<div class="ops"><a href="' + _esc(login_url) + '" target="_blank" rel="noopener" '
        'style="flex:1;text-decoration:none"><button class="primary" type="button">'
        "打开登录页</button></a></div>"
    )
    return _shell("需要登录 · 小豆邀请码 MCP 授权", body)


def build_login_url(base_url: str, next_url: str = "") -> str:
    """拼一个登录页地址（登录后可用 next 回到原页面）。"""
    base = (base_url or "").rstrip("/")
    if not base:
        return "/"
    if next_url:
        return base + "/?next=" + urllib.parse.quote(next_url, safe="")
    return base + "/"
