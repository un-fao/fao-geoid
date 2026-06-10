"""Workspace + collection schemas (management surface)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


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

    @field_validator("metadata")
    @classmethod
    def _validate_dedup_grid(cls, metadata: dict[str, Any]) -> dict[str, Any]:
        # The stamped value feeds ::double precision casts in the BEFORE-INSERT
        # trigger, the incumbent-lookup, and migrations — a non-numeric (or
        # explicit-null, which setdefault would preserve as an unstamped hole)
        # value must never reach the database. bool is an int subclass: exclude.
        if "dedup_grid" not in metadata:
            return metadata
        grid = metadata["dedup_grid"]
        if isinstance(grid, bool) or not isinstance(grid, (int, float)) or grid <= 0:
            raise ValueError(
                "metadata.dedup_grid must be a positive number (decimal degrees per vertex)"
            )
        return metadata


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
