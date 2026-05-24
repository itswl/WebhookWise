from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class APIResponse(BaseModel, Generic[T]):
    success: bool
    data: T | None = None
    message: str | None = None
    error: str | None = None


class CursorPaginationInfo(BaseModel):
    next_cursor: int | None = None
    has_more: bool = False
    page_size: int | None = None
