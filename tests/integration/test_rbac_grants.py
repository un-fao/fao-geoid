"""Integration: per-collection grant ladder (viewer < editor < owner) on the
write + manage paths, plus Keycloak-``sub`` backfill on first authorized access.

Read enforcement for private collections is a deferred phase, so these pin the two
load-bearing Core surfaces: who may WRITE to a collection and who may MANAGE its
grants. ``GET /collections`` stays admin-gated (unchanged), so reads are not asserted
here.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

ADMIN = {"Authorization": "Bearer test-admin-token"}


async def _create(client, slug: str, **over) -> None:
    body = {"id": slug, "writable_anon": False, **over}
    resp = await client.post("/manage/collections", headers=ADMIN, json=body)
    assert resp.status_code == 201, resp.text


async def _grant(client, slug: str, email: str, role: str) -> None:
    resp = await client.post(
        f"/collections/{slug}/grants", headers=ADMIN, json={"email": email, "role": role}
    )
    assert resp.status_code == 201, resp.text


async def test_editor_writes_viewer_and_nongrantee_cannot(
    oidc_client, make_token, bearer, unit_square_ccw, other_square
):
    await _create(oidc_client, "team")
    await _grant(oidc_client, "team", "editor@x.org", "editor")
    await _grant(oidc_client, "team", "viewer@x.org", "viewer")

    editor = bearer(make_token(sub="kc-ed", email="editor@x.org"))
    viewer = bearer(make_token(sub="kc-vw", email="viewer@x.org"))
    stranger = bearer(make_token(sub="kc-st", email="stranger@x.org"))

    assert (
        await oidc_client.post("/collections/team/items", headers=editor, json=unit_square_ccw)
    ).status_code == 201
    assert (
        await oidc_client.post("/collections/team/items", headers=viewer, json=other_square)
    ).status_code == 403
    assert (
        await oidc_client.post("/collections/team/items", headers=stranger, json=other_square)
    ).status_code == 403


async def test_sysadmin_jwt_bypasses_grants(oidc_client, make_token, bearer, unit_square_ccw):
    await _create(oidc_client, "bypass")
    admin = bearer(make_token(sub="kc-adm", email="adm@fao.org", roles=["geoid.sysadmin"]))
    assert (
        await oidc_client.post("/collections/bypass/items", headers=admin, json=unit_square_ccw)
    ).status_code == 201


async def test_owner_can_manage_grants(oidc_client, make_token, bearer):
    await _create(oidc_client, "owned")
    await _grant(oidc_client, "owned", "owner@x.org", "owner")
    owner = bearer(make_token(sub="kc-ow", email="owner@x.org"))

    add = await oidc_client.post(
        "/collections/owned/grants", headers=owner, json={"email": "new@x.org", "role": "viewer"}
    )
    assert add.status_code == 201
    listed = await oidc_client.get("/collections/owned/grants", headers=owner)
    assert listed.status_code == 200
    assert {g["email"] for g in listed.json()} >= {"owner@x.org", "new@x.org"}


async def test_editor_cannot_manage_grants(oidc_client, make_token, bearer):
    await _create(oidc_client, "team2")
    await _grant(oidc_client, "team2", "editor@x.org", "editor")
    editor = bearer(make_token(sub="kc-ed", email="editor@x.org"))
    # editor is not owner → cannot list/manage grants (403)
    assert (await oidc_client.get("/collections/team2/grants", headers=editor)).status_code == 403


async def test_grant_revoke_then_revoke_again_is_404(oidc_client):
    await _create(oidc_client, "rev")
    await _grant(oidc_client, "rev", "temp@x.org", "viewer")
    deleted = await oidc_client.delete("/collections/rev/grants/temp@x.org", headers=ADMIN)
    assert deleted.status_code == 204
    # revoking again → 404
    assert (
        await oidc_client.delete("/collections/rev/grants/temp@x.org", headers=ADMIN)
    ).status_code == 404


async def test_grant_role_upsert_changes_write_outcome(
    oidc_client, make_token, bearer, unit_square_ccw
):
    await _create(oidc_client, "promote")
    await _grant(oidc_client, "promote", "p@x.org", "viewer")
    user = bearer(make_token(sub="kc-p", email="p@x.org"))
    # viewer can't write
    assert (
        await oidc_client.post("/collections/promote/items", headers=user, json=unit_square_ccw)
    ).status_code == 403
    # promote to editor (upsert) → can write
    await _grant(oidc_client, "promote", "p@x.org", "editor")
    assert (
        await oidc_client.post("/collections/promote/items", headers=user, json=unit_square_ccw)
    ).status_code == 201


async def test_subject_backfilled_on_first_authorized_write(
    oidc_client, make_token, bearer, unit_square_ccw
):
    await _create(oidc_client, "bf")
    # Granted by admin with no subject known → principal_subject is null initially.
    await _grant(oidc_client, "bf", "dave@x.org", "editor")
    before = await oidc_client.get("/collections/bf/grants", headers=ADMIN)
    dave_before = next(g for g in before.json() if g["email"] == "dave@x.org")
    assert dave_before["subject"] is None

    # Dave's first authorized write backfills his Keycloak sub.
    dave = bearer(make_token(sub="kc-dave", email="dave@x.org"))
    assert (
        await oidc_client.post("/collections/bf/items", headers=dave, json=unit_square_ccw)
    ).status_code == 201

    after = await oidc_client.get("/collections/bf/grants", headers=ADMIN)
    dave_after = next(g for g in after.json() if g["email"] == "dave@x.org")
    assert dave_after["subject"] == "kc-dave"
