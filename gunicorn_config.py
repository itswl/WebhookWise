"""Gunicorn runtime configuration."""

from __future__ import annotations

import os

# Gunicorn evaluates this file before the ASGI app and runtime config manager
# exist, so process-shape settings are read directly from the environment.


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


bind = f"{os.getenv('HOST', '0.0.0.0')}:{os.getenv('PORT', '8000')}"
workers = _int_env("API_WORKERS", 2)
worker_class = "uvicorn.workers.UvicornWorker"
timeout = _int_env("GUNICORN_TIMEOUT", 120)
graceful_timeout = _int_env("GUNICORN_GRACEFUL_TIMEOUT", 30)

# Recycle each worker after a bounded number of requests, with jitter so the
# workers do not all recycle at once. This caps gradual RSS growth (allocator
# fragmentation / slow leaks) over a long-lived process; it complements jemalloc
# rather than replacing it. The ceiling is high because this is a low-volume
# webhook receiver, so recycling is a slow safety net, not a hot-path event.
max_requests = _int_env("GUNICORN_MAX_REQUESTS", 10000)
max_requests_jitter = _int_env("GUNICORN_MAX_REQUESTS_JITTER", 1000)

# preload_app is deliberately left off: UvicornWorker runs the app lifespan in
# each worker, where the per-process DB engine, Redis pool, and MCP session
# manager are created. Preloading would build those once in the pre-fork master
# and share their sockets across forked workers, so the app must be imported
# after the fork instead.

# keepalive is left at gunicorn's default (2s): no keep-alive-reusing reverse
# proxy fronts the app in the deployment topology, so raising it would only tie
# up workers holding idle client connections.
loglevel = os.getenv("THIRD_PARTY_LOG_LEVEL", "WARNING").lower()
raw_env = [
    f"UVICORN_LOOP={os.getenv('UVICORN_LOOP', 'uvloop')}",
    f"UVICORN_HTTP={os.getenv('UVICORN_HTTP', 'httptools')}",
]

# Observability is exported via OTLP from the application process, so Gunicorn no
# longer needs Prometheus multiprocess cleanup hooks.
