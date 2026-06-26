"""Request-scoped dependencies: the auth seam and the DB session.

**Hybrid, no flag day.** ``require_principal`` tries the static ``GEOID_ADMIN_TOKEN``
bearer first (constant-time compare → the global ``sysadmin`` tier); if it doesn't
match and ``settings.oidc_enabled`` is true, the credential is validated as a
Keycloak RS256 JWT (FAO ``<realm>`` realm) and its claims mapped to a
:class:`Principal`; otherwise the request is 401. The static token therefore keeps
working unchanged whether or not OIDC is enabled, so nothing regresses.

The OIDC validation/mapping logic lives in :mod:`geoid.auth.oidc` (pure, network-free,
unit-tested). PyJWT and ``PyJWKClient`` are imported **lazily** here so a
static-token-only image built without the ``oidc`` extra still imports this module.
On any token/JWKS failure the OIDC path raises the **same** 401 the static path uses,
so the error body and headers are byte-identical regardless of which path ran.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from geoid.config import Settings, get_settings

if TYPE_CHECKING:
    from jwt import PyJWKClient

logger = logging.getLogger(__name__)

_BEARER = "Bearer"
_WWW_AUTH = {"WWW-Authenticate": _BEARER}

# auto_error=False -> anonymous requests (no header) are allowed through; routes
# decide whether anonymity is acceptable. Also renders an Authorize button in /docs.
# The same paste-a-bearer field carries BOTH credential types (static admin token
# AND a Keycloak JWT) — there is no separate static-only scheme.
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="GeoIDBearer",
    description="Paste your access token here to authorize your requests.",
)


@dataclass(frozen=True)
class Principal:
    """The resolved caller. Anonymous principals have ``subject is None``.

    ``email``/``email_verified`` are populated only on the Keycloak path (the static
    admin and anonymous principals leave them at their defaults). They are appended
    after the original three fields so ``admin()``/``anonymous()`` and every existing
    call site stay valid.
    """

    subject: str | None
    is_admin: bool = False
    roles: tuple[str, ...] = field(default_factory=tuple)
    email: str | None = None
    email_verified: bool = False

    @property
    def is_anonymous(self) -> bool:
        return self.subject is None

    @classmethod
    def anonymous(cls) -> Principal:
        return cls(subject=None, is_admin=False, roles=())

    @classmethod
    def admin(cls) -> Principal:
        return cls(subject="admin", is_admin=True, roles=("superadmin",))


def _invalid_token() -> HTTPException:
    """The single 401 both the static and OIDC paths raise (byte-identical body)."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid bearer token",
        headers=_WWW_AUTH,
    )


@lru_cache
def get_jwks_client() -> PyJWKClient | None:
    """The process-wide JWKS client (cached), or ``None`` when OIDC is disabled.

    Injected via ``Depends`` so tests override it with a fake (no network). The real
    client caches signing keys by ``kid`` in-process with a 300 s lifespan, so the
    blocking JWKS fetch happens at most once per key-rotation window. PyJWT is
    imported lazily so importing this module never requires the ``oidc`` extra.
    """
    settings = get_settings()
    if not settings.oidc_enabled:
        return None
    from jwt import PyJWKClient

    return PyJWKClient(settings.oidc_jwks_url, cache_keys=True, lifespan=300)


async def _resolve_oidc(
    token: str, settings: Settings, jwks_client: PyJWKClient | None
) -> Principal:
    """Validate ``token`` as a Keycloak JWT and map its claims to a Principal.

    The (rare, cache-missing) blocking JWKS fetch is offloaded to a worker thread so
    it never stalls the event loop. Any validation/JWKS failure becomes the same 401
    the static path raises; only the exception *type* is logged — never the token or
    the JWKS body.
    """
    import anyio
    import jwt

    from geoid.auth.oidc import decode_and_validate, principal_from_claims

    if jwks_client is None:
        # oidc_enabled but no client (misconfiguration) — treat as an invalid token,
        # never a 500: an unauthenticated caller must not learn about server state.
        raise _invalid_token()
    try:
        claims = await anyio.to_thread.run_sync(decode_and_validate, token, settings, jwks_client)
    except jwt.PyJWTError as exc:
        logger.info("OIDC token rejected: %s", type(exc).__name__)
        raise _invalid_token() from exc
    return principal_from_claims(claims, settings)


async def require_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
    jwks_client: PyJWKClient | None = Depends(get_jwks_client),
) -> Principal:
    """Resolve the caller's identity. Anonymous is a valid (non-error) outcome.

    Dispatch: no credentials → anonymous; non-Bearer scheme → 401; the static admin
    token (constant-time compare) → ``sysadmin``; else, when OIDC is enabled, validate
    as a Keycloak JWT; otherwise 401. ``settings``/``jwks_client`` are injected (not
    fetched directly) so the auth path honours ``app.dependency_overrides`` in tests.
    """
    if creds is None:
        return Principal.anonymous()
    if creds.scheme.lower() != _BEARER.lower():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unsupported authorization scheme",
            headers=_WWW_AUTH,
        )
    # Constant-time comparison so a wrong token can't be recovered via timing.
    if hmac.compare_digest(creds.credentials, settings.admin_token):
        return Principal.admin()
    if settings.oidc_enabled:
        return await _resolve_oidc(creds.credentials, settings, jwks_client)
    raise _invalid_token()


async def require_admin(principal: Principal = Depends(require_principal)) -> Principal:
    """Gate a route on admin privileges. Anonymous -> 401, non-admin -> 403."""
    if principal.is_admin:
        return principal
    raise HTTPException(
        status_code=(
            status.HTTP_401_UNAUTHORIZED if principal.is_anonymous else status.HTTP_403_FORBIDDEN
        ),
        detail="Admin privileges required",
        headers=_WWW_AUTH,
    )
