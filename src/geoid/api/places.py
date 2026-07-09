"""Write / registry router — the product surface.

POST a polygon → ``{geoid, uri}``; resolve durably by geoid; resolve by
``(external_id, collection)``. Anonymous POSTs are allowed into ``writable_anon``
collections via the shared registry service (no special code path).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
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
    PlaceRecord,
)
from geoid.services import authz_service, ogc_service, registry_service
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
        status.HTTP_201_CREATED: {
            "headers": {
                "Location": {
                    "description": (
                        "Durable resolver URI of the newly minted geoid (OGC API - "
                        "Features Part 4, Requirement 6: a 201 carries a Location header)."
                    ),
                    "schema": {"type": "string", "format": "uri"},
                }
            }
        },
        status.HTTP_409_CONFLICT: {
            "model": GeometryConflictResponse,
            "description": (
                "An identical geometry already exists in the catalog (geometry "
                "dedup is global). The body carries the incumbent "
                "geoid/uri/collection only for members of the incumbent's "
                "collection (sysadmin / own mint / any grant — public_read does "
                "NOT disclose); otherwise those fields are null. An external_id "
                "duplicate also answers 409 — discriminate on ``constraint``."
            ),
        },
    },
)
async def create_item(
    collection_id: str,
    feature: PlaceCreate,
    response: Response,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> MintResponse:
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    result = await registry_service.create_place(
        session,
        settings=settings,
        principal=principal,
        collection=collection,
        feature=feature,
    )
    response.headers["Location"] = result.uri
    return result


@router.post(
    "/collections/{collection_id}/items/bulk",
    response_model=BulkReport,
    status_code=status.HTTP_200_OK,
    summary=(
        "Bulk-mint geoids from a GeoJSON FeatureCollection; "
        "processed in-request, returns a per-feature report"
    ),
    description=(
        "Partial success at the *report* level, atomic at the *transaction* level: "
        "the whole batch runs in ONE database transaction, so a request timeout or "
        "client disconnect before the response discards ALL rows — including the "
        "ones the report would have listed as accepted. Rows are durable only once "
        "the 200 report is received. There is no resumability; re-submit the batch "
        "(already-minted features simply reject as `geometry_conflict`)."
    ),
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


async def _can_read_collection(
    session: AsyncSession,
    principal: Principal,
    *,
    collection_id: uuid.UUID,
    collection_slug: str,
    public_read: bool,
) -> bool:
    """Grant lookup + the pure ``can_read`` predicate.

    Takes the collection facts as scalars — both resolvers already hold them (on
    the read row / the Collection), so no refetch. NOTE: ``load_caller_grant``
    backfills the Keycloak ``sub`` on first authorized access, so this read path
    can issue one idempotent UPDATE.
    """
    grant = await authz_service.load_caller_grant(
        session, principal, collection_id, collection_slug
    )
    return authz_service.can_read(principal, public_read, grant)


@router.get(
    "/me/geoids",
    response_model=list[PlaceRecord],
    summary="List the geoids the authenticated caller has minted (newest first)",
    description=(
        "Keyed on the caller's stable subject (Keycloak `sub`) recorded at mint "
        "time. Anonymous callers have no identity to list — 401. Geometry-free "
        "records; resolve a geoid for the feature itself."
    ),
)
async def list_my_geoids(
    principal: Principal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[PlaceRecord]:
    if principal.is_anonymous:
        # 401 (not 403): same anonymous split as grants._require_manageable.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    rows = await place_repo.list_by_creator(session, principal.subject, limit=limit, offset=offset)
    return [PlaceRecord.from_row(row, base_url=settings.base_url_clean) for row in rows]


@router.get(
    "/{geoid}",
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
    # Deliberately ungated (2026-07-03): the geoid is the capability —
    # public_read hides discovery (external-id lookup, dedup-409 incumbent),
    # never exact-geoid resolution.
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
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FeatureModel | WKTResponse:
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    # Feature-level mask (same 404 as an unknown external_id), checked before the
    # place fetch. Public collections skip the grant lookup entirely.
    if (
        not collection.public_read
        and not principal.is_admin
        and not await _can_read_collection(
            session,
            principal,
            collection_id=collection.id,
            collection_slug=collection.slug,
            public_read=collection.public_read,
        )
    ):
        raise PlaceNotFoundError(f"{collection_id}/{external_id}")
    row = await place_repo.get_by_external_id(session, collection.id, external_id)
    if row is None:
        raise PlaceNotFoundError(f"{collection_id}/{external_id}")
    feature = ogc_service.build_feature(settings, row)
    return feature_response(feature, fmt)
