"""工作区：把 MCP 工具和 Markdown 技能包接到课脉上。

两个 API 族：
    /workspace/overview          一眼看全：连了哪些 MCP、有哪些 skill
    /workspace/mcp/call          手动调一个工具（**不经过模型**）
    /workspace/skills/{name}     按需读技能全文

为什么调用要「手动」：
MCP 工具能执行任意本地命令（跑 SPICE、烧 FPGA）。让模型自己决定调什么、
什么时候调，等于把一个能改机器的入口交给一段可能出错的自然语言推理。
所以第一版坚持「人来点」，模型只负责解释结果。等有了可信的确认流程再放开。
"""

from __future__ import annotations

import json
import logging
import threading

from fastapi import HTTPException
from pydantic import BaseModel, Field

from sqlalchemy import select

from . import agent_runtime, course_tools, longterm
from . import importers
from . import skills as skill_store
from .concierge import plan as build_plan
from .mcp_client import McpError, hub
from .models import Chapter, Course, Document

logger = logging.getLogger(__name__)


class McpCallInput(BaseModel):
    server: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: dict = Field(default_factory=dict)
    timeout: float = Field(default=45.0, ge=1, le=300)


class PlanInput(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    course_id: str = ""
    chapter_id: str = ""


class MemoryWriteInput(BaseModel):
    content: str = Field(min_length=1, max_length=200_000)
    course_id: str = ""


class SkillImportInput(BaseModel):
    text: str = ""
    path: str = ""
    overwrite: bool = False


class McpServerInput(BaseModel):
    name: str = ""
    command: str = ""
    args: list[str] | str = []
    env: dict[str, str] = {}
    url: str = ""
    disabled: bool = False


class McpImportInput(BaseModel):
    server: McpServerInput
    confirm: bool = False
    overwrite: bool = False


class AgentStartInput(BaseModel):
    goal: str = Field(min_length=1, max_length=4000)
    course_id: str = ""
    max_turns: int = Field(default=6, ge=1, le=12)
    # 同一个 conversation_id 的多次运行共享上下文；空串表示每次全新
    conversation_id: str = ""


class AgentResumeInput(BaseModel):
    run_id: str
    approve: bool = False
    note: str = ""


def register_workspace_routes(app, services) -> None:
    # 课程工具要查库，这里把 Services 交给它；不写进模块级全局变量是为了让测试能换掉
    course_tools.bind(services)
    @app.get("/workspace/overview")
    def workspace_overview(probe: bool = True):
        """MCP 服务器 + 技能清单。

        ``probe=false`` 只读配置、不启动子进程——界面先秒开，
        再由前端追一次 ``probe=true`` 把工具补上。
        否则每打开一次工作区都要等所有服务器握手完（实测 18 秒）。
        """
        try:
            mcp = hub().overview(probe=probe)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP 概览失败", exc_info=True)
            mcp = {"config_path": "", "servers": [], "error": f"{type(exc).__name__}: {exc}"}
        return {"mcp": mcp, "skills": skill_store.list_skills()}

    @app.post("/workspace/mcp/reload")
    def workspace_mcp_reload():
        config = hub().reload()
        return {"ok": True, "config_path": config["path"], "servers": sorted(config["servers"])}

    @app.post("/workspace/mcp/refresh")
    def workspace_mcp_refresh(server: str | None = None):
        if server:
            try:
                entry = hub().server(server)
                return {"server": server, "tools": entry.tools(refresh=True, timeout=20.0)}
            except McpError as exc:
                raise HTTPException(502, str(exc)) from exc
        hub().close_all()
        return {"ok": True}

    @app.post("/workspace/mcp/call")
    def workspace_mcp_call(payload: McpCallInput):
        try:
            entry = hub().server(payload.server)
            result = entry.call(payload.tool, payload.arguments, timeout=payload.timeout)
        except McpError as exc:
            raise HTTPException(502, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 工具内部炸了也要把原因带回去
            logger.warning("MCP 调用失败", exc_info=True)
            raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc
        # 结果可能很大，界面只展示前 20000 字，但要如实告诉用户被截断了
        text = result["text"]
        truncated = len(text) > 20000
        return {
            "is_error": result["is_error"],
            "text": text[:20000],
            "truncated": truncated,
            "chars": len(text),
        }

    @app.post("/concierge/plan")
    def concierge_plan(payload: PlanInput):
        """一句话 → 一串动作。只出计划，不动手。

        章节和资料状态从数据库现读，避免前端传错参数——
        调度层出错最贵的代价是「在错误的章节上跑了一次生成」。
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
        if payload.course_id:
            with services.sessions() as db:
                course = db.get(Course, payload.course_id)
                if course is None:
                    raise HTTPException(404, "课程不存在")
                context["course_name"] = course.name
                # 生笔记/出题都要求有已解析的资料，否则后端会 409——
                # 与其让用户点了执行才看到报错，不如在计划阶段就说清楚
                ready_docs = db.scalars(
                    select(Document.id).where(
                        Document.course_id == payload.course_id,
                        Document.status == "ready",
                        Document.kind.in_(["lecture", "note"]),
                    ).limit(1)
                ).all()
                context["has_material"] = bool(ready_docs)
                context["has_deck"] = bool(db.scalars(
                    select(Document.id).where(
                        Document.course_id == payload.course_id,
                        Document.status == "ready",
                        Document.kind == "lecture",
                    ).limit(1)
                ).all())
                chapters = db.scalars(
                    select(Chapter).where(Chapter.course_id == payload.course_id)
                    .order_by(Chapter.position)
                ).all()
                context["chapters"] = [
                    {"id": c.id, "title": c.title, "index": index + 1}
                    for index, c in enumerate(chapters)
                ]
                newest = db.scalar(
                    select(Document).where(
                        Document.course_id == payload.course_id,
                        Document.kind == "lecture",
                        Document.status == "ready",
                    ).order_by(Document.created_at.desc())
                )
                if newest is not None:
                    context["document_id"] = newest.id
        return build_plan(payload.message, context)

    # ── 导入：技能包与 MCP 服务器 ─────────────────────────
    def _fail(exc: Exception) -> HTTPException:
        return HTTPException(422, str(exc))

    @app.post("/workspace/skills/import")
    def import_skill(payload: SkillImportInput):
        """导入一份技能：粘贴 SKILL.md 正文，或给一个本地目录。

        目录导入会把 references/、脚本一起搬走——只搬 SKILL.md 的话，
        正文里写的 references/xxx.md 全成了死链。
        """
        try:
            if payload.path.strip():
                return importers.import_skill_dir(payload.path.strip(), payload.overwrite)
            if not payload.text.strip():
                raise importers.ImportError_("请粘贴 SKILL.md 的内容，或填一个目录路径")
            return importers.import_skill(payload.text, payload.overwrite)
        except importers.ImportError_ as exc:
            raise _fail(exc) from exc

    @app.delete("/workspace/skills/{name}", status_code=204)
    def remove_skill(name: str):
        try:
            importers.delete_skill(name)
        except importers.ImportError_ as exc:
            raise _fail(exc) from exc

    @app.post("/workspace/mcp/preview")
    def preview_mcp(payload: McpServerInput):
        """只看不装：返回将要执行的命令行原文。

        界面在用户点「导入」之前必须显示这一行——MCP 服务器最终是拿这条命令
        起的子进程，不给他看就等于让他盲签。
        """
        try:
            normalized = importers.normalize_server(payload.model_dump())
        except importers.ImportError_ as exc:
            raise _fail(exc) from exc
        return {
            "name": normalized["name"],
            "command": importers.preview_command(normalized["server"]),
            "server": normalized["server"],
            "warning": "导入后，这条命令会在这台机器上长期运行。",
        }

    @app.post("/workspace/mcp/test")
    def test_mcp(payload: McpServerInput):
        """真起一次子进程，握手并拉工具列表。连不上就别导入了。"""
        return importers.test_mcp_server(payload.model_dump())

    @app.post("/workspace/mcp/servers")
    def import_mcp(payload: McpImportInput):
        try:
            return importers.import_mcp_server(
                payload.server.model_dump(), confirm=payload.confirm, overwrite=payload.overwrite
            )
        except importers.ImportError_ as exc:
            raise _fail(exc) from exc

    @app.delete("/workspace/mcp/servers/{name}")
    def remove_mcp(name: str):
        try:
            return importers.delete_mcp_server(name)
        except importers.ImportError_ as exc:
            raise _fail(exc) from exc

    # ── Agent：模型自己选工具、自己调 ─────────────────────
    @app.post("/agent/run")
    def agent_run(payload: AgentStartInput):
        """一次性版：内部就是消费 stream 的事件，跑完把最终状态返回。

        不另写一套循环——同步和流式共用 _turn，否则改了一边忘了另一边，
        表现就是"界面里会停下来的工具，脚本调用时不问就跑了"。
        """
        from . import agent_runtime as runtime

        run_id = ""
        pending = None
        answer = ""
        for event in runtime.stream(
            services.llm, payload.goal, payload.course_id,
            payload.max_turns, payload.conversation_id,
        ):
            if event["type"] == "run":
                run_id = event["run_id"]
            elif event["type"] == "blocked":
                pending = event["pending"]
            elif event["type"] == "final":
                answer = event["answer"]
            elif event["type"] == "error":
                answer = event["message"]
        try:
            return runtime.get_run(run_id).public()
        except KeyError:
            return {"id": run_id, "status": "failed", "answer": answer, "steps": [], "pending": pending}

    @app.post("/agent/stream")
    def agent_stream(payload: AgentStartInput):
        """流式版：每一步结束就推一行 SSE。

        同步那版 4 轮要 85 秒，界面只能转圈。这条路径让用户看到
        "它先查了什么、为什么换了工具"——过程本身就是可核对性的一部分。
        """
        from fastapi.responses import StreamingResponse

        def events():
            def pack(event: dict) -> str:
                # SSE 要求每个事件以空行结束，用显式换行拼，别依赖 f-string 里的转义
                return "data: " + json.dumps(event, ensure_ascii=False) + chr(10) + chr(10)

            try:
                for event in agent_runtime.stream(
                    services.llm, payload.goal, payload.course_id,
                    payload.max_turns, payload.conversation_id,
                ):
                    yield pack(event)
            except Exception as exc:  # noqa: BLE001 - 出错也要让前端收到一条可读的消息
                logger.exception("agent 流式中断")
                yield pack({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            yield pack({"type": "end"})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/agent/resume")
    def agent_resume(payload: AgentResumeInput):
        """用户点了「允许」或「拒绝」，接着跑。"""
        try:
            return agent_runtime.resume(services.llm, payload.run_id, payload.approve, payload.note)
        except KeyError as exc:
            raise HTTPException(404, "这次运行已经过期了，重新发起一次") from exc

    @app.get("/agent/runs/{run_id}")
    def agent_status(run_id: str):
        try:
            return agent_runtime.get_run(run_id).public()
        except KeyError as exc:
            raise HTTPException(404, "这次运行已经过期了") from exc

    @app.get("/agent/tools")
    def agent_tools(probe: bool = False):
        """模型能用的工具清单（界面展示用，也用来解释它为什么选了某个）。"""
        return {"tools": agent_runtime.tool_catalog(probe=probe)}

    # ── 长期记忆：文件在哪、写了什么 ──────────────────────
    @app.get("/memory")
    def memory_inventory():
        """记忆都存在磁盘上，这里只列清单，界面点开再看内容。"""
        return {"root": str(longterm.root()), "files": longterm.inventory()}

    @app.get("/memory/file")
    def memory_read_file(name: str):
        try:
            return {"name": name, "content": longterm.read_file(name), "path": str(longterm.resolve(name))}
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.put("/memory/file")
    def memory_write_file(name: str, payload: MemoryWriteInput):
        try:
            return longterm.write_file(name, payload.content)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/memory/log")
    def memory_log(payload: MemoryWriteInput):
        return longterm.append_log(payload.content, payload.course_id)

    @app.get("/memory/digest")
    def memory_digest(course_id: str = ""):
        """模型实际看到的那段记忆摘要——排查"它怎么不记得"时看这个。"""
        return {"digest": longterm.digest(course_id)}

    @app.get("/workspace/skills")
    def workspace_skills(q: str = ""):
        if q.strip():
            return {"skills": skill_store.match_skills(q), "query": q}
        return {"skills": skill_store.list_skills()}

    @app.get("/workspace/skills/{name}")
    def workspace_skill_detail(name: str):
        try:
            return skill_store.load_skill(name)
        except KeyError as exc:
            raise HTTPException(404, f"没有名为 {name} 的技能") from exc


def close_workspace() -> None:
    """应用退出时收掉所有 MCP 子进程，避免留下孤儿占端口/句柄。"""
    try:
        hub().close_all()
    except Exception:  # noqa: BLE001
        logger.warning("关闭 MCP 服务器失败", exc_info=True)


def warmup_workspace() -> None:
    """后端启动后在后台把 MCP 工具列表预先拉一遍。

    否则进程重启后的第一次打开工作区要等 20 秒（两个服务器握手）。
    用守护线程跑，不阻塞 /health，界面该秒开还是秒开。
    """
    def run() -> None:
        try:
            hub().overview(probe=True)
            logger.info("MCP 预热完成")
        except Exception:  # noqa: BLE001 - 预热失败不该影响启动
            logger.warning("MCP 预热失败", exc_info=True)

    threading.Thread(target=run, name="mcp-warmup", daemon=True).start()
