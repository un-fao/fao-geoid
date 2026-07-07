"""Job status + results — the OGC API - Processes Part 1 v1.0 read subset.

Visibility is creator-or-sysadmin; everyone else (and every unknown id) gets the
byte-identical ``no-such-job`` 404 (existence-masking, matching the resolver
posture). Reading a status lazily reaps dead jobs (no scheduler): a ``running``
row whose heartbeat went stale, or an ``accepted`` row that never started, flips
to ``failed`` on read — convergent against a live worker because every worker
write is guarded ``WHERE status='running'``.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.config import Settings, get_settings
from geoid.db import get_session
from geoid.deps import Principal, require_principal
from geoid.models import ImportJob
from geoid.repositories import job_repo
from geoid.schemas.job import JobLink, OgcException, StatusInfo, status_info_from_job
from geoid.services.exceptions import (
    JobFailedError,
    JobNotFoundError,
    JobResultsNotReadyError,
)

router = APIRouter(prefix="/jobs", tags=["registry"])

_RESULTS_REL = "http://www.opengis.net/def/rel/ogc/1.0/results"
_EXCEPTIONS_REL = "http://www.opengis.net/def/rel/ogc/1.0/exceptions"
# An accepted row older than this that never started = the execution was never
# scheduled (launch races aside, starts take seconds).
_ACCEPTED_STALE_SECONDS = 900


async def _load_visible_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    principal: Principal,
    settings: Settings,
) -> ImportJob:
    job = await job_repo.get(session, job_id)
    visible = job is not None and (
        principal.is_admin
        or (principal.subject is not None and principal.subject == job.created_by)
    )
    if job is None or not visible:
        raise JobNotFoundError(str(job_id))
    if job.status in ("accepted", "running"):
        reaped = await job_repo.reap(
            session,
            job.id,
            running_stale_seconds=settings.job_stale_seconds,
            accepted_stale_seconds=_ACCEPTED_STALE_SECONDS,
        )
        if reaped:
            refreshed = await job_repo.get(session, job_id)
            if refreshed is not None:
                return refreshed
    return job


def _links(job: ImportJob, settings: Settings) -> list[JobLink]:
    base = settings.base_url_clean
    links = [JobLink(href=f"{base}/jobs/{job.id}", rel="self", type="application/json")]
    if job.status == "successful":
        links.append(
            JobLink(href=f"{base}/jobs/{job.id}/results", rel=_RESULTS_REL, type="application/json")
        )
    elif job.status == "failed":
        links.append(
            JobLink(
                href=f"{base}/jobs/{job.id}/results", rel=_EXCEPTIONS_REL, type="application/json"
            )
        )
    return links


@router.get(
    "/{job_id}",
    response_model=StatusInfo,
    response_model_exclude_none=True,
    summary="Import job status (OGC API - Processes statusInfo)",
    responses={404: {"model": OgcException, "description": "Unknown (or not your) job id."}},
)
async def get_job_status(
    job_id: uuid.UUID,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> StatusInfo:
    job = await _load_visible_job(session, job_id, principal, settings)
    return status_info_from_job(job, links=_links(job, settings))


@router.get(
    "/{job_id}/results",
    summary="Import job results (per-feature outcome report)",
    responses={
        200: {"description": "The results document (summary + per-file reports)."},
        404: {
            "model": OgcException,
            "description": "Unknown/not-your job id, or the job is still running.",
        },
        500: {"model": OgcException, "description": "The job failed; detail carries why."},
    },
)
async def get_job_results(
    job_id: uuid.UUID,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    job = await _load_visible_job(session, job_id, principal, settings)
    if job.status == "successful":
        report = await job_repo.get_report(session, job.id)
        if report is None:
            raise JobFailedError(str(job.id), "results document missing")
        return report
    if job.status == "failed":
        raise JobFailedError(str(job.id), job.message)
    raise JobResultsNotReadyError(str(job.id))
