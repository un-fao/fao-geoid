"""Integration tests for the import-job state machine (guarded transitions).

``claim`` is the idempotency hinge (CAS accepted→running); ``heartbeat``/
``finish`` are guarded on the current status so an externally flipped row can
never be resurrected; ``reap`` flips only genuinely stale rows.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from geoid.models import Collection
from geoid.repositories import job_repo

pytestmark = pytest.mark.integration

_REF = "https://storage.googleapis.com/bucket/data.json"


async def _public_collection_id(session):
    return (
        await session.execute(select(Collection.id).where(Collection.slug == "public"))
    ).scalar_one()


async def _insert(session, **overrides):
    job = await job_repo.insert_job(
        session,
        collection_id=await _public_collection_id(session),
        source_ref=overrides.pop("source_ref", _REF),
        source_is_prefix=overrides.pop("source_is_prefix", False),
        created_by=overrides.pop("created_by", "importer-1"),
        created_by_email=overrides.pop("created_by_email", None),
        created_by_admin=overrides.pop("created_by_admin", False),
    )
    await session.commit()
    return job


async def test_insert_defaults_and_get(session):
    job = await _insert(session)
    row = await job_repo.get(session, job.id)
    assert row is not None
    assert row.status == "accepted"
    assert row.created_by == "importer-1"
    assert row.started_at is None and row.finished_at is None
    assert row.created_at is not None and row.updated_at is not None


async def test_claim_is_a_one_shot_cas(session):
    job = await _insert(session)
    claimed = await job_repo.claim(session, job.id)
    await session.commit()
    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.started_at is not None
    # Second claim (a re-executed worker) must no-op.
    assert await job_repo.claim(session, job.id) is None


async def test_heartbeat_only_while_running(session):
    job = await _insert(session)
    assert await job_repo.heartbeat(session, job.id) is False  # accepted, not running
    await job_repo.claim(session, job.id)
    assert await job_repo.heartbeat(session, job.id, progress=30) is True
    row = await job_repo.get(session, job.id)
    assert row.progress == 30
    await job_repo.finish(session, job.id, status="failed", message="boom")
    assert await job_repo.heartbeat(session, job.id) is False  # terminal → guard holds


async def test_finish_is_guarded_on_the_expected_status(session):
    job = await _insert(session)
    # A worker finish (expects running) can't touch an accepted row.
    assert await job_repo.finish(session, job.id, status="successful") is False
    await job_repo.claim(session, job.id)
    assert (
        await job_repo.finish(session, job.id, status="successful", report={"summary": {}})
        is True
    )
    # Terminal is terminal: no second transition.
    assert await job_repo.finish(session, job.id, status="failed", message="late") is False
    row = await job_repo.get(session, job.id)
    assert row.status == "successful"
    assert row.finished_at is not None
    assert await job_repo.get_report(session, job.id) == {"summary": {}}


async def test_reap_flips_only_stale_rows(session):
    fresh = await _insert(session)
    await job_repo.claim(session, fresh.id)
    await session.commit()
    # A live (fresh-heartbeat) running row must NOT be reaped.
    assert (
        await job_repo.reap(session, fresh.id, running_stale_seconds=600, accepted_stale_seconds=900)
        is False
    )

    stale = await _insert(session)
    await job_repo.claim(session, stale.id)
    await session.execute(
        text("UPDATE import_job SET updated_at = now() - interval '20 minutes' WHERE id = :id"),
        {"id": stale.id},
    )
    assert (
        await job_repo.reap(session, stale.id, running_stale_seconds=600, accepted_stale_seconds=900)
        is True
    )
    row = await job_repo.get(session, stale.id)
    assert row.status == "failed"
    assert "died or timed out" in row.message

    stranded = await _insert(session)
    await session.execute(
        text("UPDATE import_job SET created_at = now() - interval '20 minutes' WHERE id = :id"),
        {"id": stranded.id},
    )
    assert (
        await job_repo.reap(
            session, stranded.id, running_stale_seconds=600, accepted_stale_seconds=900
        )
        is True
    )
    row = await job_repo.get(session, stranded.id)
    assert row.status == "failed"
    assert "never started" in row.message


async def test_reaped_worker_cannot_resurrect_the_row(session):
    job = await _insert(session)
    await job_repo.claim(session, job.id)
    await session.execute(
        text("UPDATE import_job SET updated_at = now() - interval '20 minutes' WHERE id = :id"),
        {"id": job.id},
    )
    await job_repo.reap(session, job.id, running_stale_seconds=600, accepted_stale_seconds=900)
    # The zombie's guarded writes all no-op now.
    assert await job_repo.heartbeat(session, job.id) is False
    assert await job_repo.finish(session, job.id, status="successful", report={}) is False
    assert (await job_repo.get(session, job.id)).status == "failed"


async def test_count_active_counts_only_accepted_and_running(session):
    assert await job_repo.count_active(session) == 0
    first = await _insert(session)
    second = await _insert(session)
    third = await _insert(session)
    await job_repo.claim(session, second.id)
    await job_repo.claim(session, third.id)
    await job_repo.finish(session, third.id, status="failed", message="x")
    assert await job_repo.count_active(session) == 2  # first (accepted) + second (running)
