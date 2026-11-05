"""审计日志、技能工具范围、MCP 白名单的测试。

这三样都是"防它乱来"的设施，所以断言的重点是**该拦的真的拦住了**，
以及**日志真的改不动**——不然它只是给人心理安慰。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import agent_runtime, audit
from app.mcp_client import tool_allowed


@pytest.fixture(autouse=True)
def audit_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("KEMAI_AUDIT_DIR", str(tmp_path))
    return tmp_path


# ── 审计 ────────────────────────────────────────────────


def test_record_writes_one_line_per_call(audit_dir):
    audit.record(tool="kemai.course_brief", decision="allow", ok=True, ms=12, run_id="r1")
    audit.record(tool="demo.drop", decision="ask", ok=False, run_id="r1")
    files = list(audit_dir.glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["tool"] == "kemai.course_brief"


def test_args_are_shaped_not_copied(audit_dir):
    """参数里可能是密钥或用户原话，只记形状。"""
    entry = audit.record(tool="x", args={"api_key": "sk-very-secret-123", "n": 3}, ok=True)
    assert entry["args"]["api_key"] == "str[18]"
    assert "sk-very-secret" not in json.dumps(entry, ensure_ascii=False)
    assert entry["args"]["n"] == 3


def test_chain_verifies(audit_dir):
    for i in range(5):
        audit.record(tool=f"t{i}", ok=True)
    result = audit.verify()
    assert result["ok"] is True
    assert result["lines"] == 5


def test_tampering_is_detected(audit_dir):
    for i in range(4):
        audit.record(tool=f"t{i}", ok=True)
    path = next(audit_dir.glob("*.jsonl"))
    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["tool"] = "伪造的工具"
    lines[1] = json.dumps(row, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = audit.verify()
    assert result["ok"] is False
    assert result["broken_at"] == 2, "应该指出是第 2 行被改过"


def test_tail_returns_newest_first(audit_dir):
    for i in range(3):
        audit.record(tool=f"t{i}", ok=True)
    rows = audit.tail(limit=2)
    assert [r["tool"] for r in rows] == ["t2", "t1"]


def test_audit_failure_does_not_raise(monkeypatch, tmp_path):
    """审计坏掉不该让正在跑的任务失败。"""
    monkeypatch.setenv("KEMAI_AUDIT_DIR", str(tmp_path / "nested" / "deep"))
    monkeypatch.setattr(audit, "_today", lambda: (_ for _ in ()).throw(OSError("磁盘满了")))
    entry = audit.record(tool="x", ok=True)
    assert entry["tool"] == "x"


# ── 技能的 allowed-tools ─────────────────────────────────


def test_tool_in_scope_matches_exact_and_glob():
    assert agent_runtime._tool_in_scope("kemai.course_brief", ["kemai.course_brief"])
    assert agent_runtime._tool_in_scope("ltspice.jobs", ["ltspice.*"])
    assert not agent_runtime._tool_in_scope("blender.run", ["ltspice.*"])


def test_tool_in_scope_understands_other_agent_syntax():
    """别的 agent 写 Bash(browser-use:*)，取括号里第一段当提示。"""
    assert agent_runtime._tool_in_scope("browser-use.click", ["Bash(browser-use:*)"])
    assert not agent_runtime._tool_in_scope("kemai.course_brief", ["Bash(browser-use:*)"])


# ── MCP 白名单 ───────────────────────────────────────────


def test_allow_list_limits_exposure():
    config = {"allow_tools": ["jobs", "inspect"]}
    assert tool_allowed(config, "jobs")
    assert not tool_allowed(config, "run_code")


def test_deny_list_wins_over_allow():
    config = {"allow_tools": ["*"], "deny_tools": ["run_code", "edit_*"]}
    assert tool_allowed(config, "jobs")
    assert not tool_allowed(config, "run_code")
    assert not tool_allowed(config, "edit_schematic")


def test_no_lists_means_everything():
    assert tool_allowed({}, "anything")
