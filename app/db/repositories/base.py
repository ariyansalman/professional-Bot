"""Repository base: typed CRUD plus keyset-friendly pagination."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


@dataclass(slots=True)
class Page(Generic[ModelT]):
    """One page of results plus the metadata the UI needs to draw controls."""

    items: Sequence[ModelT]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / self.per_page)) if self.per_page else 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def is_empty(self) -> bool:
        return not self.items

    @property
    def label(self) -> str:
        return f"{self.page}/{self.pages}"


class BaseRepository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, entity_id: Any) -> ModelT | None:
        return await self.session.get(self.model, entity_id)

    async def get_many(self, ids: Sequence[Any]) -> list[ModelT]:
        if not ids:
            return []
        stmt = select(self.model).where(self.model.id.in_(list(ids)))  # type: ignore[attr-defined]
        return list((await self.session.scalars(stmt)).all())

    async def add(self, entity: ModelT) -> ModelT:
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def add_all(self, entities: Sequence[ModelT]) -> Sequence[ModelT]:
        self.session.add_all(list(entities))
        await self.session.flush()
        return entities

    async def paginate(
        self, stmt: Select, *, page: int = 1, per_page: int = 10
    ) -> Page[ModelT]:
        page = max(1, page)
        per_page = max(1, per_page)
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self.session.scalar(count_stmt)) or 0)
        rows = await self.session.scalars(stmt.limit(per_page).offset((page - 1) * per_page))
        return Page(items=list(rows.all()), total=total, page=page, per_page=per_page)

    async def count(self, stmt: Select) -> int:
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        return int((await self.session.scalar(count_stmt)) or 0)
