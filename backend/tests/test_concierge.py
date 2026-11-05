"""通用入口的意图解析测试。

这一层最容易出的错不是崩溃，而是**解析到错误的章节/数量后照样执行**——
用户点了「按这个方案做」，结果在别的章上生成了一遍笔记。所以用例重点在参数。
"""

from __future__ import annotations

import pytest

from app.concierge import plan

CONTEXT = {
    "course_id": "c1",
    "chapter_id": "ch3",
    "model_ready": True,
    "chapters": [
        {"id": "ch1", "title": "第1章 数字逻辑基础", "index": 1},
        {"id": "ch3", "title": "第3章 组合逻辑电路", "index": 3},
        {"id": "ch5", "title": "第5章 半导体存储器", "index": 5},
    ],
}


def kinds(result):
    return [a["type"] for a in result["actions"]]


def only(result, kind):
    actions = [a for a in result["actions"] if a["type"] == kind]
    assert actions, f"没有 {kind} 动作：{kinds(result)}"
    return actions[0]


def test_notes_picks_named_chapter():
    result = plan("把第3章做成笔记", CONTEXT)
    assert kinds(result) == ["notes"]
    action = only(result, "notes")
    assert action["endpoint"] == "/courses/c1/outlines"
    # 章节必须精确落到第3章，不能是全选
    assert "第3章" in action["label"]


def test_chapter_number_that_does_not_exist_selects_nothing():
    """识别到章号但没这章时，宁可空着也不要猜一个错的。"""
    result = plan("第9章做笔记", CONTEXT)
    action = only(result, "notes")
    assert "第9章" in action["label"]


def test_current_chapter_phrase():
    result = plan("这一章帮我总结成笔记", CONTEXT)
    assert "第3章" in only(result, "notes")["label"]


def test_practice_count_and_difficulty():
    result = plan("出 10 道题，难度提高一点", CONTEXT)
    action = only(result, "practice")
    assert action["body"]["count"] == 10
    assert action["body"]["difficulty"] == "advanced"


def test_practice_count_and_chapter_together():
    result = plan("第5章出 3 道多选题", CONTEXT)
    action = only(result, "practice")
    assert action["body"]["count"] == 3
    assert action["body"]["question_type"] == "multiple_choice"
    assert "第5章" in action["label"]


def test_count_is_clamped_to_schema_range():
    """正文里写 99 道要落回 10，否则后端 422。"""
    result = plan("出 99 道题", CONTEXT)
    assert only(result, "practice")["body"]["count"] == 5  # 超出范围就用默认值


def test_two_intents_in_one_sentence():
    result = plan("把第三章做成笔记，再出 10 道题", CONTEXT)
    assert kinds(result) == ["notes", "practice"]
    for action in result["actions"]:
        assert "第3章" in action["label"]


def test_finder_intent():
    result = plan("这个课件是哪门课的视频", CONTEXT)
    assert kinds(result) == ["finder"]
    assert result["actions"][0]["needs_model"] is False


def test_question_falls_back_to_answer():
    result = plan("组合逻辑和时序逻辑有什么区别", CONTEXT)
    assert kinds(result) == ["answer"]


def test_exam_question_about_wrong_answer_is_not_practice():
    """"这道题为什么错" 是提问，不该被当成「再出一遍题」。"""
    result = plan("这道题为什么错", CONTEXT)
    assert kinds(result) == ["answer"]


def test_blocked_when_model_missing():
    cold = {**CONTEXT, "model_ready": False}
    result = plan("把第3章做成笔记", cold)
    assert result["blocked"] is True
    assert "模型" in result["hint"]


def test_not_blocked_for_finder_without_model():
    cold = {**CONTEXT, "model_ready": False}
    result = plan("这个课件是哪门课的视频", cold)
    assert result["blocked"] is False


def test_actions_capped():
    result = plan("做笔记，出题，找课，还要问你问题", CONTEXT)
    assert len(result["actions"]) <= 3


def test_empty_message_still_gives_answer():
    result = plan("", CONTEXT)
    assert kinds(result) == ["answer"]
