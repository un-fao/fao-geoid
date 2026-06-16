"""Integration tests for the async bulk-export process (Phase 6).

The bulk-export PROCESS (distinct from the public ``GET /bulk`` stream) is async-only:
a worker renders the whole collection to one GeoJSON object in storage and the job
result is a time-limited download reference. Covers: async-only enforcement, the
worker render+store+sign round-trip, the result shape, count parity with the seeded
collection, and the deferred-format rejection.
"""

from __future__ import annotations

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


async def _seed(client, admin_headers, collection: str, features: list[dict]) -> None:
    """Mint the seed places via the synchronous bulk-ingest process."""
    body = {"inputs": {"collection": collection, "items": {"features": features}}}
    resp = await client.post("/processes/bulk-ingest/execution", headers=admin_headers, json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted_count"] == len(features)


async def _post_export(client, admin_headers, collection: str, *, fmt: str | None = None):
    inputs: dict = {"collection": collection}
    if fmt is not None:
        inputs["format"] = fmt
    headers = {**admin_headers, "Prefer": "respond-async"}
    return await client.post(
        "/processes/bulk-export/execution", headers=headers, json={"inputs": inputs}
    )


async def _run_worker():
    from geoid.config import get_settings
    from geoid.services import ingest_service

    return await ingest_service.process_pending_jobs(get_settings())


async def test_bulk_export_is_listed_as_a_process(client):
    resp = await client.get("/processes")
    assert resp.status_code == 200
    assert "bulk-export" in [p["id"] for p in resp.json()["processes"]]


async def test_bulk_export_requires_async_execution(client, admin_headers):
    await _seed(client, admin_headers, "public", [_square(10, 10)])
    # No Prefer: respond-async -> 400 (a large export must not stream inline).
    resp = await client.post(
        "/processes/bulk-export/execution",
        headers=admin_headers,
        json={"inputs": {"collection": "public"}},
    )
    assert resp.status_code == 400
    assert "respond-async" in resp.text


async def test_bulk_export_requires_admin(client, admin_headers):
    await _seed(client, admin_headers, "public", [_square(11, 11)])
    resp = await client.post(
        "/processes/bulk-export/execution",
        headers={"Prefer": "respond-async"},
        json={"inputs": {"collection": "public"}},
    )
    assert resp.status_code == 401


async def test_bulk_export_unknown_collection_404(client, admin_headers):
    resp = await _post_export(client, admin_headers, "no-such-collection")
    assert resp.status_code == 404


async def test_bulk_export_accepts_async_and_returns_location(client, admin_headers):
    await _seed(client, admin_headers, "public", [_square(12, 12)])
    resp = await _post_export(client, admin_headers, "public")
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "accepted"
    assert resp.headers["location"].endswith(f"/jobs/{body['jobID']}")


async def test_bulk_export_worker_writes_object_and_signs_url(client, admin_headers):
    from geoid.storage import get_blob_store

    await _seed(client, admin_headers, "public", [_square(20, 20), _square(21, 21)])
    resp = await _post_export(client, admin_headers, "public")
    job_id = resp.json()["jobID"]

    claimed = await _run_worker()
    assert job_id in [str(c) for c in claimed]

    status = (await client.get(f"/jobs/{job_id}", headers=admin_headers)).json()
    assert status["status"] == "successful"

    result = (await client.get(f"/jobs/{job_id}/results", headers=admin_headers)).json()
    assert result["format"] == "geojson"
    assert result["type"] == "application/geo+json"
    assert result["count"] == 2
    assert result["collection"] == "public"
    assert result["href"]  # a file URI (local backend) or a signed URL (gcs)

    # The stored object is a complete, valid GeoJSON FeatureCollection.
    raw = await get_blob_store().get(f"exports/{job_id}.geojson")
    fc = json.loads(raw)
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 2
    assert all(f["type"] == "Feature" and "geoid" in f["properties"] for f in fc["features"])


async def test_bulk_export_empty_collection_yields_zero_count(client, admin_headers):
    # 'public' exists (bootstrap) but is empty in a fresh test DB.
    resp = await _post_export(client, admin_headers, "public")
    job_id = resp.json()["jobID"]
    await _run_worker()
    result = (await client.get(f"/jobs/{job_id}/results", headers=admin_headers)).json()
    assert result["count"] == 0


async def test_bulk_export_deferred_format_fails_with_clear_message(client, admin_headers):
    await _seed(client, admin_headers, "public", [_square(30, 30)])
    # 'geoparquet' is a valid enum value (accepted at parse) but not yet rendered:
    # the worker marks the job failed rather than silently producing nothing.
    resp = await _post_export(client, admin_headers, "public", fmt="geoparquet")
    assert resp.status_code == 201
    job_id = resp.json()["jobID"]

    await _run_worker()
    status = (await client.get(f"/jobs/{job_id}", headers=admin_headers)).json()
    assert status["status"] == "failed"
    assert "geoparquet" in (status["message"] or "").lower()
    # A failed job has no results.
    assert (await client.get(f"/jobs/{job_id}/results", headers=admin_headers)).status_code == 404
