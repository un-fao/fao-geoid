"""Import-job data access — guarded status transitions over ``import_job``.

The state machine is enforced by WHERE clauses, never by trust: ``claim`` is the
compare-and-set idempotency hinge (only an ``accepted`` row becomes ``running``,
so a re-executed worker no-ops), and every worker-side write (``heartbeat``,
``finish``) is guarded on the expected current status — a job flipped
externally (the on-read reaper, a future cancel) can never be resurrected by a
zombie worker. All timestamps are SQL-side ``now()`` so app clock skew never
orders transitions.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from geoid.domain.identifiers import uuid7
from geoid.models import ImportJob

_ACTIVE_STATUSES = ("accepted", "running")


async def insert_job(
    session: AsyncSession,
    *,
    collection_id: uuid.UUID,
    source_ref: str,
    source_is_prefix: bool,
    created_by: str,
    created_by_email: str | None,
    created_by_admin: bool,
) -> ImportJob:
    job = ImportJob(
        id=uuid7(),
        collection_id=collection_id,
        status="accepted",
        source_ref=source_ref,
        source_is_prefix=source_is_prefix,
        created_by=created_by,
        created_by_email=created_by_email,
        created_by_admin=created_by_admin,
    )
    session.add(job)
    await session.flush()
    await session.refresh(job)
    return job


async def get(session: AsyncSession, job_id: uuid.UUID) -> ImportJob | None:
    """Load a job WITHOUT its report blob (the status surface never needs it).

    ``populate_existing``: job rows are mutated by raw guarded UPDATEs (reap,
    the test backdates) that bypass the ORM identity map — a poll must always
    reflect the DB row, never a stale cached instance.
    """
    stmt = (
        select(ImportJob)
        .options(defer(ImportJob.report))
        .where(ImportJob.id == job_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_report(session: AsyncSession, job_id: uuid.UUID) -> dict[str, Any] | None:
    """The stored results document alone (the one read that pays for the blob)."""
    stmt = select(ImportJob.report).where(ImportJob.id == job_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def claim(session: AsyncSession, job_id: uuid.UUID) -> ImportJob | None:
    """CAS ``accepted`` → ``running``. None = already claimed/finished (safe no-op)."""
    stmt = (
        update(ImportJob)
        .where(ImportJob.id == job_id, ImportJob.status == "accepted")
        .values(status="running", started_at=func.now(), updated_at=func.now())
        .returning(ImportJob)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def heartbeat(
    session: AsyncSession, job_id: uuid.UUID, *, progress: int | None = None
) -> bool:
    """Refresh ``updated_at`` (+ optional progress) — ONLY while still ``running``.

    False = the row was flipped externally (reaper/cancel); the worker must abort.
    """
    values: dict[str, Any] = {"updated_at": func.now()}
    if progress is not None:
        values["progress"] = progress
    stmt = (
        update(ImportJob)
        .where(ImportJob.id == job_id, ImportJob.status == "running")
        .values(**values)
        .returning(ImportJob.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def finish(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    status: str,
    message: str | None = None,
    report: dict[str, Any] | None = None,
    progress: int | None = None,
    expected: str = "running",
) -> bool:
    """Terminal transition, guarded on the expected current status."""
    values: dict[str, Any] = {
        "status": status,
        "message": message,
        "finished_at": func.now(),
        "updated_at": func.now(),
    }
    if report is not None:
        values["report"] = report
    if progress is not None:
        values["progress"] = progress
    stmt = (
        update(ImportJob)
        .where(ImportJob.id == job_id, ImportJob.status == expected)
        .values(**values)
        .returning(ImportJob.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def set_execution(session: AsyncSession, job_id: uuid.UUID, execution_name: str) -> None:
    stmt = update(ImportJob).where(ImportJob.id == job_id).values(execution_name=execution_name)
    await session.execute(stmt)


async def reap(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    running_stale_seconds: int,
    accepted_stale_seconds: int,
) -> bool:
    """Flip a dead job to ``failed`` (the lazy on-read reaper; no scheduler).

    Two guarded, SQL-side-timed UPDATEs: a ``running`` row whose heartbeat
    (``updated_at``) went silent = the worker died or hit the task timeout; an
    ``accepted`` row that never started = the execution was never scheduled.
    Convergent against a live worker: its writes are guarded ``status='running'``,
    so once reaped it can only abort — never resurrect the row.
    """
    reaped = await session.execute(
        text(
            "UPDATE import_job SET status='failed', "
            "message='import worker died or timed out', "
            "finished_at=now(), updated_at=now() "
            "WHERE id=:id AND status='running' "
            "AND updated_at < now() - make_interval(secs => :stale) "
            "RETURNING id"
        ),
        {"id": job_id, "stale": running_stale_seconds},
    )
    if reaped.scalar_one_or_none() is not None:
        return True
    stranded = await session.execute(
        text(
            "UPDATE import_job SET status='failed', "
            "message='import execution never started', "
            "finished_at=now(), updated_at=now() "
            "WHERE id=:id AND status='accepted' "
            "AND created_at < now() - make_interval(secs => :stale) "
            "RETURNING id"
        ),
        {"id": job_id, "stale": accepted_stale_seconds},
    )
    return stranded.scalar_one_or_none() is not None


async def count_active(session: AsyncSession) -> int:
    """Jobs currently accepted/running (the submit-time concurrency valve)."""
    stmt = select(func.count()).select_from(ImportJob).where(ImportJob.status.in_(_ACTIVE_STATUSES))
    return (await session.execute(stmt)).scalar_one()
