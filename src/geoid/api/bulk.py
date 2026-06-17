"""DPG down-payment — one public GeoJSON bulk-export route (1.3 seam).

Streams a collection as GeoJSON so the open base data is copyable with a single
GET (a Digital Public Good requirement). Two output shapes are content-negotiated
on ``Accept``: a single ``application/geo+json`` FeatureCollection (default), or an
``application/geo+json-seq`` GeoJSON Text Sequence (RFC 8142 — one RS-delimited
Feature per record, friendlier to line-oriented stream consumers).

Session lifecycle: the streaming generator opens and owns its OWN session via
``get_sessionmaker()`` rather than the request-scoped ``get_session`` dependency.
``get_session`` commits and closes when the handler returns — i.e. *before*
StreamingResponse iterates the cursor — so a server-side cursor read inside the
body would run on a closed session. The collection 404 is resolved up front in a
short-lived session so the client still gets a real 404 (not a 200 with an empty
stream).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.db import get_session, get_sessionmaker
from geoid.domain.geometry_format import GeometryFormat, encode_geometry, negotiate_format
from geoid.repositories import collection_repo, place_repo
from geoid.services.exceptions import CollectionNotFoundError
from geoid.services.ogc_service import export_feature

router = APIRouter(tags=["bulk"])

_GEOJSON = "application/geo+json"
_GEOJSON_SEQ = "application/geo+json-seq"
_WKT = "text/plain"
# RFC 8142: each JSON text is preceded by RS (0x1e) and followed by LF.
_RS = b"\x1e"
_LF = b"\n"


async def _stream_feature_collection(collection_id: uuid.UUID) -> AsyncIterator[bytes]:
    """A single GeoJSON FeatureCollection, streamed over an owned session."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        yield b'{"type":"FeatureCollection","features":['
        first = True
        async for row in place_repo.iter_collection_geojson(session, collection_id):
            chunk = json.dumps(export_feature(row), separators=(",", ":"))
            if first:
                first = False
            else:
                yield b","
            yield chunk.encode("utf-8")
        yield b"]}"


async def _stream_geojson_seq(collection_id: uuid.UUID) -> AsyncIterator[bytes]:
    """A GeoJSON Text Sequence (RFC 8142): RS + compact Feature + LF per record."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async for row in place_repo.iter_collection_geojson(session, collection_id):
            chunk = json.dumps(export_feature(row), separators=(",", ":")).encode("utf-8")
            yield _RS + chunk + _LF


async def _stream_wkt(collection_id: uuid.UUID) -> AsyncIterator[bytes]:
    """Newline-delimited WKT, one geometry per row (null geometry skipped).

    WKT is a vendor-extension encoding; geometry is the open base data, so no
    HATEOAS/properties — just one bare WKT line per place.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async for row in place_repo.iter_collection_geojson(session, collection_id):
            geojson = row.get("geometry")
            if not geojson:
                continue
            wkt = encode_geometry(json.loads(geojson), GeometryFormat.WKT)
            yield wkt.encode("utf-8") + _LF


def _wants_seq(request: Request) -> bool:
    return _GEOJSON_SEQ in request.headers.get("accept", "").lower()


@router.get(
    "/collections/{collection_id}/bulk",
    summary="Bulk export a collection as GeoJSON (public, DPG)",
)
async def bulk_export(
    collection_id: str,
    request: Request,
    f: str | None = Query(
        default=None, description="Output format: geojson (default) or wkt (vendor extension)"
    ),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    # ?f= overrides Accept; an unknown ?f= 400s before any I/O.
    try:
        fmt = negotiate_format(f, request.headers.get("accept"))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    # Resolve the 404 up front (short-lived dependency session); the streaming
    # generators below open their own sessions so the cursor outlives this one.
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)

    if fmt is GeometryFormat.WKT:
        return StreamingResponse(
            _stream_wkt(collection.id),
            media_type=_WKT,
            headers={
                "Content-Disposition": f'attachment; filename="{collection.slug}.wkt"',
            },
        )

    if _wants_seq(request):
        return StreamingResponse(
            _stream_geojson_seq(collection.id),
            media_type=_GEOJSON_SEQ,
            headers={
                "Content-Disposition": f'attachment; filename="{collection.slug}.geojsons"',
            },
        )
    return StreamingResponse(
        _stream_feature_collection(collection.id),
        media_type=_GEOJSON,
        headers={
            # Use the validated stored slug (not the raw path param) so header
            # safety does not depend on an invariant maintained elsewhere.
            "Content-Disposition": f'attachment; filename="{collection.slug}.geojson"',
        },
    )
