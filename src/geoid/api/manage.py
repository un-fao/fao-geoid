"""Management router (admin-gated) — collection create + list.

Collections are the flat entry point: the catalog tier is a single internal row
(bootstrap-created) and is not exposed here. Item enumeration (the former
``/collections/{id}/item-ids``) has been removed — no endpoint lists a
collection's items.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.db import get_session
from geoid.deps import require_admin
from geoid.schemas.collection import CollectionCreate, CollectionOut
from geoid.services import listing_service

router = APIRouter(prefix="/manage", tags=["manage"], dependencies=[Depends(require_admin)])


@router.post("/collections", response_model=CollectionOut, status_code=status.HTTP_201_CREATED)
async def create_collection(
    body: CollectionCreate,
    session: AsyncSession = Depends(get_session),
) -> CollectionOut:
    return await listing_service.create_collection(session, body)


@router.get(
    "/collections",
    response_model=list[CollectionOut],
    summary="List collections (1.2)",
)
async def list_collections(session: AsyncSession = Depends(get_session)) -> list[CollectionOut]:
    return await listing_service.list_collections(session)
