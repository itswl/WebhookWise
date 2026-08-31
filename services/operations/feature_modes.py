"""The off / shadow / enforce ladder for risky automated decisions.

Absorbed from Versus Incident's training -> shadow -> detect rollout: an
automated decision that can change behaviour gets one three-position switch
instead of a per-feature boolean. ``off`` computes nothing, ``shadow`` computes
and records what WOULD have happened without doing it, ``enforce`` does it.
The shadow position is the point of the ladder: a feature earns ``enforce`` by
showing a clean shadow ledger, not by an argument. WebhookWise already ran
this play once by hand (the relay/judge shadow pair rode a forward rule); this
makes the pattern a named, reusable position instead of a wiring trick.

Modes live in ordinary str settings so the existing runtime-settings plane
(DB override + audit + dashboard) applies unchanged. An unknown value degrades
to ``off`` and says so: a typo must fail safe, never enforce.
"""

from __future__ import annotations

from enum import StrEnum

from core.logger import get_logger

logger = get_logger("operations.feature_modes")


class FeatureMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    ENFORCE = "enforce"


FEATURE_MODE_VALUES: tuple[str, ...] = tuple(mode.value for mode in FeatureMode)


def parse_feature_mode(raw: object, *, default: FeatureMode = FeatureMode.OFF, setting: str = "") -> FeatureMode:
    """Parse a mode string, degrading unknown values to ``default`` loudly."""
    text = str(raw or "").strip().lower()
    if not text:
        return default
    try:
        return FeatureMode(text)
    except ValueError:
        logger.warning(
            "[FeatureMode] Unknown mode %r for %s; degrading to %s",
            raw,
            setting or "setting",
            default.value,
        )
        return default


def resolve_feature_mode(
    setting_key: str,
    config_value: str,
    *,
    legacy_enforce: bool | None = None,
) -> FeatureMode:
    """Resolve a mode setting through the runtime-settings override plane.

    ``legacy_enforce`` maps a pre-ladder boolean onto the ladder when the mode
    setting itself is unset: True -> enforce, False -> off. That keeps an
    existing deployment's behaviour identical until someone touches the new
    setting, which is the same backward-compatibility contract the boolean
    made.
    """
    from services.operations import runtime_settings as rt

    raw = rt.override_or(setting_key, str(config_value or ""))
    text = str(raw or "").strip().lower()
    if not text and legacy_enforce is not None:
        return FeatureMode.ENFORCE if legacy_enforce else FeatureMode.OFF
    return parse_feature_mode(text, setting=setting_key)
