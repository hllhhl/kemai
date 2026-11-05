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

from . import skills as skill_store
from .mcp_client import McpError, hub

logger = logging.getLogger(__name__)


class McpCallInput(BaseModel):
    server: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: dict = Field(default_factory=dict)
    timeout: float = Field(default=45.0, ge=1, le=300)


def register_workspace_routes(app) -> None:
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
