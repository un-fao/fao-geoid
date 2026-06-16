"""async bulk-ingest job queue (ingest_job) + place(ingest_batch_id) index

``ingest_job`` is the durable system of record for ASYNCHRONOUS bulk ingests: the
execute handler commits a row here BEFORE best-effort triggering a Cloud Run Job,
so a dropped trigger is a *delay* (the Scheduler fallback drains it), never a loss.
The worker claims rows with ``SELECT ... FOR UPDATE SKIP LOCKED``.

Two deliberate departures from the place/registry/change_log tables:

- **Status-mutable, so NO immutability / append-only triggers.** The worker moves a
  row accepted -> running -> successful|failed and stamps ``notified_at``; applying
  the ``place`` immutability or hinge append-only triggers here would make every
  worker UPDATE raise (and self-deadlock the queue). Same posture as
  ``dedup_recipe_stamp`` (0003): a CHECK pins the status enum instead of a trigger.
- **No FK to ``place``** (the Citus posture the two hinges already take): a job row
  must outlive a future sharding of ``place``. It DOES FK ``collection`` (a small,
  unsharded reference table) — the collection is validated at enqueue time anyway.

Sync (Tier-1) ingest needs NO row here — its TEMP staging is per-transaction; this
table is for async jobs + idempotency only.

Revision ID: 0004_ingest_jobs
Revises: 0003_dedup_recipe_stamp
Create Date: 2026-06-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_ingest_jobs"
down_revision: str | None = "0003_dedup_recipe_stamp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ingest_job (
            id              uuid PRIMARY KEY,
            collection_id   uuid NOT NULL REFERENCES collection(id),
            process_id      text NOT NULL,
            mode            text NOT NULL,
            -- Exactly one source: an inline FeatureCollection (payload) OR a
            -- by-reference blob (blob_uri, fetched via the BlobStore seam).
            blob_uri        text,
            payload         jsonb,
            status          text NOT NULL DEFAULT 'accepted',
            report          jsonb,
            message         text,
            idempotency_key text UNIQUE,
            notify_email    text,
            subscriber      jsonb,
            notified_at     timestamptz,
            claimed_at      timestamptz,
            started_at      timestamptz,
            finished_at     timestamptz,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_ingest_job_status
                CHECK (status IN ('accepted', 'running', 'successful', 'failed', 'dismissed'))
        );
        """
    )
    # The SKIP LOCKED claim scans accepted rows oldest-first.
    op.execute("CREATE INDEX ix_ingest_job_status_created ON ingest_job (status, created_at);")
    # place.ingest_batch_id is the place-set id; index it so set-membership filters
    # (CQL2 ingest_batch_id='...') don't full-scan place.
    op.execute("CREATE INDEX place_ingest_batch_id_ix ON place (ingest_batch_id);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS place_ingest_batch_id_ix;")
    op.execute("DROP INDEX IF EXISTS ix_ingest_job_status_created;")
    op.execute("DROP TABLE IF EXISTS ingest_job;")
