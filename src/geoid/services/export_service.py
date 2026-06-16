"""Bulk-export service — stream a collection into one object + a download reference.

The async counterpart of the public ``GET /collections/{id}/bulk`` stream: where
``/bulk`` streams a collection inline over the request, the **bulk-export process**
writes a complete GeoJSON ``FeatureCollection`` to object storage and returns a
time-limited download reference (a GCS V4 signed URL, ≤7-day; a stable file URI on
the local backend). It is async-only because Cloud Run caps a request at 60 minutes —
a large or permissioned export must not stream inline (504 on overrun).

Nothing about feature shaping is reinvented: each row is rendered by
``ogc_service.export_feature`` (the same compact Feature the ``/bulk`` stream emits),
so the inline and by-reference exports are byte-identical. GeoParquet (1.1.0 /
OGC 24-013) and FlatGeobuf are a later opt-in format on this same path (lazy
GDAL/pyogrio in the worker); GeoJSON ships now.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from geoid.config import Settings
from geoid.domain.identifiers import new_geoid
from geoid.models import Collection, IngestJob
from geoid.repositories import collection_repo, job_repo, place_repo
from geoid.schemas.ingest import (
    ExportFormat,
    ExportInputs,
    ExportResult,
    ProcessDescription,
    ProcessSummary,
    Subscriber,
)
from geoid.schemas.ogc import Link
from geoid.services.ogc_service import export_feature
from geoid.storage import get_blob_store

logger = logging.getLogger("geoid.export")

BULK_EXPORT_PROCESS_ID = "bulk-export"
_PROCESS_VERSION = "1.0.0"
_GEOJSON = "application/geo+json"


# --- OGC process metadata (pure shaping) ------------------------------------


def export_summary(settings: Settings) -> ProcessSummary:
    base = settings.base_url_clean
    return ProcessSummary(
        id=BULK_EXPORT_PROCESS_ID,
        version=_PROCESS_VERSION,
        title="Bulk export a collection (GeoJSON)",
        description=(
            "Write a whole collection to one GeoJSON object and return a "
            "time-limited download reference (a GCS V4 signed URL, ≤7-day). "
            "Async-only: a large export must not stream inline under Cloud Run's "
            "60-minute request cap."
        ),
        # Export is async-only and its output is a download reference, not an
        # inline value — so it advertises only async-execute + reference transmission.
        jobControlOptions=["async-execute"],
        outputTransmission=["reference"],
        links=[
            Link(
                href=f"{base}/processes/{BULK_EXPORT_PROCESS_ID}",
                rel="self",
                type="application/json",
                title="Process description",
            ),
            Link(
                href=f"{base}/processes/{BULK_EXPORT_PROCESS_ID}/execution",
                rel="http://www.opengis.net/def/rel/ogc/1.0/execute",
                type="application/json",
                title="Execute",
            ),
        ],
    )


def export_description(settings: Settings, process_id: str) -> ProcessDescription | None:
    if process_id != BULK_EXPORT_PROCESS_ID:
        return None
    summary = export_summary(settings)
    return ProcessDescription(
        **summary.model_dump(),
        inputs={
            "collection": {
                "title": "Source collection slug",
                "schema": {"type": "string"},
            },
            "format": {
                "title": "Output format",
                "minOccurs": 0,
                "description": (
                    "GeoJSON ships now; 'geoparquet' (1.1.0 / OGC 24-013) and "
                    "'flatgeobuf' are a deferred opt-in on this same path."
                ),
                "schema": {
                    "type": "string",
                    "enum": ["geojson", "geoparquet", "flatgeobuf"],
                    "default": "geojson",
                },
            },
        },
        outputs={
            "download": {
                "title": "Download reference",
                "schema": {"type": "object"},
            }
        },
    )


# --- Input parsing ----------------------------------------------------------


def parse_export_inputs(inputs: dict[str, Any]) -> ExportInputs:
    """Validate the OGC ``inputs`` bag into the bulk-export contract (422 on miss)."""
    return ExportInputs.model_validate(inputs)


# --- Enqueue (the execute handler durably persists, then triggers the worker) --


async def enqueue_export(
    session: AsyncSession,
    *,
    collection: Collection,
    inputs: ExportInputs,
    idempotency_key: str | None,
    subscriber: Subscriber | None,
) -> tuple[IngestJob, bool]:
    """Durably enqueue an async export. Returns ``(job, created)``.

    The requested format + collection slug ride in ``payload`` (there is no input
    blob to stage — the source is the live collection). The caller commits, then
    best-effort triggers the worker, exactly like the async-ingest path.
    """
    job_id = new_geoid()
    return await job_repo.create_job(
        session,
        job_id=job_id,
        collection_id=collection.id,
        process_id=BULK_EXPORT_PROCESS_ID,
        mode="async",
        blob_uri=None,
        payload={"format": inputs.format, "collection": collection.slug},
        idempotency_key=idempotency_key,
        notify_email=None,
        subscriber=subscriber.model_dump() if subscriber else None,
    )


# --- Worker: render → store → sign → mark successful ------------------------


async def run_export(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, job_id: uuid.UUID
) -> None:
    """Process one claimed export job: render the collection, store it, sign a URL.

    Self-handles its terminal state (marks the job ``failed`` on any error and never
    raises) so the worker drain is not aborted by one bad job — mirroring the ingest
    worker. The completion notice (callback) is sent by the caller's ``finally``.
    """
    async with sessionmaker() as session:
        job = await job_repo.get_job(session, job_id)
        if job is None or job.status != "running":
            return  # dismissed or already terminal — nothing to do
        collection = await collection_repo.get_by_id(session, job.collection_id)
        payload = job.payload or {}

    if collection is None:
        await job_repo.fail_job(sessionmaker, job_id, "collection no longer exists")
        return
    try:
        result = await _render_and_store(sessionmaker, settings, collection, job_id, payload)
    except Exception as exc:  # noqa: BLE001 — terminal-mark the job, never crash the drain
        logger.exception("export job %s failed", job_id)
        await job_repo.fail_job(sessionmaker, job_id, str(exc))
        return

    async with sessionmaker() as session:
        await job_repo.mark_successful(session, job_id, result.model_dump())
        await session.commit()


async def _render_and_store(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    collection: Collection,
    job_id: uuid.UUID,
    payload: dict[str, Any],
) -> ExportResult:
    fmt: ExportFormat = payload.get("format", "geojson")
    if fmt != "geojson":
        raise ValueError(
            f"export format {fmt!r} is not yet supported; GeoParquet (1.1.0 / "
            "OGC 24-013) and FlatGeobuf are a deferred opt-in on this path"
        )

    body, count = await _render_geojson(sessionmaker, collection.id)
    key = f"exports/{job_id}.geojson"
    store = get_blob_store()
    await store.put(key, body, content_type=_GEOJSON)
    href = await store.signed_url(key, expires_seconds=settings.export_signed_url_ttl_seconds)
    # Only a GCS signed URL actually expires; the local backend returns a stable file URI.
    expires = settings.export_signed_url_ttl_seconds if settings.storage_backend == "gcs" else None
    return ExportResult(
        href=href,
        type=_GEOJSON,
        format="geojson",
        collection=collection.slug,
        count=count,
        expires_seconds=expires,
    )


async def _render_geojson(
    sessionmaker: async_sessionmaker[AsyncSession], collection_id: uuid.UUID
) -> tuple[bytes, int]:
    """Render the whole collection as one compact GeoJSON FeatureCollection.

    The DB read is streamed (server-side cursor in ``iter_collection_geojson``); the
    output object is assembled in memory because ``BlobStore.put`` takes bytes. For
    the 1.3 corpus sizes that is well within a worker container; a resumable/streamed
    upload is the next step if a collection ever outgrows memory.
    """
    parts: list[str] = ['{"type":"FeatureCollection","features":[']
    count = 0
    async with sessionmaker() as session:
        async for row in place_repo.iter_collection_geojson(session, collection_id):
            feature = json.dumps(export_feature(row), separators=(",", ":"))
            parts.append(feature if count == 0 else f",{feature}")
            count += 1
    parts.append("]}")
    return "".join(parts).encode("utf-8"), count
