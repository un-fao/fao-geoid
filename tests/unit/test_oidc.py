"""Unit tests for OIDC JWT validation + claim→Principal mapping (no network).

Mints synthetic RS256 tokens against an in-test RSA keypair and validates them with
a FAKE injected JWKS (returns the in-test public key for any token), so the whole
``decode_and_validate`` path runs offline. ``principal_from_claims`` is exercised as
a pure projection.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from geoid.auth.oidc import UnknownSigningKeyError, decode_and_validate, principal_from_claims
from geoid.config import Settings

pytestmark = pytest.mark.unit

_ISSUER = "https://idp.test/realms/geoid"


@pytest.fixture(scope="module")
def _keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        oidc_issuer=_ISSUER,
        oidc_jwks_url="https://idp.test/jwks",
        # audience/roles_client/admin_role/leeway use their defaults (geoid-be, etc.)
    )


def _fake_jwks(public_key):
    signing_key = jwt.PyJWK(RSAAlgorithm.to_jwk(public_key, as_dict=True))
    return SimpleNamespace(get_signing_key_from_jwt=lambda _token: signing_key)


def _claims(**overrides):
    now = int(time.time())
    base = {
        "sub": "user-123",
        "iss": _ISSUER,
        "aud": "geoid-be",
        "exp": now + 300,
        "iat": now,
        "email": "alice@example.org",
        "email_verified": True,
        "resource_access": {"geoid-roles": {"roles": ["geoid.sysadmin"]}},
    }
    base.update(overrides)
    return base


def _encode(private_key, claims, *, algorithm="RS256", key=None):
    return jwt.encode(claims, key or private_key, algorithm=algorithm)


# --- decode_and_validate ------------------------------------------------------


def test_valid_token_decodes_to_claims(_keypair, settings):
    private_key, public_key = _keypair
    token = _encode(private_key, _claims())
    claims = decode_and_validate(token, settings, _fake_jwks(public_key))
    assert claims["sub"] == "user-123"
    assert claims["aud"] == "geoid-be"


def test_expired_token_is_rejected(_keypair, settings):
    private_key, public_key = _keypair
    token = _encode(private_key, _claims(exp=int(time.time()) - 3600))
    with pytest.raises(jwt.PyJWTError):
        decode_and_validate(token, settings, _fake_jwks(public_key))


def test_wrong_audience_is_rejected(_keypair, settings):
    private_key, public_key = _keypair
    token = _encode(private_key, _claims(aud="some-other-client"))
    with pytest.raises(jwt.InvalidAudienceError):
        decode_and_validate(token, settings, _fake_jwks(public_key))


def test_wrong_issuer_is_rejected(_keypair, settings):
    private_key, public_key = _keypair
    token = _encode(private_key, _claims(iss="https://evil.test/realms/geoid"))
    with pytest.raises(jwt.InvalidIssuerError):
        decode_and_validate(token, settings, _fake_jwks(public_key))


def test_wrong_algorithm_is_rejected(_keypair, settings):
    # HS256-signed token (alg-confusion attempt) must be refused: RS256 is pinned.
    _, public_key = _keypair
    token = jwt.encode(_claims(), "shared-secret-at-least-32-bytes-long!!", algorithm="HS256")
    with pytest.raises(jwt.PyJWTError):
        decode_and_validate(token, settings, _fake_jwks(public_key))


def test_missing_required_sub_is_rejected(_keypair, settings):
    private_key, public_key = _keypair
    claims = _claims()
    del claims["sub"]
    token = _encode(private_key, claims)
    with pytest.raises(jwt.MissingRequiredClaimError):
        decode_and_validate(token, settings, _fake_jwks(public_key))


def test_empty_sub_is_rejected(_keypair, settings):
    private_key, public_key = _keypair
    token = _encode(private_key, _claims(sub=""))
    with pytest.raises(jwt.MissingRequiredClaimError):
        decode_and_validate(token, settings, _fake_jwks(public_key))


def test_leeway_tolerates_small_clock_skew(_keypair, settings):
    # exp 10s in the past is within the 30s default leeway -> still valid.
    private_key, public_key = _keypair
    token = _encode(private_key, _claims(exp=int(time.time()) - 10))
    claims = decode_and_validate(token, settings, _fake_jwks(public_key))
    assert claims["sub"] == "user-123"


def test_leeway_rejects_just_past_the_boundary(_keypair, settings):
    # exp 35s in the past is beyond the 30s leeway -> rejected (pins the boundary).
    private_key, public_key = _keypair
    token = _encode(private_key, _claims(exp=int(time.time()) - 35))
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_and_validate(token, settings, _fake_jwks(public_key))


def test_nbf_in_future_beyond_leeway_is_rejected(_keypair, settings):
    private_key, public_key = _keypair
    token = _encode(private_key, _claims(nbf=int(time.time()) + 300))
    with pytest.raises(jwt.ImmatureSignatureError):
        decode_and_validate(token, settings, _fake_jwks(public_key))


def test_alg_none_forgery_is_rejected(_keypair, settings):
    # An unsigned alg=none token must be refused: RS256 is pinned.
    _, public_key = _keypair
    token = jwt.encode(_claims(), None, algorithm="none")
    with pytest.raises(jwt.PyJWTError):
        decode_and_validate(token, settings, _fake_jwks(public_key))


def _real_jwks(public_key):
    # A real PyJWKClient whose fetch returns one valid key, kid "real" (no network).
    jwk = RSAAlgorithm.to_jwk(public_key, as_dict=True) | {"kid": "real", "use": "sig"}
    jwks_client = jwt.PyJWKClient("https://idp.test/jwks")
    jwks_client.fetch_data = lambda: {"keys": [jwk]}
    return jwks_client


@pytest.mark.parametrize("headers", [{"kid": "made-up"}, None], ids=["unknown-kid", "no-kid"])
def test_unmatched_kid_is_a_token_rejection_not_a_jwks_failure(_keypair, settings, headers):
    # A kid the healthy JWKS can't resolve (or no kid at all) is a bad token: it must
    # surface as an InvalidTokenError, not PyJWKClientError (the JWKS-outage signal).
    private_key, public_key = _keypair
    token = jwt.encode(_claims(), private_key, algorithm="RS256", headers=headers)

    with pytest.raises(UnknownSigningKeyError) as exc_info:
        decode_and_validate(token, settings, _real_jwks(public_key))
    assert isinstance(exc_info.value, jwt.InvalidTokenError)
    assert not isinstance(exc_info.value, jwt.PyJWKClientError)


def test_matching_kid_still_validates_with_a_real_jwks_client(_keypair, settings):
    private_key, public_key = _keypair
    token = jwt.encode(_claims(), private_key, algorithm="RS256", headers={"kid": "real"})
    assert decode_and_validate(token, settings, _real_jwks(public_key))["sub"] == "user-123"


@pytest.mark.parametrize(
    "raised",
    [
        json.JSONDecodeError("Expecting value", "<html>cf-challenge</html>", 0),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        RecursionError(),
        ConnectionResetError(),
        TypeError("unhashable type: 'list'"),
        NotImplementedError(),
        KeyError("k"),
        jwt.PyJWKSetError("The JWK Set did not contain any keys"),
        OverflowError("cannot fit 'int' into an index-sized integer"),
        AttributeError("'NoneType' object has no attribute 'read'"),
    ],
    ids=[
        "html-body",
        "binary-body",
        "deeply-nested-body",
        "reset-mid-body",
        "list-valued-alg",
        "alg-none-key",
        "oct-key-without-k",
        "empty-key-set",
        "oversized-content-length",
        "unforeseen-failure",
    ],
)
def test_undecodable_jwks_response_surfaces_as_pyjwkclienterror(_keypair, settings, raised):
    # PyJWT lets an unreadable JWKS response escape unwrapped; it must surface as
    # PyJWKClientError (shared 401) with a fixed message that never echoes the body.
    private_key, _ = _keypair
    token = _encode(private_key, _claims())

    def _raise(_token):
        raise raised

    with pytest.raises(jwt.PyJWKClientError) as exc_info:
        decode_and_validate(token, settings, SimpleNamespace(get_signing_key_from_jwt=_raise))
    # The cause's class name tells operators which failure it was; nothing else.
    assert str(exc_info.value) == (
        f"Unable to load the JWKS signing keys ({type(raised).__name__})"
    )
    assert "cf-challenge" not in str(exc_info.value)


@pytest.mark.parametrize(
    "raised",
    [
        jwt.DecodeError("Not enough segments"),
        jwt.InvalidTokenError("Key ID header parameter must be a string"),
        jwt.PyJWKClientError("The JWKS endpoint did not contain any signing keys"),
    ],
    ids=["malformed-token", "non-string-kid", "jwks-client-error"],
)
def test_pyjwt_errors_from_key_lookup_pass_through_unchanged(_keypair, settings, raised):
    # Token-side errors must stay token rejections (WARNING), and PyJWT's own JWKS
    # errors keep their message; only foreign exceptions are rewrapped.
    private_key, _ = _keypair
    token = _encode(private_key, _claims())

    def _raise(_token):
        raise raised

    with pytest.raises(type(raised)) as exc_info:
        decode_and_validate(token, settings, SimpleNamespace(get_signing_key_from_jwt=_raise))
    assert exc_info.value is raised


def test_rs256_token_naming_an_ec_key_is_rejected_as_pyjwterror(_keypair, settings):
    # A mixed-algorithm realm JWKS: the token's kid resolves to an EC key. The
    # mismatch must be an ordinary PyJWTError (shared 401), never a TypeError (500).
    private_key, _ = _keypair
    token = _encode(private_key, _claims())
    ec_jwk = jwt.PyJWK(
        ECAlgorithm.to_jwk(ec.generate_private_key(ec.SECP256R1()).public_key(), as_dict=True)
    )
    jwks = SimpleNamespace(get_signing_key_from_jwt=lambda _token: ec_jwk)

    with pytest.raises(jwt.InvalidAlgorithmError):
        decode_and_validate(token, settings, jwks)


# --- principal_from_claims (pure projection) ---------------------------------


def test_sysadmin_role_maps_to_admin(settings):
    p = principal_from_claims(_claims(), settings)
    assert p.subject == "user-123"
    assert p.email == "alice@example.org"
    assert p.email_verified is True
    assert p.is_admin is True


def test_non_admin_role_is_not_admin(settings):
    claims = _claims(resource_access={"geoid-roles": {"roles": ["some.other.role"]}})
    p = principal_from_claims(claims, settings)
    assert p.is_admin is False
    assert p.is_anonymous is False


def test_missing_resource_access_is_not_admin(settings):
    claims = _claims()
    del claims["resource_access"]
    p = principal_from_claims(claims, settings)
    assert p.is_admin is False


def test_missing_email_degrades_gracefully(settings):
    claims = _claims()
    del claims["email"]
    del claims["email_verified"]
    p = principal_from_claims(claims, settings)
    assert p.email is None
    assert p.email_verified is False
    # subject still present -> not anonymous
    assert p.is_anonymous is False


@pytest.mark.parametrize("value", ["false", "true", 1, None], ids=repr)
def test_email_verified_is_true_only_for_json_true(settings, value):
    # A realm mapper typed as String emits "false"; bool("false") is True, which
    # would let an unverified email inherit a grant.
    p = principal_from_claims(_claims(email_verified=value), settings)
    assert p.email_verified is False


@pytest.mark.parametrize("value", [["alice@example.org"], {"a": 1}, 42], ids=repr)
def test_non_string_email_is_treated_as_absent(settings, value):
    p = principal_from_claims(_claims(email=value), settings)
    assert p.email is None


def test_empty_claims_never_raise(settings):
    p = principal_from_claims({}, settings)
    assert p.subject is None
    assert p.is_admin is False
    assert p.email is None


@pytest.mark.parametrize(
    "resource_access",
    [
        [],  # list instead of dict
        "geoid.sysadmin",  # bare string
        42,  # number
        {"geoid-roles": {"roles": "geoid.sysadmin"}},  # roles as a bare string
        {"geoid-roles": ["roles"]},  # client block as a list
    ],
    ids=["list", "string", "number", "string-roles", "list-client-block"],
)
def test_malformed_resource_access_shapes_never_raise(settings, resource_access):
    # A validly-signed token with a realm-misconfigured claim shape must degrade to
    # no-roles/non-admin, never raise (a 500 from the auth path). In particular a
    # bare-string roles value must NOT be iterated character-by-character.
    p = principal_from_claims(_claims(resource_access=resource_access), settings)
    assert p.is_admin is False


def test_bare_string_roles_are_not_iterated_into_characters():
    # With admin role "g", iterating the bare string "geoid.sysadmin" would yield "g"
    # and grant admin; the guard must treat the whole value as no roles.
    settings = Settings(
        _env_file=None,
        oidc_issuer=_ISSUER,
        oidc_jwks_url="https://idp.test/jwks",
        oidc_roles_client="geoid-roles",
        oidc_admin_role="g",
    )
    claims = _claims(resource_access={"geoid-roles": {"roles": "geoid.sysadmin"}})
    p = principal_from_claims(claims, settings)
    assert p.is_admin is False


def test_non_string_role_entries_do_not_block_admin(settings):
    claims = _claims(resource_access={"geoid-roles": {"roles": ["geoid.sysadmin", 42, None]}})
    p = principal_from_claims(claims, settings)
    assert p.is_admin is True


def test_jwks_client_sends_non_urllib_user_agent(monkeypatch):
    # Cloudflare 403s the default Python-urllib UA on the FAO realm's JWKS endpoint;
    # the client must construct with a non-urllib User-Agent so the header can't be
    # silently dropped. No network: PyJWKClient does not fetch at construction.
    from geoid import config, deps

    oidc_settings = config.Settings(
        _env_file=None,
        oidc_issuer="https://idp.test/realms/x",
        oidc_jwks_url="https://idp.test/jwks",
    )
    monkeypatch.setattr(deps, "get_settings", lambda: oidc_settings)
    deps.get_jwks_client.cache_clear()
    client = deps.get_jwks_client()
    deps.get_jwks_client.cache_clear()
    ua = client.headers.get("User-Agent", "")
    assert ua and "urllib" not in ua.lower()


async def test_unknown_kid_logs_a_warning_without_echoing_the_kid(_keypair, settings, caplog):
    # kid is caller input checked before any signature: an unknown one must log as an
    # ordinary token rejection (WARNING, type only), never as an ERROR JWKS failure,
    # and its text (here a newline and a forged entry) must not reach the log.
    import logging

    from fastapi import HTTPException

    from geoid.deps import _resolve_oidc

    private_key, public_key = _keypair
    token = jwt.encode(
        _claims(),
        private_key,
        algorithm="RS256",
        headers={"kid": "nope\nCRITICAL geoid forged entry"},
    )
    caplog.set_level(logging.WARNING, logger="geoid")

    with pytest.raises(HTTPException) as exc_info:
        await _resolve_oidc(token, settings, _real_jwks(public_key))

    assert exc_info.value.status_code == 401
    records = [r for r in caplog.records if r.name == "geoid.deps"]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.WARNING, "OIDC token rejected: UnknownSigningKeyError")
    ]


async def test_jwks_failure_log_escapes_control_characters(_keypair, settings, caplog):
    # Backstop: whatever text a PyJWKClientError carries, a control character in it
    # must not split the ERROR line into a forged second entry.
    import logging

    from fastapi import HTTPException

    from geoid.deps import _resolve_oidc

    def _raise(_token):
        raise jwt.PyJWKClientError("upstream\nCRITICAL geoid forged entry")

    caplog.set_level(logging.WARNING, logger="geoid")
    with pytest.raises(HTTPException):
        await _resolve_oidc(
            _encode(_keypair[0], _claims()),
            settings,
            SimpleNamespace(get_signing_key_from_jwt=_raise),
        )

    assert [r.getMessage() for r in caplog.records if r.name == "geoid.deps"] == [
        "OIDC JWKS failure: upstream\\nCRITICAL geoid forged entry"
    ]


def test_realm_jwk_client_logs_advertised_signing_keys_pyjwt_cannot_use(
    _keypair, monkeypatch, caplog
):
    # PyJWKSet silently skips a key it cannot parse, so a rotation that publishes a
    # malformed signing key next to a valid old one would surface only as per-request
    # unknown-kid rejections (WARNING). The fetch itself must report it at ERROR.
    import logging

    from geoid.auth.oidc import RealmJWKClient

    valid = RSAAlgorithm.to_jwk(_keypair[1], as_dict=True) | {"kid": "k1", "use": "sig"}
    broken = {"kty": "RSA", "kid": "k2\nforged", "use": "sig", "alg": "RS256", "e": "AQAB"}
    numeric_kid = valid | {"kid": 42}  # parses, but can never match a token's string kid
    empty_kid = valid | {"kid": ""}  # PyJWT drops it from the signing keys
    enc_only = {"kty": "oct", "kid": "enc", "use": "enc"}
    payload = {"keys": [valid, broken, numeric_kid, empty_kid, enc_only]}
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: payload)
    caplog.set_level(logging.WARNING, logger="geoid")

    assert RealmJWKClient("https://idp.test/jwks").fetch_data() is payload

    assert [(r.levelno, r.getMessage()) for r in caplog.records if r.name.startswith("geoid")] == [
        (
            logging.ERROR,
            "OIDC JWKS failure: signing key 'k2\\nforged' is unusable (InvalidKeyError)",
        ),
        (logging.ERROR, "OIDC JWKS failure: signing key 42 is unusable (unusable kid)"),
        (logging.ERROR, "OIDC JWKS failure: signing key '' is unusable (unusable kid)"),
    ]


def test_realm_jwk_client_passes_unparseable_payloads_through(monkeypatch, caplog):
    # A non-dict payload is PyJWT's to reject (PyJWKClientError); the check adds nothing.
    from geoid.auth.oidc import RealmJWKClient

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: ["not", "a", "set"])
    assert RealmJWKClient("https://idp.test/jwks").fetch_data() == ["not", "a", "set"]
    assert [r for r in caplog.records if r.name.startswith("geoid")] == []


def test_production_jwks_client_stops_accepting_a_removed_key(_keypair, monkeypatch):
    # A key the realm removes (e.g. after a compromise) must stop validating once the
    # 300 s JWKS cache expires: no per-kid cache may outlive the key set.
    from geoid import config, deps

    oidc_settings = config.Settings(
        _env_file=None,
        oidc_issuer="https://idp.test/realms/x",
        oidc_jwks_url="https://idp.test/jwks",
    )
    monkeypatch.setattr(deps, "get_settings", lambda: oidc_settings)
    jwk = RSAAlgorithm.to_jwk(_keypair[1], as_dict=True)
    payload = {"keys": [jwk | {"kid": "old", "use": "sig"}, jwk | {"kid": "new", "use": "sig"}]}
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: payload)
    deps.get_jwks_client.cache_clear()
    client = deps.get_jwks_client()
    deps.get_jwks_client.cache_clear()

    assert client.get_signing_key("old").key_id == "old"
    payload["keys"] = payload["keys"][1:]  # the realm removes "old"
    client.jwk_set_cache = None  # its 300 s JWKS cache has expired

    with pytest.raises(jwt.PyJWKClientError):
        client.get_signing_key("old")
