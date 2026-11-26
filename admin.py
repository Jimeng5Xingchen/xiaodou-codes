"""管理端核心逻辑：用户治理、用户组、系统设置、系统授权发件邮箱。

权限模型：
- role='admin' 的账号可访问全部管理接口
- 最后一个管理员不可被封号 / 删除 / 降级，避免把系统锁死
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

import auth
import db as store

ROLE_ADMIN = "admin"
ROLE_USER = "user"
STATUS_ACTIVE = "active"
STATUS_BANNED = "banned"
STATUS_DELETED = "deleted"


# ---------------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------------

def require_admin(token: str) -> dict[str, Any] | None:
    """校验管理员身份，返回管理员用户对象；非管理员返回 None。"""
    user = auth.user_by_token(token)
    if not user:
        return None
    if user.get("role") != ROLE_ADMIN or user.get("status") == STATUS_BANNED:
        return None
    return user


# ---------------------------------------------------------------------------
# 用户组
# ---------------------------------------------------------------------------

def list_groups() -> list[dict[str, Any]]:
    with store.db() as conn:
        rows = conn.execute(
            "SELECT g.id, g.name, g.description, g.created_at, "
            "(SELECT COUNT(*) FROM users u WHERE u.group_id=g.id AND u.status!='deleted') AS members "
            "FROM groups g ORDER BY g.id ASC"
        ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "description": r["description"] or "",
            "createdAt": r["created_at"],
            "members": int(r["members"] or 0),
        }
        for r in rows
    ]


def create_group(name: str, description: str = "") -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "用户组名称不能为空"}
    if len(name) > 24:
        return {"ok": False, "error": "用户组名称不超过 24 个字"}
    try:
        with store._lock, store.db() as conn:
            conn.execute(
                "INSERT INTO groups(name,description,created_at) VALUES(?,?,?)",
                (name, description.strip()[:100], int(time.time())),
            )
    except sqlite3.IntegrityError:
        return {"ok": False, "error": "同名用户组已存在"}
    return {"ok": True, "message": "用户组已创建", "groups": list_groups()}


def update_group(group_id: int, name: str | None, description: str | None) -> dict[str, Any]:
    sets: list[str] = []
    vals: list[Any] = []
    if name is not None:
        name = name.strip()
        if not name:
            return {"ok": False, "error": "用户组名称不能为空"}
        sets.append("name=?")
        vals.append(name[:24])
    if description is not None:
        sets.append("description=?")
        vals.append(description.strip()[:100])
    if not sets:
        return {"ok": False, "error": "没有要更新的内容"}
    try:
        with store._lock, store.db() as conn:
            vals.append(group_id)
            conn.execute(f"UPDATE groups SET {', '.join(sets)} WHERE id=?", vals)
    except sqlite3.IntegrityError:
        return {"ok": False, "error": "同名用户组已存在"}
    return {"ok": True, "message": "用户组已更新", "groups": list_groups()}


def delete_group(group_id: int) -> dict[str, Any]:
    with store._lock, store.db() as conn:
        # 组内用户回到「未分组」
        conn.execute("UPDATE users SET group_id=0 WHERE group_id=?", (group_id,))
        conn.execute("DELETE FROM groups WHERE id=?", (group_id,))
    return {"ok": True, "message": "用户组已删除，组内成员已移出", "groups": list_groups()}


def assign_group(user_id: int, group_id: int) -> dict[str, Any]:
    with store._lock, store.db() as conn:
        if group_id:
            g = conn.execute("SELECT id FROM groups WHERE id=?", (group_id,)).fetchone()
            if not g:
                return {"ok": False, "error": "用户组不存在"}
        conn.execute("UPDATE users SET group_id=? WHERE id=?", (group_id, user_id))
    return {"ok": True, "message": "用户分类已更新"}


# ---------------------------------------------------------------------------
# 用户治理
# ---------------------------------------------------------------------------

def list_users(
    keyword: str = "", group_id: int | None = None, status: str = "", limit: int = 200
) -> dict[str, Any]:
    """用户列表，支持关键字 / 分组 / 状态筛选。"""
    sql = [
        "SELECT u.id, u.email, u.username, u.bio, u.gender, u.role, u.status, u.group_id, "
        "u.created_at, u.last_login, "
        "(u.password_hash != '') AS has_password, "
        "(SELECT COUNT(*) FROM submissions s WHERE s.user_id=u.id) AS submissions "
        "FROM users u WHERE u.status != 'deleted'"
    ]
    params: list[Any] = []
    kw = (keyword or "").strip().lower()
    if kw:
        sql.append(" AND (LOWER(u.email) LIKE ? OR LOWER(u.username) LIKE ?)")
        params += [f"%{kw}%", f"%{kw}%"]
    if group_id is not None:
        sql.append(" AND u.group_id = ?")
        params.append(group_id)
    if status:
        sql.append(" AND u.status = ?")
        params.append(status)
    sql.append(" ORDER BY u.id DESC LIMIT ?")
    params.append(max(1, min(int(limit or 200), 1000)))
    with store.db() as conn:
        rows = conn.execute(" ".join(sql), params).fetchall()
        groups = {g["id"]: g["name"] for g in conn.execute("SELECT id,name FROM groups").fetchall()}
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE status != 'deleted'"
        ).fetchone()["c"]
    users = [
        {
            "id": r["id"],
            "email": r["email"],
            "username": r["username"] or "",
            "bio": r["bio"] or "",
            "gender": r["gender"] or "",
            "role": r["role"] or ROLE_USER,
            "status": r["status"] or STATUS_ACTIVE,
            "groupId": int(r["group_id"] or 0),
            "groupName": groups.get(int(r["group_id"] or 0), "未分组"),
            "createdAt": r["created_at"],
            "lastLogin": r["last_login"],
            "hasPassword": bool(r["has_password"]),
            "submissions": int(r["submissions"] or 0),
        }
        for r in rows
    ]
    return {"ok": True, "users": users, "total": int(total), "count": len(users)}


def ban_user(actor_id: int, user_id: int, reason: str = "") -> dict[str, Any]:
    """封号。不可封禁自己，也不可封禁最后一个管理员。"""
    if actor_id == user_id:
        return {"ok": False, "error": "不能封禁自己的账号"}
    with store._lock, store.db() as conn:
        row = conn.execute(
            "SELECT id, role, status FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "用户不存在"}
        if row["role"] == ROLE_ADMIN:
            admins = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE role='admin' AND status='active'"
            ).fetchone()["c"]
            if admins <= 1:
                return {"ok": False, "error": "系统至少需要保留一个可用管理员"}
        conn.execute(
            "UPDATE users SET status='banned', ban_reason=?, banned_at=? WHERE id=?",
            ((reason or "").strip()[:100], int(time.time()), user_id),
        )
        # 封禁后立即失效其登录态
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    return {"ok": True, "message": "账号已封禁"}


def unban_user(user_id: int) -> dict[str, Any]:
    with store._lock, store.db() as conn:
        conn.execute(
            "UPDATE users SET status='active', ban_reason='', banned_at=0 WHERE id=?", (user_id,)
        )
    return {"ok": True, "message": "账号已解封"}


def set_user_status(actor_id: int, user_id: int, status: str, reason: str = "") -> dict[str, Any]:
    """切换账号状态：active / banned。"""
    status = (status or "").strip()
    if status not in (STATUS_ACTIVE, STATUS_BANNED):
        return {"ok": False, "error": "状态参数不合法"}
    if status == STATUS_BANNED:
        return ban_user(actor_id, user_id, reason)
    return unban_user(user_id)


def set_user_role(actor_id: int, user_id: int, role: str) -> dict[str, Any]:
    """设置管理员 / 普通用户。不可移除最后一个管理员。"""
    role = (role or "").strip()
    if role not in (ROLE_ADMIN, ROLE_USER):
        return {"ok": False, "error": "角色参数不合法"}
    if actor_id == user_id and role != ROLE_ADMIN:
        return {"ok": False, "error": "不能移除自己的管理员权限"}
    with store._lock, store.db() as conn:
        row = conn.execute("SELECT id, role FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "用户不存在"}
        if row["role"] == ROLE_ADMIN and role != ROLE_ADMIN:
            admins = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE role='admin' AND status='active'"
            ).fetchone()["c"]
            if admins <= 1:
                return {"ok": False, "error": "系统至少需要保留一个可用管理员"}
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
    return {"ok": True, "message": "角色已更新"}


def delete_user(actor_id: int, user_id: int) -> dict[str, Any]:
    """彻底删除用户账号（含分享记录与登录态）。"""
    if actor_id == user_id:
        return {"ok": False, "error": "不能删除自己的账号"}
    with store._lock, store.db() as conn:
        row = conn.execute("SELECT id, role, email FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "用户不存在"}
        if row["role"] == ROLE_ADMIN:
            admins = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE role='admin' AND status='active'"
            ).fetchone()["c"]
            if admins <= 1:
                return {"ok": False, "error": "系统至少需要保留一个可用管理员"}
        conn.execute("DELETE FROM submissions WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM codes WHERE email=?", (row["email"],))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    return {"ok": True, "message": "用户已删除"}


def reset_user_password(user_id: int, password: str) -> dict[str, Any]:
    """管理员重置指定用户密码。"""
    if not auth.valid_password(password):
        return {"ok": False, "error": "密码需 6-32 位"}
    digest, salt = auth._make_password(password)
    with store._lock, store.db() as conn:
        conn.execute(
            "UPDATE users SET password_hash=?, password_salt=? WHERE id=?",
            (digest, salt, user_id),
        )
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    return {"ok": True, "message": "密码已重置，该用户需重新登录"}


# ---------------------------------------------------------------------------
# 系统设置
# ---------------------------------------------------------------------------

def get_settings_view() -> dict[str, Any]:
    """管理端看到的系统设置（不含敏感明文）。"""
    accounts = list_mail_accounts()
    return {
        "ok": True,
        "settings": {
            "allowSpecialEmail": auth.allow_special_email(),
            "emailWhitelist": auth.email_whitelist(),
            "registrationEnabled": auth.registration_enabled(),
            "setupDone": auth.is_initialized(),
            "adminEmail": store.get_setting("admin_email", ""),
            "setupAt": store.get_setting("setup_at", ""),
        },
        "groups": list_groups(),
        "mailAccounts": accounts,
    }


def update_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """保存系统设置。"""
    if "allowSpecialEmail" in payload:
        val = payload["allowSpecialEmail"]
        store.set_setting("allow_special_email", "0" if val in (False, 0, "0", "false") else "1")
    if "registrationEnabled" in payload:
        val = payload["registrationEnabled"]
        store.set_setting("registration_enabled", "0" if val in (False, 0, "0", "false") else "1")
    if "emailWhitelist" in payload:
        raw = payload["emailWhitelist"]
        if isinstance(raw, list):
            items = [str(x).strip() for x in raw if str(x).strip()]
        else:
            items = [
                line.strip()
                for line in str(raw or "").replace(",", "\n").replace("，", "\n").split("\n")
                if line.strip()
            ]
        store.set_setting("email_whitelist", "\n".join(items))
    return {"ok": True, "message": "设置已保存", "settings": get_settings_view()["settings"]}


# ---------------------------------------------------------------------------
# 系统授权发件邮箱
# ---------------------------------------------------------------------------

def list_mail_accounts() -> list[dict[str, Any]]:
    """系统授权的发件邮箱列表（密码脱敏）。"""
    with store.db() as conn:
        rows = conn.execute(
            "SELECT id,address,provider,host,port,use_ssl,enabled,is_default,note,created_at,"
            "(password != '') AS has_password FROM mail_accounts ORDER BY id ASC"
        ).fetchall()
    return [
        {
            "id": r["id"],
            "address": r["address"],
            "provider": r["provider"] or "smtp",
            "host": r["host"] or "",
            "port": int(r["port"] or 465),
            "useSsl": bool(r["use_ssl"]),
            "enabled": bool(r["enabled"]),
            "isDefault": bool(r["is_default"]),
            "note": r["note"] or "",
            "createdAt": r["created_at"],
            "hasPassword": bool(r["has_password"]),
        }
        for r in rows
    ]


def save_mail_account(
    account_id: int = 0,
    address: str = "",
    provider: str = "smtp",
    host: str = "",
    port: int = 465,
    use_ssl: bool = True,
    password: str = "",
    enabled: bool = True,
    is_default: bool = False,
    note: str = "",
) -> dict[str, Any]:
    """新增或更新一个授权发件邮箱。"""
    address = (address or "").strip().lower()
    if not address:
        return {"ok": False, "error": "发件邮箱不能为空"}
    if not auth.valid_email(address):
        return {"ok": False, "error": "发件邮箱格式不正确"}
    provider = (provider or "smtp").strip().lower()
    if provider not in ("smtp", "dev"):
        return {"ok": False, "error": "暂仅支持 smtp / dev 两种发信方式"}
    host = (host or "").strip()
    if provider == "smtp" and not host:
        return {"ok": False, "error": "SMTP 服务器地址不能为空"}
    try:
        port = int(port or 465)
    except (TypeError, ValueError):
        return {"ok": False, "error": "端口必须是数字"}
    password = password or ""
    now = int(time.time())
    with store._lock, store.db() as conn:
        if is_default:
            conn.execute("UPDATE mail_accounts SET is_default=0")
        if account_id:
            sets = [
                "address=?", "provider=?", "host=?", "port=?", "use_ssl=?",
                "enabled=?", "is_default=?", "note=?",
            ]
            vals: list[Any] = [
                address, provider, host, port, 1 if use_ssl else 0,
                1 if enabled else 0, 1 if is_default else 0, (note or "").strip()[:100],
            ]
            if password:
                sets.append("password=?")
                vals.append(password)
            vals.append(account_id)
            try:
                conn.execute(f"UPDATE mail_accounts SET {', '.join(sets)} WHERE id=?", vals)
            except sqlite3.IntegrityError:
                return {"ok": False, "error": "该发件邮箱已存在"}
        else:
            try:
                conn.execute(
                    "INSERT INTO mail_accounts(address,provider,host,port,use_ssl,password,"
                    "enabled,is_default,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        address, provider, host, port, 1 if use_ssl else 0, password,
                        1 if enabled else 0, 1 if is_default else 0,
                        (note or "").strip()[:100], now,
                    ),
                )
            except sqlite3.IntegrityError:
                return {"ok": False, "error": "该发件邮箱已存在"}
        # 若没有默认账号，自动把第一个启用的设为默认
        has_default = conn.execute(
            "SELECT COUNT(*) AS c FROM mail_accounts WHERE is_default=1"
        ).fetchone()["c"]
        if not has_default:
            conn.execute(
                "UPDATE mail_accounts SET is_default=1 WHERE id=("
                "SELECT id FROM mail_accounts WHERE enabled=1 ORDER BY id ASC LIMIT 1)"
            )
    return {"ok": True, "message": "已保存", "mailAccounts": list_mail_accounts()}


def delete_mail_account(account_id: int) -> dict[str, Any]:
    with store._lock, store.db() as conn:
        conn.execute("DELETE FROM mail_accounts WHERE id=?", (account_id,))
        has_default = conn.execute(
            "SELECT COUNT(*) AS c FROM mail_accounts WHERE is_default=1"
        ).fetchone()["c"]
        if not has_default:
            conn.execute(
                "UPDATE mail_accounts SET is_default=1 WHERE id=("
                "SELECT id FROM mail_accounts WHERE enabled=1 ORDER BY id ASC LIMIT 1)"
            )
    return {"ok": True, "message": "已删除", "mailAccounts": list_mail_accounts()}


def set_default_mail_account(account_id: int) -> dict[str, Any]:
    with store._lock, store.db() as conn:
        conn.execute("UPDATE mail_accounts SET is_default=0")
        conn.execute("UPDATE mail_accounts SET is_default=1 WHERE id=?", (account_id,))
    return {"ok": True, "message": "已设为默认发件邮箱", "mailAccounts": list_mail_accounts()}
