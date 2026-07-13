"""Deployment migration gate contracts."""

from scripts.healthcheck import _expected_migration_heads, _migration_heads_match


def test_expected_migration_head_is_current_image_head() -> None:
    assert _expected_migration_heads() == {"0013_noise_reduction_actions"}


def test_migration_gate_rejects_stale_and_partial_revisions() -> None:
    expected = {"0012_operator_workflow"}

    assert _migration_heads_match(expected, expected)
    assert not _migration_heads_match({"0010_audit_log"}, expected)
    assert not _migration_heads_match(set(), expected)
