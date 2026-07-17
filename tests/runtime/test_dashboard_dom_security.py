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


def test_dashboard_has_no_third_party_runtime_scripts() -> None:
    overview = (PROJECT_ROOT / "templates/static/js/overview.js").read_text(encoding="utf-8")
    html = (PROJECT_ROOT / "templates/dashboard.html").read_text(encoding="utf-8")
    middleware = (PROJECT_ROOT / "core/web/middleware.py").read_text(encoding="utf-8")

    assert "cdn.jsdelivr.net" not in overview
    assert "cdn.jsdelivr.net" not in html
    assert "cdn.jsdelivr.net" not in middleware
    assert "new Chart(" not in overview
