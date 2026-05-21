"""TaskIQ entrypoint wiring.

This module imports task definitions so TaskIQ CLI entrypoints register labels
without making ``core.taskiq_broker`` depend on ``services``.
"""

from __future__ import annotations

import services.operations.tasks as _tasks  # noqa: F401
from core.taskiq_broker import broker, dynamic_schedule_source, scheduler

__all__ = ("broker", "dynamic_schedule_source", "scheduler")
