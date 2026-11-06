"""课程工具的形状与边界。

这些工具是 agent 唯一能"看到这门课"的入口，所以要求两件事：
名字稳定（模型靠名字选）、失败要回一句人能读的话（而不是抛异常让循环崩掉）。
"""

from __future__ import annotations

import pytest

from app import course_tools


def test_catalog_names_are_namespaced_and_unique():
    names = [tool["name"] for tool in course_tools.catalog()]
    assert len(names) == len(set(names))
    assert all(name.startswith("kemai.") for name in names), names
    # 名字里带点，agent 循环按最后一个点切服务器名，别用会和 MCP 撞的前缀
    assert "kemai.course_brief" in names


def test_every_tool_declares_read_only_or_not():
    for tool in course_tools.catalog():
        assert isinstance(tool["read_only"], bool)
        assert isinstance(tool["destructive"], bool)
        assert tool["description"].strip()


def test_unknown_tool_returns_a_readable_message():
    result = course_tools.call("kemai.nope", {}, "c1")
    assert result["ok"] is False
    assert "没有这个课程工具" in result["text"]


def test_missing_course_is_explained_not_crashed():
    result = course_tools.call("kemai.notes_read", {}, "")
    assert result["ok"] is False
    assert "选一门课" in result["text"]


def test_unbound_services_gives_a_clear_error(monkeypatch):
    """测试或冷启动时没绑定 Services，要给一句人话，不是 AttributeError。"""
    monkeypatch.setattr(course_tools, "_services", None)
    result = course_tools.call("kemai.course_brief", {}, "c1")
    assert result["ok"] is False
    assert "Services" in result["text"]


def test_export_rejects_unknown_kind(monkeypatch):
    monkeypatch.setattr(course_tools, "_services", object())
    result = course_tools.call("kemai.docs_export", {"kind": "pdf"}, "c1")
    assert result["ok"] is False
    assert "docx 或 pptx" in result["text"]


def test_bili_tools_are_exposed_to_the_agent():
    """用户说「上 B 站找课」时，Agent 不能再说没有这个接口。"""
    from app import course_tools

    names = {item["name"] for item in course_tools.catalog()}
    for tool in ("kemai.bili_status", "kemai.bili_search", "kemai.bili_parts",
                 "kemai.bili_transcript", "kemai.course_import_text"):
        assert tool in names


def test_bili_status_reports_login_state():
    from app import course_tools

    result = course_tools.call("kemai.bili_status", {}, "course-1")
    assert result["ok"] is True
    assert "B 站" in result["text"] and "登录" in result["text"]


def test_bili_search_failure_is_explained_not_silenced(monkeypatch):
    """反爬拦截时要说清原因和下一步，而不是抛一句『没有这个工具』。"""
    from app import course_tools
    from app.bilibili import BilibiliError

    class Blocked:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def search(self, *args, **kwargs):
            raise BilibiliError("被 B 站反爬拦截（返回了网页）")

    monkeypatch.setattr(course_tools, "_bili_client", lambda: Blocked())
    result = course_tools.call("kemai.bili_search", {"keyword": "模拟集成电路"}, "course-1")
    assert result["ok"] is False
    assert "反爬" in result["text"] and "登录" in result["text"]


def test_bili_tools_are_exposed_to_the_agent():
    """用户说「上 B 站找课」时，Agent 不能再说没有这个接口。"""
    from app import course_tools

    names = {item["name"] for item in course_tools.catalog()}
    for tool in ("kemai.bili_status", "kemai.bili_search", "kemai.bili_parts",
                 "kemai.bili_transcript", "kemai.course_import_text"):
        assert tool in names


def test_bili_status_reports_login_state():
    from app import course_tools

    result = course_tools.call("kemai.bili_status", {}, "course-1")
    assert result["ok"] is True
    assert "B 站" in result["text"] and "登录" in result["text"]


def test_bili_search_failure_is_explained_not_silenced(monkeypatch):
    """反爬拦截时要说清原因和下一步，而不是抛一句『没有这个工具』。"""
    from app import course_tools
    from app.bilibili import BilibiliError

    class Blocked:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def search(self, *args, **kwargs):
            raise BilibiliError("被 B 站反爬拦截（返回了网页）")

    monkeypatch.setattr(course_tools, "_bili_client", lambda: Blocked())
    result = course_tools.call("kemai.bili_search", {"keyword": "模拟集成电路"}, "course-1")
    assert result["ok"] is False
    assert "反爬" in result["text"] and "登录" in result["text"]
