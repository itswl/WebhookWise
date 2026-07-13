"""Regression tests for the batch-1 code-review fixes.

Each test pins a specific finding from the review so the fix cannot silently
regress:
- #1  JSON unescape order (contracts.deep_analysis_report._decode_escaped_json_text)
- #10 normalize_level substring mis-classification (adapters.simple_adapters)
- #11 feishu_card adapter must populate AlertIdentity for dedup
- #12 decompress_payload must degrade gracefully on corrupt data
"""

from __future__ import annotations

import zstandard as zstd


def test_decode_escaped_json_is_order_independent() -> None:
    """#1: a literal "\\n" (backslash + n) must survive decoding, not become a
    real newline. The old chained .replace() collapsed escapes in the wrong
    order and corrupted such input."""
    from contracts.deep_analysis_report import _decode_escaped_json_text

    # Input is the 4 characters: backslash backslash n  -> should decode to a
    # single literal backslash followed by 'n', NOT a newline.
    assert _decode_escaped_json_text(r'{"k":"\\n"}') == '{"k":"\\n"}'
    # A real escaped newline (\n) still decodes to a newline.
    assert "\n" in _decode_escaped_json_text(r'{"k":"a\nb"}')
    # Escaped quote + escaped backslash combos do not cross-corrupt.
    assert _decode_escaped_json_text(r"a\\\"b") == r"a\"b"


def test_normalize_level_does_not_substring_match_ok() -> None:
    """#10: values that merely CONTAIN "ok" (token_expired, stockout) must not
    be classified as info."""
    from adapters.simple_adapters import normalize_level

    assert normalize_level("token_expired") == "warning"
    assert normalize_level("stockout") == "warning"
    # But genuine recovery wording and exact/compound forms still work.
    assert normalize_level("ok") == "info"
    assert normalize_level("all ok") == "info"
    assert normalize_level("service recovered normally") == "info"
    assert normalize_level("critical-alert") == "critical"


def test_feishu_card_adapter_sets_alert_identity() -> None:
    """#11: feishu_card must carry an AlertIdentity so dedup works by identity
    rather than degrading to a full-payload hash."""
    from adapters.normalized import extract_alert_identity
    from adapters.simple_adapters import _norm_feishu_card

    data = {
        "card": {
            "header": {"title": {"content": "磁盘告警"}},
            "elements": [
                {
                    "tag": "markdown",
                    "content": "告警策略：disk-full\n告警日志主题：prod-logs\n告警级别：critical\n触发条件：>90%",
                }
            ],
        }
    }
    result = _norm_feishu_card(data)
    # _norm_feishu_card returns a WebhookData (a TypedDict / mapping) directly.
    identity = extract_alert_identity(result)
    assert identity is not None
    assert identity.get("source") == "feishu_card"
    assert identity.get("name") == "disk-full"
    assert identity.get("resource") == "prod-logs"
    # Distinct strategy/condition -> distinct fingerprint, so different rules on
    # the same log topic do not collapse together.
    assert identity.get("fingerprint")


def test_decompress_payload_degrades_on_corrupt_data() -> None:
    """#12: corrupt zstd / invalid UTF-8 must not raise (which would 500 every
    API serializing that event) — they degrade to a placeholder / replacement."""
    from core.compression import _UNDECODABLE_PLACEHOLDER, decompress_payload

    # Bytes that start with the zstd magic but are not a valid frame.
    corrupt_zstd = b"\x28\xb5\x2f\xfd" + b"\x00\x01\x02broken"
    assert decompress_payload(corrupt_zstd) == _UNDECODABLE_PLACEHOLDER

    # Invalid UTF-8 (non-zstd) decodes with replacement instead of raising.
    out = decompress_payload(b"\xff\xfe\xfa")
    assert isinstance(out, str)

    # A valid round-trip still works.
    raw = b"hello world"
    framed = zstd.ZstdCompressor(level=3).compress(raw)
    assert decompress_payload(framed) == "hello world"

    # Malformed hex-encoded string degrades instead of raising ValueError.
    assert decompress_payload("\\xZZ") == _UNDECODABLE_PLACEHOLDER
