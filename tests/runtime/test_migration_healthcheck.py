"""Deployment migration gate contracts."""

from scripts.healthcheck import _expected_migration_heads, _migration_heads_match


def test_expected_migration_head_is_current_image_head() -> None:
    assert _expected_migration_heads() == {"0017_maintenance_windows"}


def test_migration_gate_rejects_stale_and_partial_revisions() -> None:
    expected = {"0012_operator_workflow"}

    assert _migration_heads_match(expected, expected)
    assert not _migration_heads_match({"0010_audit_log"}, expected)
    assert not _migration_heads_match(set(), expected)


def test_revision_ids_fit_alembic_version_column() -> None:
    """alembic_version.version_num is VARCHAR(32); a longer revision id passes
    every sqlite-backed test and then fails the real-Postgres upgrade at the
    final version-row UPDATE (caught live by the e2e for 0017)."""
    import re
    from pathlib import Path

    versions = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    pattern = re.compile(r'^revision(?::\s*str)?\s*=\s*"([^"]+)"', re.MULTILINE)
    too_long: list[str] = []
    for path in sorted(versions.glob("*.py")):
        match = pattern.search(path.read_text(encoding="utf-8"))
        if match and len(match.group(1)) > 32:
            too_long.append(f"{path.name}: {match.group(1)}")
    assert too_long == [], f"revision ids longer than 32 chars: {too_long}"
