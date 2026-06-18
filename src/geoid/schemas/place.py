"""Place schemas — the create request (a GeoJSON Feature) and the mint response."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, Literal

from geojson_pydantic import Feature
from geojson_pydantic.geometries import MultiPolygon, Polygon
from pydantic import BaseModel, Field, model_validator

from geoid.domain.geometry_format import decode_geometry

# Polygon-first: points/lines are rejected at the schema boundary (422).
PolygonalGeometry = Polygon | MultiPolygon

_LON_MIN, _LON_MAX = -180.0, 180.0
_LAT_MIN, _LAT_MAX = -90.0, 90.0


def iter_positions(coordinates: Any) -> Iterator[tuple[float, float]]:
    """Yield every (lon, lat) position in an arbitrarily nested coordinate array.

    Works across Polygon (list[ring]) and MultiPolygon (list[polygon]) nesting;
    a leaf position is a sequence whose first element is a number.
    """
    if isinstance(coordinates, (list, tuple)) and coordinates:
        head = coordinates[0]
        if isinstance(head, (int, float)):
            yield (float(coordinates[0]), float(coordinates[1]))
            return
        for item in coordinates:
            yield from iter_positions(item)


class PlaceCreate(Feature[PolygonalGeometry, dict[str, Any] | None]):
    """Incoming GeoJSON Feature for a place.

    The optional GeoJSON ``id`` member becomes the place's ``external_id``
    (collection-scoped unique). Geometry must be a Polygon/MultiPolygon in
    EPSG:4326, supplied either as a GeoJSON geometry object **or** as a WKT string
    (a vendor extension; both single create and per-feature bulk). ``properties`` is
    accepted verbatim into provenance/jsonb; the recognised ``_whisp`` block is
    mirrored as client provenance.
    """

    @model_validator(mode="before")
    @classmethod
    def _decode_string_geometry(cls, data: Any) -> Any:
        """Accept a WKT-string ``geometry`` member by decoding it to GeoJSON first.

        Runs BEFORE geojson-pydantic parses, so a WKT polygon converges on the exact
        same geometry dict a GeoJSON polygon would — identical geometry text →
        identical ``geoid_geom_hash_default`` → same geoid / 409. A GeoJSON object
        ``geometry`` is left untouched (the default path is byte-for-byte unchanged);
        a malformed WKT string raises ``ValueError`` → ``ValidationError`` → 422.
        """
        if isinstance(data, dict) and isinstance(data.get("geometry"), str):
            return {**data, "geometry": decode_geometry(data["geometry"])}
        return data

    @model_validator(mode="after")
    def _validate_lonlat_bounds(self) -> PlaceCreate:
        if self.geometry is None:
            raise ValueError("geometry is required")
        for lon, lat in iter_positions(self.geometry.coordinates):
            if not (_LON_MIN <= lon <= _LON_MAX) or not (_LAT_MIN <= lat <= _LAT_MAX):
                raise ValueError(
                    f"coordinate out of bounds: lon={lon}, lat={lat} "
                    f"(expected lon∈[{_LON_MIN},{_LON_MAX}], lat∈[{_LAT_MIN},{_LAT_MAX}]); "
                    "GeoJSON is lon,lat — check for a swapped pair"
                )
        return self

    @property
    def external_id(self) -> str | None:
        return None if self.id is None else str(self.id)


def geometry_to_geojson(feature: PlaceCreate) -> str:
    """Serialise a feature's geometry to a GeoJSON geometry string for PostGIS.

    The SINGLE source for both the single-row and the bulk write paths (both reach
    ``place_repo.insert_place`` through ``registry_service``), so they stage
    byte-identical geometry text into ``ST_GeomFromGeoJSON`` — a divergence here
    would break the golden-vector hash parity the two paths share.
    """
    geom = feature.geometry
    return json.dumps({"type": geom.type, "coordinates": geom.coordinates})


class MintResponse(BaseModel):
    """Response to a successful POST (201): the resolvable forms of the NEW geoid.

    A duplicate geometry never reaches this model — it fails with a 409 whose
    body is :class:`GeometryConflictResponse`.
    """

    geoid: str = Field(description="The bare UUIDv7 — the canonical, immutable identifier.")
    uri: str = Field(description="Durable resolver URI, e.g. https://data.fao.org/geoid/<uuid>.")
    item_url: str = Field(description="Collection-scoped OGC API Features item URL.")
    collection: str = Field(description="Collection slug the place was minted into.")
    external_id: str | None = Field(default=None)


class GeometryConflictResponse(BaseModel):
    """409 body when an identical geometry already exists anywhere in the catalog.

    Mirrors the ``_error()`` envelope (``code``/``message`` + extras). The
    ``constraint`` field discriminates this conflict from the external_id 409.
    """

    code: int = Field(description="HTTP status code (409).")
    message: str = Field(description="Human-readable conflict description.")
    geoid: str = Field(description="The INCUMBENT geoid the geometry is already registered under.")
    uri: str = Field(description="Durable resolver URI of the incumbent geoid.")
    collection: str = Field(description="Collection slug the incumbent belongs to.")
    constraint: str = Field(description='Always "uq_geoid_registry_geom_hash" for this conflict.')


# --- Bulk write (synchronous multi-geometry POST) ---------------------------

# Per-feature reject discriminator. Maps 1:1 to the single-row write path's 4xx so
# a bulk reject reads exactly like the response a single POST would return.
BulkRejectReason = Literal[
    "schema_invalid",  # failed the PlaceCreate pydantic schema (single-row 422)
    "invalid_geometry",  # non-polygon / not ST_IsValid / unparseable GeoJSON (422)
    "geometry_conflict",  # identical geometry already registered — 409 + incumbent
    "external_id_conflict",  # duplicate (collection, external_id) — 409
    "geoid_conflict",  # geoid PK collision — 409 (backstop)
]


class BulkFeatureCollection(BaseModel):
    """A GeoJSON FeatureCollection (RFC 7946 §3.3) submitted to the bulk route.

    ``features`` are RAW dicts, NOT ``PlaceCreate`` — each is validated INSIDE the
    per-feature loop (``PlaceCreate.model_validate``) so one malformed feature
    becomes a single rejected row rather than a 422 that sinks the whole request at
    FastAPI's body boundary. The envelope itself is still validated up front: an
    empty ``features`` list or a wrong ``type`` is a 422.
    """

    type: Literal["FeatureCollection"]
    features: list[dict[str, Any]] = Field(min_length=1)


class BulkAccepted(BaseModel):
    """A feature that minted a new geoid (mirrors :class:`MintResponse`)."""

    index: int = Field(description="Zero-based position in the submitted features array.")
    geoid: str
    uri: str
    item_url: str
    external_id: str | None = None


class BulkRejected(BaseModel):
    """A feature that did not mint, with the reason it was skipped.

    Partial success is the contract: one bad feature never aborts the batch. The
    fields mirror the single-row error envelope — for a geometry conflict the
    incumbent ``geoid``/``uri``/``collection`` are carried (the same payload a
    single duplicate POST's 409 returns).
    """

    index: int = Field(description="Zero-based position in the submitted features array.")
    reason: BulkRejectReason
    detail: str | None = Field(default=None, description="Human-readable explanation.")
    geoid: str | None = Field(default=None, description="Incumbent geoid for a geometry conflict.")
    uri: str | None = Field(
        default=None, description="Incumbent resolver URI for a geometry conflict."
    )
    collection: str | None = Field(
        default=None, description="Incumbent collection for a geometry conflict."
    )
    external_id: str | None = None


class BulkSummary(BaseModel):
    received: int = Field(description="Features in the submitted FeatureCollection.")
    accepted: int = Field(description="Features that minted a new geoid.")
    rejected: int = Field(description="Features skipped (see ``rejected`` for the reasons).")


class BulkReport(BaseModel):
    """Outcome of a synchronous bulk POST — always 200, partial success.

    Valid geometries are inserted; bad ones are reported with the same reason the
    single-item endpoint returns. ``accepted`` + ``rejected`` are disjoint and
    together account for every submitted feature (``summary.received``).
    """

    summary: BulkSummary
    accepted: list[BulkAccepted] = Field(default_factory=list)
    rejected: list[BulkRejected] = Field(default_factory=list)
