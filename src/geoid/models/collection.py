"""Collection ORM model (≈ STAC collection) — owns places, scopes anonymous writes."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from geoid.db import Base


class Collection(Base):
    __tablename__ = "collection"
    __table_args__ = (UniqueConstraint("catalog_id", "slug", name="uq_collection_catalog_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    catalog_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalog.id"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    # When true anyone may write (anonymous included — the reserved `public`).
    # Mapped onto the legacy `writable_anon` DB column: the 2026-07-09 rename is
    # API/ORM-only, deliberately without a migration. Use `public_write` in
    # Python, `writable_anon` in SQL.
    public_write: Mapped[bool] = mapped_column(
        "writable_anon", Boolean, nullable=False, server_default=text("false")
    )
    # When false, reads are grant-gated (404-masked); see authz_service.can_read.
    # default=True alongside server_default avoids an async refetch after flush.
    public_read: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
