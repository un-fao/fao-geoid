"""Listing service — backs the 1.2 management slice (admin-gated)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from geoid.models import Collection
from geoid.repositories import catalog_repo, collection_repo, place_repo
from geoid.schemas.collection import CatalogOut, CollectionOut, ItemIdList
from geoid.services.exceptions import CatalogNotFoundError, CollectionNotFoundError


def _collection_out(collection: Collection) -> CollectionOut:
    return CollectionOut(
        id=str(collection.id),
        catalog_id=str(collection.catalog_id),
        slug=collection.slug,
        title=collection.title,
        writable_anon=collection.writable_anon,
        metadata=collection.meta,
    )


async def list_catalogs(session: AsyncSession) -> list[CatalogOut]:
    catalogs = await catalog_repo.list_all(session)
    return [
        CatalogOut(id=str(c.id), slug=c.slug, title=c.title, metadata=c.meta) for c in catalogs
    ]


async def list_collections_in_catalog(
    session: AsyncSession, catalog_slug: str
) -> list[CollectionOut]:
    catalog = await catalog_repo.get_by_slug(session, catalog_slug)
    if catalog is None:
        raise CatalogNotFoundError(catalog_slug)
    collections = await collection_repo.list_by_catalog(session, catalog.id)
    return [_collection_out(c) for c in collections]


async def list_item_ids(
    session: AsyncSession, collection_slug: str, *, limit: int, offset: int
) -> ItemIdList:
    collection = await collection_repo.get_by_slug(session, collection_slug)
    if collection is None:
        raise CollectionNotFoundError(collection_slug)
    ids, total = await place_repo.list_item_ids(session, collection.id, limit=limit, offset=offset)
    return ItemIdList(
        collection=collection_slug,
        numberMatched=total,
        numberReturned=len(ids),
        item_ids=[str(i) for i in ids],
    )
