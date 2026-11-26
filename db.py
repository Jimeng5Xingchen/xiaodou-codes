"""SQLite 存储层：连接、建表、系统设置读写。

用户表在原有基础上扩展了管理员与治理字段：
- role       角色：user / admin
- status     账号状态：active / banned
- group_id   所属用户组（0 表示未分组）
- ban_reason 封禁原因

新增表：
- groups          用户组（可自定义分类）
- settings        键值型系统设置（邮箱白名单、特殊符号开关、初始化标记等）
- mail_accounts   系统授权发件邮箱（多账号，可指定默认发件人）
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "users.sqlite3")

_lock = threading.RLock()


@contextmanager
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """建表 + 兼容旧库补列。幂等，可重复调用。"""
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                username TEXT DEFAULT '',
                bio TEXT DEFAULT '',
                gender TEXT DEFAULT '',
                password_hash TEXT DEFAULT '',
                password_salt TEXT DEFAULT '',
                created_at INTEGER NOT NULL,
                last_login INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS codes(
                email TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                sent_at INTEGER NOT NULL,
                attempts INTEGER DEFAULT 0,
                purpose TEXT DEFAULT 'login'
            );
            CREATE TABLE IF NOT EXISTS sessions(
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS submissions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                source TEXT DEFAULT '',
                created_at INTEGER NOT NULL,
                UNIQUE(code, user_id)
            );
            CREATE TABLE IF NOT EXISTS groups(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                description TEXT DEFAULT '',
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS mail_accounts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT UNIQUE NOT NULL,
                provider TEXT DEFAULT 'smtp',
                host TEXT DEFAULT '',
                port INTEGER DEFAULT 465,
                use_ssl INTEGER DEFAULT 1,
                password TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                is_default INTEGER DEFAULT 0,
                note TEXT DEFAULT '',
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            """
        )
        # 兼容旧库：补齐后加的资料列 / 治理列
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        for col, ddl in (
            ("username", "TEXT DEFAULT ''"),
            ("bio", "TEXT DEFAULT ''"),
            ("gender", "TEXT DEFAULT ''"),
            ("password_hash", "TEXT DEFAULT ''"),
            ("password_salt", "TEXT DEFAULT ''"),
            ("role", "TEXT DEFAULT 'user'"),
            ("status", "TEXT DEFAULT 'active'"),
            ("group_id", "INTEGER DEFAULT 0"),
            ("ban_reason", "TEXT DEFAULT ''"),
            ("banned_at", "INTEGER DEFAULT 0"),
        ):
            if col not in cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        code_cols = {row["name"] for row in conn.execute("PRAGMA table_info(codes)")}
        if "purpose" not in code_cols:
            conn.execute("ALTER TABLE codes ADD COLUMN purpose TEXT DEFAULT 'login'")
        # 索引必须在补列之后创建（旧库新增列才有 group_id）
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_group ON users(group_id)")


# ---------- 系统设置（键值） ----------

def get_setting(key: str, default: str = "") -> str:
    try:
        with db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row and row["value"] is not None else default
    except sqlite3.Error:
        return default


def set_setting(key: str, value: str) -> None:
    with _lock, db() as conn:
        set_setting_conn(conn, key, value)


def set_setting_conn(conn, key: str, value: str) -> None:
    """在已有连接上写设置（避免嵌套开连接导致 SQLite 锁等待）。"""
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def get_setting_json(key: str, default: Any = None) -> Any:
    raw = get_setting(key, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def set_setting_json(key: str, value: Any) -> None:
    set_setting(key, json.dumps(value, ensure_ascii=False))


def all_settings() -> dict[str, str]:
    try:
        with db() as conn:
            rows = conn.execute("SELECT key,value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    except sqlite3.Error:
        return {}
