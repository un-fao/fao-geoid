"""Custom responses.

OGC API Features serves GeoJSON features with the ``application/geo+json`` media
type (not ``application/json``); some clients content-negotiate on it. We subclass
``JSONResponse`` only to set that media type — FastAPI already serializes the
``response_model`` efficiently, so no faster JSON library is needed here.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse, PlainTextResponse

from geoid.domain.geometry_format import GeometryFormat
from geoid.schemas.ogc import FeatureModel
from geoid.services import ogc_service


class GeoJSONResponse(JSONResponse):
    media_type = "application/geo+json"


class WKTResponse(PlainTextResponse):
    """Bare WKT geometry as ``text/plain`` (its own honest media type — WKT is not
    smuggled into a JSON envelope). Used for the single-feature resolver WKT path."""

    media_type = "text/plain"


def feature_response(feature: FeatureModel, fmt: GeometryFormat) -> FeatureModel | WKTResponse:
    """Render a single feature in the negotiated format (shared by all item routes).

    GeoJSON returns the model unchanged (FastAPI serialises it via the route's
    ``GeoJSONResponse``); WKT returns a bare ``text/plain`` body with the reciprocal
    GeoJSON alternate carried in the ``Link`` header.
    """
    if fmt is GeometryFormat.WKT:
        return WKTResponse(
            ogc_service.feature_to_wkt(feature),
            headers={"Link": ogc_service.feature_geojson_alternate_header(feature)},
        )
    return feature
