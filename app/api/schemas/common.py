"""Shared API response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class ErrorDetail(APIModel):
    code: str = Field(description="Stable machine-readable error code")
    message: str = Field(description="Human-readable, safe to display")
    request_id: str | None = Field(default=None, description="Correlation id for support")


class ErrorResponse(APIModel):
    error: ErrorDetail


class PageMeta(APIModel):
    page: int
    per_page: int
    total: int
    pages: int
    has_next: bool


class Paginated(APIModel, Generic[T]):
    data: list[T]
    meta: PageMeta


class HealthResponse(APIModel):
    status: str
    version: str
    #: Never includes credentials, hostnames or connection strings.
    checks: dict[str, Any] = Field(default_factory=dict)
