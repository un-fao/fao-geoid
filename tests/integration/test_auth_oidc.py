"""Integration: Keycloak-only auth dispatch.

Pins that a valid ``geoid.sysadmin`` JWT is treated as admin, that a valid
non-admin JWT is a *non-anonymous* 403 (not 401) on admin routes, and that every
rejected credential — malformed, expired, forged, JWKS outage — returns the SAME
byte-identical 401.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


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
    token = make_token(sub="kc-admin", email="admin@fao.org", roles=["geoid.sysadmin"])
    await oidc_client.post(
        "/manage/collections", headers=bearer(token), json={"id": "sm", "public_write": False}
    )
    resp = await oidc_client.post(
        "/collections/sm/items", headers=bearer(token), json=unit_square_ccw
    )
    assert resp.status_code == 201


# --- Forgeries and infra failures all collapse onto the same 401 ---------------


async def test_alg_none_forgery_is_401(oidc_client, bearer):
    import time

    import jwt as pyjwt

    claims = {
        "sub": "forged",
        "iss": "https://idp.test/realms/geoid",
        "aud": "geoid-be",
        "exp": int(time.time()) + 300,
    }
    token = pyjwt.encode(claims, None, algorithm="none")
    resp = await oidc_client.get("/manage/collections", headers=bearer(token))
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid bearer token"}


async def test_expired_just_past_leeway_boundary_is_401(oidc_client, make_token, bearer):
    # exp 35s in the past: 5s beyond the 30s leeway — the boundary must reject.
    token = make_token(sub="kc-admin", roles=["geoid.sysadmin"], exp_delta=-35)
    resp = await oidc_client.get("/manage/collections", headers=bearer(token))
    assert resp.status_code == 401


@pytest.fixture
async def jwks_error_client(db_clean):
    """An OIDC-enabled app whose JWKS client fails for every token (unknown kid /
    IdP outage / Cloudflare block). The client must still see only the plain 401."""
    from types import SimpleNamespace

    import jwt as pyjwt
    from httpx import ASGITransport, AsyncClient

    from geoid.config import Settings, get_settings
    from geoid.deps import get_jwks_client
    from geoid.main import create_app

    settings = Settings(
        _env_file=None,
        oidc_issuer="https://idp.test/realms/geoid",
        oidc_jwks_url="https://idp.test/jwks",
    )

    def _raise(_token):
        raise pyjwt.PyJWKClientError('Unable to find a signing key that matches: "unknown-kid"')

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_jwks_client] = lambda: SimpleNamespace(
        get_signing_key_from_jwt=_raise
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


async def test_unknown_kid_jwks_failure_is_401(jwks_error_client, make_token, bearer):
    token = make_token(sub="kc-admin", roles=["geoid.sysadmin"])
    resp = await jwks_error_client.get("/manage/collections", headers=bearer(token))
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid bearer token"}


async def test_public_operations_do_not_touch_failing_jwks(
    jwks_error_client, make_token, bearer, unit_square_ccw
):
    headers = bearer(make_token(sub="kc-public"))

    single = await jwks_error_client.post("/items", headers=headers, json=unit_square_ccw)
    assert single.status_code == 201

    bulk = await jwks_error_client.post(
        "/items/bulk",
        headers=headers,
        json={"type": "FeatureCollection", "features": [unit_square_ccw]},
    )
    assert bulk.status_code == 200

    resolved = await jwks_error_client.get(f"/{single.json()['geoid']}", headers=headers)
    assert resolved.status_code == 200
    assert set(resolved.json()["properties"]) == {"geoid", "uri"}


_BODY_MARKER = "cf-challenge-marker"


@pytest.fixture
async def bad_jwks_client(request, db_clean, monkeypatch, oidc_keypair):
    """An OIDC-enabled app whose REAL ``PyJWKClient`` fetches a JWKS URL that answers
    200 with an unusable body, selected by ``request.param``: an HTML page (a
    Cloudflare challenge in front of the realm), a set holding a malformed key next to
    the valid one, or an empty set.

    Yields ``(http_client, jwks_gets)``; ``jwks_gets`` counts the GETs the loopback
    server saw, proving the fetch reached it rather than a proxy.
    """
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from httpx import ASGITransport, AsyncClient
    from jwt import PyJWKClient
    from jwt.algorithms import RSAAlgorithm

    from geoid.config import Settings, get_settings
    from geoid.deps import get_jwks_client
    from geoid.main import create_app

    valid_key = RSAAlgorithm.to_jwk(oidc_keypair[1], as_dict=True) | {"kid": "k1", "use": "sig"}
    malformed_key = {
        "kty": "RSA",
        "kid": _BODY_MARKER,
        "alg": ["RS256"],
        "n": valid_key["n"],
        "e": valid_key["e"],
    }
    content_type, body = {
        "html": ("text/html", f"<html>{_BODY_MARKER}</html>"),
        "malformed-key": ("application/json", json.dumps({"keys": [valid_key, malformed_key]})),
        "empty-set": ("application/json", json.dumps({"keys": []})),
    }[request.param]
    jwks_gets: list[str] = []

    class _JwksHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            jwks_gets.append(self.path)
            payload = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _JwksHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Lowercase wins over NO_PROXY in urllib's second pass; keeps a developer proxy
        # from intercepting the loopback fetch.
        monkeypatch.setenv("no_proxy", "127.0.0.1")
        jwks_url = f"http://127.0.0.1:{server.server_port}/jwks"

        settings = Settings(
            _env_file=None, oidc_issuer="https://idp.test/realms/geoid", oidc_jwks_url=jwks_url
        )
        app = create_app()
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_jwks_client] = lambda: PyJWKClient(jwks_url)
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            yield http_client, jwks_gets
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    ("bad_jwks_client", "cause"),
    [
        ("html", "JSONDecodeError"),
        ("malformed-key", "TypeError"),
        ("empty-set", "PyJWKSetError"),
    ],
    indirect=["bad_jwks_client"],
)
async def test_unusable_jwks_response_is_shared_401(
    bad_jwks_client, cause, make_token, bearer, caplog
):
    import logging

    http_client, jwks_gets = bad_jwks_client
    caplog.set_level(logging.WARNING, logger="geoid")
    token = make_token(sub="kc-admin", roles=["geoid.sysadmin"])

    resp = await http_client.get("/manage/collections", headers=bearer(token))
    # Control: a malformed bearer fails as DecodeError before any JWKS fetch.
    control = await http_client.get("/manage/collections", headers=bearer("not.a.jwt"))

    assert jwks_gets == ["/jwks"]
    assert resp.status_code == control.status_code == 401
    assert resp.content == control.content
    assert resp.headers.get("content-type") == control.headers.get("content-type")
    assert resp.headers.get("www-authenticate") == control.headers.get("www-authenticate")
    jwks_errors = [
        r.getMessage()
        for r in caplog.records
        if r.name == "geoid.deps" and r.levelno == logging.ERROR
    ]
    assert jwks_errors == [f"OIDC JWKS failure: Unable to load the JWKS signing keys ({cause})"]
    assert not any(_BODY_MARKER in r.getMessage() for r in caplog.records)


async def test_rejection_401s_are_byte_identical_across_failure_modes(
    oidc_client, make_token, bearer
):
    # Cross-failure parity asserted as response EQUALITY, not against a literal: a
    # bearer that is not a JWT at all (decode error) and a properly signed but
    # expired JWT (claims error) must be indistinguishable on the wire.
    not_a_jwt = await oidc_client.get("/manage/collections", headers=bearer("wrong-token"))
    bad_jwt = await oidc_client.get(
        "/manage/collections", headers=bearer(make_token(exp_delta=-3600))
    )
    assert not_a_jwt.status_code == bad_jwt.status_code == 401
    assert not_a_jwt.content == bad_jwt.content
    assert not_a_jwt.headers.get("www-authenticate") == bad_jwt.headers.get("www-authenticate")
    assert not_a_jwt.headers.get("content-type") == bad_jwt.headers.get("content-type")


async def test_non_ascii_bearer_token_is_401_not_500(client):
    # Starlette decodes headers as latin-1, so non-ASCII bytes reach the OIDC
    # validation path as a str; a stray "Bearer café" must be an ordinary 401,
    # never a 500 (an unauthenticated-input crash would break the anti-oracle
    # 401). Value passed as bytes because httpx itself refuses non-ASCII str
    # header values.
    headers = {b"Authorization": "Bearer caf\xe9".encode("latin-1")}
    resp = await client.get("/manage/collections", headers=headers)
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "Bearer"
