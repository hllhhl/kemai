"""任务：以"我让它干了什么"为单位组织历史。

和现有概念的关系：
    ChatSession  一次对话（问答留痕）
    Job          一种后台作业的内部状态（笔记生成跑了几成）
    Task         用户视角的一次委托，可能包含多个动作

执行仍然由前端调各自端点完成，这里只负责**记下委托是什么、计划长什么样、
最后成没成**。好处是不重复实现校验逻辑；代价是任务状态靠前端回写，
所以 PATCH 必须校验状态迁移，防止前端把 failed 写成 done。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from .concierge import plan as build_plan
from .sentinel import RISK, sentinel
from .models import (Chapter, Course, Document, Outline, PracticeQuestion, PracticeSet,
                     Task, new_id)

logger = logging.getLogger(__name__)

STATUSES = {"planned", "running", "done", "failed", "cancelled"}
# 只允许往前推，不能从终态回退
TRANSITIONS = {
    "planned": {"running", "cancelled", "failed"},
    "running": {"done", "failed", "cancelled"},
    "done": set(),
    "failed": {"running"},   # 允许重试
    "cancelled": {"running"},
}


class TaskInput(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    course_id: str
    chapter_id: str = ""


class TaskPatch(BaseModel):
    status: str | None = None
    result: list[dict] | None = None
    title: str | None = Field(default=None, max_length=200)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _title(message: str) -> str:
    """任务标题就是那句话本身，太长截断——用户认的是自己说过的话。"""
    text = " ".join(message.split())
    return text if len(text) <= 42 else text[:42] + "…"


def _public(task: Task) -> dict:
    try:
        plan = json.loads(task.plan or "{}")
    except ValueError:
        plan = {}
    try:
        result = json.loads(task.result or "[]")
    except ValueError:
        result = []
    return {
        "id": task.id,
        "course_id": task.course_id,
        "title": task.title,
        "message": task.message,
        "status": task.status,
        "plan": plan,
        "result": result,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def register_task_routes(app, services) -> None:
    @app.get("/tasks")
    def list_tasks(course_id: str = "", limit: int = 40):
        limit = max(1, min(limit, 200))
        with services.sessions() as db:
            query = select(Task)
            if course_id:
                query = query.where(Task.course_id == course_id)
            query = query.order_by(Task.created_at.desc()).limit(limit)
            return [_public(task) for task in db.scalars(query)]

    @app.post("/tasks", status_code=201)
    def create_task(payload: TaskInput):
        """把一句话变成任务：解析意图 → 存计划。

        计划在这里就固化下来。后面执行时如果课程资料变了，
        用户看到的仍然是当初答应他的那套动作，不会悄悄变成别的。
        """
        context: dict = {
            "course_id": payload.course_id,
            "chapter_id": payload.chapter_id,
            "chapters": [],
            "document_id": "",
            "course_name": "",
            "has_material": False,
            "has_deck": False,
            "model_ready": bool(getattr(services.llm, "configured", False)),
        }
        with services.sessions() as db:
            course = db.get(Course, payload.course_id)
            if course is None:
                raise HTTPException(404, "课程不存在")
            context["course_name"] = course.name
            chapters = db.scalars(
                select(Chapter).where(Chapter.course_id == payload.course_id).order_by(Chapter.position)
            ).all()
            context["chapters"] = [
                {"id": c.id, "title": c.title, "index": i + 1} for i, c in enumerate(chapters)
            ]
            context["has_material"] = bool(db.scalars(
                select(Document.id).where(
                    Document.course_id == payload.course_id,
                    Document.status == "ready",
                    Document.kind.in_(["lecture", "note"]),
                ).limit(1)
            ).all())
            context["has_deck"] = bool(db.scalars(
                select(Document.id).where(
                    Document.course_id == payload.course_id,
                    Document.status == "ready",
                    Document.kind == "lecture",
                ).limit(1)
            ).all())
            # 已有产物 → 这次执行是覆盖，哨兵会抬成"先问一句"
            context["has_outline"] = bool(db.scalars(
                select(Outline.id).where(
                    Outline.course_id == payload.course_id,
                    Outline.status.in_(("completed", "ready")),
                ).limit(1)
            ).all())
            context["has_practice"] = bool(db.scalars(
                select(PracticeSet.document_id).where(
                    PracticeSet.document_id.in_(
                        select(Document.id).where(Document.course_id == payload.course_id)
                    )
                ).limit(1)
            ).all())
            newest = db.scalar(
                select(Document).where(
                    Document.course_id == payload.course_id,
                    Document.kind == "lecture",
                    Document.status == "ready",
                ).order_by(Document.created_at.desc())
            )
            if newest is not None:
                context["document_id"] = newest.id

            plan = build_plan(payload.message, context)
            task = Task(
                id=new_id(),
                course_id=payload.course_id,
                title=_title(payload.message),
                message=payload.message,
                status="planned",
                plan=json.dumps(plan, ensure_ascii=False),
                result="[]",
            )
            db.add(task)
            db.commit()
            return _public(task)

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str):
        with services.sessions() as db:
            task = db.get(Task, task_id)
            if task is None:
                raise HTTPException(404, "任务不存在")
            return _public(task)

    @app.patch("/tasks/{task_id}")
    def patch_task(task_id: str, payload: TaskPatch):
        with services.sessions() as db:
            task = db.get(Task, task_id)
            if task is None:
                raise HTTPException(404, "任务不存在")
            if payload.status is not None:
                if payload.status not in STATUSES:
                    raise HTTPException(422, f"未知状态：{payload.status}")
                if payload.status != task.status and payload.status not in TRANSITIONS.get(task.status, set()):
                    raise HTTPException(409, f"任务状态不能从 {task.status} 变成 {payload.status}")
                task.status = payload.status
            if payload.result is not None:
                task.result = json.dumps(payload.result, ensure_ascii=False)
            if payload.title is not None:
                task.title = payload.title
            task.updated_at = _now()
            db.commit()
            return _public(task)

    @app.post("/tasks/{task_id}/verify")
    def verify_task(task_id: str):
        """核对产物：HTTP 202 只说明「已受理」，不说明「做成了」。

        之前任务一拿到 202 就标 done，于是模型报错、章节没确认、生成中途失败
        这些情况在界面上全都显示成「已完成」——这是最坏的一种谎：
        用户以为笔记有了，点进去是空的。

        这里不信任前端回写的状态，直接查库：
            笔记   Outline.status == ready 且 markdown 有实质内容
            题目   该课程下 PracticeQuestion 的条数
            找课   本地没有可核对的落点，如实说 unknown
        """
        with services.sessions() as db:
            task = db.get(Task, task_id)
            if task is None:
                raise HTTPException(404, "任务不存在")
            plan = json.loads(task.plan or "{}")
            result = json.loads(task.result or "[]")
            done_labels = {r.get("label") for r in result if r.get("state") == "done"}

            items: list[dict] = []
            for action in plan.get("actions", []):
                if action.get("label") not in done_labels:
                    continue
                items.append(_check_action(db, task.course_id, action))

            summary = "verified"
            if any(i["verdict"] == "contradicted" for i in items):
                summary = "contradicted"
            elif any(i["verdict"] == "pending" for i in items):
                summary = "pending"
            elif any(i["verdict"] == "unknown" for i in items):
                summary = "unknown"
            if not items:
                summary = "nothing"

            # 只有「核对过且对不上」才把任务判失败；查不到不算失败
            if summary == "contradicted" and task.status == "running":
                task.status = "failed"
                task.updated_at = _now()
                db.commit()
            return {"summary": summary, "items": items, "task": _public(task)}

    # ── 哨兵：执行前的放行/拦截 ──────────────────────────
    class GrantInput(BaseModel):
        action_type: str
        scope: str = "session"

    class ModeInput(BaseModel):
        mode: str

    @app.get("/sentinel")
    def get_sentinel():
        """当前模式与已授权的动作类型，界面据此显示"这次不问了"。"""
        return {"config": sentinel().snapshot(), "risks": RISK}

    @app.post("/sentinel/grant")
    def grant_sentinel(payload: GrantInput):
        try:
            return {"config": sentinel().grant(payload.action_type, payload.scope)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/sentinel/revoke")
    def revoke_sentinel(payload: GrantInput):
        try:
            return {"config": sentinel().revoke(payload.action_type)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.put("/sentinel/mode")
    def set_sentinel_mode(payload: ModeInput):
        try:
            sentinel().set_mode(payload.mode)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"config": sentinel().snapshot()}

    @app.delete("/tasks/{task_id}", status_code=204)
    def delete_task(task_id: str):
        with services.sessions() as db:
            task = db.get(Task, task_id)
            if task is None:
                raise HTTPException(404, "任务不存在")
            db.delete(task)
            db.commit()

def _check_action(db, course_id: str, action: dict) -> dict:
    """一条动作到底做成了没有。只查库，不问模型，也不信前端的回写。"""
    kind = action.get("type")
    label = action.get("label", "")
    base = {"label": label, "type": kind, "verdict": "unknown", "detail": ""}

    if kind == "notes":
        outline = db.scalar(
            select(Outline).where(Outline.course_id == course_id).order_by(Outline.created_at.desc())
        )
        if outline is None:
            return {**base, "detail": "库里没有笔记记录"}
        if outline.status in ("completed", "ready") and len((outline.markdown or "").strip()) >= 200:
            return {**base, "verdict": "verified", "detail": f"笔记已生成 {len(outline.markdown)} 字"}
        if outline.status in ("queued", "running", "processing"):
            return {**base, "verdict": "pending", "detail": f"还在生成（{outline.status}）"}
        return {**base, "verdict": "contradicted", "detail": f"笔记状态是 {outline.status}，正文 {len(outline.markdown or '')} 字"}

    if kind == "practice":
        wanted = int((action.get("body") or {}).get("count") or 0)
        rows = db.scalars(
            select(PracticeQuestion.id).where(
                PracticeQuestion.document_id.in_(
                    select(Document.id).where(Document.course_id == course_id)
                )
            ).limit(500)
        ).all()
        got = len(rows)
        if got == 0:
            # 报过 done 却一道题都没有：这是「说了没做」，不是「查不到」
            return {**base, "verdict": "contradicted", "detail": "报完成，但题库里一道题都没有"}
        if wanted and got < wanted:
            return {**base, "verdict": "pending", "detail": f"出了 {got} 道，目标是 {wanted} 道"}
        return {**base, "verdict": "verified", "detail": f"题库里有 {got} 道题"}

    if kind == "finder":
        return {**base, "detail": "找课结果在界面上，本地没有可核对的落点"}

    if kind == "answer":
        return {**base, "detail": "回答由对话产生，不在任务里核对"}

    return {**base, "detail": "这类动作没有可核对的产物"}
