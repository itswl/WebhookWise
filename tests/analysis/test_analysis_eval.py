"""The offline importance eval, and the properties that make its number mean something.

Three things have to hold or the gate is decoration: the committed corpus still
meets the committed thresholds, the score does not move with the developer's
environment, and a malformed corpus or a mistyped threshold fails loudly instead
of quietly scoring nothing.
"""

from __future__ import annotations

import json as stdlib_json
from pathlib import Path

import pytest

from scripts.eval_analysis import (
    DEFAULT_BASELINE,
    DEFAULT_CORPUS,
    CorpusError,
    build_rule_policy,
    check_thresholds,
    classify,
    derive_thresholds,
    load_baseline,
    load_corpus,
    main,
    run_rules,
)


def _write(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _case(**overrides: object) -> str:
    case: dict[str, object] = {
        "id": "c1",
        "source": "prometheus",
        "parsed_data": {"RuleName": "ServiceDownCritical", "Level": "critical"},
        "expected": {"importance": "high"},
    }
    case.update(overrides)
    return stdlib_json.dumps(case)


# ── The committed contract ────────────────────────────────────────────────────


class TestCommittedCorpus:
    def test_the_corpus_still_meets_the_recorded_thresholds(self) -> None:
        """The gate's own assertion, so a contributor who only runs pytest sees it."""
        cases = load_corpus(DEFAULT_CORPUS)
        metrics = run_rules(cases, "default").metrics()
        thresholds = load_baseline(DEFAULT_BASELINE)["engines"]["rules"]["thresholds"]
        assert check_thresholds(metrics, thresholds) == []

    def test_the_corpus_carries_labelled_high_cases(self) -> None:
        """high_recall is the headline metric; it is meaningless with nothing to recall."""
        cases = load_corpus(DEFAULT_CORPUS)
        assert sum(1 for case in cases if case.expected_importance == "high") >= 5

    def test_every_case_records_where_it_came_from(self) -> None:
        """A case nobody can trace is a case nobody dares to relabel."""
        assert all(case.origin for case in load_corpus(DEFAULT_CORPUS))


# ── Reproducibility ───────────────────────────────────────────────────────────


class TestDeclaredPolicyIgnoresTheEnvironment:
    def test_exported_keywords_do_not_move_the_default_policy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CI and a laptop have to agree, whatever either has in its environment."""
        before = build_rule_policy("default")
        monkeypatch.setenv("RULE_HIGH_KEYWORDS", "banana")
        monkeypatch.setenv("RULE_THRESHOLD_MULTIPLIER", "1.5")
        assert build_rule_policy("default") == before

    def test_the_declared_policy_is_the_committed_default(self) -> None:
        from core.config.defaults import AIConfig
        from core.text import split_csv_lower

        policy = build_rule_policy("default")
        assert list(policy.high_keywords) == split_csv_lower(AIConfig.model_fields["RULE_HIGH_KEYWORDS"].default)

    def test_scoring_the_same_corpus_twice_gives_the_same_numbers(self) -> None:
        cases = load_corpus(DEFAULT_CORPUS)
        assert run_rules(cases, "default").metrics() == run_rules(cases, "default").metrics()


# ── Scoring ───────────────────────────────────────────────────────────────────


class TestClassify:
    @pytest.mark.parametrize(
        ("expected", "predicted", "verdict"),
        [
            ("high", "high", "match"),
            ("high", "medium", "miss"),
            ("high", "low", "miss"),
            ("medium", "low", "miss"),
            ("low", "high", "overcall"),
            ("medium", "high", "overcall"),
            (None, "high", "unlabeled"),
        ],
    )
    def test_direction_is_kept(self, expected: str | None, predicted: str, verdict: str) -> None:
        assert classify(expected, predicted) == verdict


class TestMetrics:
    def test_unlabelled_cases_are_replayed_but_not_scored(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            _case(id="labelled"),
            _case(id="unlabelled", expected=None),
        )
        metrics = run_rules(load_corpus(path), "default").metrics()
        assert (metrics["total"], metrics["labeled"], metrics["unlabeled"]) == (2, 1, 1)
        assert metrics["exact_rate"] == 1.0

    def test_an_undercall_and_an_overcall_land_in_different_buckets(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            # Critical level, labelled low by an operator -> the engine over-calls.
            _case(id="over", expected={"importance": "low"}),
            # An info-level notice labelled high -> the engine under-calls.
            _case(
                id="under",
                parsed_data={"RuleName": "QueueDepth", "Level": "info"},
                expected={"importance": "high"},
            ),
        )
        metrics = run_rules(load_corpus(path), "default").metrics()
        assert (metrics["misses"], metrics["overcalls"]) == (1, 1)
        assert metrics["high_recall"] == 0.0
        assert metrics["exact_rate"] == 0.0

    def test_rates_are_zero_rather_than_undefined_with_nothing_labelled(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _case(expected=None))
        metrics = run_rules(load_corpus(path), "default").metrics()
        assert metrics["exact_rate"] == 0.0
        assert metrics["high_recall"] == 0.0

    def test_triage_is_scored_only_where_it_is_labelled(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            _case(id="with-triage", expected={"importance": "high", "triage_verdict": "act_now"}),
            _case(id="without-triage"),
        )
        metrics = run_rules(load_corpus(path), "default").metrics()
        assert metrics["triage_labeled"] == 1
        assert metrics["triage_exact_rate"] == 1.0

    def test_disagreements_name_the_case_and_its_origin(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _case(id="over", expected={"importance": "low"}, origin="event 41221"))
        report = run_rules(load_corpus(path), "default")
        rows = report.disagreements()
        assert [(row.case_id, row.verdict, row.origin) for row in rows] == [("over", "overcall", "event 41221")]


# ── Failing loudly ────────────────────────────────────────────────────────────


class TestCorpusValidation:
    @pytest.mark.parametrize(
        ("line", "expected_message"),
        [
            ("{not json", "invalid JSON"),
            ("[]", "must be a JSON object"),
            ('{"source": "p", "parsed_data": {}}', "non-empty string id"),
            ('{"id": "a", "parsed_data": {}}', "non-empty string source"),
            ('{"id": "a", "source": "p"}', "parsed_data must be an object"),
            ('{"id": "a", "source": "p", "parsed_data": {}, "expected": {"importance": "urgent"}}', "not one of"),
            ('{"id": "a", "source": "p", "parsed_data": {}, "expected": {"triage_verdict": "panic"}}', "not one of"),
            ('{"id": "a", "source": "p", "parsed_data": {}, "expected": 3}', "must be an object"),
        ],
    )
    def test_a_malformed_case_is_reported_not_skipped(self, tmp_path: Path, line: str, expected_message: str) -> None:
        with pytest.raises(CorpusError, match=expected_message):
            load_corpus(_write(tmp_path, line))

    def test_duplicate_ids_are_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(CorpusError, match="duplicate id"):
            load_corpus(_write(tmp_path, _case(), _case()))

    def test_an_empty_corpus_is_an_error_not_a_perfect_score(self, tmp_path: Path) -> None:
        with pytest.raises(CorpusError, match="corpus is empty"):
            load_corpus(_write(tmp_path, "# only a comment"))

    def test_comment_and_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        cases = load_corpus(_write(tmp_path, "# header", "", _case()))
        assert [case.id for case in cases] == ["c1"]

    def test_a_missing_corpus_names_the_path(self, tmp_path: Path) -> None:
        with pytest.raises(CorpusError, match="corpus not found"):
            load_corpus(tmp_path / "nope.jsonl")


class TestBaselineValidation:
    def _baseline(self, tmp_path: Path, payload: object) -> Path:
        path = tmp_path / "baseline.json"
        path.write_text(stdlib_json.dumps(payload), encoding="utf-8")
        return path

    def test_a_mistyped_threshold_key_fails_instead_of_disabling_the_check(self, tmp_path: Path) -> None:
        path = self._baseline(tmp_path, {"engines": {"rules": {"thresholds": {"min_exact_rat": 0.9}}}})
        with pytest.raises(CorpusError, match="unknown key"):
            load_baseline(path)

    def test_an_engine_without_thresholds_is_rejected(self, tmp_path: Path) -> None:
        path = self._baseline(tmp_path, {"engines": {"rules": {"recorded": {}}}})
        with pytest.raises(CorpusError, match="needs a 'thresholds' object"):
            load_baseline(path)

    def test_a_baseline_without_engines_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(CorpusError, match="'engines' object"):
            load_baseline(self._baseline(tmp_path, {"rules": {}}))


class TestThresholds:
    def _metrics(self, **overrides: object) -> dict[str, object]:
        metrics: dict[str, object] = {
            "labeled": 20,
            "exact_rate": 0.9,
            "high_recall": 1.0,
            "over_rate": 0.05,
            "miss_rate": 0.0,
            "errors": 0,
        }
        metrics.update(overrides)
        return metrics

    def test_a_met_contract_produces_no_failures(self) -> None:
        thresholds = {"min_labeled": 20, "min_exact_rate": 0.85, "min_high_recall": 1.0, "max_over_rate": 0.1}
        assert check_thresholds(self._metrics(), thresholds) == []

    def test_a_new_miss_fails_on_its_own_axis(self) -> None:
        failures = check_thresholds(self._metrics(high_recall=0.9, miss_rate=0.05), {"min_high_recall": 1.0})
        assert failures == ["high_recall 0.9 is below the required 1"]

    def test_a_shrinking_corpus_fails(self) -> None:
        """Deleting labels must not be a way to make the gate green."""
        assert check_thresholds(self._metrics(labeled=4), {"min_labeled": 20}) == ["labeled 4 is below the required 20"]

    def test_an_unscoreable_case_is_a_failure_on_its_own(self) -> None:
        assert check_thresholds(self._metrics(errors=2), {}) == ["2 case(s) failed to score"]

    def test_thresholds_absent_from_the_baseline_are_not_invented(self) -> None:
        assert check_thresholds(self._metrics(exact_rate=0.0), {"min_labeled": 1}) == []

    def test_derived_thresholds_are_exact_on_misses_and_loose_on_the_average(self) -> None:
        derived = derive_thresholds(self._metrics(exact_rate=0.9, high_recall=1.0, over_rate=0.05))
        assert derived["min_high_recall"] == 1.0
        assert derived["min_exact_rate"] == 0.85
        assert derived["max_over_rate"] == 0.1


# ── The command line ──────────────────────────────────────────────────────────


class TestCommandLine:
    def test_run_returns_zero_on_the_committed_corpus(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["run"]) == 0
        assert "EVAL GREEN" in capsys.readouterr().out

    def test_a_regressed_score_returns_one_and_lists_the_disagreement(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        corpus = _write(tmp_path, _case(id="under", parsed_data={"RuleName": "Quiet", "Level": "info"}))
        baseline = tmp_path / "baseline.json"
        baseline.write_text(
            stdlib_json.dumps({"engines": {"rules": {"thresholds": {"min_high_recall": 1.0}}}}), encoding="utf-8"
        )
        # A corpus other than the committed one does not gate, so this asserts the
        # score and the report rather than the exit code.
        assert main(["run", "--corpus", str(corpus), "--baseline", str(baseline), "--report"]) == 0
        out = capsys.readouterr().out
        assert "EVAL SCORED (not gated" in out
        assert "MISS" in out

    def test_the_committed_corpus_gates_and_can_go_red(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        baseline = tmp_path / "baseline.json"
        baseline.write_text(
            stdlib_json.dumps({"engines": {"rules": {"thresholds": {"min_labeled": 9999}}}}), encoding="utf-8"
        )
        assert main(["run", "--baseline", str(baseline)]) == 1
        assert "EVAL RED" in capsys.readouterr().out

    def test_a_broken_corpus_exits_two_rather_than_scoring(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["run", "--corpus", str(_write(tmp_path, "{oops"))]) == 2
        assert "invalid JSON" in capsys.readouterr().out

    def test_baseline_is_a_dry_run_unless_asked_to_write(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "baseline.json"
        assert main(["baseline", "--baseline", str(target)]) == 0
        assert not target.exists()
        assert "dry run" in capsys.readouterr().out

    def test_baseline_write_records_thresholds_that_then_pass(self, tmp_path: Path) -> None:
        target = tmp_path / "baseline.json"
        assert main(["baseline", "--baseline", str(target), "--write"]) == 0
        assert main(["run", "--baseline", str(target)]) == 0

    def test_json_output_carries_the_metrics_and_failures(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["run", "--json"]) == 0
        payload = stdlib_json.loads(capsys.readouterr().out)
        assert payload["failures"] == []
        assert payload["metrics"]["engine"] == "rules"


def test_the_fingerprint_moves_with_every_committed_thing_that_steers_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each source in the digest has to actually change it, or the gate above is
    a gate over a subset and the rest of the steering is unguarded.

    The list is deliberately committed material only. The deployed model name is
    an env var, so an offline gate cannot see it — that hole is named in the note
    and covered by recording the model into the baseline instead.
    """
    from scripts import eval_analysis

    baseline = eval_analysis.steering_fingerprint()
    assert baseline == eval_analysis.steering_fingerprint(), "the digest must be deterministic"

    for relative in eval_analysis.STEERING_SOURCES:
        original = (eval_analysis.ROOT / relative).read_bytes()
        try:
            (eval_analysis.ROOT / relative).write_bytes(original + b"\n# nudge\n")
            assert eval_analysis.steering_fingerprint() != baseline, f"editing {relative} must move the digest"
        finally:
            (eval_analysis.ROOT / relative).write_bytes(original)

    assert eval_analysis.steering_fingerprint() == baseline, "and restoring it must move it back"


def _ai_entry(fingerprint: str, **recorded: object) -> dict[str, object]:
    """A schema-valid ai entry. load_baseline validates the whole file on the way
    in, which is right — a malformed baseline is a real problem — so a fixture
    that skips thresholds tests the validator, not the gate."""
    return {
        "steering_fingerprint": fingerprint,
        "recorded": dict(recorded),
        "thresholds": {
            "min_labeled": 16,
            "min_exact_rate": 0.95,
            "min_high_recall": 1.0,
            "max_over_rate": 0.05,
            "max_miss_rate": 0.0,
        },
    }


def test_a_moved_prompt_with_a_stale_ai_score_fails_the_gate(tmp_path: Path) -> None:
    """The whole point. A prompt rewrite used to face nothing at all."""
    from scripts.eval_analysis import main

    path = tmp_path / "baseline.json"
    path.write_text(
        stdlib_json.dumps({"engines": {"ai": _ai_entry("0000deadbeef0000")}}),
        encoding="utf-8",
    )

    assert main(["assert-fresh", "--baseline", str(path)]) == 1


def test_no_recorded_ai_score_is_a_failure_and_not_a_skip(tmp_path: Path) -> None:
    """A gate that reports "not applicable" while the thing it guards is
    unguarded is how the ai path stayed unmeasured for months. The message names
    the one command that fixes it."""
    from scripts.eval_analysis import main

    empty = tmp_path / "baseline.json"
    empty.write_text(
        stdlib_json.dumps({"engines": {"rules": _ai_entry("irrelevant")}}),
        encoding="utf-8",
    )

    assert main(["assert-fresh", "--baseline", str(empty)]) == 1
    assert main(["assert-fresh", "--baseline", str(tmp_path / "absent.json")]) == 1


def test_a_matching_fingerprint_passes(tmp_path: Path) -> None:
    """And it must actually pass when the score is current, or the gate gets
    disabled by whoever is trying to ship."""
    from scripts.eval_analysis import main, steering_fingerprint

    path = tmp_path / "baseline.json"
    path.write_text(
        stdlib_json.dumps({"engines": {"ai": _ai_entry(steering_fingerprint(), model="glm-5.3")}}),
        encoding="utf-8",
    )

    assert main(["assert-fresh", "--baseline", str(path)]) == 0


def test_an_unwritable_baseline_still_prints_what_it_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A paid measurement must survive a write error.

    This failed once for the dullest possible reason: recorded inside a container
    whose uid could not write the mounted evals/ directory, after ten minutes and
    every model call had already been paid for. The traceback threw the numbers
    away. Now the run exits non-zero AND prints them, so the answer can be saved
    by hand instead of bought twice.
    """
    from scripts import eval_analysis

    target = tmp_path / "nowhere" / "baseline.json"

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "write_text", _explode)
    monkeypatch.setattr(eval_analysis, "_score", lambda _args: eval_analysis.Report(engine="ai", policy="env"))

    code = eval_analysis.main(["baseline", "--engine", "ai", "--baseline", str(target), "--write"])
    out = capsys.readouterr().out

    assert code == 1, "a failed record is a failure"
    assert "could not write" in out
    assert '"engines"' in out, "the measurement itself has to be in the output"
