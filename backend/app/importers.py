"""导入技能包与 MCP 服务器。

不另造格式：技能就是 SKILL.md（YAML frontmatter + 正文），MCP 就是 mcp.json 的
mcpServers 段。这一层只做「把外面的东西搬进这两个位置」，不改语法、不发明中间格式
——否则你从别的 agent 导出的东西，到这里就得再翻译一遍。

有一条硬线：**MCP 服务器配置里带 command，导入它等于允许一条命令长期跑在这台机器上。**
所以：
    · 导入前把将要执行的完整命令行原样给用户看
    · 接口必须带 confirm=true，缺了直接拒
    · 名字只允许 [a-z0-9-_]，挡住 ../ 这类路径穿越
    · 已存在的同名 server 不静默覆盖，除非显式覆盖

技能包没那么危险（它是给模型看的文字），但一样要防覆盖：技能可以来自任何地方，
把用户自己写的那份悄悄换掉，比导入失败更糟。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from . import skills as skill_store
from .mcp_client import config_paths, hub

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_SKILL_BYTES = 1_000_000


class ImportError_(Exception):
    """导入失败，消息直接给用户看。"""


def _user_skills_dir() -> Path:
    path = Path.home() / ".workbuddy" / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path


def skill_exists(name: str) -> bool:
    return any((root / name / "SKILL.md").exists() for root in skill_store.skill_roots())


def validate_skill(text: str) -> tuple[str, dict, str]:
    """校验一段 SKILL.md，返回 (名字, 元数据, 正文)。

    要求 name/description 都有：没有名字的技能在列表里没法被点到，
    没有描述的技能模型不知道该不该用——两者缺一，这份技能就等于不存在。
    """
    if len(text.encode("utf-8")) > MAX_SKILL_BYTES:
        raise ImportError_("技能文件超过 1 MB，这不像是一份技能说明")
    meta, body = skill_store.parse_frontmatter(text)
    name = str(meta.get("name") or "").strip().lower()
    description = str(meta.get("description") or "").strip()
    if not name:
        raise ImportError_("缺少 name（frontmatter 里要有 name: xxx）")
    if not NAME_RE.match(name):
        raise ImportError_("name 只能用 a-z、0-9、- 和 _，且不超过 64 字符")
    if not description:
        raise ImportError_("缺少 description：模型靠它判断什么时候用这个技能")
    if len(description) > 1024:
        raise ImportError_("description 超过 1024 字符，请压到一行能说清")
    if not body.strip():
        raise ImportError_("正文是空的，技能没有任何步骤说明")
    return name, meta, body


def import_skill(text: str, overwrite: bool = False) -> dict:
    name, meta, _ = validate_skill(text)
    target_dir = _user_skills_dir() / name
    existed = (target_dir / "SKILL.md").exists()
    if existed and not overwrite:
        raise ImportError_(f"技能 {name} 已经存在；确定要覆盖就勾上「覆盖同名技能」")
    target_dir.mkdir(parents=True, exist_ok=True)
    # 原样落盘：frontmatter 的键顺序、注释都保留，用户回头还要手改
    (target_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return {
        "name": name,
        "description": str(meta.get("description") or ""),
        "path": str(target_dir / "SKILL.md"),
        "overwrote": existed,
    }


def import_skill_dir(source: str, overwrite: bool = False) -> dict:
    """从一个本地目录导入技能：目录里要有 SKILL.md。"""
    root = Path(source).expanduser()
    if not root.is_dir():
        raise ImportError_(f"目录不存在：{source}")
    manifest = root / "SKILL.md"
    if not manifest.exists():
        raise ImportError_("这个目录里没有 SKILL.md")
    result = import_skill(manifest.read_text(encoding="utf-8", errors="replace"), overwrite)

    # 技能常带脚本和模板，一起搬过去，否则 SKILL.md 里写的 references/x.md 全失效
    target_dir = _user_skills_dir() / result["name"]
    for child in root.iterdir():
        if child.name == "SKILL.md" or child.is_dir() and child.name.startswith("."):
            continue
        destination = target_dir / child.name
        if child.is_dir():
            shutil.copytree(child, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(child, destination)
    result["files"] = sum(1 for _ in target_dir.rglob("*") if _.is_file())
    return result


def delete_skill(name: str) -> None:
    if not NAME_RE.match(name):
        raise ImportError_("技能名不合法")
    removed = False
    for root in skill_store.skill_roots():
        target = root / name
        if target.is_dir():
            shutil.rmtree(target)
            removed = True
    if not removed:
        raise ImportError_(f"没有名为 {name} 的技能")


# ── MCP ───────────────────────────────────────────────────


def _writable_config() -> Path:
    """写用户级配置：项目级那份是仓库里的，改了会被 git 冲突缠住。"""
    for path in config_paths():
        if path.parent == Path.home() / ".workbuddy":
            return path
    return Path.home() / ".workbuddy" / "mcp.json"


def normalize_server(payload: dict) -> dict:
    """把界面表单或粘贴的 JSON 统一成 mcp.json 里的一段。

    支持两种形态：本地命令（command + args）和远程地址（url）。
    两者都有时优先 command —— 用户更容易填错 url，而 command 是明确的。
    """
    name = str(payload.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise ImportError_("服务器名只能用 a-z、0-9、- 和 _，且不超过 64 字符")

    command = str(payload.get("command") or "").strip()
    url = str(payload.get("url") or "").strip()
    if not command and not url:
        raise ImportError_("要么填 command（本地启动命令），要么填 url（远程地址）")

    args = payload.get("args") or []
    if isinstance(args, str):
        # 界面上一行一个参数，比让人手写 JSON 数组现实
        args = [part for part in args.splitlines() if part.strip()]
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ImportError_("args 必须是字符串数组（或每行一个）")

    env = payload.get("env") or {}
    if not isinstance(env, dict):
        raise ImportError_("env 必须是键值对象")

    server: dict = {}
    if command:
        server["command"] = command
        if args:
            server["args"] = args
        if env:
            server["env"] = {str(k): str(v) for k, v in env.items()}
    else:
        server["url"] = url
        if env:
            server["headers"] = {str(k): str(v) for k, v in env.items()}

    if payload.get("disabled"):
        server["disabled"] = True
    return {"name": name, "server": server}


def preview_command(server: dict) -> str:
    """将要执行的命令行原文——导入前必须让用户看见的就是这一行。"""
    if server.get("command"):
        parts = [server["command"], *[str(a) for a in server.get("args") or []]]
        # 参数里有空格就加引号，让用户看到的就是实际会被解析的样子
        return " ".join(f'"{p}"' if " " in p else p for p in parts)
    return f"（远程）{server.get('url', '')}"


def import_mcp_server(payload: dict, confirm: bool = False, overwrite: bool = False) -> dict:
    if not confirm:
        raise ImportError_("导入 MCP 服务器等于允许一条命令长期在这台机器上运行，请确认后再提交")

    normalized = normalize_server(payload)
    name, server = normalized["name"], normalized["server"]
    command_line = preview_command(server)

    path = _writable_config()
    data: dict = {"mcpServers": {}}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except ValueError as exc:
            # 配置坏了就别动它：合并进一份解析不了的 JSON 只会更坏
            raise ImportError_(f"现有 mcp.json 解析失败（{exc}），请先手工修好再导入") from exc

    servers = data.setdefault("mcpServers", {})
    existed = name in servers
    if existed and not overwrite:
        raise ImportError_(f"已经有一个叫 {name} 的服务器；要用新的就勾上「覆盖同名」")
    servers[name] = server

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 立刻让运行中的 hub 看到它，否则用户要重启应用才生效
    try:
        hub().reload()
    except Exception:  # noqa: BLE001 - 重载失败不该让导入本身失败
        logger.warning("导入后重载 MCP 配置失败", exc_info=True)

    return {
        "name": name,
        "server": server,
        "command": command_line,
        "config_path": str(path),
        "overwrote": existed,
    }


def delete_mcp_server(name: str) -> dict:
    path = _writable_config()
    if not path.exists():
        raise ImportError_("还没有 mcp.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers") or {}
    if name not in servers:
        raise ImportError_(f"没有名为 {name} 的服务器")
    # 用户级配置里没有、只有项目级的，删不掉——说清楚，别假装删了
    servers.pop(name)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        hub().reload()
    except Exception:  # noqa: BLE001
        pass
    return {"name": name, "config_path": str(path)}


def test_mcp_server(payload: dict, timeout: float = 12.0) -> dict:
    """试连一次：握手 + 拉工具列表。导入前点一下，比导入后发现连不上好。"""
    normalized = normalize_server(payload)
    name, server = normalized["name"], normalized["server"]
    from .mcp_client import McpServer

    instance = McpServer(name, server)
    try:
        tools = instance.tools(timeout=timeout)
        return {
            "ok": True,
            "command": preview_command(server),
            "tools": [{"name": t["name"], "description": t.get("description", "")} for t in tools],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "command": preview_command(server), "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            instance.close()
        except Exception:  # noqa: BLE001
            pass
