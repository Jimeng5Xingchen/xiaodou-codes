/* Cloudflare Pages 部署说明 */

## 一句话结论

**可以，但只能把「前端」放上去，后端不能一起搬。**

Cloudflare Pages = 静态托管 + Pages Functions（跑在 Workers 运行时上，只支持 JS/TS）。
本站是 FastAPI + SQLite + 后台常驻线程 + 线程池，**整体迁移到 Pages 需要重写大半**，不划算。

推荐架构：**前端放 CF Pages（全球 CDN、静态流量免费无限），后端留在现有 Python 服务。**
后端已加 CORS 放行，前端支持 `?api=` 指定后端地址，跨域链路是通的。

---

## 部署步骤

```bash
cd cf-pages

# 1) 构建（后端地址留空 = 同源部署）
./build.sh

# 2) 部署
npx wrangler pages deploy dist --project-name=xiaodou-codes
```

首次使用需先登录：

```bash
npx wrangler login
```

部署完会得到 `https://xiaodou-codes.pages.dev` 之类的地址。

### 自定义域名

Workers & Pages → 你的项目 → Custom domains → 添加域名。
CNAME 记录指向 `<项目名>.pages.dev`，然后在域名注册商处添加这条 CNAME。

### 前端在 CF Pages、后端在别处

```bash
./build.sh https://your-backend.example.com
```

构建脚本会生成 `dist/config.js` 写入后端地址，前端自动跨域调用。
也可用 URL 参数临时切换后端（调试用）：

```
https://xiaodou-codes.pages.dev/?api=https://your-backend.example.com
```

---

## 为什么后端不能一起搬到 Pages

| 本站现状 | CF Pages / Workers 限制 | 后果 |
|---|---|---|
| `ThreadPoolExecutor` × 11 | Pyodide/WASM 无线程、无 multiprocessing | 11 处线程池全部改 asyncio |
| `threading.Thread` 后台调度器（每 180s 抓取） | Worker 无常驻进程 | 改 Cron Trigger，且 **Pages 无 Cron Trigger**，必须另建一个 Worker 定时 POST 回来 |
| SQLite 文件 `data/users.sqlite3` | 无文件系统 | 换 D1（SQLite 兼容），Python 侧要走 `env.DB.prepare()`，不能直接 `sqlite3.connect()` |
| `data/codes.json`、`data/submitted.json` | 无文件系统 | 换 KV / R2 / D1 |
| 网易 SMTP 发验证码（465） | Worker 可用 TCP socket 发 465（**Pages Functions 不行**），另需 `nodejs_compat` | 换 HTTP 邮件 API 更省事 |
| `requests`/`urllib` 同步抓取 | 顶层 `requests.get()` 在 Workers 里会卡死 | 全部改 `httpx`/async |
| 每 25 秒自动刷新 + 400 个候选码官方校验 | 免费版 **10 万请求/天**、**CPU 10ms/request**、子请求 50 个 | 免费版扛不住，需 Workers 付费（$5/月起） |

结论：想彻底搬到 CF，**应该做 Workers（不是 Pages）+ D1 + Cron Worker + 付费版**，
工作量约等于重写 `collector.py`（1102 行）+ `auth.py`（563 行）+ `app.py`（606 行）。

---

## 额度参考（2026-11 官方数据）

**Workers（Pages Functions 共用同一份额度）**

| 项目 | 免费版 | 付费版（$5/月起） |
|---|---|---|
| 请求数 | 10 万/天 | 1000 万/月，超出 $0.30/百万 |
| CPU / 请求 | **10 ms** | 30 秒（可调至 5 分钟） |
| 子请求 / 请求 | 50 | 10000 |
| 内存 | 128 MB | 128 MB |
| Cron Trigger 数 | 5 | 250 |

**D1（免费版）**：10 个库/账号，单库 500 MB，5M 行读/天，10 万行写/天，50 次查询/Worker 调用。

**KV（免费版）**：10 万读/天，但只有 **1000 次写/天** —— 存 25 秒刷新的数据会瞬间写爆，
若走 Workers 方案应改用 R2 或 D1。

**静态资源本身免费无限** —— 这正是把前端放 Pages 的最大好处。

---

## 注意事项

1. **前端每 25 秒轮询一次**。若前端放 Pages、后端在别处，Pages 静态资源请求免费，
   但后端那侧的请求仍按后端自己的计费算，与本方案无关。
2. **跨域需要 `X-User-Token` 放行**。本站登录态走自定义头（平台网关会覆盖 `Authorization`），
   自定义头会触发浏览器预检，`app.py` 已把 `allow_headers` 设为 `*`，登录态跨域可用。
3. **后端 CORS**：`app.py` 已内置 `CORSMiddleware`（`allow_origins=["*"]`）。
   本站无敏感 Cookie 凭证（登录态纯靠 token 头），故 `allow_credentials=False` 是安全的。
4. **抓取频率**：豆瓣等站点对数据中心 IP 风控严格。CF 出口 IP 段被风控的概率更高，
   建议保留原有的「豆瓣 15 分钟缓存 + 失败降级」策略，不要为了「实时」而调高频率。
