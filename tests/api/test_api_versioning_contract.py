from __future__ import annotations


def _route_paths() -> set[str]:
    from core.app import app

    return {str(getattr(route, "path", "")) for route in app.routes}


def test_business_api_routes_are_v1_only() -> None:
    paths = _route_paths()

    required_v1_paths = {
        "/v1/webhook",
        "/v1/webhook/{source}",
        "/v1/webhooks",
        "/v1/webhooks/by-request/{request_id}",
        "/v1/webhooks/{webhook_id}",
        "/v1/reanalyze/{webhook_id}",
        "/v1/forward/{webhook_id}",
        "/v1/deep-analyze/{webhook_id}",
        "/v1/deep-analyses",
        "/v1/deep-analyses/{webhook_id}",
        "/v1/deep-analyses/{analysis_id}/retry",
        "/v1/deep-analyses/{analysis_id}/forward",
        "/v1/forward-rules",
        "/v1/forward-rules/sensitive",
        "/v1/forward-rules/{rule_id}",
        "/v1/forward-rules/{rule_id}/test",
        "/v1/outbox",
        "/v1/admin/dead-letters",
        "/v1/admin/dead-letters/{event_id}/replay",
        "/v1/admin/dead-letters/replay-all",
        "/v1/admin/outbox/{outbox_id}/retry",
        "/v1/admin/suppressed",
        "/v1/ai-usage",
        "/v1/prompt",
        "/v1/prompt/reload",
        "/v1/health/deep",
    }

    assert required_v1_paths <= paths
    assert not {path for path in paths if path.startswith("/api/")}
    assert "/webhook" not in paths
    assert "/webhook/{source}" not in paths
