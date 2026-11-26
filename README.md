# 小豆邀请码抓码站

聚合公开分享的小豆（豆包旗下 AI 个人助理）邀请码，并用**豆包官方邀请页接口**逐个校验有效性。

## 数据来源

| 来源 | 说明 |
|---|---|
| B 站搜索 | 多关键词搜索 B 站，提取标题中的邀请码 |
| B 站评论区 | 抓取小豆相关视频的评论，UP 主与观众互贴的新码主要出自这里 |
| 社区共享 JSON | 若干公开抓码站发布的数据快照（X / 小红书渠道） |
| 社区公开搜索页 | 微博 / 知乎 / 贴吧 / 微信公众号 / 小红书 / 抖音，抓到的候选一律经官方接口前置校验，只有真码入库 |
| 豆瓣小组 | 讨论搜索（`cat=1013`）+ 话题详情页正文与评论区，候选经官方接口前置校验；自带长 TTL 缓存应对豆瓣限流 |
| 豆包官方接口 | `activity.doubao.com/alice/ug/campaign/onboarding/invite_code/info/` 校验码是否存在及过期时间 |

### 多源命中与回复评论
同一邀请码被多个来源命中时，记录会汇总所有命中来源（`hits` 字段）及各自回复评论，前端点击「N 源命中 / N 条回复」即可查看逐条来源、直达原文并展开回复内容。

> 微博、知乎、贴吧、微信公众号、小红书、抖音等多需登录态，匿名抓取通常拿不到评论正文。
> 对这些平台，页面底部提供「去社区找码 / 分享」一键直达入口，用户可前往平台搜索最新分享；
> 站内采集部分做了官方接口前置校验，即使抓到页面里的脚本哈希等噪音串也不会污染结果。

## 状态含义

- **官方校验有效** —— 官方接口返回该码存在且未过期（最可靠）
- **待确认** —— 社区标注可能可用，但官方接口未识别（码可能已被消耗或不属于小豆）
- **社区反馈已用** —— 社区来源已反馈该码被使用
- **已过期** —— 官方接口显示已过期

## 本地运行

```bash
pip install -r requirements.txt
PORT=8080 python3 app.py
# 打开 http://127.0.0.1:8080
```

服务端每 180 秒采集一次（`REFRESH_SECONDS` 可调），页面每 20 秒自动拉取最新结果。

## 接口

- `GET /` 抓码站页面
- `GET /api/codes` 采集结果 JSON
- `GET /api/health` 健康检查
- `GET /api/refresh` 手动触发一次采集（异步）
- `POST /api/submit` 提交候选码，逐个经官方接口校验，只收录真实有效码

## 用户系统（邮箱验证码登录）

支持邮箱验证码注册/登录，无需密码。登录后提交的码记录在本人账号下。
数据存 SQLite（`data/users.sqlite3`），零额外依赖。

**邮件通道可插拔**：优先读管理端登记的数据库发件账号，其次环境变量，最后兼容项目根目录 `mail_config.json`（该文件已加入 `.gitignore`，不会进入仓库）。

| Provider | 说明 |
|---|---|
| `smtp`（当前默认） | 网易邮箱 `smtp.yeah.net:465`（SSL），授权码在「管理 → 系统邮箱授权」中配置并存入数据库，已实测发信成功 |
| `dev` | 不发真实邮件，验证码在页面上直接显示，仅用于演示/测试 |
| `temporam` | 用 Temporam `/v3/messages` 发信，需 Pro 及以上套餐 + 已验证的 `from` 邮箱 |

当前发件账号在管理后台「系统邮箱授权」中配置，SMTP `smtp.yeah.net:465`，使用邮箱授权码登录；
仓库不保存任何发件账号或授权码。所有用户都能收到真实验证码邮件，不再在页面直显验证码。

需要用环境变量覆盖时（例如换邮箱）：

```bash
MAIL_PROVIDER=smtp SMTP_HOST=smtp.qq.com SMTP_PORT=465 SMTP_USER=you@qq.com \
SMTP_PASS=授权码 SMTP_FROM=you@qq.com python3 app.py
```

> 注意：`mail_config.json` 含邮箱授权码，等同账号密码，请勿公开提交或分享该文件。
> 之前提供的 Temporam Token 为 `hobby` 套餐，`outbound.limit=0`、发信返回 403
> `sender_not_allowed`，无法作为发件邮箱，故改用网易 SMTP。

## 管理后台（用户治理 / 系统配置）

### 初始部署

首次访问时前端会弹出「初始化部署」引导（`GET /api/setup/state` 返回 `initialized: false`）。
填写 **登录账号（邮箱）**、**绑定邮箱**、**登录密码** 后提交 `POST /api/setup/init`，
系统创建第一个 `role=admin` 的账号并直接签发登录 token。初始化完成后该接口自动关闭，无法重复执行。

```bash
# 查询初始化状态
curl http://127.0.0.1:8080/api/setup/state
# {"ok":true,"initialized":false,"mailReady":true,"provider":"smtp"}
```

### 管理页

管理员登录后导航栏出现「管理」入口（`static/js/admin.js`，样式 `static/css/admin.css`），包含四块：

| 模块 | 能力 | 对应接口 |
|---|---|---|
| **系统邮箱授权** | 登记多个 SMTP 发件邮箱（地址 / 服务器 / 端口 / SSL / 授权码），可设默认发件人、启停、测试发信 | `/api/admin/mail-accounts` |
| **注册策略** | 是否开放新用户注册、是否允许含特殊符号的邮箱、邮箱白名单（完整邮箱或 `@域名`） | `/api/admin/settings` |
| **用户组** | 自定义分组（如「内测用户」），组内成员可批量筛选；删除分组时成员回到「未分组」 | `/api/admin/groups` |
| **用户管理** | 搜索 / 按分组 / 状态筛选用户，改分类、封号解封、重置密码、设为管理员、删除账号 | `/api/admin/users` |

**安全护栏**（`admin.py`）：

- 不能封禁 / 删除 / 降级自己
- 系统始终保留至少一个可用管理员
- 封禁后立即清除该用户的全部登录态
- 邮箱白名单只约束「尚未注册」的邮箱，不会把已有用户挡在门外
- 所有管理接口校验 `role=admin`，非管理员返回 403

### 数据表

| 表 | 用途 |
|---|---|
| `users` | 原有字段 + `role` / `status` / `group_id` / `ban_reason` / `banned_at` |
| `groups` | 用户组 |
| `settings` | 键值型系统设置（`setup_done` / `email_whitelist` / `allow_special_email` / `registration_enabled` 等） |
| `mail_accounts` | 系统授权发件邮箱 |
| `mcp_tokens` | AI 客户端连接令牌（只存 SHA-256 摘要，可过期 / 可撤销） |
| `mcp_clients` | 已注册的 MCP 客户端（授权来源与回调地址） |
| `mcp_auth_codes` | 浏览器远端授权的一次性授权码（OAuth 2.1 风格 + PKCE） |

表结构集中在 `db.py`，启动时 `db.init_db()` 幂等建表并为旧库补列。

发信时优先使用管理端登记的授权账号（默认账号优先），其次回退到 `mail_config.json` / 环境变量。

## 用户提交（不上传 Cookie）

站点不收集任何账号 Cookie / 登录凭证。用户提交有两种方式：

1. **手动粘贴**：把社区找到的码贴进页面输入框，本站调用官方接口校验后收录。
2. **抓取书签**：把页面提供的书签拖到书签栏，在自己已登录的社区页面点击它，
   书签脚本只读取当前页面公开文字中的候选码并 POST 回本站；Cookie 始终留在用户浏览器内。

用户提交的码持久化在 `data/submitted.json`，重启后自动恢复并并入采集结果。

## MCP 服务（AI 连接）

后端内置一个 Model Context Protocol 服务端（JSON-RPC 2.0 / Streamable HTTP，零第三方依赖），
让 AI 客户端（Claude Desktop / Cursor / Cherry Studio / Codex 等）直接接入。

### 端点

| 路径 | 方法 | 说明 |
|---|---|---|
| `/mcp` | POST | MCP 主端点：`initialize` / `tools/list` / `tools/call` / `resources/*` |
| `/mcp` | GET | 返回 405（本服务为无状态实现，无服务端主动推送通道） |
| `/mcp/authorize` | GET | 浏览器授权确认页（显示客户端名称、申请权限、回调地址） |
| `/mcp/authorize` | POST | 授权页表单提交 → 302 回跳 `redirect_uri?code=…&state=…` |
| `/mcp/authorize/approve` | POST | JSON 接口，站内按钮直接换取回调地址 |
| `/mcp/token` | POST | 令牌端点：`authorization_code` + PKCE → `access_token` |
| `/mcp/register` | POST | 动态客户端注册（RFC 7591 风格） |
| `/.well-known/oauth-authorization-server` | GET | OAuth 元数据，供客户端自动发现授权流程 |
| `/.well-known/oauth-protected-resource` | GET | 受保护资源元数据 |

用户级接口（需登录态）：`GET /api/mcp/config`、`POST /api/mcp/tokens`、
`DELETE /api/mcp/tokens/{id}`、`POST /api/mcp/tokens/{id}/rotate`、
`GET /api/mcp/clients`、`DELETE /api/mcp/clients/{client_id}`、`POST /api/mcp/revoke-all`。

### 鉴权

令牌解析顺序：`X-MCP-Token` 头 → `Authorization: Bearer` → `?access_token=`。
把 `X-MCP-Token` 放在首位是因为部分发布平台网关会强制改写 `Authorization` 头，自定义头不会被篡改。

客户端配置示例（Cursor / Claude Desktop / Cherry Studio 通用格式）：

```json
{
  "mcpServers": {
    "小豆邀请码": {
      "type": "http",
      "url": "https://<后端地址>/mcp",
      "headers": { "X-MCP-Token": "<连接 Token>" }
    }
  }
}
```

### 权限范围（scope）

| scope | 含义 |
|---|---|
| `codes:read` | 读取邀请码（搜索、详情、统计、社区入口） |
| `codes:write` | 发布邀请码与邀请码链接 |
| `account:read` | 读取你的账号资料与分享记录 |
| `tokens:manage` | 管理连接令牌（查询 / 新建 / 撤销） |

### 工具清单

`search_codes`（搜索）、`get_code_detail`（详情 + 回复评论）、`submit_code`（发布邀请码）、
`submit_link`（发布邀请码链接）、`list_my_shares`、`get_my_profile`、`get_site_stats`、
`list_community_links`、`refresh_codes`（仅管理员）、`list_access_tokens`、
`create_access_token`、`revoke_access_token`。

### 浏览器远端授权流程

```
AI 客户端 → 打开浏览器 GET /mcp/authorize?client_id=…&redirect_uri=…
            → 用户在授权页确认（显示客户端名称与所申请权限）
            → 302 回跳 redirect_uri?code=…&state=…
            → AI 客户端 POST /mcp/token（code + code_verifier）
            → 返回 access_token → 之后用 X-MCP-Token 调用 /mcp
```

授权码一次性、10 分钟过期，全程支持 PKCE（S256 / plain）。
回调地址会做校验（已登记客户端必须匹配，未登记客户端仅允许 https / localhost / 自定义 scheme），防开放重定向。

令牌明文只在创建时返回一次，服务端仅保存 SHA-256 摘要；用户可在「我的账号 → AI / MCP 授权」随时单独撤销某个客户端。

## 前端结构（UI 分层）

样式与外观逻辑已从 `index.html` 拆出，便于单独调整材质而不动业务逻辑：

```
static/
├── index.html          页面结构 + 业务脚本
├── css/
│   ├── tokens.css      设计令牌：色板 / 形状 / 玻璃变量 / Material You 派生色
│   ├── base.css        布局与组件基础样式
│   ├── glass.css       毛玻璃 · 水晶胶囊材质层（data-glass / data-capsule）
│   ├── material.css    安卓 11 / Material You 层（data-md）
│   ├── ui-panel.css    外观设置面板的开关 / 滑块 / 色板控件
│   ├── admin.css       管理后台样式
│   ├── mcp.css         「我的账号」页 AI / MCP 授权区块样式
│   └── responsive.css  断点适配：移动端 / 平板 / PC 宽屏 / 打印
└── js/
    ├── ui-control.js   UI 控制组件（window.XDUI）
    ├── admin.js        管理后台模块（window.XDAdmin）
    └── mcp.js          AI / MCP 授权模块（window.XDMCP）
```

### AI / MCP 授权模块

「我的账号」页只放一个紧凑的「AI / MCP 授权」按钮（带已连接状态小圆点），点击后在弹窗内完成全部操作；
弹窗支持点遮罩 / 关闭按钮 / Esc 关闭，内容区独立滚动，兼容浅色、深色、毛玻璃与安卓11主题。

`static/js/mcp.js`（`window.XDMCP`）负责这个弹窗的 AI 接入：

- 展示**连接地址**与**连接 Token**（明文保存在本机浏览器 `xd_mcp_creds_v1`，按账号隔离）；
- 展示**免配置链接**：`.../mcp/t/<token>?sharecode=...`，令牌放在 URL 路径里，供只能填 URL、不能自定义请求头的客户端使用；
  对应的后端端点是 `POST /mcp/t/{access_token}`。注意网关会改写 `Authorization` 头并丢弃 `access_token` 查询参数，
  所以标准端点必须用 `X-MCP-Token` 头，或用这个免配置端点；
- 高级设置里可**自定义连接地址**（保存在本机 `xd_mcp_endpoint_v1`，留空恢复站点配置），连接地址不是硬编码；
- 按客户端单独签发 / 重新生成 / 撤销令牌，可「断开全部」；
- 生成**浏览器远端授权**链接（客户端名称、客户端 ID、回调地址、权限勾选），一键打开授权页；
- 导出 MCP 客户端配置 JSON（`mcpServers` 标准格式，同时给出带请求头的标准版与免配置版）；
- 展示已授权客户端与 AI 可调用的工具清单。

后端对应 `mcp_server.py`（协议与工具）、`mcp_store.py`（令牌 / 客户端 / 授权码存储）、
`mcp_views.py`（服务端渲染的授权页，自包含样式）。

### 外观设置面板

标题右侧「外观」按钮打开，分四组：

| 分组 | 选项 |
|---|---|
| 外观主题 | 跟随系统 / 暗黑 / 亮色（跟随系统会实时响应系统深浅色切换） |
| 外观颜色 | 7 个预设色 + 自定义取色器 |
| 材质与形状 | 毛玻璃水晶效果（开关）· 胶囊形状（开关）· 安卓 11 风格（开关）· 动效（开关） |
| 玻璃微调 | 表面透明度 0–100% · 背景模糊 0–40px · 水晶高光 0–100% · 推荐参数 / 纯色模式 / 全部重置 |

所有状态存在 `localStorage` 的 `xd_ui_v1`，按需增量写入 `xd_theme_v1` / `xd_accent_v1`。

### 实现要点

- **材质层用 `<html>` 上的 `data-glass` / `data-capsule` / `data-md` / `data-motion` 开关**，
  CSS 用 `:where()` 包裹选择器把特异性压到最低，避免各层互相覆盖。
- **毛玻璃**依赖 `backdrop-filter`；不支持时 `@supports` 自动降级为高不透明度纯色。
  `prefers-reduced-transparency: reduce` 下强制关闭模糊。
- **背景光晕**（`.bg-aurora`）提供可折射的彩色底层，毛玻璃才有"玻璃厚度"感；
  关闭毛玻璃时光晕自动减弱。
- **Material You 动态取色**由主题色派生容器色 / on-容器色，按明暗模式分别走
  Material 3 的 tone 90（亮）与 tone 30（暗），保证文字对比度达标。
- **PC 兼容**：悬停增强只在 `@media(hover:hover) and (pointer:fine)` 生效，
  避免触屏点击后残留 hover 位移；键盘可达性用 `:focus-visible` 焦点环；
  滚动条、打印样式单独处理。

## 部署方式

### 方式一：整站部署（当前默认）

```bash
pip install -r requirements.txt
PORT=8080 python3 app.py
```

后端同时托管前端页面与 API，同源部署。

### 方式二：前端托管到 Cloudflare Pages（推荐给「要自定义域名 / 要免费静态流量」的场景）

```bash
cd cf-pages
./build.sh                                   # 同源
./build.sh https://your-backend.example.com # 前端在 Pages，后端在别处
npx wrangler pages deploy dist --project-name=xiaodou-codes
```

后端需放行 CORS —— `app.py` 已内置 `CORSMiddleware`，并显式允许 `X-User-Token`
自定义头（该头会触发浏览器预检，不放行会导致跨域登录态失效）。

前端 API 基址优先级：`?api=` 查询参数 > 构建时注入的 `__XD_API_BASE__` > 同源。
抓取书签的提交地址同样跟随该基址，跨域部署时不会误打到 Pages 静态源。

详细说明与 Cloudflare 额度数据见 [`cf-pages/README.md`](cf-pages/README.md)。

### 方式三：GitHub Pages（单仓库）

公开仓库 `Jimeng5Xingchen/xiaodou-codes` 同时承载源码与 Pages 站点，不再需要第二个公开站点仓。
先把仓库设为 Public，再在 `Settings → Pages → Build and deployment → Source` 选择 **GitHub Actions**。

流程由 `.github/workflows/deploy-pages.yml` 自动完成：

```
定时（每 30 分钟）/ 手动触发 / 推送到 main
  → Actions 拉取源码
  → 执行 collector.run_once() 多源采集（零第三方依赖，urllib + curl）
  → 写入 data/codes.json 快照（采集失败时沿用仓库内基线快照）
  → scripts/build_pages.py 构建站点（注入 API 基址、sharecode、快照）
  → actions/upload-pages-artifact + actions/deploy-pages 发布
```

站点地址：**https://jimeng5xingchen.github.io/xiaodou-codes/**

配置项（仓库级 variable，在 `Settings → Secrets and variables → Actions`）：

| 名称 | 类型 | 值 |
|---|---|---|
| `XD_API_BASE` | variable | 实时后端地址，如 `https://xxx.app.workbuddy.host` |
| `XD_SHARECODE` | variable | 后端所在平台网关的 sharecode |

`PAGES_REPO`、`PAGES_DEPLOY_TOKEN` 已不再需要（官方 `GITHUB_TOKEN` 负责 Pages 部署），可删除。

**前端取数优先级**：`?api=` 参数 > `config.js` 运行时配置 > 构建期注入 > 同源；
实时 API 不可用时自动回退到 `data/codes.json` 静态快照，页面顶部显示「静态快照」提示条，
此时登录 / 注册 / 提交等需要实时后端的功能会自动禁用。

**子路径兼容**：所有静态资源均使用相对路径（`img/logo.png`、`css/tokens.css`），
因此站点放在 `https://<user>.github.io/<repo>/` 子路径下也能正常加载。

> 采集失败不会中断流水线：会沿用上一份快照继续发布，避免站点因外部源临时不可达而空白。

> **不要把整个后端搬到 Pages。** Pages Functions 只跑 JS/TS，且没有 Cron Trigger、
> 没有文件系统、没有线程池（Python Workers 也不行）；本项目的 `ThreadPoolExecutor`
> （11 处）、后台调度线程、SQLite 文件存储都要重写，代价远大于收益。

## 说明

邀请码通常一次性使用，且账号每天验证次数有限，请优先使用标注「官方校验有效」的码。
本工具仅聚合公开可见内容，与豆包 / 字节跳动无任何关联，不提供任何付费码或代激活服务。
