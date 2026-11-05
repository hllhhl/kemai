"""长期记忆（文件层）的测试。

这一层的失败方式很讨厌：不是报错，而是**悄悄忘了**或者**悄悄改了**。
所以断言分两类——写进去的必须读得回来；不该被改的必须一个字都没动。
"""

from __future__ import annotations

import pytest

from app import longterm


@pytest.fixture(autouse=True)
def memory_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("KEMAI_MEMORY_DIR", str(tmp_path))
    return tmp_path


def test_ensure_creates_templates_but_never_overwrites(memory_dir):
    longterm.ensure()
    for name in ("IDENTITY.md", "SOUL.md", "USER.md", "MEMORY.md"):
        assert (memory_dir / name).exists()

    # 用户改过的内容不能被下次启动洗掉
    (memory_dir / "USER.md").write_text("# 我手写的\n", encoding="utf-8")
    longterm.ensure()
    assert (memory_dir / "USER.md").read_text(encoding="utf-8") == "# 我手写的\n"


def test_write_then_read_round_trip():
    longterm.write("memory", "## 约定\n- 导出 PPT 一节一页")
    assert "一节一页" in longterm.read("memory")


def test_append_keeps_existing_lines():
    longterm.write("course", "- 第一条约定", "c1")
    longterm.write("course", "- 第二条约定", "c1", append=True)
    text = longterm.read("course", "c1")
    assert "第一条约定" in text and "第二条约定" in text


def test_replace_is_explicit_not_accidental():
    """默认追加；只有明确 append=False 才整份替换——避免模型"整理"掉用户内容。"""
    longterm.write("course", "- 旧内容", "c1")
    longterm.write("course", "- 新内容", "c1", append=False)
    text = longterm.read("course", "c1")
    assert "旧内容" not in text
    assert "新内容" in text


def test_course_memory_is_per_course():
    longterm.write("course", "- A 课的东西", "course-a")
    longterm.write("course", "- B 课的东西", "course-b")
    assert "A 课" in longterm.read("course", "course-a")
    assert "A 课" not in longterm.read("course", "course-b")


def test_traversal_is_refused():
    for bad in ("../config.py", "..\\..\\windows\\system.ini", "/etc/passwd"):
        with pytest.raises(ValueError):
            longterm.read_file(bad)


def test_write_file_rejects_non_markdown():
    with pytest.raises(ValueError):
        longterm.write_file("evil.exe", "x")


def test_log_is_dated_and_appended():
    longterm.append_log("跑了一次笔记生成", "c1")
    longterm.append_log("又跑了一次", "c1")
    text = longterm.latest_log()
    assert "跑了一次笔记生成" in text and "又跑了一次" in text
    assert text.count("- **") == 2


def test_digest_includes_user_and_course():
    longterm.write("user", "- 习惯：要结论和证据")
    longterm.write("course", "- 这门课笔记用 ### 分小节", "c9")
    text = longterm.digest("c9")
    assert "要结论和证据" in text
    assert "### 分小节" in text


def test_digest_one_course_does_not_leak_another():
    longterm.write("course", "- 另一门课的私事", "other")
    text = longterm.digest("mine")
    assert "另一门课的私事" not in text


def test_digest_respects_budget():
    longterm.write("memory", "很长的记忆\n" * 2000)
    text = longterm.digest("", budget=1200)
    assert len(text) <= 1300, f"摘要超预算：{len(text)}"
    assert "已截断" in text


def test_inventory_lists_relative_names():
    longterm.write("course", "- x", "abc")
    names = [row["name"] for row in longterm.inventory()]
    assert "USER.md" in names
    assert any(name.startswith("courses/") for name in names)
    assert all(not name.startswith("\\") for name in names)
