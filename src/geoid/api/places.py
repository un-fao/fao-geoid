"""Write / registry router — the product surface.

POST a polygon → ``{geoid, uri}``; resolve durably by geoid; resolve by
``(external_id, collection)``. Anonymous POSTs are allowed into ``public_write``
collections via the shared registry service (no special code path).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.api.format_param import output_format
from geoid.api.responses import GeoJSONResponse, WKTResponse, feature_response, resolver_cache
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


def _created_by(row: dict) -> str | None:
    """The mint-time creator ``sub`` recorded in provenance (None for anonymous mints)."""
    return (row.get("provenance") or {}).get("created_by")


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
                "geoid/uri/collection when the incumbent's collection is "
                "public_read or the caller is a member of it (sysadmin / own "
                "mint / any grant); otherwise those fields are null. An "
                "external_id duplicate also answers 409 — discriminate on "
                "``constraint``."
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
    # whole request (413) before any insert. Auth (anon → public_write) is checked
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
    response: Response,
    fmt: GeometryFormat = Depends(output_format),
    if_none_match: str | None = Header(default=None, include_in_schema=False),
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FeatureModel | WKTResponse | Response:
    # Existence stays un-gated for any geoid holder (the geoid is the capability;
    # no 404 mask), but the BODY is caller-aware since 2026-07-09: full metadata
    # only for sysadmin / creator / members, the geometry-only masked body for
    # everyone else. Taking a principal also makes a present-but-malformed
    # bearer 401 (it used to be ignored).
    row = await place_repo.get_by_geoid(session, geoid)
    if row is None:
        raise PlaceNotFoundError(str(geoid))
    cache = resolver_cache(
        settings=settings, principal=principal, geoid=geoid, fmt=fmt, if_none_match=if_none_match
    )
    if isinstance(cache, Response):
        return cache
    created_by = _created_by(row)
    # Query-avoiding order: sysadmin/creator need no grant; anonymous can hold none.
    full = authz_service.can_see_metadata(principal, created_by, None)
    if not full and not principal.is_anonymous:
        grant = await authz_service.load_caller_grant(
            session, principal, row["collection_id"], row["collection_slug"]
        )
        full = authz_service.can_see_metadata(principal, created_by, grant)
    feature = ogc_service.build_feature(settings, row, full=full)
    # BOTH header merges are required: FastAPI copies the injected response's
    # headers only on the model-return (GeoJSON) path, never onto the returned
    # WKTResponse — that one gets them via feature_response(headers=...).
    response.headers.update(cache)
    return feature_response(feature, fmt, headers=cache)


@router.get(
    "/collections/{collection_id}/external/{external_id}",
    response_model=FeatureModel,
    response_class=GeoJSONResponse,
    summary="Resolve a place by (external_id, collection) — answers like the geoid resolver",
    description=(
        "Behaves exactly like `GET /{geoid}`: an existing (collection, "
        "external_id) answers 200 to every caller — the full feature for "
        "sysadmin / the creator / grant holders, the geometry-only masked body "
        "for everyone else. 404 only for an unknown collection or external_id."
    ),
    responses={200: {"content": {"text/plain": {}}}},
)
async def resolve_by_external_id(
    collection_id: str,
    external_id: str,
    response: Response,
    fmt: GeometryFormat = Depends(output_format),
    if_none_match: str | None = Header(default=None, include_in_schema=False),
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FeatureModel | WKTResponse | Response:
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    row = await place_repo.get_by_external_id(session, collection.id, external_id)
    if row is None:
        raise PlaceNotFoundError(f"{collection_id}/{external_id}")
    cache = resolver_cache(
        settings=settings,
        principal=principal,
        geoid=row["geoid"],
        fmt=fmt,
        if_none_match=if_none_match,
    )
    if isinstance(cache, Response):
        return cache
    # Existence is never masked (client ruling 2026-07-09 round 2 — consistent
    # with the geoid resolver); only the BODY is caller-aware, on the same
    # query-avoiding order as resolve_geoid.
    created_by = _created_by(row)
    full = authz_service.can_see_metadata(principal, created_by, None)
    if not full and not principal.is_anonymous:
        grant = await authz_service.load_caller_grant(
            session, principal, collection.id, collection.slug
        )
        full = authz_service.can_see_metadata(principal, created_by, grant)
    feature = ogc_service.build_feature(settings, row, full=full)
    # Same dual merge as resolve_geoid (injected-response headers reach only the
    # GeoJSON model path; the WKT Response takes them via feature_response).
    response.headers.update(cache)
    return feature_response(feature, fmt, headers=cache)
