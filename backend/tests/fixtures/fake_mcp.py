"""一个最小的 MCP 服务器，用来在不依赖真实服务器的情况下验证协议实现。"""
import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except ValueError:
        continue
    method = message.get("method")
    request_id = message.get("id")

    if method == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-mcp", "version": "9.9.9"},
        }}), flush=True)
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [
            {"name": "echo", "description": "把输入原样返回",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
            {"name": "boom", "description": "总是报错",
             "annotations": {"destructiveHint": True},
             "inputSchema": {"type": "object", "properties": {}}},
        ]}}), flush=True)
    elif method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "boom":
            print(json.dumps({"jsonrpc": "2.0", "id": request_id,
                              "error": {"code": -32603, "message": "故意失败"}}), flush=True)
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": "echo: " + str(arguments.get("text", ""))}],
                "isError": False,
            }}), flush=True)
