#!/usr/bin/env python3
"""构建 GitHub Pages 站点产物。

产物内容
--------
1. 前端静态资源（index.html / css / js / img）
2. 最新采集快照 data/codes.json —— 供纯静态访问时展示邀请码列表
3. data/meta.json —— 记录快照时间、数据来源、后端 API 地址

站点在浏览器端的取数优先级：
  1) ?api=<后端地址>  显式指定实时 API
  2) 站内 config.json 指定的实时 API（构建时注入）
  3) data/codes.json  静态快照（离线兜底，GitHub Pages 核心场景）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
STATIC = os.path.join(REPO, "static")
CACHE = os.path.join(REPO, "data", "codes.json")

MARKER = "const __XD_API_BASE__ = '';"
CONFIG_MARKER = "const __XD_STATIC__ = false;"
SHARE_MARKER = "const __XD_SHARECODE__ = '';"


def build(out_dir: str, api_base: str = "", snapshot: str | None = None,
          sharecode: str = "") -> None:
    out_dir = os.path.abspath(out_dir)
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    # 1) 复制静态资源
    for name in ("css", "js", "img"):
        src = os.path.join(STATIC, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(out_dir, name))
    for name in ("index.html",):
        src = os.path.join(STATIC, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(out_dir, name))

    html_path = os.path.join(out_dir, "index.html")
    html = open(html_path, encoding="utf-8").read()

    # 2) 注入实时 API 基址（可为空 = 纯静态）
    if MARKER not in html:
        raise SystemExit("找不到 __XD_API_BASE__ 注入点，static/index.html 可能已被改动")
    html = html.replace(MARKER, "const __XD_API_BASE__ = %s;" % json.dumps(api_base), 1)

    # 3) 注入静态模式标记：告诉前端「本部署带快照」
    has_snapshot = False
    snap_path = snapshot or CACHE
    if os.path.isfile(snap_path):
        os.makedirs(os.path.join(out_dir, "data"), exist_ok=True)
        shutil.copy2(snap_path, os.path.join(out_dir, "data", "codes.json"))
        has_snapshot = True
    if CONFIG_MARKER not in html:
        raise SystemExit("找不到 __XD_STATIC__ 注入点，static/index.html 可能已被改动")
    html = html.replace(
        CONFIG_MARKER, "const __XD_STATIC__ = %s;" % ("true" if has_snapshot else "false"), 1
    )

    # 3.5) 注入跨域 sharecode
    if SHARE_MARKER not in html:
        raise SystemExit("找不到 __XD_SHARECODE__ 注入点，static/index.html 可能已被改动")
    html = html.replace(SHARE_MARKER, "const __XD_SHARECODE__ = %s;" % json.dumps(sharecode), 1)
    open(html_path, "w", encoding="utf-8").write(html)

    # 4) 写入 config.js（运行时可覆盖 API 地址，无需重新构建）
    cfg = {"api": api_base, "sharecode": sharecode, "static": has_snapshot}
    with open(os.path.join(out_dir, "config.js"), "w", encoding="utf-8") as fh:
        fh.write("// 运行时配置：可直接改这个文件调整后端地址，无需重新构建\n")
        fh.write("window.__XD_CONFIG__ = %s;\n" % json.dumps(cfg, ensure_ascii=False, indent=2))

    # 5) meta.json：展示快照时间
    meta = {"builtAt": "", "api": api_base}
    try:
        snap = json.load(open(snap_path, encoding="utf-8"))
        meta["lastUpdated"] = snap.get("lastUpdated", "")
        meta["total"] = (snap.get("counts") or {}).get("total", 0)
    except Exception:  # noqa: BLE001
        pass
    with open(os.path.join(out_dir, "data", "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)

    # 6) GitHub Pages 路由回退 + 安全响应头
    with open(os.path.join(out_dir, "_redirects"), "w", encoding="utf-8") as fh:
        fh.write("/*    /index.html   200\n")

    with open(os.path.join(out_dir, "_headers"), "w", encoding="utf-8") as fh:
        fh.write(
            "/*\n"
            "  X-Content-Type-Options: nosniff\n"
            "  Referrer-Policy: strict-origin-when-cross-origin\n"
            "  X-Frame-Options: SAMEORIGIN\n"
            "  Permissions-Policy: geolocation=(), microphone=(), camera=()\n"
            "\n"
            "/data/*\n"
            "  Cache-Control: public, max-age=300\n"
        )

    # 7) .nojekyll：让 GitHub Pages 原样发布 css/js/img 目录
    open(os.path.join(out_dir, ".nojekyll"), "w").close()

    print("构建完成:", out_dir)
    print("  实时 API:", api_base or "(纯静态快照)")
    print("  静态快照:", "已包含" if has_snapshot else "无")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(REPO, "site"))
    ap.add_argument("--api", default=os.environ.get("XD_API_BASE", ""))
    ap.add_argument("--sharecode", default=os.environ.get("XD_SHARECODE", ""))
    ap.add_argument("--snapshot", default=None)
    args = ap.parse_args()
    build(args.out, args.api, args.snapshot, args.sharecode)
    sys.exit(0)
