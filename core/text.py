"""Small text parsing helpers shared across service boundaries."""

from __future__ import annotations


def split_csv_lower(value: str) -> list[str]:
    """Split a comma-separated string into lowercase, non-empty items."""
    return [item.strip().lower() for item in value.split(",") if item.strip()]
