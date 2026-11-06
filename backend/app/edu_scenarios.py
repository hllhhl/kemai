"""教育场景库：hermes-edu-skills 作为软件内置资源，不是可安装的技能包。

仓库 https://github.com/hezkvectory/hermes-edu-skills 提供 170 个中文教育场景
（教材同步、备考复习、每日练习、错题巩固、教师工具等）。直接把 SKILL.md 丢进
用户 skills 目录只会多出 170 条技能描述，模型挑不过来，也没法和课脉已有的
练习/错题/章节工具联动。

所以这里改成软件功能：catalog.json 作为只读索引，SKILL.md 作为场景详情正文，
按学段/学科/分类筛选；选中后由后端把场景和它需要的参数拼成一段可执行的目标，
并明确告诉 Agent 该优先用课脉的哪些工具。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "assets" / "hermes-edu"

CATEGORY_LABELS = {
    "textbook-sync": "教材同步",
    "teacher-tools": "教师工具",
    "daily-practice": "每日练习",
    "exam-prep": "备考复习",
    "learning-core": "学习核心",
    "reading-writing": "阅读写作",
    "family-education": "亲子陪学",
    "career-learning": "职业与升学",
    "language-learning": "语言学习",
}

STAGE_LABELS = {
    "preschool": "学前", "primary": "小学", "junior": "初中",
    "senior": "高中", "college": "大学", "adult": "成人",
}

# 上游声明的工具名 → 课脉里真实存在的工具/能力，生成目标时写进提示词，
# 让场景不只是"一段描述"，而是能落到已有功能上。
TOOL_HINTS = {
    "context.load": "kemai.course_brief（先了解这门课的章节与资料）",
    "curriculum.resolve": "kemai.course_brief（确认教材章节）",
    "practice.generate_items": "练习生成与 kemai.practice_read（读题库）",
    "practice.grade_answers": "判题与错题本",
    "mistake.query_recent": "kemai.wrong_read（读最近错题）",
    "mistake.cluster": "kemai.wrong_read（归纳同类错因）",
    "memory.write": "把结论写进课程记忆",
    "plan.generate": "复习笔记 / 复习计划",
    "workflow.create": "按步骤把成果落到笔记或练习里",
    "report.aggregate_metrics": "错题本与练习记录汇总",
    "organization.query_context": "kemai.course_brief",
    "entitlement.check": "",
}


@lru_cache(maxsize=1)
def _catalog() -> list[dict]:
    path = ROOT / "catalog.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in payload.get("skills", []) if isinstance(item, dict)]


def _labels(values, mapping=None) -> list[str]:
    if isinstance(values, str):
        values = [values]
    return [(mapping or {}).get(str(value), str(value)) for value in (values or [])]


def facets() -> dict:
    from collections import Counter

    items = _catalog()
    def count(key, mapping=None):
        counter = Counter()
        for item in items:
            for value in _labels(item.get(key), mapping):
                if value:
                    counter[value] += 1
        return [{"value": value, "count": count_} for value, count_ in counter.most_common()]

    return {
        "total": len(items),
        "categories": count("category", CATEGORY_LABELS),
        "stages": count("stages", STAGE_LABELS),
        "subjects": count("subjects"),
        "scenarios": count("scenarios"),
    }


def _brief(item: dict) -> dict:
    return {
        "slug": item.get("slug") or item.get("name") or "",
        "title": item.get("title") or item.get("name") or "",
        "description": item.get("description") or "",
        "category": CATEGORY_LABELS.get(item.get("category", ""), item.get("category", "")),
        "stages": _labels(item.get("stages"), STAGE_LABELS),
        "grades": item.get("grades") or [],
        "subjects": item.get("subjects") or [],
        "abilities": item.get("abilities") or [],
        "scenarios": item.get("scenarios") or [],
        "requiresData": item.get("requiresData") or [],
        "requiresTools": item.get("requiresTools") or [],
        "path": item.get("path") or "",
    }


def search(q: str = "", category: str = "", stage: str = "", subject: str = "",
           limit: int = 60) -> list[dict]:
    query = q.strip().lower()
    result = []
    for item in _catalog():
        brief = _brief(item)
        if category and brief["category"] != category:
            continue
        if stage and stage not in brief["stages"]:
            continue
        if subject and subject not in brief["subjects"]:
            continue
        if query:
            haystack = " ".join([
                brief["title"], brief["description"], brief["slug"],
                " ".join(brief["subjects"]), " ".join(brief["abilities"]),
                " ".join(brief["scenarios"]),
            ]).lower()
            if query not in haystack:
                continue
        result.append(brief)
    return result[:max(1, min(limit, 200))]


def detail(slug: str) -> dict | None:
    for item in _catalog():
        brief = _brief(item)
        if brief["slug"] != slug:
            continue
        path = ROOT / brief["path"]
        body = ""
        if path.is_file():
            try:
                body = path.read_text(encoding="utf-8", errors="replace")[:12000]
            except OSError:
                body = ""
        return {**brief, "markdown": body}
    return None


def build_goal(slug: str, answers: dict) -> str | None:
    """把场景 + 用户填的参数拼成一段能直接交给 Agent 的目标。"""
    item = detail(slug)
    if item is None:
        return None
    lines = [
        f"【教育场景：{item['title']}】",
        item["description"],
        "",
        f"适用：{'、'.join(item['stages']) or '不限'} / {'、'.join(item['subjects']) or '综合'}；"
        f"场景类型：{'、'.join(item['scenarios']) or '通用'}。",
    ]
    filled = [(str(key), str(value).strip()) for key, value in (answers or {}).items()
              if str(value).strip()]
    if filled:
        lines += ["", "本次的具体条件："]
        lines += [f"- {key}：{value[:200]}" for key, value in filled[:12]]
    hints = [TOOL_HINTS.get(tool, "") for tool in item["requiresTools"]]
    hints = [hint for hint in hints if hint]
    if hints:
        lines += ["", "优先使用课脉里已有的能力：" + "；".join(dict.fromkeys(hints)) + "。"]
    lines += ["", "请按这个场景给出今天能完成、可检查的具体成果，不要只列建议。"]
    return "\n".join(lines)
