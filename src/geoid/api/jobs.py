"""OGC API - Processes job router — status, results, and dismiss.

Polling ``GET /jobs/{id}`` is the *contract* for async ingest completion (always
available, testable); the email + OGC callback are secondary push conveniences.
``GET /jobs/{id}/results`` returns the IngestionReport once the job is successful.
Admin-gated like the rest of the bulk surface.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.config import Settings, get_settings
from geoid.db import get_session
from geoid.deps import require_admin
from geoid.repositories import job_repo
from geoid.schemas.ingest import StatusInfo
from geoid.services import ingest_service
from geoid.services.exceptions import JobNotFoundError

router = APIRouter(tags=["jobs"], dependencies=[Depends(require_admin)])


@router.get("/jobs/{job_id}", response_model=StatusInfo, summary="Job status (OGC StatusInfo)")
async def get_job_status(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> StatusInfo:
    job = await job_repo.get_job(session, job_id)
    if job is None:
        raise JobNotFoundError(str(job_id))
    return ingest_service.job_status_info(settings, job)


@router.get("/jobs/{job_id}/results", summary="Job results (the IngestionReport)")
async def get_job_results(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    job = await job_repo.get_job(session, job_id)
    if job is None:
        raise JobNotFoundError(str(job_id))
    if job.report is None:
        # OGC: results are only available for a successful job.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"results not available; job status is {job.status!r}",
        )
    return job.report


@router.delete(
    "/jobs/{job_id}",
    response_model=StatusInfo,
    summary="Dismiss (cancel) a queued or running job (OGC /conf/dismiss)",
)
async def dismiss_job(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> StatusInfo:
    job = await job_repo.get_job(session, job_id)
    if job is None:
        raise JobNotFoundError(str(job_id))
    # No-op if already terminal (a finished result must never be clobbered); the
    # worker's terminal updates are guarded WHERE status='running' for the inverse race.
    await job_repo.mark_dismissed(session, job_id)
    await session.refresh(job)
    return ingest_service.job_status_info(settings, job)
