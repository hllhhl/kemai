"""工作区接层的测试：MCP 协议客户端 + 技能包解析。

MCP 用 `fixtures/fake_mcp.py` 起一个真的子进程对话——
不是 mock 掉传输层，因为最容易错的地方恰恰是协议本身
（握手顺序、id 配对、通知不带响应）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app import skills as skill_store
from app.mcp_client import McpError, McpServer

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp.py"


def fake_server_config():
    return {"command": sys.executable, "args": ["-u", str(FIXTURE)]}


def test_mcp_handshake_and_tools_list():
    server = McpServer("fake", fake_server_config())
    try:
        tools = server.tools()
        assert [t["name"] for t in tools] == ["echo", "boom"]
        assert tools[0]["description"] == "把输入原样返回"
        assert tools[0]["schema"]["properties"]["text"]["type"] == "string"
        # annotations 要翻译成课脉自己的判断
        assert tools[1]["destructive"] is True
        assert server.info["version"] == "9.9.9"
    finally:
        server.close()


def test_mcp_tool_call_returns_text():
    server = McpServer("fake", fake_server_config())
    try:
        result = server.call("echo", {"text": "你好"})
        assert result["is_error"] is False
        assert result["text"] == "echo: 你好"
    finally:
        server.close()


def test_mcp_tool_error_is_raised_not_swallowed():
    server = McpServer("fake", fake_server_config())
    try:
        with pytest.raises(McpError, match="故意失败"):
            server.call("boom", {})
    finally:
        server.close()


def test_mcp_close_is_idempotent():
    server = McpServer("fake", fake_server_config())
    server.tools()
    server.close()
    server.close()  # 再关一次不该炸
    assert server.process is None


def test_frontmatter_parsed():
    meta, body = skill_store.parse_frontmatter(
        '---\nname: demo\ndescription: "用引号包起来的说明"\nagent_created: true\n---\n\n# 标题\n正文\n'
    )
    assert meta["name"] == "demo"
    assert meta["description"] == "用引号包起来的说明"
    assert meta["agent_created"] == "true"
    assert body.lstrip().startswith("# 标题")


def test_no_frontmatter_returns_whole_text():
    meta, body = skill_store.parse_frontmatter("# 只有正文\n内容\n")
    assert meta == {}
    assert body.startswith("# 只有正文")


def test_description_falls_back_to_first_paragraph(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    folder = skills_dir / "alpha"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: alpha\n---\n\n# 标题\n这是第一段说明。\n", encoding="utf-8")
    monkeypatch.setattr(skill_store, "skill_roots", lambda: [skills_dir])

    items = skill_store.list_skills()
    assert len(items) == 1
    assert items[0]["name"] == "alpha"
    assert items[0]["description"] == "这是第一段说明。"


def test_project_skill_overrides_user_skill(tmp_path, monkeypatch):
    user_dir = tmp_path / "user"
    project_dir = tmp_path / "project"
    for root, desc in ((user_dir, "用户版"), (project_dir, "项目版")):
        folder = root / "same"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: same\ndescription: {desc}\n---\n正文\n", encoding="utf-8")
    monkeypatch.setattr(skill_store, "skill_roots", lambda: [user_dir, project_dir])

    items = skill_store.list_skills()
    assert len(items) == 1
    assert items[0]["description"] == "项目版"


def test_match_skills_prefers_name_hit(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    for name, desc in (("fpga-flow", "板级调试"), ("report", "用 fpga 写报告")):
        folder = skills_dir / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n正文\n", encoding="utf-8")
    monkeypatch.setattr(skill_store, "skill_roots", lambda: [skills_dir])

    hits = skill_store.match_skills("fpga")
    assert hits[0]["name"] == "fpga-flow", "名字命中要排在描述命中前面"


def test_load_skill_returns_body(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    folder = skills_dir / "beta"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: beta\ndescription: 说明\n---\n\n# 正文标题\n步骤一\n", encoding="utf-8")
    monkeypatch.setattr(skill_store, "skill_roots", lambda: [skills_dir])

    loaded = skill_store.load_skill("beta")
    assert loaded["name"] == "beta"
    assert "# 正文标题" in loaded["body"]
    assert "description: 说明" not in loaded["body"], "正文里不该再带 frontmatter"

    with pytest.raises(KeyError):
        skill_store.load_skill("不存在")
