"""邮件发送层：三种可插拔 provider。

- smtp      : 用标准 SMTP 发信（当前默认，网易邮箱 smtp.yeah.net）
- dev       : 不发真实邮件，验证码由调用方直显
- temporam  : 用 Temporam /v3/messages 发信，需 Pro 及以上套餐 + 已验证的 from 邮箱

配置优先读环境变量，未设置时回退到项目根目录的 mail_config.json，
避免把密钥写死在源码里，又能在发布环境（无法注入环境变量）下生效。
"""

from __future__ import annotations

import json
import os
import smtplib
import urllib.error
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any


def _load_file_config() -> dict[str, str]:
    """从项目根目录的 mail_config.json 读取默认配置。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mail_config.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(k): str(v) for k, v in data.items()}
    except Exception:  # noqa: BLE001
        return {}


_FILE_CONFIG = _load_file_config()


def _conf(key: str, default: str = "") -> str:
    """环境变量优先，其次 mail_config.json，最后默认值。"""
    value = os.environ.get(key)
    if value is None:
        value = _FILE_CONFIG.get(key, default)
    return str(value).strip()


PROVIDER = _conf("MAIL_PROVIDER", "smtp").lower()

# Temporam 配置
TEMPORAM_TOKEN = _conf("TEMPORAM_TOKEN")
TEMPORAM_API = _conf("TEMPORAM_API", "https://api.temporam.com/v3/messages")
TEMPORAM_FROM = _conf("TEMPORAM_FROM")
TEMPORAM_FROM_NAME = _conf("TEMPORAM_FROM_NAME", "小豆抓码站")

# SMTP 配置
SMTP_HOST = _conf("SMTP_HOST")
SMTP_PORT = int(_conf("SMTP_PORT", "465") or "465")
SMTP_USER = _conf("SMTP_USER")
SMTP_PASS = _conf("SMTP_PASS")
SMTP_FROM = _conf("SMTP_FROM", SMTP_USER)
SMTP_FROM_NAME = _conf("SMTP_FROM_NAME", "小豆抓码站")
SMTP_SSL = _conf("SMTP_SSL", "1") != "0"


def test_account(account_id: int, to: str) -> dict[str, Any]:
    """测试某个授权发件邮箱的发信能力。account_id=0 表示用当前生效账号。"""
    to = (to or "").strip()
    if not to:
        return {"ok": False, "error": "请填写测试收件邮箱"}
    account = None
    if account_id:
        try:
            import db as store

            with store.db() as conn:
                row = conn.execute(
                    "SELECT address,provider,host,port,use_ssl,password,enabled,is_default "
                    "FROM mail_accounts WHERE id=?", (account_id,)
                ).fetchone()
            if not row:
                return {"ok": False, "error": "授权邮箱不存在"}
            account = {
                "address": row["address"],
                "provider": row["provider"] or "smtp",
                "host": row["host"] or "",
                "port": int(row["port"] or 465),
                "use_ssl": bool(row["use_ssl"]),
                "password": row["password"] or "",
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"读取失败：{exc}"}
    else:
        account = _effective_account()
    if not account:
        return {"ok": False, "error": "尚未配置可用的发件邮箱"}
    if account.get("provider") == "dev":
        return {"ok": True, "provider": "dev", "message": "当前为 dev 模式，未真实发信"}
    subject = "小豆抓码站 - 发信测试"
    html = (
        "<div style='font-family:-apple-system,Segoe UI,PingFang SC,sans-serif;'>"
        "<p>这是一封来自小豆抓码站的发信测试邮件。</p>"
        f"<p style='color:#888;font-size:13px;'>发件账号：{account.get('address','')}</p>"
        "</div>"
    )
    return _send_smtp(account, to, subject, html)


def provider() -> str:
    return PROVIDER


def dev_mode() -> bool:
    """是否由前端直显验证码。"""
    return provider() == "dev"


def db_accounts() -> list[dict[str, Any]]:
    """读取管理端配置的授权发件邮箱（数据库优先于文件配置）。"""
    try:
        import db as store

        with store.db() as conn:
            rows = conn.execute(
                "SELECT address,provider,host,port,use_ssl,password,enabled,is_default "
                "FROM mail_accounts ORDER BY is_default DESC, id ASC"
            ).fetchall()
        return [
            {
                "address": r["address"],
                "provider": r["provider"] or "smtp",
                "host": r["host"] or "",
                "port": int(r["port"] or 465),
                "use_ssl": bool(r["use_ssl"]),
                "password": r["password"] or "",
                "enabled": bool(r["enabled"]),
                "is_default": bool(r["is_default"]),
            }
            for r in rows
            if r["enabled"]
        ]
    except Exception:  # noqa: BLE001
        return []


def _effective_account() -> dict[str, Any] | None:
    """本次发信使用的账号：优先数据库授权账号，其次文件 / 环境变量配置。"""
    accounts = db_accounts()
    if accounts:
        # 优先默认账号，其次第一个启用的
        for acc in accounts:
            if acc["is_default"]:
                return acc
        return accounts[0]
    if PROVIDER == "dev":
        return {"address": "", "provider": "dev", "host": "", "port": 0, "use_ssl": True,
                "password": "", "enabled": True, "is_default": True}
    if PROVIDER == "smtp" and all([SMTP_HOST, SMTP_USER, SMTP_PASS, SMTP_FROM]):
        return {
            "address": SMTP_FROM,
            "provider": "smtp",
            "host": SMTP_HOST,
            "port": SMTP_PORT,
            "use_ssl": SMTP_SSL,
            "password": SMTP_PASS,
            "enabled": True,
            "is_default": True,
        }
    return None


def _send_smtp(acc: dict[str, Any], to: str, subject: str, html: str) -> dict[str, Any]:
    """按给定账号配置发送一封邮件。"""
    if not all([acc.get("host"), acc.get("address"), acc.get("password")]):
        return {"ok": False, "provider": "smtp", "error": "SMTP 配置不完整"}
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(SMTP_FROM_NAME, "utf-8")), acc["address"]))
    msg["To"] = to
    try:
        if acc.get("use_ssl", True):
            server = smtplib.SMTP_SSL(acc["host"], int(acc.get("port") or 465), timeout=20)
        else:
            server = smtplib.SMTP(acc["host"], int(acc.get("port") or 465), timeout=20)
            server.starttls()
        with server:
            server.login(acc["address"], acc["password"])
            server.sendmail(acc["address"], [to], msg.as_string())
        return {"ok": True, "provider": "smtp", "from": acc["address"]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "provider": "smtp", "error": f"SMTP 发送失败：{exc}"}


def send_verification_code(email: str, code: str, purpose: str = "login") -> dict[str, Any]:
    """发送验证码邮件。返回 dict：{ok, error, provider}。"""
    scenes = {
        "change_email": ("更换邮箱验证码", "更换绑定邮箱"),
        "reset_password": ("重置密码验证码", "重置密码"),
        "set_password": ("设置密码验证码", "设置登录密码"),
    }
    title, scene = scenes.get(purpose, ("邮箱验证码", "登录 / 注册"))
    subject = f"小豆抓码站 - {title}"
    text = (
        f"你的验证码是：{code}\n\n"
        f"用于{scene}，验证码 10 分钟内有效，请尽快填写。\n"
        f"如果不是你本人操作，请忽略此邮件。\n"
    )
    html = (
        "<div style='font-family:-apple-system,Segoe UI,PingFang SC,sans-serif;'>"
        "<p>你的验证码是：</p>"
        f"<div style='font-size:28px;font-weight:700;letter-spacing:4px;color:#d2691e;'>{code}</div>"
        f"<p style='color:#888;font-size:13px;'>用于{scene}，10 分钟内有效。如非本人操作请忽略。</p>"
        "</div>"
    )

    if PROVIDER == "dev" and not db_accounts():
        return {"ok": True, "provider": "dev", "dev_code": code}

    # 管理端配置的授权发件邮箱优先于文件 / 环境变量配置
    account = _effective_account()
    if account and account.get("provider") == "dev":
        return {"ok": True, "provider": "dev", "dev_code": code}
    if account and account.get("provider") == "smtp":
        return _send_smtp(account, email, subject, html)

    if PROVIDER == "temporam":
        if not TEMPORAM_TOKEN:
            return {"ok": False, "provider": "temporam", "error": "未配置 TEMPORAM_TOKEN"}
        if not TEMPORAM_FROM:
            return {"ok": False, "provider": "temporam", "error": "未配置 TEMPORAM_FROM（已验证的发件邮箱）"}
        payload = {
            "from": TEMPORAM_FROM,
            "to": email,
            "subject": subject,
            "text": text,
            "html": html,
            "from_name": TEMPORAM_FROM_NAME,
        }
        try:
            req = urllib.request.Request(
                TEMPORAM_API,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {TEMPORAM_TOKEN}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                status = resp.getcode()
                data = json.loads(resp.read().decode("utf-8"))
            if status == 200 and (data.get("data") or {}).get("status") == "sent":
                return {"ok": True, "provider": "temporam", "id": data["data"].get("id")}
            err = (data.get("error") or {}).get("message") or f"HTTP {status}"
            return {"ok": False, "provider": "temporam", "error": str(err)}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "ignore")
            try:
                parsed = json.loads(body)
                err = (parsed.get("error") or {}).get("message") or body[:120]
            except json.JSONDecodeError:
                err = body[:120]
            hint = "（hobby 套餐不支持发信，需升级套餐并验证发件邮箱）" if exc.code == 403 else ""
            return {"ok": False, "provider": "temporam", "error": f"HTTP {exc.code}: {err} {hint}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "provider": "temporam", "error": f"请求失败：{exc}"}

    if PROVIDER == "smtp":
        if not all([SMTP_HOST, SMTP_USER, SMTP_PASS, SMTP_FROM]):
            return {"ok": False, "provider": "smtp", "error": "SMTP 配置不完整"}
        msg = MIMEText(html, "html", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((str(Header(SMTP_FROM_NAME, "utf-8")), SMTP_FROM))
        msg["To"] = email
        try:
            if SMTP_SSL:
                server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20)
            else:
                server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
                server.starttls()
            with server:
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SMTP_FROM, [email], msg.as_string())
            return {"ok": True, "provider": "smtp"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "provider": "smtp", "error": f"SMTP 发送失败：{exc}"}

    return {"ok": False, "provider": PROVIDER, "error": f"未知 MAIL_PROVIDER：{PROVIDER}"}
