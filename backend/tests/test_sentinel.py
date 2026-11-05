"""哨兵与产物核对的测试。

这两块都属于「说错了会让人真丢东西」的部分，所以断言写得比实现还细：
不仅要证明该拦的拦住了，还要证明**不该拦的没被误拦**——
一个总是问东问西的审批弹窗，用户三天就会无脑点是。
"""

from __future__ import annotations

import json

import pytest

from app import sentinel as sentinel_module
from app.concierge import plan
from app.sentinel import ALLOW, ASK, DENY

CONTEXT = {
    "course_id": "c1",
    "chapter_id": "ch3",
    "chapters": [
        {"id": "ch1", "title": "第1章 绪论", "index": 1},
        {"id": "ch3", "title": "第3章 栈和队列", "index": 3},
    ],
    "document_id": "d1",
    "course_name": "数据结构",
    "model_ready": True,
    "has_material": True,
    "has_deck": True,
}

MESSAGES = ("把第3章做成笔记", "出 3 道题", "这个课件是哪门课的视频", "什么是栈")


@pytest.fixture(autouse=True)
def clean_sentinel(tmp_path, monkeypatch):
    """每个用例一份干净的哨兵配置，别让上一个用例的授权漏过来。"""
    monkeypatch.setenv("KEMAI_DATA_DIR", str(tmp_path))
    fresh = sentinel_module.Sentinel()
    monkeypatch.setattr(sentinel_module, "_sentinel", fresh)
    yield fresh


def test_new_product_is_allowed_by_default():
    """第一次做笔记不是覆盖，不该每次都问——问多了用户就闭眼点。"""
    action = next(a for a in plan("把第3章做成笔记", CONTEXT)["actions"] if a["type"] == "notes")
    assert action["risk"] == "moderate"
    assert action["decision"] == ALLOW


def test_overwriting_existing_notes_asks_once():
    """已经有笔记时再生成会盖掉旧版本，这一条必须停下来问。"""
    result = plan("把第3章做成笔记", {**CONTEXT, "has_outline": True})
    action = next(a for a in result["actions"] if a["type"] == "notes")
    assert action["decision"] == ASK
    assert action["overwrites"] is True
    assert result["needs_confirm"] == 1


def test_read_only_actions_are_never_gated():
    """只读动作在严格模式下也不该问：查个课也要点头，工具就没法用了。"""
    for message in ("这个课件是哪门课的视频", "什么是栈"):
        for action in plan(message, CONTEXT)["actions"]:
            if action["risk"] == "low":
                assert action["decision"] == ALLOW, f"{action['label']} 被误拦"


def test_strict_mode_asks_for_moderate_risk(clean_sentinel):
    clean_sentinel.set_mode("strict")
    action = next(a for a in plan("把第3章做成笔记", CONTEXT)["actions"] if a["type"] == "notes")
    assert action["decision"] == ASK


def test_auto_mode_still_asks_before_overwriting(clean_sentinel):
    """auto 模式也不放过「覆盖」这一步，这是有意的。

    auto 的意思是「别为常规操作打断我」，不是「别告诉我我在覆盖旧东西」。
    删掉一份用户花了几十分钟等出来的笔记，比多弹一次窗贵得多。
    """
    clean_sentinel.set_mode("auto")
    action = next(
        a for a in plan("把第3章做成笔记", {**CONTEXT, "has_outline": True})["actions"]
        if a["type"] == "notes"
    )
    assert action["overwrites"] is True
    assert action["decision"] == ASK


def test_grant_then_revoke_round_trip(clean_sentinel):
    """授权要立刻生效，撤销也要立刻生效——不能等重启。"""
    before = next(a for a in plan("把第3章做成笔记", {**CONTEXT, "has_outline": True})["actions"]
                  if a["type"] == "notes")
    assert before["decision"] == ASK

    clean_sentinel.grant("notes", scope="session")
    after = next(a for a in plan("把第3章做成笔记", {**CONTEXT, "has_outline": True})["actions"]
                 if a["type"] == "notes")
    assert after["decision"] == ALLOW, "授权后仍在问"

    clean_sentinel.revoke("notes")
    again = next(a for a in plan("把第3章做成笔记", {**CONTEXT, "has_outline": True})["actions"]
                 if a["type"] == "notes")
    assert again["decision"] == ASK, "撤销后没恢复"


def test_always_grant_survives_reload(tmp_path, monkeypatch):
    """scope=always 要落盘：重启应用后不该再问一遍。"""
    monkeypatch.setenv("KEMAI_DATA_DIR", str(tmp_path))
    first = sentinel_module.Sentinel()
    first.grant("notes", scope="always")

    second = sentinel_module.Sentinel()
    second.load()
    assert "notes" in second.snapshot()["always_allow"]

    saved = json.loads((tmp_path / "sentinel.json").read_text(encoding="utf-8"))
    assert saved["always_allow"] == ["notes"]


def test_session_grant_is_not_written_to_disk(tmp_path, monkeypatch):
    """session 授权不该写盘：那是「这一次别问」，重启后要给回控制权。"""
    monkeypatch.setenv("KEMAI_DATA_DIR", str(tmp_path))
    s = sentinel_module.Sentinel()
    s.grant("practice", scope="session")
    assert not (tmp_path / "sentinel.json").exists()


def test_deny_disables_the_action_and_blocks_plan(clean_sentinel):
    clean_sentinel._state["deny"] = ["notes"]
    result = plan("把第3章做成笔记", CONTEXT)
    action = result["actions"][0]
    assert action["decision"] == DENY
    assert action["ready"] is False, "被停用的动作不该显示为可执行"
    assert result["denied"] == 1
    assert result["blocked"] is True


def test_unknown_action_type_defaults_to_moderate():
    """没登记过的动作按「会改东西」处理——宁可多问一句，不要默默执行。"""
    verdict = sentinel_module.sentinel().assess({"type": "unknown_kind"})
    assert verdict["risk"] == "moderate"


def test_all_planned_actions_carry_a_verdict():
    """每条动作都必须带 risk/decision，前端靠它渲染，缺一条界面就少一个徽章。"""
    for message in MESSAGES:
        for action in plan(message, CONTEXT)["actions"]:
            for key in ("risk", "decision", "reason", "risk_note"):
                assert key in action, f"{action['label']} 缺 {key}"
