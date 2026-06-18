"""Management router (admin-gated) — collection create + the 1.2 listing slice.

Collections are the flat entry point: the catalog tier is a single internal row
(bootstrap-created) and is not exposed here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.api.paging import enforce_max_offset
from geoid.config import Settings, get_settings
from geoid.db import get_session
from geoid.deps import require_admin
from geoid.repositories import catalog_repo, collection_repo
from geoid.schemas.collection import CollectionCreate, CollectionOut, ItemIdList
from geoid.services import listing_service

router = APIRouter(prefix="/manage", tags=["manage"], dependencies=[Depends(require_admin)])


@router.post("/collections", response_model=CollectionOut, status_code=status.HTTP_201_CREATED)
async def create_collection(
    body: CollectionCreate,
    session: AsyncSession = Depends(get_session),
) -> CollectionOut:
    catalog = await catalog_repo.get_or_create_default(session)
    collection = await collection_repo.create(
        session,
        catalog_id=catalog.id,
        slug=body.id,
        title=body.title,
        writable_anon=body.writable_anon,
        metadata=body.metadata,
    )
    return CollectionOut(
        id=collection.slug,
        title=collection.title,
        writable_anon=collection.writable_anon,
        metadata=collection.meta,
    )


@router.get(
    "/collections",
    response_model=list[CollectionOut],
    summary="List collections (1.2)",
)
async def list_collections(session: AsyncSession = Depends(get_session)) -> list[CollectionOut]:
    return await listing_service.list_collections(session)


@router.get(
    "/collections/{collection_id}/item-ids",
    response_model=ItemIdList,
    summary="List item-ids (geoids) in a collection (1.2)",
    # Phase-1 demo: hidden from the API definition (Processes-only surface).
    # The route stays live and serving; re-enable = drop include_in_schema.
    include_in_schema=False,
)
async def list_item_ids(
    collection_id: str,
    limit: int = Query(default=1000, ge=1, le=100_000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ItemIdList:
    enforce_max_offset(offset, settings)
    return await listing_service.list_item_ids(session, collection_id, limit=limit, offset=offset)
