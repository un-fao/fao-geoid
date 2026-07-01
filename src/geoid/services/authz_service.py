"""Per-collection authorization decisions (precedence + the one grant DB touch).

Precedence (highest first): **sysadmin** (static admin token OR Keycloak
``geoid.sysadmin``) bypasses every per-collection check; then the grant ladder
**owner > editor > viewer**; then the data-layer fallback (``writable_anon`` for
writes) for callers with no grant.

:func:`load_caller_grant` is the single grant lookup — it returns ``None`` (no
query needed) for sysadmin / anonymous / unverified-email callers, who never carry
a per-collection grant, and otherwise the caller's grant (backfilling the Keycloak
``sub`` on first authorized access). The ``can_*`` predicates are pure.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from geoid.deps import Principal
from geoid.domain.roles import Role, role_at_least
from geoid.models import Collection, CollectionGrant
from geoid.repositories import grant_repo

logger = logging.getLogger(__name__)


async def load_caller_grant(
    session: AsyncSession, principal: Principal, collection: Collection
) -> CollectionGrant | None:
    """The ONE per-collection grant DB touch.

    Returns ``None`` without querying for the sysadmin tier, anonymous callers, and
    callers whose email is absent or unverified (grants are keyed by verified email).
    Otherwise the caller's grant on this collection — and if that grant's
    ``principal_subject`` was null, the Keycloak ``sub`` is backfilled now (records
    the subject on first authorized access). A grant whose recorded ``sub`` differs
    from the caller's is NOT honored (deny + WARNING log): a reused/reassigned email
    with a new Keycloak identity must not silently inherit the grant.
    """
    if principal.is_admin or principal.is_anonymous:
        return None
    if not principal.email_verified or not principal.email:
        return None
    grant = await grant_repo.get_grant(session, collection.id, principal.email)
    if grant is None:
        return None
    if grant.principal_subject is not None and grant.principal_subject != principal.subject:
        logger.warning(
            "grant subject mismatch for %s on collection %s: stored sub differs from "
            "caller sub — grant not honored (email reuse or IdP identity change?)",
            principal.email,
            collection.slug,
        )
        return None
    if grant.principal_subject is None and principal.subject:
        await grant_repo.backfill_subject(
            session, collection.id, principal.email, principal.subject
        )
    return grant


def can_write(principal: Principal, collection: Collection, grant: CollectionGrant | None) -> bool:
    """sysadmin OR an editor/owner grant OR a writable_anon collection → may write.

    Preserves both legacy paths: anonymous-into-writable_anon and
    authenticated-into-writable_anon. An authenticated non-grantee facing a
    non-writable collection falls through to False → 403.
    """
    return (
        principal.is_admin
        or (grant is not None and role_at_least(grant.role, Role.EDITOR))
        or collection.writable_anon
    )


def can_manage(principal: Principal, collection: Collection, grant: CollectionGrant | None) -> bool:
    """sysadmin OR an owner grant → may change grants."""
    return principal.is_admin or (grant is not None and grant.role == Role.OWNER.value)
