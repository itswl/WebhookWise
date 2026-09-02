"""The digest verb reaches the operator: the form, the badge, both dictionaries."""

from __future__ import annotations

import re

from tests.helpers.paths import PROJECT_ROOT


def _static_js(name: str) -> str:
    return (PROJECT_ROOT / "templates" / "static" / "js" / name).read_text(encoding="utf-8")


def test_inbound_digest_action_is_wired() -> None:
    module = _static_js("inbound-rules.js")

    # The verb list and the valued-verb list come from the API; the value field
    # follows the verb, and the payload carries the value.
    assert "result.actions_with_value" in module
    assert "'inbound.field.digestWindow'" in module
    assert "'inbound.badge.digestWindow'" in module
    assert "action_value: value('action_value').trim()" in module
    assert "addEventListener('change', _syncInboundValueField)" in module

    # Action labels are looked up dynamically ('inbound.action.' + action), so
    # the prefix is skipped and the full verb set is asserted explicitly below.
    keys = {
        key for key in re.findall(r"\bt\(\s*'([^']+)'", module) if key.startswith("inbound.") and not key.endswith(".")
    }
    keys.update(
        {
            "inbound.action.skip_ai",
            "inbound.action.skip_deep_analysis",
            "inbound.action.cap_importance",
            "inbound.action.digest",
            "inbound.field.actionValue",
            "inbound.field.capCeiling",
            "inbound.field.digestWindow",
            "inbound.hint.digestWindow",
            "inbound.badge.digestWindow",
        }
    )
    assert keys
    for dict_name in ("i18n.en.js", "i18n.zh.js"):
        dictionary = _static_js(dict_name)
        missing = [key for key in sorted(keys) if f"'{key}':" not in dictionary]
        assert missing == [], f"{dict_name} lacks {missing}"

    zh = _static_js("i18n.zh.js")
    assert "'inbound.action.digest': '汇总投递'" in zh
    assert "'inbound.field.digestWindow': '窗口（分钟）'" in zh
    assert "'inbound.badge.digestWindow': '{n} 分钟'" in zh
    en = _static_js("i18n.en.js")
    assert "'inbound.action.digest': 'Digest delivery'" in en
    assert "'inbound.field.digestWindow': 'Window (minutes)'" in en
