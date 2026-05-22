"""API process runtime hook wiring."""

from __future__ import annotations

from core.service_lifecycle import configure_runtime_lifecycle_hooks
from services.analysis.ai_analyzer import initialize_openai_client, reset_openai_client


def install_runtime_lifecycle_hooks() -> None:
    configure_runtime_lifecycle_hooks(
        initialize_ai_client=initialize_openai_client,
        reset_ai_client=reset_openai_client,
    )
