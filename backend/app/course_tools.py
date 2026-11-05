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

import logging
from typing import Any

from sqlalchemy import func, select

from .models import (
    Chapter,
    Course,
    Document,
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
        "kemai.course_brief": _brief,
        "kemai.notes_read": _notes,
        "kemai.practice_read": _practice,
        "kemai.wrong_read": _wrong,
        "kemai.docs_export": _export,
    }.get(name)
    if handler is None:
        return {"ok": False, "text": f"没有这个课程工具：{name}"}
    if not course_id:
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
    return {"ok": True, "text": prefix + text}


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
