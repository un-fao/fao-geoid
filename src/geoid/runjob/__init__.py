"""Cloud Run Jobs trigger seam — best-effort ``jobs.run`` for the async worker.

The web service commits the ingest_job row first, then calls :func:`trigger_ingest_worker`
to wake a worker promptly. This is a *fallible, quota-gated* API call by design: a
failure is non-fatal because the durable row makes a missed trigger a delay (the
Cloud Scheduler fallback drains it), not a loss. Plain drain (no overrides) → the
caller needs only ``roles/run.invoker`` on the Job, not ``run.jobs.runWithOverrides``.

The Google client is imported lazily (mirroring ``GCSStore``/``gcsfs``): an on-prem
deploy of the same image that never sets ``GEOID_INGEST_JOB_NAME`` never imports it.
"""

from __future__ import annotations

import logging
import os

from geoid.config import Settings

logger = logging.getLogger("geoid.runjob")


def _default_project() -> str | None:
    return os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")


async def trigger_ingest_worker(settings: Settings) -> bool:
    """Best-effort: request one execution of the ingest worker Job. Never raises.

    Returns True iff the run was requested. Unconfigured (``ingest_job_name`` unset,
    e.g. on-prem / tests) is a clean no-op — the queue is drained by the Scheduler
    fallback or a manual ``geoid ingest-worker`` run.
    """
    if not settings.ingest_job_name or not settings.ingest_job_region:
        return False

    project = settings.ingest_job_project or _default_project()
    if not project:
        logger.warning("ingest job trigger skipped: no GCP project resolved")
        return False

    try:
        from google.cloud import run_v2
    except ImportError:
        logger.warning("google-cloud-run not installed; cannot trigger ingest job")
        return False

    name = (
        f"projects/{project}/locations/{settings.ingest_job_region}/jobs/{settings.ingest_job_name}"
    )
    try:
        client = run_v2.JobsAsyncClient()
        # jobs.run returns a long-running Operation immediately; do NOT await it.
        await client.run_job(name=name)
        logger.info("triggered ingest worker job %s", name)
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort; the fallback drain covers us
        logger.warning("ingest job trigger failed (will drain via fallback): %s", exc)
        return False
