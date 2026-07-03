"""Integration: non-public collections (``public_read=false``) end to end.

Covers the four 2026-07-03 surfaces: 404-masking on the external-id resolver
only (existence-masked, the body identical to an unknown id's — ``GET /{geoid}``
is deliberately ungated: the geoid is the capability), caller-aware dedup-409
disclosure (single + bulk), ``GET /me/geoids``, and the sysadmin ``/manage``
item inventory.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

ADMIN = {"Authorization": "Bearer test-admin-token"}
CONFLICT_MESSAGE = "identical geometry already exists in the catalog"


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


async def _create(client, slug: str, **over) -> None:
    body = {"id": slug, "writable_anon": False, **over}
    resp = await client.post("/manage/collections", headers=ADMIN, json=body)
    assert resp.status_code == 201, resp.text


async def _grant(client, slug: str, email: str, role: str) -> None:
    resp = await client.post(
        f"/collections/{slug}/grants", headers=ADMIN, json={"email": email, "role": role}
    )
    assert resp.status_code == 201, resp.text


async def _mint(client, slug: str, feature: dict, headers: dict | None = None) -> dict:
    resp = await client.post(f"/collections/{slug}/items", headers=headers or ADMIN, json=feature)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- public_read default --------------------------------------------------------


async def test_public_read_defaults_true(client, admin_headers):
    await client.post("/manage/collections", headers=admin_headers, json={"id": "plain"})
    listed = (await client.get("/manage/collections", headers=admin_headers)).json()
    by_id = {c["id"]: c for c in listed}
    # Both a freshly created collection and the bootstrap `public` stay readable.
    assert by_id["plain"]["public_read"] is True
    assert by_id["public"]["public_read"] is True


# --- resolver gating: /{geoid} open, external-id masked ---------------------------


async def test_resolver_serves_private_collection_to_any_geoid_holder(
    oidc_client, make_token, bearer
):
    # The geoid is the capability: exact-geoid resolution ignores public_read.
    await _create(oidc_client, "priv", public_read=False)
    minted = await _mint(oidc_client, "priv", _square(10, 10))
    geoid = minted["geoid"]

    stranger = bearer(make_token(sub="kc-st", email="stranger@x.org"))
    for headers in (None, stranger, ADMIN):
        resp = await oidc_client.get(f"/{geoid}", headers=headers)
        assert resp.status_code == 200, resp.text
        feature = resp.json()
        assert feature["type"] == "Feature"
        assert feature["id"] == geoid


async def test_external_id_resolver_masks_private_collection(oidc_client, make_token, bearer):
    await _create(oidc_client, "priv-x", public_read=False)
    await _grant(oidc_client, "priv-x", "viewer@x.org", "viewer")
    await _mint(oidc_client, "priv-x", _square(20, 20, external_id="ext-1"))

    url = "/collections/priv-x/external/ext-1"
    assert (await oidc_client.get(url)).status_code == 404
    stranger = bearer(make_token(sub="kc-st", email="stranger@x.org"))
    masked = await oidc_client.get(url, headers=stranger)
    assert masked.status_code == 404
    # Feature-level mask: same body an unknown external_id in this collection gets.
    viewer = bearer(make_token(sub="kc-vw", email="viewer@x.org"))
    unknown = await oidc_client.get("/collections/priv-x/external/nope", headers=viewer)
    assert masked.json()["message"] == "place not found: priv-x/ext-1"
    assert set(masked.json()) == set(unknown.json())

    assert (await oidc_client.get(url, headers=viewer)).status_code == 200
    assert (await oidc_client.get(url, headers=ADMIN)).status_code == 200


async def test_public_collection_still_resolves_anonymously(client, unit_square_ccw):
    minted = await _mint(client, "public", unit_square_ccw, headers={})
    assert (await client.get(f"/{minted['geoid']}")).status_code == 200


async def test_geoid_resolver_ignores_malformed_bearer(client, unit_square_ccw):
    # No principal on this route: a garbage credential is ignored, not 401'd.
    minted = await _mint(client, "public", unit_square_ccw)
    resp = await client.get(f"/{minted['geoid']}", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 200


async def test_external_id_resolver_rejects_malformed_bearer(client):
    # The external-id resolver keeps require_principal: a PRESENT-but-invalid
    # credential is 401, never silently anonymous.
    await _mint(client, "public", _square(15, 15, external_id="mb-1"))
    resp = await client.get(
        "/collections/public/external/mb-1", headers={"Authorization": "Bearer nope"}
    )
    assert resp.status_code == 401


# --- caller-aware dedup-409 disclosure --------------------------------------------


async def test_dedup_409_masked_for_stranger_and_anonymous(oidc_client, make_token, bearer):
    await _create(oidc_client, "priv-d", public_read=False)
    await _mint(oidc_client, "priv-d", _square(30, 30))

    stranger = bearer(make_token(sub="kc-st", email="stranger@x.org"))
    for headers in (stranger, None):
        resp = await oidc_client.post(
            "/collections/public/items", headers=headers, json=_square(30, 30)
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["message"] == CONFLICT_MESSAGE
        assert body["constraint"] == "uq_geoid_registry_geom_hash"
        assert body["geoid"] is None
        assert body["uri"] is None
        assert body["collection"] is None


async def test_dedup_409_disclosed_for_viewer_and_sysadmin(oidc_client, make_token, bearer):
    await _create(oidc_client, "priv-e", public_read=False)
    await _grant(oidc_client, "priv-e", "viewer@x.org", "viewer")
    minted = await _mint(oidc_client, "priv-e", _square(40, 40))

    viewer = bearer(make_token(sub="kc-vw", email="viewer@x.org"))
    for headers in (viewer, ADMIN):
        resp = await oidc_client.post(
            "/collections/public/items", headers=headers, json=_square(40, 40)
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["geoid"] == minted["geoid"]
        assert body["uri"] == minted["uri"]
        assert body["collection"] == "priv-e"


async def test_dedup_409_disclosed_to_the_creator_via_sub(oidc_client, make_token, bearer):
    # Disclosure through created_by == sub alone: the creator's grant is revoked
    # after the mint, so neither grant nor public_read can explain the disclosure.
    await _create(oidc_client, "priv-f", public_read=False)
    await _grant(oidc_client, "priv-f", "cr@x.org", "editor")
    creator = bearer(make_token(sub="kc-cr", email="cr@x.org"))
    minted = await _mint(oidc_client, "priv-f", _square(50, 50), headers=creator)
    deleted = await oidc_client.delete("/collections/priv-f/grants/cr@x.org", headers=ADMIN)
    assert deleted.status_code == 204

    resp = await oidc_client.post(
        "/collections/public/items", headers=creator, json=_square(50, 50)
    )
    assert resp.status_code == 409
    assert resp.json()["geoid"] == minted["geoid"]
    assert resp.json()["collection"] == "priv-f"


async def test_dedup_409_anonymous_incumbent_never_matches_anonymous_caller(oidc_client):
    # Both created_by and the caller's subject are None; the non-null guard must
    # keep None == None from reading as "own mint".
    await _create(oidc_client, "dropbox", writable_anon=True, public_read=False)
    await _mint(oidc_client, "dropbox", _square(60, 60), headers={})

    resp = await oidc_client.post("/collections/dropbox/items", json=_square(60, 60))
    assert resp.status_code == 409
    assert resp.json()["geoid"] is None
    assert resp.json()["collection"] is None


async def test_bulk_geometry_conflict_masked_and_disclosed(oidc_client, make_token, bearer):
    await _create(oidc_client, "priv-b", public_read=False)
    await _grant(oidc_client, "priv-b", "viewer@x.org", "viewer")
    minted = await _mint(oidc_client, "priv-b", _square(70, 70))

    stranger = bearer(make_token(sub="kc-st", email="stranger@x.org"))
    fc = {"type": "FeatureCollection", "features": [_square(70, 70), _square(80, 80)]}
    report = (
        await oidc_client.post("/collections/public/items/bulk", headers=stranger, json=fc)
    ).json()
    assert report["summary"] == {"received": 2, "accepted": 1, "rejected": 1}
    masked = report["rejected"][0]
    assert masked["reason"] == "geometry_conflict"
    assert masked["detail"] == CONFLICT_MESSAGE
    assert masked["geoid"] is None
    assert masked["uri"] is None
    assert masked["collection"] is None

    viewer = bearer(make_token(sub="kc-vw", email="viewer@x.org"))
    fc2 = {"type": "FeatureCollection", "features": [_square(70, 70), _square(90, 90)]}
    report2 = (
        await oidc_client.post("/collections/public/items/bulk", headers=viewer, json=fc2)
    ).json()
    disclosed = report2["rejected"][0]
    assert disclosed["reason"] == "geometry_conflict"
    assert disclosed["geoid"] == minted["geoid"]
    assert disclosed["collection"] == "priv-b"


# --- GET /me/geoids ---------------------------------------------------------------


async def test_me_geoids_requires_authentication(client):
    assert (await client.get("/me/geoids")).status_code == 401


async def test_me_geoids_lists_only_own_mints_newest_first(oidc_client, make_token, bearer):
    alice = bearer(make_token(sub="kc-alice", email="alice@x.org"))
    bob = bearer(make_token(sub="kc-bob", email="bob@x.org"))
    first = await _mint(oidc_client, "public", _square(100, 10), headers=alice)
    second = await _mint(oidc_client, "public", _square(102, 10, external_id="mine"), headers=alice)
    await _mint(oidc_client, "public", _square(104, 10), headers=bob)

    resp = await oidc_client.get("/me/geoids", headers=alice)
    assert resp.status_code == 200
    records = resp.json()
    assert [r["geoid"] for r in records] == [second["geoid"], first["geoid"]]
    assert all(
        set(r) == {"geoid", "uri", "collection", "external_id", "created_at"} for r in records
    )
    assert records[0]["external_id"] == "mine"
    assert records[0]["collection"] == "public"

    bobs = (await oidc_client.get("/me/geoids", headers=bob)).json()
    assert len(bobs) == 1


async def test_me_geoids_pagination(oidc_client, make_token, bearer):
    carol = bearer(make_token(sub="kc-carol", email="carol@x.org"))
    minted = {
        (await _mint(oidc_client, "public", _square(110 + 2 * i, 20), headers=carol))["geoid"]
        for i in range(3)
    }
    page1 = (await oidc_client.get("/me/geoids?limit=2", headers=carol)).json()
    page2 = (await oidc_client.get("/me/geoids?limit=2&offset=2", headers=carol)).json()
    assert len(page1) == 2 and len(page2) == 1
    assert {r["geoid"] for r in page1 + page2} == minted
    assert (await oidc_client.get("/me/geoids?limit=0", headers=carol)).status_code == 422


# --- sysadmin /manage item inventory ------------------------------------------------


async def test_manage_items_auth_and_404(oidc_client, make_token, bearer):
    await _create(oidc_client, "inv")
    assert (await oidc_client.get("/manage/collections/inv/items")).status_code == 401
    user = bearer(make_token(sub="kc-u", email="u@x.org"))
    assert (await oidc_client.get("/manage/collections/inv/items", headers=user)).status_code == 403
    assert (
        await oidc_client.get("/manage/collections/nope/items", headers=ADMIN)
    ).status_code == 404


async def test_manage_items_lists_collection_inventory(client, admin_headers):
    await _create(client, "inv2", public_read=False)
    first = await _mint(client, "inv2", _square(120, 30))
    second = await _mint(client, "inv2", _square(122, 30, external_id="b"))

    records = (await client.get("/manage/collections/inv2/items", headers=admin_headers)).json()
    assert [r["geoid"] for r in records] == [second["geoid"], first["geoid"]]
    assert all(
        set(r) == {"geoid", "uri", "collection", "external_id", "created_at"} for r in records
    )
    assert all(r["collection"] == "inv2" for r in records)

    page = (
        await client.get("/manage/collections/inv2/items?limit=1", headers=admin_headers)
    ).json()
    assert len(page) == 1


async def test_public_items_path_is_still_405(client):
    # The public enumeration surface stays removed: POST owns /items, GET answers 405.
    assert (await client.get("/collections/public/items")).status_code == 405
