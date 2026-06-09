"""Workspace + collection schemas (management surface)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class WorkspaceCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspaceOut(BaseModel):
    id: str
    slug: str
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CollectionCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str | None = None
    writable_anon: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class CollectionOut(BaseModel):
    id: str
    workspace_id: str
    slug: str
    title: str | None = None
    writable_anon: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ItemIdList(BaseModel):
    """1.2 slice: list of item-ids (geoids) in a collection."""

    collection: str
    number_matched: int = Field(alias="numberMatched")
    number_returned: int = Field(alias="numberReturned")
    item_ids: list[str]

    model_config = {"populate_by_name": True}
