"""Integration tests for the management slice (1.2) and bulk export (DPG)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_manage_requires_admin_token(client):
    assert (await client.get("/manage/workspaces")).status_code == 401


async def test_manage_rejects_wrong_token(client):
    resp = await client.get("/manage/workspaces", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


async def test_create_workspace_and_collection(client, admin_headers):
    ws = await client.post("/manage/workspaces", headers=admin_headers, json={"slug": "forestry", "title": "Forestry"})
    assert ws.status_code == 201

    coll = await client.post(
        "/manage/workspaces/forestry/collections",
        headers=admin_headers,
        json={"slug": "eudr", "title": "EUDR plots", "writable_anon": False},
    )
    assert coll.status_code == 201
    assert coll.json()["slug"] == "eudr"
    assert coll.json()["writable_anon"] is False


async def test_create_collection_in_unknown_workspace_404(client, admin_headers):
    resp = await client.post(
        "/manage/workspaces/ghost/collections", headers=admin_headers, json={"slug": "c"}
    )
    assert resp.status_code == 404


async def test_list_collections_in_workspace_slice(client, admin_headers):
    await client.post("/manage/workspaces", headers=admin_headers, json={"slug": "ws"})
    await client.post("/manage/workspaces/ws/collections", headers=admin_headers, json={"slug": "a"})
    await client.post("/manage/workspaces/ws/collections", headers=admin_headers, json={"slug": "b"})

    body = (await client.get("/manage/workspaces/ws/collections", headers=admin_headers)).json()
    assert {c["slug"] for c in body} == {"a", "b"}


async def test_list_item_ids_slice(client, admin_headers, unit_square_ccw, other_square):
    await client.post("/collections/public/items", json=unit_square_ccw)
    await client.post("/collections/public/items", json=other_square)

    body = (await client.get("/manage/collections/public/item-ids", headers=admin_headers)).json()
    assert body["numberMatched"] == 2
    assert body["numberReturned"] == 2
    assert len(body["item_ids"]) == 2


async def test_bulk_export_returns_geojson_feature_collection(client, unit_square_ccw, other_square):
    await client.post("/collections/public/items", json=unit_square_ccw)
    await client.post("/collections/public/items", json=other_square)

    resp = await client.get("/collections/public/bulk")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/geo+json")
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) == 2
    for feature in body["features"]:
        assert feature["type"] == "Feature"
        assert feature["geometry"]["type"] == "Polygon"


async def test_bulk_export_unknown_collection_404(client):
    assert (await client.get("/collections/ghost/bulk")).status_code == 404
