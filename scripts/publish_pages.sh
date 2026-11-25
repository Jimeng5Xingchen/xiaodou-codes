#!/usr/bin/env bash
# 发布站点产物到公开仓库（由 GitHub Actions 调用）
#
# 关键点：
#  1) 克隆远端历史后再覆盖内容 —— 保留提交记录，避免强推重置 Pages 配置
#  2) 站点仓库的 .github/workflows/deploy.yml 必须一起推送 ——
#     否则仓库会退化成纯静态文件，GitHub Pages 失去发布入口
set -euo pipefail

REPO="${PAGES_REPO:-Jimeng5Xingchen/xiaodou-pages}"
TOKEN="${DEPLOY_TOKEN:?未配置 secrets.PAGES_DEPLOY_TOKEN}"

cd "$(dirname "$0")/.."
ROOT="$PWD"

git config --global user.name "github-actions[bot]"
git config --global user.email "github-actions[bot]@users.noreply.github.com"
git config --global http.version HTTP/1.1
git config --global advice.detachedHead false

BUILD="$ROOT/site_build"
WORK="$ROOT/site_work"

# 1) 构建站点产物
python3 scripts/build_pages.py --out "$BUILD"
mkdir -p "$BUILD/.github/workflows"
cp "$ROOT/.github/workflows/pages-site.yml" "$BUILD/.github/workflows/deploy.yml"

# 2) 准备工作目录：优先克隆远端历史，保留 Pages 配置
rm -rf "$WORK"
if git clone -q --depth 1 "https://${TOKEN}@github.com/${REPO}.git" "$WORK" 2>/dev/null; then
  echo "已克隆远端历史"
else
  echo "远端克隆失败（首次部署 / 网络波动），初始化新仓库"
  rm -rf "$WORK"
  mkdir -p "$WORK"
  git -C "$WORK" init -q
  git -C "$WORK" checkout -q -b main
  # 新仓库没有历史，用强制推送覆盖
  FORCE_FLAG="-f"
fi
FORCE_FLAG="${FORCE_FLAG:-}"

# 3) 用新产物覆盖工作目录内容（保留 .git）
find "$WORK" -mindepth 1 -maxdepth 1 ! -name '.git' -exec rm -rf {} +
cp -a "$BUILD/." "$WORK/"

cd "$WORK"
git add -A
if git diff --cached --quiet; then
  echo "站点内容无变化，跳过推送"
  exit 0
fi

git commit -q -m "chore: 更新站点快照 $(date -u +%Y-%m-%dT%H:%M:%SZ)"
git remote remove origin 2>/dev/null || true
git remote add origin "https://${TOKEN}@github.com/${REPO}.git"
git push ${FORCE_FLAG} origin HEAD:main
echo "已推送到 ${REPO}"
