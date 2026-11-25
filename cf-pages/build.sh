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

# 复制静态资源（index.html + css/ + js/ + img/）
cp "$HERE/../static/index.html" "$OUT/index.html"
mkdir -p "$OUT/css" "$OUT/js" "$OUT/img"
cp "$HERE/../static/css/"*.css "$OUT/css/"
cp "$HERE/../static/js/"*.js "$OUT/js/"
cp "$HERE/../static/img/"*.png "$OUT/img/"

# 静态页面：把后端地址注入 __XD_API_BASE__ / __XD_SHARECODE__ 占位符
# 统一复用 scripts/build_pages.py（与 GitHub Pages 部署链同一套产物逻辑）
SHARE="${2:-${XD_SHARECODE:-}}"
API="$API" XD_SHARECODE="$SHARE" python3 "$HERE/../scripts/build_pages.py" \
  --out "$OUT" --api "$API" --sharecode "$SHARE"

# Cloudflare Pages 的响应头（补充 build_pages.py 生成的通用安全头）
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
/img/*
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
