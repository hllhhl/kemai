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
    """模型永远不肯收尾时，必须在轮数上限停住，而不是一直烧额度。"""
    llm = FakeLLM([{"action": "tool", "tool": "demo.read", "args": {}}])
    result = agent_runtime.start(llm, "无限查", max_turns=3)
    assert result["turn"] == 3
    assert result["status"] == "done"
    assert len(fake_tools) == 3


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
