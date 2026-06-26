"""Unit tests for OIDC JWT validation + claim→Principal mapping (no network).

Mints synthetic RS256 tokens against an in-test RSA keypair and validates them with
a FAKE injected JWKS (returns the in-test public key for any token), so the whole
``decode_and_validate`` path runs offline. ``principal_from_claims`` is exercised as
a pure projection.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from geoid.auth.oidc import decode_and_validate, principal_from_claims
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
    return SimpleNamespace(get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=public_key))


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


def test_leeway_tolerates_small_clock_skew(_keypair, settings):
    # exp 10s in the past is within the 30s default leeway -> still valid.
    private_key, public_key = _keypair
    token = _encode(private_key, _claims(exp=int(time.time()) - 10))
    claims = decode_and_validate(token, settings, _fake_jwks(public_key))
    assert claims["sub"] == "user-123"


# --- principal_from_claims (pure projection) ---------------------------------


def test_sysadmin_role_maps_to_admin(settings):
    p = principal_from_claims(_claims(), settings)
    assert p.subject == "user-123"
    assert p.email == "alice@example.org"
    assert p.email_verified is True
    assert p.is_admin is True
    assert "geoid.sysadmin" in p.roles


def test_non_admin_role_is_not_admin(settings):
    claims = _claims(resource_access={"geoid-roles": {"roles": ["some.other.role"]}})
    p = principal_from_claims(claims, settings)
    assert p.is_admin is False
    assert p.roles == ("some.other.role",)
    assert p.is_anonymous is False


def test_missing_resource_access_yields_empty_roles(settings):
    claims = _claims()
    del claims["resource_access"]
    p = principal_from_claims(claims, settings)
    assert p.roles == ()
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


def test_empty_claims_never_raise(settings):
    p = principal_from_claims({}, settings)
    assert p.subject is None
    assert p.roles == ()
    assert p.is_admin is False
    assert p.email is None
