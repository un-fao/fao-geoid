"""async import jobs (OGC API - Processes subset)

Adds ``import_job``: the job store for the async storage-blob bulk import
(``POST /collections/{id}/items/import`` → poll ``GET /jobs/{id}``). Postgres is
the single source of truth for job state; the Cloud Run execution is forensics
only (``execution_name``). Like ``collection_grant`` this table is **MUTABLE**
(status transitions via guarded UPDATEs) — deliberately NO append-only triggers,
so the user-trigger inventory stays at 7.

``source_ref`` may be a presigned HTTPS URL whose query string is a bearer
secret — it is stored for the worker but never echoed in results, logs, or
error text. ``report`` is the full results document, written ONCE at success
(write-once/read-whole; bounded by GEOID_JOB_MAX_FEATURES). No secondary
indexes: the PK covers every current query — add ``(created_by, created_at)``
with a future ``GET /jobs`` listing. No retention sweep yet: add a 90-day
cleanup when the table matters.

Identity/dedup SQL untouched — NO recipe-stamp row.

Revision ID: 0010_import_jobs
Revises: 0009_public_read
Create Date: 2026-07-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_import_jobs"
down_revision: str | None = "0009_public_read"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE import_job (
            id                uuid PRIMARY KEY,
            collection_id     uuid NOT NULL,
            status            text NOT NULL DEFAULT 'accepted'
                              CONSTRAINT ck_import_job_status
                              CHECK (status IN ('accepted','running','successful','failed','dismissed')),
            source_ref        text NOT NULL,
            source_is_prefix  boolean NOT NULL DEFAULT false,
            created_by        text NOT NULL,
            created_by_email  text,
            created_by_admin  boolean NOT NULL DEFAULT false,
            progress          integer,
            message           text,
            report            jsonb,
            execution_name    text,
            created_at        timestamptz NOT NULL DEFAULT now(),
            started_at        timestamptz,
            finished_at       timestamptz,
            updated_at        timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_import_job_collection
                FOREIGN KEY (collection_id) REFERENCES collection(id)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS import_job;")
