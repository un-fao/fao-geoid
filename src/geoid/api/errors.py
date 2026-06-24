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
    CollectionNotFoundError,
    GeometryConflictError,
    GeometryInvalidError,
    PlaceNotFoundError,
    RegistryConsistencyError,
)

logger = logging.getLogger(__name__)


def _error(status_code: int, message: str, **extra: object) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": status_code, "message": message, **extra},
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

    @app.exception_handler(GeometryConflictError)
    async def _geometry_conflict(_: Request, exc: GeometryConflictError) -> JSONResponse:
        # The ruling: the insert fails AND the body names the existing geoid.
        # uri is derived purely (domain.identifiers) so the client can resolve
        # the incumbent without a second request; ``constraint`` lets clients
        # discriminate this 409 from the external_id one.
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
            # here to look it up). Surfacing this branch is unexpected.
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
        return _error(
            status.HTTP_409_CONFLICT,
            "integrity constraint violation",
            constraint=constraint,
            sqlstate=sqlstate,
        )
