"""Write / registry router — the product surface.

POST a polygon → ``{geoid, uri}``; resolve durably by geoid; resolve by
``(external_id, collection)``. Anonymous POSTs are allowed into ``writable_anon``
collections via the shared registry service (no special code path).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.api.format_param import output_format
from geoid.api.responses import GeoJSONResponse, WKTResponse, feature_response
from geoid.config import Settings, get_settings
from geoid.db import get_session
from geoid.deps import Principal, require_principal
from geoid.domain.geometry_format import GeometryFormat
from geoid.repositories import collection_repo, place_repo
from geoid.schemas.ogc import FeatureModel
from geoid.schemas.place import (
    BulkFeatureCollection,
    BulkReport,
    GeometryConflictResponse,
    MintResponse,
    PlaceCreate,
)
from geoid.services import ogc_service, registry_service
from geoid.services.exceptions import (
    BulkLimitExceededError,
    CollectionNotFoundError,
    PlaceNotFoundError,
)

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


@router.post(
    "/collections/{collection_id}/items/bulk",
    response_model=BulkReport,
    status_code=status.HTTP_200_OK,
    summary="Bulk-mint geoids from a GeoJSON FeatureCollection (synchronous, partial success)",
)
async def create_items_bulk(
    collection_id: str,
    body: BulkFeatureCollection,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> BulkReport:
    # A write bound MUST error, never truncate: too many features rejects the
    # whole request (413) before any insert. Auth (anon → writable_anon) is checked
    # once up front in the service, since it depends on principal + collection only.
    if len(body.features) > settings.bulk_max_features:
        raise BulkLimitExceededError(len(body.features), settings.bulk_max_features)
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    return await registry_service.create_places_bulk(
        session,
        settings=settings,
        principal=principal,
        collection=collection,
        features=body.features,
    )


@router.get(
    "/geoid/{geoid}",
    response_model=FeatureModel,
    response_class=GeoJSONResponse,
    summary="Durable geoid resolver",
    responses={200: {"content": {"text/plain": {}}}},
)
async def resolve_geoid(
    geoid: uuid.UUID,
    fmt: GeometryFormat = Depends(output_format),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FeatureModel | WKTResponse:
    row = await place_repo.get_by_geoid(session, geoid)
    if row is None:
        raise PlaceNotFoundError(str(geoid))
    feature = ogc_service.build_feature(settings, row)
    return feature_response(feature, fmt)


@router.get(
    "/collections/{collection_id}/external/{external_id}",
    response_model=FeatureModel,
    response_class=GeoJSONResponse,
    summary="Resolve a place by (external_id, collection)",
    responses={200: {"content": {"text/plain": {}}}},
)
async def resolve_by_external_id(
    collection_id: str,
    external_id: str,
    fmt: GeometryFormat = Depends(output_format),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FeatureModel | WKTResponse:
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    row = await place_repo.get_by_external_id(session, collection.id, external_id)
    if row is None:
        raise PlaceNotFoundError(f"{collection_id}/{external_id}")
    feature = ogc_service.build_feature(settings, row)
    return feature_response(feature, fmt)
