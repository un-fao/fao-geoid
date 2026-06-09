"""Geoid registry (HINGE 1) — global geoid uniqueness as a thin separate table.

Global uniqueness lives HERE, not as a ``UNIQUE`` on ``place``. That separation is
what lets ``place`` be partitioned by ``collection_id`` and later Citus-sharded
(Citus requires unique keys to include the distribution column; a global
``UNIQUE(geoid)`` on a sharded ``place`` would be impossible, but a reference-table
registry stays globally unique). Populated by an ``AFTER INSERT`` trigger on place.
"""

from __future__ import annotations

import uuid

from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from geoid.db import Base


class GeoidRegistry(Base):
    __tablename__ = "geoid_registry"

    geoid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    place_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    collection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
