"""Ingest-job queue data access — durable system of record for async ingests.

The job row is committed BEFORE the worker is triggered, so a dropped trigger is a
delay (drained by the Scheduler fallback), never a loss. The worker claims rows
with ``FOR UPDATE SKIP LOCKED`` and flips them to ``running`` in one statement, so
concurrent drains never double-process and an extra drain that finds an empty queue
simply claims nothing.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from geoid.models import IngestJob


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> IngestJob | None:
    return await session.get(IngestJob, job_id)


async def create_job(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    collection_id: uuid.UUID,
    process_id: str,
    mode: str,
    blob_uri: str | None,
    payload: dict[str, Any] | None,
    idempotency_key: str | None,
    notify_email: str | None,
    subscriber: dict[str, Any] | None,
) -> tuple[IngestJob, bool]:
    """Insert an async job. Returns ``(job, created)``.

    Idempotency-Key is a UNIQUE column: a second submission with the same key
    inserts nothing and returns the EXISTING job (``created=False``) — a lost 202
    must not double-process or double-email a blob. The insert is one statement
    (``ON CONFLICT DO NOTHING``), so the de-dup is race-safe.
    """
    insert_stmt = text(
        """
        INSERT INTO ingest_job (
            id, collection_id, process_id, mode, blob_uri, payload,
            status, idempotency_key, notify_email, subscriber
        )
        VALUES (
            :id, :collection_id, :process_id, :mode, :blob_uri, CAST(:payload AS jsonb),
            'accepted', :idempotency_key, :notify_email, CAST(:subscriber AS jsonb)
        )
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """
    )
    params = {
        "id": job_id,
        "collection_id": collection_id,
        "process_id": process_id,
        "mode": mode,
        "blob_uri": blob_uri,
        "payload": json.dumps(payload) if payload is not None else None,
        "idempotency_key": idempotency_key,
        "notify_email": notify_email,
        "subscriber": json.dumps(subscriber) if subscriber is not None else None,
    }
    row = (await session.execute(insert_stmt, params)).first()
    if row is not None:
        job = await get_job(session, row[0])
        if job is None:
            raise RuntimeError("just-inserted ingest_job vanished before re-read")
        return job, True

    # Conflict on idempotency_key -> return the incumbent job.
    existing_id = (
        await session.execute(
            select(IngestJob.id).where(IngestJob.idempotency_key == idempotency_key)
        )
    ).scalar_one()
    job = await get_job(session, existing_id)
    if job is None:
        raise RuntimeError("ingest_job for the conflicting idempotency_key vanished")
    return job, False


async def claim_jobs(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """Atomically claim up to ``limit`` accepted jobs: lock, flip to running, return ids.

    ``FOR UPDATE SKIP LOCKED`` lets concurrent workers each take a disjoint set
    without blocking; flipping status to ``running`` in the same statement is the
    durable claim marker (other drains filter ``status='accepted'``).
    """
    stmt = text(
        """
        WITH claimed AS (
            SELECT id FROM ingest_job
            WHERE status = 'accepted'
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT :limit
        )
        UPDATE ingest_job j
        SET status = 'running', claimed_at = now(), started_at = now(), updated_at = now()
        FROM claimed
        WHERE j.id = claimed.id
        RETURNING j.id
        """
    )
    rows = (await session.execute(stmt, {"limit": limit})).all()
    return [r[0] for r in rows]


async def mark_successful(session: AsyncSession, job_id: uuid.UUID, report: dict[str, Any]) -> None:
    # AND status='running' so a concurrent dismiss (/conf/dismiss) is not clobbered.
    await session.execute(
        text(
            """
            UPDATE ingest_job
            SET status = 'successful', report = CAST(:report AS jsonb),
                finished_at = now(), updated_at = now()
            WHERE id = :id AND status = 'running'
            """
        ),
        {"id": job_id, "report": json.dumps(report)},
    )


async def mark_failed(session: AsyncSession, job_id: uuid.UUID, message: str) -> None:
    await session.execute(
        text(
            """
            UPDATE ingest_job
            SET status = 'failed', message = :message, finished_at = now(), updated_at = now()
            WHERE id = :id AND status = 'running'
            """
        ),
        {"id": job_id, "message": message},
    )


async def fail_job(
    sessionmaker: async_sessionmaker[AsyncSession], job_id: uuid.UUID, message: str
) -> None:
    """Mark a job failed in its OWN committed transaction.

    The shared owned-session wrapper over :func:`mark_failed` used by both worker
    bodies (ingest + export). The completion notice is sent separately by the worker
    dispatcher's ``finally``, so a failed job notifies exactly like a successful one.
    """
    async with sessionmaker() as session:
        await mark_failed(session, job_id, message)
        await session.commit()


async def mark_dismissed(session: AsyncSession, job_id: uuid.UUID) -> bool:
    """Dismiss (/conf/dismiss) a queued or running job; True if a row transitioned.

    Only ``accepted``/``running`` jobs can be dismissed; a terminal job is left
    untouched (returns False) so a completed result is never clobbered.
    """
    row = (
        await session.execute(
            text(
                """
                UPDATE ingest_job
                SET status = 'dismissed', finished_at = now(), updated_at = now()
                WHERE id = :id AND status IN ('accepted', 'running')
                RETURNING id
                """
            ),
            {"id": job_id},
        )
    ).first()
    return row is not None


async def mark_notified(session: AsyncSession, job_id: uuid.UUID) -> bool:
    """Claim the single notification for a job. True iff THIS call won the claim.

    ``WHERE notified_at IS NULL`` makes the send at-most-once even if a job is
    re-claimed/retried — only the caller that flips notified_at sends the email.
    """
    row = (
        await session.execute(
            text(
                """
                UPDATE ingest_job
                SET notified_at = now(), updated_at = now()
                WHERE id = :id AND notified_at IS NULL
                RETURNING id
                """
            ),
            {"id": job_id},
        )
    ).first()
    return row is not None
