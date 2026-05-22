"""TaskIQ entrypoint wiring.

This module imports task definitions so TaskIQ CLI entrypoints register labels
without making ``core.taskiq_broker`` depend on ``services``.
"""

from __future__ import annotations

import services.operations.tasks as _tasks  # noqa: F401
from core.service_lifecycle import configure_runtime_lifecycle_hooks
from core.taskiq_broker import broker, dynamic_schedule_source, scheduler
from services.analysis.ai_analyzer import initialize_openai_client, reset_openai_client

configure_runtime_lifecycle_hooks(
    initialize_ai_client=initialize_openai_client,
    reset_ai_client=reset_openai_client,
)

__all__ = ("broker", "dynamic_schedule_source", "scheduler")
