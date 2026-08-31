"""The rule engine must score 100% on the synthetic severity suite.

Ground truth here is by construction (see tests/synthetic/README.md): every
scenario was written from the documented rule semantics. The keyword rules are
the severity floor under the AI path — measured on live traffic they missed
none of the high alerts the model missed two of — so a regression in them is a
regression in the floor, and this suite is where it surfaces.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

SCENARIO_DIR = Path(__file__).parent / "scenarios"


def _load_scenarios() -> list[dict[str, Any]]:
    scenarios = []
    for path in sorted(SCENARIO_DIR.glob("*.json")):
        loaded = json.loads(path.read_text(encoding="utf-8"))
        loaded["_file"] = path.name
        scenarios.append(loaded)
    return scenarios


_SCENARIOS = _load_scenarios()


def test_the_suite_is_not_empty() -> None:
    assert len(_SCENARIOS) >= 18


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=[s["id"] for s in _SCENARIOS])
def test_rule_verdict_matches_construction(scenario: dict[str, Any]) -> None:
    from services.analysis.ai_analyzer import analyze_with_rules

    result = analyze_with_rules(dict(scenario["payload"]), scenario["source"])

    expected = scenario["expected"]
    assert result["importance"] == expected["importance"], scenario["note"]
    if "triage_verdict" in expected:
        assert result.get("triage_verdict") == expected["triage_verdict"], scenario["note"]


def test_scenario_files_keep_their_shape() -> None:
    """A scenario a scorer cannot read is a scenario that silently stops guarding."""
    for scenario in _SCENARIOS:
        assert scenario["id"] == scenario["_file"].removesuffix(".json")
        assert scenario["note"].strip()
        assert isinstance(scenario["payload"], dict) and scenario["payload"]
        assert scenario["expected"]["importance"] in {"low", "medium", "high"}
        assert scenario["source"].strip()
