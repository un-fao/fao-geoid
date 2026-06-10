"""Custom responses.

OGC API Features serves GeoJSON features with the ``application/geo+json`` media
type (not ``application/json``); some clients content-negotiate on it. We subclass
``JSONResponse`` only to set that media type — FastAPI already serializes the
``response_model`` efficiently, so no faster JSON library is needed here.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse


class GeoJSONResponse(JSONResponse):
    media_type = "application/geo+json"


class SchemaJSONResponse(JSONResponse):
    """OGC Part-3 queryables are a JSON Schema document, served as schema+json."""

    media_type = "application/schema+json"
