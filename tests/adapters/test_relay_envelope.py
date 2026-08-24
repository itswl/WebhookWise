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


def test_a_sender_truncated_envelope_still_identifies_its_alert() -> None:
    """A relay that cuts its own body at a fixed width leaves valid JSON and then
    stops mid-token, so `json.loads` rejects everything — including the fields
    that arrived intact. For an EventBridge envelope those are the identifying
    ones, because they come first.

    Production, four times since 2026-08-05: an AWS Health event listing many
    affected RDS entities arrived cut to 4000 characters by `aws-sns-bypass/1.0`,
    landed as source=unknown with an empty rule name, and was rated `low`. An
    account-level notice, silently demoted, with no rule able to route it.
    """
    import json

    from adapters.ecosystem_adapters import _unwrap_json_envelope

    event = {
        "version": "0",
        "id": "c71e3229-c505-5939-f447-b5555998e4cb",
        "detail-type": "AWS Health Event",
        "source": "aws.health",
        "account": "000000000000",
        "region": "ap-southeast-1",
        "detail": {"eventTypeCode": "AWS_RDS_MAINTENANCE_SCHEDULED", "affectedEntities": [{"entityValue": "arn:x"}]},
    }
    full = json.dumps(event)
    truncated = full[: full.index('"detail"') + 40]

    recovered = _unwrap_json_envelope({"text": truncated})

    assert recovered["detail-type"] == "AWS Health Event", "the identifying fields arrived and must survive"
    assert recovered["source"] == "aws.health"
    assert recovered["region"] == "ap-southeast-1"
    # The incomplete pair is dropped whole rather than half-parsed.
    assert "detail" not in recovered


def test_an_envelope_truncated_beyond_use_is_left_alone() -> None:
    """A salvage that recovers nothing must leave the wrapper untouched, or a
    caller cannot tell "truncated beyond use" from a real single-key document."""
    from adapters.ecosystem_adapters import _unwrap_json_envelope

    wrapper = {"text": '{"detail-type": "AWS Health Ev'}

    assert _unwrap_json_envelope(wrapper) == wrapper


def test_salvage_does_not_fire_on_a_document_that_merely_looks_wrapped() -> None:
    """The three conditions still hold: a real alert with a `message` field keeps
    its own shape, truncation handling or not."""
    from adapters.ecosystem_adapters import _unwrap_json_envelope

    alert = {"message": '{"a": 1, "b": 2', "severity": "high", "host": "db-1"}

    assert _unwrap_json_envelope(alert) == alert


def test_the_rule_identity_survives_a_cut_inside_the_detail_object() -> None:
    """Stopping at the top level is not enough, and this is why.

    `detect` on the aws_health adapter needs BOTH `source` and
    `detail.eventTypeCode`, and `detail` is exactly the object a truncation lands
    inside — the affected-entity list is what makes these documents long. Top
    level alone recovers enough to say "an AWS Health event" and not enough to
    say WHICH, so the adapter still would not match and the alert would still
    arrive nameless.

    Verified against the real event 1362 (cut to 4000 chars): source=aws_health,
    RuleName=AWS_RDS_ENGINE_UPGRADE, where production had recorded
    source=unknown, no rule name, importance low.
    """
    import json

    from adapters.ecosystem_adapters import _unwrap_json_envelope

    event = {
        "version": "0",
        "source": "aws.health",
        "region": "ap-southeast-1",
        "detail": {
            "eventArn": "arn:aws:health:::event/RDS/x",
            "service": "RDS",
            "eventTypeCode": "AWS_RDS_ENGINE_UPGRADE",
            "affectedEntities": [{"entityValue": "arn:aws:rds:ap-southeast-1:0:db:one"}],
        },
    }
    full = json.dumps(event)
    truncated = full[: full.index('"affectedEntities"') + 40]

    recovered = _unwrap_json_envelope({"text": truncated})

    assert recovered["source"] == "aws.health"
    assert recovered["detail"]["eventTypeCode"] == "AWS_RDS_ENGINE_UPGRADE", "the rule identity must survive"
    assert "affectedEntities" not in recovered["detail"], "the pair the cut fell inside is dropped whole"


def test_salvage_recursion_is_bounded() -> None:
    """A pathological document must not recurse without limit."""
    from adapters.ecosystem_adapters import _MAX_SALVAGE_DEPTH, _unwrap_json_envelope

    depth = _MAX_SALVAGE_DEPTH + 4
    nested = '{"a": 1, "b": ' + '{"a": 1, "b": ' * depth + '"cut'

    recovered = _unwrap_json_envelope({"text": nested, "other": "scalar"})

    # It returns *something* without blowing the stack; how deep is the bound's business.
    assert isinstance(recovered, dict)
