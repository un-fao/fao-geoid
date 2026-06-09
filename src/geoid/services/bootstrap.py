"""Startup bootstrap — guarantee the reserved anonymous collection exists.

Anonymous contributions land in one reserved ``public`` collection
(``writable_anon=true``). The slug is config-driven (``GEOID_PUBLIC_COLLECTION``),
so seeding lives here (idempotent) rather than in a migration.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from geoid.config import Settings
from geoid.models import Collection
from geoid.repositories import collection_repo, workspace_repo

_DEFAULT_WORKSPACE_SLUG = "default"


async def ensure_public_collection(session: AsyncSession, settings: Settings) -> Collection:
    """Create the default workspace + reserved public collection if missing."""
    workspace = await workspace_repo.get_by_slug(session, _DEFAULT_WORKSPACE_SLUG)
    if workspace is None:
        workspace = await workspace_repo.create(
            session, slug=_DEFAULT_WORKSPACE_SLUG, title="Default workspace"
        )

    collection = await collection_repo.get_by_slug(
        session, settings.public_collection, workspace_id=workspace.id
    )
    if collection is None:
        collection = await collection_repo.create(
            session,
            workspace_id=workspace.id,
            slug=settings.public_collection,
            title="Public (anonymous contributions)",
            writable_anon=True,
            metadata={"dedup_grid": settings.dedup_grid_default},
        )
    return collection
