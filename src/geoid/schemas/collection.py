"""Collection schemas (management surface).

Collections are the flat entry point of the API; the catalog tier is internal and
never exposed. The public collection identifier is ``id`` (the slug value), used
directly in URLs — there is no separate internal UUID in responses.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

_ID_FORMAT = (
    "Client-chosen identifier, used directly in URLs. Lowercase letters, digits, "
    "hyphens and underscores only; must start with a letter or digit; max 128 chars."
)


class CollectionCreate(BaseModel):
    id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
        description=_ID_FORMAT,
        examples=["land-parcels"],
    )
    title: str | None = Field(default=None, examples=["Land Parcels"])
    writable_anon: bool = Field(default=False, examples=[False])
    metadata: dict[str, Any] = Field(default_factory=dict, examples=[{}])

    @field_validator("metadata")
    @classmethod
    def _reject_dedup_grid(cls, metadata: dict[str, Any]) -> dict[str, Any]:
        # Geometry dedup is global: there is no per-collection grid. Fail fast
        # rather than silently ignore the key — an admin who sends it believes
        # an override exists, and that belief must not survive.
        if "dedup_grid" in metadata:
            raise ValueError(
                "metadata.dedup_grid is not supported: geometry dedup is global "
                "(one geometry → one geoid across the catalog) with a single "
                "migration-pinned precision grid"
            )
        return metadata


class CollectionOut(BaseModel):
    id: str
    title: str | None = None
    writable_anon: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
