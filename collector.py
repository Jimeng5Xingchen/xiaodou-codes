"""小豆邀请码采集器：多源抓取 + 官方接口校验 + 合并去重。

数据来源
--------
1. B 站搜索页（公开搜索结果，标题/简介中携带邀请码）
2. B 站视频评论区（UP 主与观众在评论区互贴邀请码）
3. 社区共享 JSON 快照（多个上游抓码站公开发布的数据文件）
4. 社区公开搜索页（微博/知乎/贴吧/微信公众号/小红书/抖音，官方接口前置校验）
5. 豆瓣小组（讨论搜索 + 话题详情页评论区，官方接口前置校验）
6. 豆包官方邀请页校验接口（判断邀请码是否真实存在及过期时间）

所有来源均为公开可访问内容，本工具只做聚合与状态标注。
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Iterable

CST = timezone(timedelta(hours=8))
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 邀请码形如 6 位大写字母数字，或官方分享链接里的 M-XXXXXX 形式
CODE_RE = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{6})(?![A-Z0-9])")
OFFICIAL_CODE_RE = re.compile(r"(?<![A-Z0-9])(M-[A-Z0-9]{6})(?![A-Z0-9])")

# 明显不是小豆邀请码的噪音词，避免误采
NOISE = {
    "ABC123", "123ABC", "OPENAI", "STATUS", "NUMBER", "INSULT", "E2SR4Z",
    "ANDROID", "IOSMAC", "SPOTIFY", "YOUTUBE", "TENCENT", "BILIBILI",
    "DOUBLE", "SECOND", "LONDON", "BEIJING", "SHANGHAI", "CHINA12",
    "PLAYER", "MASTER", "SPRING", "WINTER", "SUMMER", "AUTUMN",
    "MONDAY", "FRIDAY", "SUNDAY", "COFFEE", "MUSIC", "MOVIE",
}

# 命中邀请码上下文的关键词，用于判定来源相关度
RELEVANT_HINTS = (
    "小豆", "豆包", "邀请码", "invite", "invite code", "doubao", "xiaodou",
    "字节", "AI 助理", "AI个人助理", "个人助理",
)

SOURCE_LABELS = {
    "bilibili_search": "B站搜索",
    "bilibili_comment": "B站评论",
    "community_json": "社区共享",
    "community_x": "社区共享 · X",
    "community_note": "社区共享 · 小红书",
    "weibo": "微博",
    "zhihu": "知乎",
    "tieba": "贴吧",
    "wechat": "微信公众号",
    "xiaohongshu": "小红书",
    "douyin": "抖音",
    "douban": "豆瓣小组",
    "douban_topic": "豆瓣小组 · 讨论",
    "douban_comment": "豆瓣小组 · 评论",
    "other_community": "社区",
}

# 社区搜索直达链接：既用于采集抓取，也用于前端「去社区找码」入口
# 每项：(source key, 显示名, 搜索 URL 模板 {q}, 页面 Referer)
COMMUNITY_SEARCHES: list[tuple[str, str, str, str]] = [
    ("weibo", "微博", "https://s.weibo.com/weibo?q={q}", "https://s.weibo.com/"),
    ("zhihu", "知乎", "https://www.zhihu.com/search?type=content&q={q}", "https://www.zhihu.com/"),
    ("tieba", "贴吧", "https://tieba.baidu.com/f/search/res?isnew=1&kw={q}", "https://tieba.baidu.com/"),
    ("wechat", "微信公众号", "https://weixin.sogou.com/weixin?type=2&query={q}", "https://weixin.sogou.com/"),
    ("xiaohongshu", "小红书", "https://www.xiaohongshu.com/search_result?keyword={q}", "https://www.xiaohongshu.com/"),
    ("douyin", "抖音", "https://www.douyin.com/search/{q}", "https://www.douyin.com/"),
    ("douban", "豆瓣小组", "https://www.douban.com/group/search?cat=1013&q={q}", "https://www.douban.com/group/"),
]


def now_cst() -> datetime:
    return datetime.now(CST)


def http_get(url: str, *, referer: str | None = None, timeout: int = 20) -> str:
    cmd = ["curl", "-sL", "--max-time", str(timeout), "-A", UA]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    cmd.append(url)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 8)
        return out.stdout or ""
    except subprocess.TimeoutExpired:
        return ""


def http_json(url: str, *, referer: str | None = None, timeout: int = 15) -> Any:
    raw = http_get(url, referer=referer, timeout=timeout)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


@dataclass
class CodeRecord:
    """一条邀请码记录。"""

    code: str
    source: str = "unknown"
    source_label: str = ""
    source_url: str = ""
    author: str = ""
    title: str = ""
    posted_at: str = ""
    posted_at_utc: str = ""
    reply_count: int = 0
    status: str = "unknown"          # available / expired / used / unknown
    status_note: str = ""
    expire_at: int = 0
    verified: bool = False
    first_seen: str = ""
    seen_count: int = 1
    extra: dict = field(default_factory=dict)
    sharer: dict = field(default_factory=dict)
    # 每个命中来源的详情（多源命中时前端可逐个查看，含各自回复评论）
    hits: list = field(default_factory=list)
    # 分类：auto 自动采集 / site 站内分享 / external 站外分享
    category: str = "auto"

    def key(self) -> str:
        return self.code.upper()

    def source_hit(self) -> dict:
        """把本条记录整理成一个命中来源详情。"""
        comments = list(self.extra.get("comments") or [])
        return {
            "source": self.source,
            "label": self.source_label or SOURCE_LABELS.get(self.source, "社区"),
            "url": self.source_url,
            "author": self.author,
            "title": self.title,
            "posted_at": self.posted_at,
            "reply_count": self.reply_count,
            "comments": comments,
            "user_submitted": bool(self.extra.get("user_submitted")),
        }

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("extra", None)
        # 没有分享者信息时不下发空字段
        if not d.get("sharer"):
            d.pop("sharer", None)
        return d


def _is_relevant(text: str) -> bool:
    low = text.lower()
    return any(h.lower() in low for h in RELEVANT_HINTS)


def extract_codes(text: str) -> list[str]:
    """从文本中抽取候选邀请码，先匹配官方 M- 形式，再匹配纯 6 位。"""
    found: list[str] = []
    for m in OFFICIAL_CODE_RE.finditer(text):
        found.append(m.group(1))
    stripped = OFFICIAL_CODE_RE.sub(" ", text)
    for m in CODE_RE.finditer(stripped):
        candidate = m.group(1)
        if candidate in NOISE:
            continue
        if not any(ch.isdigit() for ch in candidate):
            continue
        if not any(ch.isalpha() for ch in candidate):
            continue
        found.append(candidate)
    out: list[str] = []
    for c in found:
        if c not in out:
            out.append(c)
    return out


# --------------------------------------------------------------------------- #
# 源 1：B 站搜索
# --------------------------------------------------------------------------- #

BILI_SEARCH_KEYWORDS = [
    "小豆邀请码",
    "小豆 邀请码",
    "豆包 小豆 邀请码",
    "小豆 邀请码 分享",
    "小豆",
    "小豆 助理",
    "豆包小豆",
    "小豆 体验",
]

# 视频池上限与评论抓取并发度
BILI_VIDEO_POOL = 60
BILI_COMMENT_WORKERS = 10


def fetch_bilibili_video_pool(keyword: str, page: int = 1) -> tuple[list[CodeRecord], dict[str, str]]:
    """抓取搜索页，返回该页的码记录与相关视频池。"""
    url = (
        "https://search.bilibili.com/all?keyword="
        + urllib.parse.quote(keyword)
        + f"&page={page}"
    )
    raw = http_get(url, referer="https://search.bilibili.com/")
    if not raw:
        return [], {}
    records: list[CodeRecord] = []
    pool: dict[str, str] = {}
    pattern = re.compile(
        r'href="//www\.bilibili\.com/video/(BV[0-9A-Za-z]+)/?"[^>]*>\s*'
        r'<h3[^>]*title="((?:[^"\\]|\\.)*)"'
    )
    for match in pattern.finditer(raw):
        bvid, raw_title = match.groups()
        title = html.unescape(re.sub(r"</?em[^>]*>", "", raw_title)).strip()
        if not _is_relevant(title):
            continue
        pool.setdefault(bvid, title)
        for code in extract_codes(title):
            records.append(
                CodeRecord(
                    code=code,
                    source="bilibili_search",
                    source_label=SOURCE_LABELS["bilibili_search"],
                    source_url=f"https://www.bilibili.com/video/{bvid}/",
                    author="B站UP主",
                    title=title,
                )
            )
    return records, pool


# --------------------------------------------------------------------------- #
# 源 2：B 站视频评论区
# --------------------------------------------------------------------------- #

def fetch_bilibili_comments(bvid: str, keyword: str) -> list[CodeRecord]:
    url = (
        "https://api.bilibili.com/x/v2/reply/main"
        f"?type=1&oid={bvid}&mode=3&next=0&ps=20"
    )
    data = http_json(url, referer=f"https://www.bilibili.com/video/{bvid}/")
    if not isinstance(data, dict) or data.get("code") != 0:
        return []
    replies = (data.get("data") or {}).get("replies") or []
    records: list[CodeRecord] = []
    for reply in replies:
        if not isinstance(reply, dict):
            continue
        message = reply.get("content", {}).get("message", "") or ""
        author = (reply.get("member") or {}).get("uname", "") or ""
        rpid = reply.get("rpid")
        ctime = reply.get("ctime")
        posted = ""
        if ctime:
            posted = datetime.fromtimestamp(ctime, CST).strftime("%Y-%m-%d %H:%M:%S")
        reply_count = int(reply.get("rcount", 0) or 0)
        # 有回复的评论，抓一层子回复，供前端「查看回复」展示
        sub_replies: list[dict] = []
        if reply_count > 0 and rpid:
            sub_url = (
                "https://api.bilibili.com/x/v2/reply/reply"
                f"?type=1&oid={bvid}&root={rpid}&ps=20&pn=1"
            )
            sub_data = http_json(sub_url, referer=f"https://www.bilibili.com/video/{bvid}/")
            if isinstance(sub_data, dict) and sub_data.get("code") == 0:
                for sub in (sub_data.get("data") or {}).get("replies") or []:
                    if not isinstance(sub, dict):
                        continue
                    sub_msg = sub.get("content", {}).get("message", "") or ""
                    sub_author = (sub.get("member") or {}).get("uname", "") or ""
                    sub_ctime = sub.get("ctime")
                    sub_posted = (
                        datetime.fromtimestamp(sub_ctime, CST).strftime("%Y-%m-%d %H:%M:%S")
                        if sub_ctime else ""
                    )
                    sub_replies.append({
                        "author": sub_author, "text": sub_msg, "posted_at": sub_posted,
                    })
        for code in extract_codes(message):
            records.append(
                CodeRecord(
                    code=code,
                    source="bilibili_comment",
                    source_label=SOURCE_LABELS["bilibili_comment"],
                    source_url=f"https://www.bilibili.com/video/{bvid}/?replyId={rpid}",
                    author=author,
                    title=message[:120],
                    posted_at=posted,
                    reply_count=reply_count,
                    extra={"comments": [{"author": author, "text": message, "posted_at": posted}] + sub_replies},
                )
            )
    return records


def collect_bilibili() -> list[CodeRecord]:
    """先搜索拿到相关视频，再去评论区找码。"""
    records: list[CodeRecord] = []
    video_pool: dict[str, str] = {}

    def search_job(keyword: str) -> tuple[list[CodeRecord], dict[str, str]]:
        out: list[CodeRecord] = []
        pool: dict[str, str] = {}
        for page in (1, 2):
            try:
                recs, vids = fetch_bilibili_video_pool(keyword, page)
            except Exception:
                continue
            out.extend(recs)
            for bvid, title in vids.items():
                pool.setdefault(bvid, title)
        return out, pool

    with ThreadPoolExecutor(max_workers=4) as pool:
        for chunk, vids in pool.map(search_job, BILI_SEARCH_KEYWORDS):
            records.extend(chunk)
            for bvid, title in vids.items():
                video_pool.setdefault(bvid, title)

    # 视频池按相关度排序后截断，优先抓取小豆相关度高的视频
    ranked = sorted(
        video_pool.items(),
        key=lambda kv: (-sum(h in kv[1] for h in ("小豆", "豆包", "邀请码")), kv[0]),
    )[:BILI_VIDEO_POOL]

    def comment_job(item: tuple[str, str]) -> list[CodeRecord]:
        bvid, _ = item
        try:
            return fetch_bilibili_comments(bvid, "小豆")
        except Exception:
            return []

    if ranked:
        with ThreadPoolExecutor(max_workers=BILI_COMMENT_WORKERS) as pool:
            for chunk in pool.map(comment_job, ranked):
                records.extend(chunk)
    return records


# --------------------------------------------------------------------------- #
# 源 3：社区共享 JSON 快照
# --------------------------------------------------------------------------- #

COMMUNITY_FEEDS = [
    ("https://good1uck.github.io/xiaodou-codes/codes.json", "community_x"),
    ("https://paipiange.github.io/xiaodou-codes/codes.json", "community_x"),
    (
        "https://raw.githubusercontent.com/tranger1234/xiaodou-invite-codes/HEAD/data.json",
        "community_note",
    ),
    (
        "https://raw.githubusercontent.com/CCCcccxxxrrr/xiaodou-codes/HEAD/codes.json",
        "community_x",
    ),
]


def _platform_label(url: str, fallback: str = "社区共享") -> str:
    """根据原文链接域名推断平台，用于来源标签。"""
    if "xiaohongshu.com" in url:
        return "社区共享 · 小红书"
    if "x.com" in url or "twitter.com" in url:
        return "社区共享 · X"
    if "doubao.com" in url:
        return "官方分享页"
    if "bilibili.com" in url:
        return "B站"
    return fallback


def _parse_good_format(data: Any) -> list[CodeRecord]:
    records: list[CodeRecord] = []
    if not isinstance(data, dict):
        return records
    for row in data.get("codes") or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip().upper()
        if not code:
            continue
        url = row.get("postUrl") or row.get("officialUrl") or ""
        records.append(
            CodeRecord(
                code=code,
                source="community_json",
                source_label=_platform_label(url, "社区共享"),
                source_url=url,
                author=row.get("author") or "社区用户",
                title=row.get("note") or "",
                posted_at=row.get("postedAt") or "",
                posted_at_utc=row.get("postedAtUtc") or "",
                reply_count=int(row.get("replyCount", 0) or 0),
                status=row.get("status") or "unknown",
                extra={"upstream_status": row.get("status") or ""},
            )
        )
    return records


def _parse_note_format(data: Any) -> list[CodeRecord]:
    records: list[CodeRecord] = []
    if not isinstance(data, dict):
        return records
    for row in data.get("codes") or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip().upper()
        if not code:
            continue
        found_at = row.get("foundAt")
        posted = ""
        if isinstance(found_at, (int, float)):
            posted = datetime.fromtimestamp(found_at / 1000, CST).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        records.append(
            CodeRecord(
                code=code,
                source="community_json",
                source_label=SOURCE_LABELS["community_note"],
                source_url=row.get("sourceUrl") or "",
                author=row.get("sourceName") or "社区用户",
                title=row.get("snippet") or "",
                posted_at=posted,
                status="unknown",
            )
        )
    return records


def _parse_flat_list(data: Any) -> list[CodeRecord]:
    """解析扁平数组格式的社区数据。

    形如 [{"code":"LK2B9N","platform":"X","publishedAt":"...","replies":0,"url":"..."}]
    """
    records: list[CodeRecord] = []
    if not isinstance(data, list):
        return records
    for row in data:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip().upper()
        if not code:
            continue
        posted = str(row.get("publishedAt") or "")
        posted_utc = ""
        if posted:
            try:
                dt = datetime.fromisoformat(posted)
                posted_utc = dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                posted = dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                posted_utc = ""
        platform = str(row.get("platform") or "社区")
        url = row.get("url") or ""
        records.append(
            CodeRecord(
                code=code,
                source="community_json",
                source_label=_platform_label(url, f"社区共享 · {platform}"),
                source_url=url,
                author=str(row.get("author") or "社区用户").lstrip("@"),
                posted_at=posted,
                posted_at_utc=posted_utc,
                reply_count=int(row.get("replies", 0) or 0),
                status="unknown",
            )
        )
    return records


def fetch_community_feeds() -> list[CodeRecord]:
    records: list[CodeRecord] = []

    def job(item: tuple[str, str]) -> list[CodeRecord]:
        url, source = item
        data = http_json(url)
        if not data:
            return []
        if isinstance(data, list):
            parsed = _parse_flat_list(data)
        elif source == "community_note":
            parsed = _parse_note_format(data)
        else:
            parsed = _parse_good_format(data)
        for rec in parsed:
            rec.source = source
            rec.source_label = SOURCE_LABELS.get(source) or rec.source_label or "社区共享"
        return parsed

    with ThreadPoolExecutor(max_workers=4) as pool:
        for chunk in pool.map(job, COMMUNITY_FEEDS):
            records.extend(chunk)
    return records


# --------------------------------------------------------------------------- #
# 源 4：社区公开搜索页（微博 / 知乎 / 贴吧 / 微信公众号 / 小红书 / 抖音）
#
# 这些平台多数需要登录态，匿名抓取通常拿不到正文；即便拿到，页面里的
# 6 位串也大多是脚本哈希等噪音。因此这里抓到候选后，一律用官方 oracle
# 前置校验，只有通过官方接口的真码才进入结果集，避免污染列表。
# 若某平台返回登录墙，则自然产出 0 条，不影响其它源。
# --------------------------------------------------------------------------- #

def fetch_community_search_pages() -> list[CodeRecord]:
    q = urllib.parse.quote("小豆邀请码")

    def job(item: tuple[str, str, str, str]) -> list[CodeRecord]:
        source, label, template, referer = item
        url = template.format(q=q)
        raw = http_get(url, referer=referer, timeout=18)
        if not raw or "小豆" not in raw:
            return []
        # 只取「小豆/邀请码/豆包」上下文附近出现的码，进一步降噪
        candidates: list[str] = []
        for match in re.finditer(r"(小豆|邀请码|豆包)", raw):
            window = raw[max(0, match.start() - 120) : match.start() + 200]
            for code in extract_codes(window):
                if code not in candidates:
                    candidates.append(code)
        if not candidates:
            return []

        # 官方接口前置校验：只保留真实存在的码
        results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            for code, res in zip(candidates, pool.map(official_verify, candidates)):
                results[code] = res

        records: list[CodeRecord] = []
        for code, res in results.items():
            if not res.get("verified"):
                continue
            expired = bool(res.get("expired"))
            records.append(
                CodeRecord(
                    code=code,
                    source=source,
                    source_label=label,
                    source_url=url.replace("{q}", q),
                    author=label,
                    title=f"来自{label}公开搜索结果",
                    status="expired" if expired else "available",
                    status_note="官方接口显示已过期" if expired else "官方接口校验有效",
                    verified=True,
                    expire_at=int(res.get("expire_at") or 0),
                )
            )
        return records

    all_records: list[CodeRecord] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for chunk in pool.map(job, COMMUNITY_SEARCHES):
            all_records.extend(chunk)
    return all_records


# --------------------------------------------------------------------------- #
# 源 5：豆瓣小组（讨论搜索 + 话题详情评论区）
#
# 豆瓣对匿名抓取限流很严（短时间多次请求会触发「登录跳转」墙），因此：
#   - 自带长 TTL 缓存（默认 15 分钟），即使站点每 5 秒刷新也不会反复打豆瓣
#   - 每次最多抓少量关键词 / 话题，请求之间加延迟
#   - 抓到候选码一律用官方 oracle 前置校验，避免页面噪音污染
#   - 任一环节被限流就自然产出 0 条，不影响其它源
# --------------------------------------------------------------------------- #

DOUBAN_SEARCH_KEYWORDS = [
    "小豆 邀请码",
    "豆包 小豆 邀请码",
]

DOUBAN_MAX_TOPICS = 8  # 每轮最多进入详情页的话题数
DOUBAN_CACHE_TTL = 900  # 秒；豆瓣自带缓存，避免高频刷新打爆

_douban_cache: dict = {"at": 0.0, "records": []}

# 豆瓣对 curl 默认 UA 敏感，补语言头可显著提高匿名命中率
DOUBAN_HEADERS = ["-H", "Accept-Language: zh-CN,zh;q=0.9"]


def _douban_get(url: str, referer: str = "https://www.douban.com/group/") -> str:
    cmd = ["curl", "-sL", "--max-time", "18", "-A", UA] + DOUBAN_HEADERS
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    cmd.append(url)
    try:
        raw = subprocess.run(cmd, capture_output=True, text=True, timeout=26).stdout or ""
    except subprocess.TimeoutExpired:
        return ""
    # 命中登录墙 / 风控页直接视为空，避免误采
    if "登录跳转" in raw or "有异常请求" in raw or "sec.douban.com" in raw:
        return ""
    return raw


def fetch_douban_topic_list(keyword: str) -> list[tuple[str, str]]:
    """讨论搜索（cat=1013），返回 [(话题ID, 标题)]。"""
    url = (
        "https://www.douban.com/group/search?cat=1013&q="
        + urllib.parse.quote(keyword)
    )
    raw = _douban_get(url)
    if not raw:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    pattern = re.compile(
        r'href="https?://www\.douban\.com/group/topic/(\d+)/?"[^>]*>(.*?)</a>',
        re.S,
    )
    for m in pattern.finditer(raw):
        tid = m.group(1)
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if tid in seen:
            continue
        seen.add(tid)
        out.append((tid, title))
    return out[:DOUBAN_MAX_TOPICS]


def _clean_douban_text(raw_html: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", raw_html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text).strip()


def fetch_douban_topic_detail(topic_id: str, title: str = "") -> list[CodeRecord]:
    """抓话题详情页：正文里的码，以及每条回复里的码（附回复评论上下文）。"""
    url = f"https://www.douban.com/group/topic/{topic_id}/"
    raw = _douban_get(url)
    if not raw:
        return []
    records: list[CodeRecord] = []

    # 正文：#link-report 里的 .topic-richtext / .topic-content
    body = ""
    body_m = re.search(
        r'<div[^>]*id="link-report"[^>]*>(.*?)</div>\s*</div>',
        raw, re.S,
    )
    if not body_m:
        body_m = re.search(
            r'<div[^>]*class="[^"]*topic-richtext[^"]*"[^>]*>(.*?)</div>',
            raw, re.S,
        )
    if body_m:
        body = _clean_douban_text(body_m.group(1))

    def add_from(text: str, is_comment: bool, reply: dict | None = None) -> None:
        if not _is_relevant(text) and not _is_relevant(title):
            return
        for code in extract_codes(text):
            comments = []
            if is_comment and reply:
                comments = [{
                    "author": reply.get("author", ""),
                    "text": reply.get("text", ""),
                    "posted_at": reply.get("posted_at", ""),
                }]
            extra: dict = {}
            if comments:
                extra["comments"] = comments
            records.append(
                CodeRecord(
                    code=code,
                    source="douban_comment" if is_comment else "douban_topic",
                    source_label=(
                        SOURCE_LABELS["douban_comment"] if is_comment
                        else SOURCE_LABELS["douban_topic"]
                    ),
                    source_url=url,
                    author=(reply or {}).get("author", "") if is_comment else "豆瓣用户",
                    title=(reply or {}).get("text", "")[:120] if is_comment else (title or body[:120]),
                    posted_at=(reply or {}).get("posted_at", "") if is_comment else "",
                    extra=extra,
                )
            )

    add_from(body, False)

    # 评论区：直接定位每条回复的正文节点，再向前回溯作者与时间，避免嵌套 div 干扰。
    content_re = re.compile(
        r'<(?:div|span|p)[^>]*class="[^"]*(?:reply-content|comment-content|short)[^"]*"[^>]*>(.*?)</(?:div|span|p)>',
        re.S,
    )
    for cm in content_re.finditer(raw):
        text = _clean_douban_text(cm.group(1))
        if not text or not extract_codes(text):
            continue
        # 在该回复正文之前的一段窗口里找最近的作者 / 时间（取最后一个匹配）
        window = raw[max(0, cm.start() - 500):cm.start()]
        author = ""
        author_ms = re.findall(
            r'class="[^"]*(?:reply-author|comment-info)[^"]*"[^>]*>.*?>?\s*([^\s<][^<]*?)\s*</a>',
            window, re.S,
        )
        if not author_ms:
            author_ms = re.findall(r'class="[^"]*(?:reply-author|author)[^"]*"[^>]*>\s*([^<]+)', window, re.S)
        if author_ms:
            author = author_ms[-1].strip()
        posted = ""
        time_ms = re.findall(
            r'class="[^"]*(?:reply-time|comment-time|time)[^"]*"[^>]*>\s*([^<]+)',
            window, re.S,
        )
        if time_ms:
            posted = time_ms[-1].strip()
        add_from(text, True, {"author": author, "text": text, "posted_at": posted})

    return records


def collect_douban() -> list[CodeRecord]:
    """带长 TTL 缓存的豆瓣采集，失败 / 限流安全降级为空。"""
    now = time.time()
    if now - _douban_cache["at"] < DOUBAN_CACHE_TTL and _douban_cache["records"]:
        return list(_douban_cache["records"])

    candidates: list[CodeRecord] = []
    for kw in DOUBAN_SEARCH_KEYWORDS:
        try:
            topics = fetch_douban_topic_list(kw)
        except Exception:
            topics = []
        time.sleep(1.2)  # 请求间延迟，降低触发风控概率
        for tid, title in topics:
            # 搜索结果标题里若直接带码，先收一条
            if extract_codes(title):
                for code in extract_codes(title):
                    candidates.append(
                        CodeRecord(
                            code=code,
                            source="douban_topic",
                            source_label=SOURCE_LABELS["douban_topic"],
                            source_url=f"https://www.douban.com/group/topic/{tid}/",
                            title=title,
                            author="豆瓣用户",
                        )
                    )
            try:
                candidates.extend(fetch_douban_topic_detail(tid, title))
            except Exception:
                pass
            time.sleep(1.0)

    if not candidates:
        # 空结果也缓存较短时间，避免每 5 秒反复打豆瓣
        _douban_cache["at"] = now
        _douban_cache["records"] = []
        return []

    # 官方接口前置校验，只保留真码
    codes: list[str] = []
    for rec in candidates:
        if rec.code not in codes:
            codes.append(rec.code)
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for code, res in zip(codes, pool.map(official_verify, codes)):
            results[code] = res

    verified: list[CodeRecord] = []
    for rec in candidates:
        res = results.get(rec.code, {})
        if not res.get("verified"):
            continue
        expired = bool(res.get("expired"))
        rec.verified = True
        rec.status = "expired" if expired else "available"
        rec.status_note = "官方接口显示已过期" if expired else "官方接口校验有效"
        rec.expire_at = int(res.get("expire_at") or 0)
        verified.append(rec)

    _douban_cache["at"] = now
    _douban_cache["records"] = verified
    return list(verified)


# --------------------------------------------------------------------------- #
# 源 6：豆包官方接口校验
# --------------------------------------------------------------------------- #

OFFICIAL_INFO_API = (
    "https://activity.doubao.com/alice/ug/campaign/onboarding/invite_code/info/"
)


# 官方接口校验额度有限（每账号每天约 10 次量级），加 TTL 缓存避免高频抓取打爆额度。
# 5 分钟内同一码复用结果；过期时间临近的码缩短缓存，保证状态能及时翻转。
_VERIFY_CACHE: dict[str, tuple[float, dict]] = {}
_VERIFY_TTL = 300  # 秒


def official_verify(code: str) -> dict:
    """带 TTL 缓存的官方校验，避免高频抓取打爆额度。"""
    now = time.time()
    cached = _VERIFY_CACHE.get(code)
    if cached and now - cached[0] < _VERIFY_TTL:
        return cached[1]
    result = _official_verify_uncached(code)
    # 已过期的码缓存短一些，便于抢到新码后状态及时更新
    ttl = _VERIFY_TTL
    if result.get("verified") and not result.get("expired") and result.get("expire_at"):
        left = result["expire_at"] - now
        ttl = min(ttl, max(30, int(left / 2)))
    _VERIFY_CACHE[code] = (now, result)
    return result


def _official_verify_uncached(code: str) -> dict:
    """真正调用豆包官方邀请页接口。M-XXXXXX 形式，接口只接受 share_id。"""
    share_id = code if code.startswith("M-") else f"M-{code}"
    payload = json.dumps({"share_id": share_id})
    cmd = [
        "curl", "-s", "--max-time", "12", "-A", UA,
        "-X", "POST", OFFICIAL_INFO_API,
        "-H", "Content-Type: application/json",
        "-H", f"Referer: https://activity.doubao.com/ug/invite-share/{share_id}",
        "-d", payload,
    ]
    try:
        raw = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20
        ).stdout
        data = json.loads(raw)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, TypeError):
        return {"verified": False, "reason": "请求失败"}
    if not isinstance(data, dict):
        return {"verified": False, "reason": "响应异常"}
    if data.get("code") == 0 and isinstance(data.get("data"), dict):
        info = data["data"]
        expire_at = int(info.get("expire_at") or 0)
        real_code = str(info.get("code") or "")
        expired = bool(expire_at) and expire_at < int(time.time())
        return {
            "verified": True,
            "expire_at": expire_at,
            "real_code": real_code,
            "expired": expired,
            "reason": "",
        }
    return {"verified": False, "reason": str(data.get("msg") or "未知")}


# --------------------------------------------------------------------------- #
# 合并与状态判定
# --------------------------------------------------------------------------- #

UPSTREAM_STATUS_MAP = {
    "maybe_available": "unknown",
    "maybe_used": "used",
    "confirmed_used": "used",
}


def _append_hit(exist: CodeRecord, rec: CodeRecord) -> None:
    """把一条记录的来源详情并入命中列表（去重）。用户提交来源排最前。"""
    hit = rec.source_hit()
    # 用 (来源, 链接, 正文) 去重，避免同一条评论重复出现
    sig = (hit["source"], hit["url"], hit["title"])
    for old in exist.hits:
        if (old.get("source"), old.get("url"), old.get("title")) == sig:
            # 已存在则合并评论
            seen = {c.get("text") for c in old.get("comments", [])}
            for c in hit.get("comments", []):
                if c.get("text") not in seen:
                    old.setdefault("comments", []).append(c)
                    seen.add(c.get("text"))
            return
    if hit.get("user_submitted"):
        exist.hits.insert(0, hit)
    else:
        exist.hits.append(hit)


def merge_records(groups: Iterable[Iterable[CodeRecord]]) -> list[CodeRecord]:
    """按邀请码去重合并，保留信息最完整的一条，并汇总全部命中来源。"""
    merged: dict[str, CodeRecord] = {}
    for group in groups:
        for rec in group:
            if not rec.code:
                continue
            rec.code = rec.code.upper()
            key = rec.key()
            exist = merged.get(key)
            if exist is None:
                rec.first_seen = now_cst().strftime("%Y-%m-%d %H:%M:%S")
                # 归一化上游状态标签
                if rec.status in UPSTREAM_STATUS_MAP:
                    rec.extra.setdefault("upstream_status", rec.status)
                    rec.status = UPSTREAM_STATUS_MAP[rec.status]
                rec.hits = []
                _append_hit(rec, rec)
                merged[key] = rec
                continue
            exist.seen_count += 1
            _append_hit(exist, rec)
            # 用户提交的码优先标注来源（比社区快照更可信）
            if rec.extra.get("user_submitted"):
                exist.source = rec.source
                exist.source_label = rec.source_label
                exist.category = rec.category
                exist.status = rec.status
                exist.status_note = rec.status_note
                exist.verified = True
                # 用户提交优先：作者展示分享者，站外保留原文链接、站内清空来源
                exist.author = rec.author
                exist.source_url = rec.source_url
                exist.extra.pop("upstream_status", None)
                exist.extra["user_submitted"] = True
                if rec.sharer:
                    exist.sharer = rec.sharer
            # 同源多条时也保留更具体的来源标签
            elif len(rec.source_label) > len(exist.source_label):
                exist.source_label = rec.source_label
            # 补齐缺失字段
            if not exist.source_url and rec.source_url:
                exist.source_url = rec.source_url
                if not exist.source_label:
                    exist.source_label = rec.source_label
            if not exist.author and rec.author:
                exist.author = rec.author
            if not exist.posted_at and rec.posted_at:
                exist.posted_at = rec.posted_at
                exist.posted_at_utc = rec.posted_at_utc
            if rec.reply_count > exist.reply_count:
                exist.reply_count = rec.reply_count
            if not exist.title and rec.title:
                exist.title = rec.title
            # 多来源命中说明更可能是真码
            if exist.source != rec.source:
                exist.extra.setdefault("also_seen", [])
                if rec.source not in exist.extra["also_seen"]:
                    exist.extra["also_seen"].append(rec.source)
                # 保留更具体的来源标签（如「社区共享 · 小红书」优于「社区共享」）
                # 用户提交的来源优先级最高，不被社区快照覆盖
                if not exist.extra.get("user_submitted") and len(rec.source_label) > len(exist.source_label):
                    exist.source_label = rec.source_label
                if (
                    not exist.extra.get("user_submitted")
                    and exist.source == "community_json"
                    and rec.source != "community_json"
                ):
                    exist.source = rec.source
    return list(merged.values())


def finalize_status(records: list[CodeRecord], verify_limit: int = 400) -> list[CodeRecord]:
    """用官方接口校验一批码，并结合上游状态与时间给出最终状态。

    状态优先级：官方接口 > 社区反馈 > 未知。
    """
    # 优先校验：社区标注可能可用的、较新的码
    candidates = sorted(
        records,
        key=lambda r: (
            0 if r.extra.get("upstream_status") == "maybe_available" else 1,
            r.posted_at_utc or "",
        ),
    )[:verify_limit]
    candidate_ids = {id(r) for r in candidates}

    def job(rec: CodeRecord) -> tuple[CodeRecord, dict]:
        return rec, official_verify(rec.code)

    with ThreadPoolExecutor(max_workers=12) as pool:
        for rec, result in pool.map(job, candidates):
            if result.get("verified"):
                rec.verified = True
                rec.expire_at = int(result.get("expire_at") or 0)
                rec.extra["real_code"] = result.get("real_code", "")
                if result.get("expired"):
                    rec.status = "expired"
                    rec.status_note = "官方接口显示已过期"
                else:
                    rec.status = "available"
                    expire = rec.expire_at
                    rec.status_note = (
                        "官方接口校验有效"
                        + (f"，{datetime.fromtimestamp(expire, CST):%m-%d %H:%M} 过期"
                           if expire else "")
                    )
            else:
                rec.status_note = ""
                rec.extra["official_reason"] = result.get("reason", "")

    # 未参与官方校验的记录，依据社区反馈给状态
    for rec in records:
        if id(rec) in candidate_ids:
            continue
        upstream = rec.extra.get("upstream_status", "")
        if upstream == "maybe_available":
            rec.status = "unknown"
            rec.status_note = "社区标注可能可用，官方接口未确认"
        elif upstream in ("maybe_used", "confirmed_used"):
            rec.status = "used"
            rec.status_note = "社区反馈已被使用" if upstream == "confirmed_used" else "社区反馈可能已被使用"
        else:
            rec.status = "unknown"
            rec.status_note = "未确认可用性"

    # 已校验但官方未确认的记录
    for rec in records:
        if id(rec) not in candidate_ids or rec.verified:
            continue
        upstream = rec.extra.get("upstream_status", "")
        if upstream == "maybe_available":
            rec.status = "unknown"
            rec.status_note = "社区标注可能可用，官方接口未确认"
        elif upstream in ("maybe_used", "confirmed_used"):
            rec.status = "used"
            rec.status_note = "社区反馈已被使用" if upstream == "confirmed_used" else "社区反馈可能已被使用"
        else:
            rec.status = "unknown"
            rec.status_note = "未确认可用性"

    order = {"available": 0, "unknown": 1, "used": 2, "expired": 3}
    # 先按状态分组，组内按发布时间倒序（新的在前）
    buckets: dict[str, list[CodeRecord]] = {}
    for rec in records:
        buckets.setdefault(rec.status, []).append(rec)
    ordered: list[CodeRecord] = []
    for st in sorted(buckets, key=lambda s: order.get(s, 9)):
        group = buckets[st]
        group.sort(key=lambda r: (r.posted_at_utc or "", r.code), reverse=True)
        ordered.extend(group)
    return ordered


def run_once(user_records: list[CodeRecord] | None = None) -> dict:
    """执行一轮完整采集。user_records 为用户提交并经官方校验的码。"""
    started = time.time()
    errors: list[str] = []

    def safe(name: str, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
            return []

    with ThreadPoolExecutor(max_workers=4) as pool:
        fut_search = pool.submit(safe, "bilibili", collect_bilibili)
        fut_community = pool.submit(safe, "community", fetch_community_feeds)
        fut_pages = pool.submit(safe, "community_pages", fetch_community_search_pages)
        fut_douban = pool.submit(safe, "douban", collect_douban)
        search_records = fut_search.result()
        community_records = fut_community.result()
        page_records = fut_pages.result()
        douban_records = fut_douban.result()

    groups: list[list[CodeRecord]] = [search_records, community_records, page_records, douban_records]
    if user_records:
        groups.append(user_records)
    merged = merge_records(groups)
    finalized = finalize_status(merged)

    counts: dict[str, int] = {}
    for rec in finalized:
        counts[rec.status] = counts.get(rec.status, 0) + 1

    payload = {
        "lastUpdated": now_cst().strftime("%Y-%m-%d %H:%M:%S 北京时间"),
        "lastUpdatedUtc": now_cst().isoformat(),
        "elapsed": round(time.time() - started, 1),
        "counts": {
            "total": len(finalized),
            "available": counts.get("available", 0),
            "unknown": counts.get("unknown", 0),
            "used": counts.get("used", 0),
            "expired": counts.get("expired", 0),
            "bilibili": len(search_records),
            "community": len(community_records),
            "community_pages": len(page_records),
            "douban": len(douban_records),
        },
        "errors": errors,
        "codes": [rec.to_dict() for rec in finalized],
    }
    return payload


if __name__ == "__main__":
    result = run_once()
    print(json.dumps(result["counts"], ensure_ascii=False, indent=2))
    print("errors:", result["errors"])
    for rec in result["codes"][:15]:
        print(f"  {rec['code']:10} {rec['status']:10} {rec['source_label']:14} {rec['title'][:40]}")
