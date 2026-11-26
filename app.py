"""小豆邀请码抓取站 —— Web 服务。

提供：
  GET /            前端页面
  GET /api/codes   采集结果 JSON
  GET /api/health  健康检查

后台定时刷新采集结果，页面每 20 秒轮询一次 /api/codes。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import auth
import admin
import collector
import mailer
import mcp_server
import mcp_store
import mcp_views

CST = timezone(timedelta(hours=8))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "codes.json")
SUBMITTED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "submitted.json")

REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "180"))

# 用户手动提交 / 书签工具送来的码（已通过官方校验），持久化保存
_submitted: dict[str, dict] = {}

@asynccontextmanager
async def lifespan(_: FastAPI):
    auth.init_db()
    _load_submitted()
    cached = _read_cache()
    if cached:
        with _lock:
            _state.clear()
            _state.update(cached)
    threading.Thread(target=_scheduler, daemon=True).start()
    yield


app = FastAPI(title="小豆邀请码抓码站", docs_url=None, redoc_url=None, lifespan=lifespan)

# 前端可独立托管在 Cloudflare Pages（静态资源全球 CDN），此时页面与 API 跨域。
# 放行自定义登录头 X-User-Token —— 自定义头会触发浏览器预检，未列入 allow_headers 会被拦掉。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id"],
    max_age=86400,
)

_lock = threading.Lock()
_state: dict = {
    "lastUpdated": "",
    "lastUpdatedUtc": "",
    "counts": {"total": 0},
    "errors": [],
    "codes": [],
}
_refreshing = False


def _write_cache(payload: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
    except OSError:
        pass


def _read_cache() -> dict | None:
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _load_submitted() -> None:
    global _submitted
    try:
        with open(SUBMITTED_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _submitted = data
    except (OSError, json.JSONDecodeError):
        _submitted = {}


def _save_submitted() -> None:
    try:
        os.makedirs(os.path.dirname(SUBMITTED_PATH), exist_ok=True)
        with open(SUBMITTED_PATH, "w", encoding="utf-8") as fh:
            json.dump(_submitted, fh, ensure_ascii=False)
    except OSError:
        pass


def _submitted_records() -> list:
    """把已提交且官方校验过的码转成采集记录。"""
    out = []
    for code, info in list(_submitted.items()):
        user_id = int(info.get("user_id") or 0)
        sharer = auth.sharer_info(user_id) if user_id else {}
        # 分享者展示名作为 author，未登录提交保留来源
        author = sharer.get("name") or info.get("author", "")
        # 旧数据无 share_type：带来源链接的按站外分享处理
        share_type = info.get("share_type") or (
            "external" if info.get("source_url") else "site"
        )
        # 站内分享不显示来源；站外分享显示识别到的来源
        if share_type == "external":
            src_label = info.get("source_label") or SOURCE_LABEL_EXTERNAL
            src_url = info.get("source_url", "")
            category = "external"
        else:
            src_label = SOURCE_LABEL_SITE
            src_url = ""
            category = "site"
        out.append(
            collector.CodeRecord(
                code=code,
                source="user_submit",
                source_label=src_label,
                source_url=src_url,
                author=author if share_type == "external" else "",
                title=info.get("title", "用户提交"),
                posted_at=info.get("posted_at", ""),
                posted_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                status="available",
                status_note="用户提交，官方接口校验有效",
                verified=True,
                extra={"user_submitted": True},
                sharer=sharer,
                expire_at=int(info.get("expire_at") or 0),
                category=category,
            )
        )
    return out


SOURCE_LABEL_SITE = "站内分享"
SOURCE_LABEL_EXTERNAL = "站外分享"

# 链接域名 -> 来源标签，用于站外分享自动识别链接源
SOURCE_DOMAINS = [
    ("weibo.com", "微博"), ("t.cn", "微博"),
    ("zhihu.com", "知乎"), ("tieba.baidu.com", "贴吧"),
    ("douban.com", "豆瓣小组"), ("bilibili.com", "B站"), ("b23.tv", "B站"),
    ("xiaohongshu.com", "小红书"), ("xhslink.com", "小红书"),
    ("douyin.com", "抖音"), ("mp.weixin.qq.com", "微信公众号"),
    ("weixin.qq.com", "微信公众号"), ("x.com", "X"), ("twitter.com", "X"),
    ("doubao.com", "官方分享页"), ("activity.doubao.com", "官方分享页"),
]


def detect_source_from_url(url: str) -> str:
    """根据链接域名推断分享来源标签。"""
    low = (url or "").lower()
    for domain, label in SOURCE_DOMAINS:
        if domain in low:
            return label
    return "站外分享"

# 提交框允许直接粘贴链接：服务端抓取该页面文字并识别其中的邀请码
_CODE_RE = re.compile(r"(?<![A-Z0-9])(?:M-[A-Z0-9]{6}|[A-Z0-9]{6})(?![A-Z0-9])")
_URL_RE = re.compile(r"^https?://\S+", re.I)


def fetch_codes_from_url(url: str) -> list[str]:
    """抓取链接页面文字，识别其中的候选邀请码。失败返回空列表。"""
    url = url.strip()
    if not _URL_RE.match(url):
        return []
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read(400_000).decode("utf-8", "ignore")
        # 去掉脚本/样式，减少噪音
        raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", raw)
        text = urllib.parse.unquote(text)
        found: list[str] = []
        for m in _CODE_RE.finditer(text.upper()):
            c = m.group(0)
            if c not in found:
                found.append(c)
        # URL 路径/参数里也可能带码
        for m in _CODE_RE.finditer(urllib.parse.unquote(url).upper()):
            c = m.group(0)
            if c not in found:
                found.append(c)
        return found[:50]
    except Exception as exc:  # noqa: BLE001
        print(f"[fetch_url] failed {url[:80]}: {exc}", flush=True)
        return []


def refresh() -> None:
    global _refreshing
    with _lock:
        if _refreshing:
            return
        _refreshing = True
    try:
        payload = collector.run_once(user_records=_submitted_records())
        with _lock:
            _state.clear()
            _state.update(payload)
        _write_cache(payload)
        print(
            f"[refresh] {payload['lastUpdated']} total={payload['counts'].get('total')} "
            f"available={payload['counts'].get('available')} "
            f"errors={payload.get('errors')}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _state.setdefault("errors", []).append(f"refresh: {exc}")
        print(f"[refresh] failed: {exc}", flush=True)
    finally:
        with _lock:
            _refreshing = False


def _scheduler() -> None:
    while True:
        refresh()
        time.sleep(REFRESH_SECONDS)


@app.get("/")
def index() -> Response:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# 静态资源：CSS / JS / 图片 已从 index.html 拆出为独立文件
app.mount("/css", StaticFiles(directory=os.path.join(STATIC_DIR, "css")), name="css")
app.mount("/js", StaticFiles(directory=os.path.join(STATIC_DIR, "js")), name="js")
app.mount("/img", StaticFiles(directory=os.path.join(STATIC_DIR, "img")), name="img")


@app.get("/api/codes")
def api_codes() -> Response:
    with _lock:
        payload = dict(_state)
    payload["refreshing"] = _refreshing
    payload["refreshSeconds"] = REFRESH_SECONDS
    payload["serverTime"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S 北京时间")
    # 社区直达搜索链接（关键词现拼，供页面「去社区找码」入口使用）
    q = "小豆邀请码"
    payload["communityLinks"] = [
        {
            "name": name,
            "url": template.format(q=urllib.parse.quote(q)),
            "key": key,
        }
        for key, name, template, _ in collector.COMMUNITY_SEARCHES
    ]
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def api_health() -> Response:
    with _lock:
        payload = {
            "ok": True,
            "lastUpdated": _state.get("lastUpdated"),
            "total": _state.get("counts", {}).get("total", 0),
            "available": _state.get("counts", {}).get("available", 0),
            "refreshing": _refreshing,
        }
    payload["mailProvider"] = mailer.provider()
    payload["devMode"] = mailer.dev_mode()
    return JSONResponse(payload)


def _token_from(request: Request) -> str:
    # 注意：发布平台网关会强制覆盖 Authorization 头为它自己的 JWT，
    # 前端传的 Bearer token 到不了这里。因此优先用自定义头和 Cookie。
    custom = request.headers.get("X-User-Token", "").strip()
    if custom:
        return custom
    cookie = request.cookies.get("xd_session", "")
    if cookie:
        return cookie
    # 本地直连（无网关）时兼容标准 Bearer
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return ""


@app.post("/api/auth/send-code")
async def api_send_code(request: Request) -> Response:
    """发送邮箱验证码。dev 模式下响应里直接返回 dev_code。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    email = str(body.get("email") or "").strip().lower()
    result = auth.send_code(email)
    return JSONResponse(result)


@app.post("/api/auth/verify")
async def api_verify(request: Request) -> Response:
    """校验验证码，成功返回会话 token（同时写入 Cookie）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    email = str(body.get("email") or "").strip().lower()
    code = str(body.get("code") or "").strip()
    result = auth.verify_code(email, code)
    return _auth_response(result)


def _auth_response(result: dict) -> Response:
    """统一的登录响应：有 token 时写 HttpOnly Cookie。"""
    resp = JSONResponse(result)
    if result.get("ok") and result.get("token"):
        resp.set_cookie(
            "xd_session",
            result["token"],
            max_age=30 * 86400,
            httponly=True,
            samesite="lax",
        )
    return resp


@app.post("/api/auth/login-password")
async def api_login_password(request: Request) -> Response:
    """密码登录。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    email = str(body.get("email") or "").strip().lower()
    password = str(body.get("password") or "")
    return _auth_response(auth.login_with_password(email, password))


@app.post("/api/auth/reset/send")
async def api_reset_send(request: Request) -> Response:
    """忘记密码：给绑定邮箱发重置验证码。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    return JSONResponse(auth.reset_password_send(str(body.get("email") or "").strip()))


@app.post("/api/auth/reset/confirm")
async def api_reset_confirm(request: Request) -> Response:
    """忘记密码：验证码 + 新密码完成重置。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    return JSONResponse(auth.reset_password_confirm(
        str(body.get("email") or "").strip(),
        str(body.get("code") or "").strip(),
        str(body.get("password") or ""),
    ))


@app.post("/api/auth/password")
async def api_set_password(request: Request) -> Response:
    """已登录用户设置/修改密码。"""
    token = _token_from(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    return JSONResponse(auth.set_password(token, str(body.get("password") or "")))


@app.get("/api/auth/me")
def api_me(request: Request) -> Response:
    token = _token_from(request)
    user = auth.user_by_token(token)
    if not user:
        return JSONResponse({"ok": False, "user": None})
    return JSONResponse({"ok": True, "user": user, "stats": auth.stats(token)})


@app.post("/api/auth/profile")
async def api_profile(request: Request) -> Response:
    """更新当前用户资料（用户名 / 简介 / 性别）。"""
    token = _token_from(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    result = auth.update_profile(
        token,
        username=body.get("username"),
        bio=body.get("bio"),
        gender=body.get("gender"),
    )
    if result.get("ok"):
        # 用户名变化后，让用户已分享的码重新带上新展示名
        threading.Thread(target=refresh, daemon=True).start()
    return JSONResponse(result)


@app.post("/api/auth/change-email/send")
async def api_change_email_send(request: Request) -> Response:
    """给新邮箱发更换验证码。"""
    token = _token_from(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    result = auth.change_email_send(token, str(body.get("email") or "").strip())
    return JSONResponse(result)


@app.post("/api/auth/change-email/confirm")
async def api_change_email_confirm(request: Request) -> Response:
    """校验新邮箱验证码并更换邮箱。"""
    token = _token_from(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    result = auth.change_email_confirm(
        token,
        str(body.get("email") or "").strip(),
        str(body.get("code") or "").strip(),
    )
    if result.get("ok"):
        resp = JSONResponse(result)
        if result.get("user"):
            resp.set_cookie(
                "xd_session", token, max_age=30 * 86400, httponly=True, samesite="lax"
            )
        return resp
    return JSONResponse(result)


@app.post("/api/auth/logout")
def api_logout(request: Request) -> Response:
    auth.logout(_token_from(request))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("xd_session")
    return resp


# ---------------------------------------------------------------------------
# 初始部署引导
# ---------------------------------------------------------------------------

@app.get("/api/setup/state")
def api_setup_state() -> Response:
    """是否已完成初始化。未完成时前端展示「创建管理员」引导页。"""
    return JSONResponse({
        "ok": True,
        "initialized": auth.is_initialized(),
        "mailReady": bool(mailer._effective_account()),
        "provider": mailer.provider(),
        "needKey": bool(auth.setup_key()),
    })


@app.post("/api/setup/init")
async def api_setup_init(request: Request) -> Response:
    """初始部署：填写账号 / 密码 / 邮箱，创建第一个管理员。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    result = auth.setup_initial_admin(
        account=str(body.get("account") or body.get("email") or ""),
        password=str(body.get("password") or ""),
        email=str(body.get("email") or body.get("account") or ""),
        username=str(body.get("username") or ""),
        init_key=str(body.get("setupKey") or ""),
    )
    return _auth_response(result)


# ---------------------------------------------------------------------------
# 账号注销
# ---------------------------------------------------------------------------

@app.post("/api/auth/delete-account")
async def api_delete_account(request: Request) -> Response:
    """注销当前账号（需密码确认）。"""
    token = _token_from(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    result = auth.delete_account(token, str(body.get("password") or ""))
    if result.get("ok"):
        resp = JSONResponse(result)
        resp.delete_cookie("xd_session")
        return resp
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# 管理端 API
# ---------------------------------------------------------------------------

def _require_admin(request: Request) -> tuple[dict | None, Response | None]:
    """管理员鉴权：返回 (管理员, 错误响应)。"""
    actor = admin.require_admin(_token_from(request))
    if not actor:
        return None, JSONResponse({"ok": False, "error": "需要管理员权限"}, status_code=403)
    return actor, None


@app.get("/api/admin/overview")
def api_admin_overview(request: Request) -> Response:
    """管理页总览：系统设置 + 用户组 + 授权发件邮箱。"""
    _, err = _require_admin(request)
    if err:
        return err
    return JSONResponse(admin.get_settings_view())


@app.post("/api/admin/settings")
async def api_admin_settings(request: Request) -> Response:
    """保存系统设置（白名单 / 特殊符号 / 注册开关）。"""
    _, err = _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    return JSONResponse(admin.update_settings(body))


@app.get("/api/admin/users")
def api_admin_users(request: Request) -> Response:
    """用户列表（支持关键字 / 分组 / 状态筛选）。"""
    _, err = _require_admin(request)
    if err:
        return err
    q = request.query_params.get("q", "")
    group = request.query_params.get("group", "")
    status = request.query_params.get("status", "")
    limit = request.query_params.get("limit", "200")
    try:
        gid = int(group) if group not in ("", None) else None
    except ValueError:
        gid = None
    try:
        lim = int(limit)
    except ValueError:
        lim = 200
    return JSONResponse(admin.list_users(q, gid, status, lim))


@app.post("/api/admin/users/{user_id}/status")
async def api_admin_user_status(user_id: int, request: Request) -> Response:
    """封号 / 解封。"""
    actor, err = _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    return JSONResponse(
        admin.set_user_status(actor["id"], user_id, str(body.get("status") or ""),
                              str(body.get("reason") or ""))
    )


@app.post("/api/admin/users/{user_id}/group")
async def api_admin_user_group(user_id: int, request: Request) -> Response:
    """修改用户所属分类（用户组）。"""
    _, err = _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    try:
        gid = int(body.get("groupId") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "分组参数不合法"}, status_code=400)
    return JSONResponse(admin.assign_group(user_id, gid))


@app.post("/api/admin/users/{user_id}/role")
async def api_admin_user_role(user_id: int, request: Request) -> Response:
    """设置用户角色（管理员 / 普通用户）。"""
    actor, err = _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    return JSONResponse(admin.set_user_role(actor["id"], user_id, str(body.get("role") or "")))


@app.post("/api/admin/users/{user_id}/password")
async def api_admin_user_password(user_id: int, request: Request) -> Response:
    """重置指定用户密码。"""
    _, err = _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    return JSONResponse(admin.reset_user_password(user_id, str(body.get("password") or "")))


@app.delete("/api/admin/users/{user_id}")
def api_admin_delete_user(user_id: int, request: Request) -> Response:
    """删除用户。"""
    actor, err = _require_admin(request)
    if err:
        return err
    return JSONResponse(admin.delete_user(actor["id"], user_id))


@app.get("/api/admin/groups")
def api_admin_groups(request: Request) -> Response:
    """用户组列表。"""
    _, err = _require_admin(request)
    if err:
        return err
    return JSONResponse({"ok": True, "groups": admin.list_groups()})


@app.post("/api/admin/groups")
async def api_admin_create_group(request: Request) -> Response:
    """新建用户组。"""
    _, err = _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    return JSONResponse(admin.create_group(str(body.get("name") or ""),
                                           str(body.get("description") or "")))


@app.put("/api/admin/groups/{group_id}")
async def api_admin_update_group(group_id: int, request: Request) -> Response:
    """修改用户组。"""
    _, err = _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    name = body.get("name") if "name" in body else None
    desc = body.get("description") if "description" in body else None
    return JSONResponse(admin.update_group(group_id, name, desc))


@app.delete("/api/admin/groups/{group_id}")
def api_admin_delete_group(group_id: int, request: Request) -> Response:
    """删除用户组。"""
    _, err = _require_admin(request)
    if err:
        return err
    return JSONResponse(admin.delete_group(group_id))


@app.get("/api/admin/mail-accounts")
def api_admin_mail_accounts(request: Request) -> Response:
    """系统授权发件邮箱列表。"""
    _, err = _require_admin(request)
    if err:
        return err
    return JSONResponse({"ok": True, "mailAccounts": admin.list_mail_accounts()})


@app.post("/api/admin/mail-accounts")
async def api_admin_save_mail_account(request: Request) -> Response:
    """新增 / 更新授权发件邮箱。"""
    _, err = _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    return JSONResponse(
        admin.save_mail_account(
            account_id=int(body.get("id") or 0),
            address=str(body.get("address") or ""),
            provider=str(body.get("provider") or "smtp"),
            host=str(body.get("host") or ""),
            port=body.get("port") or 465,
            use_ssl=bool(body.get("useSsl", True)),
            password=str(body.get("password") or ""),
            enabled=bool(body.get("enabled", True)),
            is_default=bool(body.get("isDefault", False)),
            note=str(body.get("note") or ""),
        )
    )


@app.post("/api/admin/mail-accounts/{account_id}/default")
def api_admin_default_mail_account(account_id: int, request: Request) -> Response:
    """设为默认发件邮箱。"""
    _, err = _require_admin(request)
    if err:
        return err
    return JSONResponse(admin.set_default_mail_account(account_id))


@app.delete("/api/admin/mail-accounts/{account_id}")
def api_admin_delete_mail_account(account_id: int, request: Request) -> Response:
    """删除授权发件邮箱。"""
    _, err = _require_admin(request)
    if err:
        return err
    return JSONResponse(admin.delete_mail_account(account_id))


@app.post("/api/admin/mail-accounts/test")
async def api_admin_test_mail_account(request: Request) -> Response:
    """测试某个授权发件邮箱能否正常发信。"""
    _, err = _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    to = str(body.get("to") or "").strip()
    if not to:
        return JSONResponse({"ok": False, "error": "请填写测试收件邮箱"})
    account_id = int(body.get("id") or 0)
    result = mailer.test_account(account_id, to)
    return JSONResponse(result)


@app.get("/api/refresh")
def api_refresh() -> Response:
    """手动触发一次采集（异步执行，立即返回）。"""
    threading.Thread(target=refresh, daemon=True).start()
    return JSONResponse({"ok": True, "message": "已开始后台采集"})


@app.post("/api/submit")
async def api_submit(request: Request) -> Response:
    """接收用户提交的候选码，逐个经官方接口校验，只有真码才收录。

    请求体：{"codes": ["ABC123", "M-XYZ..."], "source": "微博", "url": "..."}
    Cookie / 登录态一律不经过本站；本站只接收码字符串本身。
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)

    token = _token_from(request)
    user = auth.user_by_token(token)
    return JSONResponse(process_submit(body, user))


def process_submit(body: dict, user: dict | None) -> dict:
    """提交处理核心：网页表单与 MCP 工具共用同一套逻辑。"""
    # 分享类型：site 站内共享（必须登录，仅提交码）；external 站外分享（可带链接）
    share_type = str(body.get("share_type") or body.get("shareType") or "external")
    if share_type not in ("site", "external"):
        share_type = "external"

    is_logged_in = bool(user)

    # 站内共享码：必须登录才能分享
    if share_type == "site" and not is_logged_in:
        return {
            "ok": False,
            "needLogin": True,
            "error": "站内共享需要先登录，请登录后分享你的邀请码。",
        }

    raw_codes = body.get("codes") or []
    if isinstance(raw_codes, str):
        raw_codes = re.split(r"[\s,，;；]+", raw_codes)
    # 宽松抽取候选码：用户提交场景不做「必须含数字」过滤，
    # 真码可能是纯字母（如 FVWAHD）；噪音一律交给官方 oracle 兜底。
    loose_re = re.compile(r"(?<![A-Z0-9])(?:M-[A-Z0-9]{6}|[A-Z0-9]{6})(?![A-Z0-9])")
    candidates: list[str] = []
    linked_urls: list[str] = []
    for item in raw_codes:
        if not isinstance(item, str):
            continue
        item = item.strip()
        if not item:
            continue
        # 站外分享支持链接：抓取页面并识别其中的码，链接本身作为原文出处
        if _URL_RE.match(item):
            if share_type == "external":
                linked_urls.append(item[:500])
                for c in fetch_codes_from_url(item):
                    if c not in candidates:
                        candidates.append(c)
            continue
        if share_type == "site":
            # 站内分享只提交码本身
            normalized = item.upper()
            if normalized not in candidates:
                candidates.append(normalized)
            continue
        for match in loose_re.finditer(item.upper()):
            normalized = match.group(0)
            if normalized not in candidates:
                candidates.append(normalized)
    candidates = candidates[:50]
    if not candidates:
        return {
            "ok": False,
            "error": "未识别到邀请码。"
            + ("请直接粘贴邀请码。" if share_type == "site"
               else "可直接粘贴邀请码，或粘贴一条包含邀请码的分享链接让系统自动抓取识别。"),
        }

    # 官方接口前置校验（并发）
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        for code, res in zip(candidates, pool.map(collector.official_verify, candidates)):
            results[code] = res

    source = str(body.get("source") or "用户提交")[:40]
    source_url = str(body.get("url") or "")[:500]
    if not source_url and linked_urls:
        source_url = linked_urls[0]
    sharer_name = auth.display_name(user) if user else ""
    # 站外分享自动识别链接来源；站内分享不记录来源
    if share_type == "external":
        source_label = detect_source_from_url(source_url) if source_url else SOURCE_LABEL_EXTERNAL
    else:
        source_label = SOURCE_LABEL_SITE
        source_url = ""
    added: list[str] = []
    rejected: list[str] = []
    expired: list[str] = []
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    for code, res in results.items():
        if not res.get("verified"):
            rejected.append(code)
            continue
        if res.get("expired"):
            expired.append(code)
            continue
        _submitted[code] = {
            "share_type": share_type,
            "source_label": source_label,
            "source_url": source_url,
            "author": sharer_name or source,
            "title": f"{sharer_name or source}分享" if share_type == "external" else sharer_name or source,
            "posted_at": now,
            "expire_at": int(res.get("expire_at") or 0),
            "user_id": user["id"] if user else 0,
        }
        added.append(code)
    if added:
        _save_submitted()
        # 记录到登录用户名下（匿名提交则跳过，不影响主流程）
        if is_logged_in:
            for code in added:
                auth.record_submission_for_user(user, code, source)
        # 立即重算一次结果，让新码马上出现在列表
        threading.Thread(target=refresh, daemon=True).start()

    return {
        "ok": True,
        "received": len(candidates),
        "added": added,
        "expired": expired,
        "rejected": rejected,
        "loggedIn": is_logged_in,
        "shareType": share_type,
        "message": (
            f"{'站内共享' if share_type == 'site' else '站外分享'}："
            f"官方校验通过 {len(added)} 个，已收录；"
            f"已过期 {len(expired)} 个，未通过 {len(rejected)} 个"
        ),
    }


# ---------------------------------------------------------------------------
# MCP 服务（Model Context Protocol）
# ---------------------------------------------------------------------------

def _mcp_token_from(request: Request) -> str:
    """MCP 访问令牌解析。

    优先级：``X-MCP-Token`` 头 > ``Authorization: Bearer`` > ``?access_token=``。
    之所以把自定义头放在最前：部分发布平台网关会强制改写 ``Authorization`` 头，
    自定义头不会被篡改，MCP 客户端在配置文件里加一行 header 即可。
    """
    custom = request.headers.get("X-MCP-Token", "").strip()
    if custom:
        return custom
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    q = request.query_params.get("access_token", "") or request.query_params.get("token", "")
    return q.strip()


def _mcp_unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32001, "message": message, "type": "Unauthorized"},
        },
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="xiaodou-codes"'},
    )


def _mcp_context(request: Request) -> tuple[dict | None, Response | None]:
    """校验 MCP 令牌，返回 (上下文, 错误响应)。"""
    raw = _mcp_token_from(request)
    if not raw:
        return None, _mcp_unauthorized("缺少访问令牌：请在客户端配置 X-MCP-Token 或 Authorization: Bearer")
    rec = mcp_store.resolve_token(raw)
    if not rec:
        return None, _mcp_unauthorized("访问令牌无效、已过期或已被撤销")
    user = auth.user_by_id(rec.get("user_id", 0))
    if not user:
        return None, _mcp_unauthorized("令牌对应的账号不存在")
    status = user.get("status") or "active"
    if status not in ("active", ""):
        return None, _mcp_unauthorized("账号已被封禁或注销，MCP 连接已断开")
    mcp_store.touch_token(rec["id"])
    if rec.get("clientId"):
        mcp_store.mark_client_used(rec["clientId"])
    ctx = {
        "user": user,
        "token": rec,
        "scopes": rec.get("scopes") or [],
    }
    return ctx, None


@app.post("/mcp")
async def api_mcp(request: Request) -> Response:
    """MCP 主端点（Streamable HTTP 传输，JSON-RPC 2.0）。"""
    ctx, err = _mcp_context(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            mcp_server._err(None, mcp_server.PARSE_ERROR, "请求体不是合法 JSON"), status_code=400
        )

    accept = (request.headers.get("Accept", "") or "").lower()
    want_sse = "text/event-stream" in accept and "application/json" not in accept

    if isinstance(body, list):
        messages = mcp_server.handle_batch(body, ctx)
        if not messages:
            return Response(status_code=202)
    elif isinstance(body, dict):
        res = mcp_server.handle_request(body, ctx)
        if res is None:  # 通知类请求，无需响应体
            return Response(status_code=202)
        messages = [res]
    else:
        return JSONResponse(
            mcp_server._err(None, mcp_server.INVALID_REQUEST, "无效的 JSON-RPC 请求"), status_code=400
        )

    if want_sse:
        frames = "".join(mcp_server.sse_body(m) for m in messages)
        return Response(
            content=frames,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
    payload = messages if isinstance(body, list) else messages[0]
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/mcp")
def api_mcp_get() -> Response:
    """本服务不提供服务端主动推送，返回 405（符合 MCP 传输规范允许的行为）。"""
    return JSONResponse(
        {"ok": False, "error": "本 MCP 服务为无状态实现，不支持 GET 推送通道，请使用 POST。"},
        status_code=405,
        headers={"Allow": "POST, DELETE"},
    )


@app.delete("/mcp")
def api_mcp_delete() -> Response:
    """无状态服务：结束会话等价于返回 200。"""
    return JSONResponse({"ok": True})


# ---- 浏览器远端授权（OAuth 2.1 风格 + PKCE）----

def _safe_redirect_uri(client: dict, uri: str) -> bool:
    """校验回调地址，防开放重定向。"""
    uri = (uri or "").strip()
    if not uri:
        return False
    allowed = client.get("redirectUris") or []
    if allowed and uri not in allowed:
        return False
    low = uri.lower()
    if low.startswith(("javascript:", "data:", "vbscript:", "file:")):
        return False
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):", uri)
    if not m:
        return False
    scheme = m.group(1).lower()
    if scheme in ("http", "https"):
        if scheme == "http":
            host = re.sub(r"^https?://", "", uri).split("/")[0].split(":")[0].lower()
            if host not in ("localhost", "127.0.0.1", "::1") and not host.endswith(".local"):
                return False
        return True
    # 自定义 scheme（vscode://、cursor:// 等客户端回调）
    return True


def _mcp_authorize_params(request: Request) -> dict:
    q = request.query_params
    return {
        "client_id": (q.get("client_id") or "").strip()[:120],
        "redirect_uri": (q.get("redirect_uri") or "").strip()[:500],
        "scope": (q.get("scope") or "").strip()[:400],
        "state": (q.get("state") or "").strip()[:300],
        "code_challenge": (q.get("code_challenge") or "").strip()[:200],
        "code_challenge_method": (q.get("code_challenge_method") or "plain").strip()[:20],
        "client_name": (q.get("client_name") or q.get("client_id") or "").strip()[:60],
    }


@app.get("/mcp/authorize")
async def api_mcp_authorize(request: Request) -> Response:
    """浏览器授权确认页：AI 客户端把用户引导到这里，用户点「授权」后回调。"""
    params = _mcp_authorize_params(request)
    base = str(request.base_url).rstrip("/")
    next_url = str(request.url)

    client = mcp_store.get_client(params["client_id"]) if params["client_id"] else None
    if not client:
        # 首次出现的 client_id 自动登记；授权页是 GET 请求，
        # 这里不能改写已登记客户端的回调地址（防回调劫持）。
        client = mcp_store.register_client(
            params["client_id"] or ("client-" + os.urandom(6).hex()),
            params["client_name"] or params["client_id"] or "未命名客户端",
            params["redirect_uri"],
            update=False,
        )
    if not params["scope"]:
        params["scope"] = " ".join(mcp_store.DEFAULT_SCOPES)
    if not params["code_challenge_method"]:
        params["code_challenge_method"] = "plain"

    error = ""
    if not _safe_redirect_uri(client, params["redirect_uri"]):
        error = "回调地址不合法或与客户端登记的地址不匹配，已阻止授权。"

    user = auth.user_by_token(_token_from(request))
    logged_in = bool(user and (user.get("status") in ("active", "")))
    account_label = ""
    if logged_in:
        account_label = auth.mask_email(user.get("email", "")) or user.get("email", "")

    html_page = mcp_views.consent_page(
        client_name=client.get("name") or "未命名客户端",
        client_id=params["client_id"],
        redirect_uri=params["redirect_uri"],
        scope=params["scope"],
        state=params["state"],
        code_challenge=params["code_challenge"],
        code_challenge_method=params["code_challenge_method"],
        login_url=mcp_views.build_login_url(base, next_url),
        logged_in=logged_in,
        account_label=account_label,
        error=error,
    )
    return Response(
        content=html_page,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                 "X-Frame-Options": "DENY"},
    )


def _authorize_result(params: dict, user: dict, decision: str) -> dict:
    """处理授权决定，返回 {ok, redirect_url, error}。"""
    client = mcp_store.get_client(params["client_id"]) or {}
    if not _safe_redirect_uri(client, params["redirect_uri"]):
        return {"ok": False, "error": "回调地址不合法"}
    if decision != "approve":
        target = params["redirect_uri"]
        joiner = "&" if "?" in target else "?"
        url = target + joiner + "error=access_denied"
        if params.get("state"):
            url += "&state=" + urllib.parse.quote(params["state"], safe="")
        return {"ok": True, "redirect_url": url, "denied": True}
    code = mcp_store.create_auth_code(
        user_id=user["id"],
        client_id=params["client_id"],
        redirect_uri=params["redirect_uri"],
        scope=params["scope"],
        state=params["state"],
        code_challenge=params["code_challenge"],
        code_challenge_method=params["code_challenge_method"],
    )
    mcp_store.mark_client_approved(params["client_id"], user["id"])
    target = params["redirect_uri"]
    joiner = "&" if "?" in target else "?"
    url = target + joiner + "code=" + urllib.parse.quote(code, safe="")
    if params.get("state"):
        url += "&state=" + urllib.parse.quote(params["state"], safe="")
    return {"ok": True, "redirect_url": url}


@app.post("/mcp/authorize")
async def api_mcp_authorize_submit(request: Request) -> Response:
    """授权页表单提交（授权 / 拒绝），成功后 302 回客户端回调地址。"""
    try:
        form = await request.form()
        data = {k: str(v) for k, v in form.items()}
    except Exception:
        data = {}
    params = {
        "client_id": data.get("client_id", "").strip()[:120],
        "redirect_uri": data.get("redirect_uri", "").strip()[:500],
        "scope": data.get("scope", "").strip()[:400],
        "state": data.get("state", "").strip()[:300],
        "code_challenge": data.get("code_challenge", "").strip()[:200],
        "code_challenge_method": (data.get("code_challenge_method") or "plain").strip()[:20],
    }
    decision = data.get("decision", "approve")

    user = auth.user_by_token(_token_from(request))
    if not user or (user.get("status") not in ("active", "")):
        return JSONResponse(
            {"ok": False, "error": "登录态已失效，请重新登录后再授权"}, status_code=401
        )
    result = _authorize_result(params, user, decision)
    if not result.get("ok"):
        return JSONResponse({"ok": False, "error": result.get("error")}, status_code=400)
    if request.headers.get("X-Requested-With") == "fetch" or "application/json" in (
        request.headers.get("Content-Type") or ""
    ):
        return JSONResponse({"ok": True, "redirect": result["redirect_url"]})
    return RedirectResponse(result["redirect_url"], status_code=302)


@app.post("/mcp/authorize/approve")
async def api_mcp_authorize_approve(request: Request) -> Response:
    """站内按钮用的 JSON 接口：直接换取回调地址（等价于点「授权」）。"""
    token = _token_from(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    user = auth.user_by_token(token)
    if not user or (user.get("status") not in ("active", "")):
        return JSONResponse({"ok": False, "error": "登录态已失效，请重新登录"}, status_code=401)
    params = {
        "client_id": str(body.get("client_id") or "").strip()[:120],
        "redirect_uri": str(body.get("redirect_uri") or "").strip()[:500],
        "scope": str(body.get("scope") or " ".join(mcp_store.DEFAULT_SCOPES)).strip()[:400],
        "state": str(body.get("state") or "").strip()[:300],
        "code_challenge": str(body.get("code_challenge") or "").strip()[:200],
        "code_challenge_method": str(body.get("code_challenge_method") or "plain").strip()[:20],
    }
    if not params["client_id"]:
        params["client_id"] = "web-" + os.urandom(6).hex()
        mcp_store.register_client(params["client_id"], str(body.get("client_name") or "网页授权"),
                                  params["redirect_uri"])
    result = _authorize_result(params, user, "approve")
    if not result.get("ok"):
        return JSONResponse({"ok": False, "error": result.get("error")}, status_code=400)
    return JSONResponse({"ok": True, "redirect": result["redirect_url"], "state": params["state"]})


@app.post("/mcp/token")
async def api_mcp_token(request: Request) -> Response:
    """令牌端点：授权码 + PKCE 换长期访问令牌。"""
    ctype = (request.headers.get("Content-Type") or "").lower()
    data: dict = {}
    if "application/json" in ctype:
        try:
            raw = await request.json()
            data = raw if isinstance(raw, dict) else {}
        except Exception:
            data = {}
    else:
        try:
            form = await request.form()
            data = {k: str(v) for k, v in form.items()}
        except Exception:
            data = {}

    grant = str(data.get("grant_type") or "").strip()
    if grant != "authorization_code":
        return JSONResponse(
            {"error": "unsupported_grant_type", "error_description": "仅支持 authorization_code"},
            status_code=400,
        )

    code = str(data.get("code") or "").strip()
    client_id = str(data.get("client_id") or "").strip()
    redirect_uri = str(data.get("redirect_uri") or "").strip()
    verifier = str(data.get("code_verifier") or "").strip()

    result = mcp_store.consume_auth_code(code, client_id, redirect_uri, verifier)
    if not result.get("ok"):
        return JSONResponse(
            {"error": "invalid_grant", "error_description": result.get("error", "授权码无效")},
            status_code=400,
        )

    client = mcp_store.get_client(result.get("client_id") or "") or {}
    scopes = mcp_store.normalize_scopes((result.get("scope") or "").split())
    created = mcp_store.create_token(
        user_id=result["user_id"],
        name=client.get("name") or "MCP 客户端",
        scopes=scopes,
        ttl_days=mcp_store.DEFAULT_TTL_DAYS,
        client_id=result.get("client_id") or "",
    )
    return JSONResponse({
        "access_token": created["token"],
        "token_type": "Bearer",
        "expires_in": max(0, created["expiresAt"] - int(time.time())),
        "scope": " ".join(created["scopes"]),
        "client_id": result.get("client_id") or "",
    })


@app.post("/mcp/register")
async def api_mcp_register(request: Request) -> Response:
    """动态客户端注册（RFC 7591 风格），便于 MCP 客户端自动登记。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    client_id = str(body.get("client_id") or "").strip() or ("mcp-" + os.urandom(8).hex())
    name = str(body.get("client_name") or body.get("name") or client_id)[:60]
    uris = body.get("redirect_uris") or []
    if isinstance(uris, str):
        uris = [u.strip() for u in uris.split(",") if u.strip()]
    client = mcp_store.register_client(client_id, name, uris, update=True)
    return JSONResponse({
        "client_id": client["clientId"],
        "client_id_issued_at": int(time.time()),
        "client_name": client["name"],
        "redirect_uris": client["redirectUris"],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": " ".join(mcp_store.DEFAULT_SCOPES),
    })


def _mcp_metadata(request: Request) -> dict:
    base = str(request.base_url).rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": base + "/mcp/authorize",
        "token_endpoint": base + "/mcp/token",
        "registration_endpoint": base + "/mcp/register",
        "scopes_supported": mcp_store.ALL_SCOPES,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


@app.get("/.well-known/oauth-authorization-server")
def api_oauth_metadata(request: Request) -> Response:
    """OAuth 授权服务器元数据：MCP 客户端据此自动发现授权流程。"""
    return JSONResponse(_mcp_metadata(request))


@app.get("/.well-known/oauth-protected-resource")
def api_oauth_resource(request: Request) -> Response:
    """受保护资源元数据。"""
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "resource": base + "/mcp",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": mcp_store.ALL_SCOPES,
    })


@app.get("/.well-known/oauth-protected-resource/mcp")
def api_oauth_resource_mcp(request: Request) -> Response:
    return api_oauth_resource(request)


# ---- 「我的账号」页使用的用户级 MCP 接口 ----

def _mcp_base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@app.get("/api/mcp/config")
def api_mcp_config(request: Request) -> Response:
    """MCP 连接信息：连接地址、令牌列表、已授权客户端与可用工具。"""
    token = _token_from(request)
    user = auth.user_by_token(token)
    if not user:
        return JSONResponse({"ok": False, "error": "请先登录"}, status_code=401)
    base = _mcp_base_url(request)
    return JSONResponse({
        "ok": True,
        "endpoint": base + "/mcp",
        "authorizeUrl": base + "/mcp/authorize",
        "tokenUrl": base + "/mcp/token",
        "registerUrl": base + "/mcp/register",
        "scopes": [{"name": k, "desc": v} for k, v in mcp_store.SCOPES.items()],
        "defaultScopes": mcp_store.DEFAULT_SCOPES,
        "tokens": mcp_store.list_tokens(user["id"]),
        "clients": mcp_store.list_clients(user["id"]),
        "tools": [
            {"name": t["name"], "title": t.get("title", ""), "desc": t["description"]}
            for t in mcp_server.TOOLS
        ],
        "serverInfo": mcp_server.SERVER_INFO,
    })


@app.post("/api/mcp/tokens")
async def api_mcp_create_token(request: Request) -> Response:
    """新建连接令牌（明文仅此一次返回）。"""
    token = _token_from(request)
    user = auth.user_by_token(token)
    if not user:
        return JSONResponse({"ok": False, "error": "请先登录"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    created = mcp_store.create_token(
        user_id=user["id"],
        name=str(body.get("name") or "AI 客户端")[:40],
        scopes=body.get("scopes"),
        ttl_days=body.get("ttlDays") or mcp_store.DEFAULT_TTL_DAYS,
    )
    return JSONResponse({
        "ok": True,
        "id": created["id"],
        "token": created["token"],
        "scopes": created["scopes"],
        "expiresAt": created["expiresAt"],
        "message": "连接令牌已生成，请立即复制保存（服务端只保存摘要）。",
    })


@app.delete("/api/mcp/tokens/{token_id}")
def api_mcp_revoke_token(token_id: int, request: Request) -> Response:
    """撤销连接令牌。"""
    token = _token_from(request)
    user = auth.user_by_token(token)
    if not user:
        return JSONResponse({"ok": False, "error": "请先登录"}, status_code=401)
    ok = mcp_store.revoke_token(user["id"], token_id)
    return JSONResponse({"ok": ok, "message": "已撤销" if ok else "未找到该令牌"})


@app.post("/api/mcp/tokens/{token_id}/rotate")
async def api_mcp_rotate_token(token_id: int, request: Request) -> Response:
    """重新生成令牌：撤销旧的并签发新的。"""
    token = _token_from(request)
    user = auth.user_by_token(token)
    if not user:
        return JSONResponse({"ok": False, "error": "请先登录"}, status_code=401)
    old = None
    for t in mcp_store.list_tokens(user["id"]):
        if t["id"] == token_id:
            old = t
            break
    if not old:
        return JSONResponse({"ok": False, "error": "未找到该令牌"}, status_code=404)
    mcp_store.revoke_token(user["id"], token_id)
    created = mcp_store.create_token(
        user_id=user["id"], name=old["name"], scopes=old["scopes"], ttl_days=mcp_store.DEFAULT_TTL_DAYS
    )
    return JSONResponse({
        "ok": True,
        "id": created["id"],
        "token": created["token"],
        "scopes": created["scopes"],
        "expiresAt": created["expiresAt"],
        "message": "已重新生成，旧令牌已失效。",
    })


@app.get("/api/mcp/clients")
def api_mcp_clients(request: Request) -> Response:
    """已授权的 MCP 客户端列表。"""
    token = _token_from(request)
    user = auth.user_by_token(token)
    if not user:
        return JSONResponse({"ok": False, "error": "请先登录"}, status_code=401)
    return JSONResponse({"ok": True, "clients": mcp_store.list_clients(user["id"])})


@app.delete("/api/mcp/clients/{client_id}")
def api_mcp_revoke_client(client_id: str, request: Request) -> Response:
    """解除某个客户端的授权（同时撤销其全部令牌）。"""
    token = _token_from(request)
    user = auth.user_by_token(token)
    if not user:
        return JSONResponse({"ok": False, "error": "请先登录"}, status_code=401)
    n = mcp_store.revoke_client(user["id"], client_id)
    return JSONResponse({"ok": True, "revoked": n, "message": f"已解除授权，撤销 {n} 个令牌"})


@app.post("/api/mcp/revoke-all")
def api_mcp_revoke_all(request: Request) -> Response:
    """一键断开所有 AI 客户端。"""
    token = _token_from(request)
    user = auth.user_by_token(token)
    if not user:
        return JSONResponse({"ok": False, "error": "请先登录"}, status_code=401)
    n = mcp_store.revoke_all_tokens(user["id"])
    return JSONResponse({"ok": True, "revoked": n, "message": f"已断开全部连接（{n} 个令牌）"})


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
