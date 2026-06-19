"""Integration tests for the synchronous bulk mint route.

``POST /collections/{id}/items/bulk`` takes a GeoJSON FeatureCollection and mints
a geoid per feature, synchronously, with partial success: valid geometries are
inserted and bad ones are reported with the same reason a single POST returns.
The whole batch always answers 200 (the only non-200s are 413 over the cap, 404
unknown collection, 403 anonymous-into-non-writable, and a 422 envelope error).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

_BULK = "/collections/public/items/bulk"


def _square(x: int, y: int, *, external_id: str | None = None) -> dict:
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


def _fc(*features: dict) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


async def test_bulk_mints_distinct_resolvable_geoids(client):
    resp = await client.post(_BULK, json=_fc(_square(0, 0), _square(5, 5), _square(10, 10)))
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"received": 3, "accepted": 3, "rejected": 0}
    geoids = [a["geoid"] for a in body["accepted"]]
    assert len(set(geoids)) == 3
    # Each minted geoid resolves durably and reports itself.
    for geoid in geoids:
        resolved = await client.get(f"/{geoid}")
        assert resolved.status_code == 200
        assert resolved.json()["properties"]["geoid"] == geoid


async def test_bulk_geoid_matches_single_route_hash(client, unit_square_ccw):
    # Hash parity: the same geometry minted via the single route is recognised as a
    # duplicate by the bulk route, and the conflict names the single-route geoid —
    # proving both paths compute the identical geom_hash.
    single = await client.post("/collections/public/items", json=unit_square_ccw)
    assert single.status_code == 201
    incumbent = single.json()["geoid"]

    resp = await client.post(_BULK, json=_fc(unit_square_ccw))
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"received": 1, "accepted": 0, "rejected": 1}
    rejected = body["rejected"][0]
    assert rejected["reason"] == "geometry_conflict"
    assert rejected["geoid"] == incumbent
    assert rejected["collection"] == "public"


async def test_bulk_in_batch_geometry_twins_collapse(
    client, unit_square_ccw, unit_square_reversed, other_square
):
    # Two encodings of the same canonical geometry in one batch: the first mints,
    # the second collapses onto it (the arbiter sees the in-batch row), the third
    # (distinct) mints. No SAVEPOINT poisoning — the batch survives the conflict.
    resp = await client.post(_BULK, json=_fc(unit_square_ccw, unit_square_reversed, other_square))
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"received": 3, "accepted": 2, "rejected": 1}
    rejected = body["rejected"][0]
    assert rejected["index"] == 1
    assert rejected["reason"] == "geometry_conflict"
    first_accepted = next(a["geoid"] for a in body["accepted"] if a["index"] == 0)
    assert rejected["geoid"] == first_accepted


async def test_bulk_geometry_conflict_against_existing_place(client, unit_square_ccw):
    first = await client.post(_BULK, json=_fc(unit_square_ccw))
    minted = first.json()["accepted"][0]["geoid"]
    # Re-POST the same geometry -> all geometry_conflict against the incumbent.
    again = await client.post(_BULK, json=_fc(unit_square_ccw))
    body = again.json()
    assert body["summary"] == {"received": 1, "accepted": 0, "rejected": 1}
    assert body["rejected"][0]["reason"] == "geometry_conflict"
    assert body["rejected"][0]["geoid"] == minted


async def test_bulk_external_id_conflict_in_batch(client):
    resp = await client.post(
        _BULK, json=_fc(_square(0, 0, external_id="dup"), _square(10, 10, external_id="dup"))
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"received": 2, "accepted": 1, "rejected": 1}
    rejected = body["rejected"][0]
    assert rejected["index"] == 1
    assert rejected["reason"] == "external_id_conflict"
    assert rejected["external_id"] == "dup"


async def test_bulk_external_id_conflict_against_existing(client):
    first = await client.post("/collections/public/items", json=_square(0, 0, external_id="ext1"))
    assert first.status_code == 201
    # Different geometry, same external_id already taken in the collection -> reject.
    resp = await client.post(_BULK, json=_fc(_square(10, 10, external_id="ext1")))
    body = resp.json()
    assert body["summary"] == {"received": 1, "accepted": 0, "rejected": 1}
    assert body["rejected"][0]["reason"] == "external_id_conflict"


async def test_bulk_invalid_geometry_rejected_batch_continues(client):
    # A self-intersecting "bowtie" passes the PlaceCreate schema (valid RFC 7946
    # structure) but fails the ST_IsValid DB CHECK -> invalid_geometry, and the
    # surrounding valid features still mint.
    bowtie = {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]]},
        "properties": {},
    }
    resp = await client.post(_BULK, json=_fc(_square(10, 10), bowtie, _square(20, 20)))
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"received": 3, "accepted": 2, "rejected": 1}
    rejected = body["rejected"][0]
    assert rejected["index"] == 1
    assert rejected["reason"] == "invalid_geometry"
    assert rejected["detail"]  # carries ST_IsValidReason


async def test_bulk_schema_invalid_rejected_batch_continues(client):
    # A Point fails the polygon-only PlaceCreate schema -> one schema_invalid row,
    # never a 422 that sinks the whole request.
    point = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [0, 0]},
        "properties": {},
    }
    resp = await client.post(_BULK, json=_fc(_square(10, 10), point, _square(20, 20)))
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"received": 3, "accepted": 2, "rejected": 1}
    rejected = body["rejected"][0]
    assert rejected["index"] == 1
    assert rejected["reason"] == "schema_invalid"


async def test_bulk_accepts_wkt_string_geometry(client):
    # A WKT-string geometry is decoded by PlaceCreate, so it mints alongside GeoJSON
    # in the same batch (vendor extension; identical canonicalisation).
    wkt_feature = {
        "type": "Feature",
        "geometry": "POLYGON ((50 50, 51 50, 51 51, 50 51, 50 50))",
        "properties": {},
    }
    resp = await client.post(_BULK, json=_fc(_square(0, 0), wkt_feature))
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"received": 2, "accepted": 2, "rejected": 0}
    wkt_geoid = next(a["geoid"] for a in body["accepted"] if a["index"] == 1)
    assert (await client.get(f"/{wkt_geoid}")).status_code == 200


async def test_bulk_over_limit_returns_413(client):
    # A write bound MUST error, never truncate. The cap is checked before any insert.
    resp = await client.post(_BULK, json=_fc(*[_square(0, 0) for _ in range(1001)]))
    assert resp.status_code == 413
    body = resp.json()
    assert body["count"] == 1001
    assert body["limit"] == 1000


async def test_bulk_empty_feature_collection_is_422(client):
    resp = await client.post(_BULK, json={"type": "FeatureCollection", "features": []})
    assert resp.status_code == 422


async def test_bulk_unknown_collection_404(client):
    resp = await client.post("/collections/ghost/items/bulk", json=_fc(_square(0, 0)))
    assert resp.status_code == 404


async def test_bulk_anonymous_into_non_writable_collection_403(client, admin_headers):
    await client.post(
        "/manage/collections",
        headers=admin_headers,
        json={"id": "locked", "writable_anon": False},
    )
    # Anonymous (no token) into a non-writable_anon collection -> fail-fast 403.
    resp = await client.post(
        "/collections/locked/items/bulk", json=_fc(_square(0, 0), _square(5, 5))
    )
    assert resp.status_code == 403
    assert resp.json()["collection"] == "locked"
    # Fail-fast: the 403 happens before any insert, so nothing was written.
    listing = await client.get("/manage/collections/locked/item-ids", headers=admin_headers)
    assert listing.json()["numberMatched"] == 0
