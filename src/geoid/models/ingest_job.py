"""Ingest job ORM model — the durable async bulk-ingest queue (status-mutable).

Unlike ``place`` / ``geoid_registry`` / ``change_log`` this row is UPDATEd through
its lifecycle (accepted -> running -> successful|failed|dismissed) by the worker, so
it deliberately carries NO immutability / append-only triggers (migration 0004) — a
CHECK pins the status enum instead. No FK to ``place`` (Citus posture); it FKs
``collection`` only.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from geoid.db import Base

# OGC StatusInfo status code list (Clause 7.12). Pinned by the 0004 CHECK.
JOB_STATUSES = ("accepted", "running", "successful", "failed", "dismissed")


class IngestJob(Base):
    __tablename__ = "ingest_job"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collection.id"), nullable=False
    )
    process_id: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    # Exactly one source: inline FeatureCollection (payload) or by-reference blob.
    blob_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'accepted'"))
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    message: Mapped[str | None] = mapped_column(String, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    notify_email: Mapped[str | None] = mapped_column(String, nullable=True)
    subscriber: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
