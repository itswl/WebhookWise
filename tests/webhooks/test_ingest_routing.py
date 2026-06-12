"""Tests for severity-based ingest priority routing."""

from __future__ import annotations

import pytest


def _enable(temp_config, levels: str = "critical") -> None:
    temp_config.mq.WEBHOOK_PRIORITY_ROUTING_ENABLED = True
    temp_config.mq.WEBHOOK_PRIORITY_LEVELS = levels


def test_routing_off_by_default(temp_config) -> None:
    from services.webhooks.ingest_routing import is_priority_payload

    temp_config.mq.WEBHOOK_PRIORITY_ROUTING_ENABLED = False
    assert is_priority_payload('{"severity":"critical"}') is False


def test_critical_severity_is_priority(temp_config) -> None:
    from services.webhooks.ingest_routing import is_priority_payload

    _enable(temp_config)
    assert is_priority_payload('{"severity":"critical","alertname":"X"}') is True
    assert is_priority_payload('{"Level":"fatal"}') is True  # normalize_level -> critical
    assert is_priority_payload('{"labels":{"severity":"firing"}}') is True


def test_non_priority_severities(temp_config) -> None:
    from services.webhooks.ingest_routing import is_priority_payload

    _enable(temp_config)
    assert is_priority_payload('{"severity":"warning"}') is False
    assert is_priority_payload('{"severity":"info"}') is False
    assert is_priority_payload('{"foo":1}') is False  # no severity field


def test_best_effort_on_bad_input(temp_config) -> None:
    from services.webhooks.ingest_routing import is_priority_mapping, is_priority_payload

    _enable(temp_config)
    assert is_priority_payload("not-json") is False
    assert is_priority_payload("") is False
    assert is_priority_payload("[1,2,3]") is False  # not an object
    assert is_priority_mapping({}) is False


def test_configurable_levels(temp_config) -> None:
    from services.webhooks.ingest_routing import is_priority_payload

    _enable(temp_config, levels="critical,warning")
    assert is_priority_payload('{"severity":"warning"}') is True  # now warning counts
    assert is_priority_payload('{"severity":"info"}') is False


@pytest.mark.parametrize("raw", ['{"Severity":"P0"}', '{"priority":"urgent"}', '{"severity":"sev1"}'])
def test_various_high_aliases(temp_config, raw: str) -> None:
    from services.webhooks.ingest_routing import is_priority_payload

    _enable(temp_config)
    assert is_priority_payload(raw) is True
