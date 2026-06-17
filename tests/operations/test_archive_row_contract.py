"""Contract: _archive_row must populate every ArchivedWebhookEvent column.

A column added to ArchivedWebhookEvent but forgotten in _archive_row silently
drops that field on archival with no error (the bulk insert just omits it). This
test fails loudly when the two drift apart.
"""

from __future__ import annotations

from models import ArchivedWebhookEvent
from services.operations.data_maintenance import _archive_row


def test_archive_row_keys_match_archived_columns() -> None:
    # archived_at is stamped by the archival call itself, not copied from the event.
    archived_columns = {c.name for c in ArchivedWebhookEvent.__table__.columns}

    class _Event:
        def __getattr__(self, _name: str) -> None:
            return None

    produced_keys = set(_archive_row(_Event(), archived_at=None).keys())  # type: ignore[arg-type]

    missing = archived_columns - produced_keys
    extra = produced_keys - archived_columns
    assert not missing, f"_archive_row is missing archived columns: {sorted(missing)}"
    assert not extra, f"_archive_row produces keys not on the archived table: {sorted(extra)}"


def test_acknowledgement_columns_are_archived() -> None:
    # Direct guard for the ack feature: both ack columns must round-trip to archive.
    keys = set(_archive_row(_FakeEvent(), archived_at=None).keys())  # type: ignore[arg-type]
    assert "acknowledged_at" in keys
    assert "acknowledged_by" in keys


class _FakeEvent:
    def __getattr__(self, _name: str) -> None:
        return None
