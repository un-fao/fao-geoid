"""Place ORM model (≈ STAC item) — INSERT-only; ``id`` IS the geoid (UUIDv7).

Immutability is enforced in the database (a ``BEFORE UPDATE OR DELETE`` trigger
raises), not in Python. ``geom_hash`` is set by a ``BEFORE INSERT`` trigger from
the canonical dedup recipe, so it is read-only from the ORM's perspective.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from geoalchemy2.elements import WKBElement
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from geoid.db import Base


class Place(Base):
    __tablename__ = "place"
    __table_args__ = (
        UniqueConstraint("collection_id", "external_id", name="uq_place_collection_external_id"),
        UniqueConstraint("geom_hash", name="uq_place_geom_hash"),
        CheckConstraint(
            "GeometryType(geom) IN ('POLYGON', 'MULTIPOLYGON')",
            name="ck_place_geom_is_polygonal",
        ),
        CheckConstraint("ST_IsValid(geom)", name="ck_place_geom_is_valid"),
    )

    # id == geoid (UUIDv7), minted app-side by the registry service.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collection.id"), nullable=False
    )
    geom: Mapped[WKBElement] = mapped_column(
        Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
        nullable=False,
    )
    # Set by the BEFORE INSERT trigger from the canonical recipe; never written by app code.
    geom_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provenance: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    predecessor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("place.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    originating_instance: Mapped[str | None] = mapped_column(String, nullable=True)
    ingest_batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
