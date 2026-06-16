"""Integration tests for the async bulk-ingest path (Phase 3).

Covers the durable queue + worker drain, by-value and by-reference sources,
Idempotency-Key replay, the job status/results surface, SKIP LOCKED disjointness,
and the no-immutability-triggers invariant on ingest_job.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = pytest.mark.integration


def _square(x: float, y: float, *, external_id: str | None = None) -> dict:
    feature: dict = {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[x, y], [x + 1, y], [x + 1, y + 1], [x, y + 1], [x, y]]],
        },
        "properties": {},
    }
    if external_id is not None:
        feature["id"] = external_id
    return feature


def _async_body(collection: str, features: list[dict]) -> dict:
    return {
        "inputs": {
            "collection": collection,
            "items": {"type": "FeatureCollection", "features": features},
        }
    }


async def _post_async(client, admin_headers, body, *, idem: str | None = None):
    headers = {**admin_headers, "Prefer": "respond-async"}
    if idem is not None:
        headers["Idempotency-Key"] = idem
    return await client.post("/processes/bulk-ingest/execution", headers=headers, json=body)


async def _run_worker():
    from geoid.config import get_settings
    from geoid.services import ingest_service

    return await ingest_service.process_pending_jobs(get_settings())


async def test_async_execute_returns_201_with_location(client, admin_headers):
    resp = await _post_async(client, admin_headers, _async_body("public", [_square(10, 10)]))
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "accepted"  # OGC enum (not "success"/"queued")
    assert body["type"] == "process"
    assert resp.headers["location"].endswith(f"/jobs/{body['jobID']}")


async def test_async_worker_drains_and_completes(client, admin_headers):
    resp = await _post_async(
        client, admin_headers, _async_body("public", [_square(20, 20), _square(21, 21)])
    )
    job_id = resp.json()["jobID"]

    # Pull-polling is the contract: before the worker runs, the job is accepted and
    # has no results yet.
    assert (await client.get(f"/jobs/{job_id}", headers=admin_headers)).json()[
        "status"
    ] == "accepted"
    assert (await client.get(f"/jobs/{job_id}/results", headers=admin_headers)).status_code == 404

    claimed = await _run_worker()
    assert job_id in [str(c) for c in claimed]

    status = (await client.get(f"/jobs/{job_id}", headers=admin_headers)).json()
    assert status["status"] == "successful"

    results = await client.get(f"/jobs/{job_id}/results", headers=admin_headers)
    assert results.status_code == 200
    report = results.json()
    assert report["accepted_count"] == 2
    assert report["batch_id"] == job_id  # async batch_id IS the jobID


async def test_async_by_reference_blob(client, admin_headers):
    from geoid.storage import get_blob_store

    fc = {"type": "FeatureCollection", "features": [_square(30, 30), _square(31, 31)]}
    key = "test-ingest/by-ref.geojson"
    await get_blob_store().put(key, json.dumps(fc).encode("utf-8"))

    body = {"inputs": {"collection": "public", "items": {"href": key}}}
    resp = await _post_async(client, admin_headers, body)
    assert resp.status_code == 201
    job_id = resp.json()["jobID"]

    await _run_worker()
    report = (await client.get(f"/jobs/{job_id}/results", headers=admin_headers)).json()
    assert report["accepted_count"] == 2


async def test_async_batch_id_populates_ingest_batch_id(client, admin_headers, session):
    from sqlalchemy import text

    resp = await _post_async(client, admin_headers, _async_body("public", [_square(40, 40)]))
    job_id = resp.json()["jobID"]
    await _run_worker()

    rows = (
        await session.execute(
            text("SELECT id FROM place WHERE ingest_batch_id = :b"), {"b": job_id}
        )
    ).all()
    assert len(rows) == 1


async def test_idempotency_key_replays_existing_job(client, admin_headers):
    body = _async_body("public", [_square(50, 50)])
    first = await _post_async(client, admin_headers, body, idem="key-123")
    assert first.status_code == 201
    job_id = first.json()["jobID"]

    # Same key, same (or different) body -> the EXISTING job, no new enqueue, 200.
    second = await _post_async(client, admin_headers, body, idem="key-123")
    assert second.status_code == 200
    assert second.json()["jobID"] == job_id


async def test_job_status_requires_admin(client, admin_headers):
    resp = await _post_async(client, admin_headers, _async_body("public", [_square(60, 60)]))
    job_id = resp.json()["jobID"]
    assert (await client.get(f"/jobs/{job_id}")).status_code == 401


async def test_unknown_job_returns_404(client, admin_headers):
    resp = await client.get("/jobs/019e0000-0000-7000-8000-000000000000", headers=admin_headers)
    assert resp.status_code == 404


async def test_ingest_job_has_no_immutability_triggers(session):
    from sqlalchemy import text

    count = (
        await session.execute(
            text(
                """
                SELECT count(*) FROM pg_trigger t
                JOIN pg_class c ON c.oid = t.tgrelid
                WHERE c.relname = 'ingest_job' AND NOT t.tgisinternal
                """
            )
        )
    ).scalar_one()
    assert count == 0  # status-mutable: the worker UPDATEs it; triggers would deadlock


async def test_dismiss_queued_job_prevents_processing(client, admin_headers, session):
    from sqlalchemy import text

    resp = await _post_async(client, admin_headers, _async_body("public", [_square(33, 33)]))
    job_id = resp.json()["jobID"]

    dismissed = await client.delete(f"/jobs/{job_id}", headers=admin_headers)
    assert dismissed.status_code == 200
    assert dismissed.json()["status"] == "dismissed"  # OGC enum

    # The worker claims only 'accepted' rows, so a dismissed job is never processed.
    await _run_worker()
    assert (await client.get(f"/jobs/{job_id}", headers=admin_headers)).json()[
        "status"
    ] == "dismissed"
    count = (
        await session.execute(
            text("SELECT count(*) FROM place WHERE ingest_batch_id = :b"), {"b": job_id}
        )
    ).scalar_one()
    assert count == 0


async def test_dismiss_terminal_job_is_noop(client, admin_headers):
    resp = await _post_async(client, admin_headers, _async_body("public", [_square(34, 34)]))
    job_id = resp.json()["jobID"]
    await _run_worker()  # -> successful

    dismissed = await client.delete(f"/jobs/{job_id}", headers=admin_headers)
    assert dismissed.status_code == 200
    assert dismissed.json()["status"] == "successful"  # a finished result is not clobbered


async def test_dismiss_unknown_job_404(client, admin_headers):
    resp = await client.delete("/jobs/019e0000-0000-7000-8000-000000000000", headers=admin_headers)
    assert resp.status_code == 404


async def test_mark_notified_is_at_most_once(session):
    from geoid.domain.identifiers import new_geoid
    from geoid.repositories import collection_repo, job_repo

    coll = await collection_repo.get_by_slug(session, "public")
    job, _ = await job_repo.create_job(
        session,
        job_id=new_geoid(),
        collection_id=coll.id,
        process_id="bulk-ingest",
        mode="async",
        blob_uri=None,
        payload={"features": []},
        idempotency_key=None,
        notify_email="ops@fao.org",
        subscriber=None,
    )
    await session.commit()

    first = await job_repo.mark_notified(session, job.id)
    await session.commit()
    second = await job_repo.mark_notified(session, job.id)
    await session.commit()
    assert first is True  # this caller won the single notification claim
    assert second is False  # at-most-once: a re-claim sends nothing


async def test_async_notify_email_stored_on_job(client, admin_headers, session):
    from sqlalchemy import text

    body = {
        "inputs": {
            "collection": "public",
            "items": {"type": "FeatureCollection", "features": [_square(82, 82)]},
            "notifyEmail": "ops@fao.org",
        }
    }
    resp = await _post_async(client, admin_headers, body)
    job_id = resp.json()["jobID"]
    row = (
        await session.execute(
            text("SELECT notify_email FROM ingest_job WHERE id = :i"), {"i": job_id}
        )
    ).first()
    assert row[0] == "ops@fao.org"


async def test_concurrent_skip_locked_claims_are_disjoint(client, admin_headers):
    from geoid.db import get_sessionmaker
    from geoid.repositories import job_repo

    await _post_async(client, admin_headers, _async_body("public", [_square(70, 70)]))
    await _post_async(client, admin_headers, _async_body("public", [_square(71, 71)]))

    sm = get_sessionmaker()

    async def claim_one():
        async with sm() as s:
            ids = await job_repo.claim_jobs(s, limit=1)
            await s.commit()
            return [str(i) for i in ids]

    a, b = await asyncio.gather(claim_one(), claim_one())
    assert set(a).isdisjoint(set(b))  # SKIP LOCKED: no row claimed twice
    assert len(a) + len(b) == 2
