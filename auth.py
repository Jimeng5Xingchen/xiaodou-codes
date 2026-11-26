"""用户系统：邮箱验证码注册/登录 + 会话管理（SQLite，零额外依赖）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import db as store
import mailer

DB_PATH = store.DB_PATH
CODE_TTL = 600          # 验证码 10 分钟
SESSION_TTL = 30 * 86400  # 会话 30 天
SEND_COOLDOWN = 60      # 同邮箱 60 秒只能发一次

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_lock = threading.RLock()

# 无状态会话签名密钥：登录态完全编码在 token 里，不依赖服务端 session 表。
# 这样即使发布环境多实例 / SQLite 不共享，刷新页面也能保持登录。
# 注意：必须硬编码常量，不能用环境变量——发布平台可能给不同 worker 注入不同的值，
# 导致「签发进程」和「验证进程」密钥不一致，登录态随即失效。
_TOKEN_SECRET = b"xiaodou-codes-v1-static-secret"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_token(user: dict[str, Any]) -> str:
    """签发无状态 token：payload(base64url).signature(hmac)。"""
    payload = {
        "id": user["id"],
        "email": user["email"],
        "username": user.get("username", "") or "",
        "bio": user.get("bio", "") or "",
        "gender": user.get("gender", "") or "",
        "has_password": bool(user.get("has_password")),
        "role": user.get("role", "user") or "user",
        "group_id": int(user.get("group_id", 0) or 0),
        "exp": int(time.time()) + SESSION_TTL,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    sig = _b64e(hmac.new(_TOKEN_SECRET, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def parse_token(token: str) -> dict[str, Any] | None:
    """验签并解析无状态 token，过期/篡改返回 None。"""
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    try:
        expect = _b64e(hmac.new(_TOKEN_SECRET, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expect):
            return None
        payload = json.loads(_b64d(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return {
            "id": int(payload["id"]),
            "email": payload["email"],
            "username": payload.get("username", ""),
            "bio": payload.get("bio", ""),
            "gender": payload.get("gender", ""),
            "has_password": bool(payload.get("has_password")),
            "role": payload.get("role", "user") or "user",
            "group_id": int(payload.get("group_id", 0) or 0),
        }
    except Exception:
        return None


def _db():
    return store.db()


def init_db() -> None:
    store.init_db()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


_PBKDF2_ROUNDS = 120_000


def _make_password(password: str) -> tuple[str, str]:
    """生成 (哈希, 盐)，PBKDF2-HMAC-SHA256。"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ROUNDS
    ).hex()
    return digest, salt


def _check_password(password: str, digest: str, salt: str) -> bool:
    if not digest or not salt:
        return False
    try:
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ROUNDS
        ).hex()
    except ValueError:
        return False
    return secrets.compare_digest(candidate, digest)


def valid_password(pw: str) -> bool:
    """密码规则：6-32 位。"""
    return isinstance(pw, str) and 6 <= len(pw) <= 32


def valid_email(email: str) -> bool:
    return bool(email) and len(email) <= 254 and bool(EMAIL_RE.match(email))


# ---------------------------------------------------------------------------
# 注册策略（由管理端配置）
# ---------------------------------------------------------------------------

# 严格模式：只允许「字母数字 . _ - +」等常见邮箱字符，
# 关闭特殊符号后进一步收紧为「字母数字与 . - _」的白名单形态。
_STRICT_EMAIL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
_PLAIN_EMAIL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


def allow_special_email() -> bool:
    """是否允许带特殊符号（+ % 等）的邮箱注册。默认允许。"""
    return store.get_setting("allow_special_email", "1") != "0"


def email_whitelist() -> list[str]:
    """邮箱白名单（每行一个，支持完整邮箱或 @域名 形式）。"""
    raw = store.get_setting("email_whitelist", "")
    return [line.strip() for line in raw.replace(",", "\n").split("\n") if line.strip()]


def _email_in_whitelist(email: str, whitelist: list[str]) -> bool:
    """邮箱是否命中白名单（支持完整邮箱与 @域名 两种写法）。"""
    email = (email or "").strip().lower()
    for item in whitelist:
        item = item.strip().lower()
        if not item:
            continue
        if item.startswith("@"):
            if email.endswith(item):
                return True
        elif item == email:
            return True
    return False


def check_email_allowed(email: str) -> tuple[bool, str]:
    """注册 / 换绑邮箱前的策略校验。返回 (是否允许, 错误说明)。"""
    email = (email or "").strip().lower()
    if not email:
        return False, "邮箱不能为空"
    if not valid_email(email):
        return False, "邮箱格式不正确"
    if not allow_special_email() and not _PLAIN_EMAIL_RE.match(email):
        return False, "当前不允许使用含特殊符号的邮箱（如 + % 等）"
    whitelist = email_whitelist()
    if whitelist and not _email_in_whitelist(email, whitelist):
        return False, "该邮箱不在允许注册的白名单内"
    return True, ""


def registration_enabled() -> bool:
    """是否开放新用户注册（管理端可关闭）。"""
    return store.get_setting("registration_enabled", "1") != "0"


def send_code(email: str, purpose: str = "login") -> dict[str, Any]:
    """发送验证码。返回 {ok, error, dev_code, cooldown}。"""
    email = (email or "").strip().lower()
    if not valid_email(email):
        return {"ok": False, "error": "邮箱格式不正确"}
    # 特殊符号策略：任何发码场景都生效
    if purpose in ("login", "change_email") and not allow_special_email():
        if not _PLAIN_EMAIL_RE.match(email):
            return {"ok": False, "error": "当前不允许使用含特殊符号的邮箱（如 + % 等）"}
    # 白名单只约束「尚未注册」的邮箱，避免把已有用户挡在门外
    if purpose in ("login", "change_email"):
        whitelist = email_whitelist()
        if whitelist and not _email_in_whitelist(email, whitelist):
            with _db() as c2:
                exists = c2.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone()
            if not exists:
                return {"ok": False, "error": "该邮箱不在允许注册的白名单内"}
    now = int(time.time())
    with _lock, _db() as conn:
        row = conn.execute(
            "SELECT sent_at FROM codes WHERE email=? AND purpose=?", (email, purpose)
        ).fetchone()
        if row and now - row["sent_at"] < SEND_COOLDOWN:
            wait = SEND_COOLDOWN - (now - row["sent_at"])
            return {"ok": False, "error": f"请 {wait} 秒后再发送", "cooldown": wait}
        code = f"{secrets.randbelow(1000000):06d}"
        conn.execute(
            "INSERT OR REPLACE INTO codes(email,code,expires_at,sent_at,attempts,purpose) "
            "VALUES(?,?,?,?,0,?)",
            (email, code, now + CODE_TTL, now, purpose),
        )

    result = mailer.send_verification_code(email, code, purpose=purpose)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "邮件发送失败")}
    out = {"ok": True, "message": "验证码已发送", "provider": result.get("provider")}
    if result.get("dev_code"):
        out["dev_code"] = result["dev_code"]  # 仅 dev 模式直显
    return out


def verify_code(
    email: str, code: str, *, create_session: bool = True, purpose: str = "login"
) -> dict[str, Any]:
    """校验验证码，成功则注册/登录并返回会话 token。"""
    email = (email or "").strip().lower()
    code = (code or "").strip()
    if not valid_email(email) or not code:
        return {"ok": False, "error": "邮箱或验证码无效"}
    now = int(time.time())
    with _lock, _db() as conn:
        row = conn.execute(
            "SELECT * FROM codes WHERE email=? AND purpose=?", (email, purpose)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "请先获取验证码"}
        if now > row["expires_at"]:
            return {"ok": False, "error": "验证码已过期，请重新获取"}
        if row["attempts"] >= 10:
            return {"ok": False, "error": "尝试次数过多，请重新获取验证码"}
        if code != row["code"]:
            conn.execute(
                "UPDATE codes SET attempts=attempts+1 WHERE email=? AND purpose=?",
                (email, purpose),
            )
            return {"ok": False, "error": "验证码错误"}
        # 校验通过，删除验证码
        conn.execute("DELETE FROM codes WHERE email=? AND purpose=?", (email, purpose))
        # 纯验证场景（如改邮箱验证新邮箱）：不注册、不建会话
        if not create_session:
            return {"ok": True, "verified": True, "email": email}
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user:
            user_id = user["id"]
            conn.execute("UPDATE users SET last_login=? WHERE id=?", (now, user_id))
            is_new = False
        else:
            # 全新用户 = 注册，受「是否开放注册」开关约束
            if not registration_enabled():
                return {"ok": False, "error": "本站当前已关闭新用户注册"}
            cur = conn.execute(
                "INSERT INTO users(email,created_at,last_login) VALUES(?,?,?)", (email, now, now)
            )
            user_id = cur.lastrowid
            is_new = True

        profile = conn.execute(
            "SELECT username, bio, gender, role, status, group_id FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        user_obj = {
            "id": user_id,
            "email": email,
            "username": (profile["username"] if profile else "") or "",
            "bio": (profile["bio"] if profile else "") or "",
            "gender": (profile["gender"] if profile else "") or "",
            "has_password": bool(user["password_hash"]) if user else False,
            "role": (profile["role"] if profile else "user") or "user",
            "status": (profile["status"] if profile else "active") or "active",
            "group_id": int((profile["group_id"] if profile else 0) or 0),
        }
        # 已封禁的账号禁止登录
        if user_obj["status"] == "banned":
            reason = conn.execute(
                "SELECT ban_reason FROM users WHERE id=?", (user_id,)
            ).fetchone()
            note = (reason["ban_reason"] if reason else "") or ""
            return {"ok": False, "error": ("该账号已被封禁" + (f"：{note}" if note else ""))}
        # 兼容：仍写入 session 表；但登录态以无状态 token 为准，不依赖它
        token = ""
        if create_session:
            token = sign_token(user_obj)
            try:
                conn.execute(
                    "INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                    (_hash(token), user_id, now + SESSION_TTL, now),
                )
            except sqlite3.Error:
                pass
        return {
            "ok": True,
            "token": token,
            "user": {**user_obj, "is_new": is_new},
        }


def _public_user(row) -> dict[str, Any]:
    """把 users 行转为公开用户信息（不含密码）。"""
    keys = row.keys()
    return {
        "id": row["id"],
        "email": row["email"],
        "username": (row["username"] if "username" in keys else "") or "",
        "bio": (row["bio"] if "bio" in keys else "") or "",
        "gender": (row["gender"] if "gender" in keys else "") or "",
        "has_password": bool(("password_hash" in keys and row["password_hash"])),
        "role": (row["role"] if "role" in keys else "user") or "user",
        "status": (row["status"] if "status" in keys else "active") or "active",
        "group_id": int((row["group_id"] if "group_id" in keys else 0) or 0),
    }


def login_with_password(email: str, password: str) -> dict[str, Any]:
    """密码登录。返回 {ok, error, token, user}。"""
    email = (email or "").strip().lower()
    password = password or ""
    if not valid_email(email) or not password:
        return {"ok": False, "error": "邮箱或密码无效"}
    now = int(time.time())
    with _lock, _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not row or not row["password_hash"]:
            return {"ok": False, "error": "邮箱或密码错误（该账号可能未设置密码）"}
        if not _check_password(password, row["password_hash"], row["password_salt"] or ""):
            return {"ok": False, "error": "邮箱或密码错误"}
        if (row["status"] if "status" in row.keys() else "active") == "banned":
            note = (row["ban_reason"] if "ban_reason" in row.keys() else "") or ""
            return {"ok": False, "error": "该账号已被封禁" + (f"：{note}" if note else "")}
        conn.execute("UPDATE users SET last_login=? WHERE id=?", (now, row["id"]))
        user = _public_user(row)
        token = sign_token(user)
        try:
            conn.execute(
                "INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                (_hash(token), row["id"], now + SESSION_TTL, now),
            )
        except sqlite3.Error:
            pass
        user["is_new"] = False
        return {"ok": True, "token": token, "user": user}


def set_password(token: str, password: str) -> dict[str, Any]:
    """为已登录用户设置密码（可直接设置，用于首次开启密码登录）。"""
    user = user_by_token(token)
    if not user:
        return {"ok": False, "error": "请先登录"}
    if not valid_password(password):
        return {"ok": False, "error": "密码需 6-32 位"}
    digest, salt = _make_password(password)
    with _lock, _db() as conn:
        conn.execute(
            "UPDATE users SET password_hash=?, password_salt=? WHERE id=?",
            (digest, salt, user["id"]),
        )
    fresh = {**user, "has_password": True}
    return {"ok": True, "message": "密码已设置", "user": fresh, "token": sign_token(fresh)}


def reset_password_send(email: str) -> dict[str, Any]:
    """忘记密码：给已绑定邮箱发重置验证码。"""
    email = (email or "").strip().lower()
    if not valid_email(email):
        return {"ok": False, "error": "邮箱格式不正确"}
    with _db() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone()
        if not exists:
            # 不暴露账号是否存在，统一提示
            return {"ok": True, "message": "如果该邮箱已绑定，验证码已发送"}
    return send_code(email, purpose="reset_password")


def reset_password_confirm(email: str, code: str, new_password: str) -> dict[str, Any]:
    """忘记密码：校验验证码并设置新密码。"""
    email = (email or "").strip().lower()
    code = (code or "").strip()
    if not valid_password(new_password):
        return {"ok": False, "error": "密码需 6-32 位"}
    result = verify_code(email, code, create_session=False, purpose="reset_password")
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "验证失败")}
    digest, salt = _make_password(new_password)
    with _lock, _db() as conn:
        row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if not row:
            return {"ok": False, "error": "账号不存在"}
        conn.execute(
            "UPDATE users SET password_hash=?, password_salt=? WHERE id=?",
            (digest, salt, row["id"]),
        )
    return {"ok": True, "message": "密码已重置，请用新密码登录"}


def user_by_token(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    # 主路径：无状态签名 token，验签即可，不依赖服务端 session 表
    signed = parse_token(token)
    if signed:
        return signed
    # 兼容旧版随机 token：回退查 session 表
    now = int(time.time())
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT u.id, u.email, u.username, u.bio, u.gender, u.password_hash, "
                "u.role, u.status, u.group_id, u.created_at, u.last_login "
                "FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token_hash=? AND s.expires_at>?",
                (_hash(token), now),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    return {
        "id": row["id"],
        "email": row["email"],
        "username": row["username"] or "",
        "bio": row["bio"] or "",
        "gender": row["gender"] or "",
        "has_password": bool(row["password_hash"]),
        "role": row["role"] or "user",
        "status": row["status"] or "active",
        "group_id": int(row["group_id"] or 0),
    }


def display_name(user: dict[str, Any]) -> str:
    """分享者展示名：优先用户名，其次邮箱前缀（脱敏）。"""
    if not user:
        return ""
    name = (user.get("username") or "").strip()
    if name:
        return name[:20]
    email = user.get("email") or ""
    return email.split("@")[0][:20] if email else ""


def mask_email(email: str) -> str:
    """邮箱脱敏：abc@example.com -> a***@example.com。"""
    if not email or "@" not in email:
        return ""
    name, domain = email.split("@", 1)
    head = name[0] if name else ""
    return f"{head}***@{domain}"


def user_by_id(user_id: int) -> dict[str, Any] | None:
    """按 id 查用户公开资料（用于展示分享者）。"""
    if not user_id:
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT id, email, username, bio, gender, role, status, group_id "
            "FROM users WHERE id=?", (user_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "email": row["email"],
        "username": row["username"] or "",
        "bio": row["bio"] or "",
        "gender": row["gender"] or "",
        "role": row["role"] or "user",
        "status": row["status"] or "active",
        "group_id": int(row["group_id"] or 0),
    }


def sharer_info(user_id: int) -> dict[str, Any]:
    """生成分享者公开展示信息（不含完整邮箱）。"""
    user = user_by_id(user_id)
    if not user:
        return {}
    return {
        "name": display_name(user),
        "emailMasked": mask_email(user["email"]),
        "gender": user["gender"],
        "bio": user["bio"],
    }


def logout(token: str) -> None:
    if token:
        with _db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash(token),))


def record_submission(token: str, code: str, source: str = "") -> bool:
    """记录用户提交的码（去重）。返回是否新增。"""
    user = user_by_token(token)
    if not user:
        return False
    with _lock, _db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO submissions(code,user_id,source,created_at) VALUES(?,?,?,?)",
            (code.upper(), user["id"], source[:40], int(time.time())),
        )
        return cur.rowcount > 0


def update_profile(
    token: str, *, username: str | None = None, bio: str | None = None, gender: str | None = None
) -> dict[str, Any]:
    """更新当前用户资料。返回 {ok, error, user}。"""
    user = user_by_token(token)
    if not user:
        return {"ok": False, "error": "请先登录"}
    sets: list[str] = []
    vals: list[Any] = []
    if username is not None:
        name = str(username).strip()[:20]
        sets.append("username=?"); vals.append(name)
    if bio is not None:
        text = str(bio).strip()[:100]
        sets.append("bio=?"); vals.append(text)
    if gender is not None:
        g = str(gender).strip()
        if g not in ("", "male", "female", "other"):
            g = ""
        sets.append("gender=?"); vals.append(g)
    if not sets:
        return {"ok": False, "error": "没有要更新的内容"}
    with _lock, _db() as conn:
        vals.append(user["id"])
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", vals)
    fresh = dict(user)
    if username is not None:
        fresh["username"] = str(username).strip()[:20]
    if bio is not None:
        fresh["bio"] = str(bio).strip()[:100]
    if gender is not None:
        g = str(gender).strip()
        fresh["gender"] = g if g in ("", "male", "female", "other") else ""
    return {"ok": True, "user": fresh, "token": sign_token(fresh)}


def change_email_send(token: str, new_email: str) -> dict[str, Any]:
    """给新邮箱发验证码（purpose=change_email）。"""
    user = user_by_token(token)
    if not user:
        return {"ok": False, "error": "请先登录"}
    new_email = (new_email or "").strip().lower()
    if not valid_email(new_email):
        return {"ok": False, "error": "邮箱格式不正确"}
    if new_email == user["email"]:
        return {"ok": False, "error": "新邮箱与当前邮箱相同"}
    with _db() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE email=?", (new_email,)).fetchone()
        if exists:
            return {"ok": False, "error": "该邮箱已被其他账号使用"}
    return send_code(new_email, purpose="change_email")


def change_email_confirm(token: str, new_email: str, code: str) -> dict[str, Any]:
    """校验新邮箱验证码并更换邮箱，保留所有资料与提交记录。"""
    user = user_by_token(token)
    if not user:
        return {"ok": False, "error": "请先登录，或登录已过期"}
    new_email = (new_email or "").strip().lower()
    code = (code or "").strip()
    result = verify_code(new_email, code, create_session=False, purpose="change_email")
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "验证失败")}
    with _lock, _db() as conn:
        try:
            conn.execute("UPDATE users SET email=? WHERE id=?", (new_email, user["id"]))
        except sqlite3.IntegrityError:
            return {"ok": False, "error": "该邮箱已被其他账号使用"}
    fresh = {**user, "email": new_email}
    return {"ok": True, "user": fresh, "token": sign_token(fresh), "message": "邮箱已更新"}


def stats(token: str) -> dict[str, Any]:
    user = user_by_token(token)
    if not user:
        return {}
    with _db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM submissions WHERE user_id=?", (user["id"],)
        ).fetchone()["c"]
    return {"submitted": n}


# ---------------------------------------------------------------------------
# 账号注销
# ---------------------------------------------------------------------------

def delete_account(token: str, password: str = "") -> dict[str, Any]:
    """注销当前账号：清空资料、密码、分享记录与登录态。

    要求校验密码（已设置密码时）；未设置密码的账号可用邮箱验证码注销，
    由调用方先完成 verify_code(purpose="delete_account")。
    """
    user = user_by_token(token)
    if not user:
        return {"ok": False, "error": "请先登录，或登录已过期"}
    with _lock, _db() as conn:
        row = conn.execute(
            "SELECT password_hash, password_salt FROM users WHERE id=?", (user["id"],)
        ).fetchone()
        if row and row["password_hash"]:
            if not password:
                return {"ok": False, "error": "请输入密码以确认注销", "needPassword": True}
            if not _check_password(password, row["password_hash"], row["password_salt"] or ""):
                return {"ok": False, "error": "密码不正确"}
        # 清空个人资料与凭证，保留账号行以便统计
        conn.execute(
            "UPDATE users SET username='', bio='', gender='', password_hash='', "
            "password_salt='', status='deleted', group_id=0 WHERE id=?",
            (user["id"],),
        )
        conn.execute("DELETE FROM submissions WHERE user_id=?", (user["id"],))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        conn.execute("DELETE FROM codes WHERE email=?", (user["email"],))
    return {"ok": True, "message": "账号已注销，相关资料与分享记录已清除"}


# ---------------------------------------------------------------------------
# 管理员：初始化
# ---------------------------------------------------------------------------

def is_initialized() -> bool:
    """系统是否已完成初始部署（创建过管理员）。"""
    if store.get_setting("setup_done", "") == "1":
        return True
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE role='admin'"
        ).fetchone()
    return bool(row and row["c"])


def count_admins() -> int:
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE role='admin' AND status!='deleted'"
        ).fetchone()
    return int(row["c"]) if row else 0


def setup_initial_admin(
    account: str, password: str, email: str, username: str = ""
) -> dict[str, Any]:
    """初始部署：创建第一个管理员账号。

    account  登录账号（邮箱）
    password 登录密码
    email    绑定的邮箱
    """
    if is_initialized():
        return {"ok": False, "error": "系统已完成初始化，无法重复执行"}
    email = (email or "").strip().lower()
    account = (account or "").strip().lower()
    if not account:
        account = email
    if not valid_email(account):
        return {"ok": False, "error": "登录账号必须是合法邮箱"}
    if not valid_email(email):
        return {"ok": False, "error": "邮箱格式不正确"}
    if not valid_password(password):
        return {"ok": False, "error": "密码需 6-32 位"}
    if account != email:
        with _db() as conn:
            exists = conn.execute("SELECT 1 FROM users WHERE email=?", (account,)).fetchone()
        if exists:
            return {"ok": False, "error": "该账号已被占用"}
    digest, salt = _make_password(password)
    now = int(time.time())
    with _lock, _db() as conn:
        # 兜底：并发下只允许一个初始化
        exists = conn.execute(
            "SELECT 1 FROM users WHERE role='admin'"
        ).fetchone()
        if exists:
            return {"ok": False, "error": "系统已完成初始化，无法重复执行"}
        try:
            conn.execute(
                "INSERT INTO users(email,username,bio,gender,password_hash,password_salt,"
                "role,status,group_id,created_at,last_login) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    account,
                    (username or "").strip()[:20] or "管理员",
                    "",
                    "",
                    digest,
                    salt,
                    "admin",
                    "active",
                    0,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            return {"ok": False, "error": "该账号已被占用"}
        # 记录绑定邮箱与初始化标记（复用同一连接，避免嵌套开连接锁库）
        store.set_setting_conn(conn, "admin_email", email)
        store.set_setting_conn(conn, "setup_done", "1")
        store.set_setting_conn(conn, "setup_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    user = user_by_id_by_email(account)
    if not user:
        return {"ok": False, "error": "初始化异常，请重试"}
    user["role"] = "admin"
    user["status"] = "active"
    user["group_id"] = 0
    token = sign_token(user)
    return {"ok": True, "message": "初始化完成，已创建管理员账号", "token": token, "user": user}


def user_by_id_by_email(email: str) -> dict[str, Any] | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT id, email, username, bio, gender, role, status, group_id "
            "FROM users WHERE email=?", ((email or "").strip().lower(),)
        ).fetchone()
    if not row:
        return None
    return _public_user(row)
