"""OGC API Features read router — landing, conformance, collections, items.

Core + GeoJSON + CQL2 (Part 3 filtering) + offset paging + HATEOAS, with
DynaStore-shaped envelopes (``numberMatched`` / ``numberReturned`` / ``timeStamp``).
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.exc import IntegrityError, OperationalError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.api.cql import build_cql_clause
from geoid.api.format_param import output_format
from geoid.api.paging import enforce_max_offset
from geoid.api.responses import (
    GeoJSONResponse,
    SchemaJSONResponse,
    WKTResponse,
    feature_response,
)
from geoid.config import Settings, get_settings
from geoid.db import get_session
from geoid.domain.geometry_format import GeometryFormat
from geoid.repositories import collection_repo, place_repo
from geoid.schemas.ogc import (
    CollectionDesc,
    CollectionsResponse,
    ConformanceDeclaration,
    FeatureCollectionModel,
    FeatureModel,
    LandingPage,
    Link,
)
from geoid.services import ogc_service
from geoid.services.exceptions import CollectionNotFoundError, PlaceNotFoundError

router = APIRouter(tags=["ogc"])


@router.get("/", response_model=LandingPage, summary="OGC API Features landing page")
async def landing(settings: Settings = Depends(get_settings)) -> LandingPage:
    return ogc_service.landing_page(settings)


@router.get("/conformance", response_model=ConformanceDeclaration)
async def conformance() -> ConformanceDeclaration:
    return ogc_service.conformance()


@router.get("/collections", response_model=CollectionsResponse)
async def list_collections(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> CollectionsResponse:
    collections = await collection_repo.list_all(session)
    base = settings.base_url_clean
    # One batched ST_Extent query for all collections (avoids the N+1 full scans).
    extents = await place_repo.collection_extents(session, [c.id for c in collections])
    descs = [
        ogc_service.collection_desc(
            settings, slug=c.slug, title=c.title, extent_bbox=extents.get(c.id)
        )
        for c in collections
    ]
    return CollectionsResponse(
        links=[
            Link(href=f"{base}/collections", rel="self", type="application/json"),
            Link(href=f"{base}/", rel="parent", type="application/json"),
        ],
        collections=descs,
    )


@router.get("/collections/{collection_id}", response_model=CollectionDesc)
async def describe_collection(
    collection_id: str,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> CollectionDesc:
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    extent = await place_repo.collection_extent(session, collection.id)
    return ogc_service.collection_desc(
        settings, slug=collection.slug, title=collection.title, extent_bbox=extent
    )


@router.get(
    "/collections/{collection_id}/items",
    response_model=FeatureCollectionModel,
    response_class=GeoJSONResponse,
    summary="Features (CQL2 + paging)",
    responses={200: {"content": {"text/plain": {}}}},
)
async def get_items(
    collection_id: str,
    request: Request,
    cql_filter: str | None = Query(
        default=None, alias="filter", description="CQL2 filter expression"
    ),
    filter_lang: str | None = Query(
        default=None, alias="filter-lang", description="cql2-text (default) or cql2-json"
    ),
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    fmt: GeometryFormat = Depends(output_format),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FeatureCollectionModel | WKTResponse:
    # Pure parameter validation precedes any I/O (and matches manage.py's order,
    # so cap-vs-404 precedence is identical on both surfaces). An unknown ?format=/?f=
    # already 400'd in the output_format dependency, before this body runs.
    enforce_max_offset(offset, settings)
    effective_limit = min(limit or settings.default_limit, settings.max_limit)
    cql_clause = build_cql_clause(cql_filter, filter_lang, place_repo.queryable_field_mapping())

    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)

    try:
        rows, number_matched = await place_repo.list_items(
            session,
            collection.id,
            cql_clause=cql_clause,
            limit=effective_limit,
            offset=offset,
        )
    except StatementError as exc:
        # A type-mismatched filter literal (geoid='not-a-uuid', created_at>'x')
        # passes parse/translate and only fails at bind/execute time — that is
        # still user input, so map it to 400. Integrity and operational errors
        # (timeouts, disconnects) keep their existing handling.
        if cql_clause is None or isinstance(exc, (IntegrityError, OperationalError)):
            raise
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"invalid CQL2 filter value: {exc.orig or exc}",
        ) from exc

    preserved = [
        (k, v)
        for k, v in request.query_params.multi_items()
        if k not in ("limit", "offset", "f", "format")
    ]
    fc = ogc_service.build_feature_collection(
        settings,
        rows=rows,
        collection=collection_id,
        number_matched=number_matched,
        limit=effective_limit,
        offset=offset,
        query_suffix=urlencode(preserved),
    )
    if fmt is GeometryFormat.WKT:
        # Bare text/plain: paging + the GeoJSON alternate move to the Link header
        # since the WKT body can't carry HATEOAS links.
        return WKTResponse(
            ogc_service.collection_to_wkt(fc),
            headers={"Link": ogc_service.links_to_header(fc.links)},
        )
    return fc


# Registered BEFORE the /items/{geoid} route below, or the literal segment
# "queryables" would be captured as a geoid. The canonical resource is
# /collections/{id}/queryables (OGC Part-3 Queryables requirement class); the
# /items/queryables spelling is kept as a hidden alias for discoverability.
@router.get(
    "/collections/{collection_id}/queryables",
    response_class=SchemaJSONResponse,
    summary="Queryables — the fields usable in CQL2 filters (OGC Part 3)",
    # Phase-1 demo: hidden from the API definition (Processes-only surface).
    # The route stays live and serving; re-enable = drop include_in_schema.
    include_in_schema=False,
)
@router.get("/collections/{collection_id}/items/queryables", include_in_schema=False)
async def get_queryables(
    collection_id: str,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Any:
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    doc = ogc_service.queryables(
        settings,
        collection_id=collection_id,
        queryable_names=set(place_repo.queryable_field_mapping()),
    )
    return SchemaJSONResponse(doc)


@router.get(
    "/collections/{collection_id}/items/{geoid}",
    response_model=FeatureModel,
    response_class=GeoJSONResponse,
    summary="A single feature by geoid",
    responses={200: {"content": {"text/plain": {}}}},
)
async def get_item(
    collection_id: str,
    geoid: uuid.UUID,
    fmt: GeometryFormat = Depends(output_format),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FeatureModel | WKTResponse:
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    row = await place_repo.get_by_geoid(session, geoid)
    if row is None or row["collection_slug"] != collection_id:
        raise PlaceNotFoundError(f"{collection_id}/{geoid}")
    feature = ogc_service.build_feature(settings, row)
    return feature_response(feature, fmt)
