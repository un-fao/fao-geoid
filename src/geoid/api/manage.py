"""Management router (admin-gated) — workspace/collection create + the 1.2 listing slice."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.api.paging import enforce_max_offset
from geoid.config import Settings, get_settings
from geoid.db import get_session
from geoid.deps import require_admin
from geoid.repositories import collection_repo, workspace_repo
from geoid.schemas.collection import (
    CollectionCreate,
    CollectionOut,
    ItemIdList,
    WorkspaceCreate,
    WorkspaceOut,
)
from geoid.services import listing_service
from geoid.services.exceptions import WorkspaceNotFoundError

router = APIRouter(prefix="/manage", tags=["manage"], dependencies=[Depends(require_admin)])


@router.post("/workspaces", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceCreate, session: AsyncSession = Depends(get_session)
) -> WorkspaceOut:
    workspace = await workspace_repo.create(
        session, slug=body.slug, title=body.title, metadata=body.metadata
    )
    return WorkspaceOut(
        id=str(workspace.id), slug=workspace.slug, title=workspace.title, metadata=workspace.meta
    )


@router.get("/workspaces", response_model=list[WorkspaceOut])
async def list_workspaces(session: AsyncSession = Depends(get_session)) -> list[WorkspaceOut]:
    return await listing_service.list_workspaces(session)


@router.post(
    "/workspaces/{workspace_slug}/collections",
    response_model=CollectionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_collection(
    workspace_slug: str,
    body: CollectionCreate,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> CollectionOut:
    workspace = await workspace_repo.get_by_slug(session, workspace_slug)
    if workspace is None:
        raise WorkspaceNotFoundError(workspace_slug)
    # Stamp the configured precision default unless the admin supplied one (theirs
    # wins). The stamped value in metadata is the source of truth read by both the
    # BEFORE-INSERT trigger and the incumbent-lookup, so they can't drift.
    metadata = dict(body.metadata)
    metadata.setdefault("dedup_grid", settings.dedup_grid_default)
    collection = await collection_repo.create(
        session,
        workspace_id=workspace.id,
        slug=body.slug,
        title=body.title,
        writable_anon=body.writable_anon,
        metadata=metadata,
    )
    return CollectionOut(
        id=str(collection.id),
        workspace_id=str(collection.workspace_id),
        slug=collection.slug,
        title=collection.title,
        writable_anon=collection.writable_anon,
        metadata=collection.meta,
    )


@router.get(
    "/workspaces/{workspace_slug}/collections",
    response_model=list[CollectionOut],
    summary="List collections in a workspace (1.2)",
)
async def list_collections_in_workspace(
    workspace_slug: str, session: AsyncSession = Depends(get_session)
) -> list[CollectionOut]:
    return await listing_service.list_collections_in_workspace(session, workspace_slug)


@router.get(
    "/collections/{collection_id}/item-ids",
    response_model=ItemIdList,
    summary="List item-ids (geoids) in a collection (1.2)",
)
async def list_item_ids(
    collection_id: str,
    limit: int = Query(default=1000, ge=1, le=100_000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ItemIdList:
    enforce_max_offset(offset, settings)
    return await listing_service.list_item_ids(
        session, collection_id, limit=limit, offset=offset
    )
