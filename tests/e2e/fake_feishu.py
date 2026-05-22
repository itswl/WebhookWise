from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Any
from urllib.parse import urlparse

REQUESTS: list[dict[str, Any]] = []
LOCK = Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeFeishu/1.0"

    def _send_json(self, status: int, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
        length = int(self.headers.get("content-length", "0") or "0")
        raw = self.rfile.read(length)
        try:
            body: Any = json.loads(raw.decode("utf-8")) if raw else None
        except json.JSONDecodeError:
            body = raw.decode("utf-8", errors="replace")
        with LOCK:
            REQUESTS.append({"path": self.path, "headers": dict(self.headers), "json": body})
        self._send_json(200, {"StatusCode": 0, "StatusMessage": "success"})

    def log_message(self, fmt: str, *args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
