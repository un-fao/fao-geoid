"""Import-execution dispatch — how a submitted job actually starts running.

``inline`` (default): an asyncio task in this process — dev/compose/tests, zero
cloud deps. ``cloud_run_job``: one Cloud Run Job execution per import, with a
per-execution env override carrying the job id (``GEOID_JOB_ID``). The SDK is
imported lazily (PyJWT precedent) so an image without the ``jobs`` extra still
imports this module; the sync SDK call runs in a worker thread (JWKS precedent).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from geoid.config import Settings

logger = logging.getLogger(__name__)

# Strong refs so inline tasks aren't garbage-collected mid-run.
_INLINE_TASKS: set[asyncio.Task] = set()

_jobs_client: Any = None


async def launch(settings: Settings, job_id: uuid.UUID) -> str | None:
    """Start the import execution for ``job_id``; return its execution name (if any)."""
    if settings.job_executor == "inline":
        from geoid.services import import_service

        task = asyncio.create_task(import_service.run_job(str(job_id)))
        _INLINE_TASKS.add(task)
        task.add_done_callback(_INLINE_TASKS.discard)
        return None
    return await _launch_cloud_run_job(settings, job_id)


def _get_jobs_client() -> Any:
    global _jobs_client
    if _jobs_client is None:
        from google.cloud import run_v2

        _jobs_client = run_v2.JobsClient()
    return _jobs_client


def _run_job_request(settings: Settings, job_id: uuid.UUID) -> Any:
    from google.cloud import run_v2

    return run_v2.RunJobRequest(
        name=settings.import_job_name,
        overrides=run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    env=[run_v2.EnvVar(name="GEOID_JOB_ID", value=str(job_id))]
                )
            ]
        ),
    )


async def _launch_cloud_run_job(settings: Settings, job_id: uuid.UUID) -> str | None:
    import anyio

    client = _get_jobs_client()
    request = _run_job_request(settings, job_id)

    def _dispatch() -> Any:
        # Return the LRO handle only — operation.result() would block until the
        # whole import finishes. metadata.name is the execution id (forensics).
        return client.run_job(request=request)

    operation = await anyio.to_thread.run_sync(_dispatch)
    metadata = getattr(operation, "metadata", None)
    return getattr(metadata, "name", None)
