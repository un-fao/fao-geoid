"""Collection ORM model (≈ STAC collection) — owns places, scopes dedup + anon."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from geoid.db import Base


class Collection(Base):
    __tablename__ = "collection"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug", name="uq_collection_workspace_slug"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace.id"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    # When true this collection accepts anonymous writes (the reserved `public`).
    writable_anon: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # metadata->>'dedup_grid' overrides the per-collection ST_ReducePrecision grid.
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
