"""Startup bootstrap — guarantee the reserved anonymous collection exists.

Anonymous contributions land in one reserved ``public`` collection
(``writable_anon=true``). The slug is config-driven (``GEOID_PUBLIC_COLLECTION``),
so seeding lives here (idempotent) rather than in a migration.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from geoid.config import Settings
from geoid.models import Collection
from geoid.repositories import catalog_repo, collection_repo

_DEFAULT_CATALOG_SLUG = "default"


async def ensure_public_collection(session: AsyncSession, settings: Settings) -> Collection:
    """Create the default catalog + reserved public collection if missing."""
    catalog = await catalog_repo.get_by_slug(session, _DEFAULT_CATALOG_SLUG)
    if catalog is None:
        catalog = await catalog_repo.create(
            session, slug=_DEFAULT_CATALOG_SLUG, title="Default catalog"
        )

    collection = await collection_repo.get_by_slug(
        session, settings.public_collection, catalog_id=catalog.id
    )
    if collection is None:
        collection = await collection_repo.create(
            session,
            catalog_id=catalog.id,
            slug=settings.public_collection,
            title="Public (anonymous contributions)",
            writable_anon=True,
        )
    return collection
