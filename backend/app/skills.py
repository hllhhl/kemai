"""Skills：把 Markdown 技能包读进来。

格式（和 WorkBuddy / Claude 一致）：
    <root>/<name>/SKILL.md
        ---
        name: xxx
        description: 一句话说明什么时候该用它
        ---
        正文（流程、命令、避坑清单）

课脉只做两件事：**列出来**、**按需读全文**。
不复制、不改写用户的技能包，直接读原目录——改了原文件，课脉这边立刻生效。

为什么"按需"：一个 skill 全文常常几千字，23 个全塞进上下文就是几万 token。
所以清单阶段只取 frontmatter 的 name 和 description，命中之后才读正文。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re

logger = logging.getLogger(__name__)

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


def skill_roots() -> list[Path]:
    """用户级在前，项目级在后（项目级同名时优先）。"""
    roots = [Path.home() / ".workbuddy" / "skills"]
    project = Path(__file__).resolve().parents[2] / ".workbuddy" / "skills"
    if project.exists():
        roots.append(project)
    return roots


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """返回 (元数据, 正文)。没有 frontmatter 就返回空元数据 + 全文。

    手写解析而不是用 yaml：技能包的 frontmatter 经常带长句、冒号、引号，
    完整 YAML 解析器在这些地方反而容易炸，而我们只需要 key: value。
    """
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text
    meta: dict = {}
    key = None
    for raw in match.group(1).splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t" and key:
            # 续行（YAML 的折叠写法）
            meta[key] = (str(meta.get(key) or "") + " " + line.strip()).strip()
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        meta[key] = value
    return meta, text[match.end():]


TROUBLE_SECTIONS = re.compile(r"已知问题|避坑|常见错误|常见坑|缺陷|失败模式|Troubleshoot", re.I)


def lint_skill(meta: dict, body: str) -> list[str]:
    """按成熟技能包的范式检查缺什么，只提示不拦截。

    范本来自两处：addyosmani/agent-skills（触发条件—步骤—验证—反例四段式）
    以及本机 ltspice-mcp 那份 SKILL.md（本机路径表 + 标准工作流 + **已确认的
    缺陷与绕行**）。后者最值钱：那两条缺陷是踩过之后写下来的，没有它们，
    下一个人（或 Agent）会再踩一遍。

    技能是用户自己的资产，缺字段不该拦着不让用，所以在界面上列出来即可。
    """
    problems: list[str] = []
    if not str(meta.get("description") or "").strip():
        problems.append("缺 description：模型只能看到简介，没写就不知道什么时候该用它")
    if not str(meta.get("allowed-tools") or "").strip():
        problems.append("没写 allowed-tools：会拿到全部工具，权限边界不明确")
    if len(body.strip()) < 200:
        problems.append("正文过短：缺少步骤或注意事项，模型读了也难照着做")
    elif not TROUBLE_SECTIONS.search(body):
        problems.append("缺「已知问题 / 避坑」段落：这类踩坑经验最容易被重复踩")
    return problems


def describe(path: Path, root: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta, body = parse_frontmatter(text)
    name = str(meta.get("name") or path.parent.name)
    description = str(meta.get("description") or "").strip()
    if not description:
        # 没写 description 就取正文第一段非标题行，界面上总得有个说明
        for line in body.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                description = stripped[:160]
                break
    return {
        "name": name,
        "description": description,
        "path": str(path),
        "scope": "project" if root == skill_roots()[-1] and len(skill_roots()) > 1 else "user",
        "chars": len(text),
        "words": len(body.split()),
        "agent_created": str(meta.get("agent_created") or "").lower() == "true",
        # 技能可以声明它允许用哪些工具，形如 Bash(browser-use:*) 或 ltspice.*
        "allowed_tools": [t.strip() for t in str(meta.get("allowed-tools") or "").split(",") if t.strip()],
        "version": str(meta.get("version") or ""),
        "problems": lint_skill(meta, body),
    }


def list_skills() -> list[dict]:
    found: dict[str, dict] = {}
    for root in skill_roots():
        if not root.exists():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            path = entry / "SKILL.md"
            if not path.exists():
                continue
            item = describe(path, root)
            if item is None:
                continue
            found[item["name"]] = item  # 后出现的（项目级）覆盖前面的
    return sorted(found.values(), key=lambda item: item["name"])


def load_skill(name: str) -> dict:
    for skill in list_skills():
        if skill["name"] != name:
            continue
        text = Path(skill["path"]).read_text(encoding="utf-8", errors="replace")
        _, body = parse_frontmatter(text)
        return {**skill, "body": body}
    raise KeyError(name)


def match_skills(query: str, limit: int = 5) -> list[dict]:
    """按关键词粗排：名字命中 > 描述命中。够用就行，真正的语义匹配交给模型。"""
    q = (query or "").strip().lower()
    if not q:
        return []
    terms = [t for t in re.split(r"[\s,，。;；]+", q) if t]
    scored = []
    for skill in list_skills():
        name = skill["name"].lower()
        desc = skill["description"].lower()
        score = 0
        for term in terms:
            if term in name:
                score += 3
            if term in desc:
                score += 1
        if score:
            scored.append((score, skill))
    scored.sort(key=lambda pair: -pair[0])
    return [skill for _, skill in scored[:limit]]
