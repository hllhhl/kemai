"""技能质量检查：缺什么能看出来，但不拦着用。

范式来自 addyosmani/agent-skills 的四段式，以及本机 ltspice-mcp 那份 SKILL.md
（本机路径表 + 标准工作流 + 已确认的缺陷与绕行）。后者最值钱，所以缺「避坑」
会被单独指出来。
"""
from __future__ import annotations

from app.skills import lint_skill

FULL = """---
name: demo
description: 什么时候用它、不适用于什么
allowed-tools: demo.read, demo.write
---

# 用法

1. 先 inspect 确认模拟器与能力，不要跳步
2. 再 verify_circuit 做静态校验，把坏卡片拦在仿真之前
3. 最后 run_experiments 跑批，取数一律用 analyze_results 的 recipe

## 标准工作流

按上面的顺序做，别跳步。跳过第一步会在缺依赖时得到含糊的报错，
跳过第二步会让一次跑批白等十分钟。

## 已知问题

- 路径含空格会失败，先把整段路径用引号包住再传
- 暂存区只认绝对路径，相对路径会被解析到工作目录之下
"""


def test_a_complete_skill_has_no_complaints():
    meta = {"name": "demo", "description": "什么时候用", "allowed-tools": "demo.read"}
    assert lint_skill(meta, FULL) == []


def test_missing_description_is_called_out():
    problems = lint_skill({"name": "demo", "allowed-tools": "demo.read"}, FULL)
    assert any("description" in item for item in problems)


def test_missing_allowed_tools_is_called_out():
    problems = lint_skill({"name": "demo", "description": "什么时候用"}, FULL)
    assert any("allowed-tools" in item for item in problems)


def test_short_body_is_called_out():
    problems = lint_skill({"name": "demo", "description": "x", "allowed-tools": "a"}, "太短了")
    assert any("正文过短" in item for item in problems)


def test_body_without_pitfalls_is_called_out():
    """没写踩过的坑，等于把坑留给下一个人再踩一次。"""
    body = "# 用法\n\n" + ("照着这个顺序做就不会错。" * 20)
    problems = lint_skill({"name": "demo", "description": "x", "allowed-tools": "a"}, body)
    assert any("避坑" in item for item in problems)


def test_english_troubleshooting_heading_also_counts():
    body = "# Usage\n\n" + ("Do this then that. " * 20) + "\n\n## Troubleshooting\n\n- known issue"
    problems = lint_skill({"name": "demo", "description": "x", "allowed-tools": "a"}, body)
    assert not any("避坑" in item for item in problems)
