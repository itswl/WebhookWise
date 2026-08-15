"""Deployment migration gate contracts."""

import importlib.util

from scripts.healthcheck import _expected_migration_heads, _migration_heads_match


def test_expected_migration_head_is_current_image_head() -> None:
    assert _expected_migration_heads() == {"0031_inbound_rules"}


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


def test_migration_json_targets_name_tables_that_actually_exist() -> None:
    """A migration naming a table that does not exist fails mid-revision, after
    the earlier statements in it have already run.

    0029 named `forward_outbox`; the table is `forward_outboxes`. It surfaced
    only because the revision was rehearsed against a clone of production first.
    The assertion is cheap, so it stops being luck.
    """
    import models  # noqa: F401 - registers every table on Base.metadata
    from db.session import Base
    from tests.helpers.paths import PROJECT_ROOT

    path = PROJECT_ROOT / "alembic/versions/0029_neutral_deep_analysis.py"
    spec = importlib.util.spec_from_file_location("_migration_0029", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    real_tables = set(Base.metadata.tables)
    for table, column in module._JSON_COLUMNS:
        assert table in real_tables, f"migration 0029 targets unknown table {table!r}"
        assert column in Base.metadata.tables[table].columns, f"{table} has no column {column!r}"
