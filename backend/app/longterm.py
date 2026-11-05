"""长期记忆（文件层）：照 WorkBuddy 的布局来。

位置（默认 data/memory，可用 KEMAI_MEMORY_DIR 覆盖）：

    IDENTITY.md        agent 是谁（名字、定位）
    SOUL.md            性格与边界（怎么说话、什么不做）
    USER.md            关于用户（习惯、偏好）
    MEMORY.md          跨课程的长期笔记
    courses/<id>.md    这门课长期记什么（约定、踩过的坑）
    logs/YYYY-MM-DD.md 每日日志，只追加

为什么不塞进数据库：用户要能直接打开看、改、备份、删。看不见的"记忆"
他不会信，也不会去纠正；出问题时 diff 一眼能看出哪次改坏了。

和 memory.py（会话记忆）的分工：
    memory.py    这一次对话用的偏好/主题，带 epoch，会失效
    longterm.py  跨会话的长期事实，用户可编辑，模型只能按规则改
两者混在一处，要么上下文被旧账撑爆，要么重要内容跟着过期一起被清掉。
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DIGEST_BUDGET = 4000
WRITABLE = ("user", "memory", "course", "soul", "identity")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")

TEMPLATES = {
    "IDENTITY.md": """# IDENTITY.md - 我是谁

- **名字**：课脉
- **定位**：课程知识体系助手，管这门课的笔记、题库、资料
- **风格**：直接，先给结论和依据，不铺垫

""",
    "SOUL.md": """# SOUL.md - 我怎么做事

## 核心
- **先查再问**：能自己查到的（课程结构、笔记、题库）就先查，别把问题原样丢回给用户。
- **有依据才说**：数字和结论要指到工具输出；查不到就说查不到。
- **改动前说清**：会覆盖、会删除、会花钱的动作，先说清代价。

## 边界
- 不编造课件里没有的内容。
- 不替用户做不可撤销的决定。
- 一次只做被要求的事，不顺手扩大范围。

""",
    "USER.md": """# USER.md - 关于你

- **怎么称呼**：
- **习惯**：
- **不喜欢的**：

""",
    "MEMORY.md": """# 长期记忆（跨课程）

""",
}


def root() -> Path:
    base = os.getenv("KEMAI_MEMORY_DIR")
    if base:
        return Path(base)
    from .config import Settings

    return Path(Settings.from_env().data_dir) / "memory"


def ensure() -> Path:
    """建目录与默认文件。只在不存在时写，绝不覆盖用户改过的内容。"""
    base = root()
    (base / "courses").mkdir(parents=True, exist_ok=True)
    (base / "logs").mkdir(parents=True, exist_ok=True)
    for name, text in TEMPLATES.items():
        target = base / name
        if not target.exists():
            target.write_text(text, encoding="utf-8")
    return base


def _course_file(course_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", course_id)[:40] or "unknown"
    path = root() / "courses" / f"{safe}.md"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# 课程记忆 {course_id}\n\n", encoding="utf-8")
    return path


def read(scope: str, course_id: str = "") -> str:
    ensure()
    fixed = {"identity": "IDENTITY.md", "soul": "SOUL.md", "user": "USER.md", "memory": "MEMORY.md"}
    if scope in fixed:
        return (root() / fixed[scope]).read_text(encoding="utf-8", errors="replace")
    if scope == "course":
        if not course_id:
            raise ValueError("读课程记忆需要 course_id")
        return _course_file(course_id).read_text(encoding="utf-8", errors="replace")
    if scope == "log":
        return latest_log()
    raise ValueError(f"未知的 scope：{scope}")


def write(scope: str, content: str, course_id: str = "", append: bool = False) -> dict:
    """写长期记忆。

    append=True 是日常用法（往课程记忆里补一条约定）；
    append=False 是整份替换，只在用户明确说"把那条改掉"时用——
    否则模型一次"整理"就把用户手写的东西洗掉了。
    """
    if scope not in WRITABLE:
        raise ValueError(f"{scope} 不能直接写；日志请用 append_log")
    ensure()
    if scope == "course":
        if not course_id:
            raise ValueError("写课程记忆需要 course_id")
        target = _course_file(course_id)
    else:
        target = root() / {
            "user": "USER.md", "memory": "MEMORY.md",
            "soul": "SOUL.md", "identity": "IDENTITY.md",
        }[scope]

    text = content.strip()
    if append:
        old = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
        merged = old.rstrip() + "\n" + text + "\n"
    else:
        merged = text + "\n"

    # 先写临时文件再替换：写一半中断不会留下半个记忆文件
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(merged, encoding="utf-8")
    tmp.replace(target)
    return {"path": str(target), "bytes": len(merged.encode("utf-8")), "append": append}


def append_log(text: str, course_id: str = "") -> dict:
    """每日日志：只追加，带时间和课程，事后能对上哪次做的。"""
    ensure()
    day = datetime.now().strftime("%Y-%m-%d")
    path = root() / "logs" / f"{day}.md"
    stamp = datetime.now().strftime("%H:%M")
    head = f"- **{stamp}**" + (f" · 课程 `{course_id[:8]}`" if course_id else "")
    line = f"{head}\n  {text.strip()}\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return {"path": str(path), "bytes": len(line.encode("utf-8"))}


def latest_log() -> str:
    folder = root() / "logs"
    logs = sorted(folder.glob("*.md")) if folder.exists() else []
    return logs[-1].read_text(encoding="utf-8", errors="replace") if logs else ""


def digest(course_id: str = "", budget: int = DIGEST_BUDGET) -> str:
    """拼给模型看的记忆片段。

    顺序按"越靠前越重要"：身份 → 性格 → 用户 → 跨课程 → 这门课。
    超预算从后往前截——先保证它知道自己是谁、用户是谁。
    """
    ensure()
    parts: list[str] = []

    def add(label: str, text: str, cap: int) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        if len(cleaned) > cap:
            cleaned = cleaned[:cap] + "\n…（后面还有，要全文请用 memory_read）"
        parts.append(f"## {label}\n{cleaned}")

    try:
        add("我是谁（IDENTITY.md）", read("identity"), 400)
        add("怎么做（SOUL.md）", read("soul"), 900)
        add("关于用户（USER.md）", read("user"), 600)
        add("跨课程记忆（MEMORY.md）", read("memory"), 1000)
        if course_id:
            add("这门课的记忆", read("course", course_id), 800)
    except Exception:  # noqa: BLE001 - 记忆读不出来不该让 agent 起不来
        logger.warning("记忆摘要拼装失败", exc_info=True)
        return ""

    text = "\n\n".join(parts)
    if len(text) > budget:
        text = text[:budget] + "\n…（记忆过长，已截断）"
    return text


def resolve(name: str) -> Path:
    """把 "USER.md"、"courses/xxx.md" 解析成真实路径，并挡住越界。"""
    base = ensure().resolve()
    target = (base / name.replace("\\", "/")).resolve()
    if base != target and base not in target.parents:
        raise ValueError("记忆文件名不合法")
    return target


def read_file(name: str) -> str:
    target = resolve(name)
    if not target.exists():
        raise ValueError(f"没有这个记忆文件：{name}")
    return target.read_text(encoding="utf-8", errors="replace")


def write_file(name: str, content: str) -> dict:
    """整份写一个记忆文件——这是给"用户自己编辑"用的入口，不走模型。"""
    target = resolve(name)
    if target.suffix != ".md":
        raise ValueError("只支持 .md")
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)
    return {"path": str(target), "bytes": len(content.encode("utf-8"))}


def inventory() -> list[dict]:
    """给界面用的清单：有哪些记忆文件、多大、最后改于何时。"""
    ensure()
    base = root()
    rows = []
    for path in sorted(base.rglob("*.md")):
        stat = path.stat()
        rows.append({
            "name": str(path.relative_to(base)).replace("\\", "/"),
            "bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        })
    return rows
