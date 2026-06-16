"""Integration tests for the sync bulk-ingest process (Phase 1).

Exercises the set-based path against real PostGIS: mint + global dedup parity with
the single-row path, per-row partial success across every reject mode, the admin
gate, and ingest_batch_id population.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def _square(x: float, y: float, *, external_id: str | None = None, winding: str = "ccw") -> dict:
    ring_ccw = [[x, y], [x + 1, y], [x + 1, y + 1], [x, y + 1], [x, y]]
    ring = ring_ccw if winding == "ccw" else list(reversed(ring_ccw))
    feature: dict = {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {},
    }
    if external_id is not None:
        feature["id"] = external_id
    return feature


def _execute(collection: str, features: list[dict]) -> dict:
    return {
        "inputs": {
            "collection": collection,
            "items": {"type": "FeatureCollection", "features": features},
        }
    }


async def _ingest(client, admin_headers, collection, features):
    resp = await client.post(
        "/processes/bulk-ingest/execution",
        headers=admin_headers,
        json=_execute(collection, features),
    )
    return resp


async def test_bulk_ingest_mints_distinct_geoids(client, admin_headers):
    features = [_square(10, 10), _square(20, 20), _square(30, 30)]
    resp = await _ingest(client, admin_headers, "public", features)
    assert resp.status_code == 200
    report = resp.json()
    assert report["total"] == 3
    assert report["accepted_count"] == 3
    assert report["rejected_count"] == 0
    geoids = [a["geoid"] for a in report["accepted"]]
    assert len(set(geoids)) == 3
    # Every accepted geoid resolves.
    for a in report["accepted"]:
        got = await client.get(f"/geoid/{a['geoid']}")
        assert got.status_code == 200
    assert report["batch_id"]
    assert report["place_set_uri"].endswith(f"/place-sets/{report['batch_id']}")


async def test_bulk_ingest_requires_admin(client):
    resp = await client.post(
        "/processes/bulk-ingest/execution", json=_execute("public", [_square(1, 1)])
    )
    assert resp.status_code == 401


async def test_bulk_hash_matches_single_row_path(client, admin_headers):
    # A geometry ingested in bulk must dedup against the single-row write path —
    # proves the bulk geom_hash is byte-identical (golden-vector parity).
    report = (await _ingest(client, admin_headers, "public", [_square(40, 40)])).json()
    bulk_geoid = report["accepted"][0]["geoid"]

    # Same square, reversed winding -> identical canonical geometry -> single-row
    # POST fails 409 carrying the bulk-minted incumbent geoid.
    clash = await client.post(
        "/collections/public/items",
        headers=admin_headers,
        json=_square(40, 40, winding="cw"),
    )
    assert clash.status_code == 409
    assert clash.json()["geoid"] == bulk_geoid


async def test_bulk_geometry_conflict_against_existing(client, admin_headers):
    incumbent = (
        await client.post("/collections/public/items", headers=admin_headers, json=_square(50, 50))
    ).json()["geoid"]

    report = (
        await _ingest(client, admin_headers, "public", [_square(50, 50), _square(60, 60)])
    ).json()
    assert report["accepted_count"] == 1
    assert report["rejected_count"] == 1
    reject = report["rejected"][0]
    assert reject["reason"] == "geometry_conflict"
    assert reject["incumbent_geoid"] == incumbent
    assert reject["constraint"] == "uq_geoid_registry_geom_hash"


async def test_bulk_in_batch_geometry_twins_collapse(client, admin_headers):
    features = [_square(70, 70), _square(70, 70, winding="cw"), _square(80, 80)]
    report = (await _ingest(client, admin_headers, "public", features)).json()
    assert report["accepted_count"] == 2  # one twin + the distinct square
    assert report["rejected_count"] == 1
    reject = report["rejected"][0]
    assert reject["reason"] == "geometry_conflict_in_batch"
    assert reject["row_no"] == 1
    # The reported incumbent is the surviving twin (row 0), which was accepted.
    accepted_geoids = {a["geoid"] for a in report["accepted"]}
    assert reject["incumbent_geoid"] in accepted_geoids


async def test_bulk_external_id_conflict_in_batch(client, admin_headers):
    features = [
        _square(11, 11, external_id="dup"),
        _square(12, 12, external_id="dup"),
    ]
    report = (await _ingest(client, admin_headers, "public", features)).json()
    assert report["accepted_count"] == 1
    reject = report["rejected"][0]
    assert reject["reason"] == "external_id_conflict"
    assert reject["constraint"] == "uq_place_collection_external_id"
    assert reject["row_no"] == 1


async def test_bulk_external_id_conflict_against_existing(client, admin_headers):
    await client.post(
        "/collections/public/items",
        headers=admin_headers,
        json=_square(13, 13, external_id="taken"),
    )
    report = (
        await _ingest(client, admin_headers, "public", [_square(14, 14, external_id="taken")])
    ).json()
    assert report["accepted_count"] == 0
    assert report["rejected"][0]["reason"] == "external_id_conflict"


async def test_bulk_self_intersecting_geometry_rejected_batch_continues(client, admin_headers):
    bowtie = {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]]},
        "properties": {},
    }
    report = (await _ingest(client, admin_headers, "public", [_square(45, 45), bowtie])).json()
    assert report["accepted_count"] == 1
    reject = report["rejected"][0]
    assert reject["reason"] == "geometry_invalid"
    assert reject["row_no"] == 1


async def test_bulk_schema_invalid_point_rejected(client, admin_headers):
    point = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [0, 0]},
        "properties": {},
    }
    report = (await _ingest(client, admin_headers, "public", [_square(15, 15), point])).json()
    assert report["accepted_count"] == 1
    assert report["rejected"][0]["reason"] == "schema_invalid"
    assert report["rejected"][0]["row_no"] == 1


async def test_bulk_populates_ingest_batch_id(client, admin_headers, session):
    from sqlalchemy import text

    report = (await _ingest(client, admin_headers, "public", [_square(16, 16)])).json()
    batch_id = report["batch_id"]
    geoid = report["accepted"][0]["geoid"]
    row = (
        await session.execute(
            text("SELECT ingest_batch_id FROM place WHERE id = :id"), {"id": geoid}
        )
    ).first()
    assert str(row[0]) == batch_id


async def test_bulk_by_reference_without_async_is_400(client, admin_headers):
    body = {"inputs": {"collection": "public", "items": {"href": "gs://bucket/blob.geojson"}}}
    resp = await client.post("/processes/bulk-ingest/execution", headers=admin_headers, json=body)
    assert resp.status_code == 400


async def test_bulk_unknown_collection_404(client, admin_headers):
    resp = await _ingest(client, admin_headers, "nope", [_square(1, 1)])
    assert resp.status_code == 404


async def test_bulk_max_features_returns_413(db_clean, admin_headers, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from geoid.config import get_settings
    from geoid.main import create_app

    monkeypatch.setenv("GEOID_BULK_MAX_FEATURES", "2")
    get_settings.cache_clear()
    try:
        transport = ASGITransport(app=create_app())
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            features = [_square(1, 1), _square(2, 2), _square(3, 3)]
            resp = await c.post(
                "/processes/bulk-ingest/execution",
                headers=admin_headers,
                json=_execute("public", features),
            )
            assert resp.status_code == 413
            body = resp.json()
            assert body["count"] == 3
            assert body["limit"] == 2
    finally:
        get_settings.cache_clear()


async def test_respond_async_returns_201_accepted(client, admin_headers):
    # Prefer: respond-async selects the async path (durable job + 201 + Location).
    # The end-to-end worker drain is covered in test_bulk_async.py.
    resp = await client.post(
        "/processes/bulk-ingest/execution",
        headers={**admin_headers, "Prefer": "respond-async"},
        json=_execute("public", [_square(1, 1)]),
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "accepted"
