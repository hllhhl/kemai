"""Agent 运行时：让模型自己挑工具、自己调，但每一步都过哨兵。

这一层补的是课脉最缺的东西。之前它是「你说一句话 → 我拆成固定几步 → 你点确认」，
本质是个表单。真正的 agent 得有循环：看当前状态 → 选一个动作 → 执行 → 看结果 →
再决定下一步，直到能回答或者卡住。

用的是**文本协议**而不是原生 function calling：课脉的模型走的是自建 bridge，
不同网关对 `tools` 字段的支持参差不齐，一旦某个网关不认就直接哑火。
让模型输出一个 JSON 动作再自己解析，慢一点，但在任何 OpenAI 兼容端点上都能跑。

循环里三条硬约束（都是照着会出事的地方设的）：

1. **轮数上限**。没有上限的 agent 会在两个工具之间来回横跳烧光额度。
2. **每次调用前过哨兵**。模型的判断可能错，删东西这种事不能只靠它自觉；
   需要确认时就停下来等人，而不是"先做了再说"。
3. **工具输出截断后才喂回模型**。一个 300KB 的日志塞进上下文，
   下一次调用就会超预算然后整轮报废。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from . import course_tools
from .mcp_client import McpError, hub
from .sentinel import ALLOW, ASK, DENY, sentinel

logger = logging.getLogger(__name__)

MAX_TURNS = 6
MAX_OBSERVATION = 4000      # 单个工具输出喂回模型的上限（字符）
MAX_HISTORY = 12            # 保留最近几条观察，防止上下文越滚越大

SYSTEM = """你是一个会用工具的课程助手。每一步只输出一个 JSON 对象，不要输出别的文字。

可用动作：
{"action": "skill_list"}
{"action": "skill_use", "name": "技能名"}
{"action": "tool", "tool": "服务器.工具名", "args": {...}}
{"action": "final", "answer": "给用户的回答"}

调工具的完整形状（tool 必须写全 "服务器.工具"，args 必须是对象）：
{"action": "tool", "tool": "kemai.course_brief", "args": {}}

注意：action 只能是上面五个值之一，"kemai.course_brief" 这类是 tool 的名字，不是 action。

规则：
- 先想清楚缺什么信息，再决定调哪个工具；能直接回答就别调。
- 工具失败时看错误信息决定换一个还是直接告诉用户，不要重复同一个调用。
- 回答用中文，写明依据来自哪个工具的输出；工具没给的信息不要编。
- 不确定时用 final 说明缺什么，不要硬凑。

可用工具清单：
__TOOLS__

可用技能：
__SKILLS__
"""


class AgentRun:
    """一次 agent 循环的状态。要能被"暂停等人确认"，所以状态得留在服务端。"""

    def __init__(self, goal: str, course_id: str = "", max_turns: int = MAX_TURNS):
        self.id = uuid.uuid4().hex
        self.goal = goal
        self.course_id = course_id
        self.max_turns = max_turns
        self.messages: list[dict] = []
        self.steps: list[dict] = []
        self.status = "running"          # running | done | blocked | failed
        self.answer = ""
        self.pending: dict | None = None  # 等待确认的那一次调用
        self.turn = 0
        self.created_at = time.time()

    def public(self) -> dict:
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status,
            "answer": self.answer,
            "steps": self.steps,
            "pending": self.pending,
            "turn": self.turn,
            "max_turns": self.max_turns,
        }


_runs: dict[str, AgentRun] = {}
_RUN_TTL = 3600


def _gc() -> None:
    now = time.time()
    for run_id, run in list(_runs.items()):
        if now - run.created_at > _RUN_TTL:
            _runs.pop(run_id, None)


def get_run(run_id: str) -> AgentRun:
    run = _runs.get(run_id)
    if run is None:
        raise KeyError(run_id)
    return run


# ── 工具清单 ──────────────────────────────────────────────


def tool_catalog(probe: bool = False) -> list[dict]:
    """把 MCP 工具整理成给模型看的清单。

    ``probe=False`` 时不启动子进程——只有已经在缓存里的服务器才会出现。
    否则第一次进页面就要等十几秒握手。
    """
    catalog: list[dict] = []
    try:
        overview = hub().overview(probe=probe)
    except Exception:  # noqa: BLE001
        logger.warning("读取 MCP 清单失败", exc_info=True)
        return catalog

    for server in overview.get("servers", []):
        if server.get("disabled") or server.get("error"):
            continue
        for tool in server.get("tools", []):
            catalog.append({
                "name": f"{server['name']}.{tool['name']}",
                "server": server["name"],
                "tool": tool["name"],
                "description": (tool.get("description") or "")[:160],
                "read_only": bool(tool.get("read_only")),
                "destructive": bool(tool.get("destructive")),
                "schema": tool.get("schema") or {},
            })

    # 课程工具和 MCP 工具放在同一张清单里：模型不用知道哪个是外挂、哪个是内置，
    # 它只需要知道"想查课程结构该调哪个"。
    catalog.extend(course_tools.catalog())
    return catalog


def _skill_lines() -> list[str]:
    try:
        from . import skills as skill_store

        return [f"- {s['name']}: {s['description'][:90]}" for s in skill_store.list_skills()[:20]]
    except Exception:  # noqa: BLE001
        return []


def _system_prompt() -> str:
    catalog = tool_catalog(probe=False)
    tools = "\n".join(
        f"- {t['name']}{'（只读）' if t['read_only'] else ''}: {t['description']}" for t in catalog
    ) or "（当前没有已连接的 MCP 工具）"
    skills = "\n".join(_skill_lines()) or "（没有技能）"
    # 不能用 str.format：提示词里的 JSON 示例带花括号，会被当成占位符
    return SYSTEM.replace("__TOOLS__", tools).replace("__SKILLS__", skills)


# ── 执行一个工具调用 ──────────────────────────────────────


def _assess(tool_name: str, catalog: list[dict]) -> dict:
    """这一调用该不该问人。

    判定用的是哨兵那套，但风险来自两处：MCP 自己标的 annotations，
    以及动作类型。两边取更谨慎的那个——一个标了 destructive 的工具
    不能因为"我们没登记过这种动作"就当成 moderate 放过去。
    """
    entry = next((t for t in catalog if t["name"] == tool_name), None)
    if entry is None:
        return {"risk": "sensitive", "decision": DENY, "reason": "这个工具不在已连接清单里"}

    kind = "delete" if entry["destructive"] else ("finder" if entry["read_only"] else "practice")
    verdict = sentinel().assess({"type": kind})
    if tool_name.startswith("kemai."):
        # 内置工具的风险是固定的（写死在这里），不像 MCP 那样靠 annotations 自报
        verdict = {
            **verdict,
            "reason": "只读课程数据" if entry["read_only"] else "会生成文件，但不动已有内容",
        }
    if entry["destructive"]:
        verdict = {**verdict, "risk": "sensitive", "decision": ASK, "reason": "这个工具会改动或删除内容"}
    return {**verdict, "read_only": entry["read_only"]}


def call_tool(tool_name: str, args: dict, timeout: float = 60.0) -> dict:
    """实际执行。工具名是 ``server.tool``，但服务器名里可能带点，所以按最后一个点切。"""
    if "." not in tool_name:
        return {"ok": False, "text": f"工具名要写成 服务器.工具，收到的是 {tool_name}"}
    server_name, tool = tool_name.rsplit(".", 1)
    try:
        result = hub().server(server_name).call(tool, args, timeout=timeout)
    except McpError as exc:
        return {"ok": False, "text": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "text": f"{type(exc).__name__}: {exc}"}

    content = result.get("content") or []
    texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    text = "\n".join(texts) or json.dumps(result, ensure_ascii=False)[:MAX_OBSERVATION]
    return {"ok": not result.get("isError"), "text": text}


# ── 循环 ─────────────────────────────────────────────────


def start(llm, goal: str, course_id: str = "", max_turns: int = MAX_TURNS) -> dict:
    _gc()
    run = AgentRun(goal, course_id, max_turns)
    run.messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": goal},
    ]
    _runs[run.id] = run
    return advance(run, llm)


def _turn(run: AgentRun, llm) -> str:
    """跑一轮：问模型 → 执行一个动作。返回 continue / done / blocked / failed。

    抽出来是为了让「一次跑完」和「边跑边报」共用同一套判定。
    两套实现迟早会走偏，而 agent 循环走偏一次就是调错工具。
    """
    from .llm import parse_json

    run.turn += 1
    try:
        raw = llm.complete(run.messages, json_mode=True)
        decision = parse_json(raw)
    except Exception as exc:  # noqa: BLE001
        run.status = "failed"
        run.answer = f"模型这一步没给出可用结果：{type(exc).__name__}: {exc}"
        return "failed"

    action = str(decision.get("action") or "").strip()
    run.steps.append({"turn": run.turn, "kind": action or "unknown",
                      "detail": str(decision.get("thought") or "")[:200]})

    if action == "final":
        run.status = "done"
        run.answer = str(decision.get("answer") or "").strip() or "（模型没有给出回答）"
        return "done"

    if action == "skill_list":
        from . import skills as skill_store

        lines = [f"{s['name']}: {s['description']}" for s in skill_store.list_skills()]
        _observe(run, "技能清单", "\n".join(lines) or "没有技能")
        return "continue"

    if action == "skill_use":
        name = str(decision.get("name") or "").strip()
        try:
            from . import skills as skill_store

            skill = skill_store.load_skill(name)
            _observe(run, f"技能 {name}", skill.get("body", "")[:MAX_OBSERVATION])
        except KeyError:
            _observe(run, f"技能 {name}", "没有这个技能")
        return "continue"

    if action == "tool":
        tool_name = str(decision.get("tool") or "").strip()
        args = decision.get("args") or {}
        if not isinstance(args, dict):
            _observe(run, tool_name, "args 必须是对象")
            return "continue"

        catalog = tool_catalog(probe=True)
        verdict = _assess(tool_name, catalog)
        # reason 也要留痕：界面要能回答"为什么这一步被拒了"
        run.steps[-1].update({"tool": tool_name, "args": _mask(args),
                              "risk": verdict["risk"], "decision": verdict["decision"],
                              "reason": verdict.get("reason", "")})

        if verdict["decision"] == DENY:
            _observe(run, tool_name, f"被拒绝：{verdict['reason']}")
            return "continue"
        if verdict["decision"] == ASK:
            run.status = "blocked"
            run.pending = {"tool": tool_name, "args": args,
                           "risk": verdict["risk"], "reason": verdict["reason"]}
            return "blocked"

        if tool_name.startswith("kemai."):
            result = course_tools.call(tool_name, args, run.course_id)
        else:
            result = call_tool(tool_name, args)
        run.steps[-1].update({"ok": result["ok"], "output": result["text"][:400]})
        _observe(run, tool_name, result["text"])
        return "continue"

    # 模型没按协议来：把规则再说一遍，让它下一轮改
    _observe(run, "协议", f'无法识别的 action：{action or "(空)"}。只能输出 skill_list / skill_use / tool / final。')
    return "continue"


def _close_out(run: AgentRun) -> None:
    """轮数用完还没结论时，给一句诚实的话，别装作完成了。"""
    if run.status == "running":
        run.status = "done"
        run.answer = run.answer or f"已经走了 {run.max_turns} 步还没得出结论。要接着做，把问题问得更具体一点。"


def advance(run: AgentRun, llm) -> dict:
    """一次跑到底：问模型 → 执行或停下。返回当前状态。"""
    if not getattr(llm, "configured", False):
        run.status = "failed"
        run.answer = "还没有配置模型，先到右上角「模型配置」填一个。"
        return run.public()

    while run.status == "running" and run.turn < run.max_turns:
        outcome = _turn(run, llm)
        if outcome in ("done", "blocked", "failed"):
            return run.public()

    _close_out(run)
    return run.public()


def stream(llm, goal: str, course_id: str = "", max_turns: int = MAX_TURNS):
    """边跑边报：每一步结束就吐一个事件。

    同步跑 4 轮要 85 秒，界面只能转圈。流式之后用户能看到它先查了什么、
    为什么换了个工具——这比最后那句回答更能建立信任。
    """
    _gc()
    run = AgentRun(goal, course_id, max_turns)
    run.messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": goal},
    ]
    _runs[run.id] = run
    yield {"type": "run", "run_id": run.id, "max_turns": run.max_turns}

    if not getattr(llm, "configured", False):
        run.status = "failed"
        run.answer = "还没有配置模型，先到右上角「模型配置」填一个。"
        yield {"type": "error", "message": run.answer}
        return

    while run.status == "running" and run.turn < run.max_turns:
        outcome = _turn(run, llm)
        if run.steps:
            yield {"type": "step", "step": run.steps[-1]}
        if outcome == "blocked":
            yield {"type": "blocked", "pending": run.pending}
            return
        if outcome == "failed":
            yield {"type": "error", "message": run.answer}
            return
        if outcome == "done":
            yield {"type": "final", "answer": run.answer}
            return

    _close_out(run)
    yield {"type": "final", "answer": run.answer}


def _mask(args: dict) -> dict:
    """展示用的参数：值可能是密钥，只留形状。"""
    masked = {}
    for key, value in list(args.items())[:12]:
        if isinstance(value, str):
            masked[key] = f"str[{len(value)}]"
        elif isinstance(value, (int, float, bool)) or value is None:
            masked[key] = value
        else:
            masked[key] = type(value).__name__
    return masked


def _observe(run: AgentRun, source: str, text: str) -> None:
    body = text if len(text) <= MAX_OBSERVATION else text[:MAX_OBSERVATION] + "\n…（输出过长，已截断）"
    run.messages.append({"role": "user", "content": f"[工具结果：{source}]\n{body}"})
    # 只留最近的观察：历史越长，模型越容易翻旧账而忽略当前状态
    turn_messages = [m for m in run.messages if m["role"] != "system"]
    if len(turn_messages) > MAX_HISTORY:
        drop = len(turn_messages) - MAX_HISTORY
        kept, removed = [], 0
        for message in run.messages:
            if message["role"] != "system" and removed < drop:
                removed += 1
                continue
            kept.append(message)
        run.messages = kept


def resume(llm, run_id: str, approve: bool, note: str = "") -> dict:
    """用户在界面上点了「允许」或「拒绝」之后继续。"""
    run = get_run(run_id)
    if run.status != "blocked" or not run.pending:
        return run.public()

    pending = run.pending
    run.pending = None
    run.status = "running"

    if not approve:
        _observe(run, pending["tool"], f"用户没有同意这次调用：{note or '拒绝了'}")
        return advance(run, llm)

    result = call_tool(pending["tool"], pending["args"])
    if run.steps:
        run.steps[-1].update({"ok": result["ok"], "output": result["text"][:400], "approved": True})
    _observe(run, pending["tool"], result["text"])
    return advance(run, llm)
