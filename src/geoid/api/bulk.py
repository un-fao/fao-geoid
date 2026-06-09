"""DPG down-payment — one public GeoJSON bulk-export route (1.3 seam).

Streams a collection as a GeoJSON FeatureCollection so the open base data is
copyable with a single GET (a Digital Public Good requirement). Full permissioned
bulk download + place-set URIs come in milestone 1.3.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.db import get_session
from geoid.repositories import collection_repo, place_repo
from geoid.services.exceptions import CollectionNotFoundError

router = APIRouter(tags=["bulk"])


def _export_feature(row: dict) -> dict:
    provenance = dict(row.get("provenance") or {})
    submitted = dict(provenance.pop("submitted_properties", {}) or {})
    created_at = row.get("created_at")
    created_iso = created_at.isoformat() if isinstance(created_at, datetime) else created_at
    return {
        "type": "Feature",
        "id": str(row["geoid"]),
        "geometry": json.loads(row["geometry"]) if row.get("geometry") else None,
        "properties": {
            **submitted,
            "geoid": str(row["geoid"]),
            "external_id": row.get("external_id"),
            "data_quality_status": row.get("data_quality_status"),
            "created_at": created_iso,
        },
    }


async def _stream_feature_collection(
    session: AsyncSession, collection_id: uuid.UUID
) -> AsyncIterator[bytes]:
    yield b'{"type":"FeatureCollection","features":['
    first = True
    async for row in place_repo.iter_collection_geojson(session, collection_id):
        chunk = json.dumps(_export_feature(row), separators=(",", ":"))
        if first:
            first = False
        else:
            yield b","
        yield chunk.encode("utf-8")
    yield b"]}"


@router.get(
    "/collections/{collection_id}/bulk",
    summary="Bulk export a collection as GeoJSON (public, DPG)",
)
async def bulk_export(
    collection_id: str, session: AsyncSession = Depends(get_session)
) -> StreamingResponse:
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    return StreamingResponse(
        _stream_feature_collection(session, collection.id),
        media_type="application/geo+json",
        headers={
            # Use the validated stored slug (not the raw path param) so header
            # safety does not depend on an invariant maintained elsewhere.
            "Content-Disposition": f'attachment; filename="{collection.slug}.geojson"',
        },
    )
