from __future__ import annotations

from pydantic import BaseModel


class APIResponse[T](BaseModel):
    success: bool
    data: T | None = None
    message: str | None = None
    error: str | None = None


class CursorPaginationInfo(BaseModel):
    next_cursor: int | None = None
    has_more: bool = False
    page_size: int | None = None
