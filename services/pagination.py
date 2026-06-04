"""Shared helpers for cursor-based read-side pagination."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class CursorWindow(Generic[_T]):
    rows: list[_T]
    has_more: bool
    next_cursor: int | None


def clamp_page_params(
    page: int,
    page_size: int,
    *,
    max_page: int,
    max_page_size: int | None = None,
) -> tuple[int, int]:
    """Clamp page parameters to a positive, bounded range."""
    page_size_limit = max_page if max_page_size is None else max_page_size
    return max(1, min(page, max_page)), max(1, min(page_size, page_size_limit))


def apply_cursor_window(
    query: Any,
    id_column: Any,
    *,
    page: int,
    page_size: int,
    cursor: int | None,
) -> Any:
    """Apply cursor-or-offset pagination and fetch one extra row."""
    query = query.where(id_column < cursor) if cursor is not None else query.offset((page - 1) * page_size)
    return query.limit(page_size + 1)


def trim_cursor_window(
    rows: Sequence[_T],
    page_size: int,
    cursor_getter: Callable[[_T], int | None],
) -> CursorWindow[_T]:
    """Trim the extra row and derive the next cursor from the visible page."""
    has_more = len(rows) > page_size
    visible_rows = list(rows[:page_size] if has_more else rows)
    next_cursor = cursor_getter(visible_rows[-1]) if has_more and visible_rows else None
    return CursorWindow(rows=visible_rows, has_more=has_more, next_cursor=next_cursor)
