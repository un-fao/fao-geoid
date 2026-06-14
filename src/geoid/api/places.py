"""Write / registry router — the product surface.

POST a polygon → ``{geoid, uri}``; resolve durably by geoid; resolve by
``(external_id, collection)``. Anonymous POSTs are allowed into ``writable_anon``
collections via the shared registry service (no special code path).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.api.responses import GeoJSONResponse
from geoid.config import Settings, get_settings
from geoid.db import get_session
from geoid.deps import Principal, require_principal
from geoid.repositories import collection_repo, place_repo
from geoid.schemas.ogc import FeatureModel
from geoid.schemas.place import GeometryConflictResponse, MintResponse, PlaceCreate
from geoid.services import ogc_service, registry_service
from geoid.services.exceptions import CollectionNotFoundError, PlaceNotFoundError

router = APIRouter(tags=["registry"])


@router.post(
    "/collections/{collection_id}/items",
    response_model=MintResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Mint a geoid for a place (duplicate geometry fails with 409)",
    responses={
        status.HTTP_409_CONFLICT: {
            "model": GeometryConflictResponse,
            "description": (
                "An identical geometry already exists in the catalog; the body "
                "carries the incumbent geoid (geometry dedup is global). An "
                "external_id duplicate also answers 409 — discriminate on "
                "``constraint``."
            ),
        }
    },
)
async def create_item(
    collection_id: str,
    feature: PlaceCreate,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> MintResponse:
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    return await registry_service.create_place(
        session,
        settings=settings,
        principal=principal,
        collection=collection,
        feature=feature,
    )


@router.get(
    "/geoid/{geoid}",
    response_model=FeatureModel,
    response_class=GeoJSONResponse,
    summary="Durable geoid resolver",
)
async def resolve_geoid(
    geoid: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FeatureModel:
    row = await place_repo.get_by_geoid(session, geoid)
    if row is None:
        raise PlaceNotFoundError(str(geoid))
    return ogc_service.build_feature(settings, row)


@router.get(
    "/collections/{collection_id}/external/{external_id}",
    response_model=FeatureModel,
    response_class=GeoJSONResponse,
    summary="Resolve a place by (external_id, collection)",
)
async def resolve_by_external_id(
    collection_id: str,
    external_id: str,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FeatureModel:
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    row = await place_repo.get_by_external_id(session, collection.id, external_id)
    if row is None:
        raise PlaceNotFoundError(f"{collection_id}/{external_id}")
    return ogc_service.build_feature(settings, row)
