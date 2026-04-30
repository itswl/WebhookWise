"""基础响应模型定义"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class APIResponse(BaseModel, Generic[T]):
    """统一 API 响应包装"""

    success: bool
    data: T | None = None
    message: str | None = None
    error: str | None = None


class CursorPaginationInfo(BaseModel):
    next_cursor: int | None = None
    has_more: bool = False
    limit: int | None = None
    page_size: int | None = None


class ErrorResponse(BaseModel):
    success: bool = False
    error: str
