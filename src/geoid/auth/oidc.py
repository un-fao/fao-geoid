"""Pure OIDC claim → Principal mapping and Keycloak JWT validation.

No FastAPI, no DB, no network state held here: :func:`decode_and_validate` takes
the ``PyJWKClient`` as a parameter so it stays unit-testable with a fake (injected)
JWKS — ``tests/unit/test_oidc.py`` mints synthetic RS256 tokens against an in-test
keypair, no network. PyJWT is imported at module top because this module is only
ever loaded when OIDC is active (``deps.py`` imports it lazily) or under tests
(where the ``oidc`` dev extra is present); a static-token-only image never imports
it, so ``deps.py`` still imports cleanly without PyJWT.

Confirmed from the live ``<realm>`` realm metadata: RS256; our API
audience is ``geoid-be``; the global admin role is ``geoid.sysadmin`` at
``resource_access["geoid-roles"]["roles"]``; ``sub`` is the stable identity; tokens
carry ``email`` + ``email_verified``.
"""

from __future__ import annotations

from typing import Any

import jwt
from jwt import PyJWKClient

from geoid.config import Settings
from geoid.deps import Principal


def principal_from_claims(claims: dict[str, Any], settings: Settings) -> Principal:
    """Map validated Keycloak claims onto a :class:`Principal`.

    Total by design: missing/absent claims degrade gracefully (no roles, ``email``
    None) and never raise — validation already happened in
    :func:`decode_and_validate`, so this is pure projection. ``is_admin`` is true iff
    the configured ``oidc_admin_role`` (``geoid.sysadmin``) is present under
    ``resource_access[oidc_roles_client]["roles"]``.
    """
    subject = claims.get("sub")
    email = claims.get("email")
    email_verified = bool(claims.get("email_verified", False))
    resource_access = claims.get("resource_access") or {}
    client_block = resource_access.get(settings.oidc_roles_client) or {}
    roles = tuple(client_block.get("roles") or ())
    return Principal(
        subject=subject,
        is_admin=settings.oidc_admin_role in roles,
        roles=roles,
        email=email,
        email_verified=email_verified,
    )


def decode_and_validate(token: str, settings: Settings, jwks_client: PyJWKClient) -> dict[str, Any]:
    """Validate a Keycloak RS256 JWT against the realm JWKS and return its claims.

    Pins ``algorithms=["RS256"]`` (rejects ``none`` and HS256 alg-confusion),
    validates ``iss`` / ``aud`` / ``exp`` / ``nbf`` with ``oidc_leeway_seconds`` of
    skew, and *requires* the core claims so a token missing any is rejected, not
    silently accepted. The signing key is resolved by ``kid`` from the (cached) JWKS
    client. Raises a ``jwt.PyJWTError`` subclass on any failure — bad signature,
    wrong ``aud``/``iss``, expired, missing required claim, or a JWKS fetch error
    (``PyJWKClientError`` is itself a ``PyJWTError``) — which the caller maps to 401.
    """
    signing_key = jwks_client.get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=settings.oidc_audience,
        issuer=settings.oidc_issuer,
        leeway=settings.oidc_leeway_seconds,
        options={"require": ["exp", "iss", "aud", "sub"]},
    )
