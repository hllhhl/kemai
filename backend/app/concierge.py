"""通用入口：把一句话变成一串可执行的动作。

现状问题：笔记、练习、找课各有一个按钮，用户得先知道自己该点哪个。
这里补的是**意图到动作的那一跳**——说「把第三章做成笔记再出 10 道题」，
它给出两条动作，用户确认后依次执行。

设计上的两个克制：

1. **只出计划，不动手**。生成笔记要几十秒、要烧 token，而且会覆盖已有结果。
   所以 plan 只返回「我准备做什么」，执行由用户点确认后触发。
2. **执行仍走原有端点**。动作里带的是 `method` + `endpoint` + `body`，
   前端直接调那些已经存在的接口。校验逻辑（章节是否确认、资料是否处理完、
   模型是否配好）全部在端点里，这里绝不重复实现一份——重复实现必然不同步。

没有模型 Key 时：意图解析退化为关键词规则，动作照常给出，但会标明
「这一步需要模型」。不会假装能做完然后失败。
"""

from __future__ import annotations

import re

# 需要模型才能执行的动作
NEEDS_MODEL = {"notes", "practice", "digest", "answer"}

MAX_ACTIONS = 3


def _chapters(context: dict) -> list[dict]:
    return list(context.get("chapters") or [])


def _pick_chapters(message: str, context: dict) -> tuple[list[str], str]:
    """从话里认出「第几章」。认不出就用当前章节，再认不出就全课程。"""
    chapters = _chapters(context)
    chinese = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
               "七": 7, "八": 8, "九": 9, "十": 10}

    for match in re.finditer(r"第\s*([0-9]+|[一二三四五六七八九十])\s*章", message):
        raw = match.group(1)
        index = int(raw) if raw.isdigit() else chinese.get(raw, 0)
        if not index:
            continue
        # 章节可能是「第3章」也可能是顺序号，两种都比一次
        for chapter in chapters:
            title = str(chapter.get("title") or "")
            if title.startswith(f"第{index}章") or title.startswith(f"第 {index} 章"):
                return [chapter["id"]], title
            if int(chapter.get("index") or 0) == index:
                return [chapter["id"]], title
        # 识别到章号但没匹配上：宁可什么都不选，也不要猜错的章节
        return [], f"第{index}章"

    if re.search(r"这(一)?章|当前章|本章", message):
        current = context.get("chapter_id")
        if current:
            title = next((c["title"] for c in chapters if c["id"] == current), "当前章节")
            return [current], title
    return [], "全部章节"


def _pick_count(message: str, default: int = 5) -> int:
    for match in re.finditer(r"([0-9]+)\s*(?:道|个)\s*(?:题|练习)?", message):
        value = int(match.group(1))
        if 1 <= value <= 10:
            return value
    return default


def _pick_difficulty(message: str) -> str:
    if re.search(r"基础|简单|入门|容易", message):
        return "basic"
    if re.search(r"提高|难|拔高|挑战|考研", message):
        return "advanced"
    return "medium"


def _pick_type(message: str) -> str:
    if re.search(r"多选", message):
        return "multiple_choice"
    if re.search(r"单选", message):
        return "single_choice"
    if re.search(r"大题|简答|计算|填空", message):
        return "other"
    return "mixed"


def plan(message: str, context: dict) -> dict:
    text = (message or "").strip()
    actions: list[dict] = []
    course_id = context.get("course_id") or ""

    wants_notes = bool(re.search(r"笔记|提纲|整理成一?份?复习|总结这?门课|知识梳理", text))
    # 「出 10 道题」「几道题」这类中间夹数字的说法最常见，不能只匹配「出题」两个字
    wants_practice = bool(re.search(
        r"练习|出题|做题|刷题|测一测|考我|几道题|"
        r"出\s*[0-9一二三四五六七八九十]+\s*道|"
        r"[0-9]+\s*道\s*(?:题|练习)", text))
    wants_finder = bool(re.search(r"找课|是哪门课|哪个课程|对一下课件|视频在哪", text))
    wants_answer = not any((wants_notes, wants_practice, wants_finder))

    if wants_notes:
        chapter_ids, label = _pick_chapters(text, context)
        actions.append({
            "type": "notes",
            "label": f"生成复习笔记（{label}）",
            "detail": "按章节梳理原文，产出可阅读、可提问的笔记",
            "method": "POST",
            "endpoint": f"/courses/{course_id}/outlines",
            "body": {},
            "needs_model": True,
            "seconds": 60,
        })

    if wants_practice:
        chapter_ids, label = _pick_chapters(text, context)
        count = _pick_count(text)
        difficulty = _pick_difficulty(text)
        question_type = _pick_type(text)
        difficulty_label = {"basic": "基础", "medium": "中等", "advanced": "提高"}[difficulty]
        actions.append({
            "type": "practice",
            "label": f"出 {count} 道练习题（{label}）",
            "detail": f"难度 {difficulty_label}，题型 {question_type}",
            "method": "POST",
            "endpoint": f"/courses/{course_id}/practice/generate",
            "body": {
                "mode": "topics",
                "chapter_ids": chapter_ids,
                "topic": "",
                "weak_topic_ids": [],
                "count": count,
                "question_type": question_type,
                "difficulty": difficulty,
            },
            "needs_model": True,
            "seconds": 40,
        })

    if wants_finder:
        document_id = context.get("document_id") or ""
        actions.append({
            "type": "finder",
            "label": "反查这门课在 B 站的视频",
            "detail": "用课件页指纹找同源课程，并把每一讲对回课件页码",
            "method": "POST",
            "endpoint": f"/courses/{course_id}/finder/find",
            "body": {"document_id": document_id},
            "needs_model": False,
            "seconds": 30,
        })

    if wants_answer or not actions:
        actions.append({
            "type": "answer",
            "label": "直接问我",
            "detail": "检索课程资料后回答，引用会标到具体页码或视频时间",
            "method": "POST",
            "endpoint": "/sessions/{session_id}/chat",
            "body": {"message": text},
            "needs_model": True,
            "seconds": 8,
        })

    actions = actions[:MAX_ACTIONS]
    needs_model = any(a["needs_model"] for a in actions)
    model_ready = bool(context.get("model_ready"))

    if needs_model and not model_ready:
        hint = "这些动作都要模型才能跑完，先去右上角「模型配置」填一个模型。"
    elif actions and all(a["type"] == "answer" for a in actions):
        hint = "我会用课程资料回答，答不上来会直说。"
    else:
        hint = "确认后开始执行。"

    return {
        "intent": "+".join(a["type"] for a in actions),
        "actions": actions,
        "needs_model": needs_model,
        "model_ready": model_ready,
        "hint": hint,
        "blocked": needs_model and not model_ready,
    }
