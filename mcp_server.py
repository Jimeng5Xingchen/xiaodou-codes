"""小豆邀请码 MCP 服务端。

实现 Model Context Protocol（Streamable HTTP 传输，JSON-RPC 2.0）。
零第三方依赖：直接用 FastAPI / Starlette 的 Response 手工实现协议帧，
方便部署到没有额外依赖的轻量平台。

协议要点
--------
- ``POST /mcp``            客户端主入口，Content-Type: application/json，
                           响应按 Accept 头返回 JSON 或 SSE（text/event-stream）。
- ``GET /mcp``             服务端→客户端推送通道；本服务无主动推送，返回 405。
- ``DELETE /mcp``          显式结束会话；本服务为无状态，返回 200。
- 鉴权：``X-MCP-Token`` 头 > ``Authorization: Bearer`` > ``?access_token=``。

对外暴露的工具（tools）
----------------------
读取类：``search_codes`` / ``get_code_detail`` / ``get_site_stats``
        / ``list_community_links`` / ``get_my_profile`` / ``list_my_shares``
写入类：``submit_code``（发布邀请码） / ``submit_link``（发布邀请码链接）
        / ``refresh_codes``（触发采集，仅管理员）
管理类：``list_access_tokens`` / ``create_access_token`` / ``revoke_access_token``
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import mcp_store

PROTOCOL_LATEST = "2025-06-18"
PROTOCOL_SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {
    "name": "xiaodou-codes-mcp",
    "title": "小豆邀请码 MCP 服务",
    "version": "1.0.0",
}

# JSON-RPC 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _app():
    """延迟获取 app 模块。

    注意：以 ``python app.py`` 方式启动时，运行中的模块是 ``__main__``，
    此时 ``import app`` 会加载**第二份**模块副本（``_state`` 等全局状态不共享），
    因此优先复用 ``__main__``；用 ``uvicorn app:app`` 启动时则回退到正常导入。
    """
    import sys

    main = sys.modules.get("__main__")
    if main is not None and hasattr(main, "process_submit"):
        return main
    import app as app_module

    return app_module


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def _fmt_cst(ts: Any) -> str:
    """把 Unix 时间戳格式化成北京时间字符串。"""
    try:
        n = int(ts or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    from datetime import datetime, timezone, timedelta

    return datetime.fromtimestamp(n, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _brief(rec: dict, full: bool = False) -> dict[str, Any]:
    """把一条邀请码记录整理成给 AI 看的结构。"""
    out = {
        "code": rec.get("code", ""),
        "status": rec.get("status", "unknown"),
        "statusNote": rec.get("status_note", ""),
        "category": rec.get("category", "auto"),
        "source": rec.get("source_label") or rec.get("source", ""),
        "sourceUrl": rec.get("source_url", ""),
        "author": rec.get("author", ""),
        "postedAt": rec.get("posted_at", ""),
        "replyCount": int(rec.get("reply_count") or 0),
        "seenCount": int(rec.get("seen_count") or 1),
        "verified": bool(rec.get("verified")),
        "expireAt": int(rec.get("expire_at") or 0),
    }
    sharer = rec.get("sharer") or {}
    if sharer:
        out["sharer"] = {
            "name": sharer.get("name", ""),
            "emailMasked": sharer.get("emailMasked", ""),
            "bio": sharer.get("bio", ""),
        }
    if full:
        hits = rec.get("hits") or []
        out["hits"] = [
            {
                "source": h.get("label") or h.get("source", ""),
                "url": h.get("url", ""),
                "author": h.get("author", ""),
                "title": h.get("title", ""),
                "postedAt": h.get("posted_at", ""),
                "replyCount": int(h.get("reply_count") or 0),
                "comments": [
                    {
                        "author": c.get("author", ""),
                        "text": c.get("text", ""),
                        "postedAt": c.get("posted_at", ""),
                    }
                    for c in (h.get("comments") or [])
                ][:20],
            }
            for h in hits[:10]
        ]
    return out


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

def tool_search_codes(args: dict, ctx: dict) -> dict[str, Any]:
    """搜索邀请码：支持关键字、状态、分类过滤与可用优先排序。"""
    app = _app()
    with app._lock:
        payload = dict(app._state)
    all_codes = payload.get("codes") or []

    keyword = str(args.get("keyword") or "").strip().lower()
    status = str(args.get("status") or "all").strip().lower()
    category = str(args.get("category") or "all").strip().lower()
    only_available = bool(args.get("onlyAvailable"))
    limit = _clamp(args.get("limit"), 1, 100, 20)

    def _cat(rec: dict) -> str:
        return rec.get("category") or ("site" if rec.get("sharer") else "auto")

    picked: list[dict] = []
    for rec in all_codes:
        if keyword:
            blob = " ".join(
                str(rec.get(k) or "")
                for k in ("code", "author", "title", "source_label", "status_note")
            ).lower()
            if keyword not in blob:
                continue
        if status and status != "all" and rec.get("status") != status:
            continue
        if category and category != "all" and _cat(rec) != category:
            continue
        if only_available and rec.get("status") != "available":
            continue
        picked.append(rec)

    order = {"available": 0, "unknown": 1, "used": 2, "expired": 3}
    # 先按时间倒序，再按状态稳定排序 —— 稳定排序保证同状态内仍是时间倒序
    picked.sort(key=lambda r: str(r.get("posted_at_utc") or r.get("posted_at") or ""), reverse=True)
    picked.sort(key=lambda r: order.get(r.get("status", "unknown"), 5))

    results = [_brief(r) for r in picked[:limit]]
    lines = []
    for r in results:
        flag = {"available": "✅有效", "unknown": "❓待确认", "used": "⚠️已用", "expired": "⌛已过期"}.get(
            r["status"], r["status"]
        )
        src = r.get("source") or "社区"
        extra = f"（{src}）" if src else ""
        lines.append(f"- {r['code']} {flag}{extra}" + (f" {r['sourceUrl']}" if r.get("sourceUrl") else ""))
    text = (
        f"找到 {len(picked)} 条邀请码（返回前 {len(results)} 条）：\n" + "\n".join(lines)
        if results
        else "没有匹配的邀请码。试试放宽过滤条件，或用 submit_link 让系统去社区链接里抓。"
    )
    return {
        "text": text,
        "data": {
            "total": len(picked),
            "lastUpdated": payload.get("lastUpdated", ""),
            "codes": results,
        },
    }


def tool_get_code_detail(args: dict, ctx: dict) -> dict[str, Any]:
    """查看单条邀请码详情，含多源命中与回复评论。"""
    app = _app()
    code = str(args.get("code") or "").strip().upper()
    if not code:
        return {"text": "请提供要查询的邀请码。", "isError": True}
    with app._lock:
        payload = dict(app._state)
    for rec in payload.get("codes") or []:
        if str(rec.get("code", "")).upper() == code:
            detail = _brief(rec, full=True)
            comments = []
            for h in detail.get("hits", []):
                for c in h.get("comments", []):
                    comments.append(f"[{h['source']}] {c['author']}：{c['text']}")
            lines = [
                f"邀请码：{detail['code']}",
                f"状态：{detail['statusNote'] or detail['status']}",
                f"来源：{detail['source']} {detail['sourceUrl']}",
                f"分享者：{detail.get('sharer', {}).get('name', '') or detail['author'] or '未署名'}",
                f"时间：{detail['postedAt']}",
            ]
            if comments:
                lines.append("回复评论：")
                lines.extend("  " + c for c in comments[:20])
            return {"text": "\n".join(lines), "data": detail}
    return {"text": f"未找到邀请码 {code}。", "isError": True}


def tool_submit_code(args: dict, ctx: dict) -> dict[str, Any]:
    """发布邀请码（站内共享 / 站外分享），经官方接口校验后收录。"""
    app = _app()
    user = ctx.get("user") or {}
    raw = args.get("codes")
    if isinstance(raw, str):
        codes = [raw]
    elif isinstance(raw, (list, tuple)):
        codes = [str(c) for c in raw]
    else:
        codes = []
    codes = [c for c in codes if str(c).strip()]
    if not codes:
        return {"text": "请提供要发布的邀请码（codes 字段）。", "isError": True}

    share_type = str(args.get("shareType") or args.get("share_type") or "site").strip().lower()
    if share_type not in ("site", "external"):
        share_type = "site"
    body = {
        "codes": codes,
        "share_type": share_type,
        "source": str(args.get("source") or "MCP 分享")[:40],
        "url": str(args.get("url") or "")[:500],
    }
    result = app.process_submit(body, user)
    added = result.get("added") or []
    text = (
        f"{'站内共享' if share_type == 'site' else '站外分享'}完成："
        f"收录 {len(added)} 个，已过期 {len(result.get('expired') or [])} 个，"
        f"未通过校验 {len(result.get('rejected') or [])} 个。"
    )
    if added:
        text += "\n" + "\n".join(f"- {c}" for c in added)
    else:
        text += "\n" + (result.get("message") or "没有码通过官方校验。")
    return {
        "text": text,
        "data": {
            "added": added,
            "expired": result.get("expired") or [],
            "rejected": result.get("rejected") or [],
            "shareType": share_type,
        },
        "isError": not bool(result.get("ok")),
    }


def tool_submit_link(args: dict, ctx: dict) -> dict[str, Any]:
    """发布邀请码链接：系统抓取该页面文字，自动识别其中的邀请码并校验收录。"""
    url = str(args.get("url") or "").strip()
    if not url:
        return {"text": "请提供要分享的链接（url 字段）。", "isError": True}
    if not url.startswith(("http://", "https://")):
        return {"text": "链接必须是 http(s) 地址。", "isError": True}
    app = _app()
    body = {
        "codes": [url],
        "share_type": "external",
        "source": str(args.get("source") or app.detect_source_from_url(url))[:40],
        "url": url,
    }
    result = app.process_submit(body, ctx.get("user") or {})
    added = result.get("added") or []
    text = (
        f"链接解析完成：收录 {len(added)} 个邀请码"
        + (f"（{', '.join(added)}）" if added else "")
        + f"；未通过校验 {len(result.get('rejected') or [])} 个。"
    )
    if not added:
        text += " 页面里可能没有邀请码，或该码已失效。"
    return {
        "text": text,
        "data": {
            "url": url,
            "added": added,
            "rejected": result.get("rejected") or [],
            "expired": result.get("expired") or [],
        },
        "isError": not bool(result.get("ok")),
    }


def tool_list_my_shares(args: dict, ctx: dict) -> dict[str, Any]:
    """我分享的邀请码列表。"""
    import auth

    app = _app()
    user = ctx.get("user") or {}
    limit = _clamp(args.get("limit"), 1, 200, 50)
    with app._lock:
        payload = dict(app._state)
    live = {str(c.get("code", "")).upper(): c for c in (payload.get("codes") or [])}

    items = []
    for sub in auth.list_submissions(user.get("id", 0), limit * 3):
        code = str(sub.get("code") or "").upper()
        rec = live.get(code)
        if rec:
            item = _brief(rec)
        else:
            # 已提交但尚未进入采集结果（后台采集还在跑）
            item = {
                "code": code,
                "status": "unknown",
                "statusNote": "已提交，正在校验收录",
                "category": "site",
                "source": sub.get("source") or "",
                "sourceUrl": "",
                "author": user.get("username") or "",
                "postedAt": _fmt_cst(sub.get("createdAt")),
                "replyCount": 0,
                "seenCount": 1,
                "verified": False,
                "expireAt": 0,
            }
        items.append(item)
    items = items[:limit]
    lines = [
        f"- {r['code']} [{r['status']}] {r.get('source') or ''} {r.get('postedAt') or ''}"
        for r in items
    ]
    return {
        "text": (
            f"你共分享 {len(items)} 个邀请码：\n" + "\n".join(lines)
            if items
            else "你还没有分享过邀请码。可以用 submit_code / submit_link 发布。"
        ),
        "data": {"total": len(items), "codes": items},
    }


def tool_get_site_stats(args: dict, ctx: dict) -> dict[str, Any]:
    """站点整体统计与最近更新时间。"""
    app = _app()
    with app._lock:
        payload = dict(app._state)
    counts = payload.get("counts") or {}
    return {
        "text": (
            f"最近更新：{payload.get('lastUpdated', '未知')}\n"
            f"总收录 {counts.get('total', 0)} 条，官方校验有效 {counts.get('available', 0)} 条，"
            f"待确认 {counts.get('unknown', 0)} 条，社区反馈已用 {counts.get('used', 0)} 条，"
            f"已过期 {counts.get('expired', 0)} 条。"
        ),
        "data": {
            "lastUpdated": payload.get("lastUpdated", ""),
            "counts": counts,
            "errors": payload.get("errors") or [],
        },
    }


def tool_get_my_profile(args: dict, ctx: dict) -> dict[str, Any]:
    """读取当前授权账号的资料。"""
    user = ctx.get("user") or {}
    token = ctx.get("token") or {}
    return {
        "text": (
            f"账号：{user.get('email', '')}\n"
            f"用户名：{user.get('username') or '（未设置）'}\n"
            f"简介：{user.get('bio') or '（未设置）'}\n"
            f"角色：{user.get('role', 'user')}\n"
            f"令牌权限：{' '.join(token.get('scopes') or [])}"
        ),
        "data": {
            "id": user.get("id"),
            "email": user.get("email"),
            "username": user.get("username", ""),
            "bio": user.get("bio", ""),
            "role": user.get("role", "user"),
            "groupId": user.get("group_id", 0),
            "tokenScopes": token.get("scopes") or [],
            "tokenExpiresAt": token.get("expiresAt", 0),
        },
    }


def tool_list_community_links(args: dict, ctx: dict) -> dict[str, Any]:
    """社区直达搜索链接（微博 / 知乎 / 贴吧 / 豆瓣 / 小红书 等）。"""
    import collector

    q = str(args.get("keyword") or "小豆邀请码")
    links = [
        {"name": name, "url": template.format(q=q), "key": key}
        for key, name, template, _ in collector.COMMUNITY_SEARCHES
    ]
    return {
        "text": "社区直达入口：\n" + "\n".join(f"- {l['name']}：{l['url']}" for l in links),
        "data": {"keyword": q, "links": links},
    }


def tool_refresh_codes(args: dict, ctx: dict) -> dict[str, Any]:
    """触发一次全源采集刷新（仅管理员）。"""
    user = ctx.get("user") or {}
    if user.get("role") != "admin":
        return {"text": "只有管理员可以触发全源采集。", "isError": True}
    app = _app()
    import threading

    threading.Thread(target=app.refresh, daemon=True).start()
    return {"text": "已触发一次全源采集，稍后可用 search_codes 查看最新结果。"}


def tool_list_access_tokens(args: dict, ctx: dict) -> dict[str, Any]:
    """查询本账号下的 MCP 连接令牌（只显示前缀与元信息，不含明文）。"""
    user = ctx.get("user") or {}
    items = mcp_store.list_tokens(user.get("id", 0))
    lines = [
        f"- #{t['id']} {t['name']}（{t['prefix']}…）权限：{' '.join(t['scopes'])} "
        f"{'已撤销' if t['revokedAt'] else ('有效' if t['active'] else '已过期')}"
        for t in items
    ]
    return {
        "text": (
            f"本账号共有 {len(items)} 个连接令牌：\n" + "\n".join(lines)
            if items
            else "还没有创建过连接令牌。"
        ),
        "data": {"tokens": items},
    }


def tool_create_access_token(args: dict, ctx: dict) -> dict[str, Any]:
    """新建一个 MCP 连接令牌（明文只在这次返回）。"""
    user = ctx.get("user") or {}
    name = str(args.get("name") or "AI 客户端")[:40]
    scopes = args.get("scopes")
    ttl_days = _clamp(args.get("expiresDays"), 1, 365, mcp_store.DEFAULT_TTL_DAYS)
    created = mcp_store.create_token(user.get("id", 0), name=name, scopes=scopes, ttl_days=ttl_days)
    return {
        "text": (
            f"已创建连接令牌：{created['token']}\n"
            f"权限：{' '.join(created['scopes'])}\n"
            f"请立即复制保存，服务端只保存摘要，明文不会再次显示。"
        ),
        "data": {
            "id": created["id"],
            "token": created["token"],
            "scopes": created["scopes"],
            "expiresAt": created["expiresAt"],
        },
    }


def tool_revoke_access_token(args: dict, ctx: dict) -> dict[str, Any]:
    """撤销一个连接令牌。"""
    user = ctx.get("user") or {}
    token_id = _clamp(args.get("tokenId"), 1, 2**31, 0)
    if not token_id:
        return {"text": "请提供要撤销的令牌 id。", "isError": True}
    ok = mcp_store.revoke_token(user.get("id", 0), token_id)
    return {
        "text": "连接令牌已撤销。" if ok else "未找到该令牌（可能不属于当前账号或已撤销）。",
        "data": {"revoked": ok, "tokenId": token_id},
        "isError": not ok,
    }


# ---------------------------------------------------------------------------
# 工具清单
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_codes",
        "title": "搜索邀请码",
        "description": (
            "在本站聚合的邀请码里搜索。可按关键字、状态（available/unknown/used/expired）、"
            "分类（auto/site/external）过滤，默认官方校验有效的排在最前。"
            "适合回答「现在有哪些可用邀请码」「小豆邀请码是什么」这类问题。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "关键字，匹配邀请码 / 作者 / 来源 / 备注"},
                "status": {
                    "type": "string",
                    "enum": ["all", "available", "unknown", "used", "expired"],
                    "description": "按状态过滤，默认 all",
                },
                "category": {
                    "type": "string",
                    "enum": ["all", "auto", "site", "external"],
                    "description": "分类：auto=自动采集，site=站内共享，external=站外分享",
                },
                "onlyAvailable": {"type": "boolean", "description": "只看官方校验有效的码"},
                "limit": {"type": "integer", "description": "返回条数，1-100，默认 20"},
            },
        },
        "scopes": ["codes:read"],
        "handler": tool_search_codes,
    },
    {
        "name": "get_code_detail",
        "title": "查看邀请码详情",
        "description": "查看单条邀请码的详情：状态、来源链接、分享者，以及多源命中时的各来源回复评论。",
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "邀请码，如 LK2B9N"}},
            "required": ["code"],
        },
        "scopes": ["codes:read"],
        "handler": tool_get_code_detail,
    },
    {
        "name": "submit_code",
        "title": "发布邀请码",
        "description": (
            "发布你找到的邀请码到本站。所有码都会先经官方接口校验，"
            "只有校验通过且未过期的码才会收录。shareType=site 为站内共享（需登录），"
            "external 为站外分享（可带原文链接）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "邀请码列表，可传单个字符串",
                },
                "shareType": {
                    "type": "string",
                    "enum": ["site", "external"],
                    "description": "site=站内共享，external=站外分享，默认 site",
                },
                "source": {"type": "string", "description": "来源标注，如「微博」「豆瓣小组」"},
                "url": {"type": "string", "description": "原文链接（站外分享时使用）"},
            },
            "required": ["codes"],
        },
        "scopes": ["codes:write"],
        "handler": tool_submit_code,
    },
    {
        "name": "submit_link",
        "title": "发布邀请码链接",
        "description": (
            "把一条社区分享链接交给系统：自动抓取页面文字、识别其中的邀请码、"
            "逐个做官方校验，通过的收录到站内，并记录原文出处。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "分享链接（http/https）"},
                "source": {"type": "string", "description": "来源标注，缺省按链接域名自动识别"},
            },
            "required": ["url"],
        },
        "scopes": ["codes:write"],
        "handler": tool_submit_link,
    },
    {
        "name": "list_my_shares",
        "title": "我分享的邀请码",
        "description": "列出当前授权账号分享过的全部邀请码及其状态。",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "返回条数，默认 50"}},
        },
        "scopes": ["account:read"],
        "handler": tool_list_my_shares,
    },
    {
        "name": "get_my_profile",
        "title": "我的账号资料",
        "description": "读取当前授权账号的用户名、邮箱、简介、角色与令牌权限。",
        "inputSchema": {"type": "object", "properties": {}},
        "scopes": ["account:read"],
        "handler": tool_get_my_profile,
    },
    {
        "name": "get_site_stats",
        "title": "站点统计",
        "description": "本站邀请码收录总量、各状态占比与最近一次采集时间。",
        "inputSchema": {"type": "object", "properties": {}},
        "scopes": ["codes:read"],
        "handler": tool_get_site_stats,
    },
    {
        "name": "list_community_links",
        "title": "社区直达入口",
        "description": "返回微博 / 知乎 / 贴吧 / 豆瓣 / 小红书 等社区的邀请码搜索直达链接。",
        "inputSchema": {
            "type": "object",
            "properties": {"keyword": {"type": "string", "description": "搜索词，默认「小豆邀请码」"}},
        },
        "scopes": ["codes:read"],
        "handler": tool_list_community_links,
    },
    {
        "name": "refresh_codes",
        "title": "触发采集刷新",
        "description": "立即触发一次全源采集（仅管理员生效）。",
        "inputSchema": {"type": "object", "properties": {}},
        "scopes": ["codes:read"],
        "handler": tool_refresh_codes,
    },
    {
        "name": "list_access_tokens",
        "title": "查询连接令牌",
        "description": "列出本账号下的所有 MCP 连接令牌（仅元信息，不含明文）。",
        "inputSchema": {"type": "object", "properties": {}},
        "scopes": ["tokens:manage"],
        "handler": tool_list_access_tokens,
    },
    {
        "name": "create_access_token",
        "title": "新建连接令牌",
        "description": "为 AI 客户端新建一个连接令牌。明文只在本次调用返回，需要立即保存。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "令牌备注，如「Cursor」"},
                "scopes": {
                    "type": "array",
                    "items": {"type": "string", "enum": mcp_store.ALL_SCOPES},
                    "description": "权限范围，缺省为读码 + 发码 + 读资料",
                },
                "expiresDays": {"type": "integer", "description": "有效期（天），1-365，默认 90"},
            },
        },
        "scopes": ["tokens:manage"],
        "handler": tool_create_access_token,
    },
    {
        "name": "revoke_access_token",
        "title": "撤销连接令牌",
        "description": "撤销一个连接令牌，撤销后该 AI 客户端立即失去访问能力。",
        "inputSchema": {
            "type": "object",
            "properties": {"tokenId": {"type": "integer", "description": "令牌 id"}},
            "required": ["tokenId"],
        },
        "scopes": ["tokens:manage"],
        "handler": tool_revoke_access_token,
    },
]

TOOL_MAP: dict[str, dict[str, Any]] = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------------------
# JSON-RPC 处理
# ---------------------------------------------------------------------------

def _err(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _ok(id_: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _negotiate(version: str) -> str:
    v = str(version or "")
    return v if v in PROTOCOL_SUPPORTED else PROTOCOL_LATEST


def handle_request(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any] | None:
    """处理一条 JSON-RPC 请求。通知（无 id）返回 None。"""
    method = str(payload.get("method") or "")
    id_ = payload.get("id")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    if method == "initialize":
        return _ok(
            id_,
            {
                "protocolVersion": _negotiate(params.get("protocolVersion", "")),
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "logging": {},
                },
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "这是小豆邀请码聚合站。用 search_codes 查可用邀请码；"
                    "用 submit_code 发布邀请码、用 submit_link 发布社区分享链接；"
                    "所有码都会经过官方接口校验后才收录。"
                ),
            },
        )

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return _ok(id_, {})

    if method == "tools/list":
        return _ok(
            id_,
            {
                "tools": [
                    {
                        "name": t["name"],
                        "title": t.get("title", ""),
                        "description": t["description"],
                        "inputSchema": t["inputSchema"],
                    }
                    for t in TOOLS
                ]
            },
        )

    if method == "resources/list":
        return _ok(
            id_,
            {
                "resources": [
                    {
                        "uri": "xd://codes/recent",
                        "name": "最近邀请码",
                        "description": "本站最新收录的邀请码列表",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": "xd://me/profile",
                        "name": "当前账号资料",
                        "description": "当前授权账号的资料",
                        "mimeType": "application/json",
                    },
                ]
            },
        )

    if method == "resources/read":
        uri = str(params.get("uri") or "")
        if uri == "xd://me/profile":
            res = tool_get_my_profile({}, ctx)
            return _ok(id_, {"contents": [{"uri": uri, "mimeType": "application/json",
                                          "text": json.dumps(res.get("data", {}), ensure_ascii=False)}]})
        if uri == "xd://codes/recent":
            res = tool_search_codes({"limit": 30}, ctx)
            return _ok(id_, {"contents": [{"uri": uri, "mimeType": "application/json",
                                          "text": json.dumps(res.get("data", {}), ensure_ascii=False)}]})
        return _err(id_, INVALID_PARAMS, f"未知的资源 URI：{uri}")

    if method == "prompts/list":
        return _ok(id_, {"prompts": []})

    if method == "logging/setLevel":
        return _ok(id_, {})

    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        tool = TOOL_MAP.get(name)
        if not tool:
            return _err(id_, METHOD_NOT_FOUND, f"未知工具：{name}")
        scopes = set(ctx.get("scopes") or [])
        need = set(tool.get("scopes") or [])
        if need and not need.issubset(scopes):
            missing = ", ".join(sorted(need - scopes))
            return _ok(
                id_,
                {
                    "content": [{"type": "text", "text": f"当前令牌缺少权限：{missing}。请重新授权并勾选对应权限。"}],
                    "isError": True,
                },
            )
        try:
            res = tool["handler"](args, ctx)
        except Exception as exc:  # noqa: BLE001
            return _ok(
                id_,
                {
                    "content": [{"type": "text", "text": f"工具执行失败：{exc}"}],
                    "isError": True,
                },
            )
        payload_out: dict[str, Any] = {
            "content": [{"type": "text", "text": str(res.get("text", ""))}],
        }
        if res.get("data") is not None:
            payload_out["structuredContent"] = res["data"]
        if res.get("isError"):
            payload_out["isError"] = True
        return _ok(id_, payload_out)

    if id_ is None:
        return None
    return _err(id_, METHOD_NOT_FOUND, f"不支持的方法：{method}")


def handle_batch(payloads: list[Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for item in payloads:
        if not isinstance(item, dict):
            out.append(_err(None, INVALID_REQUEST, "请求必须是 JSON-RPC 对象"))
            continue
        res = handle_request(item, ctx)
        if res is not None:
            out.append(res)
    return out


def sse_body(message: dict[str, Any]) -> str:
    """把一条 JSON-RPC 消息包成 SSE 帧。"""
    return "event: message\ndata: " + json.dumps(message, ensure_ascii=False) + "\n\n"


def server_time() -> int:
    return int(time.time())
