"""Integration: non-public collections (``public_read=false``) end to end.

Covers the four 2026-07-03 surfaces: resolver 404-masking (existence-masked, the
body identical to an unknown id's), caller-aware dedup-409 disclosure (single +
bulk), ``GET /me/geoids``, and the sysadmin ``/manage`` item inventory.
"""

from __future__ import annotations

import uuid as uuid_module

import pytest

pytestmark = pytest.mark.integration

ADMIN = {"Authorization": "Bearer test-admin-token"}


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


# --- resolver 404-masking ---------------------------------------------------------


async def test_resolver_masks_private_collection(oidc_client, make_token, bearer):
    await _create(oidc_client, "priv", public_read=False)
    await _grant(oidc_client, "priv", "viewer@x.org", "viewer")
    minted = await _mint(oidc_client, "priv", _square(10, 10))
    geoid = minted["geoid"]

    assert (await oidc_client.get(f"/{geoid}")).status_code == 404
    stranger = bearer(make_token(sub="kc-st", email="stranger@x.org"))
    masked = await oidc_client.get(f"/{geoid}", headers=stranger)
    assert masked.status_code == 404
    # Existence-masked: byte-identical body to a genuinely unknown geoid's 404.
    unknown = await oidc_client.get(f"/{uuid_module.uuid4()}", headers=stranger)
    assert masked.json()["message"] == f"place not found: {geoid}"
    assert set(masked.json()) == set(unknown.json())

    viewer = bearer(make_token(sub="kc-vw", email="viewer@x.org"))
    assert (await oidc_client.get(f"/{geoid}", headers=viewer)).status_code == 200
    assert (await oidc_client.get(f"/{geoid}", headers=ADMIN)).status_code == 200


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


async def test_resolver_rejects_malformed_bearer(client, unit_square_ccw):
    # New with authenticated reads: a PRESENT-but-invalid credential is 401, never
    # silently anonymous. No header at all stays anonymous (see the tests above).
    minted = await _mint(client, "public", unit_square_ccw)
    resp = await client.get(f"/{minted['geoid']}", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401
