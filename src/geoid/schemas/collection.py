"""Collection schemas (management surface).

Collections are the flat entry point of the API; the catalog tier is internal and
never exposed. The public collection identifier is ``id`` (the slug value), used
directly in URLs — there is no separate internal UUID in responses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

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
    public_read: bool = Field(
        default=True,
        description="When false, features are 404-masked on the external-id "
        "resolver unless the caller holds a grant on this collection (viewer or "
        "above) or is sysadmin. Does not affect the dedup-409 incumbent, which is "
        "always masked for non-members.",
        examples=[True],
    )
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
    public_read: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class GrantCreate(BaseModel):
    """Grant (or re-grant) a per-collection role to a principal by email.

    ``viewer`` is the read tier: on a non-public collection it unlocks the
    resolvers (no more 404 mask) and the dedup-409 incumbent disclosure; it does
    NOT authorize writes (``editor``+) or grant management (``owner``). The
    ``owner`` role itself is sysadmin-only to grant — a collection owner may
    grant ``editor``/``viewer`` only.
    """

    email: str = Field(min_length=3, max_length=320, examples=["alice@example.org"])
    role: Literal["owner", "editor", "viewer"] = Field(examples=["editor"])

    @field_validator("email")
    @classmethod
    def _email_shape(cls, email: str) -> str:
        # Light structural check (grants are keyed by email; the repo normalises).
        # Not a full RFC 5322 parser — just reject the obviously malformed.
        stripped = email.strip()
        if "@" not in stripped or stripped.startswith("@") or stripped.endswith("@"):
            raise ValueError("email must contain a local part and a domain")
        return stripped


class GrantOut(BaseModel):
    email: str
    role: str
    subject: str | None = None
    granted_by: str | None = None
    created_at: datetime
