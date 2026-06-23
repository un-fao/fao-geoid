"""Listing service — backs the 1.2 management slice (admin-gated)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from geoid.models import Collection
from geoid.repositories import catalog_repo, collection_repo
from geoid.schemas.collection import CollectionCreate, CollectionOut


def _collection_out(collection: Collection) -> CollectionOut:
    return CollectionOut(
        id=collection.slug,
        title=collection.title,
        writable_anon=collection.writable_anon,
        metadata=collection.meta,
    )


async def create_collection(session: AsyncSession, body: CollectionCreate) -> CollectionOut:
    """Create a collection under the single internal catalog (admin-gated write).

    The catalog tier is a create-once internal row, so this resolves it via
    ``get_or_create_default`` before inserting the collection — the management
    surface stays flat (collections only). A duplicate ``id`` raises the catalog-slug
    IntegrityError, mapped to 409 in ``api/errors.py``.
    """
    catalog = await catalog_repo.get_or_create_default(session)
    collection = await collection_repo.create(
        session,
        catalog_id=catalog.id,
        slug=body.id,
        title=body.title,
        writable_anon=body.writable_anon,
        metadata=body.metadata,
    )
    return _collection_out(collection)


async def list_collections(session: AsyncSession) -> list[CollectionOut]:
    collections = await collection_repo.list_all(session)
    return [_collection_out(c) for c in collections]
