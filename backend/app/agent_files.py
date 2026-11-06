"""让 Agent 真的能动手：操作文件、跑代码、改课脉自己的源码。

之前 Agent 只能从一张写死的工具清单里挑（MCP 工具 + 15 个课内工具），
相当于把它关在柜台后面：能查、能答，不能碰到电脑上的任何东西，更不可能
改软件本身。这个模块把「手」还给它。

边界是刻意保留的，不是能力不够：

1. 只对**可信根目录**开放。默认两个：课脉项目根（源码/前端）、用户数据目录；
   再加一个 data/agent-workspace 给 Agent 自由发挥。桌面、下载、文档这些
   个人目录**默认不开**，要动得用户显式加进来——整理文件可以，但不能让
   模型自己去翻个人目录。
2. 所有路径先 resolve 再校验前缀，挡住 ../ 穿越和符号链接跳出。
3. 写之前自动备份到 data/agent-backups/<时间戳>/，随时能还原。
4. 写工具在 catalog 里标 read_only=False，走哨兵 ASK，用户点同意才执行。
5. run_python 限定工作目录 + 超时 + 输出截断，不提供 shell 直通。

「Agent 能改课脉本身的设计」是用户明确要的。但改源码 ≠ 立刻生效：Python
后端要重启、前端要重新构建，工具会把这点如实告诉模型，不假装改完就好了。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import Settings

TEXT_SUFFIXES = {".py", ".vue", ".ts", ".js", ".jsx", ".css", ".json", ".md", ".txt", ".yaml", ".yml", ".mjs", ".cjs"}
EDITABLE_SUFFIXES = {".py", ".vue", ".ts", ".js", ".jsx", ".css", ".json", ".md", ".mjs", ".cjs"}
MAX_READ = 60_000
MAX_WRITE = 200_000
RUN_TIMEOUT = 60


def project_root() -> Path:
    """开发版是 D:\\kemai；安装版是 resources/。两者都是软件自己的根。"""
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    return Path(Settings.from_env().data_dir)


def workspace_dir() -> Path:
    path = data_dir() / "agent-workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def roots() -> list[Path]:
    """可信根目录。用户想放开别的目录，往 data/agent-roots.json 里加绝对路径。"""
    result = [project_root().resolve(), data_dir().resolve(), workspace_dir().resolve()]
    custom = data_dir() / "agent-roots.json"
    if custom.is_file():
        try:
            extra = json.loads(custom.read_text(encoding="utf-8"))
            for item in extra.get("roots", []) if isinstance(extra, dict) else extra:
                candidate = Path(str(item)).expanduser().resolve()
                if candidate.is_dir():
                    result.append(candidate)
        except (OSError, ValueError, TypeError):
            pass
    # 去重，长路径排前面（后面的 startswith 判定才不会被父目录抢先）
    unique: list[Path] = []
    for root in result:
        if root not in unique:
            unique.append(root)
    return sorted(unique, key=lambda p: -len(str(p)))


def safe_path(raw: str, *, must_exist: bool = False) -> tuple[Path | None, str]:
    """把用户/模型给的路径收进可信根目录。返回 (路径, 错误说明)。"""
    text = str(raw or "").strip()
    if not text:
        return None, "路径不能为空"
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = workspace_dir() / candidate
    try:
        resolved = candidate.expanduser().resolve()
    except (OSError, RuntimeError):
        return None, "路径无法解析"
    for root in roots():
        if resolved == root or root in resolved.parents:
            if must_exist and not resolved.exists():
                return None, f"路径不存在：{resolved}"
            return resolved, ""
    return None, (
        f"路径不在可信目录内：{resolved}。"
        f"当前开放的根目录：{', '.join(str(r) for r in roots())}。"
        "要操作别的目录，把绝对路径写进 data/agent-roots.json 的 roots 列表。"
    )


def _backup(path: Path) -> str:
    """写之前留一份原件，返回备份位置（新建文件返回空串）。"""
    if not path.is_file():
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000}"
    folder = data_dir() / "agent-backups" / stamp
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / path.name
    shutil.copy2(path, target)
    (folder / "manifest.json").write_text(
        json.dumps({"original": str(path), "backup": str(target), "at": stamp}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(target)


def _roots_text() -> str:
    return chr(10).join(f"- {root}" for root in roots())


def fs_list(args: dict) -> dict:
    path, error = safe_path(args.get("path") or ".", must_exist=True)
    if error:
        return {"ok": False, "text": error}
    if not path.is_dir():
        return {"ok": False, "text": f"不是目录：{path}"}
    try:
        items = sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name))[:200]
    except OSError as exc:
        return {"ok": False, "text": f"列目录失败：{exc}"}
    lines = [f"目录 {path}（{len(items)} 项）"]
    for item in items:
        kind = "目录" if item.is_dir() else f"{item.stat().st_size} B"
        lines.append(f"- {item.name} | {kind}")
    return {"ok": True, "text": chr(10).join(lines)}


def fs_read(args: dict) -> dict:
    path, error = safe_path(args.get("path") or "", must_exist=True)
    if error:
        return {"ok": False, "text": error}
    if not path.is_file():
        return {"ok": False, "text": f"不是文件：{path}"}
    if path.suffix.lower() not in TEXT_SUFFIXES and path.suffix.lower() not in {".log", ".csv", ".toml"}:
        return {"ok": False, "text": f"只能读文本类文件，收到 {path.suffix or '无扩展名'}"}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "text": f"读取失败：{exc}"}
    offset = max(0, int(args.get("offset") or 0))
    limit = max(200, min(int(args.get("limit") or 12000), MAX_READ))
    return {"ok": True, "text": f"文件 {path}（共 {len(text)} 字符，从 {offset} 起）："
                               + chr(10) + text[offset:offset + limit]}


def fs_write(args: dict) -> dict:
    path, error = safe_path(args.get("path") or "")
    if error:
        return {"ok": False, "text": error}
    content = str(args.get("content") or "")
    if len(content) > MAX_WRITE:
        return {"ok": False, "text": f"内容超过 {MAX_WRITE} 字符，拆开写"}
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return {"ok": False, "text": f"只能写文本类文件，收到 {path.suffix or '无扩展名'}"}
    try:
        backup = _backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "text": f"写入失败：{exc}"}
    note = f"已备份到 {backup}" if backup else "新建文件，无需备份"
    return {"ok": True, "text": f"已写入 {path}（{len(content)} 字符）。{note}。"}


def fs_organize(args: dict) -> dict:
    """按扩展名把一批文件归到子目录；只移动，不删除。"""
    folder, error = safe_path(args.get("path") or "", must_exist=True)
    if error:
        return {"ok": False, "text": error}
    if not folder.is_dir():
        return {"ok": False, "text": f"不是目录：{folder}"}
    dry = bool(args.get("dry_run", True))
    moved: list[str] = []
    for item in sorted(folder.iterdir()):
        if not item.is_file() or item.name.startswith("."):
            continue
        group = (item.suffix.lower().lstrip(".") or "misc") + " 文件"
        target_dir = folder / group
        target = target_dir / item.name
        if target.exists():
            moved.append(f"跳过（同名已存在）：{item.name}")
            continue
        if dry:
            moved.append(f"将移动 {item.name} → {group}/")
            continue
        try:
            _backup(item)
            target_dir.mkdir(exist_ok=True)
            shutil.move(str(item), str(target))
            moved.append(f"已移动 {item.name} → {group}/")
        except OSError as exc:
            moved.append(f"失败 {item.name}：{exc}")
    heading = "预演（没动文件，确认时把 dry_run 设为 false 再调一次）" if dry else "整理结果"
    return {"ok": True, "text": heading + "：" + chr(10) + chr(10).join(moved[:80])}


def self_edit(args: dict) -> dict:
    """改课脉自己的源码。这是「Agent 能改软件本身」的落点。"""
    raw = str(args.get("path") or "").strip()
    if not raw:
        return {"ok": False, "text": "path 不能为空"}
    root = project_root().resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        path = candidate.resolve()
    except (OSError, RuntimeError):
        return {"ok": False, "text": "路径无法解析"}
    if root not in path.parents and path != root:
        return {"ok": False, "text": f"self_edit 只能改课脉项目内的文件（根目录 {root}）"}
    if path.suffix.lower() not in EDITABLE_SUFFIXES:
        return {"ok": False, "text": f"self_edit 只改源码/配置文本，收到 {path.suffix}"}
    if not path.is_file():
        return {"ok": False, "text": f"文件不存在：{path}"}

    action = str(args.get("action") or "replace").strip()
    original = path.read_text(encoding="utf-8", errors="replace")
    if action == "replace":
        old = str(args.get("old") or "")
        new = str(args.get("new") or "")
        if not old:
            return {"ok": False, "text": "replace 需要 old 文本"}
        if old not in original:
            return {"ok": False, "text": "old 片段在文件里没找到，先用 fs_read 确认原文"}
        updated = original.replace(old, new, int(args.get("count") or 1))
    elif action == "append":
        updated = original.rstrip() + chr(10) + chr(10) + str(args.get("text") or "")
    elif action == "write":
        updated = str(args.get("content") or "")
        if not updated.strip():
            return {"ok": False, "text": "write 的 content 不能为空"}
    else:
        return {"ok": False, "text": f"不支持的 action：{action}（可用 replace / append / write）"}
    if updated == original:
        return {"ok": True, "text": "内容没有变化，没改动文件"}

    backup = _backup(path)
    path.write_text(updated, encoding="utf-8")
    record = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"), "file": str(path),
        "action": action, "backup": backup,
        "before_chars": len(original), "after_chars": len(updated),
    }
    log = data_dir() / "self-edits.jsonl"
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + chr(10))
    suffix = (".py" if path.suffix == ".py" else "")
    return {"ok": True, "text": (
        f"已改 {path}（{len(original)} → {len(updated)} 字符）。备份：{backup}。"
        + (" 这是 Python 后端源码，要**重启课脉**才生效；" if suffix else " 前端源码要重新构建才生效；")
        + " 改动已记到 data/self-edits.jsonl。"
    )}


def run_python(args: dict) -> dict:
    """在可信工作区里跑一段 Python。让 Agent 能自己写代码完成任务，而不是只能挑现成工具。"""
    code = str(args.get("code") or "").strip()
    if not code:
        return {"ok": False, "text": "code 不能为空"}
    if len(code) > 20_000:
        return {"ok": False, "text": "代码超过 20000 字符"}
    work, error = safe_path(args.get("cwd") or ".")
    if error:
        return {"ok": False, "text": error}
    if not work.is_dir():
        return {"ok": False, "text": f"工作目录不存在：{work}"}
    timeout = max(5, min(int(args.get("timeout") or RUN_TIMEOUT), 180))
    env = {key: value for key, value in os.environ.items() if key != "ELECTRON_RUN_AS_NODE"}
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-c", code],
            cwd=str(work), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": f"代码执行超时（{timeout} 秒）"}
    except OSError as exc:
        return {"ok": False, "text": f"启动失败：{exc}"}
    out = (done.stdout or "")[:8000]
    err = (done.stderr or "")[:4000]
    text = f"退出码 {done.returncode}" + chr(10) + "输出：" + chr(10) + (out or "（空）")
    if err:
        text += chr(10) + "错误：" + chr(10) + err
    return {"ok": done.returncode == 0, "text": text + chr(10) + f"工作目录：{work}"}


def self_status(args: dict) -> dict:
    """Agent 查自己能碰哪些地方、能改什么。"""
    return {"ok": True, "text": (
        "Agent 当前的可信根目录：" + chr(10) + _roots_text() + chr(10) + chr(10)
        + "可改的源码类型：" + "、".join(sorted(EDITABLE_SUFFIXES)) + chr(10)
        + "写操作都会先备份到 data/agent-backups/，并记到 data/self-edits.jsonl。" + chr(10)
        + "所有写工具在 catalog 里标 read_only=False，执行前会弹确认。"
    )}
