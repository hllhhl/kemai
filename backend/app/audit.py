"""审计日志：每次工具调用落一条，追加写，带哈希链。

照 WorkBuddy 的 `audit-log/YYYY-MM-DD.jsonl` 做，但只保留它真正有用的部分：

    · 一行一条 JSON，方便 grep / jq / 丢给别的工具
    · 记录**参数形状**而不是参数值——参数里可能是密钥、可能是用户原话
    · 决策（allow/ask/deny）与结果（ok/耗时）都记，才能事后回答"为什么它做了这个"
    · 每条带上一条的哈希，任何人删改中间一行都会断链

为什么需要它：课脉现在只在内存里留 steps，进程一重启就没了。
用户要问"昨天它到底调了什么、有没有先问过我"，现在答不上来。
审计日志比"我记得它调过"可靠，也比全量日志省事——它只记动作，不记内容。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_lock = threading.Lock()
GENESIS = "0" * 64


def root() -> Path:
    base = os.getenv("KEMAI_AUDIT_DIR")
    if base:
        return Path(base)
    from .config import Settings

    return Path(Settings.from_env().data_dir) / "audit"


def _today() -> Path:
    folder = root()
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"


def _last_hash(path: Path) -> str:
    """取文件最后一条的 hash，作为新记录的 prevHash。"""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            # 从尾部倒着找最后一行，避免读整个文件
            step = 4096
            data = b""
            while size > 0 and data.count(b"\n") < 2:
                take = min(step, size)
                size -= take
                handle.seek(size)
                data = handle.read(take) + data
        line = [row for row in data.splitlines() if row.strip()][-1]
        return str(json.loads(line).get("hash") or GENESIS)
    except Exception:  # noqa: BLE001 - 文件不存在或最后一行坏掉，就从创世哈希接上
        return GENESIS


def _shape(value, depth: int = 0):
    """参数只留形状：字符串记长度，列表记条数，对象记键名。"""
    if isinstance(value, dict):
        if depth >= 2:
            return {"keys": len(value)}
        return {str(k): _shape(v, depth + 1) for k, v in list(value.items())[:10]}
    if isinstance(value, str):
        return f"str[{len(value)}]"
    if isinstance(value, (list, tuple)):
        return f"list[{len(value)}]"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return type(value).__name__


def record(
    *,
    tool: str,
    args: dict | None = None,
    decision: str = "allow",
    ok: bool = True,
    ms: int = 0,
    run_id: str = "",
    conversation_id: str = "",
    course_id: str = "",
    error: str = "",
    risk: str = "",
    approved_by: str = "",
) -> dict:
    """写一条审计。失败不抛异常——审计坏掉不该让正在跑的任务失败。"""
    entry = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "tool": tool,
        "args": _shape(args or {}),
        "decision": decision,
        "risk": risk,
        "ok": ok,
        "ms": ms,
        "run": run_id,
        "conversation": conversation_id,
        "course": course_id,
        "error": error[:200],
        "approved_by": approved_by,
    }
    try:
        path = _today()
        with _lock:
            entry["prev"] = _last_hash(path)
            entry["seq"] = sum(1 for _ in path.open("rb")) + 1 if path.exists() else 1
            payload = json.dumps(entry, ensure_ascii=False, sort_keys=True)
            entry["hash"] = hashlib.sha256((entry["prev"] + payload).encode("utf-8")).hexdigest()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        logger.warning("写审计日志失败", exc_info=True)
    return entry


def tail(limit: int = 50, day: str = "") -> list[dict]:
    """读最近几条，界面用来显示"它刚做了什么"。新的在前。"""
    path = (root() / f"{day}.jsonl") if day else _today()
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return list(reversed(rows))[:limit]


def verify(day: str = "") -> dict:
    """校验哈希链。用户怀疑"日志被人改过"时用它。"""
    path = (root() / f"{day}.jsonl") if day else _today()
    if not path.exists():
        return {"ok": True, "lines": 0, "broken_at": 0}
    prev = GENESIS
    count = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for count, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                return {"ok": False, "lines": count, "broken_at": count, "reason": "这一行不是合法 JSON"}
            if row.get("prev") != prev:
                return {"ok": False, "lines": count, "broken_at": count, "reason": "prev 对不上上一条的 hash"}
            body = {k: v for k, v in row.items() if k != "hash"}
            expect = hashlib.sha256((prev + json.dumps(body, ensure_ascii=False, sort_keys=True)).encode("utf-8")).hexdigest()
            if expect != row.get("hash"):
                return {"ok": False, "lines": count, "broken_at": count, "reason": "hash 与内容不符"}
            prev = row["hash"]
    return {"ok": True, "lines": count, "broken_at": 0}
