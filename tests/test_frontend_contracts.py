from pathlib import Path


def test_alerts_js_cursor_meta_parser_exists():
    p = Path(__file__).resolve().parents[1] / "templates" / "static" / "js" / "alerts.js"
    s = p.read_text(encoding="utf-8")
    assert "_extractCursorMeta" in s
    assert "result.cursor || result.pagination" in s


def test_dashboard_has_load_more_button():
    p = Path(__file__).resolve().parents[1] / "templates" / "dashboard.html"
    s = p.read_text(encoding="utf-8")
    assert 'id="loadMoreBtn"' in s
