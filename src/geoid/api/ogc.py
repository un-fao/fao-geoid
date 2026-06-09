"""OGC API Features read router — landing, conformance, collections, items.

Core + GeoJSON + CQL2 (Part 3 filtering) + bbox + offset paging + HATEOAS, with
DynaStore-shaped envelopes (``numberMatched`` / ``numberReturned`` / ``timeStamp``).
"""

from __future__ import annotations

import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.api.cql import build_cql_clause, parse_bbox
from geoid.api.responses import GeoJSONResponse
from geoid.config import Settings, get_settings
from geoid.db import get_session
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
    summary="Features (bbox + CQL2 + paging)",
)
async def get_items(
    collection_id: str,
    request: Request,
    bbox: str | None = Query(default=None, description="minx,miny,maxx,maxy in CRS84"),
    cql_filter: str | None = Query(
        default=None, alias="filter", description="CQL2 filter expression"
    ),
    filter_lang: str | None = Query(
        default=None, alias="filter-lang", description="cql2-text (default) or cql2-json"
    ),
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FeatureCollectionModel:
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)

    effective_limit = min(limit or settings.default_limit, settings.max_limit)
    bbox_tuple = parse_bbox(bbox)
    cql_clause = build_cql_clause(cql_filter, filter_lang, place_repo.queryable_field_mapping())

    rows, number_matched = await place_repo.list_items(
        session,
        collection.id,
        bbox=bbox_tuple,
        cql_clause=cql_clause,
        limit=effective_limit,
        offset=offset,
    )

    preserved = [
        (k, v) for k, v in request.query_params.multi_items() if k not in ("limit", "offset")
    ]
    return ogc_service.build_feature_collection(
        settings,
        rows=rows,
        collection=collection_id,
        number_matched=number_matched,
        limit=effective_limit,
        offset=offset,
        query_suffix=urlencode(preserved),
    )


@router.get(
    "/collections/{collection_id}/items/{geoid}",
    response_model=FeatureModel,
    response_class=GeoJSONResponse,
    summary="A single feature by geoid",
)
async def get_item(
    collection_id: str,
    geoid: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FeatureModel:
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    row = await place_repo.get_by_geoid(session, geoid)
    if row is None or row["collection_slug"] != collection_id:
        raise PlaceNotFoundError(f"{collection_id}/{geoid}")
    return ogc_service.build_feature(settings, row)
