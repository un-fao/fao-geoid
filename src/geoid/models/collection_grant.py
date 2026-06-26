"""CollectionGrant ORM model — per-collection RBAC (owner/editor/viewer).

MUTABLE (unlike ``place`` / ``geoid_registry`` / ``change_log``): grants are
upserted and revoked, so there are no append-only triggers. Keyed by a normalised
(lower-cased) ``principal_email``; ``principal_subject`` (the Keycloak ``sub``) is
backfilled on first authorized access. Constraint names mirror migration 0006 and
are exported from ``models/__init__.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from geoid.db import Base


class CollectionGrant(Base):
    __tablename__ = "collection_grant"
    __table_args__ = (
        UniqueConstraint(
            "collection_id",
            "principal_type",
            "principal_email",
            name="uq_collection_grant_principal",
        ),
        CheckConstraint("role IN ('owner', 'editor', 'viewer')", name="ck_collection_grant_role"),
        CheckConstraint(
            "principal_type IN ('user', 'group')", name="ck_collection_grant_principal_type"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("collection.id", ondelete="CASCADE", name="fk_collection_grant_collection"),
        nullable=False,
    )
    principal_type: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'user'")
    )
    principal_email: Mapped[str] = mapped_column(String, nullable=False)
    principal_subject: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String, nullable=False)
    granted_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
