"""导入技能包与 MCP 服务器的测试。

重点不是"能导入"，而是"不该导入的时候真的没导"：
缺少 description、同名未确认覆盖、没勾确认就跑命令——这几条错了，
轻则技能列表里多一个点不动的空壳，重则一条陌生命令长期跑在这台机器上。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import importers

GOOD = """---
name: demo-skill
description: 把课件整理成复习清单
---

# 步骤
1. 读课件
"""


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(importers, "_user_skills_dir", lambda: skills_dir)
    monkeypatch.setattr(importers, "skill_store", _FakeStore(skills_dir))
    config = tmp_path / "mcp.json"
    monkeypatch.setattr(importers, "_writable_config", lambda: config)
    # hub 是个函数，reload 在它返回的单例上
    class _Hub:
        @staticmethod
        def reload():
            return {"path": str(config), "servers": {}}

    monkeypatch.setattr(importers, "hub", lambda: _Hub())
    return {"skills": skills_dir, "config": config}


class _FakeStore:
    """只提供 importers 用到的两个函数，别把真实用户目录扯进来。"""

    def __init__(self, root: Path):
        self.root = root

    def skill_roots(self):
        return [self.root]

    @staticmethod
    def parse_frontmatter(text: str):
        from app.skills import parse_frontmatter

        return parse_frontmatter(text)


def test_missing_description_is_refused(sandbox):
    with pytest.raises(importers.ImportError_, match="description"):
        importers.import_skill("---\nname: no-desc\n---\n正文")


def test_bad_name_is_refused(sandbox):
    """名字要能直接当目录名：挡掉路径穿越、空格和超长。"""
    for bad in ("../evil", "Has Space", "x" * 80, "-leading-dash"):
        with pytest.raises(importers.ImportError_):
            importers.import_skill(f"---\nname: {bad}\ndescription: d\n---\n正文")


def test_uppercase_name_is_normalized_not_refused(sandbox):
    """大写不算错，统一转小写——Windows 目录名不分大小写，混着放迟早撞车。"""
    result = importers.import_skill("---\nname: MySkill\ndescription: d\n---\n正文")
    assert result["name"] == "myskill"


def test_empty_body_is_refused(sandbox):
    """只有 frontmatter 的技能等于没有步骤，导入它只会让列表里多一个空壳。"""
    with pytest.raises(importers.ImportError_, match="正文"):
        importers.import_skill("---\nname: empty\ndescription: d\n---\n\n")


def test_import_then_overwrite_gate(sandbox):
    first = importers.import_skill(GOOD)
    assert first["overwrote"] is False
    assert (sandbox["skills"] / "demo-skill" / "SKILL.md").exists()

    with pytest.raises(importers.ImportError_, match="已经存在"):
        importers.import_skill(GOOD)

    second = importers.import_skill(GOOD, overwrite=True)
    assert second["overwrote"] is True


def test_import_dir_brings_its_references(sandbox, tmp_path):
    """目录导入要把 references/ 一起搬走，否则正文里的链接全是死链。"""
    source = tmp_path / "src-skill"
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text(GOOD, encoding="utf-8")
    (source / "references" / "notes.md").write_text("# 参考", encoding="utf-8")

    result = importers.import_skill_dir(str(source))
    assert result["files"] >= 2
    assert (sandbox["skills"] / "demo-skill" / "references" / "notes.md").exists()


def test_mcp_requires_confirm(sandbox):
    with pytest.raises(importers.ImportError_, match="确认"):
        importers.import_mcp_server({"name": "x", "command": "npx"}, confirm=False)


def test_mcp_writes_and_merges_config(sandbox):
    sandbox["config"].write_text(json.dumps({"mcpServers": {"old": {"command": "a"}}}), encoding="utf-8")
    importers.import_mcp_server({"name": "filesystem", "command": "npx", "args": ["-y", "srv"]}, confirm=True)

    saved = json.loads(sandbox["config"].read_text(encoding="utf-8"))
    assert set(saved["mcpServers"]) == {"old", "filesystem"}, "合并时不能把别的服务器弄丢"
    assert saved["mcpServers"]["filesystem"]["args"] == ["-y", "srv"]


def test_mcp_overwrite_needs_explicit_flag(sandbox):
    importers.import_mcp_server({"name": "srv", "command": "a"}, confirm=True)
    with pytest.raises(importers.ImportError_, match="覆盖"):
        importers.import_mcp_server({"name": "srv", "command": "b"}, confirm=True)
    result = importers.import_mcp_server({"name": "srv", "command": "b"}, confirm=True, overwrite=True)
    assert result["overwrote"] is True


def test_broken_config_is_not_touched(sandbox):
    """现有 mcp.json 坏了就别往里写——合并进一份解析不了的 JSON 只会更坏。"""
    sandbox["config"].write_text("{ 这不是 json", encoding="utf-8")
    with pytest.raises(importers.ImportError_, match="解析失败"):
        importers.import_mcp_server({"name": "x", "command": "npx"}, confirm=True)
    assert sandbox["config"].read_text(encoding="utf-8") == "{ 这不是 json"


def test_command_preview_quotes_spaces(sandbox):
    command = importers.preview_command({"command": "npx", "args": ["-y", r"C:\课程 资料"]})
    assert '"C:\\课程 资料"' in command, "带空格的参数要加引号，否则用户看到的和实际执行的不一样"


def test_remote_server_uses_url(sandbox):
    normalized = importers.normalize_server({"name": "remote", "url": "http://127.0.0.1:7000/mcp"})
    assert normalized["server"] == {"url": "http://127.0.0.1:7000/mcp"}


def test_args_accept_one_per_line(sandbox):
    """界面上一行一个参数，比让人手写 JSON 数组现实。"""
    normalized = importers.normalize_server({"name": "x", "command": "npx", "args": "-y\nsrv\n\n"})
    assert normalized["server"]["args"] == ["-y", "srv"]
