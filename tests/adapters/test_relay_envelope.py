"""Relay envelopes must not hide a payload from every adapter.

Production sent AWS Health events through an SNS relay as
`{"text": "<json>", "subject": ..., "source": "aws-sns"}`. Every detector saw a
three-key wrapper, matched nothing, and the alert landed as source=unknown with
no identity — invisible to dedup, routing, and per-rule quality aggregation.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from adapters.ecosystem_adapters import _unwrap_json_envelope

AWS_HEALTH = {
    "version": "0",
    "id": "8812fa8e-b0a2-0f0d-1ffb-3cf0f6be0c4e",
    "detail-type": "AWS Health Event",
    "source": "aws.health",
    "account": "159221664420",
    "time": "2026-08-02T10:04:47Z",
    "region": "ap-southeast-1",
    "resources": ["159221664420"],
    "detail": {
        "service": "SES",
        "eventTypeCode": "AWS_SES_ENFORCEMENT_PROBATION",
        "eventTypeCategory": "accountNotification",
        "eventArn": "arn:aws:health:ap-southeast-1::event/SES/AWS_SES_ENFORCEMENT_PROBATION/x",
        "eventDescription": [{"latestDescription": "Amazon SES has placed your account under review."}],
    },
}


def test_sns_relay_envelope_is_unwrapped() -> None:
    envelope = {"text": json.dumps(AWS_HEALTH), "subject": "AWS Notification", "source": "aws-sns"}
    assert _unwrap_json_envelope(envelope)["detail-type"] == "AWS Health Event"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"message": '{"a": 1}', "severity": "high", "host": "web-01"}, "outer document is the alert, not a wrapper"),
        ({"text": '{"a":1,"b":2,"c":3}', "meta": {"x": 1}}, "a structured sibling means the outer doc is real"),
        ({"text": "just a sentence"}, "not JSON"),
        ({"text": "[1, 2, 3]"}, "an array is not a document"),
        ({"text": "{}", "a": 1}, "empty inner object"),
        ({"text": '{"a":1}', "body": '{"b":2}'}, "ambiguous: two candidate envelopes"),
        ({"alertname": '{"a":1,"b":2,"c":3}'}, "key is not a known relay wrapper"),
    ],
)
def test_real_payloads_are_never_gutted(payload: dict[str, Any], reason: str) -> None:
    assert _unwrap_json_envelope(payload) == payload, reason


def test_unwrapped_aws_health_event_gets_an_identity() -> None:
    """End to end: the adapter now names the rule, so dedup and the per-rule
    quality view have something to key on."""
    from adapters.ecosystem_adapters import initialize_adapters, normalize_webhook_event

    initialize_adapters()
    result = normalize_webhook_event(
        {"text": json.dumps(AWS_HEALTH), "subject": "AWS Notification", "source": "aws-sns"}, None
    )

    assert result.source == "aws_health"
    identity = result.data.get("_alert_identity")
    assert isinstance(identity, dict)
    # eventTypeCode, not eventArn: the ARN is per-occurrence and would defeat dedup.
    assert identity["name"] == "aws_ses_enforcement_probation"
    assert identity["service"] == "ses"
