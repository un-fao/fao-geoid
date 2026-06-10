"""OGC API Features response assembly (pure shaping; no DB access).

Builds landing page, conformance, collection descriptions, and GeoJSON
feature/feature-collection envelopes with HATEOAS links, mirroring DynaStore.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, TypedDict

from geoid.config import Settings
from geoid.domain.identifiers import did_for, uri_for
from geoid.schemas.ogc import (
    CollectionDesc,
    ConformanceDeclaration,
    Extent,
    FeatureCollectionModel,
    FeatureModel,
    LandingPage,
    Link,
    SpatialExtent,
)

CONFORMANCE_CLASSES = [
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/core",
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/oas30",
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/geojson",
    "http://www.opengis.net/spec/ogcapi-features-3/1.0/conf/queryables",
    "http://www.opengis.net/spec/ogcapi-features-3/1.0/conf/filter",
    "http://www.opengis.net/spec/ogcapi-features-3/1.0/conf/features-filter",
    "http://www.opengis.net/spec/cql2/1.0/conf/cql2-text",
    "http://www.opengis.net/spec/cql2/1.0/conf/cql2-json",
]

_GEOJSON = "application/geo+json"
_JSON = "application/json"
_SCHEMA_JSON = "application/schema+json"
_QUERYABLES_REL = "http://www.opengis.net/def/rel/ogc/1.0/queryables"

# JSON-Schema fragments for the CQL2 queryables. Must mirror
# ``place_repo.queryable_field_mapping()`` exactly — the queryables document is
# a closed set (additionalProperties: false) and the filter path rejects names
# outside the mapping, so the two are pinned equal by a unit test.
_QUERYABLE_SCHEMAS: dict[str, dict[str, Any]] = {
    "geoid": {"type": "string", "format": "uuid", "title": "geoid (UUIDv7)"},
    "external_id": {"type": "string", "title": "Caller-supplied external id"},
    "data_quality_status": {"type": "string", "title": "Data quality status"},
    "created_at": {"type": "string", "format": "date-time", "title": "Creation time"},
    "geometry": {"format": "geometry-any", "title": "Place geometry (Polygon/MultiPolygon)"},
}


class ItemRow(TypedDict, total=False):
    """A place read row. ``total=False``: the list path omits ``collection_slug``
    (injected per page), the single-item path selects it directly."""

    geoid: uuid.UUID
    geometry: str | None
    external_id: str | None
    provenance: dict[str, Any]
    data_quality_status: str
    created_at: datetime
    predecessor_id: uuid.UUID | None
    originating_instance: str | None
    collection_slug: str


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def landing_page(settings: Settings) -> LandingPage:
    base = settings.base_url_clean
    return LandingPage(
        title="GeoID — OGC API Features",
        description=(
            "A federated registry of immutable, resolvable geospatial place identifiers "
            "(geoids), served as OGC API Features."
        ),
        links=[
            Link(href=f"{base}/", rel="self", type=_JSON, title="This landing page"),
            Link(href=f"{base}/conformance", rel="conformance", type=_JSON,
                 title="Conformance classes"),
            Link(href=f"{base}/collections", rel="data", type=_JSON, title="Collections"),
            Link(href=f"{base}/docs", rel="service-doc", type="text/html",
                 title="API documentation (Swagger)"),
            Link(href=f"{base}/openapi.json", rel="service-desc", type=_JSON,
                 title="OpenAPI definition"),
        ],
    )


def conformance() -> ConformanceDeclaration:
    return ConformanceDeclaration(conformsTo=list(CONFORMANCE_CLASSES))


def collection_desc(
    settings: Settings,
    *,
    slug: str,
    title: str | None,
    description: str | None = None,
    extent_bbox: tuple[float, float, float, float] | None = None,
) -> CollectionDesc:
    base = settings.base_url_clean
    extent = Extent()  # world default
    if extent_bbox is not None:
        extent = Extent(spatial=SpatialExtent(bbox=[list(extent_bbox)]))
    return CollectionDesc(
        id=slug,
        title=title or slug,
        description=description,
        extent=extent,
        links=[
            Link(href=f"{base}/collections/{slug}", rel="self", type=_JSON),
            Link(href=f"{base}/collections/{slug}/items", rel="items", type=_GEOJSON,
                 title="Features"),
            Link(href=f"{base}/collections/{slug}/queryables", rel=_QUERYABLES_REL,
                 type=_SCHEMA_JSON, title="Queryables (CQL2 filterable fields)"),
            Link(href=f"{base}/collections", rel="parent", type=_JSON),
        ],
    )


def queryables(
    settings: Settings, *, collection_id: str, queryable_names: set[str]
) -> dict[str, Any]:
    """The OGC Part-3 queryables resource: a JSON Schema over the CQL2 fields.

    ``queryable_names`` comes from ``place_repo.queryable_field_mapping()`` — the
    advertised set and the working set are identical (closed schema, unknown
    names 400), which a unit test enforces.
    """
    properties = {
        name: dict(schema)
        for name, schema in _QUERYABLE_SCHEMAS.items()
        if name in queryable_names
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{settings.base_url_clean}/collections/{collection_id}/queryables",
        "type": "object",
        "title": f"Queryables for collection '{collection_id}'",
        "properties": properties,
        "additionalProperties": False,
    }


def _feature_links(settings: Settings, *, geoid: uuid.UUID, collection: str,
                   predecessor_id: uuid.UUID | None) -> list[Link]:
    base = settings.base_url_clean
    links = [
        Link(href=f"{base}/collections/{collection}/items/{geoid}", rel="self", type=_GEOJSON),
        Link(href=f"{base}/geoid/{geoid}", rel="alternate", type=_GEOJSON,
             title="Durable geoid resolver"),
        Link(href=f"{base}/collections/{collection}", rel="collection", type=_JSON),
    ]
    if predecessor_id is not None:
        links.append(
            Link(
                href=f"{base}/geoid/{predecessor_id}",
                rel="predecessor-version",
                type=_GEOJSON,
                title="Superseded geoid (STAC version extension)",
            )
        )
    return links


def _with_collection_slug(row: ItemRow, collection: str) -> ItemRow:
    """Inject ``collection_slug`` into a list-path row (returns a new dict); the
    assert guards the split read-row contract documented on ``ItemRow``."""
    assert "collection_slug" not in row, "list rows must not already carry collection_slug"
    return {**row, "collection_slug": collection}


def build_feature(settings: Settings, row: ItemRow) -> FeatureModel:
    """Assemble an OGC feature from a place read row."""
    geoid: uuid.UUID = row["geoid"]
    geometry = json.loads(row["geometry"]) if row.get("geometry") else None

    provenance = dict(row.get("provenance") or {})
    submitted = dict(provenance.pop("submitted_properties", {}) or {})

    created_at = row.get("created_at")
    created_iso = created_at.isoformat() if isinstance(created_at, datetime) else created_at

    properties: dict[str, Any] = {
        **submitted,
        "geoid": str(geoid),
        "did": did_for(geoid, settings.did_host or ""),
        "uri": uri_for(geoid, settings.base_url_clean),
        "external_id": row.get("external_id"),
        "data_quality_status": row.get("data_quality_status"),
        "created_at": created_iso,
        "originating_instance": row.get("originating_instance"),
        "_geoid_provenance": provenance,
    }
    if row.get("predecessor_id") is not None:
        properties["predecessor_geoid"] = str(row["predecessor_id"])

    return FeatureModel(
        id=str(geoid),
        geometry=geometry,
        properties=properties,
        links=_feature_links(
            settings,
            geoid=geoid,
            collection=row["collection_slug"],
            predecessor_id=row.get("predecessor_id"),
        ),
    )


def build_feature_collection(
    settings: Settings,
    *,
    rows: list[ItemRow],
    collection: str,
    number_matched: int,
    limit: int,
    offset: int,
    query_suffix: str,
) -> FeatureCollectionModel:
    """Assemble a feature collection with self/next/prev paging links."""
    base = settings.base_url_clean
    items_url = f"{base}/collections/{collection}/items"
    features = [build_feature(settings, _with_collection_slug(r, collection)) for r in rows]

    links = [
        Link(href=f"{items_url}?{_paging_qs(limit, offset, query_suffix)}", rel="self",
             type=_GEOJSON),
        Link(href=f"{base}/collections/{collection}", rel="collection", type=_JSON),
    ]
    # Never advertise a link the server's own offset cap would 400 — the walk
    # simply ends at the cap (this also makes GEOID_MAX_OFFSET=0 a coherent
    # "no deep paging" switch instead of bricking every next link).
    if offset + limit < number_matched and offset + limit <= settings.max_offset:
        links.append(
            Link(
                href=f"{items_url}?{_paging_qs(limit, offset + limit, query_suffix)}",
                rel="next",
                type=_GEOJSON,
            )
        )
    if offset > 0:
        prev_offset = max(0, offset - limit)
        links.append(
            Link(
                href=f"{items_url}?{_paging_qs(limit, prev_offset, query_suffix)}",
                rel="prev",
                type=_GEOJSON,
            )
        )

    return FeatureCollectionModel(
        features=features,
        links=links,
        timeStamp=now_iso(),
        numberMatched=number_matched,
        numberReturned=len(features),
    )


def _paging_qs(limit: int, offset: int, query_suffix: str) -> str:
    qs = f"limit={limit}&offset={offset}"
    return f"{qs}&{query_suffix}" if query_suffix else qs
