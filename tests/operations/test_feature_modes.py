"""The off/shadow/enforce ladder: parsing, legacy mapping, override plane."""

from services.operations import runtime_settings as rt
from services.operations.feature_modes import FeatureMode, parse_feature_mode, resolve_feature_mode


def test_valid_values_parse_case_and_space_insensitively() -> None:
    assert parse_feature_mode("off") is FeatureMode.OFF
    assert parse_feature_mode(" Shadow ") is FeatureMode.SHADOW
    assert parse_feature_mode("ENFORCE") is FeatureMode.ENFORCE


def test_unknown_value_degrades_to_default_not_enforce() -> None:
    # A typo must fail safe: the worst mistake this parser can make is to
    # enforce something nobody asked for.
    assert parse_feature_mode("enfroce") is FeatureMode.OFF
    assert parse_feature_mode("on", default=FeatureMode.SHADOW) is FeatureMode.SHADOW


def test_empty_value_means_default() -> None:
    assert parse_feature_mode("") is FeatureMode.OFF
    assert parse_feature_mode(None) is FeatureMode.OFF


def test_legacy_boolean_maps_only_when_mode_is_unset() -> None:
    assert resolve_feature_mode("X_MODE", "", legacy_enforce=True) is FeatureMode.ENFORCE
    assert resolve_feature_mode("X_MODE", "", legacy_enforce=False) is FeatureMode.OFF
    # A set mode beats the legacy boolean in both directions.
    assert resolve_feature_mode("X_MODE", "shadow", legacy_enforce=True) is FeatureMode.SHADOW
    assert resolve_feature_mode("X_MODE", "off", legacy_enforce=True) is FeatureMode.OFF


def test_runtime_override_beats_config_value(monkeypatch) -> None:
    # AI_COST_BUDGET_MODE is a registered spec, so the override plane casts it.
    monkeypatch.setattr(rt, "_snapshot", {"AI_COST_BUDGET_MODE": "shadow"})
    assert resolve_feature_mode("AI_COST_BUDGET_MODE", "off", legacy_enforce=False) is FeatureMode.SHADOW


def test_unparseable_override_falls_back_to_config(monkeypatch) -> None:
    monkeypatch.setattr(rt, "_snapshot", {"AI_COST_BUDGET_MODE": "banana"})
    assert resolve_feature_mode("AI_COST_BUDGET_MODE", "enforce", legacy_enforce=False) is FeatureMode.ENFORCE
