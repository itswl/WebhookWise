"""Tests for the configurable instructor structured-output mode."""

from __future__ import annotations

import instructor


def test_resolve_instructor_mode_known_names() -> None:
    from services.analysis.ai_llm_client import _resolve_instructor_mode

    assert _resolve_instructor_mode("json") is instructor.Mode.JSON
    assert _resolve_instructor_mode("JSON") is instructor.Mode.JSON
    assert _resolve_instructor_mode("openrouter_structured_outputs") is instructor.Mode.OPENROUTER_STRUCTURED_OUTPUTS
    assert _resolve_instructor_mode("tools_strict") is instructor.Mode.TOOLS_STRICT
    assert _resolve_instructor_mode("  json_schema  ") is instructor.Mode.JSON_SCHEMA


def test_resolve_instructor_mode_unknown_falls_back_to_json() -> None:
    from services.analysis.ai_llm_client import _resolve_instructor_mode

    assert _resolve_instructor_mode("nonsense_mode_xyz") is instructor.Mode.JSON
    assert _resolve_instructor_mode("") is instructor.Mode.JSON


def test_provider_policy_reads_instructor_mode(temp_config, monkeypatch) -> None:
    from services.analysis.analysis_policies import AIProviderPolicy

    monkeypatch.setattr(temp_config.ai, "AI_INSTRUCTOR_MODE", "tools_strict")
    policy = AIProviderPolicy.from_config()
    assert policy.instructor_mode == "tools_strict"
