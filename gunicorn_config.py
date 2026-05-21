"""Gunicorn runtime configuration."""

from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


bind = f"{os.getenv('HOST', '0.0.0.0')}:{os.getenv('PORT', '8000')}"
workers = _int_env("API_WORKERS", 4)
worker_class = "uvicorn.workers.UvicornWorker"
timeout = _int_env("GUNICORN_TIMEOUT", 120)
graceful_timeout = _int_env("GUNICORN_GRACEFUL_TIMEOUT", 30)
loglevel = os.getenv("THIRD_PARTY_LOG_LEVEL", "WARNING").lower()
raw_env = [
    f"UVICORN_LOOP={os.getenv('UVICORN_LOOP', 'uvloop')}",
    f"UVICORN_HTTP={os.getenv('UVICORN_HTTP', 'httptools')}",
]

# Observability is exported via OTLP from the application process, so Gunicorn no
# longer needs Prometheus multiprocess cleanup hooks.
