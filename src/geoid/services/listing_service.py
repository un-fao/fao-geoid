"""Listing service — backs the 1.2 management slice (admin-gated)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from geoid.models import Collection
from geoid.repositories import collection_repo, place_repo
from geoid.schemas.collection import CollectionOut, ItemIdList
from geoid.services.exceptions import CollectionNotFoundError


def _collection_out(collection: Collection) -> CollectionOut:
    return CollectionOut(
        id=collection.slug,
        title=collection.title,
        writable_anon=collection.writable_anon,
        metadata=collection.meta,
    )


async def list_collections(session: AsyncSession) -> list[CollectionOut]:
    collections = await collection_repo.list_all(session)
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
