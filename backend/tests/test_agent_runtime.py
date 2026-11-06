"""Agent 循环的测试。

用假模型，不打网络。循环这种东西的坑几乎都在**状态机**上：轮数会不会超、
拒绝之后会不会继续、需要确认时是不是真的停下来、工具输出太长会不会撑爆上下文。
这些用真实模型测反而不稳——同样的输入，它未必给出同样的动作序列。
"""

from __future__ import annotations

import json

import pytest

from app import agent_runtime


class FakeLLM:
    """按预设脚本吐 JSON 的模型。脚本用完就停在最后一条。"""

    configured = True

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.seen: list[list[dict]] = []

    def complete(self, messages, json_mode=False):  # noqa: ARG002
        self.seen.append([dict(m) for m in messages])
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return json.dumps(step, ensure_ascii=False)


@pytest.fixture
def fake_tools(monkeypatch):
    """两个工具：一个只读，一个会改东西。"""
    catalog = [
        {"name": "demo.read", "server": "demo", "tool": "read", "description": "读",
         "read_only": True, "destructive": False, "schema": {}},
        {"name": "demo.drop", "server": "demo", "tool": "drop", "description": "删",
         "read_only": False, "destructive": True, "schema": {}},
    ]
    monkeypatch.setattr(agent_runtime, "tool_catalog", lambda probe=False: catalog)
    calls: list[tuple[str, dict]] = []

    def fake_call(tool, args, timeout=60.0):
        calls.append((tool, args))
        return {"ok": True, "text": f"{tool} 返回了一行结果"}

    monkeypatch.setattr(agent_runtime, "call_tool", fake_call)
    return calls


def test_final_answer_stops_immediately(fake_tools):
    llm = FakeLLM([{"action": "final", "answer": "直接回答，不需要工具"}])
    result = agent_runtime.start(llm, "随便问问")
    assert result["status"] == "done"
    assert result["answer"] == "直接回答，不需要工具"
    assert fake_tools == [], "不该调用任何工具"


def test_read_only_tool_runs_without_asking(fake_tools):
    llm = FakeLLM([
        {"action": "tool", "tool": "demo.read", "args": {"q": "x"}},
        {"action": "final", "answer": "查到了"},
    ])
    result = agent_runtime.start(llm, "查一下")
    assert result["status"] == "done"
    assert [c[0] for c in fake_tools] == ["demo.read"]
    # 工具输出必须进了下一轮上下文，否则模型等于白调
    second = json.dumps(llm.seen[1], ensure_ascii=False)
    assert "demo.read 返回了一行结果" in second


def test_destructive_tool_blocks_and_waits_for_a_human(fake_tools):
    llm = FakeLLM([
        {"action": "tool", "tool": "demo.drop", "args": {"id": "1"}},
        {"action": "final", "answer": "已经删了"},
    ])
    result = agent_runtime.start(llm, "删掉它")
    assert result["status"] == "blocked"
    assert result["pending"]["tool"] == "demo.drop"
    assert fake_tools == [], "没人点确认之前不许真的执行"


def test_resume_refused_keeps_the_loop_going(fake_tools):
    llm = FakeLLM([
        {"action": "tool", "tool": "demo.drop", "args": {"id": "1"}},
        {"action": "final", "answer": "那就不删了"},
    ])
    started = agent_runtime.start(llm, "删掉它")
    resumed = agent_runtime.resume(llm, started["id"], approve=False, note="别动")
    assert resumed["status"] == "done"
    assert fake_tools == [], "拒绝之后不能补执行"
    assert "别动" in json.dumps(llm.seen[-1], ensure_ascii=False) or "没有同意" in json.dumps(llm.seen[-1], ensure_ascii=False)


def test_resume_approved_runs_the_pending_call(fake_tools):
    llm = FakeLLM([
        {"action": "tool", "tool": "demo.drop", "args": {"id": "1"}},
        {"action": "final", "answer": "删完了"},
    ])
    started = agent_runtime.start(llm, "删掉它")
    resumed = agent_runtime.resume(llm, started["id"], approve=True)
    assert resumed["status"] == "done"
    assert [c[0] for c in fake_tools] == ["demo.drop"]


def test_unknown_tool_is_refused_not_executed(fake_tools):
    llm = FakeLLM([
        {"action": "tool", "tool": "demo.nonexistent", "args": {}},
        {"action": "final", "answer": "换个办法"},
    ])
    result = agent_runtime.start(llm, "试试不存在的工具")
    assert result["status"] == "done"
    assert fake_tools == []
    steps = json.dumps(result["steps"], ensure_ascii=False)
    assert "不在已连接清单" in steps


def test_max_turns_caps_the_loop(fake_tools):
    """模型永远不肯收尾时，必须在轮数上限停住，而不是一直烧额度。

    每次换一个参数，免得先被循环守卫拦下——这一条要单独验证轮数上限本身。
    """
    llm = FakeLLM([{"action": "tool", "tool": "demo.read", "args": {"i": index}}
                   for index in range(10)])
    result = agent_runtime.start(llm, "无限查", max_turns=3)
    assert result["turn"] == 3
    assert result["status"] == "done"
    assert len(fake_tools) == 3


def test_repeating_calls_are_no_longer_intercepted(fake_tools):
    """循环守卫已卸掉：重复调用由模型自己决定，只有轮数上限兜底。"""
    llm = FakeLLM([{"action": "tool", "tool": "demo.read", "args": {"q": "same"}}])
    run = agent_runtime.AgentRun("反复查同一个东西", "c1")
    run.max_turns = 5
    while run.status == "running" and run.turn < run.max_turns:
        agent_runtime._turn(run, llm)

    assert len(fake_tools) == 5, f"守卫不该中途拦下，实际只执行 {len(fake_tools)} 次"
    told = " ".join(str(message["content"]) for message in run.messages)
    assert "别再重复" not in told


def test_switching_tools_is_not_treated_as_a_loop(fake_tools):
    """换个工具是正常推进，不能被守卫误判。"""
    llm = FakeLLM([
        {"action": "tool", "tool": "demo.read", "args": {"q": "a"}},
        {"action": "tool", "tool": "demo.read", "args": {"q": "b"}},
        {"action": "final", "answer": "做完了"},
    ])
    result = agent_runtime.start(llm, "换着参数查", max_turns=4)

    assert len(fake_tools) == 2
    assert result["status"] == "done"


def test_protocol_violation_is_told_back_not_crashed(fake_tools):
    llm = FakeLLM([
        {"action": "nonsense"},
        {"action": "final", "answer": "改好了"},
    ])
    result = agent_runtime.start(llm, "乱来一次")
    assert result["status"] == "done"
    assert "无法识别的 action" in json.dumps(llm.seen[1], ensure_ascii=False)


def test_huge_tool_output_is_truncated(fake_tools, monkeypatch):
    monkeypatch.setattr(agent_runtime, "call_tool",
                        lambda tool, args, timeout=60.0: {"ok": True, "text": "字" * 50_000})
    llm = FakeLLM([
        {"action": "tool", "tool": "demo.read", "args": {}},
        {"action": "final", "answer": "好了"},
    ])
    agent_runtime.start(llm, "读一个巨大的日志")
    sent = json.dumps(llm.seen[1], ensure_ascii=False)
    assert len(sent) < 20_000, "工具输出没截断，下一轮会撑爆上下文"


def test_unconfigured_model_fails_with_a_readable_message():
    class Cold:
        configured = False

    result = agent_runtime.start(Cold(), "随便")
    assert result["status"] == "failed"
    assert "模型配置" in result["answer"]


def test_stream_emits_events_as_it_goes(fake_tools):
    """流式要有"过程"，不只是把最终结果一次性吐出来。

    顺序固定：run → step* → final → （调用方补 end）。
    少一个 run 事件，前端就不知道 run_id，后续没法 resume。
    """
    llm = FakeLLM([
        {"action": "tool", "tool": "demo.read", "args": {}},
        {"action": "final", "answer": "查完了"},
    ])
    events = list(agent_runtime.stream(llm, "查一下"))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "run"
    assert kinds.count("step") == 2, f"应该有两轮步骤，实际 {kinds}"
    assert kinds[-1] == "final"
    assert events[0].get("run_id")


def test_stream_stops_on_blocked_with_pending(fake_tools):
    """需要确认时流也不能一直吐，要停下来并把 pending 交给前端。"""
    llm = FakeLLM([
        {"action": "tool", "tool": "demo.drop", "args": {"id": "1"}},
        {"action": "final", "answer": "不该走到这里"},
    ])
    events = list(agent_runtime.stream(llm, "删掉它"))
    kinds = [e["type"] for e in events]
    assert "blocked" in kinds
    assert kinds[-1] == "blocked"
    blocked = next(e for e in events if e["type"] == "blocked")
    assert blocked["pending"]["tool"] == "demo.drop"
    assert fake_tools == []


def test_stream_reports_model_failure_as_an_event():
    """模型挂了也要让前端收到一条能显示的消息，而不是卡住等超时。"""

    class Broken:
        configured = True

        def complete(self, messages, json_mode=False):
            raise RuntimeError("网关 500")

    events = list(agent_runtime.stream(Broken(), "随便"))
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "error"
    assert "网关 500" in events[-1]["message"]


def test_stream_without_model_emits_error_once():
    class Cold:
        configured = False

    events = list(agent_runtime.stream(Cold(), "随便"))
    assert [e["type"] for e in events] == ["run", "error"]


def test_kemai_tools_route_to_course_tools(monkeypatch):
    """内置课程工具不能被当成 MCP 工具去起子进程找服务器。"""
    catalog = [{"name": "kemai.course_brief", "server": "kemai", "tool": "course_brief",
                "description": "看课程", "read_only": True, "destructive": False, "schema": {}}]
    monkeypatch.setattr(agent_runtime, "tool_catalog", lambda probe=False: catalog)
    called: list[tuple[str, dict]] = []
    monkeypatch.setattr(agent_runtime.course_tools, "call",
                        lambda name, args, course_id: (called.append((name, course_id)) or {"ok": True, "text": "课程：数据结构"}))

    def boom(*_a, **_k):
        raise AssertionError("不该走 MCP 调用路径")

    monkeypatch.setattr(agent_runtime, "call_tool", boom)

    llm = FakeLLM([
        {"action": "tool", "tool": "kemai.course_brief", "args": {}},
        {"action": "final", "answer": "看完了"},
    ])
    events = list(agent_runtime.stream(llm, "看课程", course_id="course-1"))
    assert events[-1]["type"] == "final"
    assert called == [("kemai.course_brief", "course-1")]


def test_conversation_history_is_reused(fake_tools):
    """同一个 conversation_id 的第二次运行，要能看到上一次聊过什么。"""
    llm = FakeLLM([{"action": "final", "answer": "第一次回答"}])
    list(agent_runtime.stream(llm, "第一章讲什么", conversation_id="conv-1"))

    llm2 = FakeLLM([{"action": "final", "answer": "第二章回答"}])
    list(agent_runtime.stream(llm2, "那第二章呢", conversation_id="conv-1"))

    sent = json.dumps(llm2.seen[0], ensure_ascii=False)
    assert "第一章讲什么" in sent, "上一条提问没带过去"
    assert "第一次回答" in sent, "上一次结论没带过去"


def test_new_conversation_starts_empty(fake_tools):
    llm = FakeLLM([{"action": "final", "answer": "第一次"}])
    list(agent_runtime.stream(llm, "私密问题一", conversation_id="conv-A"))

    fresh = FakeLLM([{"action": "final", "answer": "第二次"}])
    list(agent_runtime.stream(fresh, "无关问题", conversation_id="conv-B"))
    sent = json.dumps(fresh.seen[0], ensure_ascii=False)
    assert "私密问题一" not in sent, "不同对话串味了"


def test_conversation_records_tool_usage_not_full_output(fake_tools):
    """记忆里只留"用过什么工具 + 一句结果"，不留完整输出，否则下次预算被撑爆。"""
    llm = FakeLLM([
        {"action": "tool", "tool": "demo.read", "args": {}},
        {"action": "final", "answer": "好了"},
    ])
    list(agent_runtime.stream(llm, "读一下", conversation_id="conv-C"))
    conv = agent_runtime._conversations["conv-C"]
    blob = json.dumps(conv["messages"], ensure_ascii=False)
    assert "demo.read" in blob
    assert len(blob) < 2000, f"记忆太长：{len(blob)}"


def test_stream_generates_a_conversation_id_when_missing(fake_tools):
    """第一次运行必须回一个 id 回来，否则前端无从"接着上次说"。"""
    llm = FakeLLM([{"action": "final", "answer": "答"}])
    events = list(agent_runtime.stream(llm, "随便问问"))
    run_event = next(e for e in events if e["type"] == "run")
    assert run_event["conversation_id"], "没有生成 conversation_id"
    assert agent_runtime.get_run(run_event["run_id"]).conversation_id == run_event["conversation_id"]


# ── 上下文压缩（照 nanoMuse 的 fit_context）─────────────


def test_context_keeps_head_and_tail_when_shortening():
    """工具输出的结论和错误都在尾巴上，缩短时必须留尾。"""
    text = "[工具结果：x]\n" + "开头" * 2000 + "Listed 16 of 16 groups"
    shortened = agent_runtime._shorten(text, 400)
    assert len(shortened) < 600
    assert "Listed 16 of 16" in shortened, "尾巴被砍掉了"
    assert "已按上下文预算缩短" in shortened


def test_observation_shrinks_before_anything_is_dropped(fake_tools, monkeypatch):
    """先压缩工具输出，不要一上来就整条删——删了模型就"忘了"查过什么。"""
    monkeypatch.setattr(agent_runtime, "call_tool",
                        lambda tool, args, timeout=60.0: {"ok": True, "text": "字" * 6000})
    llm = FakeLLM([
        {"action": "tool", "tool": "demo.read", "args": {}},
        {"action": "tool", "tool": "demo.read", "args": {"i": 2}},
        {"action": "final", "answer": "好了"},
    ])
    run = agent_runtime.AgentRun("读两次", "c1")
    run.messages = [{"role": "system", "content": "系统提示"}, {"role": "user", "content": "读两次"}]
    run.status = "running"
    run.max_turns = 3
    while run.status == "running" and run.turn < run.max_turns:
        agent_runtime._turn(run, llm)

    observations = [m for m in run.messages if agent_runtime._is_observation(m)]
    assert len(observations) == 2, "两条观察都该在，只是被缩短"
    total = sum(len(str(m["content"])) for m in run.messages)
    assert total <= agent_runtime.CONTEXT_BUDGET + 2000


def test_system_and_user_text_are_never_shortened(fake_tools, monkeypatch):
    monkeypatch.setattr(agent_runtime, "call_tool",
                        lambda tool, args, timeout=60.0: {"ok": True, "text": "长" * 20000})
    llm = FakeLLM([
        {"action": "tool", "tool": "demo.read", "args": {}},
        {"action": "final", "answer": "好"},
    ])
    long_system = "系统提示" * 500
    run = agent_runtime.AgentRun("问题", "c1")
    run.messages = [{"role": "system", "content": long_system}, {"role": "user", "content": "用户原话"}]
    run.max_turns = 3
    while run.status == "running" and run.turn < run.max_turns:
        agent_runtime._turn(run, llm)
    assert run.messages[0]["content"] == long_system, "系统提示被改了"
    assert any(m["content"] == "用户原话" for m in run.messages), "用户原话被改了"


# ── 重复失败保护 ─────────────────────────────────────────


def test_failing_calls_are_no_longer_intercepted(fake_tools, monkeypatch):
    """失败也不再由守卫截断，模型自己决定换招还是收尾。"""
    calls = []

    def failing(tool, args, timeout=60.0):
        calls.append(tool)
        return {"ok": False, "text": "连接超时"}

    monkeypatch.setattr(agent_runtime, "call_tool", failing)
    llm = FakeLLM([{"action": "tool", "tool": "demo.read", "args": {"q": "same"}}])
    run = agent_runtime.AgentRun("一直失败", "c1")
    run.max_turns = 5
    while run.status == "running" and run.turn < run.max_turns:
        agent_runtime._turn(run, llm)

    assert len(calls) == 5, f"守卫不该中途拦下，实际只执行 {len(calls)} 次"


def test_finished_run_is_archived_into_memory(monkeypatch, fake_tools):
    """任务做完要自动留一行记忆，而不是指望模型自己想起来写。

    claude-mem 的做法：记忆靠会话结束时自动落盘。这里验证 /agent/run 这条路径
    也会归档，否则同一个问题走两个接口留下的痕迹不一样。
    """
    from app import longterm

    written: list[tuple[str, str]] = []
    monkeypatch.setattr(longterm, "append_log",
                        lambda text, course_id="": written.append((text, course_id)) or {"path": "", "bytes": 0})

    llm = FakeLLM([{"action": "final", "answer": "这门课现在有 3 份资料，全部都已解析完成并建立索引。"}])
    run = agent_runtime.AgentRun("这门课有几份资料", "course-9")
    agent_runtime.advance(run, llm)

    assert written, "任务完成后应当写一条记忆"
    text, course = written[0]
    assert "这门课有几份资料" in text and "3 份资料" in text
    assert course == "course-9"


def test_a_short_answer_is_not_worth_archiving(monkeypatch, fake_tools):
    """「好的」这种两字结论不该污染日志。"""
    from app import longterm

    written: list[str] = []
    monkeypatch.setattr(longterm, "append_log",
                        lambda text, course_id="": written.append(text) or {"path": "", "bytes": 0})

    llm = FakeLLM([{"action": "final", "answer": "好"}])
    run = agent_runtime.AgentRun("随便问问", "c1")
    agent_runtime.advance(run, llm)

    assert not written


def test_finished_run_is_archived_into_memory(monkeypatch, fake_tools):
    """任务做完要自动留一行记忆，而不是指望模型自己想起来写。

    claude-mem 的做法：记忆靠会话结束时自动落盘。这里验证 /agent/run 这条路径
    也会归档，否则同一个问题走两个接口留下的痕迹不一样。
    """
    from app import longterm

    written: list[tuple[str, str]] = []
    monkeypatch.setattr(longterm, "append_log",
                        lambda text, course_id="": written.append((text, course_id)) or {"path": "", "bytes": 0})

    llm = FakeLLM([{"action": "final", "answer": "这门课现在有 3 份资料，全部都已解析完成并建立索引。"}])
    run = agent_runtime.AgentRun("这门课有几份资料", "course-9")
    agent_runtime.advance(run, llm)

    assert written, "任务完成后应当写一条记忆"
    text, course = written[0]
    assert "这门课有几份资料" in text and "3 份资料" in text
    assert course == "course-9"


def test_a_short_answer_is_not_worth_archiving(monkeypatch, fake_tools):
    """「好的」这种两字结论不该污染日志。"""
    from app import longterm

    written: list[str] = []
    monkeypatch.setattr(longterm, "append_log",
                        lambda text, course_id="": written.append(text) or {"path": "", "bytes": 0})

    llm = FakeLLM([{"action": "final", "answer": "好"}])
    run = agent_runtime.AgentRun("随便问问", "c1")
    agent_runtime.advance(run, llm)

    assert not written


def test_conversation_is_saved_and_survives_a_restart(tmp_path, monkeypatch, fake_tools):
    """关掉应用再打开，对话得还在。

    之前会话只放在内存 dict 里，进程一退就没了——这是「Agent 该有的功能」
    里最不该缺的一个。
    """
    monkeypatch.setattr(agent_runtime, "_session_dir", lambda: tmp_path)
    agent_runtime._conversations.clear()

    llm = FakeLLM([{"action": "final", "answer": "这门课现在有 3 份资料，全部已解析完成。"}])
    result = agent_runtime.start(llm, "这门课有几份资料", "course-1", max_turns=2)
    conversation_id = result["conversation_id"]

    assert conversation_id, "第一次提问也要拿到会话 id，否则存不下来"
    assert (tmp_path / f"{conversation_id}.json").is_file()

    # 模拟进程退出：内存清空，只剩磁盘上的文件
    agent_runtime._conversations.clear()

    rows = agent_runtime.list_conversations()
    assert [row["id"] for row in rows] == [conversation_id]
    assert rows[0]["title"].startswith("这门课有几份资料")
    conv = agent_runtime.load_conversation(conversation_id)
    assert conv is not None
    assert any("3 份资料" in item["content"] for item in conv["messages"] if item["role"] == "assistant")


def test_conversation_ids_cannot_escape_the_folder(tmp_path, monkeypatch):
    """id 是从外面传进来的，拼文件名前必须挡住 ../ 这类写法。"""
    monkeypatch.setattr(agent_runtime, "_session_dir", lambda: tmp_path)

    with pytest.raises(ValueError):
        agent_runtime._session_path("../evil")
    with pytest.raises(ValueError):
        agent_runtime._session_path("../../etc/passwd")


def test_deleting_a_conversation_removes_the_file(tmp_path, monkeypatch, fake_tools):
    monkeypatch.setattr(agent_runtime, "_session_dir", lambda: tmp_path)
    agent_runtime._conversations.clear()
    llm = FakeLLM([{"action": "final", "answer": "这是一个足够长的结论，用来生成一条会话记录。"}])
    conversation_id = agent_runtime.start(llm, "随便问问", "c1", max_turns=2)["conversation_id"]

    assert agent_runtime.delete_conversation(conversation_id) is True
    assert not (tmp_path / f"{conversation_id}.json").exists()
    assert agent_runtime.load_conversation(conversation_id) is None


def test_run_reports_its_own_token_usage(fake_tools):
    """本次消耗要跟着 final 一起回界面，不能只存在后端日志里。"""
    from app import token_usage

    class MeteredLLM(FakeLLM):
        def complete(self, messages, json_mode=False):  # noqa: ARG002
            token_usage.record_usage({
                "input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
            }, "answer")
            return super().complete(messages, json_mode=json_mode)

    llm = MeteredLLM([{"action": "final", "answer": "这是一个足够长的结论，用来完成这一轮。"}])
    result = agent_runtime.start(llm, "用量测试", "c1", max_turns=2)

    assert result["usage"] is not None
    assert result["usage"]["total_tokens"] == 150
    assert result["usage"]["model_calls"] == 1


def test_stream_does_not_blow_up_on_contextvar(monkeypatch, fake_tools):
    """流式生成器会在不同 worker context 恢复，collect_usage 不能跨 yield。

    之前把 with 包住整个 generator，回答都出来了却在退出时抛
    'TokenVar was created in a different Context'，界面显示成"没有完成"。
    """
    llm = FakeLLM([{"action": "final", "answer": "这是一个足够长的结论，用来完成这一轮。"}])
    events = list(agent_runtime.stream(llm, "流式测试", "c1", max_turns=2))

    assert events[-1]["type"] == "final"
    assert events[-1]["usage"] is not None
    assert events[-1]["answer"]


def test_renaming_and_pinning_a_conversation(tmp_path, monkeypatch, fake_tools):
    monkeypatch.setattr(agent_runtime, "_session_dir", lambda: tmp_path)
    agent_runtime._conversations.clear()
    llm = FakeLLM([{"action": "final", "answer": "这是一个足够长的结论，用来生成一条会话记录。"}])
    cid = agent_runtime.start(llm, "原名", "c1", max_turns=2)["conversation_id"]

    updated = agent_runtime.update_conversation(cid, title="新名字", pinned=True)
    assert updated["title"] == "新名字" and updated["pinned"] is True

    rows = agent_runtime.list_conversations()
    assert rows[0]["id"] == cid and rows[0]["pinned"] is True
    # 重启后仍然生效
    agent_runtime._conversations.clear()
    assert agent_runtime.load_conversation(cid)["title"] == "新名字"


def test_run_reports_its_own_token_usage(fake_tools):
    """本次消耗要跟着 final 一起回界面，不能只存在后端日志里。"""
    from app import token_usage

    class MeteredLLM(FakeLLM):
        def complete(self, messages, json_mode=False):  # noqa: ARG002
            token_usage.record_usage({
                "input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
            }, "answer")
            return super().complete(messages, json_mode=json_mode)

    llm = MeteredLLM([{"action": "final", "answer": "这是一个足够长的结论，用来完成这一轮。"}])
    result = agent_runtime.start(llm, "用量测试", "c1", max_turns=2)

    assert result["usage"] is not None
    assert result["usage"]["total_tokens"] == 150
    assert result["usage"]["model_calls"] == 1


def test_stream_does_not_blow_up_on_contextvar(monkeypatch, fake_tools):
    """流式生成器会在不同 worker context 恢复，collect_usage 不能跨 yield。

    之前把 with 包住整个 generator，回答都出来了却在退出时抛
    'TokenVar was created in a different Context'，界面显示成"没有完成"。
    """
    llm = FakeLLM([{"action": "final", "answer": "这是一个足够长的结论，用来完成这一轮。"}])
    events = list(agent_runtime.stream(llm, "流式测试", "c1", max_turns=2))

    assert events[-1]["type"] == "final"
    assert events[-1]["usage"] is not None
    assert events[-1]["answer"]


def test_renaming_and_pinning_a_conversation(tmp_path, monkeypatch, fake_tools):
    monkeypatch.setattr(agent_runtime, "_session_dir", lambda: tmp_path)
    agent_runtime._conversations.clear()
    llm = FakeLLM([{"action": "final", "answer": "这是一个足够长的结论，用来生成一条会话记录。"}])
    cid = agent_runtime.start(llm, "原名", "c1", max_turns=2)["conversation_id"]

    updated = agent_runtime.update_conversation(cid, title="新名字", pinned=True)
    assert updated["title"] == "新名字" and updated["pinned"] is True

    rows = agent_runtime.list_conversations()
    assert rows[0]["id"] == cid and rows[0]["pinned"] is True
    # 重启后仍然生效
    agent_runtime._conversations.clear()
    assert agent_runtime.load_conversation(cid)["title"] == "新名字"


def test_user_message_carries_images_in_sdk_format():
    """图片块必须是 pi-ai 认的 {type,data,mimeType}，不是 OpenAI 的 image_url。"""
    from app.agent_runtime import _user_message

    message = _user_message("这张图讲了什么", [
        {"data": "AAAA", "mime_type": "image/jpeg"},
        {"data": "BBBB", "mime_type": "image/png"},
    ])
    assert message["role"] == "user"
    assert isinstance(message["content"], list)
    assert message["content"][0] == {"type": "text", "text": "这张图讲了什么"}
    assert message["content"][1] == {"type": "image", "data": "AAAA", "mimeType": "image/jpeg"}
    assert message["content"][2]["mimeType"] == "image/png"


def test_plain_message_stays_a_string():
    """没图时不套数组，免得每个请求都多一层结构。"""
    from app.agent_runtime import _user_message

    assert _user_message("你好", None) == {"role": "user", "content": "你好"}
    assert _user_message("你好", []) == {"role": "user", "content": "你好"}


def test_images_reach_the_model_on_both_entry_points(fake_tools):
    """start 和 stream 都要把图片放进消息，别只改了一边。"""
    images = [{"data": "AAAA", "mime_type": "image/png"}]

    class CaptureLLM(FakeLLM):
        seen: list[list[dict]] = []

        def complete(self, messages, json_mode=False):  # noqa: ARG002
            self.seen.append([dict(item) for item in messages])
            return super().complete(messages, json_mode=json_mode)

    script = [{"action": "final", "answer": "这是一个足够长的结论，用来完成这一轮。"}]
    first = CaptureLLM(script)
    agent_runtime.start(first, "看图", "c1", max_turns=2, images=images)
    assert first.seen[0][-1]["content"][1]["type"] == "image"

    second = CaptureLLM(script)
    list(agent_runtime.stream(second, "看图", "c1", max_turns=2, images=images))
    assert second.seen[0][-1]["content"][1]["type"] == "image"


def test_user_message_carries_images_in_sdk_format():
    """图片块必须是 pi-ai 认的 {type,data,mimeType}，不是 OpenAI 的 image_url。"""
    from app.agent_runtime import _user_message

    message = _user_message("这张图讲了什么", [
        {"data": "AAAA", "mime_type": "image/jpeg"},
        {"data": "BBBB", "mime_type": "image/png"},
    ])
    assert message["role"] == "user"
    assert isinstance(message["content"], list)
    assert message["content"][0] == {"type": "text", "text": "这张图讲了什么"}
    assert message["content"][1] == {"type": "image", "data": "AAAA", "mimeType": "image/jpeg"}
    assert message["content"][2]["mimeType"] == "image/png"


def test_plain_message_stays_a_string():
    """没图时不套数组，免得每个请求都多一层结构。"""
    from app.agent_runtime import _user_message

    assert _user_message("你好", None) == {"role": "user", "content": "你好"}
    assert _user_message("你好", []) == {"role": "user", "content": "你好"}


def test_images_reach_the_model_on_both_entry_points(fake_tools):
    """start 和 stream 都要把图片放进消息，别只改了一边。"""
    images = [{"data": "AAAA", "mime_type": "image/png"}]

    class CaptureLLM(FakeLLM):
        seen: list[list[dict]] = []

        def complete(self, messages, json_mode=False):  # noqa: ARG002
            self.seen.append([dict(item) for item in messages])
            return super().complete(messages, json_mode=json_mode)

    script = [{"action": "final", "answer": "这是一个足够长的结论，用来完成这一轮。"}]
    first = CaptureLLM(script)
    agent_runtime.start(first, "看图", "c1", max_turns=2, images=images)
    assert first.seen[0][-1]["content"][1]["type"] == "image"

    second = CaptureLLM(script)
    list(agent_runtime.stream(second, "看图", "c1", max_turns=2, images=images))
    assert second.seen[0][-1]["content"][1]["type"] == "image"
