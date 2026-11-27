"""Pure OIDC claim → Principal mapping and Keycloak JWT validation.

No FastAPI, no DB, no network state held here: :func:`decode_and_validate` takes
the ``PyJWKClient`` as a parameter so it stays unit-testable with a fake (injected)
JWKS — ``tests/unit/test_oidc.py`` mints synthetic RS256 tokens against an in-test
keypair, no network. PyJWT is imported at module top because this module is only
ever loaded when OIDC is active (``deps.py`` imports it lazily) or under tests
(where the ``oidc`` dev extra is present); an image without the extra never imports
it, so ``deps.py`` still imports cleanly without PyJWT.

Confirmed from the live ``<realm>`` realm metadata: RS256; our API
audience is ``geoid-be``; the global admin role is ``geoid.sysadmin`` at
``resource_access["geoid-roles"]["roles"]``; ``sub`` is the stable identity; tokens
carry ``email`` + ``email_verified``.
"""

from __future__ import annotations

import logging
from typing import Any

import jwt
from jwt import PyJWKClient

from geoid.config import Settings
from geoid.deps import Principal

logger = logging.getLogger(__name__)


class RealmJWKClient(PyJWKClient):
    """``PyJWKClient`` that reports advertised signing keys PyJWT cannot use.

    ``PyJWKSet`` silently skips a key it cannot parse, so a rotation that publishes a
    malformed signing key would otherwise surface only as per-request unknown-``kid``
    token rejections (WARNING). Checking on fetch keeps that ERROR-level alarm bounded
    by the fetch rate (cache lifespan and PyJWT's refresh cooldown), not by callers.
    """

    def fetch_data(self) -> Any:
        data = super().fetch_data()
        keys = data.get("keys") if isinstance(data, dict) else None
        for key in keys if isinstance(keys, list) else ():
            if not isinstance(key, dict) or key.get("use") not in ("sig", None):
                continue
            try:
                jwt.PyJWK(key)
            except jwt.PyJWTError as exc:
                # Mirrors PyJWKSet, which skips exactly these.
                problem = type(exc).__name__
            else:
                # PyJWT drops a signing key without a kid, and a non-string kid can
                # never match a token's string kid header.
                kid = key.get("kid")
                problem = None if isinstance(kid, str) and kid else "unusable kid"
            if problem:
                # ascii() keeps the IdP-supplied kid on one log line.
                logger.error(
                    "OIDC JWKS failure: signing key %s is unusable (%s)",
                    ascii(key.get("kid")),
                    problem,
                )
        return data


class UnknownSigningKeyError(jwt.InvalidTokenError):
    """No usable key in the realm JWKS matches the token's ``kid``, or it has none.

    A token-side rejection, unlike ``PyJWKClientError``: any caller can send a made-up
    ``kid``, so it must log as a rejected token, not as a JWKS outage. An advertised
    key PyJWT cannot use is alarmed separately, by :class:`RealmJWKClient` on fetch.
    """


def principal_from_claims(claims: dict[str, Any], settings: Settings) -> Principal:
    """Map validated Keycloak claims onto a :class:`Principal`.

    Total by design: missing/absent claims degrade gracefully (no roles, ``email``
    None) and never raise — validation already happened in
    :func:`decode_and_validate`, so this is pure projection. ``is_admin`` is true iff
    the configured ``oidc_admin_role`` (``geoid.sysadmin``) is present under
    ``resource_access[oidc_roles_client]["roles"]``.
    """
    subject = claims.get("sub")
    # OIDC Core types email as a string and email_verified as a JSON boolean; any
    # other shape (e.g. a String-typed realm mapper's "false") is treated as absent /
    # unverified, since grants are keyed by verified email.
    email = claims.get("email")
    if not isinstance(email, str):
        email = None
    email_verified = claims.get("email_verified") is True
    # isinstance-guarded so a realm-misconfigured claim shape (resource_access as a
    # list/string, roles as a bare string) degrades to no-roles instead of raising —
    # a validly-signed token must never 500 the auth path. A bare-string roles value
    # is deliberately NOT iterated (tuple("x") would explode into characters).
    resource_access = claims.get("resource_access")
    client_block = (
        resource_access.get(settings.oidc_roles_client)
        if isinstance(resource_access, dict)
        else None
    )
    raw_roles = client_block.get("roles") if isinstance(client_block, dict) else None
    roles = (
        tuple(r for r in raw_roles if isinstance(r, str))
        if isinstance(raw_roles, (list, tuple))
        else ()
    )
    return Principal(
        subject=subject,
        is_admin=settings.oidc_admin_role in roles,
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
    wrong ``aud``/``iss``, expired, missing required claim, a ``kid`` the JWKS does
    not hold (:class:`UnknownSigningKeyError`), or a JWKS fetch error
    (``PyJWKClientError`` is itself a ``PyJWTError``) — which the caller maps to 401.
    """
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
    except jwt.PyJWKClientError as exc:
        # PyJWT 2.15.1 raises exactly this when the fetched set has no key for the
        # token's kid (a missing kid reads as "None"): a bad token, not an outage.
        # The header parsed once already, so re-reading it here cannot raise.
        kid = jwt.get_unverified_header(token).get("kid")
        if str(exc) == f'Unable to find a signing key that matches: "{kid}"':
            raise UnknownSigningKeyError("No JWKS signing key matches the token kid") from exc
        raise
    except jwt.InvalidTokenError:
        # Token-side parse errors such as DecodeError already map to the 401 path.
        raise
    except Exception as exc:
        # Anything else is a JWKS-side failure that PyJWT 2.15.1 lets escape: an
        # undecodable body (e.g. an HTML challenge page), a mid-body connection
        # reset, one malformed key (it aborts parsing of the whole set), an empty set
        # (PyJWKSetError), an oversized Content-Length. It must be the shared 401.
        # Only the cause's class name goes into the message (and so the log): it says
        # which failure it was without echoing the response body.
        raise jwt.PyJWKClientError(
            f"Unable to load the JWKS signing keys ({type(exc).__name__})"
        ) from exc
    # Passing the PyJWK (not its raw key) makes PyJWT reject a token whose alg does
    # not match the resolved key's algorithm as InvalidAlgorithmError; a raw non-RSA
    # key would raise TypeError instead.
    claims = jwt.decode(
        token,
        signing_key,
        algorithms=["RS256"],
        audience=settings.oidc_audience,
        issuer=settings.oidc_issuer,
        leeway=settings.oidc_leeway_seconds,
        options={"require": ["exp", "iss", "aud", "sub"]},
    )
    # PyJWT's "require" rejects only a missing/None sub and accepts "" as a valid
    # string; an empty subject would use a grant without binding it on first use.
    if not claims["sub"]:
        raise jwt.MissingRequiredClaimError("sub")
    return claims
