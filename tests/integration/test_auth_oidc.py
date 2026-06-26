"""Integration: hybrid dual-auth dispatch (static admin token + Keycloak JWT).

Pins that enabling OIDC does NOT regress the static-token contract, that a valid
``geoid.sysadmin`` JWT is treated as admin, that a valid non-admin JWT is a
*non-anonymous* 403 (not 401) on admin routes, and that every rejected credential
returns the SAME byte-identical 401 the static path returns.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

ADMIN = {"Authorization": "Bearer test-admin-token"}


async def test_static_admin_token_still_works_with_oidc_enabled(oidc_client):
    resp = await oidc_client.get("/manage/collections", headers=ADMIN)
    assert resp.status_code == 200


async def test_anonymous_public_reads_still_work_with_oidc_enabled(oidc_client):
    # GET /collections stays admin-gated in Core (anonymous → 401); the landing page
    # and /conformance are the genuinely public reads, and OIDC must not regress them.
    assert (await oidc_client.get("/")).status_code == 200
    assert (await oidc_client.get("/conformance")).status_code == 200


async def test_valid_sysadmin_jwt_is_admin(oidc_client, make_token, bearer):
    token = make_token(sub="kc-admin", email="admin@fao.org", roles=["geoid.sysadmin"])
    resp = await oidc_client.get("/manage/collections", headers=bearer(token))
    assert resp.status_code == 200


async def test_valid_non_admin_jwt_is_403_on_admin_route(oidc_client, make_token, bearer):
    # A valid token is NON-anonymous: lacking admin → 403, NOT 401 (the split is kept).
    token = make_token(sub="kc-user", email="user@x.org", roles=[])
    resp = await oidc_client.get("/manage/collections", headers=bearer(token))
    assert resp.status_code == 403


async def test_no_token_is_401(oidc_client):
    resp = await oidc_client.get("/manage/collections")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


async def test_malformed_jwt_gets_identical_401_shape(oidc_client, bearer):
    resp = await oidc_client.get("/manage/collections", headers=bearer("not.a.jwt"))
    assert resp.status_code == 401
    # Byte-identical to the static path's 401 (FastAPI {"detail": ...} + the header).
    assert resp.json() == {"detail": "Invalid bearer token"}
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


async def test_expired_jwt_is_401(oidc_client, make_token, bearer):
    token = make_token(
        sub="kc-admin", email="admin@fao.org", roles=["geoid.sysadmin"], exp_delta=-3600
    )
    resp = await oidc_client.get("/manage/collections", headers=bearer(token))
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid bearer token"}


async def test_wrong_audience_jwt_is_401(oidc_client, make_token, bearer):
    token = make_token(sub="kc-admin", aud="some-other-client", roles=["geoid.sysadmin"])
    resp = await oidc_client.get("/manage/collections", headers=bearer(token))
    assert resp.status_code == 401


async def test_wrong_issuer_jwt_is_401(oidc_client, make_token, bearer):
    token = make_token(sub="kc-admin", iss="https://evil.test/realms/x", roles=["geoid.sysadmin"])
    resp = await oidc_client.get("/manage/collections", headers=bearer(token))
    assert resp.status_code == 401


async def test_sysadmin_jwt_can_mint_into_managed_collection(
    oidc_client, make_token, bearer, unit_square_ccw
):
    await oidc_client.post(
        "/manage/collections", headers=ADMIN, json={"id": "sm", "writable_anon": False}
    )
    token = make_token(sub="kc-admin", email="admin@fao.org", roles=["geoid.sysadmin"])
    resp = await oidc_client.post(
        "/collections/sm/items", headers=bearer(token), json=unit_square_ccw
    )
    assert resp.status_code == 201
