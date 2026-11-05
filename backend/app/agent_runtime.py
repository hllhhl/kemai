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
import re
import time
import uuid
from pathlib import Path
from typing import Any

from . import audit, course_tools
from .mcp_client import McpError, hub, tool_allowed
from .sentinel import ALLOW, ASK, DENY, sentinel

logger = logging.getLogger(__name__)

MAX_TURNS = 6
MAX_OBSERVATION = 4000      # 单个工具输出喂回模型的上限（字符）
MAX_HISTORY = 12            # 兼容旧引用（fit_context 后不再按条数删）
CONTEXT_BUDGET = 14000      # 消息总字符预算；超了按 fit_context 分级压缩

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
- **不要问用户"要不要我查一下"**：能查就直接查，查完再说结论。只有真的缺关键信息（比如没说清是哪一章）才问。
- 数量、字数、条数这类信息一律引用工具返回的数字，不确定就说"没查到"，不要估算。
- **长期记忆已经附在本提示词末尾**（我是谁 / 怎么做 / 关于用户 / 跨课程 / 这门课）。
  回答"我习惯怎样""我是谁"这类问题时直接引用它，不要为此调用 memory_read；
  只有需要某个文件全文时才读。
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

    def __init__(self, goal: str, course_id: str = "", max_turns: int = MAX_TURNS,
                 conversation_id: str = ""):
        self.id = uuid.uuid4().hex
        self.goal = goal
        self.course_id = course_id
        self.conversation_id = conversation_id
        self.max_turns = max_turns
        self.messages: list[dict] = []
        self.steps: list[dict] = []
        self.status = "running"          # running | done | blocked | failed
        self.answer = ""
        self.pending: dict | None = None  # 等待确认的那一次调用
        # 技能可以用 allowed-tools 声明"我只允许用这些工具"，越界要问
        self.active_skill = ""
        self.allowed_tools: list[str] = []
        # 同一调用连续失败几次：失败两次还重试第三次是纯浪费
        self.failed_calls: dict[str, int] = {}
        # 循环守卫：照 open-cowork 的 loop-guard 做的两层检测。
        # 失败次数拦不住"成功了但没进展"——同一份课程简介查两遍就是这种情况。
        self.call_streak: dict[str, int] = {}      # 完全相同调用的连续次数
        self.tool_totals: dict[str, int] = {}      # 单个工具在本轮里的累计次数
        self.last_signature = ""
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
            "conversation_id": self.conversation_id,
        }


_runs: dict[str, AgentRun] = {}
_RUN_TTL = 3600

# 跨轮对话：同一个 conversation_id 的多次运行共享上下文。
# 只留 user/assistant 这类真正的对话内容，不存 system（提示词每次现拼，
# 工具清单变了要跟着变，存旧的就是在用过期能力清单）。
_conversations: dict[str, dict] = {}
CONVERSATION_TTL = 6 * 3600
MAX_CONVERSATION_MESSAGES = 16


def _gc() -> None:
    now = time.time()
    for run_id, run in list(_runs.items()):
        if now - run.created_at > _RUN_TTL:
            _runs.pop(run_id, None)
    for conv_id, conv in list(_conversations.items()):
        if now - conv["updated"] > CONVERSATION_TTL:
            _conversations.pop(conv_id, None)


def _session_dir() -> Path:
    """会话记录存放处。放在 data 下，跟着用户数据一起搬。"""
    from .config import Settings

    root = Settings.from_env().data_dir / "agent-sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _session_path(conversation_id: str) -> Path:
    # 用 id 直接拼文件名，必须挡住 ../ 这类路径穿越
    if not re.fullmatch(r"[0-9a-f]{6,64}", conversation_id):
        raise ValueError("会话标识无效")
    return _session_dir() / f"{conversation_id}.json"


def _title_of(messages: list[dict]) -> str:
    for message in messages:
        if message.get("role") == "user" and str(message.get("content") or "").strip():
            text = " ".join(str(message["content"]).split())
            return text[:40] + ("…" if len(text) > 40 else "")
    return "未命名对话"


def persist_conversation(conversation_id: str) -> None:
    """把这一轮说完的话写到磁盘。

    之前只放在内存 dict 里，关掉应用对话就没了——成熟 agent 都留着历史，
    用户第二天回来还得能接着问。
    """
    conv = _conversations.get(conversation_id)
    if not conv:
        return
    try:
        payload = {
            "id": conversation_id,
            "course_id": conv.get("course_id", ""),
            "title": conv.get("title") or _title_of(conv["messages"]),
            "created_at": conv.get("created_at") or conv["updated"],
            "updated_at": conv["updated"],
            "messages": conv["messages"],
        }
        _session_path(conversation_id).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001 - 存不下来不该影响这次对话的结果
        logger.debug("会话落盘失败", exc_info=True)


def load_conversation(conversation_id: str) -> dict | None:
    """先看内存，再看磁盘。老会话重启后照样能接着聊。"""
    if conversation_id in _conversations:
        return _conversations[conversation_id]
    try:
        path = _session_path(conversation_id)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 文件坏了就当没有，别让聊天整个起不来
        return None
    conv = {
        "messages": payload.get("messages") or [],
        "updated": float(payload.get("updated_at") or 0),
        "course_id": payload.get("course_id", ""),
        "title": payload.get("title", ""),
        "created_at": payload.get("created_at"),
    }
    _conversations[conversation_id] = conv
    return conv


def list_conversations(course_id: str = "", limit: int = 30) -> list[dict]:
    rows: list[dict] = []
    for path in _session_dir().glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if course_id and payload.get("course_id") != course_id:
            continue
        messages = payload.get("messages") or []
        rows.append({
            "id": payload.get("id") or path.stem,
            "title": payload.get("title") or _title_of(messages),
            "course_id": payload.get("course_id", ""),
            "updated_at": payload.get("updated_at") or 0,
            "messages": len(messages),
        })
    rows.sort(key=lambda item: item["updated_at"], reverse=True)
    return rows[:limit]


def delete_conversation(conversation_id: str) -> bool:
    _conversations.pop(conversation_id, None)
    try:
        path = _session_path(conversation_id)
    except ValueError:
        return False
    if path.is_file():
        path.unlink()
        return True
    return False


def _history(conversation_id: str) -> list[dict]:
    if not conversation_id:
        return []
    conv = load_conversation(conversation_id)
    return list(conv["messages"]) if conv else []


def _archive(run: AgentRun) -> None:
    """任务做完，把「问了什么 + 结论」自动写进每日记忆。

    照 claude-mem 的思路：记忆不该指望模型自己想起来调 memory_write——
    它十次里有八次会忘。改成结束时自动落一行，日志里能对上哪次做了什么。

    只存摘要：工具输出全文进日志会很快把下一次的上下文预算吃掉。
    """
    answer = (run.answer or "").strip()
    if len(answer) < 20:
        return
    goal = " ".join(run.goal.split())[:80]
    summary = " ".join(answer.split())[:220]
    try:
        from . import longterm
        longterm.append_log(f"{goal}\n  → {summary}", run.course_id)
    except Exception:  # noqa: BLE001 - 记忆写不进去不该影响这次任务的结果
        logger.debug("自动记忆写入失败", exc_info=True)


def _remember(conversation_id: str, run: AgentRun) -> None:
    """把这一轮真正发生过的事情记下来。

    只记三类：用户说了什么、用了哪些工具（名字+一句话结果）、最后结论。
    把每条工具输出原文都存进去会很快撑爆下一次的上下文预算。
    """
    if not conversation_id:
        return
    turns: list[dict] = []
    for step in run.steps:
        kind = step.get("kind")
        if kind == "tool" and step.get("tool"):
            note = (step.get("output") or "")[:160]
            turns.append(f"[用过 {step['tool']}] {note}")
        elif kind == "final":
            continue
    record = {"role": "assistant", "content": "\n".join(turns) if turns else "（没有调用工具）"}
    conv = _conversations.setdefault(conversation_id, {"messages": [], "updated": time.time()})
    conv["messages"].append({"role": "user", "content": run.goal})
    conv["messages"].append(record)
    if run.answer:
        conv["messages"].append({"role": "assistant", "content": run.answer[:1200]})
    conv["messages"] = conv["messages"][-MAX_CONVERSATION_MESSAGES:]
    conv["updated"] = time.time()
    conv["course_id"] = run.course_id
    conv["title"] = conv.get("title") or _title_of(conv["messages"])
    persist_conversation(conversation_id)


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

    try:
        raw = hub().config().get("servers", {})
    except Exception:  # noqa: BLE001
        raw = {}
    for server in overview.get("servers", []):
        if server.get("disabled") or server.get("error"):
            continue
        config = raw.get(server["name"]) or {}
        for tool in server.get("tools", []):
            if not tool_allowed(config, tool["name"]):
                continue
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


def _system_prompt(course_id: str = "") -> str:
    catalog = tool_catalog(probe=False)
    tools = "\n".join(
        f"- {t['name']}{'（只读）' if t['read_only'] else ''}: {t['description']}" for t in catalog
    ) or "（当前没有已连接的 MCP 工具）"
    skills = "\n".join(_skill_lines()) or "（没有技能）"
    # 不能用 str.format：提示词里的 JSON 示例带花括号，会被当成占位符
    prompt = SYSTEM.replace("__TOOLS__", tools).replace("__SKILLS__", skills)
    try:
        from . import longterm

        memo = longterm.digest(course_id)
    except Exception:  # noqa: BLE001 - 记忆坏了也不能让 agent 起不来
        memo = ""
    if memo:
        # 放最后：越靠近当前问题，模型越会拿它当真；放最前面容易被长工具清单冲淡
        prompt += "\n\n# 长期记忆（用户可编辑的文件，按此执行）\n" + memo
    return prompt


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

    kind = entry.get("risk_type") or (
        "delete" if entry["destructive"] else ("finder" if entry["read_only"] else "practice")
    )
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


# ── 循环守卫 ─────────────────────────────────────────────
#
# 照 open-cowork 的 loop-guard 做的两层检测。那边一轮能连发十几个工具，
# 阈值是 3/5/8；课脉一轮只做一个动作，同样的调用第 2 次出现就已经说明模型
# 在原地打转了，所以这里收紧到 2/3。第二层管"同一工具换着参数反复用"，
# 防的是一次查十个文件那种慢死循环。
STREAK_WARN = 2
STREAK_HALT = 3
TOOL_WARN = 6
TOOL_HALT = 10


def note_call(run: "AgentRun", tool_name: str, signature: str) -> str:
    """记一次模型请求的调用，返回 \"\" / \"warn\" / \"halt\"。

    先记账再判定：被守卫拦下的调用也要算进去，否则计数永远停在阈值前，
    warn 会无限重复而升不到 halt。``run`` 只要求有那三个计数器字段，
    方便单测传假对象。
    """
    if run.last_signature == signature:
        streak = run.call_streak.get(signature, 0) + 1
    else:
        # 换了动作，之前的连续计数作废——A/B/A/B 不是死循环
        run.call_streak = {}
        streak = 1
    run.call_streak[signature] = streak
    run.last_signature = signature
    total = run.tool_totals.get(tool_name, 0) + 1
    run.tool_totals[tool_name] = total

    if streak >= STREAK_HALT or total >= TOOL_HALT:
        return "halt"
    if streak >= STREAK_WARN or total >= TOOL_WARN:
        return "warn"
    return ""


def call_tool(tool_name: str, args: dict, timeout: float = 60.0) -> dict:
    """实际执行。工具名是 ``server.tool``，但服务器名里可能带点，所以按最后一个点切。"""
    if "." not in tool_name:
        return {"ok": False, "text": f"工具名要写成 服务器.工具，收到的是 {tool_name}"}
    server_name, tool = tool_name.rsplit(".", 1)
    try:
        config = hub().config().get("servers", {}).get(server_name) or {}
        if not tool_allowed(config, tool):
            return {"ok": False, "text": f"{tool_name} 不在这个服务器的白名单里（看 mcp.json 的 allow_tools）"}
    except Exception:  # noqa: BLE001 - 配置读不出来就交给下面的调用去报错
        pass
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


def start(llm, goal: str, course_id: str = "", max_turns: int = MAX_TURNS,
          conversation_id: str = "") -> dict:
    _gc()
    # 和 stream 保持一致：不传就给一个 id，否则这条路径的对话永远存不下来
    conversation_id = conversation_id or uuid.uuid4().hex
    run = AgentRun(goal, course_id, max_turns, conversation_id)
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
            run.active_skill = name
            run.allowed_tools = [str(t) for t in (skill.get("allowed_tools") or [])]
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

        # 技能声明的工具范围：越界不硬拒，抬成"要问一句"——
        # 技能的 allowed-tools 常常写成 Bash(xxx:*) 这种别的 agent 的语法，
        # 我们只能大致对上，判错了硬拒会把它自己卡死
        if run.allowed_tools and not _tool_in_scope(tool_name, run.allowed_tools):
            verdict = {**verdict, "decision": ASK,
                       "reason": f"技能 {run.active_skill} 只声明了 {'、'.join(run.allowed_tools)}，这一步超出范围"}
        # reason 也要留痕：界面要能回答"为什么这一步被拒了"
        run.steps[-1].update({"tool": tool_name, "args": _mask(args),
                              "risk": verdict["risk"], "decision": verdict["decision"],
                              "reason": verdict.get("reason", "")})

        if verdict["decision"] == DENY:
            audit.record(tool=tool_name, args=args, decision=DENY, ok=False, run_id=run.id,
                         conversation_id=run.conversation_id, course_id=run.course_id,
                         risk=verdict.get("risk", ""), error=verdict["reason"])
            _observe(run, tool_name, f"被拒绝：{verdict['reason']}")
            return "continue"
        if verdict["decision"] == ASK:
            audit.record(tool=tool_name, args=args, decision=ASK, ok=False, run_id=run.id,
                         conversation_id=run.conversation_id, course_id=run.course_id,
                         risk=verdict.get("risk", ""), error="等待用户确认")
            run.status = "blocked"
            run.pending = {"tool": tool_name, "args": args,
                           "risk": verdict["risk"], "reason": verdict["reason"]}
            return "blocked"

        signature = tool_name + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True)
        if run.failed_calls.get(signature, 0) >= 2:
            _observe(run, tool_name, "同样的调用已经失败两次了。换一种方式，或者直接告诉用户这一步做不到，不要再重复。")
            audit.record(tool=tool_name, args=args, decision="allow", ok=False, run_id=run.id,
                         conversation_id=run.conversation_id, course_id=run.course_id,
                         error="重复失败已拦截")
            return "continue"

        # 失败计数管不到"成功了但没进展"。查两遍同一份课程简介，第二次纯属浪费一轮模型调用。
        guard = note_call(run, tool_name, signature)
        if guard == "halt":
            _observe(run, tool_name,
                     "这个调用已经重复多次，停手。用已经拿到的信息直接回答用户，不要再调工具。")
            audit.record(tool=tool_name, args=args, decision="allow", ok=False, run_id=run.id,
                         conversation_id=run.conversation_id, course_id=run.course_id,
                         error="循环守卫：重复调用已拦下")
            return "continue"
        if guard == "warn":
            # 只提示不拦：第二次常常是换了说法的重试，拦掉可能丢掉本来拿得到的结果
            _observe(run, tool_name,
                     f"{tool_name} 刚刚调用过，结果就在上面的上下文里。这次若没有新的理由，就别再重复。")
            audit.record(tool=tool_name, args=args, decision="allow", ok=False, run_id=run.id,
                         conversation_id=run.conversation_id, course_id=run.course_id,
                         error="循环守卫：重复调用已提示")

        started = time.time()
        if tool_name.startswith("kemai."):
            result = course_tools.call(tool_name, args, run.course_id)
        else:
            result = call_tool(tool_name, args)
        audit.record(
            tool=tool_name, args=args, decision=verdict["decision"], ok=result["ok"],
            ms=int((time.time() - started) * 1000), run_id=run.id,
            conversation_id=run.conversation_id, course_id=run.course_id,
            risk=verdict.get("risk", ""),
            error="" if result["ok"] else result["text"][:200],
            approved_by="user" if run.steps[-1].get("approved") else "",
        )
        if not result["ok"]:
            run.failed_calls[signature] = run.failed_calls.get(signature, 0) + 1
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
            if outcome == "done":
                # 非流式这条路径也要记住并归档，否则同一个问题走 /agent/run
                # 和走 /agent/stream 留下的痕迹不一样。
                _remember(run.conversation_id, run)
                _archive(run)
            return run.public()

    _close_out(run)
    _archive(run)
    return run.public()


def stream(llm, goal: str, course_id: str = "", max_turns: int = MAX_TURNS,
           conversation_id: str = ""):
    """边跑边报：每一步结束就吐一个事件。

    同步跑 4 轮要 85 秒，界面只能转圈。流式之后用户能看到它先查了什么、
    为什么换了个工具——这比最后那句回答更能建立信任。
    """
    _gc()
    # 传空就等于每次全新对话，前端也就永远拿不到 id。这里补一个，
    # 让「第一次运行」也能成为后续追问的起点。
    conversation_id = conversation_id or uuid.uuid4().hex
    run = AgentRun(goal, course_id, max_turns, conversation_id)
    # 历史放在 system 之后、本次问题之前：模型看得到"上次聊到哪"，
    # 但本次的工具结果仍然排在最后，优先于旧记忆
    run.messages = [{"role": "system", "content": _system_prompt(course_id)}]
    run.messages.extend(_history(conversation_id))
    run.messages.append({"role": "user", "content": goal})
    _runs[run.id] = run
    yield {"type": "run", "run_id": run.id, "max_turns": run.max_turns,
           "conversation_id": conversation_id}

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
            _remember(conversation_id, run)
            yield {"type": "blocked", "pending": run.pending}
            return
        if outcome == "failed":
            _remember(conversation_id, run)
            yield {"type": "error", "message": run.answer}
            return
        if outcome == "done":
            _remember(conversation_id, run)
            _archive(run)
            yield {"type": "final", "answer": run.answer}
            return

    _close_out(run)
    _remember(conversation_id, run)
    _archive(run)
    yield {"type": "final", "answer": run.answer}


def _tool_in_scope(tool: str, patterns: list[str]) -> bool:
    """技能写的 allowed-tools 能对上这个工具吗。

    兼容几种写法：
        kemai.course_brief   精确
        ltspice.*            通配
        Bash(browser-use:*)  别的 agent 的语法，取括号里的第一段当提示
    """
    from fnmatch import fnmatch

    for raw in patterns:
        pattern = raw.strip()
        if not pattern:
            continue
        # Bash(browser-use:*) → browser-use
        inner = pattern[pattern.find("(") + 1:pattern.rfind(")")] if "(" in pattern and ")" in pattern else ""
        candidates = [pattern, inner.split(":")[0] if inner else ""]
        for candidate in candidates:
            if candidate and (fnmatch(tool, candidate) or fnmatch(tool, candidate + ".*")):
                return True
            if candidate and candidate in tool:
                return True
    return False


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
    fit_context(run)


def _is_observation(message: dict) -> bool:
    return message.get("role") == "user" and str(message.get("content", "")).startswith("[工具结果：")


def _shorten(text: str, limit: int) -> str:
    """保留头尾、砍中间。

    工具的结论和错误几乎都在尾巴上（"Listed 16 of 16"、"Error: ..."），
    只留头部等于把最要紧的那句丢掉。
    """
    if len(text) <= limit:
        return text
    half = max(limit // 2, 40)
    return text[:half] + "\n…[工具输出已按上下文预算缩短]…\n" + text[-half:]


def fit_context(run: AgentRun, budget: int = CONTEXT_BUDGET) -> None:
    """把消息压到预算内。算法照 nanoMuse 的 fit_context。

    三级，越往后越狠：
        1. 工具输出按 3000 → 800 → 200 字符逐级缩短（**只动工具输出**，
           系统提示和用户原话一个字都不改）
        2. 还超就整条删最旧的观察，但永远保住第一条用户提问
        3. 再不行就放弃压缩，只记警告

    为什么不按"超过 N 条就从头删"：条数和体积不是一回事。三条 4000 字的
    工具输出比二十条一句话的观察还大，按条数删要么删太早，要么删不够。
    """
    if budget <= 0:
        return

    def size() -> int:
        return sum(len(str(m.get("content", ""))) for m in run.messages)

    if size() <= budget:
        return

    for limit in (3000, 800, 200):
        for message in run.messages:
            if _is_observation(message):
                message["content"] = _shorten(str(message["content"]), limit)
        if size() <= budget:
            logger.info("上下文已压缩到 %d 字符（工具输出限 %d）", size(), limit)
            return

    while size() > budget:
        index = next((i for i, m in enumerate(run.messages) if _is_observation(m) and i > 0), None)
        if index is None:
            break
        run.messages.pop(index)

    if size() > budget:
        logger.warning("上下文仍超预算（%d 字符），不再压缩", size())


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
