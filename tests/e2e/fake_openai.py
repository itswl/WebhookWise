from __future__ import annotations

import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Any
from urllib.parse import urlparse

from core import json

REQUESTS: list[dict[str, Any]] = []
LOCK = Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeOpenAI/1.0"

    def _send_json(self, status: int, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        body = json.dumps_bytes(payload)
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/ready":
            self._send_json(200, {"ok": True})
            return
        if path == "/requests":
            with LOCK:
                self._send_json(200, list(REQUESTS))
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("content-length", "0") or "0")
        raw = self.rfile.read(length)
        try:
            body: Any = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            body = raw.decode("utf-8", errors="replace")
        with LOCK:
            REQUESTS.append({"path": path, "headers": dict(self.headers), "json": body})

        if path != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": "not found"}})
            return

        analysis = {
            "source": "prometheus",
            "event_type": "checkout-critical-5xx",
            "importance": "high",
            "summary": "AI E2E 摘要：checkout-api 5xx 当前值 25 高于阈值 5",
            "impact_scope": "checkout-api 用户下单链路可能出现错误响应",
            "actions": ["检查 checkout-api 最近发布和上游依赖", "查看 5xx trace 与错误日志"],
            "risks": ["错误率持续升高会影响下单转化"],
            "monitoring_suggestions": ["保留 5xx rate 与延迟 P95 联动告警"],
        }
        payload = {
            "id": "chatcmpl-e2e",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model") or "e2e/fake-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(analysis)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
        }
        self._send_json(200, payload)

    def log_message(self, fmt: str, *args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9001), Handler).serve_forever()
