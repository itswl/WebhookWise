"""Tests for declarative YAML webhook adapters (adapters/declarative.py)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from adapters.declarative import (
    DeclarativeSpecError,
    compile_spec,
    load_specs,
    register_declarative_adapters,
)
from adapters.registry import AdapterRegistry
from adapters.simple_adapters import normalize_level

_VALID = {
    "name": "acme",
    "detect": {"all": [{"key_exists": "alert_name"}, {"key_equals": {"path": "kind", "value": "alert"}}]},
    "identity": {
        "name": "alert_name",
        "resource": ["host", "instance"],
        "service": "service",
        "fingerprint": ["id", "event_id"],
        "severity": "level",
    },
    "output": {"Type": "AcmeAlert", "RuleName": "alert_name", "Level": "level", "summary": ["message", "description"]},
}


def _payload(**over: object) -> dict[str, object]:
    base = {"alert_name": "High CPU", "kind": "alert", "host": "web-01", "service": "web", "id": "e1", "level": "crit"}
    base.update(over)
    return base


# ── Happy path ───────────────────────────────────────────────────────────────


def test_compile_detects_and_normalizes() -> None:
    spec = compile_spec(_VALID, spec="acme.yaml")
    assert spec.name == "acme" and spec.priority == 0
    assert spec.detector(_payload()) is True
    assert spec.detector({"alert_name": "x"}) is False  # kind!=alert fails the `all`

    data = spec.normalizer(_payload())
    identity = data["_alert_identity"]
    assert identity["source"] == "acme"
    assert identity["name"] == "high cpu"
    assert identity["resource"] == "web-01"
    assert identity["severity"] == normalize_level("crit")  # severity runs through the shared normalizer
    assert data["Type"] == "AcmeAlert"
    assert data["RuleName"] == "High CPU"
    assert data["Resources"] == [{"InstanceId": "web-01"}]


def test_first_non_empty_falls_through_candidates() -> None:
    spec = compile_spec(_VALID, spec="acme.yaml")
    # host blank/whitespace → resource falls through to instance.
    data = spec.normalizer(_payload(host="   ", instance="node-9"))
    assert data["_alert_identity"]["resource"] == "node-9"


def test_list_index_and_dotted_paths() -> None:
    spec = compile_spec(
        {
            "name": "prom_like",
            "detect": {"key_exists": "alerts.0.labels.alertname"},
            "identity": {"name": "alerts.0.labels.alertname"},
        },
        spec="p.yaml",
    )
    payload = {"alerts": [{"labels": {"alertname": "OOMKilled"}}]}
    assert spec.detector(payload) is True
    assert spec.normalizer(payload)["_alert_identity"]["name"] == "oomkilled"
    # Out-of-range index and non-digit segment resolve to missing (no match).
    assert spec.detector({"alerts": []}) is False
    assert spec.detector({"alerts": {"0": "x"}}) is False


def test_detect_any_and_key_prefix() -> None:
    spec = compile_spec(
        {
            "name": "pfx",
            "detect": {"any": [{"key_prefix": {"path": "source", "prefix": "acme-"}}, {"key_exists": "acme_marker"}]},
            "identity": {"name": "source"},
        },
        spec="pfx.yaml",
    )
    assert spec.detector({"source": "acme-prod"}) is True
    assert spec.detector({"acme_marker": 1, "source": "other"}) is True
    assert spec.detector({"source": "other"}) is False


def test_key_equals_matches_permissively() -> None:
    spec = compile_spec(
        {"name": "eq", "detect": {"key_equals": {"path": "code", "value": 200}}, "identity": {"name": "code"}},
        spec="eq.yaml",
    )
    assert spec.detector({"code": 200}) is True
    assert spec.detector({"code": "200"}) is True  # string/number mismatch still matches
    assert spec.detector({"code": 500}) is False
    assert spec.detector({}) is False


def test_output_defaults_type_to_name_and_level_from_severity() -> None:
    spec = compile_spec(
        {"name": "minimal", "detect": {"key_exists": "a"}, "identity": {"name": "a", "severity": "sev"}},
        spec="m.yaml",
    )
    data = spec.normalizer({"a": "x", "sev": "warning"})
    assert data["Type"] == "minimal"  # no output.Type → defaults to adapter name
    assert data["Level"] == "warning"  # derived from identity severity when output.Level absent


# ── Malformed specs raise DeclarativeSpecError naming the file ─────────────────


@pytest.mark.parametrize(
    "bad",
    [
        [],  # not a mapping
        {"name": "x", "detect": {"key_exists": "a"}, "identity": {"name": "a"}, "wat": 1},  # unknown top key
        {"detect": {"key_exists": "a"}, "identity": {"name": "a"}},  # missing name
        {"name": "  ", "detect": {"key_exists": "a"}, "identity": {"name": "a"}},  # blank name
        {"name": "x", "priority": "hi", "detect": {"key_exists": "a"}, "identity": {"name": "a"}},  # bad priority
        {"name": "x", "priority": True, "detect": {"key_exists": "a"}, "identity": {"name": "a"}},  # bool priority
        {"name": "x", "aliases": ["ok", 3], "detect": {"key_exists": "a"}, "identity": {"name": "a"}},  # bad alias
        {"name": "x", "identity": {"name": "a"}},  # missing detect
        {"name": "x", "detect": {"nope": "a"}, "identity": {"name": "a"}},  # unknown condition
        {"name": "x", "detect": {"all": []}, "identity": {"name": "a"}},  # empty combinator
        {"name": "x", "detect": {"all": "x"}, "identity": {"name": "a"}},  # combinator not a list
        {"name": "x", "detect": {"key_exists": ""}, "identity": {"name": "a"}},  # blank path
        {"name": "x", "detect": {"key_exists": "a", "extra": 1}, "identity": {"name": "a"}},  # two condition keys
        {"name": "x", "detect": {"key_equals": {"path": "a"}}, "identity": {"name": "a"}},  # key_equals missing value
        {"name": "x", "detect": {"key_equals": {"path": "a", "value": {}}}, "identity": {"name": "a"}},  # non-scalar
        {"name": "x", "detect": {"key_prefix": {"path": "a"}}, "identity": {"name": "a"}},  # prefix missing
        {
            "name": "x",
            "detect": {"key_prefix": {"path": "a", "prefix": 5}},
            "identity": {"name": "a"},
        },  # prefix not str
        {"name": "x", "detect": {"key_exists": "a"}},  # missing identity
        {"name": "x", "detect": {"key_exists": "a"}, "identity": {"resource": "r"}},  # identity missing name
        {"name": "x", "detect": {"key_exists": "a"}, "identity": {"name": "a", "bogus": "b"}},  # unknown identity field
        {"name": "x", "detect": {"key_exists": "a"}, "identity": {"name": "a", "source": ""}},  # blank source literal
        {"name": "x", "detect": {"key_exists": "a"}, "identity": {"name": []}},  # empty path list
        {"name": "x", "detect": {"key_exists": "a"}, "identity": {"name": [1]}},  # non-string path
        {"name": "x", "detect": {"key_exists": "a"}, "identity": {"name": "a"}, "output": []},  # output not mapping
        {"name": "x", "detect": {"key_exists": "a"}, "identity": {"name": "a"}, "output": {"Nope": "a"}},  # unknown out
        {"name": "x", "detect": {"key_exists": "a"}, "identity": {"name": "a"}, "output": {"Type": ""}},  # blank Type
    ],
)
def test_malformed_spec_raises(bad: object) -> None:
    with pytest.raises(DeclarativeSpecError) as excinfo:
        compile_spec(bad, spec="bad.yaml")
    assert "bad.yaml" in str(excinfo.value)


# ── Loading & registration ────────────────────────────────────────────────────


def test_load_specs_missing_dir_is_noop(tmp_path: Path) -> None:
    assert load_specs(tmp_path / "does-not-exist") == []


def test_load_specs_empty_file_raises(tmp_path: Path) -> None:
    (tmp_path / "empty.yaml").write_text("")
    with pytest.raises(DeclarativeSpecError, match="empty"):
        load_specs(tmp_path)


def test_register_from_dir_and_name_clash_skips(tmp_path: Path) -> None:
    (tmp_path / "acme.yaml").write_text(
        textwrap.dedent(
            """
            name: acme_decl
            detect:
              key_exists: alert_name
            identity:
              name: alert_name
              severity: level
            """
        )
    )
    registry = AdapterRegistry()
    registered = register_declarative_adapters(registry, specs_dir=tmp_path)
    assert registered == ["acme_decl"]
    assert registry.find_adapter_by_source("acme_decl") is not None

    # Second pass is idempotent: the name is already registered → skipped.
    assert register_declarative_adapters(registry, specs_dir=tmp_path) == []
