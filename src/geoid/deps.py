"""Request-scoped dependencies: the auth seam and the DB session.

Auth (temporary stopgap): anonymous requests are allowed everywhere the *data
layer* permits them (the reserved ``public`` collection); a single static
``GEOID_ADMIN_TOKEN`` bearer unlocks create/manage/list. The body of
:func:`require_principal` is the one place that later delegates to the FAO
unified auth service (Eduardo's team — expected to be OIDC, hence the kept
``OIDC_ISSUER`` / ``OIDC_JWKS_URL`` knobs; confirm before wiring), so every
route keeps depending on the same callable. Wiring it is a Release-1 stretch
goal gated on that service being available.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from geoid.config import Settings, get_settings

_BEARER = "Bearer"
_WWW_AUTH = {"WWW-Authenticate": _BEARER}

# auto_error=False -> anonymous requests (no header) are allowed through; routes
# decide whether anonymity is acceptable. Also renders an Authorize button in /docs.
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="GeoIDAdminToken",
    description="Static admin bearer token (temporary stopgap). Delegates to the "
    "FAO unified auth service once available.",
)


@dataclass(frozen=True)
class Principal:
    """The resolved caller. Anonymous principals have ``subject is None``."""

    subject: str | None
    is_admin: bool = False
    roles: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_anonymous(self) -> bool:
        return self.subject is None

    @classmethod
    def anonymous(cls) -> Principal:
        return cls(subject=None, is_admin=False, roles=())

    @classmethod
    def admin(cls) -> Principal:
        return cls(subject="admin", is_admin=True, roles=("superadmin",))


def _resolve_static(creds: HTTPAuthorizationCredentials | None, settings: Settings) -> Principal:
    """Temporary static-token identity resolution against the admin token."""
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
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid bearer token",
        headers=_WWW_AUTH,
    )


async def require_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> Principal:
    """Resolve the caller's identity. Anonymous is a valid (non-error) outcome.

    SEAM: this is where Release-1's stretch-goal auth lands — delegation to the
    FAO unified auth service (Eduardo's team, expected to be OIDC). When that
    service is wired and ``settings.oidc_enabled`` becomes true, validate the
    token against the configured issuer/JWKS here and map claims -> roles. Until
    then the temporary static-token path is authoritative and does not depend on
    FAO's (unverified) IdP.
    """
    settings = get_settings()
    return _resolve_static(creds, settings)


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
