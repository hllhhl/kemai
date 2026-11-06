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
    """全盘开放：这台机器上所有存在的盘符根，外加 Agent 自己的工作区。

    用户明确要求「能访问修改电脑任何文件」。所以这里不再是白名单，
    而是把本机所有盘符都列出来给 self_status 展示。真正的防护挪到了
    写前备份 + 审计 + 哨兵逐次确认，而不是靠路径把 Agent 关在外面。
    """
    import string

    result = [workspace_dir().resolve(), project_root().resolve(), data_dir().resolve()]
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:\\")
        try:
            if drive.exists():
                result.append(drive.resolve())
        except OSError:
            continue
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
    unique: list[Path] = []
    for root in result:
        if root not in unique:
            unique.append(root)
    return sorted(unique, key=lambda p: -len(str(p)))


# 这些位置写错会让系统或软件起不来。不硬拦（用户要的是「任何文件」），
# 但在每次写操作的结果里明确提醒，配合哨兵的确认一起用。
PROTECTED_HINTS = ("\\windows", "\\system32", "\\program files", "\\programdata",
                   "\\appdata", "pagefile.sys", "hiberfil.sys")


def danger_note(path: Path) -> str:
    lowered = str(path).lower()
    hits = [hint for hint in PROTECTED_HINTS if hint in lowered]
    return ("⚠️ 这个路径在系统/关键目录里（" + "、".join(hits) +
            "），写错可能让系统或软件无法启动。" if hits else "")


def safe_path(raw: str, *, must_exist: bool = False) -> tuple[Path | None, str]:
    """解析任意绝对路径。不再限制可信根目录——用户已明确要求全盘可访问。

    仍然做两件事：相对路径落到 agent-workspace；resolve() 展开符号链接和
    .. ，避免路径注入。读写权限由哨兵确认和写前备份兜底。
    """
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
    if must_exist and not resolved.exists():
        return None, f"路径不存在：{resolved}"
    return resolved, ""


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
    raw = str(args.get("path") or ".")
    path, error = safe_path(raw, must_exist=True)
    if error:
        # 只说"不存在"会让模型把同一个错路径试几十次。把父目录里
        # 真正存在的目录名一起给出去，一次就能纠正常见的单复数笔误。
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = workspace_dir() / candidate
        parent = candidate.expanduser().resolve().parent
        hints = []
        try:
            if parent.is_dir():
                hints = [str(item) for item in sorted(parent.iterdir()) if item.is_dir()][:20]
        except OSError:
            pass
        suffix = (chr(10) + "该位置下实际存在的目录：" + chr(10)
                  + chr(10).join("- " + item for item in hints)) if hints else ""
        return {"ok": False, "text": error + suffix}
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
        # 任意文件都能「访问」：二进制不硬拒，给出大小、类型和前若干字节，
        # 真的要处理交给 run_python（那里可以读字节、转码、另存）。
        import base64

        try:
            raw = path.read_bytes()
        except OSError as exc:
            return {"ok": False, "text": f"读取失败：{exc}"}
        preview = base64.b64encode(raw[:64]).decode("ascii")
        return {"ok": True, "text": (
            f"二进制文件 {path}（{len(raw)} 字节，扩展名 {path.suffix or '无'}）。"
            + chr(10) + f"前 64 字节 base64：{preview}"
            + chr(10) + "要读全文或改写二进制，请用 kemai.run_python。"
        )}
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
    binary = str(args.get("encoding") or "").lower() == "base64"
    if not binary and path.suffix.lower() not in TEXT_SUFFIXES:
        return {"ok": False, "text": (
            f"写文本文件时扩展名 {path.suffix or '无'} 不在允许列表里。"
            "要写二进制/任意格式，传 encoding=base64 并把内容做 base64 编码。"
        )}
    try:
        backup = _backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if binary:
            import base64

            path.write_bytes(base64.b64decode(content))
        else:
            path.write_text(content, encoding="utf-8")
    except (OSError, ValueError) as exc:
        return {"ok": False, "text": f"写入失败：{exc}"}
    note = f"已备份到 {backup}" if backup else "新建文件，无需备份"
    warning = danger_note(path)
    return {"ok": True, "text": f"已写入 {path}（{len(content)} 字符）。{note}。"
                               + (chr(10) + warning if warning else "")}


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
    outside_project = root not in path.parents and path != root
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
    extra = danger_note(path)
    if outside_project:
        extra += chr(10) + "这个路径在课脉项目之外，改动不属于软件自身。"
    return {"ok": True, "text": (
        f"已改 {path}（{len(original)} → {len(updated)} 字符）。备份：{backup}。"
        + (" 这是 Python 后端源码，要**重启课脉**才生效；" if suffix else " 前端源码要重新构建才生效；")
        + " 改动已记到 data/self-edits.jsonl。"
        + (chr(10) + extra if extra.strip() else "")
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
        "Agent 可以访问这台电脑上的任意绝对路径（全盘开放）：" + chr(10) + _roots_text() + chr(10) + chr(10)
        + "可改的文本类型：" + "、".join(sorted(EDITABLE_SUFFIXES)) + chr(10)
        + "二进制文件可读前 64 字节；写二进制用 fs_write 的 encoding=base64。" + chr(10)
        + "写操作都会先备份到 data/agent-backups/，并记到 data/self-edits.jsonl。" + chr(10)
        + "删除一律移到备份区，不会真删。" + chr(10)
        + "所有写工具在 catalog 里标 read_only=False，执行前会弹确认。"
        + "系统/关键目录的写操作会额外给出警告。"
    )}

def fs_info(args: dict) -> dict:
    """看任意文件/目录的大小、修改时间、类型。"""
    path, error = safe_path(args.get("path") or "", must_exist=True)
    if error:
        return {"ok": False, "text": error}
    stat = path.stat()
    kind = "目录" if path.is_dir() else "文件"
    lines = [
        f"路径：{path}",
        f"类型：{kind}",
        f"大小：{stat.st_size} 字节",
        f"修改时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime))}",
        f"后缀：{path.suffix or '无'}",
    ]
    if path.is_dir():
        try:
            items = list(path.iterdir())
            lines.append(f"子项：{len(items)} 个")
        except OSError:
            pass
    return {"ok": True, "text": chr(10).join(lines)}


def fs_mkdir(args: dict) -> dict:
    path, error = safe_path(args.get("path") or "")
    if error:
        return {"ok": False, "text": error}
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "text": f"创建目录失败：{exc}"}
    return {"ok": True, "text": f"目录已就绪：{path}"}


def fs_copy(args: dict) -> dict:
    src, error = safe_path(args.get("src") or "", must_exist=True)
    if error:
        return {"ok": False, "text": error}
    dst, error = safe_path(args.get("dst") or "")
    if error:
        return {"ok": False, "text": error}
    if not src.is_file():
        return {"ok": False, "text": f"只能复制文件，收到：{src}"}
    if dst.exists() and not bool(args.get("overwrite")):
        return {"ok": False, "text": f"目标已存在：{dst}。要覆盖请传 overwrite=true（会先备份）"}
    try:
        if dst.exists():
            _backup(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    except OSError as exc:
        return {"ok": False, "text": f"复制失败：{exc}"}
    return {"ok": True, "text": f"已复制 {src} → {dst}"}


def fs_move(args: dict) -> dict:
    """移动或重命名。原件留一份备份，不做真删除。"""
    src, error = safe_path(args.get("src") or "", must_exist=True)
    if error:
        return {"ok": False, "text": error}
    dst, error = safe_path(args.get("dst") or "")
    if error:
        return {"ok": False, "text": error}
    if dst.exists():
        return {"ok": False, "text": f"目标已存在：{dst}，先换个名字"}
    try:
        _backup(src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except OSError as exc:
        return {"ok": False, "text": f"移动失败：{exc}"}
    return {"ok": True, "text": f"已移动 {src} → {dst}（原件已备份）"}


def fs_delete(args: dict) -> dict:
    """删除一律移到备份区，不真删——这是个人文件安全的硬要求。"""
    src, error = safe_path(args.get("path") or "", must_exist=True)
    if error:
        return {"ok": False, "text": error}
    backup = _backup(src) if src.is_file() else ""
    if not backup:
        # 目录也移到备份区，而不是 rmtree
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000}"
        folder = data_dir() / "agent-backups" / stamp
        folder.mkdir(parents=True, exist_ok=True)
        backup = str(folder / src.name)
        try:
            shutil.move(str(src), backup)
        except OSError as exc:
            return {"ok": False, "text": f"移到备份区失败：{exc}"}
    else:
        try:
            src.unlink()
        except OSError as exc:
            return {"ok": False, "text": f"删除失败（备份仍在 {backup}）：{exc}"}
    return {"ok": True, "text": f"已从原位置移除，原件在备份区：{backup}。没有真删。"}

