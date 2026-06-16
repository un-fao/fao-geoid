"""Bulk-ingest schemas — the OGC API - Processes surface + the IngestionReport.

ONE ``IngestionReport`` is shared by the synchronous inline execution (returned
directly as the process result) and the asynchronous worker path (persisted on
the job row and fetched via ``GET /jobs/{id}/results``). The OGC DTOs
(``ProcessSummary`` / ``ProcessDescription`` / ``Execute`` / ``StatusInfo``)
mirror OGC 18-062r2 v1.0.0 verbatim, including the status enum
(``accepted | running | successful | failed | dismissed`` — note ``successful``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from geoid.schemas.ogc import Link

# --- The IngestionReport (the bulk-ingest process output) -------------------

# Per-row reject discriminator. Maps 1:1 to the single-row write path's failure
# modes so a bulk reject reads exactly like the 4xx a single POST would return.
RejectReason = Literal[
    "schema_invalid",  # failed the PlaceCreate pydantic schema (422-equivalent)
    "geometry_invalid",  # non-polygon / not ST_IsValid (422-equivalent, DB CHECK)
    "external_id_conflict",  # duplicate (collection, external_id) — 409-equivalent
    "geometry_conflict",  # identical geometry already registered — 409-equivalent
    "geometry_conflict_in_batch",  # identical geometry to an earlier row in THIS batch
]


class AcceptedItem(BaseModel):
    """A feature that minted a new geoid."""

    row_no: int = Field(description="Zero-based index into the input FeatureCollection.")
    geoid: str
    uri: str
    collection: str
    external_id: str | None = None


class RejectedItem(BaseModel):
    """A feature that did not mint, with the reason it was skipped.

    Partial success is the contract: one bad row never aborts the batch. The
    fields mirror the single-row error envelope — ``constraint`` discriminates the
    two 409s, ``incumbent_geoid`` carries the existing registration for a geometry
    conflict (the same payload a single duplicate POST returns).
    """

    row_no: int = Field(description="Zero-based index into the input FeatureCollection.")
    reason: RejectReason
    detail: str | None = Field(default=None, description="Human-readable explanation.")
    constraint: str | None = None
    incumbent_geoid: str | None = Field(
        default=None, description="Existing geoid for a geometry conflict."
    )
    external_id: str | None = None


class IngestionReport(BaseModel):
    """Outcome of a bulk ingest — the ``report`` output of the bulk-ingest process."""

    collection: str
    batch_id: str = Field(description="The place-set id (place.ingest_batch_id) for this ingest.")
    place_set_uri: str | None = Field(
        default=None, description="Resolvable place-set URI for set membership."
    )
    total: int = Field(description="Features in the submitted FeatureCollection.")
    accepted_count: int
    rejected_count: int
    accepted: list[AcceptedItem] = Field(default_factory=list)
    rejected: list[RejectedItem] = Field(default_factory=list)


# --- OGC API - Processes DTOs (18-062r2) ------------------------------------

# OGC StatusInfo status code list (Clause 7.12), verbatim. ``successful`` (not
# ``success``) and ``dismissed`` (from /conf/dismiss) are part of the enum.
JobStatus = Literal["accepted", "running", "successful", "failed", "dismissed"]


class ProcessSummary(BaseModel):
    """A process-list entry (OGC Core)."""

    id: str
    version: str
    title: str | None = None
    description: str | None = None
    # jobControlOptions advertises sync + async execution; outputTransmission is
    # value (inline) — by-reference outputs come with the bulk-export process.
    jobControlOptions: list[Literal["sync-execute", "async-execute"]] = Field(
        default_factory=lambda: ["sync-execute", "async-execute"]
    )
    outputTransmission: list[Literal["value", "reference"]] = Field(
        default_factory=lambda: ["value"]
    )
    links: list[Link] = Field(default_factory=list)


class ProcessList(BaseModel):
    """``GET /processes`` (OGC Core)."""

    processes: list[ProcessSummary] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)


class ProcessDescription(ProcessSummary):
    """``GET /processes/{id}`` — the summary plus the inputs/outputs schema."""

    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)


class Subscriber(BaseModel):
    """OGC /conf/callback: server POSTs the result to these URIs on completion.

    Distinct from the email notification — this is the machine-readable push.
    """

    successUri: str | None = None
    inProgressUri: str | None = None
    failedUri: str | None = None


class Execute(BaseModel):
    """OGC execute request body (``POST /processes/{id}/execution``).

    ``inputs`` is the generic OGC bag; the bulk-ingest process reads
    ``collection`` (slug), ``items`` (a GeoJSON FeatureCollection by value OR
    ``{"href": "..."}`` by reference), and an optional ``idempotencyKey``.
    """

    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] | None = None
    response: Literal["raw", "document"] = "raw"
    subscriber: Subscriber | None = None


class StatusInfo(BaseModel):
    """OGC StatusInfo (Clause 7.12) — the ``GET /jobs/{id}`` body."""

    processID: str
    type: Literal["process"] = "process"
    jobID: str
    status: JobStatus
    message: str | None = None
    created: str | None = None
    started: str | None = None
    finished: str | None = None
    updated: str | None = None
    links: list[Link] = Field(default_factory=list)


# Bulk-export formats. GeoJSON ships now; GeoParquet/FlatGeobuf are a later opt-in
# on the same async path (lazy GDAL/pyogrio in the worker), pinned to GeoParquet
# 1.1.0 / OGC 24-013 in metadata when added.
ExportFormat = Literal["geojson", "geoparquet", "flatgeobuf"]


class ExportInputs(BaseModel):
    """Validated view of an ``Execute.inputs`` bag for the bulk-export process."""

    collection: str
    format: ExportFormat = "geojson"


class ExportResult(BaseModel):
    """The bulk-export process output — a time-limited download reference."""

    href: str = Field(description="Signed download URL (GCS V4, ≤7-day) or a file URI.")
    type: str = Field(description="Media type of the exported object.")
    format: ExportFormat
    collection: str
    count: int = Field(description="Features written.")
    expires_seconds: int | None = Field(default=None, description="URL TTL (signed URLs only).")


class BulkIngestInputs(BaseModel):
    """Validated view of an ``Execute.inputs`` bag for the bulk-ingest process."""

    collection: str
    items: dict[str, Any] = Field(
        description="GeoJSON FeatureCollection (by value) or {'href': '...'} (by reference)."
    )
    idempotencyKey: str | None = None
    notifyEmail: str | None = Field(
        default=None, description="Optional recipient for the async completion email."
    )
