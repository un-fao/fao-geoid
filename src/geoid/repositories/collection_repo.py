"""Collection data access.

OGC API Features addresses collections by a flat ``{collectionId}``; we treat the
collection ``slug`` as that id. Slug uniqueness is per-workspace (the DB
constraint), so a multi-workspace deployment could in principle have a slug
collision — :func:`get_by_slug` resolves the first match and the single default
workspace makes this unambiguous in Phase 1. Pass ``workspace_id`` to scope it.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.domain.identifiers import uuid7
from geoid.models import Collection


async def get_by_slug(
    session: AsyncSession, slug: str, *, workspace_id: uuid.UUID | None = None
) -> Collection | None:
    stmt = select(Collection).where(Collection.slug == slug)
    if workspace_id is not None:
        stmt = stmt.where(Collection.workspace_id == workspace_id)
    stmt = stmt.order_by(Collection.slug.asc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_by_id(session: AsyncSession, collection_id: uuid.UUID) -> Collection | None:
    return await session.get(Collection, collection_id)


async def list_all(session: AsyncSession) -> list[Collection]:
    stmt = select(Collection).order_by(Collection.slug.asc())
    return list((await session.execute(stmt)).scalars().all())


async def list_by_workspace(session: AsyncSession, workspace_id: uuid.UUID) -> list[Collection]:
    stmt = (
        select(Collection)
        .where(Collection.workspace_id == workspace_id)
        .order_by(Collection.slug.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def create(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    slug: str,
    title: str | None = None,
    writable_anon: bool = False,
    metadata: dict[str, Any] | None = None,
) -> Collection:
    collection = Collection(
        id=uuid7(),
        workspace_id=workspace_id,
        slug=slug,
        title=title,
        writable_anon=writable_anon,
        meta=metadata or {},
    )
    session.add(collection)
    await session.flush()
    return collection
