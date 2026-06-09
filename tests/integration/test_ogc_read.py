"""Integration tests for the OGC API Features read surface."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


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


async def _seed(client, n: int) -> None:
    for i in range(n):
        resp = await client.post("/collections/public/items", json=_square(i * 2, i * 2, external_id=f"f{i}"))
        assert resp.status_code == 201


async def test_landing_page_has_required_links(client):
    body = (await client.get("/")).json()
    rels = {link["rel"] for link in body["links"]}
    assert {"self", "conformance", "data"} <= rels


async def test_conformance_declares_core_and_cql2(client):
    body = (await client.get("/conformance")).json()
    classes = body["conformsTo"]
    assert any("ogcapi-features-1/1.0/conf/core" in c for c in classes)
    assert any("cql2-text" in c for c in classes)


async def test_collections_lists_public(client):
    body = (await client.get("/collections")).json()
    ids = {c["id"] for c in body["collections"]}
    assert "public" in ids


async def test_describe_collection(client):
    body = (await client.get("/collections/public")).json()
    assert body["id"] == "public"
    assert body["itemType"] == "feature"
    assert any(link["rel"] == "items" for link in body["links"])


async def test_items_envelope_is_dynastore_shaped(client):
    await _seed(client, 2)
    body = (await client.get("/collections/public/items")).json()
    assert body["type"] == "FeatureCollection"
    assert body["numberMatched"] == 2
    assert body["numberReturned"] == 2
    assert "timeStamp" in body
    assert any(link["rel"] == "self" for link in body["links"])
    assert len(body["features"]) == 2


async def test_items_paging_with_next_and_prev_links(client):
    await _seed(client, 3)
    page1 = (await client.get("/collections/public/items?limit=2&offset=0")).json()
    assert page1["numberMatched"] == 3
    assert page1["numberReturned"] == 2
    assert any(link["rel"] == "next" for link in page1["links"])

    page2 = (await client.get("/collections/public/items?limit=2&offset=2")).json()
    assert page2["numberReturned"] == 1
    assert any(link["rel"] == "prev" for link in page2["links"])
    assert not any(link["rel"] == "next" for link in page2["links"])


async def test_bbox_filters_features(client):
    await _seed(client, 3)  # squares at (0,0),(2,2),(4,4)
    inside = (await client.get("/collections/public/items?bbox=-1,-1,1.5,1.5")).json()
    assert inside["numberMatched"] == 1
    none = (await client.get("/collections/public/items?bbox=100,100,101,101")).json()
    assert none["numberMatched"] == 0


async def test_cql2_text_filter_on_external_id(client):
    await _seed(client, 3)
    body = (await client.get("/collections/public/items?filter=external_id='f1'")).json()
    assert body["numberMatched"] == 1
    assert body["features"][0]["properties"]["external_id"] == "f1"


async def test_invalid_cql2_filter_returns_400(client):
    await _seed(client, 1)
    resp = await client.get("/collections/public/items?filter=this is not cql2 (((")
    assert resp.status_code == 400


async def test_invalid_bbox_returns_400(client):
    resp = await client.get("/collections/public/items?bbox=1,2,3")
    assert resp.status_code == 400


async def test_get_item_by_geoid(client, unit_square_ccw):
    geoid = (await client.post("/collections/public/items", json=unit_square_ccw)).json()["geoid"]
    body = (await client.get(f"/collections/public/items/{geoid}")).json()
    assert body["id"] == geoid
    assert body["geometry"]["type"] == "Polygon"
    assert any(link["rel"] == "self" for link in body["links"])


async def test_get_item_wrong_collection_returns_404(client, admin_headers, unit_square_ccw):
    geoid = (await client.post("/collections/public/items", json=unit_square_ccw)).json()["geoid"]
    await client.post("/manage/workspaces", headers=admin_headers, json={"slug": "wsx"})
    await client.post(
        "/manage/workspaces/wsx/collections",
        headers=admin_headers,
        json={"slug": "elsewhere"},
    )
    resp = await client.get(f"/collections/elsewhere/items/{geoid}")
    assert resp.status_code == 404


async def test_get_item_bad_uuid_returns_422(client):
    resp = await client.get("/collections/public/items/not-a-uuid")
    assert resp.status_code == 422
