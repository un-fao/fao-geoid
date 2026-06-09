"""Place schemas — the create request (a GeoJSON Feature) and the mint response."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from geojson_pydantic import Feature
from geojson_pydantic.geometries import MultiPolygon, Polygon
from pydantic import BaseModel, Field, model_validator

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
    EPSG:4326. ``properties`` is accepted verbatim into provenance/jsonb; the
    recognised ``_whisp`` block is mirrored as client provenance.
    """

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


class MintResponse(BaseModel):
    """Response to a successful POST: the three resolvable forms + dedup status."""

    geoid: str = Field(description="The bare UUIDv7 — the canonical, immutable identifier.")
    did: str = Field(description="did:web form, e.g. did:web:data.fao.org:geoid:<uuid>.")
    uri: str = Field(description="Durable resolver URI, e.g. https://data.fao.org/geoid/<uuid>.")
    item_url: str = Field(description="Collection-scoped OGC API Features item URL.")
    collection: str = Field(description="Collection slug the place was minted into.")
    external_id: str | None = Field(default=None)
    data_quality_status: str = Field(default="unverified")
    deduplicated: bool = Field(
        default=False,
        description="True when an identical geometry existed; the incumbent geoid is returned.",
    )
