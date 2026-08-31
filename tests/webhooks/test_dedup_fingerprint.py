"""Per-source dedup fingerprint fields, rolled out on the off/shadow/enforce ladder."""

import pytest

from services import dedup
from services.operations import runtime_settings as rt

_PAYLOAD = {
    "labels": {"alertname": "HighLatency", "instance": "web-1"},
    "annotations": {"summary": "p99 spiked"},
    "startsAt": "2026-08-31T01:02:03Z",
    "sequence": 4711,
}


def _configure(monkeypatch, temp_config, *, mode: str, fields: str) -> None:
    monkeypatch.setattr(temp_config.retry, "DEDUP_FINGERPRINT_MODE", mode, raising=False)
    monkeypatch.setattr(temp_config.retry, "DEDUP_FINGERPRINT_FIELDS", fields, raising=False)
    monkeypatch.setattr(rt, "_snapshot", {})


def _signals(monkeypatch) -> list[tuple[str, str]]:
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "core.observability.events.record_signal",
        lambda name, state, attrs=None: recorded.append((name, state)),
    )
    return recorded


# ── dot-path extraction ───────────────────────────────────────────────────────


def test_extracts_nested_paths_and_list_indexes() -> None:
    data = {"a": {"b": [{"c": "hit"}]}}
    assert dedup._extract_field_path(data, "a.b.0.c") == "hit"
    assert dedup._extract_field_path(data, "a.b.1.c") is None
    assert dedup._extract_field_path(data, "a.missing") is None
    assert dedup._extract_field_path(data, "a.b.x") is None


def test_malformed_fields_config_is_ignored() -> None:
    assert dedup._parse_fingerprint_fields("not json {") == {}
    assert dedup._parse_fingerprint_fields('["a"]') == {}
    assert dedup._parse_fingerprint_fields('{"grafana": "not-a-list"}') == {}
    assert dedup._parse_fingerprint_fields('{"Grafana": ["labels.alertname"]}') == {
        "grafana": ("labels.alertname",)
    }


# ── mode ladder behaviour ─────────────────────────────────────────────────────


def test_off_keeps_the_builtin_keys(monkeypatch, temp_config) -> None:
    _configure(monkeypatch, temp_config, mode="off", fields='{"grafana": ["labels.alertname"]}')
    assert dedup.generate_event_keys(_PAYLOAD, "grafana") == dedup._default_event_keys(_PAYLOAD, "grafana")


def test_shadow_counts_divergence_without_changing_behaviour(monkeypatch, temp_config) -> None:
    _configure(monkeypatch, temp_config, mode="shadow", fields='{"grafana": ["labels.alertname"]}')
    recorded = _signals(monkeypatch)

    keys = dedup.generate_event_keys(_PAYLOAD, "grafana")

    assert keys == dedup._default_event_keys(_PAYLOAD, "grafana")
    assert ("dedup.fingerprint", "diverged") in recorded


def test_enforce_threads_payloads_that_differ_outside_the_identity(monkeypatch, temp_config) -> None:
    """The point of the feature: timestamps/sequence noise stops fragmenting dedup."""
    _configure(
        monkeypatch,
        temp_config,
        mode="enforce",
        fields='{"grafana": ["labels.alertname", "labels.instance"]}',
    )
    noisy_restatement = {
        **_PAYLOAD,
        "startsAt": "2026-08-31T01:07:03Z",
        "sequence": 4712,
        "annotations": {"summary": "p99 spiked again"},
    }

    _, key_a = dedup.generate_event_keys(_PAYLOAD, "grafana")
    _, key_b = dedup.generate_event_keys(noisy_restatement, "grafana")
    default_a = dedup._default_event_keys(_PAYLOAD, "grafana")

    assert key_a == key_b  # identical identity fields -> one thread
    assert key_a != default_a[1]  # and it is the configured key, not the built-in
    assert dedup.generate_event_keys(_PAYLOAD, "grafana")[0] == default_a[0]  # alert_hash never moves


def test_enforce_falls_back_when_no_configured_path_matches(monkeypatch, temp_config) -> None:
    _configure(monkeypatch, temp_config, mode="enforce", fields='{"grafana": ["nothing.here"]}')
    recorded = _signals(monkeypatch)

    keys = dedup.generate_event_keys(_PAYLOAD, "grafana")

    assert keys == dedup._default_event_keys(_PAYLOAD, "grafana")
    assert ("dedup.fingerprint", "unextractable") in recorded


def test_sources_without_config_are_untouched_in_every_mode(monkeypatch, temp_config) -> None:
    for mode in ("shadow", "enforce"):
        _configure(monkeypatch, temp_config, mode=mode, fields='{"grafana": ["labels.alertname"]}')
        assert dedup.generate_event_keys(_PAYLOAD, "n9e") == dedup._default_event_keys(_PAYLOAD, "n9e")


def test_namespace_still_partitions_configured_keys(monkeypatch, temp_config) -> None:
    _configure(monkeypatch, temp_config, mode="enforce", fields='{"grafana": ["labels.alertname"]}')
    _, key_default = dedup.generate_event_keys(_PAYLOAD, "grafana")
    _, key_scoped = dedup.generate_event_keys(_PAYLOAD, "grafana", namespace="tenant-a")
    assert key_default != key_scoped


@pytest.mark.asyncio
async def test_generate_alert_hash_wrapper_keeps_working(monkeypatch, temp_config) -> None:
    _configure(monkeypatch, temp_config, mode="enforce", fields='{"grafana": ["labels.alertname"]}')
    assert dedup.generate_alert_hash(_PAYLOAD, "grafana") == dedup.generate_event_keys(_PAYLOAD, "grafana")[0]
