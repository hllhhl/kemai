"""B 站公开接口客户端。

只做四件事：搜课程、拿分 P、取字幕轨、取字幕正文。
所有请求都是公开可见的页面数据，不下载视频源、不绕过任何付费或会员限制。

两个来自实战的坑，直接写进实现里：

1. ``x/player/wbi/v2`` 需要 WBI 签名，而且 **AI 字幕只有带登录态的请求才返回**。
   所以字幕通道做成四级兜底：签名 → 免签 → 旧版 ``v2`` → ``pagelist`` 遍历所有分 P。
   免登录跑不出字幕是常态，这不是失败，需要浏览器扩展在页面上下文里带 SESSDATA 抓取。
2. 裸请求 ``search/type`` 会返回反爬 HTML。必须同时带 WBI 签名、``buvid3`` 与 Referer。

错误一律翻译成人话，界面不该出现 ``ConnectError``。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from threading import Lock
import time
import urllib.parse

import httpx

_BASE = "https://api.bilibili.com"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# WBI 混入密钥用的固定重排表
_MIXIN_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
)


class BilibiliError(RuntimeError):
    """B 站接口不可用，消息面向用户。"""


@dataclass(frozen=True)
class BiliPage:
    page: int
    cid: int
    part: str
    duration: int


@dataclass(frozen=True)
class BiliVideo:
    bvid: str
    title: str
    owner: str
    cover: str
    pages: tuple[BiliPage, ...]
    videos: int


def _escape(value: str) -> str:
    """WBI 要求过滤 ``!'()*``，否则签名对不上。"""
    out = urllib.parse.quote(str(value), safe="")
    for ch, code in (("!", "21"), ("'", "27"), ("(", "28"), (")", "29"), ("*", "2A")):
        out = out.replace(ch, "%" + code)
    return out


def chapter_id(text: str) -> str | None:
    """从分 P 标题里抽章节编号：``3.1.1栈的定义`` → ``3.1.1``。"""
    import re

    s = str(text or "").strip()
    # 编号可能用点也可能用下划线分隔：``3.1.1栈的定义`` / ``0_1_前言``。
    # 西电那门 72 讲全用下划线，认不出就会退化成「按顺序均分」，置信度掉到 0.3。
    m = re.match(r"^(\d+(?:[._]\d+){1,3})\s*[.、．:：]?\s*", s)
    if m and m.group(1):
        return m.group(1).replace("_", ".")
    m = re.match(r"^(\d+)\s*[.、．:：]\s*", s)
    if m and m.group(1):
        return m.group(1)
    m = re.match(r"^第\s*([一二三四五六七八九十百\d]+)\s*[章节讲]", s)
    if m:
        return m.group(1)
    m = re.match(r"^(?:lecture|lesson|unit|chap(?:ter)?)\s*0*(\d+)", s, re.I)
    if m:
        return m.group(1)
    return None


def chapter_prefixes(value: str) -> list[str]:
    """``3.1.1`` → ``['3', '3.1', '3.1.1']``，用于教材与网课的层级对齐。"""
    parts = str(value or "").split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


class BilibiliClient:
    """带缓存的轻客户端。线程安全，签名与访客 cookie 半小时刷新一次。"""

    def __init__(self, timeout: float = 20.0, transport: httpx.BaseTransport | None = None,
                 cookie: str = ""):
        self.cookie = cookie
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            headers={
                "User-Agent": _UA,
                "Referer": "https://www.bilibili.com/",
                # 扫码登录后带上的凭证。空字符串表示匿名。
                **({"Cookie": cookie} if cookie else {}),
            },
        )
        self._lock = Lock()
        self._mixin_key: str | None = None
        self._cookie = ""
        self._fetched_at = 0.0

    # ── 内部 ───────────────────────────────────────────────
    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _json(self, url: str, *, allow_code: tuple[int, ...] = (0,)) -> dict:
        try:
            response = self._client.get(url)
        except httpx.HTTPError as exc:
            raise BilibiliError(f"连不上 B 站：{exc.__class__.__name__}") from exc
        text = response.text
        if "反爬" in text or text.lstrip().startswith("<"):
            raise BilibiliError("被 B 站反爬拦截（返回了网页）。通常是缺 WBI 签名或请求过频，等 1-2 分钟再试。")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BilibiliError("B 站返回了非 JSON 内容：" + text[:80]) from exc
        code = int(payload.get("code", -1))
        if code == -412:
            raise BilibiliError("被 B 站限流（-412）。停 1-2 分钟再试，不要连续重试。")
        if code not in allow_code:
            raise BilibiliError(f"B 站接口返回 {code}：{payload.get('message') or '未知错误'}")
        return payload

    def _keys(self) -> str:
        """拿 WBI 密钥；nav 对未登录用户返回 -101，但 wbi_img 照给。"""
        with self._lock:
            if self._mixin_key and time.time() - self._fetched_at < 1800:
                return self._mixin_key
            payload = self._json(f"{_BASE}/x/web-interface/nav", allow_code=(-101, 0))
            wbi = (payload.get("data") or {}).get("wbi_img") or {}
            img = str(wbi.get("img_url", "")).rsplit("/", 1)[-1].split(".")[0]
            sub = str(wbi.get("sub_url", "")).rsplit("/", 1)[-1].split(".")[0]
            if not img or not sub:
                raise BilibiliError("拿不到 WBI 密钥，B 站接口结构可能变了")
            raw = img + sub
            self._mixin_key = "".join(raw[i] for i in _MIXIN_TAB)[:32]
            try:
                spi = self._json(f"{_BASE}/x/frontend/finger/spi")
                data = spi.get("data") or {}
                if data.get("b_3"):
                    self._cookie = f"buvid3={data['b_3']}; buvid4={data.get('b_4', '')}; "
            except BilibiliError:
                self._cookie = ""
            self._fetched_at = time.time()
            return self._mixin_key

    def sign(self, params: dict) -> str:
        """给参数追加 wts / w_rid，返回可直接拼在 URL 后的查询串。"""
        mixin = self._keys()
        merged = {**params, "wts": int(time.time())}
        query = "&".join(f"{_escape(k)}={_escape(merged[k])}" for k in sorted(merged))
        digest = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
        return f"{query}&w_rid={digest}"

    # ── 对外 ───────────────────────────────────────────────
    def search(self, keyword: str, page: int = 1, *, limit: int = 12) -> list[dict]:
        query = self.sign({"search_type": "video", "keyword": keyword, "page": page})
        payload = self._json(f"{_BASE}/x/web-interface/search/type?{query}")
        data = payload.get("data") or {}
        items = data.get("result") or []
        out: list[dict] = []
        for item in items[:limit]:
            pic = str(item.get("pic") or "")
            out.append({
                "bvid": item.get("bvid"),
                "title": _strip_tags(item.get("title")),
                "author": item.get("author") or "",
                "play": int(item.get("play") or 0),
                "duration": item.get("duration") or "",
                "description": _strip_tags(item.get("description")),
                "cover": ("https:" + pic) if pic.startswith("//") else pic,
            })
        return out

    def view(self, bvid: str) -> BiliVideo:
        payload = self._json(f"{_BASE}/x/web-interface/view?bvid={urllib.parse.quote(bvid)}")
        data = payload.get("data") or {}
        pages = tuple(
            BiliPage(
                page=int(p.get("page") or 0),
                cid=int(p.get("cid") or 0),
                part=str(p.get("part") or ""),
                duration=int(p.get("duration") or 0),
            )
            for p in (data.get("pages") or [])
        )
        if not pages:
            raise BilibiliError("这个视频没有分 P 信息，可能已失效或需要登录")
        return BiliVideo(
            bvid=data.get("bvid") or bvid,
            title=str(data.get("title") or ""),
            owner=str((data.get("owner") or {}).get("name") or ""),
            cover=str(data.get("pic") or ""),
            pages=pages,
            videos=int(data.get("videos") or len(pages)),
        )

    def subtitle_tracks(self, bvid: str, cid: int) -> dict:
        """四级兜底，返回 ``{"tracks": [...], "tried": [...], "via": str|None}``。"""
        tried: list[str] = []

        def collect(payload: dict, label: str) -> list[dict]:
            code = int(payload.get("code", -1))
            tried.append(f"{label}:code{code}")
            subs = ((payload.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
            return list(subs)

        signed = self.sign({"bvid": bvid, "cid": cid})
        subs = collect(self._json(f"{_BASE}/x/player/wbi/v2?{signed}", allow_code=(-404, -400, 0)), "wbi_signed")
        if subs:
            return {"tracks": subs, "tried": tried, "via": "wbi_signed"}

        subs = collect(self._json(f"{_BASE}/x/player/wbi/v2?bvid={bvid}&cid={cid}", allow_code=(-404, -400, 0)), "wbi_unsigned")
        if subs:
            return {"tracks": subs, "tried": tried, "via": "wbi_unsigned"}

        subs = collect(self._json(f"{_BASE}/x/player/v2?bvid={bvid}&cid={cid}", allow_code=(-404, -400, 0)), "legacy_v2")
        if subs:
            return {"tracks": subs, "tried": tried, "via": "legacy_v2"}

        pagelist = self._json(f"{_BASE}/x/player/pagelist?bvid={bvid}")
        pages = pagelist.get("data") or []
        tried.append(f"pagelist:code0/{len(pages)}pages")
        for page in pages:
            pcid = int(page.get("cid") or 0)
            if not pcid or pcid == cid:
                continue
            subs = collect(
                self._json(f"{_BASE}/x/player/v2?bvid={bvid}&cid={pcid}", allow_code=(-404, -400, 0)),
                f"p{page.get('page')}",
            )
            if subs:
                return {"tracks": subs, "tried": tried, "via": f"p{page.get('page')}", "cid": pcid}
        return {"tracks": [], "tried": tried, "via": None}

    def transcript(self, bvid: str, cid: int) -> dict:
        """取字幕正文。没有字幕时返回空列表和诊断，不抛异常。"""
        found = self.subtitle_tracks(bvid, cid)
        tracks = sorted(found["tracks"], key=lambda s: 1 if s.get("ai_type") == 1 else 0)
        if not tracks:
            return {"lines": [], "diagnosis": {"via": found["via"], "tried": found["tried"]}}
        url = str(tracks[0].get("subtitle_url") or "")
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            return {"lines": [], "diagnosis": {"via": found["via"], "tried": found["tried"], "error": "字幕地址为空"}}
        try:
            response = self._client.get(url)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"lines": [], "diagnosis": {"via": found["via"], "tried": found["tried"], "error": str(exc)}}
        entries = body.get("body") if isinstance(body, dict) else body
        if not isinstance(entries, list):
            entries = []
        lines = merge_transcript([
            {"from": float(x.get("from") or 0), "to": float(x.get("to") or 0), "text": str(x.get("content") or "")}
            for x in entries
        ])
        return {"lines": lines, "diagnosis": {"via": found["via"], "tried": found["tried"], "lan": tracks[0].get("lan_doc") or ""}}


def merge_transcript(lines: list[dict]) -> list[dict]:
    """B 站 AI 字幕常把一句话切成两三行，这里合回去；相邻过短的片段也合并。"""
    out: list[dict] = []
    for item in lines:
        text = str(item.get("text") or "").replace("\n", " ").strip()
        if not text:
            continue
        if out and out[-1]["text"] == text:
            continue
        if out and len(out[-1]["text"]) < 16 and len(text) < 16 and abs(float(item.get("from") or 0) - out[-1]["from"]) < 3:
            out[-1]["text"] += text
            out[-1]["to"] = float(item.get("to") or out[-1]["to"])
            continue
        out.append({"from": float(item.get("from") or 0), "to": float(item.get("to") or 0), "text": text})
    return out


def search_terms(course_name: str, chapters: list[str], glossary: list[str], limit: int = 4) -> list[str]:
    """把课件身份变成检索词。章名与课程名相同时不重复拼。"""
    course = (course_name or "").strip()
    chapter_names = [c.strip() for c in chapters if c and str(c).strip()]
    unique_chapters: list[str] = []
    for name in chapter_names:
        if name == course or name in unique_chapters:
            continue
        unique_chapters.append(name)
    terms = [t.strip() for t in glossary if t and 2 <= len(str(t).strip()) <= 14]

    out: list[str] = []
    if course and len(course) >= 2:
        out.append(course)
        # 课程名常带「基础/原理/概论」这类后缀，去掉后检索面更宽：
        # 「数字集成电路基础」搜不到的那门课，用「数字集成电路」能搜到。
        import re as _re
        short = _re.sub(r"(基础|原理|导论|概论|入门|教程|技术)$", "", course).strip()
        if short and short != course and len(short) >= 4:
            out.append(short)
        if unique_chapters:
            out.append(f"{course} {unique_chapters[0]}")
        if len(unique_chapters) > 1:
            out.append(f"{course} {unique_chapters[1]}")
        elif terms:
            out.append(f"{course} {terms[0]}")
    if len(unique_chapters) >= 2:
        out.append(" ".join(unique_chapters[:2]))
    if unique_chapters:
        out.append(f"{unique_chapters[0]} {terms[0] if terms else '课程'}")
    if len(terms) >= 2:
        out.append(" ".join(terms[:3]))

    seen: set[str] = set()
    result: list[str] = []
    for term in out:
        if not term or len(term) < 2 or term in seen:
            continue
        seen.add(term)
        result.append(term[:40])
    return result[:limit]


def _strip_tags(value) -> str:
    import html
    import re

    return html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))).strip()
