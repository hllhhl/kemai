"""Agent 的文件手与自修改能力：能动手，但不能越界、不能不留痕。"""
from __future__ import annotations

import json

import pytest

from app import agent_files, course_tools


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    workspace = data / "agent-workspace"
    workspace.mkdir()
    monkeypatch.setattr(agent_files, "project_root", lambda: project)
    monkeypatch.setattr(agent_files, "data_dir", lambda: data)
    monkeypatch.setattr(agent_files, "workspace_dir", lambda: workspace)
    (project / "app").mkdir()
    (project / "app" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    return project, data, workspace


def test_new_tools_are_visible_to_the_agent():
    names = {item["name"] for item in course_tools.catalog()}
    for tool in ("kemai.self_status", "kemai.fs_list", "kemai.fs_read", "kemai.fs_write",
                 "kemai.fs_organize", "kemai.self_edit", "kemai.run_python"):
        assert tool in names


def test_writing_creates_a_backup(sandbox):
    _, data, workspace = sandbox
    target = workspace / "note.txt"
    target.write_text("旧内容", encoding="utf-8")

    result = agent_files.fs_write({"path": "note.txt", "content": "新内容"})

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "新内容"
    backups = list((data / "agent-backups").rglob("note.txt"))
    assert backups and backups[0].read_text(encoding="utf-8") == "旧内容"


def test_path_traversal_is_resolved(sandbox):
    """全盘开放后不再硬拒路径，但必须 resolve 展开，不能让注入生效。"""
    project, _, _ = sandbox
    result = agent_files.safe_path(str(project / ".." / "outside.txt"))
    assert result[1] == ""
    assert result[0].name == "outside.txt"


def test_personal_directories_are_reachable(sandbox):
    """用户明确要求全盘可访问：桌面这类个人目录不再被白名单挡住。"""
    project, _, _ = sandbox
    desktop = project.parent / "Desktop"
    desktop.mkdir()
    result = agent_files.fs_list({"path": str(desktop)})
    assert result["ok"] is True
    assert "目录" in result["text"]


def test_run_python_runs_in_the_workspace(sandbox):
    _, _, workspace = sandbox
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    result = agent_files.run_python({"code": "import glob; print(glob.glob('*.txt'))"})
    assert result["ok"] is True
    assert "a.txt" in result["text"]


def test_self_edit_changes_source_and_logs_it(sandbox):
    project, data, _ = sandbox
    result = agent_files.self_edit({
        "path": "app/mod.py", "action": "replace", "old": "VALUE = 1", "new": "VALUE = 2",
    })
    assert result["ok"] is True
    assert (project / "app" / "mod.py").read_text(encoding="utf-8").strip() == "VALUE = 2"
    assert list((data / "agent-backups").rglob("mod.py")), "改源码必须留备份"
    log = data / "self-edits.jsonl"
    assert log.is_file()
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["action"] == "replace"


def test_self_edit_allows_files_outside_the_project(sandbox):
    """全盘开放后，改项目外的文件不再被硬拒（仍留备份与审计）。"""
    _, data, _ = sandbox
    outside = data / "outside.py"
    outside.write_text("X = 1\n", encoding="utf-8")
    result = agent_files.self_edit({"path": str(outside), "action": "append", "text": "# x"})
    assert result["ok"] is True
    assert "# x" in outside.read_text(encoding="utf-8")


def test_delete_moves_to_backup_instead_of_erasing(sandbox):
    """删除一律移到备份区，不真删——个人文件安全的硬要求。"""
    _, data, workspace = sandbox
    target = workspace / "keep.txt"
    target.write_text("重要", encoding="utf-8")
    result = agent_files.fs_delete({"path": "keep.txt"})
    assert result["ok"] is True
    assert not target.exists()
    backups = list((data / "agent-backups").rglob("keep.txt"))
    assert backups and backups[0].read_text(encoding="utf-8") == "重要"


def test_organize_is_dry_run_by_default(sandbox):
    _, _, workspace = sandbox
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    result = agent_files.fs_organize({"path": "."})
    assert result["ok"] is True and "预演" in result["text"]
    assert (workspace / "a.txt").exists(), "预演不能真的移动文件"
