"""Single source of truth for a worker/process instance id.

``f"{hostname}-{pid}"`` was computed independently in three places
(core/config/defaults.py WORKER_ID, core/observability/resource.py's
service.instance.id fallback, and api/app.py) — same expression, drifting
homes. This centralizes the expression so those callers share one definition;
business code should prefer ``config.server.WORKER_ID`` over recomputing it.
"""

from __future__ import annotations

import os
import socket


def default_worker_id() -> str:
    """`<hostname>-<pid>` — the default per-process identity."""
    return f"{socket.gethostname()}-{os.getpid()}"
