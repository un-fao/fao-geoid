"""Workspace data access."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.domain.identifiers import uuid7
from geoid.models import Workspace


async def get_by_slug(session: AsyncSession, slug: str) -> Workspace | None:
    stmt = select(Workspace).where(Workspace.slug == slug)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_by_id(session: AsyncSession, workspace_id: uuid.UUID) -> Workspace | None:
    return await session.get(Workspace, workspace_id)


async def list_all(session: AsyncSession) -> list[Workspace]:
    stmt = select(Workspace).order_by(Workspace.slug.asc())
    return list((await session.execute(stmt)).scalars().all())


async def create(
    session: AsyncSession,
    *,
    slug: str,
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Workspace:
    workspace = Workspace(id=uuid7(), slug=slug, title=title, meta=metadata or {})
    session.add(workspace)
    await session.flush()
    return workspace
