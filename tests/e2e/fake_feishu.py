from __future__ import annotations

import json
import os
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from typing import Any
from urllib.parse import urlparse

REQUESTS: list[dict[str, Any]] = []
LOCK = Lock()

# The custom-app transport talks to https://open.feishu.cn (fixed host, port
# 443). cert-init writes the TLS material for that listener into the shared
# e2e-certs volume; without it only the plain bot-webhook listener runs.
TLS_CERT_PATH = os.environ.get("FAKE_FEISHU_TLS_CERT", "/certs/server.pem")
TLS_KEY_PATH = os.environ.get("FAKE_FEISHU_TLS_KEY", "/certs/server.key")


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
        path = urlparse(self.path).path
        if path == "/open-apis/auth/v3/tenant_access_token/internal":
            self._send_json(200, {"code": 0, "tenant_access_token": "t-e2e", "expire": 7200})
            return
        if path == "/open-apis/im/v1/messages":
            self._send_json(200, {"code": 0, "data": {"message_id": "om_e2e"}})
            return
        self._send_json(200, {"StatusCode": 0, "StatusMessage": "success"})

    def log_message(self, fmt: str, *args: object) -> None:
        return


def serve_app_api_tls() -> None:
    """Serve the same handler over HTTPS on 443 for the app-API host."""
    if not (os.path.isfile(TLS_CERT_PATH) and os.path.isfile(TLS_KEY_PATH)):
        return
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(TLS_CERT_PATH, TLS_KEY_PATH)
    server = ThreadingHTTPServer(("0.0.0.0", 443), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    Thread(target=server.serve_forever, daemon=True).start()


if __name__ == "__main__":
    serve_app_api_tls()
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
