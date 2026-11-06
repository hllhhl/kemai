"""给 agent 的内置课程工具。

MCP 工具再强，也只能查 LTspice、查文件——它不知道这门课有几章、笔记写完了没有、
你哪几道题做错了。没有这几个工具，agent 再会循环也只能回答"我没有相关信息"。

刻意分成「读」和「改」两类，因为风险不同：

    course_brief   读   课程结构、资料状态
    notes_read     读   最新笔记正文
    practice_read  读   题库
    wrong_read     读   错题本
    docs_export    改   落成 docx / pptx 文件（会写盘，但不动已有内容）

前四个纯读，哨兵一定放行；导出走 moderate。**没有「删除」「重建」类工具**：
模型不该有那种手，需要的时候人来点。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy import func, select

from .models import (
    Chapter,
    Course,
    Document,
    Job,
    Outline,
    PracticeQuestion,
    WrongQuestion,
)

logger = logging.getLogger(__name__)

MAX_NOTE_CHARS = 6000


def catalog() -> list[dict]:
    """给模型看的工具清单，格式和 MCP 工具一致，agent 循环不用特判。"""
    return [
        {
            "name": "kemai.bili_status",
            "description": "查 B 站登录态。AI 字幕和搜索都需要登录；没登录时会说明该去扫码。",
            "read_only": True,
            "destructive": False,
            "schema": {"type": "object", "properties": {}},
        },
        {
            "name": "kemai.bili_search",
            "description": "在 B 站按课程名搜索视频课，返回标题、BV 号、UP 主和简介。用户说「找某门课」时先调它。",
            "read_only": True,
            "destructive": False,
            "schema": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "课程名，如 模拟集成电路设计"},
                    "limit": {"type": "integer", "description": "最多几条，默认 6"},
                },
                "required": ["keyword"],
            },
        },
        {
            "name": "kemai.bili_parts",
            "description": "看一门 B 站课程的视频详情与分 P 列表（每一讲的 cid 和标题）。",
            "read_only": True,
            "destructive": False,
            "schema": {"type": "object", "properties": {"bvid": {"type": "string", "description": "BV 号"}}, "required": ["bvid"]},
        },
        {
            "name": "kemai.bili_transcript",
            "description": "取 B 站某一讲的字幕正文（带时间戳）。没有字幕时会给出原因和可用的兜底方式。",
            "read_only": True,
            "destructive": False,
            "schema": {
                "type": "object",
                "properties": {
                    "bvid": {"type": "string", "description": "BV 号"},
                    "cid": {"type": "integer", "description": "分 P 的 cid，从 bili_parts 拿"},
                    "limit": {"type": "integer", "description": "最多返回多少行字幕，默认 120"},
                },
                "required": ["bvid", "cid"],
            },
        },
        {
            "name": "kemai.course_import_text",
            "description": "把一段字幕或讲义文本作为资料导入当前课程，之后才能生成有依据的复习笔记。",
            "read_only": False,
            "destructive": False,
            "schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要导入的正文"},
                    "title": {"type": "string", "description": "资料名，如 第1讲 绪论"},
                },
                "required": ["text"],
            },
        },
        {
            "name": "kemai.course_brief",
            "description": "看这门课的骨架：章节、资料状态、笔记有没有、题库和错题各多少条。先调它再决定做什么。",
            "read_only": True,
            "destructive": False,
            "schema": {"type": "object", "properties": {}},
        },
        {
            "name": "kemai.notes_read",
            "description": "读最新的复习笔记正文。可以用 keyword 只取包含某个词的段落。",
            "read_only": True,
            "destructive": False,
            "schema": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "只看提到这个词的段落，可空"},
                },
            },
        },
        {
            "name": "kemai.practice_read",
            "description": "读题库里的题目（题干、类型、难度）。limit 默认 10。",
            "read_only": True,
            "destructive": False,
            "schema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "最多几条，默认 10"}},
            },
        },
        {
            "name": "kemai.wrong_read",
            "description": "读错题本：题干、我选的、正确答案、错误原因。复习计划要看它。",
            "read_only": True,
            "destructive": False,
            "schema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "最多几条，默认 10"}},
            },
        },
        {
            "name": "kemai.course_search",
            "description": "在这门课的资料里做语义检索（比按字面匹配 notes_read 准）。query 用一句自然语言。",
            "read_only": True,
            "destructive": False,
            "schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要查什么，一句话"},
                    "limit": {"type": "integer", "description": "最多几段，默认 6"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "kemai.notes_build",
            "description": "开始生成这门课的复习笔记。要 30–90 秒；本工具会等到完成或超时。已有笔记时不必重复生成。",
            "read_only": False,
            "destructive": False,
            "schema": {
                "type": "object",
                "properties": {
                    "wait_seconds": {"type": "integer", "description": "最多等多久，默认 75，上限 150"},
                },
            },
        },
        {
            "name": "kemai.memory_read",
            "description": "读长期记忆。scope：identity(我是谁) / soul(怎么做) / user(关于用户) / memory(跨课程) / course(这门课) / log(今天的日志)。",
            "read_only": True,
            "destructive": False,
            "schema": {
                "type": "object",
                "properties": {"scope": {"type": "string",
                    "enum": ["identity", "soul", "user", "memory", "course", "log"]}},
                "required": ["scope"],
            },
        },
        {
            "name": "kemai.memory_write",
            "description": "往长期记忆里写。默认追加（append=true）；整份替换只用于「改掉这条」。写 user/memory/course 最常用。",
            "read_only": False,
            "destructive": False,
            "risk_type": "memory",
            "schema": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["user", "memory", "course", "soul", "identity"]},
                    "content": {"type": "string"},
                    "append": {"type": "boolean", "description": "true=追加，false=整份替换，默认 true"},
                },
                "required": ["scope", "content"],
            },
        },
        {
            "name": "kemai.memory_log",
            "description": "往今天的日志追加一条（做了什么、发现了什么）。只追加，不改已有内容。",
            "read_only": False,
            "destructive": False,
            "schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
        {
            "name": "kemai.docs_export",
            "description": "把最新笔记导出成 docx 或 pptx 文件。kind 取 docx 或 pptx。",
            "read_only": False,
            "destructive": False,
            "schema": {
                "type": "object",
                "properties": {"kind": {"type": "string", "enum": ["docx", "pptx"]}},
                "required": ["kind"],
            },
        },
    ]


_services = None


def bind(services) -> None:
    """启动时把 Services 交给这一层。

    不在这里 import Services 的原因是它要建数据库连接池；
    工具模块被 import 时就去连库，测试会跟着变慢。
    """
    global _services
    _services = services


def _require():
    if _services is None:
        raise RuntimeError("课程工具还没绑定 Services")
    return _services


def call(name: str, args: dict, course_id: str) -> dict:
    """执行一个课程工具。返回 {ok, text}，和 MCP 的返回形状保持一致。"""
    handler = {
        "kemai.bili_status": _bili_status,
        "kemai.bili_search": _bili_search,
        "kemai.bili_parts": _bili_parts,
        "kemai.bili_transcript": _bili_transcript,
        "kemai.course_import_text": _course_import_text,
        "kemai.course_brief": _brief,
        "kemai.notes_read": _notes,
        "kemai.practice_read": _practice,
        "kemai.wrong_read": _wrong,
        "kemai.course_search": _search,
        "kemai.notes_build": _build,
        "kemai.memory_read": _memory_read,
        "kemai.memory_write": _memory_write,
        "kemai.memory_log": _memory_log,
        "kemai.docs_export": _export,
    }.get(name)
    if handler is None:
        return {"ok": False, "text": f"没有这个课程工具：{name}"}
    # B 站找课是「从零建一门课」的起点，这时候本来就没有课程可选。
    # 这几个工具不碰课程数据，不该被「先选课」挡住。
    COURSE_FREE = {"kemai.bili_status", "kemai.bili_search",
                   "kemai.bili_parts", "kemai.bili_transcript"}
    if not course_id and name not in COURSE_FREE:
        return {"ok": False, "text": "当前没有选中的课程，先让用户在左边选一门课"}
    try:
        return handler(args or {}, course_id)
    except Exception as exc:  # noqa: BLE001 - 工具失败要变成模型能读的一句话
        logger.exception("课程工具 %s 失败", name)
        return {"ok": False, "text": f"{type(exc).__name__}: {exc}"}


def _brief(_args: dict, course_id: str) -> dict:
    services = _require()
    with services.sessions() as db:
        course = db.get(Course, course_id)
        if course is None:
            return {"ok": False, "text": "课程不存在"}
        chapters = db.scalars(
            select(Chapter).where(Chapter.course_id == course_id).order_by(Chapter.position)
        ).all()
        docs = db.scalars(select(Document).where(Document.course_id == course_id)).all()
        outline = db.scalar(
            select(Outline).where(Outline.course_id == course_id).order_by(Outline.created_at.desc())
        )
        questions = db.scalar(
            select(func.count()).select_from(PracticeQuestion).where(
                PracticeQuestion.document_id.in_(
                    select(Document.id).where(Document.course_id == course_id)
                )
            )
        )
        wrongs = db.scalar(
            select(func.count()).select_from(WrongQuestion).where(WrongQuestion.course_id == course_id)
        )

        ready = [d for d in docs if d.status == "ready"]
        lines = [f"课程：{course.name}"]
        if chapters:
            shown = "；".join(f"{i + 1}.{c.title}" for i, c in enumerate(chapters[:12]))
            lines.append(f"章节（{len(chapters)}）：{shown}")
        else:
            lines.append("章节：还没有（需要先上传资料并确认目录）")
        lines.append(f"资料：{len(docs)} 份，其中已解析 {len(ready)} 份")
        if outline is None:
            lines.append("笔记：还没生成过")
        else:
            lines.append(
                f"笔记：最新一份状态 {outline.status}，正文 {len(outline.markdown or '')} 字"
            )
        lines.append(f"题库：{questions or 0} 道；错题：{wrongs or 0} 道")
        return {"ok": True, "text": "\n".join(lines)}


def _notes(args: dict, course_id: str) -> dict:
    services = _require()
    keyword = str(args.get("keyword") or "").strip()
    with services.sessions() as db:
        outline = db.scalar(
            select(Outline).where(Outline.course_id == course_id).order_by(Outline.created_at.desc())
        )
        if outline is None:
            return {"ok": False, "text": "这门课还没生成过笔记"}
        markdown = outline.markdown or ""
        status = outline.status

    if status not in ("completed", "ready"):
        return {"ok": False, "text": f"最新笔记状态是 {status}，还没写完，不能当作依据"}

    if keyword:
        # 只取相关段落：整篇塞进去会挤掉后面还要用的工具输出
        paragraphs = [p.strip() for p in markdown.split("\n\n") if p.strip()]
        hits = [p for p in paragraphs if keyword in p]
        if not hits:
            return {"ok": True, "text": f"笔记里没有提到「{keyword}」的段落"}
        text = "\n\n".join(hits)
        prefix = f"（含「{keyword}」的 {len(hits)} 段）\n"
    else:
        text = markdown
        prefix = ""

    if len(text) > MAX_NOTE_CHARS:
        text = text[:MAX_NOTE_CHARS] + "\n…（后面还有，已截断）"
    # 把真实字数写在最前面：不写的话模型会自己"感觉"一个数字出来
    return {"ok": True, "text": f"（笔记正文共 {len(markdown)} 字）\n{prefix}{text}"}


def _practice(args: dict, course_id: str) -> dict:
    services = _require()
    limit = int(args.get("limit") or 10)
    limit = max(1, min(limit, 30))
    with services.sessions() as db:
        rows = db.scalars(
            select(PracticeQuestion).where(
                PracticeQuestion.document_id.in_(
                    select(Document.id).where(Document.course_id == course_id)
                )
            ).limit(limit)
        ).all()
    if not rows:
        return {"ok": True, "text": "题库是空的，还没生成过题目"}
    lines = []
    for q in rows:
        stem = " ".join((q.stem or "").split())[:120]
        lines.append(f"- [{q.question_type}] {stem}")
    return {"ok": True, "text": f"题库前 {len(rows)} 道：\n" + "\n".join(lines)}


def _wrong(args: dict, course_id: str) -> dict:
    services = _require()
    limit = int(args.get("limit") or 10)
    limit = max(1, min(limit, 30))
    with services.sessions() as db:
        rows = db.scalars(
            select(WrongQuestion).where(WrongQuestion.course_id == course_id)
            .order_by(WrongQuestion.updated_at.desc()).limit(limit)
        ).all()
    if not rows:
        return {"ok": True, "text": "错题本是空的"}
    lines = []
    for item in rows:
        q = item.question or {}
        stem = " ".join(str(q.get("stem") or "").split())[:100]
        mine = "、".join(str(x) for x in (item.selected or [])) or item.own_answer or "（未记）"
        answer = q.get("answer") or (item.resolution or {}).get("answer") or "（无）"
        lines.append(f"- {stem}\n  我选：{mine}；正确：{answer}；原因：{item.reason or '未记'}")
    return {"ok": True, "text": f"最近 {len(rows)} 道错题：\n" + "\n".join(lines)}


def _export(args: dict, course_id: str) -> dict:
    """导出走的是和界面同一个函数，不另写一份——两份实现迟早出现"界面能导出、agent 导不出"。"""
    services = _require()
    kind = str(args.get("kind") or "").strip()
    if kind not in ("docx", "pptx"):
        return {"ok": False, "text": "kind 只能是 docx 或 pptx"}

    from .docgen import markdown_to_docx, markdown_to_pptx
    from .docs_api import _exports_dir, _safe_name, _stamp

    with services.sessions() as db:
        outline = db.scalar(
            select(Outline).where(
                Outline.course_id == course_id, Outline.status.in_(("completed", "ready"))
            ).order_by(Outline.created_at.desc())
        )
        course = db.get(Course, course_id)
        if outline is None:
            return {"ok": False, "text": "还没有写完的笔记可以导出"}
        markdown = outline.markdown or ""
        title = f"{course.name} 复习笔记" if course else "复习笔记"

    name = f"{_safe_name(title, '复习笔记')}-{_stamp()}.{kind}"
    target = _exports_dir() / name
    if kind == "docx":
        markdown_to_docx(markdown, title, target)
    else:
        markdown_to_pptx(markdown, title, target)
    return {"ok": True, "text": f"已导出 {name}（{target.stat().st_size} 字节），下载地址 /exports/{name}"}

def _search(args: dict, course_id: str) -> dict:
    """向量检索。借的是对话那边同一个 RetrievalService，不另起一套索引。"""
    services = _require()
    query = str(args.get("query") or "").strip()
    if not query:
        return {"ok": False, "text": "query 不能为空"}
    limit = max(1, min(int(args.get("limit") or 6), 12))
    try:
        sources = services.retrieval.search(course_id, query, limit=limit)
    except Exception as exc:  # noqa: BLE001 - 索引没建好时给一句话，别让循环崩
        return {"ok": False, "text": f"检索不可用：{type(exc).__name__}: {exc}"}
    if not sources:
        return {"ok": True, "text": "没有检索到相关内容"}

    lines = []
    for index, source in enumerate(sources, 1):
        text = " ".join(str(source.get("text") or "").split())
        chapter = source.get("chapter_title") or source.get("chapter") or ""
        locator = source.get("locator") or {}
        label = locator.get("label") or locator.get("page") or ""
        head = " ".join(p for p in (chapter, label) if p)
        lines.append(f"[{index}] {head}\n{text[:400]}")
    return {"ok": True, "text": f"检索到 {len(sources)} 段：\n" + "\n\n".join(lines)}


_outline_service = None


def _build(args: dict, course_id: str) -> dict:
    """生成笔记：建任务 → 后台线程跑 → 等它完。

    等而不是立即返回，是因为没人愿意替 agent 记住 job id 再问一次。
    但也不能死等：默认 75 秒，超时就说"还在跑"，让用户过会儿再问。
    """
    global _outline_service
    import threading
    import time as _time

    from .outlines import GenerationBlocked, OutlineService, create_job

    services = _require()
    wait = int(args.get("wait_seconds") or 75)
    wait = max(10, min(wait, 150))

    try:
        if _outline_service is None:
            _outline_service = OutlineService(services, services.knowledge_points)
        job = create_job(_outline_service, services, course_id)
    except GenerationBlocked as exc:
        return {"ok": False, "text": str(exc)}

    threading.Thread(target=_outline_service.run, args=(job["job_id"],), daemon=True,
                     name=f"agent-outline-{job['job_id'][:6]}").start()

    deadline = _time.time() + wait
    status, markdown = "queued", ""
    while _time.time() < deadline:
        _time.sleep(3)
        with services.sessions() as db:
            job_row = db.get(Job, job["job_id"])
            outline = db.get(Outline, job["outline_id"])
            status = (job_row.status if job_row else "missing") or "queued"
            error = job_row.error if job_row else ""
            markdown = (outline.markdown if outline else "") or ""
        if status in ("completed", "failed"):
            break

    if status == "completed":
        return {"ok": True, "text": f"笔记生成完成，正文 {len(markdown)} 字（job {job['job_id'][:8]}）"}
    if status == "failed":
        return {"ok": False, "text": f"生成失败：{error or '未知原因'}（job {job['job_id'][:8]}）"}
    return {"ok": True, "text": f"已经开始了，但 {wait} 秒内没跑完（当前 {status}）。告诉用户稍后再问一次即可，不要重复发起。"}

def _memory_read(args: dict, course_id: str) -> dict:
    from . import longterm

    scope = str(args.get("scope") or "").strip()
    try:
        text = longterm.read(scope, course_id)
    except ValueError as exc:
        return {"ok": False, "text": str(exc)}
    if not text.strip():
        return {"ok": True, "text": f"{scope} 还是空的"}
    return {"ok": True, "text": text[:MAX_NOTE_CHARS]}


def _memory_write(args: dict, course_id: str) -> dict:
    from . import longterm

    scope = str(args.get("scope") or "").strip()
    content = str(args.get("content") or "").strip()
    if not content:
        return {"ok": False, "text": "content 不能为空"}
    # append 默认 true：模型最容易犯的错是"整理"一遍把用户手写的内容洗掉
    append = args.get("append")
    append = True if append is None else bool(append)
    try:
        result = longterm.write(scope, content, course_id, append=append)
    except ValueError as exc:
        return {"ok": False, "text": str(exc)}
    return {"ok": True, "text": f"已写入 {result['path']}（{result['bytes']} 字节，{'追加' if append else '替换'}）"}


def _memory_log(args: dict, course_id: str) -> dict:
    from . import longterm

    text = str(args.get("text") or "").strip()
    if not text:
        return {"ok": False, "text": "text 不能为空"}
    result = longterm.append_log(text, course_id)
    return {"ok": True, "text": f"已记到 {result['path']}"}


NL = chr(10)


# ── B 站：让 Agent 真的能去找课、拿字幕，而不是说「没有这个接口」 ──


def _bili_auth_cookie() -> str:
    from .bili_auth import get_auth
    from .config import Settings

    try:
        return get_auth(Settings.from_env().data_dir).cookie()
    except Exception:  # noqa: BLE001 - 没登录就匿名跑，别让工具起不来
        return ""


def _bili_client():
    from .bilibili import BilibiliClient

    return BilibiliClient(cookie=_bili_auth_cookie())


def _bili_status(_args: dict, _course_id: str) -> dict:
    from .bili_auth import get_auth
    from .config import Settings

    try:
        status = get_auth(Settings.from_env().data_dir).status()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "text": f"读不到 B 站登录态：{type(exc).__name__}: {exc}"}
    if status.get("loggedIn"):
        return {"ok": True, "text": f"B 站已登录（{status.get('uname') or '未取到昵称'}）。可以搜索课程、取 AI 字幕。"}
    return {"ok": True, "text": "B 站未登录。搜索可能被反爬拦截，AI 字幕取不到。请让用户到「B 站登录」扫码；"
                               "或者用浏览器扩展在视频页面导出字幕后，把文本交给 kemai.course_import_text 入库。"}


def _bili_search(args: dict, _course_id: str) -> dict:
    from .bilibili import BilibiliError

    keyword = str(args.get("keyword") or "").strip()
    if not keyword:
        return {"ok": False, "text": "keyword 不能为空"}
    limit = max(1, min(int(args.get("limit") or 6), 12))
    try:
        with _bili_client() as client:
            hits = client.search(keyword, limit=limit)
    except BilibiliError as exc:
        return {"ok": False, "text": f"B 站搜索失败：{exc}。先调 kemai.bili_status 看登录态；"
                                     "被反爬时请用户登录或稍后再试，不要编造课程。"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "text": f"B 站搜索异常：{type(exc).__name__}: {exc}"}
    if not hits:
        return {"ok": True, "text": f"没搜到与「{keyword}」相关的课程"}
    lines = []
    for hit in hits[:limit]:
        title = str(hit.get("title") or "").replace('<em class="keyword">', "").replace("</em>", "")
        lines.append(f"- {title} | BV {hit.get('bvid')} | UP {hit.get('author') or ''} | {(hit.get('description') or '')[:60]}")
    return {"ok": True, "text": "搜到 " + str(len(hits)) + " 门课：" + NL + NL.join(lines)}


def _bili_parts(args: dict, _course_id: str) -> dict:
    from .bilibili import BilibiliError

    bvid = str(args.get("bvid") or "").strip()
    if not bvid:
        return {"ok": False, "text": "bvid 不能为空"}
    try:
        with _bili_client() as client:
            video = client.view(bvid)
    except BilibiliError as exc:
        return {"ok": False, "text": f"读取视频失败：{exc}"}
    pages = list(getattr(video, "pages", ()) or ())

    def field(item, name, default=""):
        # BiliPage 是 dataclass；也兼容将来换成 dict 的情况
        return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)

    lines = [f"课程：{getattr(video, 'title', '')} | BV {bvid} | 共 {len(pages)} 讲"]
    for page in pages[:40]:
        lines.append(f"- 第 {field(page, 'page', '?')} 讲 | cid={field(page, 'cid')} | {field(page, 'part', '')}")
    return {"ok": True, "text": NL.join(lines)}


def _bili_transcript(args: dict, _course_id: str) -> dict:
    from .bilibili import BilibiliError

    bvid = str(args.get("bvid") or "").strip()
    try:
        cid = int(args.get("cid") or 0)
    except (TypeError, ValueError):
        cid = 0
    if not bvid or not cid:
        return {"ok": False, "text": "需要 bvid 和 cid（先调 kemai.bili_parts）"}
    limit = max(10, min(int(args.get("limit") or 120), 400))
    try:
        with _bili_client() as client:
            result = client.transcript(bvid, cid)
    except BilibiliError as exc:
        return {"ok": False, "text": f"取字幕失败：{exc}"}
    lines = result.get("lines") or []
    if not lines:
        diagnosis = result.get("diagnosis") or {}
        return {"ok": True, "text": "这一讲没有可用字幕。" + str(diagnosis)[:200] +
                                    "。AI 字幕需要 B 站登录；也可以让用户用浏览器扩展导出字幕文本，"
                                    "再交给 kemai.course_import_text 入库。"}
    picked = lines[:limit]
    rows = []
    for item in picked:
        seconds = int(item.get("from", 0) or 0)
        stamp = f"{seconds // 60:02d}:{seconds % 60:02d}"
        rows.append(f"[{stamp}] {item.get('text', '')}")
    body = NL.join(rows)[:6000]
    return {"ok": True, "text": f"第 {len(picked)}/{len(lines)} 行字幕（BV {bvid} / cid {cid}）：" + NL + body +
                               NL + NL + "要把这些字幕变成课程资料，调 kemai.course_import_text；"
                               "之后调 kemai.notes_build 生成逐章复习笔记。"}


def _course_import_text(args: dict, course_id: str) -> dict:
    """把字幕/讲义正文写成资料，之后 notes_build 才有依据。"""
    from .models import Document, new_id

    services = _require()
    text = str(args.get("text") or "").strip()
    title = str(args.get("title") or "导入资料").strip()[:80]
    if len(text) < 20:
        return {"ok": False, "text": "正文太短（少于 20 字），不入库"}
    document_id = new_id()
    path = services.upload_dir / f"{document_id}.md"
    path.write_text("# " + title + NL + NL + text, encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with services.sessions() as db:
        db.add(Document(id=document_id, course_id=course_id, filename=title + ".md",
                        kind="note", file_hash=digest, path=str(path)))
        services.changed(db, course_id)
        db.commit()
    try:
        services.ingest_document(document_id)
        state = "已解析并入库"
    except Exception as exc:  # noqa: BLE001 - 解析失败也要告诉模型资料已经落盘
        state = f"已落盘但解析失败（{type(exc).__name__}: {exc}）"
    return {"ok": True, "text": f"《{title}》{len(text)} 字{state}。现在可以调 kemai.notes_build 生成复习笔记。"}
