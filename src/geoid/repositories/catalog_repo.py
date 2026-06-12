"""Catalog data access."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.domain.identifiers import uuid7
from geoid.models import Catalog


async def get_by_slug(session: AsyncSession, slug: str) -> Catalog | None:
    stmt = select(Catalog).where(Catalog.slug == slug)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_by_id(session: AsyncSession, catalog_id: uuid.UUID) -> Catalog | None:
    return await session.get(Catalog, catalog_id)


async def list_all(session: AsyncSession) -> list[Catalog]:
    stmt = select(Catalog).order_by(Catalog.slug.asc())
    return list((await session.execute(stmt)).scalars().all())


async def create(
    session: AsyncSession,
    *,
    slug: str,
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Catalog:
    catalog = Catalog(id=uuid7(), slug=slug, title=title, meta=metadata or {})
    session.add(catalog)
    await session.flush()
    return catalog
