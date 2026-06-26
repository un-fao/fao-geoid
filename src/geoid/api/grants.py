"""Per-collection grant management (owner or sysadmin).

Collections are created via the admin ``/manage/collections`` alias; this router
manages the **grant ladder** on an existing collection — listing, granting/
re-granting a role by email, and revoking. Each route authorizes itself via
:mod:`geoid.services.authz_service` (the router carries no blanket dependency), and
the method+path of every route is distinct from the OGC GET read surface, so nothing
here shadows it. Mounted before the ``places`` ``/{geoid}`` catch-all so these literal
``/collections/...`` routes win.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.db import get_session
from geoid.deps import Principal, require_principal
from geoid.models import Collection
from geoid.repositories import collection_repo, grant_repo
from geoid.schemas.collection import GrantCreate, GrantOut
from geoid.services import authz_service, listing_service
from geoid.services.exceptions import (
    CollectionForbiddenError,
    CollectionNotFoundError,
    GrantNotFoundError,
)

router = APIRouter(tags=["collections"])


async def _require_manageable(
    session: AsyncSession, collection_id: str, principal: Principal
) -> Collection:
    """Load a collection and assert the caller is its owner (or sysadmin).

    404 if it does not exist; 403 (``CollectionForbiddenError``) if the caller is not
    owner/sysadmin — management ops surface forbidden, not the read-path's hide-existence.
    """
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    grant = await authz_service.load_caller_grant(session, principal, collection)
    if not authz_service.can_manage(principal, collection, grant):
        raise CollectionForbiddenError(collection_id)
    return collection


@router.post(
    "/collections/{collection_id}/grants",
    response_model=GrantOut,
    status_code=status.HTTP_201_CREATED,
    summary="Grant or update a per-collection role by email (owner or sysadmin)",
)
async def create_grant(
    collection_id: str,
    body: GrantCreate,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
) -> GrantOut:
    collection = await _require_manageable(session, collection_id, principal)
    grant = await grant_repo.upsert_grant(
        session,
        collection_id=collection.id,
        email=body.email,
        role=body.role,
        granted_by=principal.email or principal.subject,
    )
    return listing_service.grant_out(grant)


@router.get(
    "/collections/{collection_id}/grants",
    response_model=list[GrantOut],
    summary="List a collection's grants (owner or sysadmin)",
)
async def list_grants(
    collection_id: str,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
) -> list[GrantOut]:
    collection = await _require_manageable(session, collection_id, principal)
    grants = await grant_repo.list_grants(session, collection.id)
    return [listing_service.grant_out(g) for g in grants]


@router.delete(
    "/collections/{collection_id}/grants/{email}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a per-collection grant by email (owner or sysadmin)",
)
async def delete_grant(
    collection_id: str,
    email: str,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    collection = await _require_manageable(session, collection_id, principal)
    if not await grant_repo.delete_grant(session, collection.id, email):
        raise GrantNotFoundError(collection_id, email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
