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
    assert dedup._parse_fingerprint_fields('{"Grafana": ["labels.alertname"]}') == {"grafana": ("labels.alertname",)}


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


# ── label exclusion: the labels that are NOT identity ─────────────────────────

# The production payload shape, 2026-09-08. `alertname` and `grafana_folder` are
# stable; `payload` carries the violation detail and changes whenever a new
# userid/amount/timestamp appears, so Grafana — which hashes its `fingerprint`
# over the LABELS — hands WebhookWise a new identity on every firing.
_VOLATILE = {
    "Type": "GrafanaAlert",
    "alerts": [
        {
            "fingerprint": "242adcc3aa1b0f10",
            "labels": {
                "type": "signal",
                "alertname": "示例提现超限告警",
                "grafana_folder": "demo-alarm",
                "payload": "【示例数据检测告警】2026-09-08 07:23:36 userid: 100001",
            },
            "annotations": {},
        }
    ],
}


def _restated(detail: str, *, fingerprint: str) -> dict:
    """The same alert firing again with a new violation in the payload label."""
    alert = {**_VOLATILE["alerts"][0]}
    alert["fingerprint"] = fingerprint
    alert["labels"] = {**alert["labels"], "payload": detail}
    return {**_VOLATILE, "alerts": [alert]}


def _configure_exclusion(monkeypatch, temp_config, *, mode: str, labels: str, fields: str = "") -> None:
    monkeypatch.setattr(temp_config.retry, "DEDUP_FINGERPRINT_MODE", mode, raising=False)
    monkeypatch.setattr(temp_config.retry, "DEDUP_FINGERPRINT_FIELDS", fields, raising=False)
    monkeypatch.setattr(temp_config.retry, "DEDUP_FINGERPRINT_EXCLUDE_LABELS", labels, raising=False)
    monkeypatch.setattr(rt, "_snapshot", {})


def test_malformed_exclusion_config_is_ignored() -> None:
    assert dedup._parse_excluded_labels("not json {") == {}
    assert dedup._parse_excluded_labels('["payload"]') == {}
    assert dedup._parse_excluded_labels('{"grafana": "not-a-list"}') == {}
    assert dedup._parse_excluded_labels('{"Grafana": ["payload", " "]}') == {"grafana": frozenset({"payload"})}


def test_excluding_the_volatile_label_threads_every_firing(monkeypatch, temp_config) -> None:
    """The bug, fixed: 222 firings of one rule arrived as 222 original alerts."""
    _configure_exclusion(monkeypatch, temp_config, mode="enforce", labels='{"grafana": ["payload"]}')

    _, first = dedup.generate_event_keys(_VOLATILE, "grafana")
    _, later = dedup.generate_event_keys(
        _restated("【示例数据检测告警】2026-09-08 09:31:16 userid: 100002", fingerprint="3405c7784f1e0a92"),
        "grafana",
    )

    assert first == later
    # Built-in behaviour, for contrast: a new fingerprint was a new thread.
    assert (
        dedup._default_event_keys(_VOLATILE, "grafana")[1]
        != dedup._default_event_keys(_restated("x", fingerprint="3405c7784f1e0a92"), "grafana")[1]
    )


def test_the_remaining_labels_still_separate_distinct_alerts(monkeypatch, temp_config) -> None:
    """Why exclusion and not an inclusion list of the stable fields.

    Measured on production 2026-09-08: keying grafana on alertname+folder would
    have collapsed CertificateExpiredAlertRule's two threads — two different
    domains — into one, and the second expiring certificate would have been
    swallowed as a duplicate of the first. Dropping one label leaves every other
    label doing identity work, so these stay two threads.
    """
    _configure_exclusion(monkeypatch, temp_config, mode="enforce", labels='{"grafana": ["payload"]}')

    def _cert(domain: str, arn: str) -> dict:
        return {
            "Type": "GrafanaAlert",
            "alerts": [
                {
                    "fingerprint": "876289a7",
                    "labels": {
                        "alertname": "CertificateExpiredAlertRule",
                        "grafana_folder": "sre",
                        "domain_name": domain,
                        "certificate_arn": arn,
                    },
                }
            ],
        }

    _, one = dedup.generate_event_keys(_cert("example-a.test", "arn:...:cert-a"), "grafana")
    _, two = dedup.generate_event_keys(_cert("example-b.test", "arn:...:cert-b"), "grafana")
    assert one != two


def test_alert_hash_never_moves_under_an_exclusion(monkeypatch, temp_config) -> None:
    _configure_exclusion(monkeypatch, temp_config, mode="enforce", labels='{"grafana": ["payload"]}')
    assert dedup.generate_event_keys(_VOLATILE, "grafana")[0] == dedup._default_event_keys(_VOLATILE, "grafana")[0]


def test_off_and_shadow_keep_the_builtin_key_under_an_exclusion(monkeypatch, temp_config) -> None:
    _configure_exclusion(monkeypatch, temp_config, mode="off", labels='{"grafana": ["payload"]}')
    assert dedup.generate_event_keys(_VOLATILE, "grafana") == dedup._default_event_keys(_VOLATILE, "grafana")

    _configure_exclusion(monkeypatch, temp_config, mode="shadow", labels='{"grafana": ["payload"]}')
    recorded = _signals(monkeypatch)
    assert dedup.generate_event_keys(_VOLATILE, "grafana") == dedup._default_event_keys(_VOLATILE, "grafana")
    assert ("dedup.fingerprint", "diverged") in recorded


def test_excluding_every_label_falls_back_rather_than_collapsing_the_source(monkeypatch, temp_config) -> None:
    """A config that empties the label map must fragment as before.

    Collapsing is the dangerous failure: one bucket for an entire source would
    silently swallow every alert after the first.
    """
    _configure_exclusion(
        monkeypatch,
        temp_config,
        mode="enforce",
        labels='{"grafana": ["type", "alertname", "grafana_folder", "payload"]}',
    )
    recorded = _signals(monkeypatch)

    assert dedup.generate_event_keys(_VOLATILE, "grafana") == dedup._default_event_keys(_VOLATILE, "grafana")
    assert ("dedup.fingerprint", "unextractable") in recorded


def test_a_payload_with_no_label_map_falls_back(monkeypatch, temp_config) -> None:
    _configure_exclusion(monkeypatch, temp_config, mode="enforce", labels='{"n9e": ["payload"]}')
    recorded = _signals(monkeypatch)

    unlabelled = {"Type": "Other", "event": "alert", "RuleName": "no labels here"}
    assert dedup.generate_event_keys(unlabelled, "n9e") == dedup._default_event_keys(unlabelled, "n9e")
    assert ("dedup.fingerprint", "unextractable") in recorded


def test_named_identity_fields_win_over_an_exclusion(monkeypatch, temp_config) -> None:
    """The inclusion list is the more specific statement about the same source."""
    _configure_exclusion(
        monkeypatch,
        temp_config,
        mode="enforce",
        labels='{"grafana": ["payload"]}',
        fields='{"grafana": ["alerts.0.labels.alertname"]}',
    )
    _, key = dedup.generate_event_keys(_VOLATILE, "grafana")
    expected = dedup._fingerprint_dedup_key(_VOLATILE, "grafana", ("alerts.0.labels.alertname",), None)
    assert key == expected


def test_commonlabels_serve_as_the_fallback_label_map(monkeypatch, temp_config) -> None:
    _configure_exclusion(monkeypatch, temp_config, mode="enforce", labels='{"grafana": ["payload"]}')
    grouped = {
        "Type": "GrafanaAlert",
        "commonLabels": {"alertname": "GroupedAlert", "payload": "volatile-a"},
    }
    _, one = dedup.generate_event_keys(grouped, "grafana")
    _, two = dedup.generate_event_keys(
        {**grouped, "commonLabels": {"alertname": "GroupedAlert", "payload": "volatile-b"}}, "grafana"
    )
    assert one == two


def test_namespace_still_partitions_an_excluded_key(monkeypatch, temp_config) -> None:
    _configure_exclusion(monkeypatch, temp_config, mode="enforce", labels='{"grafana": ["payload"]}')
    _, plain = dedup.generate_event_keys(_VOLATILE, "grafana")
    _, scoped = dedup.generate_event_keys(_VOLATILE, "grafana", namespace="tenant-a")
    assert plain != scoped


def test_sources_without_an_exclusion_are_untouched(monkeypatch, temp_config) -> None:
    for mode in ("shadow", "enforce"):
        _configure_exclusion(monkeypatch, temp_config, mode=mode, labels='{"grafana": ["payload"]}')
        assert dedup.generate_event_keys(_VOLATILE, "n9e") == dedup._default_event_keys(_VOLATILE, "n9e")
