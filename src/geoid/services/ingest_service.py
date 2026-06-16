"""Bulk-ingest service — validate → mint → stage → set-based arbiter → report.

ONE service drives both execution tiers (only the transaction boundary differs):
the synchronous path runs :func:`run_ingest` in the request transaction; the async
worker (added later) runs the same function per chunk. Nothing about dedup or
identity is reinvented here — it is a set-based orchestration over the existing
single-row machinery (``geoid_registry`` arbiter + ``geoid_geom_hash_default`` +
UUIDv7 minting), so a bulk row mints, dedups, and reports exactly like a single POST.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from geoid.config import Settings
from geoid.deps import Principal
from geoid.domain.identifiers import new_geoid, uri_for
from geoid.domain.provenance import build_provenance, extract_client
from geoid.models import UQ_GEOID_REGISTRY_GEOM_HASH, UQ_PLACE_EXTERNAL_ID, Collection, IngestJob
from geoid.notify import get_notifier
from geoid.notify.callback import post_callback
from geoid.repositories import collection_repo, ingest_repo, job_repo
from geoid.repositories.ingest_repo import StagingRow
from geoid.schemas.ingest import (
    AcceptedItem,
    BulkIngestInputs,
    IngestionReport,
    ProcessDescription,
    ProcessList,
    ProcessSummary,
    RejectedItem,
    StatusInfo,
    Subscriber,
)
from geoid.schemas.ogc import Link
from geoid.schemas.place import PlaceCreate, geometry_to_geojson
from geoid.services import export_service
from geoid.services.exceptions import (
    AnonymousWriteForbiddenError,
    BulkLimitExceededError,
)
from geoid.storage import get_blob_store

logger = logging.getLogger("geoid.ingest")

_RESULTS_REL = "http://www.opengis.net/def/rel/ogc/1.0/results"

BULK_INGEST_PROCESS_ID = "bulk-ingest"
_PROCESS_VERSION = "1.0.0"
_MAX_REASON_PARTS = 3


# --- OGC process metadata (pure shaping) ------------------------------------


def _bulk_ingest_summary(settings: Settings) -> ProcessSummary:
    base = settings.base_url_clean
    return ProcessSummary(
        id=BULK_INGEST_PROCESS_ID,
        version=_PROCESS_VERSION,
        title="Bulk ingest places (GeoJSON)",
        description=(
            "Mint geoids for a GeoJSON FeatureCollection of polygons. Global "
            "geometry dedup, immutable UUIDv7 identity, and per-row partial "
            "success are identical to the single-row write path."
        ),
        links=[
            Link(
                href=f"{base}/processes/{BULK_INGEST_PROCESS_ID}",
                rel="self",
                type="application/json",
                title="Process description",
            ),
            Link(
                href=f"{base}/processes/{BULK_INGEST_PROCESS_ID}/execution",
                rel="http://www.opengis.net/def/rel/ogc/1.0/execute",
                type="application/json",
                title="Execute",
            ),
        ],
    )


def process_list(settings: Settings) -> ProcessList:
    base = settings.base_url_clean
    return ProcessList(
        processes=[
            _bulk_ingest_summary(settings),
            export_service.export_summary(settings),
        ],
        links=[Link(href=f"{base}/processes", rel="self", type="application/json")],
    )


def process_description(settings: Settings, process_id: str) -> ProcessDescription | None:
    """Describe a process by id, or None if this server does not offer it.

    The Processes surface offers two processes; dispatch the description by id so the
    execute handler's existence check (and ``GET /processes/{id}``) cover both.
    """
    if process_id == export_service.BULK_EXPORT_PROCESS_ID:
        return export_service.export_description(settings, process_id)
    if process_id != BULK_INGEST_PROCESS_ID:
        return None
    summary = _bulk_ingest_summary(settings)
    return ProcessDescription(
        **summary.model_dump(),
        inputs={
            "collection": {
                "title": "Target collection slug",
                "schema": {"type": "string"},
            },
            "items": {
                "title": "Features to ingest",
                "description": (
                    "A GeoJSON FeatureCollection by value, OR {'href': '...'} "
                    "referencing a GCS blob (async only)."
                ),
                "schema": {"type": "object"},
            },
            "idempotencyKey": {
                "title": "Idempotency key",
                "minOccurs": 0,
                "schema": {"type": "string"},
            },
        },
        outputs={
            "report": {
                "title": "Ingestion report",
                "schema": {"type": "object"},
            }
        },
    )


def is_known_process(process_id: str) -> bool:
    """Cheap existence check for the execute guard — avoids building (and discarding)
    a full ProcessDescription on every POST just to test it for ``None``."""
    return process_id in {BULK_INGEST_PROCESS_ID, export_service.BULK_EXPORT_PROCESS_ID}


# --- Input parsing ----------------------------------------------------------


def parse_inputs(inputs: dict[str, Any]) -> BulkIngestInputs:
    """Validate the OGC ``inputs`` bag into the bulk-ingest contract (422 on miss)."""
    return BulkIngestInputs.model_validate(inputs)


def _features_array(fc: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``features`` array of a GeoJSON FeatureCollection, or ValueError if absent.

    The single shape check shared by the inline (by-value) and blob (by-reference)
    sources, so the "is this a FeatureCollection?" rule lives in one place.
    """
    features = fc.get("features")
    if not isinstance(features, list):
        raise ValueError("not a GeoJSON FeatureCollection with a 'features' array")
    return features


def _features_by_value(items: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Extract the feature list from an inline FeatureCollection, or None if by-ref."""
    if "href" in items and "features" not in items:
        return None  # by-reference — handled by the async worker only
    return _features_array(items)


def _summarize_validation_error(exc: ValidationError) -> str:
    parts = [
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
        for e in exc.errors()[:_MAX_REASON_PARTS]
    ]
    return "; ".join(parts) or "invalid feature"


def place_set_uri(settings: Settings, batch_id: uuid.UUID) -> str:
    """Resolvable place-set URI for set-membership (``ingest_batch_id``)."""
    return f"{settings.base_url_clean}/place-sets/{batch_id}"


# --- The ingest itself ------------------------------------------------------


def _validate_and_stage(
    features: list[dict[str, Any]],
    *,
    settings: Settings,
    principal: Principal,
    batch_id: uuid.UUID,
    row_offset: int = 0,
) -> tuple[list[StagingRow], list[RejectedItem]]:
    """Per-row PlaceCreate validation; survivors get a UUIDv7 + provenance + staging row."""
    staging: list[StagingRow] = []
    rejects: list[RejectedItem] = []
    for index, feature in enumerate(features):
        row_no = row_offset + index
        try:
            place = PlaceCreate.model_validate(feature)
        except ValidationError as exc:
            rejects.append(
                RejectedItem(
                    row_no=row_no,
                    reason="schema_invalid",
                    detail=_summarize_validation_error(exc),
                )
            )
            continue
        provenance = build_provenance(
            created_by=principal.subject,
            originating_instance=settings.instance_id,
            client=extract_client(place.properties),
            extra={
                "submitted_properties": place.properties or {},
                "ingest_batch_id": str(batch_id),
            },
        )
        staging.append(
            StagingRow(
                row_no=row_no,
                geoid=new_geoid(),
                geojson_text=geometry_to_geojson(place),
                external_id=place.external_id,
                provenance=provenance,
            )
        )
    return staging, rejects


async def ingest_features(
    session: AsyncSession,
    *,
    settings: Settings,
    principal: Principal,
    collection: Collection,
    features: list[dict[str, Any]],
    batch_id: uuid.UUID,
    row_offset: int = 0,
) -> IngestionReport:
    """Run one batch through staging + the arbiter and build the report.

    The caller owns the transaction boundary (one txn for sync; one per chunk for
    the async worker). ``GEOID_BULK_MAX_FEATURES`` is enforced before any work — a
    write bound errors, never truncates. ``row_offset`` makes ``row_no`` a global
    index across an async job's chunks.
    """
    if principal.is_anonymous and not collection.writable_anon:
        raise AnonymousWriteForbiddenError(collection.slug)
    if len(features) > settings.bulk_max_features:
        raise BulkLimitExceededError(len(features), settings.bulk_max_features)

    staging, rejects = _validate_and_stage(
        features,
        settings=settings,
        principal=principal,
        batch_id=batch_id,
        row_offset=row_offset,
    )

    await ingest_repo.create_staging(session)
    await ingest_repo.copy_into_staging(session, staging)

    # Screen out every per-row abort risk BEFORE the set-based insert runs.
    for r in await ingest_repo.reject_invalid_geometry(session):
        rejects.append(
            RejectedItem(row_no=r["row_no"], reason="geometry_invalid", detail=r["detail"])
        )
    for r in await ingest_repo.reject_external_id_conflicts(session, collection.id):
        rejects.append(
            RejectedItem(
                row_no=r["row_no"],
                reason="external_id_conflict",
                detail=r["detail"],
                constraint=UQ_PLACE_EXTERNAL_ID,
                external_id=r["external_id"],
            )
        )
    # Hash the survivors ONCE; the twin screen, arbiter, and conflict join all reuse it.
    await ingest_repo.populate_geom_hashes(session)
    for r in await ingest_repo.reject_in_batch_geometry_twins(session):
        rejects.append(
            RejectedItem(
                row_no=r["row_no"],
                reason="geometry_conflict_in_batch",
                detail="identical geometry to an earlier feature in this batch",
                incumbent_geoid=str(r["winner_geoid"]),
                external_id=r["external_id"],
            )
        )

    accepted_geoids = await ingest_repo.run_arbiter(
        session,
        collection_id=collection.id,
        originating_instance=settings.instance_id,
        batch_id=batch_id,
    )
    accepted_set = set(accepted_geoids)

    # Remaining staging rows that did NOT win the arbiter are catalog-wide geometry
    # conflicts; resolve each incumbent in one join (same txn sees the inserts).
    for r in await ingest_repo.geometry_conflicts(session):
        if r["staged_geoid"] in accepted_set:
            continue
        rejects.append(
            RejectedItem(
                row_no=r["row_no"],
                reason="geometry_conflict",
                detail="identical geometry already registered in the catalog",
                constraint=UQ_GEOID_REGISTRY_GEOM_HASH,
                incumbent_geoid=str(r["incumbent_geoid"]),
                external_id=r["external_id"],
            )
        )

    staged_by_geoid = {row.geoid: row for row in staging}
    accepted = [
        AcceptedItem(
            row_no=staged_by_geoid[geoid].row_no,
            geoid=str(geoid),
            uri=uri_for(geoid, settings.base_url_clean),
            collection=collection.slug,
            external_id=staged_by_geoid[geoid].external_id,
        )
        for geoid in accepted_geoids
    ]
    accepted.sort(key=lambda a: a.row_no)
    rejects.sort(key=lambda r: r.row_no)

    return IngestionReport(
        collection=collection.slug,
        batch_id=str(batch_id),
        place_set_uri=place_set_uri(settings, batch_id),
        total=len(features),
        accepted_count=len(accepted),
        rejected_count=len(rejects),
        accepted=accepted,
        rejected=rejects,
    )


async def run_ingest_sync(
    session: AsyncSession,
    *,
    settings: Settings,
    principal: Principal,
    collection: Collection,
    inputs: BulkIngestInputs,
) -> IngestionReport:
    """Synchronous inline ingest: parse by-value features and run one transaction."""
    features = _features_by_value(inputs.items)
    if features is None:
        raise ValueError(
            "by-reference ('href') ingest requires asynchronous execution (Prefer: respond-async)"
        )
    batch_id = new_geoid()  # the sync place-set id
    return await ingest_features(
        session,
        settings=settings,
        principal=principal,
        collection=collection,
        features=features,
        batch_id=batch_id,
    )


# --- Async path: enqueue + worker drain -------------------------------------


async def enqueue_async(
    session: AsyncSession,
    *,
    settings: Settings,
    collection: Collection,
    inputs: BulkIngestInputs,
    idempotency_key: str | None,
    subscriber: Subscriber | None,
    notify_email: str | None,
) -> tuple[IngestJob, bool]:
    """Durably enqueue an async ingest. Returns ``(job, created)``.

    By-VALUE async stores the inline FeatureCollection in ``payload`` (bounded by
    ``GEOID_BULK_MAX_FEATURES`` — it is still an HTTP request body); by-REFERENCE
    async stores the blob key in ``blob_uri`` (the worker fetches + chunks it, so it
    is unbounded). The caller commits, then best-effort triggers the worker.
    """
    features = _features_by_value(inputs.items)
    if features is None:
        blob_uri = inputs.items["href"]
        payload: dict[str, Any] | None = None
    else:
        if len(features) > settings.bulk_max_features:
            raise BulkLimitExceededError(len(features), settings.bulk_max_features)
        blob_uri = None
        payload = inputs.items

    job_id = new_geoid()
    return await job_repo.create_job(
        session,
        job_id=job_id,
        collection_id=collection.id,
        process_id=BULK_INGEST_PROCESS_ID,
        mode="async",
        blob_uri=blob_uri,
        payload=payload,
        idempotency_key=idempotency_key or inputs.idempotencyKey,
        notify_email=notify_email or inputs.notifyEmail,
        subscriber=subscriber.model_dump() if subscriber else None,
    )


async def _load_features(
    settings: Settings, *, blob_uri: str | None, payload: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Resolve the FeatureCollection's features from inline payload or a blob key."""
    if payload is not None:
        fc = payload
    else:
        if blob_uri is None:
            raise ValueError("async ingest job has neither inline payload nor a blob_uri")
        data = await get_blob_store().get(blob_uri)
        fc = json.loads(data)
    return _features_array(fc)


async def _process_one(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, job_id: uuid.UUID
) -> None:
    """Process one claimed job, dispatched by ``process_id``, then notify (always).

    Each job body self-handles its terminal state (marks successful/failed, never
    raises) so one bad job cannot abort the drain; the ``finally`` then sends the
    at-most-once completion notice (email + OGC callback) for whatever terminal state
    the body left — including ``failed``.
    """
    async with sessionmaker() as session:
        job = await job_repo.get_job(session, job_id)
        if job is None or job.status != "running":
            return  # dismissed or already terminal — nothing to do
        process_id = job.process_id
    try:
        if process_id == export_service.BULK_EXPORT_PROCESS_ID:
            await export_service.run_export(sessionmaker, settings, job_id)
        else:
            await _run_ingest_job(sessionmaker, settings, job_id)
    finally:
        await _notify_completion(sessionmaker, settings, job_id)


async def _run_ingest_job(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, job_id: uuid.UUID
) -> None:
    """The async-ingest body: load source, ingest in chunks, persist the report.

    Each chunk is its own transaction (unit-of-retry = unit-of-atomicity); TEMP
    staging vanishes on abort. The worker uses an admin principal — bulk is
    admin-gated, so the enqueuer was admin and ``created_by`` is unchanged.
    """
    async with sessionmaker() as session:
        job = await job_repo.get_job(session, job_id)
        if job is None or job.status != "running":
            return  # dismissed between claim and pickup — nothing to do
        collection = await collection_repo.get_by_id(session, job.collection_id)
        blob_uri, payload = job.blob_uri, job.payload

    if collection is None:
        await job_repo.fail_job(sessionmaker, job_id, "collection no longer exists")
        return
    try:
        features = await _load_features(settings, blob_uri=blob_uri, payload=payload)
    except Exception as exc:  # noqa: BLE001 — surface load failure on the job row
        await job_repo.fail_job(sessionmaker, job_id, f"could not load features: {exc}")
        return

    accepted: list[AcceptedItem] = []
    rejected: list[RejectedItem] = []
    chunk = settings.ingest_chunk_size
    try:
        for start in range(0, len(features), chunk):
            piece = features[start : start + chunk]
            async with sessionmaker() as session:
                report = await ingest_features(
                    session,
                    settings=settings,
                    principal=Principal.admin(),
                    collection=collection,
                    features=piece,
                    batch_id=job_id,
                    row_offset=start,
                )
                await session.commit()
            accepted.extend(report.accepted)
            rejected.extend(report.rejected)
    except Exception as exc:  # noqa: BLE001 — a chunk abort fails the job (committed rows stay)
        logger.exception("ingest job %s failed", job_id)
        await job_repo.fail_job(sessionmaker, job_id, str(exc))
        return

    final = IngestionReport(
        collection=collection.slug,
        batch_id=str(job_id),
        place_set_uri=place_set_uri(settings, job_id),
        total=len(features),
        accepted_count=len(accepted),
        rejected_count=len(rejected),
        accepted=accepted,
        rejected=rejected,
    )
    async with sessionmaker() as session:
        await job_repo.mark_successful(session, job_id, final.model_dump())
        await session.commit()


async def _notify_completion(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, job_id: uuid.UUID
) -> None:
    """Send the at-most-once completion notice (email) + OGC callback (machine push).

    The ``notified_at`` claim is taken FIRST (one statement), and the send runs only
    if THIS call won it — so a re-claimed/retried job emails at most once. The send is
    after the worker txn commits (a later rollback can't un-send) and never raises
    back into the worker — polling remains the contract.
    """
    async with sessionmaker() as session:
        job = await job_repo.get_job(session, job_id)
        if job is None:
            return
        won = await job_repo.mark_notified(session, job_id)
        await session.commit()
        subscriber, report_json, status = job.subscriber, job.report, job.status
    if not won:
        return

    # Only an ingest job's report is an IngestionReport; an export job's report is an
    # ExportResult (the email composer takes None and the callback posts report_json
    # verbatim either way).
    report = (
        IngestionReport.model_validate(report_json)
        if report_json and job.process_id == BULK_INGEST_PROCESS_ID
        else None
    )
    try:
        await get_notifier().send_completion(job, report)
    except Exception:  # noqa: BLE001 — notification is best-effort; polling is the contract
        logger.exception("completion email failed for job %s", job_id)
    try:
        await post_callback(subscriber, report=report_json, status=status)
    except Exception:  # noqa: BLE001 — callback is best-effort
        logger.exception("completion callback failed for job %s", job_id)


async def process_pending_jobs(settings: Settings, *, limit: int | None = None) -> list[uuid.UUID]:
    """Drain the queue: claim accepted jobs, then process each in its own transaction.

    The worker builds its OWN sessions via ``get_sessionmaker()`` (never the
    request-scoped ``get_session``, which closes too early). An extra drain that
    finds an empty queue claims nothing and returns — harmless under SKIP LOCKED.
    """
    from geoid.db import get_sessionmaker

    sessionmaker = get_sessionmaker()
    limit = limit or settings.ingest_drain_limit
    async with sessionmaker() as session:
        claimed = await job_repo.claim_jobs(session, limit=limit)
        await session.commit()
    for job_id in claimed:
        await _process_one(sessionmaker, settings, job_id)
    return claimed


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def job_status_info(settings: Settings, job: IngestJob) -> StatusInfo:
    """Shape an ``ingest_job`` row into an OGC StatusInfo (the GET /jobs/{id} body)."""
    base = settings.base_url_clean
    links = [Link(href=f"{base}/jobs/{job.id}", rel="self", type="application/json")]
    if job.status == "successful":
        links.append(
            Link(
                href=f"{base}/jobs/{job.id}/results",
                rel=_RESULTS_REL,
                type="application/json",
                title="Ingestion report",
            )
        )
    return StatusInfo(
        processID=job.process_id,
        jobID=str(job.id),
        status=job.status,
        message=job.message,
        created=_iso(job.created_at),
        started=_iso(job.started_at),
        finished=_iso(job.finished_at),
        updated=_iso(job.updated_at),
        links=links,
    )
