"""Unit tests for the floor-satisfaction logic in scripts/check_requirements_locks.py.

The lock checker must reject a lock whose pin is below a declared floor (the drift
that name-presence checks miss), while accepting satisfied floors, extras, inline
comments, and pre-release pins.
"""

from pathlib import Path

import pytest

from scripts.check_requirements_locks import (
    _assert_locked_satisfies,
    _declared_requirements,
    _locked_versions,
)


def _write(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_locked_version_satisfying_floor_passes(tmp_path: Path) -> None:
    declared = _write(tmp_path / "requirements.txt", ["foo>=1.2.0", "bar[extra]>=2.0  # inline note"])
    lock = _write(tmp_path / "requirements.lock", ["foo==1.2.0", "bar==2.5.1"])
    # Should not raise.
    _assert_locked_satisfies(declared, _locked_versions(lock), "requirements.lock")


def test_locked_version_below_floor_raises(tmp_path: Path) -> None:
    declared = _write(tmp_path / "requirements.txt", ["foo>=1.3.0"])
    lock = _write(tmp_path / "requirements.lock", ["foo==1.2.0"])
    with pytest.raises(SystemExit, match="does not satisfy the"):
        _assert_locked_satisfies(declared, _locked_versions(lock), "requirements.lock")


def test_requirement_missing_from_lock_raises(tmp_path: Path) -> None:
    declared = _write(tmp_path / "requirements.txt", ["foo>=1.0"])
    lock = _write(tmp_path / "requirements.lock", ["other==9.9"])
    with pytest.raises(SystemExit, match="missing a direct requirement"):
        _assert_locked_satisfies(declared, _locked_versions(lock), "requirements.lock")


def test_prerelease_pin_satisfies_prerelease_floor(tmp_path: Path) -> None:
    declared = _write(tmp_path / "requirements.txt", ["otel>=0.48b0"])
    lock = _write(tmp_path / "requirements.lock", ["otel==0.62b1"])
    # A beta pin above a beta floor must be accepted (prereleases=True).
    _assert_locked_satisfies(declared, _locked_versions(lock), "requirements.lock")


def test_inline_comment_stripped_only_when_preceded_by_whitespace(tmp_path: Path) -> None:
    declared = _write(
        tmp_path / "requirements.txt",
        [
            "foo>=1.2.0  # inline note",
            "bar @ https://example.com/pkg.tar.gz#sha256=deadbeef",
        ],
    )
    reqs = {r.name: r for r in _declared_requirements(declared)}
    # A whitespace-preceded '#' is a comment and is dropped.
    assert str(reqs["foo"].specifier) == ">=1.2.0"
    # A '#' inside a token (URL fragment) is not a comment and is preserved.
    assert reqs["bar"].url == "https://example.com/pkg.tar.gz#sha256=deadbeef"


def test_unparseable_requirement_line_raises_clean_systemexit(tmp_path: Path) -> None:
    declared = _write(tmp_path / "requirements.txt", ["foo bar baz"])
    with pytest.raises(SystemExit, match="unparseable requirement line"):
        _declared_requirements(declared)
