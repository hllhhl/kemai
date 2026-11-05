"""本地假网关：前 N 次返回 500，之后返回正常的 OpenAI 兼容响应。

用来验证「Python 重试 + node bridge + pi-ai」整条链路在网关抖动时是否真能自愈。
不依赖任何线上服务，也不会真的调用模型。
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAIL_TIMES = int(sys.argv[1]) if len(sys.argv) > 1 else 2
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 18999
hits = {"chat": 0}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.endswith("/models"):
            body = json.dumps({"data": [{"id": "mock-flash"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        hits["chat"] += 1
        n = hits["chat"]
        with open("mock-gateway.log", "a", encoding="utf-8") as log:
            log.write(f"第 {n} 次请求 {self.path}\n")
        if n <= FAIL_TIMES:
            body = json.dumps({"error": {"message": "upstream gateway exploded"}}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        payload = json.loads(raw or b"{}")
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            for piece in ("仓库", "抖动", "已恢复"):
                chunk = {"id": "c1", "object": "chat.completion.chunk", "created": 1,
                         "model": "mock-flash",
                         "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            done = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "mock-flash",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}}
            self.wfile.write(f"data: {json.dumps(done)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        body = json.dumps({
            "id": "c1", "object": "chat.completion", "created": 1, "model": "mock-flash",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "网关抖动已恢复"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"mock gateway on {PORT}, 前 {FAIL_TIMES} 次返回 500", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
