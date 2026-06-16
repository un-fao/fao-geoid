"""OGC API - Processes router — the bulk surface (18-062r2 Core).

``GET /processes`` and ``GET /processes/{id}`` describe the offered processes;
``POST /processes/{id}/execution`` runs one. Without a ``Prefer`` header the
execution is synchronous and returns **200 + the IngestionReport** inline;
``Prefer: respond-async`` (RFC 7240) selects the asynchronous path (added with the
job substrate). Execution is admin-gated — bulk is "for users with the correct
permissions" — while discovery (the two GETs) is public metadata.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.config import Settings, get_settings
from geoid.db import get_session
from geoid.deps import Principal, require_admin
from geoid.models import Collection, IngestJob
from geoid.repositories import collection_repo
from geoid.runjob import trigger_ingest_worker
from geoid.schemas.ingest import (
    BulkIngestInputs,
    Execute,
    IngestionReport,
    ProcessDescription,
    ProcessList,
    StatusInfo,
)
from geoid.services import export_service, ingest_service
from geoid.services.exceptions import CollectionNotFoundError, ProcessNotFoundError

router = APIRouter(tags=["processes"])

_RESPOND_ASYNC = "respond-async"


def _wants_async(prefer: str | None) -> bool:
    return prefer is not None and _RESPOND_ASYNC in prefer.lower()


@router.get("/processes", response_model=ProcessList, summary="List processes (OGC Core)")
async def list_processes(settings: Settings = Depends(get_settings)) -> ProcessList:
    return ingest_service.process_list(settings)


@router.get(
    "/processes/{process_id}",
    response_model=ProcessDescription,
    summary="Describe a process (OGC Core)",
)
async def describe_process(
    process_id: str, settings: Settings = Depends(get_settings)
) -> ProcessDescription:
    desc = ingest_service.process_description(settings, process_id)
    if desc is None:
        raise ProcessNotFoundError(process_id)
    return desc


@router.post(
    "/processes/{process_id}/execution",
    # The handler returns either an IngestionReport (sync) or a JSONResponse (async),
    # so the response model is documented via `responses` instead of inferred.
    response_model=None,
    summary="Execute a process (sync → 200 + report; async → 201 + Location)",
    responses={
        status.HTTP_200_OK: {"model": IngestionReport, "description": "Synchronous result."},
        status.HTTP_201_CREATED: {
            "model": StatusInfo,
            "description": "Async accepted — poll Location for status.",
        },
    },
)
async def execute_process(
    process_id: str,
    body: Execute,
    prefer: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response | IngestionReport:
    if not ingest_service.is_known_process(process_id):
        raise ProcessNotFoundError(process_id)

    # bulk-export is its own (async-only) process — dispatch before the ingest parse,
    # whose inputs schema (collection/items) differs from the export inputs.
    if process_id == export_service.BULK_EXPORT_PROCESS_ID:
        return await _execute_export(session, settings, body, prefer, idempotency_key)

    try:
        inputs = ingest_service.parse_inputs(body.inputs)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    collection = await collection_repo.get_by_slug(session, inputs.collection)
    if collection is None:
        raise CollectionNotFoundError(inputs.collection)

    if _wants_async(prefer):
        return await _execute_async(session, settings, collection, inputs, idempotency_key, body)

    try:
        return await ingest_service.run_ingest_sync(
            session,
            settings=settings,
            principal=principal,
            collection=collection,
            inputs=inputs,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


async def _accept_async_job(
    session: AsyncSession, settings: Settings, job: IngestJob, created: bool
) -> Response:
    """Commit the durable job, best-effort trigger the worker, return 201/200 + Location.

    Shared by both async processes (ingest + export). The row is committed BEFORE the
    trigger so a dropped/failed ``jobs.run`` is only a delay (the Scheduler fallback
    drains it); ``get_session``'s commit-on-return is then a no-op. An idempotent
    replay (same Idempotency-Key) returns the EXISTING job (200) and does NOT
    re-trigger. OGC 34: async execution returns 201, not 202.
    """
    await session.commit()
    if created:
        await trigger_ingest_worker(settings)
    status_info = ingest_service.job_status_info(settings, job)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content=status_info.model_dump(),
        headers={"Location": f"{settings.base_url_clean}/jobs/{job.id}"},
    )


async def _execute_async(
    session: AsyncSession,
    settings: Settings,
    collection: Collection,
    inputs: BulkIngestInputs,
    idempotency_key: str | None,
    body: Execute,
) -> Response:
    """Durably enqueue an async INGEST → 201 + Location (or 200 on idempotent replay)."""
    try:
        job, created = await ingest_service.enqueue_async(
            session,
            settings=settings,
            collection=collection,
            inputs=inputs,
            idempotency_key=idempotency_key,
            subscriber=body.subscriber,
            notify_email=None,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return await _accept_async_job(session, settings, job, created)


async def _execute_export(
    session: AsyncSession,
    settings: Settings,
    body: Execute,
    prefer: str | None,
    idempotency_key: str | None,
) -> Response:
    """Durably enqueue an async EXPORT → 201 + Location.

    Export is async-only: a large/permissioned export must not stream inline under
    Cloud Run's 60-minute request cap, so a sync request (no ``Prefer: respond-async``)
    is a 400.
    """
    try:
        inputs = export_service.parse_export_inputs(body.inputs)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if not _wants_async(prefer):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "bulk-export requires asynchronous execution (Prefer: respond-async)",
        )
    collection = await collection_repo.get_by_slug(session, inputs.collection)
    if collection is None:
        raise CollectionNotFoundError(inputs.collection)
    job, created = await export_service.enqueue_export(
        session,
        collection=collection,
        inputs=inputs,
        idempotency_key=idempotency_key,
        subscriber=body.subscriber,
    )
    return await _accept_async_job(session, settings, job, created)
