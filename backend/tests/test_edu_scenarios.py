"""教育场景库与学习者档案：内置资源，不是用户 skills。"""
from __future__ import annotations

import json

import pytest

from app import edu_profile, edu_scenarios


def test_builtin_catalog_has_all_170_scenarios():
    assert edu_scenarios.facets()["total"] == 170
    assert len(edu_scenarios.facets()["categories"]) == 9


def test_filtering_by_category_and_subject():
    items = edu_scenarios.search(category="备考复习", subject="英语")
    assert items, "备考复习 / 英语 应该有场景"
    assert all("备考复习" in item["category"] for item in items)
    assert all("英语" in item["subjects"] for item in items)


def test_detail_reads_the_bundled_skill_file():
    items = edu_scenarios.search(q="错题", limit=1)
    assert items
    detail = edu_scenarios.detail(items[0]["slug"])
    assert detail is not None
    assert detail["markdown"], "场景详情要能读到内置的 SKILL.md 正文"


def test_goal_mentions_kemai_tools():
    """场景不能只是一段描述，要指到课脉真实存在的能力上。"""
    items = edu_scenarios.search(q="错题", limit=1)
    goal = edu_scenarios.build_goal(items[0]["slug"], {"年级": "高一"})
    assert goal is not None
    assert "【教育场景" in goal and "高一" in goal
    assert "kemai." in goal or "课脉" in goal


def test_unknown_scenario_returns_none():
    assert edu_scenarios.detail("not-a-real-slug") is None
    assert edu_scenarios.build_goal("not-a-real-slug", {}) is None


def test_profile_is_persisted_and_injected(tmp_path, monkeypatch):
    monkeypatch.setattr(edu_profile, "profile_path", lambda: tmp_path / "edu-profile.json")

    assert edu_profile.load()["stage"] == ""
    edu_profile.save({"stage": "大学", "goal": "期末 90 分", "weakness": "时序逻辑"})
    saved = json.loads((tmp_path / "edu-profile.json").read_text(encoding="utf-8"))
    assert saved["goal"] == "期末 90 分"

    prompt = edu_profile.as_prompt()
    assert "学习者档案" in prompt and "期末 90 分" in prompt
    assert "最关键的澄清问题" in prompt


def test_empty_profile_still_teaches_clarification(tmp_path, monkeypatch):
    monkeypatch.setattr(edu_profile, "profile_path", lambda: tmp_path / "missing.json")
    prompt = edu_profile.as_prompt()
    assert "最关键的澄清问题" in prompt
    assert "依据" in prompt


def test_profile_truncates_long_values(tmp_path, monkeypatch):
    monkeypatch.setattr(edu_profile, "profile_path", lambda: tmp_path / "p.json")
    result = edu_profile.save({"goal": "很" * 1000})
    assert len(result["goal"]) == edu_profile.MAX_LEN
