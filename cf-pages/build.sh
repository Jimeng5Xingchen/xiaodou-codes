#!/usr/bin/env bash
# 构建 Cloudflare Pages 静态产物。
#
#   ./build.sh [后端地址]
#
# 后端地址留空则默认同源（页面与 API 同域，无需额外配置）。
# 传入后端地址时会把它注入产物的 index.html，前端自动跨域调用该后端。
#
# 例：
#   ./build.sh                                        # 同源部署
#   ./build.sh https://your-backend.workers.dev        # 前端在 CF Pages，后端在别处
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/dist"
API="${1:-}"

rm -rf "$OUT"
mkdir -p "$OUT"

# 复制静态资源（index.html + css/ + js/）
cp "$HERE/../static/index.html" "$OUT/index.html"
mkdir -p "$OUT/css" "$OUT/js"
cp "$HERE/../static/css/"*.css "$OUT/css/"
cp "$HERE/../static/js/"*.js "$OUT/js/"

# 静态页面：把后端地址注入 __XD_API_BASE__ 占位符
# 用 python 做替换，避免 sed 对特殊字符（/ & 等）的转义问题
API="$API" python3 - "$HERE/../static/index.html" "$OUT/index.html" <<'PY'
import os, sys, json
src, dst = sys.argv[1], sys.argv[2]
api = os.environ.get("API", "").strip()
html = open(src, encoding="utf-8").read()
marker = "const __XD_API_BASE__ = '';"
if marker not in html:
    raise SystemExit("找不到 __XD_API_BASE__ 注入点，请检查 static/index.html 是否被改动")
# 替换成后端地址字面量（JSON 转义保证安全）
html = html.replace(marker, "const __XD_API_BASE__ = %s;" % json.dumps(api), 1)
open(dst, "w", encoding="utf-8").write(html)
print("注入后端地址:", api or "(同源)")
PY

# 安全响应头
cat > "$OUT/_headers" <<'EOF'
/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  X-Frame-Options: SAMEORIGIN
  Permissions-Policy: geolocation=(), microphone=(), camera=()

# CSS / JS 带内容哈希指纹，可长缓存
/css/*
  Cache-Control: public, max-age=604800, immutable
/js/*
  Cache-Control: public, max-age=604800, immutable
EOF

# SPA 回退：未知路径回落到首页
cat > "$OUT/_redirects" <<'EOF'
/*    /index.html   200
EOF

echo "构建完成 -> $OUT"
echo
echo "下一步："
echo "  1) cd $HERE"
echo "  2) npx wrangler pages deploy dist --project-name=xiaodou-codes"
echo "  3) 绑定自定义域名（Workers & Pages -> 你的项目 -> Custom domains）"
if [ -n "$API" ]; then
  echo
  echo "已配置后端地址：$API"
  echo "该后端需放行 CORS（本项目 app.py 已内置）。"
else
  echo
  echo "当前为同源部署。若后端在别处，请带地址重新构建："
  echo "  ./build.sh https://your-backend.workers.dev"
fi
