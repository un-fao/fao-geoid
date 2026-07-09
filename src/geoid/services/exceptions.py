"""Service-layer exceptions, mapped to HTTP in ``api/errors.py``."""

from __future__ import annotations

import uuid


class GeoidServiceError(Exception):
    """Base class for service errors."""


class CollectionNotFoundError(GeoidServiceError):
    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"collection not found: {slug!r}")


class GeometryInvalidError(GeoidServiceError):
    """Geometry failed RFC 7946 / ST_IsValid (we reject, never repair)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class AnonymousWriteForbiddenError(GeoidServiceError):
    """Anonymous POST to a collection that is not ``public_write``."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"anonymous writes are not permitted to collection {slug!r}")


class WriteNotAuthorizedError(GeoidServiceError):
    """Authenticated caller without an editor/owner grant writing to a non-open
    collection (403). The anonymous case keeps its own
    :class:`AnonymousWriteForbiddenError` so the 401-vs-403/message split is preserved.
    """

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"write access to collection {slug!r} requires an editor or owner grant")


class CollectionForbiddenError(GeoidServiceError):
    """Authenticated caller lacks owner/sysadmin rights to manage a collection (403)."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"managing collection {slug!r} requires the owner or sysadmin role")


class OwnerGrantForbiddenError(GeoidServiceError):
    """Granting the ``owner`` role is sysadmin-only (403).

    A collection owner may staff editors and viewers but never mint peer owners;
    demoting or revoking an existing owner stays an owner-level operation.
    """

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(
            f"granting the owner role on collection {slug!r} requires the sysadmin role"
        )


class LastOwnerGuardError(GeoidServiceError):
    """Revoking/demoting the LAST owner grant of a collection is blocked (409).

    Applies to everyone, sysadmin included — a sysadmin wanting the owner gone
    grants another owner first. Keeps the invariant one rule with no bypass tier.
    """

    def __init__(self, slug: str, email: str) -> None:
        self.slug = slug
        self.email = email
        super().__init__(
            f"{email!r} is the last owner of collection {slug!r}; "
            "grant another owner before revoking or demoting this one"
        )


class GrantNotFoundError(GeoidServiceError):
    """DELETE of a grant that does not exist (404)."""

    def __init__(self, slug: str, email: str) -> None:
        self.slug = slug
        self.email = email
        super().__init__(f"no grant for {email!r} on collection {slug!r}")


class PlaceNotFoundError(GeoidServiceError):
    def __init__(self, identifier: str) -> None:
        self.identifier = identifier
        super().__init__(f"place not found: {identifier}")


class BulkLimitExceededError(GeoidServiceError):
    """A bulk POST FeatureCollection exceeds ``GEOID_BULK_MAX_FEATURES`` (413).

    A *write* bound must error, never silently truncate, so the whole request is
    rejected with the count and the configured cap.
    """

    def __init__(self, count: int, limit: int) -> None:
        self.count = count
        self.limit = limit
        super().__init__(
            f"bulk ingest rejected: {count} features exceeds the limit of {limit} "
            "(GEOID_BULK_MAX_FEATURES)"
        )


class GeometryConflictError(GeoidServiceError):
    """An identical geometry already exists in the catalog (global dedup, 409).

    ``geoid``/``collection`` name the incumbent only for members of its
    collection (sysadmin / own mint / any grant — ``public_read`` does not
    disclose); both are None otherwise and the 409 body carries null incumbent
    fields.
    """

    def __init__(self, geoid: uuid.UUID | None, collection: str | None) -> None:
        self.geoid = geoid
        self.collection = collection
        super().__init__("identical geometry already exists in the catalog")


class JobNotFoundError(GeoidServiceError):
    """Unknown job id — OR a job the caller may not see (existence-masking 404).

    Mapped to the OGC ``no-such-job`` exception body: a non-creator probing a real
    job id gets the byte-identical response an unknown id gets.
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"no job with id {job_id}")


class JobResultsNotReadyError(GeoidServiceError):
    """Results requested while the job is still accepted/running (OGC 404)."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"results of job {job_id} are not ready")


class JobFailedError(GeoidServiceError):
    """Results requested for a failed job — 500 + the stored failure message (Req 46)."""

    def __init__(self, job_id: str, message: str | None) -> None:
        self.job_id = job_id
        self.message = message
        super().__init__(message or f"job {job_id} failed")


class JobRefRejectedError(GeoidServiceError):
    """Submitted href/prefix failed validation (scheme/host/bucket allowlists, 422)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class JobLaunchError(GeoidServiceError):
    """The executor could not start the import execution (500).

    The job row is already flipped to ``failed`` when this is raised — an
    auditable failure, never an orphan ``accepted`` row.
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__("failed to start import execution")


class TooManyJobsError(GeoidServiceError):
    """Submit-time concurrency valve: too many active import jobs (429)."""

    def __init__(self, active: int, limit: int) -> None:
        self.active = active
        self.limit = limit
        super().__init__(
            f"import job rejected: {active} jobs already active, limit is {limit} "
            "(GEOID_JOB_MAX_CONCURRENT); retry after one finishes"
        )


class RegistryConsistencyError(GeoidServiceError):
    """A dedup loser whose incumbent collection never materialised (500).

    Should not happen: the geoid is derived in-statement so a loser always carries
    it, and ``_resolve_incumbent_slug`` recovers the collection slug across the rare
    concurrent pre-commit window. A persistent miss means genuine registry/recipe
    drift — surfaced as a structured 500 (carrying the geoid) instead of an
    unstructured crash.
    """

    def __init__(self, geoid: uuid.UUID) -> None:
        self.geoid = geoid
        super().__init__(f"registry inconsistency: incumbent collection missing for geoid {geoid}")
