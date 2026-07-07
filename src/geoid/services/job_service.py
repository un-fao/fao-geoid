"""Import-job submission — validate, persist, launch (ordering is load-bearing).

The row is committed BEFORE the executor fires so the worker's ``claim`` can see
it (``get_session``'s later commit becomes a no-op). A launch failure flips the
row to ``failed`` (guarded, committed) and raises — an auditable failed job,
never an orphan ``accepted`` row.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from geoid.config import Settings
from geoid.deps import Principal
from geoid.models import Collection, ImportJob
from geoid.repositories import job_repo
from geoid.schemas.job import ImportSubmission
from geoid.services import import_refs, job_executor
from geoid.services.exceptions import JobLaunchError, JobRefRejectedError, TooManyJobsError
from geoid.services.registry_service import _authorize_write

logger = logging.getLogger(__name__)


async def create_job(
    session: AsyncSession,
    *,
    settings: Settings,
    principal: Principal,
    collection: Collection,
    submission: ImportSubmission,
) -> ImportJob:
    """Accept an import submission: authz → ref check → valve → row → launch.

    Raises:
        WriteNotAuthorizedError: caller may not write to this collection (403).
        JobRefRejectedError: href/prefix failed the scheme/host/bucket rules (422).
        TooManyJobsError: the active-jobs valve is full (429).
        JobLaunchError: the executor could not start the run (500; row = failed).
    """
    await _authorize_write(session, principal, collection)
    try:
        import_refs.validate_ref(submission.ref, is_prefix=submission.is_prefix, settings=settings)
    except ValueError as exc:
        raise JobRefRejectedError(str(exc)) from exc
    active = await job_repo.count_active(session)
    if active >= settings.job_max_concurrent:
        raise TooManyJobsError(active, settings.job_max_concurrent)

    job = await job_repo.insert_job(
        session,
        collection_id=collection.id,
        source_ref=submission.ref,
        source_is_prefix=submission.is_prefix,
        created_by=principal.subject or "",
        # Snapshot the email only when verified — the rebuilt worker Principal
        # must never carry MORE authority than the submitter had.
        created_by_email=principal.email if principal.email_verified else None,
        created_by_admin=principal.is_admin,
    )
    await session.commit()

    try:
        execution_name = await job_executor.launch(settings, job.id)
    except Exception as exc:
        logger.error("import job %s: executor launch failed", job.id, exc_info=exc)
        await job_repo.finish(
            session,
            job.id,
            status="failed",
            message="failed to start import execution",
            expected="accepted",
        )
        await session.commit()
        raise JobLaunchError(str(job.id)) from exc

    if execution_name:
        await job_repo.set_execution(session, job.id, execution_name)
        await session.commit()
    return job
