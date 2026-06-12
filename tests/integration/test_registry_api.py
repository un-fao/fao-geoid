"""Integration tests for the write/registry surface (POST + resolvers + errors)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_post_polygon_mints_geoid_did_uri(client, unit_square_ccw):
    resp = await client.post("/collections/public/items", json=unit_square_ccw)
    assert resp.status_code == 201
    body = resp.json()
    assert body["collection"] == "public"
    geoid = body["geoid"]
    assert body["did"] == f"did:web:data.fao.org:geoid:{geoid}"
    assert body["uri"] == f"http://testserver/geoid/{geoid}"
    assert body["item_url"] == f"http://testserver/collections/public/items/{geoid}"


async def test_anonymous_post_captures_whisp_client_provenance(client):
    feature = {
        "type": "Feature",
        "id": "whisp-plot",
        "geometry": {"type": "Polygon", "coordinates": [[[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]]]},
        "properties": {"_whisp": {"version": "2.1.0"}, "area_ha": 1.0},
    }
    resp = await client.post("/collections/public/items", json=feature)
    assert resp.status_code == 201
    geoid = resp.json()["geoid"]

    feat = (await client.get(f"/geoid/{geoid}")).json()
    # client-submitted attributes round-trip; provenance records the whisp client.
    assert feat["properties"]["area_ha"] == 1.0
    prov = feat["properties"]["_geoid_provenance"]
    assert prov["created_by"] is None  # anonymous
    assert prov["client"]["name"] == "whisp"
    assert prov["client"]["version"] == "2.1.0"
    assert prov["originating_instance"] == "test-instance"


async def test_identical_geometry_returns_409_with_incumbent_geoid(
    client, unit_square_ccw, unit_square_reversed
):
    first = await client.post("/collections/public/items", json=unit_square_ccw)
    assert first.status_code == 201
    original_geoid = first.json()["geoid"]

    # Same square, reversed winding + rotated ring start -> identical canonical
    # geometry -> the insert FAILS (409) and the body carries the incumbent.
    second = await client.post("/collections/public/items", json=unit_square_reversed)
    assert second.status_code == 409
    body = second.json()
    assert body["geoid"] == original_geoid
    assert body["collection"] == "public"
    assert body["constraint"] == "uq_place_geom_hash"
    assert body["did"] == f"did:web:data.fao.org:geoid:{original_geoid}"
    assert body["uri"] == f"http://testserver/geoid/{original_geoid}"
    assert "message" in body


async def test_different_geometry_mints_distinct_geoid(client, unit_square_ccw, other_square):
    a = await client.post("/collections/public/items", json=unit_square_ccw)
    b = await client.post("/collections/public/items", json=other_square)
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["geoid"] != b.json()["geoid"]


async def test_resolve_by_external_id(client):
    feature = {
        "type": "Feature",
        "id": "plot-xyz",
        "geometry": {"type": "Polygon", "coordinates": [[[3, 3], [4, 3], [4, 4], [3, 4], [3, 3]]]},
        "properties": {},
    }
    minted = await client.post("/collections/public/items", json=feature)
    geoid = minted.json()["geoid"]

    resp = await client.get("/collections/public/external/plot-xyz")
    assert resp.status_code == 200
    assert resp.json()["id"] == geoid


async def test_external_id_conflict_returns_409_with_constraint(
    client, unit_square_ccw, other_square
):
    await client.post("/collections/public/items", json={**unit_square_ccw, "id": "dup"})
    clash = await client.post("/collections/public/items", json={**other_square, "id": "dup"})
    assert clash.status_code == 409
    assert clash.json()["constraint"] == "uq_place_collection_external_id"


async def test_invalid_self_intersecting_polygon_returns_422_with_reason(client):
    bowtie = {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]]},
        "properties": {},
    }
    resp = await client.post("/collections/public/items", json=bowtie)
    assert resp.status_code == 422
    assert "reason" in resp.json()


async def test_point_geometry_rejected_422(client):
    point = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [0, 0]},
        "properties": {},
    }
    resp = await client.post("/collections/public/items", json=point)
    assert resp.status_code == 422


async def test_unknown_collection_returns_404(client, unit_square_ccw):
    resp = await client.post("/collections/does-not-exist/items", json=unit_square_ccw)
    assert resp.status_code == 404


async def test_resolve_unknown_geoid_returns_404(client):
    resp = await client.get("/geoid/019e0000-0000-7000-8000-000000000000")
    assert resp.status_code == 404


async def test_anonymous_write_to_managed_collection_forbidden(
    client, admin_headers, unit_square_ccw
):
    await client.post("/manage/catalogs", headers=admin_headers, json={"slug": "ws1"})
    await client.post(
        "/manage/catalogs/ws1/collections",
        headers=admin_headers,
        json={"slug": "managed", "writable_anon": False},
    )
    # anonymous (no auth header) -> 403
    anon = await client.post("/collections/managed/items", json=unit_square_ccw)
    assert anon.status_code == 403
    # admin -> 201
    owned = await client.post(
        "/collections/managed/items", headers=admin_headers, json=unit_square_ccw
    )
    assert owned.status_code == 201
