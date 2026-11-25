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


def provider() -> str:
    return PROVIDER


def dev_mode() -> bool:
    """是否由前端直显验证码。"""
    return PROVIDER == "dev"


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

    if PROVIDER == "dev":
        return {"ok": True, "provider": "dev", "dev_code": code}

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
