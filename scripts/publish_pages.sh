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
cp "$ROOT/scripts/pages-site.yml" "$BUILD/.github/workflows/deploy.yml"

# 2) 准备工作目录：优先克隆远端历史，保留 Pages 配置
rm -rf "$WORK"
FORCE_FLAG=""
CLONED=0
for attempt in 1 2 3; do
  if git clone -q --depth 1 "https://${TOKEN}@github.com/${REPO}.git" "$WORK" 2>/dev/null; then
    echo "已克隆远端历史（第 ${attempt} 次尝试）"
    CLONED=1
    break
  fi
  echo "克隆失败（第 ${attempt} 次尝试），稍后重试"
  rm -rf "$WORK"
  sleep 8
done
if [ "$CLONED" -eq 0 ]; then
  echo "远端克隆持续失败（网络波动 / GitHub 故障），初始化新仓库"
  rm -rf "$WORK"
  mkdir -p "$WORK"
  git -C "$WORK" init -q
  # 空仓库无法 checkout -b，先建一个空提交作为基线
  git -C "$WORK" -c user.name=wb -c user.email=wb@local commit -q --allow-empty -m "init"
  git -C "$WORK" branch -M main
  # 新仓库没有历史，用强制推送覆盖
  FORCE_FLAG="-f"
fi

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

# 推送带重试（GitHub 网络波动 / 事故期间容易失败）
PUSHED=0
for attempt in 1 2 3 4; do
  if git push ${FORCE_FLAG} origin HEAD:main 2>/dev/null; then
    echo "已推送到 ${REPO}（第 ${attempt} 次尝试）"
    PUSHED=1
    break
  fi
  echo "推送失败（第 ${attempt} 次尝试），稍后重试"
  sleep 10
done
if [ "$PUSHED" -eq 0 ]; then
  echo "❌ 推送失败：GitHub 服务可能正在故障，请稍后重跑工作流"
  exit 1
fi

# 说明：用 PAT 推送会正常触发站点仓的 push 工作流（GitHub 仅阻止 PAT 触发自身所在仓库的
# 某些场景），因此这里不再额外 dispatch，避免与 push 事件并发抢占 Pages 部署槽位。
echo "站点仓 push 事件将自动触发 Pages 发布"
