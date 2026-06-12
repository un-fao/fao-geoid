"""Management router (admin-gated) — catalog/collection create + the 1.2 listing slice."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.api.paging import enforce_max_offset
from geoid.config import Settings, get_settings
from geoid.db import get_session
from geoid.deps import require_admin
from geoid.repositories import catalog_repo, collection_repo
from geoid.schemas.collection import (
    CatalogCreate,
    CatalogOut,
    CollectionCreate,
    CollectionOut,
    ItemIdList,
)
from geoid.services import listing_service
from geoid.services.exceptions import CatalogNotFoundError

router = APIRouter(prefix="/manage", tags=["manage"], dependencies=[Depends(require_admin)])


@router.post("/catalogs", response_model=CatalogOut, status_code=status.HTTP_201_CREATED)
async def create_catalog(
    body: CatalogCreate, session: AsyncSession = Depends(get_session)
) -> CatalogOut:
    catalog = await catalog_repo.create(
        session, slug=body.slug, title=body.title, metadata=body.metadata
    )
    return CatalogOut(
        id=str(catalog.id), slug=catalog.slug, title=catalog.title, metadata=catalog.meta
    )


@router.get("/catalogs", response_model=list[CatalogOut])
async def list_catalogs(session: AsyncSession = Depends(get_session)) -> list[CatalogOut]:
    return await listing_service.list_catalogs(session)


@router.post(
    "/catalogs/{catalog_slug}/collections",
    response_model=CollectionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_collection(
    catalog_slug: str,
    body: CollectionCreate,
    session: AsyncSession = Depends(get_session),
) -> CollectionOut:
    catalog = await catalog_repo.get_by_slug(session, catalog_slug)
    if catalog is None:
        raise CatalogNotFoundError(catalog_slug)
    collection = await collection_repo.create(
        session,
        catalog_id=catalog.id,
        slug=body.slug,
        title=body.title,
        writable_anon=body.writable_anon,
        metadata=body.metadata,
    )
    return CollectionOut(
        id=str(collection.id),
        catalog_id=str(collection.catalog_id),
        slug=collection.slug,
        title=collection.title,
        writable_anon=collection.writable_anon,
        metadata=collection.meta,
    )


@router.get(
    "/catalogs/{catalog_slug}/collections",
    response_model=list[CollectionOut],
    summary="List collections in a catalog (1.2)",
)
async def list_collections_in_catalog(
    catalog_slug: str, session: AsyncSession = Depends(get_session)
) -> list[CollectionOut]:
    return await listing_service.list_collections_in_catalog(session, catalog_slug)


@router.get(
    "/collections/{collection_id}/item-ids",
    response_model=ItemIdList,
    summary="List item-ids (geoids) in a collection (1.2)",
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
