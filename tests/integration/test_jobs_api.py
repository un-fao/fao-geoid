"""Integration tests for the async-import API surface.

Submit: ``POST /collections/{id}/items/import`` → 201 + Location + statusInfo
(18-062r2 Req 34). Read: ``GET /jobs/{id}`` (creator-or-sysadmin; everyone else
gets the masked ``no-such-job`` 404) and ``/results``. The executor is
monkeypatched to a recorder — no worker runs here (see test_import_worker.py).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from geoid.db import get_sessionmaker

pytestmark = pytest.mark.integration

_IMPORT = "/collections/public/items/import"
_HREF = {"href": "https://storage.googleapis.com/bucket/data.json?X-Goog-Signature=SECRET"}


@pytest.fixture
def launch_recorder(monkeypatch):
    """Record executor dispatches instead of running anything."""
    from geoid.services import job_executor

    calls: list[uuid.UUID] = []

    async def _record(settings, job_id):
        calls.append(job_id)
        return f"projects/p/locations/r/jobs/geoid-import/executions/exec-{len(calls)}"

    monkeypatch.setattr(job_executor, "launch", _record)
    return calls


async def test_submit_answers_201_location_and_accepted_status_info(
    client, admin_headers, launch_recorder
):
    resp = await client.post(_IMPORT, json=_HREF, headers=admin_headers)
    assert resp.status_code == 201
    body = resp.json()
    job_id = body["jobID"]
    uuid.UUID(job_id)  # a real id
    assert body["status"] == "accepted"
    assert body["type"] == "process"
    assert body["processID"] == "import"
    assert "message" not in body  # exclude_none: absent optionals are omitted
    assert resp.headers["Location"] == f"http://testserver/jobs/{job_id}"
    assert launch_recorder == [uuid.UUID(job_id)]


async def test_submit_stores_the_execution_name(client, admin_headers, launch_recorder):
    resp = await client.post(_IMPORT, json=_HREF, headers=admin_headers)
    job_id = resp.json()["jobID"]
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (
            await session.execute(
                text("SELECT execution_name, source_ref FROM import_job WHERE id = :id"),
                {"id": job_id},
            )
        ).one()
    assert row.execution_name.endswith("/executions/exec-1")
    assert "SECRET" in row.source_ref  # stored for the worker (never echoed)


async def test_submit_is_authenticated_only_but_inline_bulk_stays_anonymous(
    client, launch_recorder
):
    resp = await client.post(_IMPORT, json=_HREF)
    assert resp.status_code == 401
    assert launch_recorder == []
    # Regression pin: the anonymous sync bulk path into public_write is untouched.
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                },
                "properties": {},
            }
        ],
    }
    bulk = await client.post("/collections/public/items/bulk", json=fc)
    assert bulk.status_code == 200


async def test_submit_to_unknown_collection_is_404(client, admin_headers, launch_recorder):
    resp = await client.post("/collections/nope/items/import", json=_HREF, headers=admin_headers)
    assert resp.status_code == 404
    assert launch_recorder == []


async def test_non_writer_gets_403(oidc_client, admin_headers, make_token, bearer, monkeypatch):
    from geoid.services import job_executor

    async def _record(settings, job_id):
        return None

    monkeypatch.setattr(job_executor, "launch", _record)
    created = await oidc_client.post(
        "/manage/collections",
        json={"id": "restricted", "title": "R", "public_write": False},
        headers=admin_headers,
    )
    assert created.status_code == 201
    token = make_token(sub="kc-user", email="user@example.org")
    resp = await oidc_client.post(
        "/collections/restricted/items/import", json=_HREF, headers=bearer(token)
    )
    assert resp.status_code == 403


async def test_bad_refs_are_422(client, admin_headers, launch_recorder):
    for payload in (
        {"href": "https://evil.example.com/data.json"},  # host not allowlisted
        {"href": "http://storage.googleapis.com/b/o.json"},  # scheme
        {"prefix": "gs://any-bucket/path/"},  # gs disabled (empty bucket allowlist)
        {"href": "https://storage.googleapis.com/b/o.json", "prefix": "gs://b/p/"},  # both
        {},  # neither
    ):
        resp = await client.post(_IMPORT, json=payload, headers=admin_headers)
        assert resp.status_code == 422, payload
    assert launch_recorder == []


async def test_concurrency_valve_answers_429(client, admin_headers, launch_recorder):
    for _ in range(3):  # GEOID_JOB_MAX_CONCURRENT default
        assert (await client.post(_IMPORT, json=_HREF, headers=admin_headers)).status_code == 201
    resp = await client.post(_IMPORT, json=_HREF, headers=admin_headers)
    assert resp.status_code == 429
    body = resp.json()
    assert body["active"] == 3 and body["limit"] == 3


async def test_launch_failure_leaves_an_auditable_failed_row(client, admin_headers, monkeypatch):
    from geoid.services import job_executor

    async def _explode(settings, job_id):
        raise RuntimeError("permission denied")

    monkeypatch.setattr(job_executor, "launch", _explode)
    resp = await client.post(_IMPORT, json=_HREF, headers=admin_headers)
    assert resp.status_code == 500
    job_id = resp.json()["job_id"]
    status = await client.get(f"/jobs/{job_id}", headers=admin_headers)
    assert status.status_code == 200
    body = status.json()
    assert body["status"] == "failed"
    assert body["message"] == "failed to start import execution"


async def test_creator_and_admin_read_status_others_get_masked_404(
    oidc_client, admin_headers, make_token, bearer, monkeypatch
):
    from geoid.services import job_executor

    async def _record(settings, job_id):
        return None

    monkeypatch.setattr(job_executor, "launch", _record)
    creator = make_token(sub="creator-sub", email="creator@example.org")
    resp = await oidc_client.post(_IMPORT, json=_HREF, headers=bearer(creator))
    assert resp.status_code == 201
    job_id = resp.json()["jobID"]

    own = await oidc_client.get(f"/jobs/{job_id}", headers=bearer(creator))
    assert own.status_code == 200
    assert own.json()["status"] == "accepted"

    admin = await oidc_client.get(f"/jobs/{job_id}", headers=admin_headers)
    assert admin.status_code == 200

    other = make_token(sub="other-sub", email="other@example.org")
    masked = await oidc_client.get(f"/jobs/{job_id}", headers=bearer(other))
    assert masked.status_code == 404
    body = masked.json()
    assert body["type"].endswith("/no-such-job")
    assert body["status"] == 404

    anonymous = await oidc_client.get(f"/jobs/{job_id}")
    assert anonymous.status_code == 404
    assert anonymous.json()["type"].endswith("/no-such-job")


async def test_unknown_job_id_is_the_same_no_such_job_404(client, admin_headers):
    resp = await client.get(f"/jobs/{uuid.uuid4()}", headers=admin_headers)
    assert resp.status_code == 404
    assert resp.json()["type"].endswith("/no-such-job")


async def test_results_before_completion_is_result_not_ready(
    client, admin_headers, launch_recorder
):
    resp = await client.post(_IMPORT, json=_HREF, headers=admin_headers)
    job_id = resp.json()["jobID"]
    results = await client.get(f"/jobs/{job_id}/results", headers=admin_headers)
    assert results.status_code == 404
    assert results.json()["type"].endswith("/result-not-ready")


async def test_status_carries_self_link_and_results_link_when_successful(
    client, admin_headers, launch_recorder
):
    resp = await client.post(_IMPORT, json=_HREF, headers=admin_headers)
    job_id = resp.json()["jobID"]
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE import_job SET status='successful', report='{\"summary\": {}}'::jsonb, "
                "finished_at=now() WHERE id=:id"
            ),
            {"id": job_id},
        )
        await session.commit()
    status = await client.get(f"/jobs/{job_id}", headers=admin_headers)
    links = {link["rel"]: link["href"] for link in status.json()["links"]}
    assert links["self"] == f"http://testserver/jobs/{job_id}"
    assert links["http://www.opengis.net/def/rel/ogc/1.0/results"].endswith(
        f"/jobs/{job_id}/results"
    )
    results = await client.get(f"/jobs/{job_id}/results", headers=admin_headers)
    assert results.status_code == 200
    assert results.json() == {"summary": {}}


async def test_failed_job_results_answer_500_with_the_stored_message(
    client, admin_headers, launch_recorder
):
    resp = await client.post(_IMPORT, json=_HREF, headers=admin_headers)
    job_id = resp.json()["jobID"]
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE import_job SET status='failed', message='upstream answered 403', "
                "finished_at=now() WHERE id=:id"
            ),
            {"id": job_id},
        )
        await session.commit()
    status = await client.get(f"/jobs/{job_id}", headers=admin_headers)
    links = {link["rel"] for link in status.json()["links"]}
    assert "http://www.opengis.net/def/rel/ogc/1.0/exceptions" in links
    results = await client.get(f"/jobs/{job_id}/results", headers=admin_headers)
    assert results.status_code == 500
    body = results.json()
    assert body["detail"] == "upstream answered 403"
    assert body["status"] == 500


async def test_on_read_reaper_flips_stale_running_jobs(client, admin_headers, launch_recorder):
    resp = await client.post(_IMPORT, json=_HREF, headers=admin_headers)
    job_id = resp.json()["jobID"]
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE import_job SET status='running', started_at=now(), "
                "updated_at = now() - interval '30 minutes' WHERE id=:id"
            ),
            {"id": job_id},
        )
        await session.commit()
    status = await client.get(f"/jobs/{job_id}", headers=admin_headers)
    body = status.json()
    assert body["status"] == "failed"
    assert "died or timed out" in body["message"]


async def test_on_read_reaper_flips_never_started_jobs(client, admin_headers, launch_recorder):
    resp = await client.post(_IMPORT, json=_HREF, headers=admin_headers)
    job_id = resp.json()["jobID"]
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(
            text("UPDATE import_job SET created_at = now() - interval '30 minutes' WHERE id=:id"),
            {"id": job_id},
        )
        await session.commit()
    status = await client.get(f"/jobs/{job_id}", headers=admin_headers)
    body = status.json()
    assert body["status"] == "failed"
    assert "never started" in body["message"]
