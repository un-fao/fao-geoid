"""Integration tests for the import worker (``run_job`` against the container DB).

The network seams are faked (httpx MockTransport / a fake GCS client); everything
else — claim CAS, chunked minting through ``create_places_bulk``, heartbeats,
report aggregation, failure paths — runs for real against PostGIS.
"""

from __future__ import annotations

import io
import json

import pytest
from sqlalchemy import func, select, text

from geoid.db import get_sessionmaker
from geoid.models import Collection, Place
from geoid.repositories import job_repo
from geoid.services import import_service

pytestmark = pytest.mark.integration

_URL = "https://storage.googleapis.com/test-bucket/data.json?X-Goog-Signature=SECRET"
_CLEAN_URL = "https://storage.googleapis.com/test-bucket/data.json"


@pytest.fixture
def job_env(monkeypatch):
    """Override GEOID_JOB_* env for the worker's get_settings() read."""
    from geoid.config import get_settings

    def _apply(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        get_settings.cache_clear()

    yield _apply
    get_settings.cache_clear()


def _square(x: float, y: float, *, external_id: str | None = None) -> dict:
    feature = {
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


def _fc(*features: dict) -> bytes:
    return json.dumps({"type": "FeatureCollection", "features": list(features)}).encode()


def _transport(payload: bytes, status_code: int = 200):
    import httpx

    return httpx.MockTransport(lambda request: httpx.Response(status_code, content=payload))


async def _make_job(session, *, ref=_URL, is_prefix=False, created_by="importer-1", email=None, admin=False, collection="public"):
    collection_id = (
        await session.execute(select(Collection.id).where(Collection.slug == collection))
    ).scalar_one()
    job = await job_repo.insert_job(
        session,
        collection_id=collection_id,
        source_ref=ref,
        source_is_prefix=is_prefix,
        created_by=created_by,
        created_by_email=email,
        created_by_admin=admin,
    )
    await session.commit()
    return job.id


async def _row(job_id):
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        job = await job_repo.get(session, job_id)
        report = await job_repo.get_report(session, job_id)
    return job, report


async def _place_count(session) -> int:
    return (await session.execute(select(func.count(Place.id)))).scalar_one()


# --- Fake GCS ----------------------------------------------------------------


class _FakeBlob:
    def __init__(self, name: str, data: bytes) -> None:
        self.name = name
        self._data = data

    def open(self, mode: str):
        assert mode == "rb"
        return io.BytesIO(self._data)


class _FakeBucket:
    def __init__(self, blobs: dict[str, _FakeBlob]) -> None:
        self._blobs = blobs

    def blob(self, name: str) -> _FakeBlob:
        return self._blobs[name]


class _FakeStorageClient:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = {name: _FakeBlob(name, data) for name, data in blobs.items()}

    def list_blobs(self, bucket: str, prefix: str | None = None):
        return [b for name, b in sorted(self._blobs.items()) if name.startswith(prefix or "")]

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(self._blobs)


# --- The happy paths ---------------------------------------------------------


async def test_mixed_outcomes_produce_a_successful_job_with_a_sync_shaped_report(session):
    payload = _fc(
        _square(0, 0),
        _square(5, 5),
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}, "properties": {}},
        _square(0, 0),  # in-batch duplicate of the first
    )
    job_id = await _make_job(session)
    exit_code = await import_service.run_job(str(job_id), transport=_transport(payload))
    assert exit_code == 0

    job, report = await _row(job_id)
    assert job.status == "successful"
    assert job.message is None
    assert report["summary"] == {"files": 1, "received": 4, "accepted": 2, "rejected": 2}
    (file_report,) = report["files"]
    assert file_report["source"] == _CLEAN_URL  # query (bearer secret) stripped
    assert "SECRET" not in json.dumps(report)
    # Per-feature rows are the sync BulkReport rows, verbatim shape.
    accepted = {a["index"]: a for a in file_report["accepted"]}
    assert set(accepted) == {0, 1}
    assert all({"index", "geoid", "uri"} <= set(a) for a in accepted.values())
    rejects = {r["index"]: r for r in file_report["rejected"]}
    assert rejects[2]["reason"] == "schema_invalid"
    assert rejects[3]["reason"] == "geometry_conflict"
    assert rejects[3]["geoid"] == accepted[0]["geoid"]  # disclosed: public collection
    # The two winners are durably in place.
    assert await _place_count(session) == 2


async def test_chunked_minting_reindexes_against_the_whole_file(session, monkeypatch):
    monkeypatch.setattr(import_service, "CHUNK_SIZE", 2)
    payload = _fc(*[_square(i * 3, i * 3) for i in range(5)])
    job_id = await _make_job(session)
    assert await import_service.run_job(str(job_id), transport=_transport(payload)) == 0
    _, report = await _row(job_id)
    (file_report,) = report["files"]
    assert [a["index"] for a in file_report["accepted"]] == [0, 1, 2, 3, 4]
    assert await _place_count(session) == 5


async def test_prefix_job_ingests_every_matching_blob_with_per_file_reports(session, job_env):
    job_env(GEOID_JOB_ALLOWED_BUCKETS="test-bucket")
    square_a = _square(0, 0)
    blobs = {
        "batch/a.geojson": _fc(square_a, _square(5, 5)),
        "batch/b.ndjson": json.dumps(_square(10, 10)).encode()
        + b"\n"
        + json.dumps(square_a).encode()  # duplicate of a.geojson's first feature
        + b"\n",
        "batch/c.json": _fc(_square(20, 20)),
        "batch/readme.txt": b"not geojson",
    }
    job_id = await _make_job(session, ref="gs://test-bucket/batch/", is_prefix=True)
    exit_code = await import_service.run_job(
        str(job_id), storage_client_factory=lambda: _FakeStorageClient(blobs)
    )
    assert exit_code == 0

    job, report = await _row(job_id)
    assert job.status == "successful"
    assert job.progress == 100
    assert report["summary"] == {"files": 3, "received": 5, "accepted": 4, "rejected": 1}
    sources = [f["source"] for f in report["files"]]
    assert sources == [
        "gs://test-bucket/batch/a.geojson",
        "gs://test-bucket/batch/b.ndjson",
        "gs://test-bucket/batch/c.json",
    ]
    b_report = report["files"][1]
    assert [r["index"] for r in b_report["rejected"]] == [1]  # within-file index
    assert b_report["rejected"][0]["reason"] == "geometry_conflict"


async def test_rerun_converges_via_dedup_with_disclosed_incumbents(session):
    payload = _fc(_square(0, 0), _square(5, 5))
    first = await _make_job(session)
    assert await import_service.run_job(str(first), transport=_transport(payload)) == 0

    second = await _make_job(session)
    assert await import_service.run_job(str(second), transport=_transport(payload)) == 0
    job, report = await _row(second)
    assert job.status == "successful"  # rejects don't fail the JOB
    assert report["summary"]["accepted"] == 0
    assert report["summary"]["rejected"] == 2
    for reject in report["files"][0]["rejected"]:
        assert reject["reason"] == "geometry_conflict"
        assert reject["geoid"]  # own mint / public collection → disclosed
    assert await _place_count(session) == 2


# --- Disclosure parity with the sync path ------------------------------------


async def test_conflict_disclosure_matches_may_disclose_incumbent(
    session, client, admin_headers
):
    created = await client.post(
        "/manage/collections",
        json={"id": "vault", "title": "Vault", "writable_anon": False, "public_read": False},
        headers=admin_headers,
    )
    assert created.status_code == 201
    minted = await client.post(
        "/collections/vault/items", json=_square(0, 0), headers=admin_headers
    )
    assert minted.status_code == 201
    incumbent = minted.json()["geoid"]

    payload = _fc(_square(0, 0))
    # A plain authenticated caller: incumbent is private, not theirs, no grant → masked.
    masked_job = await _make_job(session, created_by="importer-1")
    assert await import_service.run_job(str(masked_job), transport=_transport(payload)) == 0
    _, masked_report = await _row(masked_job)
    reject = masked_report["files"][0]["rejected"][0]
    assert reject["reason"] == "geometry_conflict"
    assert reject["geoid"] is None and reject["collection"] is None

    # A sysadmin-created job: disclosed, naming the private incumbent.
    admin_job = await _make_job(session, created_by="admin", admin=True)
    assert await import_service.run_job(str(admin_job), transport=_transport(payload)) == 0
    _, admin_report = await _row(admin_job)
    reject = admin_report["files"][0]["rejected"][0]
    assert reject["geoid"] == incumbent
    assert reject["collection"] == "vault"


# --- Failure paths -----------------------------------------------------------


async def test_upstream_403_fails_the_job_with_a_sanitized_message(session):
    job_id = await _make_job(session)
    exit_code = await import_service.run_job(
        str(job_id), transport=_transport(b"denied", status_code=403)
    )
    assert exit_code == 1
    job, report = await _row(job_id)
    assert job.status == "failed"
    assert "403" in job.message
    assert "SECRET" not in job.message
    assert report is None


async def test_oversize_source_fails_the_job(session, job_env):
    job_env(GEOID_JOB_MAX_BYTES="10")
    job_id = await _make_job(session)
    payload = _fc(_square(0, 0))
    assert await import_service.run_job(str(job_id), transport=_transport(payload)) == 1
    job, _ = await _row(job_id)
    assert job.status == "failed"
    assert "GEOID_JOB_MAX_BYTES" in job.message


async def test_malformed_document_fails_the_job(session):
    job_id = await _make_job(session)
    assert await import_service.run_job(str(job_id), transport=_transport(b"{ not json")) == 1
    job, _ = await _row(job_id)
    assert job.status == "failed"
    assert "parsing" in job.message
    assert await _place_count(session) == 0


async def test_feature_cap_fails_the_job_mid_stream(session, job_env):
    job_env(GEOID_JOB_MAX_FEATURES="3")
    payload = _fc(*[_square(i * 3, i * 3) for i in range(5)])
    job_id = await _make_job(session)
    assert await import_service.run_job(str(job_id), transport=_transport(payload)) == 1
    job, _ = await _row(job_id)
    assert job.status == "failed"
    assert "GEOID_JOB_MAX_FEATURES" in job.message


async def test_empty_prefix_fails_the_job(session, job_env):
    job_env(GEOID_JOB_ALLOWED_BUCKETS="test-bucket")
    job_id = await _make_job(session, ref="gs://test-bucket/nothing/", is_prefix=True)
    exit_code = await import_service.run_job(
        str(job_id), storage_client_factory=lambda: _FakeStorageClient({})
    )
    assert exit_code == 1
    job, _ = await _row(job_id)
    assert job.status == "failed"
    assert "no matching objects" in job.message


async def test_worker_revalidates_the_ref(session):
    # A row whose ref would no longer pass validation (defense in depth) fails.
    job_id = await _make_job(session, ref="https://evil.example.com/data.json")
    assert await import_service.run_job(str(job_id)) == 1
    job, _ = await _row(job_id)
    assert job.status == "failed"
    assert "source ref rejected" in job.message


# --- Idempotency + external-flip guards --------------------------------------


async def test_second_run_is_a_no_op_on_a_claimed_job(session):
    job_id = await _make_job(session)
    claimed = await job_repo.claim(session, job_id)
    await session.commit()
    assert claimed is not None
    # A duplicate dispatch exits 0 and leaves the row exactly as it was.
    assert await import_service.run_job(str(job_id), transport=_transport(b"x")) == 0
    job, report = await _row(job_id)
    assert job.status == "running"
    assert report is None


async def test_heartbeat_guard_aborts_after_an_external_flip(session, monkeypatch):
    from geoid.services import registry_service

    job_id = await _make_job(session)
    real_bulk = registry_service.create_places_bulk

    async def _flip_then_mint(inner_session, **kwargs):
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as flip_session:
            await flip_session.execute(
                text(
                    "UPDATE import_job SET status='failed', message='externally cancelled' "
                    "WHERE id=:id"
                ),
                {"id": job_id},
            )
            await flip_session.commit()
        return await real_bulk(inner_session, **kwargs)

    monkeypatch.setattr(registry_service, "create_places_bulk", _flip_then_mint)
    payload = _fc(_square(0, 0), _square(5, 5))
    assert await import_service.run_job(str(job_id), transport=_transport(payload)) == 1

    job, report = await _row(job_id)
    assert job.status == "failed"
    assert job.message == "externally cancelled"  # the zombie never overwrote it
    assert report is None
    # The aborted chunk's mints rolled back with it.
    assert await _place_count(session) == 0
