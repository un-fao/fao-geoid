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
        resp = await client.post(
            "/collections/public/items", json=_square(i * 2, i * 2, external_id=f"f{i}")
        )
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


async def test_conformance_declares_queryables(client):
    classes = (await client.get("/conformance")).json()["conformsTo"]
    assert any(c.endswith("ogcapi-features-3/1.0/conf/queryables") for c in classes)


async def test_queryables_is_a_json_schema_of_the_cql2_fields(client):
    resp = await client.get("/collections/public/queryables")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/schema+json")
    body = resp.json()
    assert body["type"] == "object"
    assert body["$id"].endswith("/collections/public/queryables")
    assert body["additionalProperties"] is False
    advertised = set(body["properties"])
    assert {"geoid", "external_id", "created_at", "geometry"} == advertised
    # An advertised queryable must be accepted by the live filter path.
    resp = await client.get("/collections/public/items?filter=external_id='nope'")
    assert resp.status_code == 200
    # The collection description links to its queryables (OGC Part-3 requirement).
    desc = (await client.get("/collections/public")).json()
    assert any(link["rel"].endswith("queryables") for link in desc["links"])


async def test_queryables_alias_under_items_path(client):
    resp = await client.get("/collections/public/items/queryables")
    assert resp.status_code == 200
    assert resp.json()["properties"]


async def test_queryables_unknown_collection_returns_404(client):
    resp = await client.get("/collections/nope/queryables")
    assert resp.status_code == 404


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


async def test_unknown_queryable_returns_400_not_silent_empty(client):
    await _seed(client, 1)
    resp = await client.get("/collections/public/items?filter=bogus_field='x'")
    assert resp.status_code == 400
    assert "queryable" in resp.json()["detail"]
    # The former internal 'geom' alias is no longer accepted either — the
    # queryables document is a closed set and 'geometry' is the advertised name.
    resp = await client.get(
        "/collections/public/items",
        params={"filter": "S_INTERSECTS(geom,POLYGON((0 0,1 0,1 1,0 1,0 0)))"},
    )
    assert resp.status_code == 400


async def test_spatial_filter_on_geometry_works(client, unit_square_ccw):
    await client.post("/collections/public/items", json=unit_square_ccw)
    resp = await client.get(
        "/collections/public/items",
        params={"filter": "S_INTERSECTS(geometry,POLYGON((9 9,12 9,12 12,9 12,9 9)))"},
    )
    assert resp.status_code == 200
    assert resp.json()["numberMatched"] == 1


async def test_filter_value_type_mismatch_returns_400_not_500(client):
    await _seed(client, 1)
    # Both pass parse + translate and only fail at bind/execute time.
    resp = await client.get("/collections/public/items?filter=geoid='not-a-uuid'")
    assert resp.status_code == 400
    resp = await client.get(
        "/collections/public/items", params={"filter": "created_at > 'lastweek'"}
    )
    assert resp.status_code == 400


async def test_invalid_bbox_returns_400(client):
    resp = await client.get("/collections/public/items?bbox=1,2,3")
    assert resp.status_code == 400


async def test_offset_beyond_cap_returns_400(client):
    resp = await client.get("/collections/public/items?offset=999999999")
    assert resp.status_code == 400
    assert "offset" in resp.json()["detail"]


async def test_offset_cap_precedes_collection_resolution(client):
    # Pure parameter validation runs before any I/O, on both surfaces — so an
    # unknown collection with an over-cap offset 400s here exactly as it does
    # on /manage item-ids.
    resp = await client.get("/collections/no-such-collection/items?offset=999999999")
    assert resp.status_code == 400


async def test_get_item_by_geoid(client, unit_square_ccw):
    geoid = (await client.post("/collections/public/items", json=unit_square_ccw)).json()["geoid"]
    body = (await client.get(f"/collections/public/items/{geoid}")).json()
    assert body["id"] == geoid
    assert body["geometry"]["type"] == "Polygon"
    assert any(link["rel"] == "self" for link in body["links"])


async def test_get_item_wrong_collection_returns_404(client, admin_headers, unit_square_ccw):
    geoid = (await client.post("/collections/public/items", json=unit_square_ccw)).json()["geoid"]
    await client.post("/manage/catalogs", headers=admin_headers, json={"slug": "wsx"})
    await client.post(
        "/manage/catalogs/wsx/collections",
        headers=admin_headers,
        json={"slug": "elsewhere"},
    )
    resp = await client.get(f"/collections/elsewhere/items/{geoid}")
    assert resp.status_code == 404


async def test_get_item_bad_uuid_returns_422(client):
    resp = await client.get("/collections/public/items/not-a-uuid")
    assert resp.status_code == 422
