"""MCP 客户端：连上本机的 MCP 服务器，把它们的工具变成课脉能用的工具。

为什么复用 `~/.workbuddy/mcp.json`：
用户已经在那里配好了 ltspice、quartus、blender 等服务器，格式就是 Claude Desktop
的标准格式。课脉读同一份文件，等于**零迁移**继承全部配置，而不是再养一份。

协议要点（stdio 传输，NDJSON）：
    → initialize          （带 protocolVersion，等响应）
    → notifications/initialized（通知，无 id，不等响应）
    → tools/list          （拿工具清单）
    → tools/call          （调用）

信任边界（重要）：
MCP 工具能执行任意本地命令——ltspice 那个能跑 SPICE 脚本，quartus 那个能编译烧板。
所以课脉**默认不把它交给模型自动调用**，必须由用户在界面上点确认。
这条限制写在 `allow_auto` 上，前端不给开关，模型也拿不到这个能力。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import queue
from subprocess import PIPE, Popen, TimeoutExpired
import threading
import time

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "kemai"
CLIENT_VERSION = "0.1.0"
DEFAULT_TIMEOUT = 45.0


class McpError(RuntimeError):
    pass


def config_paths() -> list[Path]:
    """按优先级返回配置文件位置：项目内覆盖全局。"""
    paths = [Path.home() / ".workbuddy" / "mcp.json"]
    project = Path(__file__).resolve().parents[2] / ".workbuddy" / "mcp.json"
    if project not in paths:
        paths.insert(0, project)
    return paths


def load_config() -> dict:
    for path in config_paths():
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("MCP 配置读不了 %s：%s", path, exc)
            continue
        servers = data.get("mcpServers") or data.get("servers") or {}
        return {"path": str(path), "servers": servers}
    return {"path": "", "servers": {}}


def tool_allowed(server_config: dict, tool: str) -> bool:
    """mcp.json 里可选的 allow_tools / deny_tools。

    UUMit 一个服务器 56 个工具，全塞进提示词会把它自己淹了。
    写白名单只暴露用得上的几个，比"整台服务器开或关"细一档。
    """
    from fnmatch import fnmatch

    deny = server_config.get("deny_tools") or []
    if any(fnmatch(tool, str(p)) for p in deny):
        return False
    allow = server_config.get("allow_tools") or []
    if allow:
        return any(fnmatch(tool, str(p)) for p in allow)
    return True


class McpServer:
    """一个 MCP 服务器 = 一个长驻子进程 + 一条请求/响应队列。"""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.process: Popen | None = None
        self.responses: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._next_id = 0
        self._tools: list[dict] | None = None
        self._error = ""
        # 失败冷却：一个连不上的服务器不该每次刷新都拖慢整页
        self._failed_at = 0.0
        self._session: dict = {}

    # ── 生命周期 ──────────────────────────────────────────
    def start(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        command = str(self.config.get("command") or "")
        if not command:
            raise McpError(f"{self.name}: 配置里没有 command")
        if not Path(command).exists() and not command.lower().endswith((".exe", ".cmd", ".bat")):
            # 允许 node/python 这类在 PATH 上的名字，但路径形式必须存在
            if os.sep in command or "/" in command:
                raise McpError(f"{self.name}: 找不到可执行文件 {command}")

        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (self.config.get("env") or {}).items()})
        working = self.config.get("cwd")

        creationflags = getattr(os, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.process = Popen(
            [command, *[str(a) for a in (self.config.get("args") or [])]],
            stdin=PIPE, stdout=PIPE, stderr=PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=env, cwd=working, creationflags=creationflags,
        )
        self.responses = queue.Queue()
        threading.Thread(target=self._pump, args=(self.process,), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(self.process,), daemon=True).start()

        result = self._call("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        }, timeout=timeout)
        self._session = result or {}
        self._notify("notifications/initialized")
        logger.info("MCP %s 已连接：%s", self.name, self._session.get("serverInfo"))

    def _pump(self, process: Popen) -> None:
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                logger.debug("%s 非协议输出：%s", self.name, line[:200])
                continue
            if "id" in message:
                self.responses.put(message)
            # 通知（没有 id）直接丢弃，课脉不订阅服务器推送

    def _drain_stderr(self, process: Popen) -> None:
        for line in process.stderr:
            logger.debug("%s stderr: %s", self.name, line.rstrip()[:300])

    def close(self) -> None:
        process, self.process = self.process, None
        self._tools = None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except TimeoutExpired:
                    process.kill()
        except OSError:
            pass

    # ── 传输 ──────────────────────────────────────────────
    def _write(self, payload: dict) -> None:
        if self.process is None or self.process.poll() is not None:
            raise McpError(f"{self.name}: 进程没在运行")
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def _call(self, method: str, params: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            message = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                message["params"] = params
            self._write(message)
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise McpError(f"{self.name}: {method} 超时（{timeout:.0f}s）")
                try:
                    response = self.responses.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    if self.process is not None and self.process.poll() is not None:
                        raise McpError(f"{self.name}: 进程已退出（代码 {self.process.returncode}）") from None
                    continue
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    error = response["error"] or {}
                    raise McpError(f"{self.name}: {error.get('message') or error}")
                return response.get("result") or {}

    # ── 能力 ──────────────────────────────────────────────
    def tools(self, refresh: bool = False, timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
        if self._tools is not None and not refresh:
            return self._tools
        self.start(timeout=timeout)
        result = self._call("tools/list", {}, timeout=timeout)
        raw = result.get("tools") or []
        self._tools = [
            {
                "name": str(tool.get("name") or ""),
                "description": str(tool.get("description") or ""),
                # MCP 的 annotations 用来判断这个工具会不会改东西
                "read_only": bool((tool.get("annotations") or {}).get("readOnlyHint")),
                "destructive": bool((tool.get("annotations") or {}).get("destructiveHint")),
                "schema": tool.get("inputSchema") or {"type": "object", "properties": {}},
            }
            for tool in raw if tool.get("name")
        ]
        return self._tools

    def call(self, tool: str, arguments: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
        self.start()
        result = self._call("tools/call", {"name": tool, "arguments": arguments or {}}, timeout=timeout)
        texts = []
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
        return {
            "is_error": bool(result.get("isError")),
            "text": "\n".join(texts),
            "raw": result,
        }

    @property
    def info(self) -> dict:
        server_info = (self._session.get("serverInfo") or {}) if self._session else {}
        return {
            "name": self.name,
            "title": server_info.get("name") or self.name,
            "version": server_info.get("version") or "",
            "error": self._error,
        }


class McpHub:
    """管理所有已配置的服务器，按需启动，空闲不动。"""

    def __init__(self):
        self._servers: dict[str, McpServer] = {}
        self._config: dict | None = None

    def reload(self) -> dict:
        self._config = load_config()
        for name, server in list(self._servers.items()):
            if name not in self._config["servers"]:
                server.close()
                self._servers.pop(name, None)
        return {"path": self._config["path"], "servers": self._config["servers"]}

    def config(self) -> dict:
        if self._config is None:
            self.reload()
        assert self._config is not None
        return self._config

    def server(self, name: str) -> McpServer:
        config = self.config()["servers"].get(name)
        if config is None:
            raise McpError(f"没有名为 {name} 的 MCP 服务器")
        if config.get("disabled"):
            raise McpError(f"{name} 已被禁用（mcp.json 里 disabled: true）")
        if name not in self._servers:
            self._servers[name] = McpServer(name, config)
        return self._servers[name]

    def overview(self, probe: bool = True) -> dict:
        """给界面看的清单。probe=False 时只读配置，不启动任何子进程。"""
        config = self.config()
        servers = []
        for name, item in config["servers"].items():
            entry = {
                "name": name,
                "disabled": bool(item.get("disabled")),
                "command": str(item.get("command") or ""),
                "tools": [],
                "error": "",
                "version": "",
            }
            # probe=False 时也别一律当成"没有工具"：已经握过手的服务器，
            # 工具列表就在内存里，直接拿来用是零成本的。
            # 否则 agent 的 system prompt 会因为"没人先打开过工作区"而变成空的。
            if not entry["disabled"] and not probe and name in self._servers:
                cached = self._servers[name]._tools
                if cached is not None:
                    entry["tools"] = cached
                    # 版本号是握手时记下的；没记到就不写，别为它再起一次进程
                    entry["version"] = getattr(self._servers[name], "_version", "") or ""
                servers.append(entry)
                continue

            if not entry["disabled"] and probe:
                cooled = time.time() - self._servers[name]._failed_at < 60 if name in self._servers else False
                if cooled:
                    entry["error"] = self._servers[name]._error or "刚刚连接失败，60 秒后再试"
                    servers.append(entry)
                    continue
                try:
                    server = self.server(name)
                    # 界面刷新时不能一个服务器卡住整页，探测超时压到 8 秒
                    entry["tools"] = server.tools(timeout=12.0)
                    server._failed_at, server._error = 0.0, ""
                    info = server.info
                    entry["version"] = info["version"]
                except McpError as exc:
                    entry["error"] = str(exc)
                    server._failed_at, server._error = time.time(), str(exc)
                except Exception as exc:  # noqa: BLE001 - 单个服务器失败不该拖垮整页
                    entry["error"] = f"{type(exc).__name__}: {exc}"
                    server._failed_at, server._error = time.time(), entry["error"]
            servers.append(entry)
        return {"config_path": config["path"], "servers": servers}

    def close_all(self) -> None:
        for server in self._servers.values():
            server.close()
        self._servers.clear()


_hub: McpHub | None = None


def hub() -> McpHub:
    global _hub
    if _hub is None:
        _hub = McpHub()
    return _hub
