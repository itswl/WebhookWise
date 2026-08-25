"""The per-machine pointer that lets Claude Code see .agents/skills.

This repository ships no `.claude/` directory. Codex reads `.agents/skills`
natively and hookprobe mounts it by absolute path; Claude Code has no
project-level discovery path that avoids `.claude/`, so its pointer lives in
$HOME and no clone inherits it. That makes the check below the only thing
standing between "somebody added a skill" and "nobody can see it".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import assert_skill_pointers as checker


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    skills = tmp_path / "repo" / ".agents" / "skills"
    for name in ("alpha", "beta"):
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    home = tmp_path / "home" / ".claude"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(checker, "SKILLS", skills)
    monkeypatch.setattr(checker, "CLAUDE_HOME", home)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return tmp_path


def _link(repo: Path, name: str, target: Path) -> None:
    (repo / "home" / ".claude" / "skills" / name).symlink_to(target)


def test_a_skill_added_without_a_pointer_is_reported(repo: Path) -> None:
    """The likely failure is not a fresh clone. It is somebody adding a skill six
    weeks from now, never linking it, and wondering why the new one is invisible
    while the old ones work."""
    _link(repo, "alpha", repo / "repo" / ".agents" / "skills" / "alpha")

    missing, wrong, stale = checker.audit()

    assert missing == ["beta"]
    assert (wrong, stale) == ([], [])


def test_a_pointer_into_another_checkout_is_wrong_not_missing(repo: Path) -> None:
    """Two clones on one machine is the ordinary way this goes subtly wrong: the
    link exists, the skill appears, and it is a different revision of it."""
    other = repo / "elsewhere" / ".agents" / "skills" / "alpha"
    other.mkdir(parents=True)
    _link(repo, "alpha", other)
    _link(repo, "beta", repo / "repo" / ".agents" / "skills" / "beta")

    missing, wrong, stale = checker.audit()

    assert wrong == ["alpha"]
    assert missing == []


def test_a_renamed_skill_leaves_a_dangling_link(repo: Path) -> None:
    """Renaming a skill leaves the old pointer aimed at nothing, which is not
    reported by "is anything missing" and would otherwise sit there forever."""
    for name in ("alpha", "beta"):
        _link(repo, name, repo / "repo" / ".agents" / "skills" / name)
    _link(repo, "gamma", repo / "repo" / ".agents" / "skills" / "gamma")

    missing, wrong, stale = checker.audit()

    assert stale == ["gamma"]
    assert (missing, wrong) == ([], [])


def test_all_pointers_present_is_silent_success(repo: Path) -> None:
    for name in ("alpha", "beta"):
        _link(repo, name, repo / "repo" / ".agents" / "skills" / name)

    assert checker.audit() == ([], [], [])
    assert checker.main([]) == 0


def test_ci_is_skipped_and_never_fails(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CI has no business owning these links. A check that failed there would be
    a check somebody deletes."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert checker.main(["--strict"]) == 0


def test_strict_is_what_makes_it_a_gate(repo: Path) -> None:
    """Default reports and continues: a missing link in one person's HOME is not
    a broken repository, and a gate that blocks a push over it teaches people to
    skip the gate. --strict exists for whoever decides otherwise."""
    assert checker.main([]) == 0, "default must not fail"
    assert checker.main(["--strict"]) == 1
