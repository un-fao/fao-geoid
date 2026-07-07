"""Async-import job schemas — the OGC API - Processes Part 1 v1.0 subset.

``StatusInfo`` mirrors 18-062r2's ``statusInfo.yaml`` **verbatim** (v1.0 field
names — the draft 2.0 renames are deliberately NOT adopted): required ``jobID``/
``status``/``type: "process"``; the five-value status enum; optional
``processID``/``message``/``progress``/``created``/``started``/``finished``/
``updated``/``links``. Serialize with ``exclude_none`` so absent optionals are
omitted, not null.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from geoid.schemas.place import BulkAccepted, BulkRejected, BulkSummary

JobStatus = Literal["accepted", "running", "successful", "failed", "dismissed"]

# The one process this subset exposes (no /processes discovery surface).
IMPORT_PROCESS_ID = "import"


class JobLink(BaseModel):
    """OGC link object (the subset statusInfo carries)."""

    href: str
    rel: str | None = None
    type: str | None = None
    title: str | None = None


class StatusInfo(BaseModel):
    """18-062r2 §7.12 statusInfo — v1.0 wire names, verbatim."""

    processID: str | None = IMPORT_PROCESS_ID
    type: Literal["process"] = "process"
    jobID: str
    status: JobStatus
    message: str | None = None
    created: datetime | None = None
    started: datetime | None = None
    finished: datetime | None = None
    updated: datetime | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    links: list[JobLink] | None = None


class ImportSubmission(BaseModel):
    """POST /collections/{id}/items/import body — exactly one of href/prefix.

    ``href``: one object, an HTTPS (pre)signed URL or a native ``gs://`` ref.
    ``prefix``: a ``gs://bucket/path/`` prefix — every matching object under it.
    """

    model_config = ConfigDict(extra="forbid")

    href: str | None = None
    prefix: str | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> ImportSubmission:
        if (self.href is None) == (self.prefix is None):
            raise ValueError("provide exactly one of 'href' or 'prefix'")
        return self

    @property
    def ref(self) -> str:
        return self.href if self.href is not None else self.prefix  # type: ignore[return-value]

    @property
    def is_prefix(self) -> bool:
        return self.prefix is not None


class ImportFileSummary(BulkSummary):
    """Per-file counts — the sync BulkSummary shape, reused verbatim."""


class ImportFileReport(BaseModel):
    """One source blob's outcome. Indices are within-file (per-blob 0-based);
    ``source`` is the query-stripped ref (a signed URL's query is a bearer secret)."""

    source: str
    summary: ImportFileSummary
    accepted: list[BulkAccepted] = Field(default_factory=list)
    rejected: list[BulkRejected] = Field(default_factory=list)


class ImportResultsSummary(BaseModel):
    files: int
    received: int
    accepted: int
    rejected: int


class ImportResults(BaseModel):
    """The /jobs/{id}/results document — uniform for single-blob and prefix jobs."""

    summary: ImportResultsSummary
    files: list[ImportFileReport] = Field(default_factory=list)


class OgcException(BaseModel):
    """RFC 7807-shaped OGC exception body (no-such-job / result-not-ready / failed)."""

    type: str
    title: str | None = None
    status: int | None = None
    detail: str | None = None


def build_results(file_reports: list[ImportFileReport]) -> ImportResults:
    """Aggregate per-file reports into the uniform results document."""
    return ImportResults(
        summary=ImportResultsSummary(
            files=len(file_reports),
            received=sum(f.summary.received for f in file_reports),
            accepted=sum(f.summary.accepted for f in file_reports),
            rejected=sum(f.summary.rejected for f in file_reports),
        ),
        files=file_reports,
    )


def status_info_from_job(job: Any, *, links: list[JobLink] | None = None) -> StatusInfo:
    """Map an ImportJob row to the v1.0 statusInfo wire shape."""
    return StatusInfo(
        jobID=str(job.id),
        status=job.status,
        message=job.message,
        created=job.created_at,
        started=job.started_at,
        finished=job.finished_at,
        updated=job.updated_at,
        progress=job.progress,
        links=links,
    )
