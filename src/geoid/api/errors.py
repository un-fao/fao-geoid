"""Exception handlers — DB constraint violations + service errors → HTTP.

Constraint→HTTP mapping (DB is the source of truth; switch on constraint_name):

    geoid (registry/pk) duplicate          -> 409 (fail)
    (collection_id, external_id) duplicate -> 409 (fail)
    geom_hash duplicate (catalog-wide)     -> 409 carrying the incumbent geoid,
                                              raised as GeometryConflictError by
                                              the service; the IntegrityError
                                              branch is only a backstop
    invalid / unsupported-type / empty geom -> 422 with ST_IsValidReason
    immutability (restrict_violation)       -> 409
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from geoid.config import get_settings
from geoid.domain.identifiers import derive_identifiers
from geoid.models import (
    PK_GEOID_REGISTRY,
    PK_PLACE,
    UQ_COLLECTION_CATALOG_SLUG,
    UQ_GEOID_REGISTRY_GEOM_HASH,
    UQ_PLACE_EXTERNAL_ID,
)
from geoid.repositories._pg_errors import (
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_RESTRICT_VIOLATION,
    pg_fields,
)
from geoid.services.exceptions import (
    AnonymousWriteForbiddenError,
    BulkLimitExceededError,
    CollectionForbiddenError,
    CollectionNotFoundError,
    GeometryConflictError,
    GeometryInvalidError,
    GrantNotFoundError,
    JobFailedError,
    JobLaunchError,
    JobNotFoundError,
    JobRefRejectedError,
    JobResultsNotReadyError,
    LastOwnerGuardError,
    PlaceNotFoundError,
    RegistryConsistencyError,
    TooManyJobsError,
    WriteNotAuthorizedError,
)

logger = logging.getLogger(__name__)

# OGC API - Processes Part 1 v1.0 registered exception types (RFC 7807-shaped
# bodies; used by the /jobs read surface, not the standard envelope below).
_OGC_EXC = "http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0"


def _error(status_code: int, message: str, **extra: object) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": status_code, "message": message, **extra},
    )


def _ogc_exception(status_code: int, exc_type: str, title: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"type": exc_type, "title": title, "status": status_code, "detail": detail},
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(CollectionNotFoundError)
    async def _collection_not_found(_: Request, exc: CollectionNotFoundError) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, str(exc), collection=exc.slug)

    @app.exception_handler(PlaceNotFoundError)
    async def _place_not_found(_: Request, exc: PlaceNotFoundError) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, str(exc))

    @app.exception_handler(BulkLimitExceededError)
    async def _bulk_limit_exceeded(_: Request, exc: BulkLimitExceededError) -> JSONResponse:
        return _error(status.HTTP_413_CONTENT_TOO_LARGE, str(exc), count=exc.count, limit=exc.limit)

    @app.exception_handler(GeometryInvalidError)
    async def _geometry_invalid(_: Request, exc: GeometryInvalidError) -> JSONResponse:
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "geometry rejected (reject-don't-repair)",
            reason=exc.reason,
        )

    @app.exception_handler(AnonymousWriteForbiddenError)
    async def _anon_forbidden(_: Request, exc: AnonymousWriteForbiddenError) -> JSONResponse:
        return _error(status.HTTP_403_FORBIDDEN, str(exc), collection=exc.slug)

    @app.exception_handler(WriteNotAuthorizedError)
    async def _write_not_authorized(_: Request, exc: WriteNotAuthorizedError) -> JSONResponse:
        return _error(status.HTTP_403_FORBIDDEN, str(exc), collection=exc.slug)

    @app.exception_handler(CollectionForbiddenError)
    async def _collection_forbidden(_: Request, exc: CollectionForbiddenError) -> JSONResponse:
        return _error(status.HTTP_403_FORBIDDEN, str(exc), collection=exc.slug)

    @app.exception_handler(GrantNotFoundError)
    async def _grant_not_found(_: Request, exc: GrantNotFoundError) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, str(exc), collection=exc.slug, email=exc.email)

    @app.exception_handler(LastOwnerGuardError)
    async def _last_owner_guard(_: Request, exc: LastOwnerGuardError) -> JSONResponse:
        return _error(status.HTTP_409_CONFLICT, str(exc), collection=exc.slug, email=exc.email)

    @app.exception_handler(GeometryConflictError)
    async def _geometry_conflict(_: Request, exc: GeometryConflictError) -> JSONResponse:
        # The insert fails AND the body names the existing geoid — when the
        # service disclosed it. A masked conflict (exc.geoid is None) keeps the
        # identical message with null incumbent fields; ``constraint`` still
        # discriminates this 409 from the external_id one.
        if exc.geoid is None:
            return _error(
                status.HTTP_409_CONFLICT,
                str(exc),
                geoid=None,
                uri=None,
                collection=None,
                constraint=UQ_GEOID_REGISTRY_GEOM_HASH,
            )
        settings = get_settings()
        ids = derive_identifiers(exc.geoid, base_url=settings.base_url_clean)
        return _error(
            status.HTTP_409_CONFLICT,
            str(exc),
            geoid=ids["geoid"],
            uri=ids["uri"],
            collection=exc.collection,
            constraint=UQ_GEOID_REGISTRY_GEOM_HASH,
        )

    @app.exception_handler(JobNotFoundError)
    async def _job_not_found(_: Request, exc: JobNotFoundError) -> JSONResponse:
        # Existence-masking: unknown id and not-the-creator answer identically.
        return _ogc_exception(
            status.HTTP_404_NOT_FOUND, f"{_OGC_EXC}/no-such-job", "no such job", str(exc)
        )

    @app.exception_handler(JobResultsNotReadyError)
    async def _job_results_not_ready(_: Request, exc: JobResultsNotReadyError) -> JSONResponse:
        return _ogc_exception(
            status.HTTP_404_NOT_FOUND,
            f"{_OGC_EXC}/result-not-ready",
            "result not ready",
            str(exc),
        )

    @app.exception_handler(JobFailedError)
    async def _job_failed(_: Request, exc: JobFailedError) -> JSONResponse:
        # Req 46: a failed job's /results carries the stored failure message in an
        # RFC 7807-shaped body. No registered OGC type exists for this case, so
        # about:blank (the status code carries the semantics).
        return _ogc_exception(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "about:blank", "import job failed", str(exc)
        )

    @app.exception_handler(JobRefRejectedError)
    async def _job_ref_rejected(_: Request, exc: JobRefRejectedError) -> JSONResponse:
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    @app.exception_handler(JobLaunchError)
    async def _job_launch_failed(_: Request, exc: JobLaunchError) -> JSONResponse:
        logger.exception("import job %s failed to launch", exc.job_id)
        return _error(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc), job_id=exc.job_id)

    @app.exception_handler(TooManyJobsError)
    async def _too_many_jobs(_: Request, exc: TooManyJobsError) -> JSONResponse:
        return _error(
            status.HTTP_429_TOO_MANY_REQUESTS, str(exc), active=exc.active, limit=exc.limit
        )

    @app.exception_handler(RegistryConsistencyError)
    async def _registry_consistency(_: Request, exc: RegistryConsistencyError) -> JSONResponse:
        # Should-not-happen registry/recipe drift: a dedup loser whose incumbent
        # collection never materialised. Registering a SPECIFIC-type handler (not a
        # bare Exception handler) keeps this on Starlette's ExceptionMiddleware, which
        # returns the response instead of the ServerErrorMiddleware re-raise the test
        # client trips on. logger.exception captures the traceback for ops.
        logger.exception("registry consistency error for geoid %s", exc.geoid)
        return _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal registry inconsistency",
        )

    @app.exception_handler(IntegrityError)
    async def _integrity(_: Request, exc: IntegrityError) -> JSONResponse:
        constraint, sqlstate = pg_fields(exc)

        if constraint == UQ_COLLECTION_CATALOG_SLUG:
            return _error(
                status.HTTP_409_CONFLICT,
                "collection id already exists",
                constraint=constraint,
            )
        if constraint == UQ_PLACE_EXTERNAL_ID:
            return _error(
                status.HTTP_409_CONFLICT,
                "external_id already exists in this collection",
                constraint=constraint,
            )
        if constraint in (PK_GEOID_REGISTRY, PK_PLACE):
            return _error(status.HTTP_409_CONFLICT, "geoid already exists", constraint=constraint)
        if constraint == UQ_GEOID_REGISTRY_GEOM_HASH:
            # Backstop only: the service normally raises GeometryConflictError
            # (whose body carries the incumbent geoid — there is no session
            # here to look it up). Surfacing this branch is unexpected, so log it.
            logger.error(
                "geom_hash 409 served from the IntegrityError backstop "
                "(no incumbent geoid in the body)",
                exc_info=exc,
            )
            return _error(
                status.HTTP_409_CONFLICT,
                "identical geometry already exists in the catalog",
                constraint=constraint,
            )
        if sqlstate == SQLSTATE_CHECK_VIOLATION:
            return _error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "geometry violates a database check (supported-type / not-empty / ST_IsValid)",
                constraint=constraint,
            )
        if sqlstate == SQLSTATE_RESTRICT_VIOLATION:
            return _error(
                status.HTTP_409_CONFLICT,
                "place is immutable; corrections mint a new geoid via predecessor_id",
            )
        # Unrecognised integrity failure — a NOT NULL violation (23502) is a server
        # bug, and pg_fields degrading to (None, None) on a driver change lands here
        # too. Presenting either as the client's 409 conflict would mislabel it:
        # log loud (stack + Postgres DETAIL) and answer an honest 500.
        logger.error(
            "unclassified integrity violation: constraint=%r sqlstate=%r",
            constraint,
            sqlstate,
            exc_info=exc,
        )
        return _error(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal error")
