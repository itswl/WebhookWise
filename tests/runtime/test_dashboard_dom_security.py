"""Static contracts for dashboard DOM injection boundaries."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_toast_renders_dynamic_messages_as_text() -> None:
    source = (PROJECT_ROOT / "templates/static/js/utils.js").read_text(encoding="utf-8")

    assert source.count("function showToast(") == 1
    assert "messageElement.textContent = cleanMessage" in source
    assert "${cleanMessage}" not in source


def test_security_headers_include_csp_and_browser_capability_restrictions() -> None:
    source = (PROJECT_ROOT / "core/web/middleware.py").read_text(encoding="utf-8")

    assert 'b"content-security-policy"' in source
    assert "object-src 'none'" in source
    assert 'b"permissions-policy"' in source


def test_external_dashboard_script_is_version_pinned() -> None:
    # Chart.js is loaded lazily by overview.js (off the critical path) instead of
    # a render-blocking <script> in the dashboard <head>. Its version must stay
    # pinned (not a floating "latest") wherever it is loaded from.
    overview = (PROJECT_ROOT / "templates/static/js/overview.js").read_text(encoding="utf-8")
    html = (PROJECT_ROOT / "templates/dashboard.html").read_text(encoding="utf-8")

    assert "https://cdn.jsdelivr.net/npm/chart.js@4.4.9/dist/chart.umd.min.js" in overview
    assert "chart.js/dist" not in overview  # guard against an unpinned URL
    assert "cdn.jsdelivr.net/npm/chart.js" not in html  # removed from the render-blocking <head>
