"""ImportJob ORM model — the async bulk-import job store (OGC Processes subset).

MUTABLE (like ``collection_grant``): status transitions run as guarded UPDATEs
in ``repositories/job_repo.py`` — no append-only triggers. Postgres is the
single source of truth for job state; ``execution_name`` (the Cloud Run
execution id) is forensics only. ``source_ref`` can carry a presigned-URL query
string (a bearer secret) — never echo it in results, logs, or error text.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from geoid.db import Base

JOB_STATUSES = ("accepted", "running", "successful", "failed", "dismissed")


class ImportJob(Base):
    __tablename__ = "import_job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('accepted','running','successful','failed','dismissed')",
            name="ck_import_job_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("collection.id", name="fk_import_job_collection"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'accepted'"))
    source_ref: Mapped[str] = mapped_column(String, nullable=False)
    source_is_prefix: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_by_email: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    progress: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str | None] = mapped_column(String, nullable=True)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    execution_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
