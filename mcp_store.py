"""MCP 授权存储层：连接令牌 / 客户端注册 / 浏览器授权码。

设计要点
--------
1. **只存摘要**：令牌明文仅在创建时返回一次，数据库只保存 SHA-256 摘要；
   即使数据库泄露，也无法直接拿来连接 MCP。
2. **可过期、可撤销**：每个令牌带过期时间与撤销时间戳，用户随时可在
   「我的账号 → AI / MCP 授权」里解除某个 AI 客户端的连接。
3. **OAuth 2.1 风格远端授权**：AI 客户端把用户引导到浏览器授权页，
   用户点「授权」后拿到一次性授权码，客户端再拿授权码换长期令牌；
   全程支持 PKCE（S256 / plain），授权码 10 分钟过期且只能用一次。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any

import db as store

TOKEN_PREFIX = "xd_mcp_"
DEFAULT_TTL_DAYS = 90
MAX_TTL_DAYS = 365
AUTH_CODE_TTL = 600  # 授权码 10 分钟

# ---- 权限范围（scope）----
SCOPES: dict[str, str] = {
    "codes:read": "读取邀请码（搜索、详情、统计、社区入口）",
    "codes:write": "发布邀请码与邀请码链接",
    "account:read": "读取你的账号资料与分享记录",
    "tokens:manage": "管理连接令牌（查询 / 新建 / 撤销）",
}
DEFAULT_SCOPES = ["codes:read", "codes:write", "account:read"]
ALL_SCOPES = list(SCOPES)


def _now() -> int:
    return int(time.time())


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def normalize_scopes(scopes: Any) -> list[str]:
    """把各种形态的 scope 输入规整成合法列表。"""
    if not scopes:
        return list(DEFAULT_SCOPES)
    if isinstance(scopes, str):
        parts = scopes.replace(",", " ").split()
    elif isinstance(scopes, (list, tuple, set)):
        parts = [str(s) for s in scopes]
    else:
        parts = [str(scopes)]
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p in SCOPES and p not in out:
            out.append(p)
    return out or list(DEFAULT_SCOPES)


def scope_text(scopes: list[str]) -> str:
    return " ".join(scopes)


def new_token_value() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# 连接令牌
# ---------------------------------------------------------------------------

def create_token(
    user_id: int,
    name: str = "",
    scopes: Any = None,
    ttl_days: int = DEFAULT_TTL_DAYS,
    client_id: str = "",
) -> dict[str, Any]:
    """签发新的连接令牌。返回明文（仅此一次）。"""
    plaintext = new_token_value()
    now = _now()
    try:
        days = int(ttl_days or DEFAULT_TTL_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_TTL_DAYS
    days = max(1, min(days, MAX_TTL_DAYS))
    expires_at = now + days * 86400
    sc = normalize_scopes(scopes)
    with store.db() as conn:
        cur = conn.execute(
            "INSERT INTO mcp_tokens(user_id,name,token_hash,token_prefix,scopes,client_id,"
            "created_at,expires_at,last_used_at,revoked_at,use_count) "
            "VALUES(?,?,?,?,?,?,?,?,0,0,0)",
            (
                int(user_id),
                (name or "AI 客户端")[:40],
                _hash(plaintext),
                plaintext[: len(TOKEN_PREFIX) + 6],
                json.dumps(sc, ensure_ascii=False),
                (client_id or "")[:80],
                now,
                expires_at,
            ),
        )
        token_id = int(cur.lastrowid or 0)
    return {
        "id": token_id,
        "token": plaintext,
        "scopes": sc,
        "expiresAt": expires_at,
        "prefix": plaintext[: len(TOKEN_PREFIX) + 6],
        "name": (name or "AI 客户端")[:40],
    }


def _row_to_dict(row) -> dict[str, Any]:
    try:
        scopes = json.loads(row["scopes"] or "[]")
    except (json.JSONDecodeError, TypeError):
        scopes = []
    if not isinstance(scopes, list):
        scopes = []
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]) if "user_id" in row.keys() else 0,
        "name": row["name"] or "",
        "prefix": row["token_prefix"] or "",
        "scopes": scopes,
        "clientId": row["client_id"] or "",
        "createdAt": int(row["created_at"] or 0),
        "expiresAt": int(row["expires_at"] or 0),
        "lastUsedAt": int(row["last_used_at"] or 0),
        "revokedAt": int(row["revoked_at"] or 0),
        "useCount": int(row["use_count"] or 0),
        "active": not int(row["revoked_at"] or 0) and int(row["expires_at"] or 0) > _now(),
    }


def list_tokens(user_id: int) -> list[dict[str, Any]]:
    with store.db() as conn:
        rows = conn.execute(
            "SELECT id,user_id,name,token_prefix,scopes,client_id,created_at,expires_at,"
            "last_used_at,revoked_at,use_count FROM mcp_tokens WHERE user_id=? "
            "ORDER BY id DESC",
            (int(user_id),),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def revoke_token(user_id: int, token_id: int) -> bool:
    """撤销某个令牌（只影响本人名下）。"""
    with store.db() as conn:
        cur = conn.execute(
            "UPDATE mcp_tokens SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at=0",
            (_now(), int(token_id), int(user_id)),
        )
        return cur.rowcount > 0


def revoke_all_tokens(user_id: int) -> int:
    with store.db() as conn:
        cur = conn.execute(
            "UPDATE mcp_tokens SET revoked_at=? WHERE user_id=? AND revoked_at=0",
            (_now(), int(user_id)),
        )
        return cur.rowcount


def revoke_client_tokens(user_id: int, client_id: str) -> int:
    with store.db() as conn:
        cur = conn.execute(
            "UPDATE mcp_tokens SET revoked_at=? WHERE user_id=? AND client_id=? AND revoked_at=0",
            (_now(), int(user_id), client_id),
        )
        return cur.rowcount


def touch_token(token_id: int, min_interval: int = 60) -> None:
    """更新最近使用时间与调用次数。

    默认 60 秒内不重复写库，避免高频调用把 SQLite 写放大。
    """
    with store.db() as conn:
        cur = conn.execute(
            "UPDATE mcp_tokens SET last_used_at=?, use_count=use_count+1 "
            "WHERE id=? AND (last_used_at=0 OR last_used_at < ?)",
            (_now(), int(token_id), _now() - max(0, int(min_interval))),
        )
        if cur.rowcount == 0:
            # 只更新计数，不动时间戳
            conn.execute(
                "UPDATE mcp_tokens SET use_count=use_count+1 WHERE id=?", (int(token_id),)
            )


def resolve_token(token: str) -> dict[str, Any] | None:
    """校验连接令牌。有效返回记录（含 user_id / scopes），无效返回 None。"""
    if not token:
        return None
    token = token.strip()
    if not token.startswith(TOKEN_PREFIX):
        return None
    with store.db() as conn:
        row = conn.execute(
            "SELECT id,user_id,name,token_prefix,scopes,client_id,created_at,expires_at,"
            "last_used_at,revoked_at,use_count FROM mcp_tokens WHERE token_hash=?",
            (_hash(token),),
        ).fetchone()
    if not row:
        return None
    if int(row["revoked_at"] or 0):
        return None
    if int(row["expires_at"] or 0) <= _now():
        return None
    return _row_to_dict(row)


def active_token_count(user_id: int) -> int:
    with store.db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM mcp_tokens WHERE user_id=? AND revoked_at=0 AND expires_at>?",
            (int(user_id), _now()),
        ).fetchone()
    return int(row["c"] or 0) if row else 0


# ---------------------------------------------------------------------------
# MCP 客户端
# ---------------------------------------------------------------------------

def register_client(
    client_id: str, name: str = "", redirect_uris: Any = "", update: bool = False
) -> dict[str, Any]:
    """注册（或更新）一个 MCP 客户端。

    ``update=False``（默认）时**不会**覆盖已登记的回调地址 ——
    授权页是 GET 请求，若允许它改写回调地址，攻击者就能用一个已知的
    client_id 把合法客户端的回调劫持到自己的域名。只有显式的动态注册
    接口（``/mcp/register``）才传 ``update=True``。
    """
    cid = (client_id or "").strip()[:120]
    if not cid:
        cid = "client-" + secrets.token_hex(8)
    uris = redirect_uris if isinstance(redirect_uris, str) else (
        ",".join(str(u) for u in redirect_uris) if redirect_uris else ""
    )
    now = _now()
    with store.db() as conn:
        row = conn.execute(
            "SELECT client_id,name,redirect_uris,created_at,approved_at,last_used_at "
            "FROM mcp_clients WHERE client_id=?",
            (cid,),
        ).fetchone()
        if row:
            old_uris = row["redirect_uris"] or ""
            old_name = row["name"] or ""
            final_uris = (uris[:500] if (update and uris) else old_uris)
            # 名称只在「原本没名字」或「原本就是 client_id」时补一次，避免被随意改名
            final_name = old_name if old_name and old_name != cid else (name or cid)[:60]
            conn.execute(
                "UPDATE mcp_clients SET name=?, redirect_uris=? WHERE client_id=?",
                (final_name[:60], final_uris, cid),
            )
            return {
                "clientId": cid,
                "name": final_name[:60],
                "redirectUris": [u for u in final_uris.split(",") if u],
                "createdAt": int(row["created_at"] or 0),
                "approvedAt": int(row["approved_at"] or 0),
            }
        conn.execute(
            "INSERT INTO mcp_clients(client_id,name,redirect_uris,created_at,approved_at,"
            "approved_by,last_used_at) VALUES(?,?,?,?,0,0,0)",
            (cid, (name or cid)[:60], uris[:500], now),
        )
    return {
        "clientId": cid,
        "name": (name or cid)[:60],
        "redirectUris": [u for u in uris.split(",") if u],
        "createdAt": now,
        "approvedAt": 0,
    }


def get_client(client_id: str) -> dict[str, Any] | None:
    with store.db() as conn:
        row = conn.execute(
            "SELECT client_id,name,redirect_uris,created_at,approved_at,approved_by,last_used_at "
            "FROM mcp_clients WHERE client_id=?",
            ((client_id or "").strip()[:120],),
        ).fetchone()
    if not row:
        return None
    return {
        "clientId": row["client_id"],
        "name": row["name"] or "",
        "redirectUris": [u for u in (row["redirect_uris"] or "").split(",") if u],
        "createdAt": int(row["created_at"] or 0),
        "approvedAt": int(row["approved_at"] or 0),
        "approvedBy": int(row["approved_by"] or 0),
        "lastUsedAt": int(row["last_used_at"] or 0),
    }


def list_clients(user_id: int) -> list[dict[str, Any]]:
    """列出该用户已授权过的客户端。"""
    with store.db() as conn:
        rows = conn.execute(
            "SELECT c.client_id, c.name, c.redirect_uris, c.created_at, c.approved_at, "
            "c.last_used_at, (SELECT COUNT(*) FROM mcp_tokens t "
            "  WHERE t.client_id=c.client_id AND t.user_id=? AND t.revoked_at=0 "
            "  AND t.expires_at>?) AS token_count "
            "FROM mcp_clients c "
            "WHERE c.approved_by=? OR c.client_id IN "
            "  (SELECT client_id FROM mcp_tokens WHERE user_id=? AND client_id<>'') "
            "ORDER BY c.approved_at DESC, c.created_at DESC",
            (int(user_id), _now(), int(user_id), int(user_id)),
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "clientId": r["client_id"],
            "name": r["name"] or r["client_id"],
            "redirectUris": [u for u in (r["redirect_uris"] or "").split(",") if u],
            "createdAt": int(r["created_at"] or 0),
            "approvedAt": int(r["approved_at"] or 0),
            "lastUsedAt": int(r["last_used_at"] or 0),
            "tokenCount": int(r["token_count"] or 0),
        })
    return out


def mark_client_approved(client_id: str, user_id: int) -> None:
    with store.db() as conn:
        conn.execute(
            "UPDATE mcp_clients SET approved_at=?, approved_by=? WHERE client_id=?",
            (_now(), int(user_id), (client_id or "").strip()[:120]),
        )


def mark_client_used(client_id: str) -> None:
    if not client_id:
        return
    with store.db() as conn:
        conn.execute(
            "UPDATE mcp_clients SET last_used_at=? WHERE client_id=?",
            (_now(), (client_id or "").strip()[:120]),
        )


def revoke_client(user_id: int, client_id: str) -> int:
    """解除某个客户端的授权：撤销其令牌 + 清空授权标记。"""
    cid = (client_id or "").strip()[:120]
    n = revoke_client_tokens(user_id, cid)
    with store.db() as conn:
        conn.execute(
            "UPDATE mcp_clients SET approved_at=0, approved_by=0 WHERE client_id=? AND approved_by=?",
            (cid, int(user_id)),
        )
    return n


# ---------------------------------------------------------------------------
# 授权码（浏览器远端授权）
# ---------------------------------------------------------------------------

def create_auth_code(
    user_id: int,
    client_id: str = "",
    redirect_uri: str = "",
    scope: Any = "",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
) -> str:
    code = "ac_" + secrets.token_urlsafe(24)
    now = _now()
    sc = scope_text(normalize_scopes(scope))
    with store.db() as conn:
        conn.execute(
            "INSERT INTO mcp_auth_codes(code,client_id,user_id,redirect_uri,scope,state,"
            "code_challenge,code_challenge_method,created_at,expires_at,used_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,0)",
            (
                code,
                (client_id or "")[:120],
                int(user_id),
                (redirect_uri or "")[:500],
                sc[:400],
                (state or "")[:300],
                (code_challenge or "")[:200],
                (code_challenge_method or "")[:20],
                now,
                now + AUTH_CODE_TTL,
            ),
        )
    return code


def consume_auth_code(
    code: str, client_id: str = "", redirect_uri: str = "", verifier: str = ""
) -> dict[str, Any]:
    """校验并消费授权码。成功返回 {ok, user_id, scope, client_id}。"""
    code = (code or "").strip()
    if not code:
        return {"ok": False, "error": "缺少授权码"}
    with store.db() as conn:
        row = conn.execute(
            "SELECT * FROM mcp_auth_codes WHERE code=?", (code,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "授权码无效"}
        if int(row["used_at"] or 0):
            return {"ok": False, "error": "授权码已被使用"}
        if int(row["expires_at"] or 0) <= _now():
            return {"ok": False, "error": "授权码已过期"}
        if client_id and row["client_id"] and client_id != row["client_id"]:
            return {"ok": False, "error": "授权码与客户端不匹配"}
        if redirect_uri and row["redirect_uri"] and redirect_uri != row["redirect_uri"]:
            return {"ok": False, "error": "回调地址不匹配"}
        # PKCE 校验
        challenge = row["code_challenge"] or ""
        if challenge:
            method = (row["code_challenge_method"] or "plain").lower()
            if not verifier:
                return {"ok": False, "error": "缺少 code_verifier"}
            if method == "s256":
                import base64
                import hashlib as _h

                digest = _h.sha256(verifier.encode()).digest()
                calc = base64.urlsafe_b64encode(digest).decode().rstrip("=")
            else:
                calc = verifier
            if not secrets.compare_digest(calc, challenge):
                return {"ok": False, "error": "code_verifier 校验失败"}
        conn.execute(
            "UPDATE mcp_auth_codes SET used_at=? WHERE code=?", (_now(), code)
        )
    return {
        "ok": True,
        "user_id": int(row["user_id"]),
        "scope": row["scope"] or "",
        "client_id": row["client_id"] or "",
        "redirect_uri": row["redirect_uri"] or "",
    }
