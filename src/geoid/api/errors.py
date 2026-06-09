"""Exception handlers — DB constraint violations + service errors → HTTP.

Constraint→HTTP mapping (DB is the source of truth; switch on constraint_name):

    geoid (registry/pk) duplicate          -> 409 (fail)
    (collection_id, external_id) duplicate -> 409 (fail)
    (collection_id, geom_hash) duplicate   -> handled as dedup (200) in the service;
                                              if it ever surfaces here -> 409
    invalid / non-polygon geometry         -> 422 with ST_IsValidReason
    immutability (restrict_violation)       -> 409
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from geoid.models import (
    PK_GEOID_REGISTRY,
    PK_PLACE,
    UQ_PLACE_EXTERNAL_ID,
    UQ_PLACE_GEOM_HASH,
)
from geoid.services.exceptions import (
    AnonymousWriteForbiddenError,
    CollectionNotFoundError,
    GeometryInvalidError,
    PlaceNotFoundError,
    WorkspaceNotFoundError,
)

# PostgreSQL SQLSTATEs we care about.
_SQLSTATE_CHECK_VIOLATION = "23514"
_SQLSTATE_RESTRICT_VIOLATION = "23001"  # raised by the immutability trigger


def _error(status_code: int, message: str, **extra: object) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": status_code, "message": message, **extra},
    )


def _pg_fields(exc: IntegrityError) -> tuple[str | None, str | None]:
    """Extract (constraint_name, sqlstate) from a SQLAlchemy asyncpg IntegrityError.

    SQLAlchemy's asyncpg adapter exposes ``sqlstate`` on ``exc.orig`` but the
    ``constraint_name`` only on the wrapped asyncpg error at ``exc.orig.__cause__``.
    """
    orig = getattr(exc, "orig", None)
    cause = getattr(orig, "__cause__", None)
    constraint = getattr(cause, "constraint_name", None) or getattr(orig, "constraint_name", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(cause, "sqlstate", None)
    return constraint, sqlstate


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(CollectionNotFoundError)
    async def _collection_not_found(_: Request, exc: CollectionNotFoundError) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, str(exc), collection=exc.slug)

    @app.exception_handler(WorkspaceNotFoundError)
    async def _workspace_not_found(_: Request, exc: WorkspaceNotFoundError) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, str(exc), workspace=exc.slug)

    @app.exception_handler(PlaceNotFoundError)
    async def _place_not_found(_: Request, exc: PlaceNotFoundError) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, str(exc))

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

    @app.exception_handler(IntegrityError)
    async def _integrity(_: Request, exc: IntegrityError) -> JSONResponse:
        constraint, sqlstate = _pg_fields(exc)

        if constraint == UQ_PLACE_EXTERNAL_ID:
            return _error(
                status.HTTP_409_CONFLICT,
                "external_id already exists in this collection",
                constraint=constraint,
            )
        if constraint in (PK_GEOID_REGISTRY, PK_PLACE):
            return _error(
                status.HTTP_409_CONFLICT, "geoid already exists", constraint=constraint
            )
        if constraint == UQ_PLACE_GEOM_HASH:
            # Normally handled as a 200 dedup in the service; surfacing here is unexpected.
            return _error(
                status.HTTP_409_CONFLICT,
                "identical geometry already exists in this collection",
                constraint=constraint,
            )
        if sqlstate == _SQLSTATE_CHECK_VIOLATION:
            return _error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "geometry violates a database check (polygon-only / ST_IsValid)",
                constraint=constraint,
            )
        if sqlstate == _SQLSTATE_RESTRICT_VIOLATION:
            return _error(
                status.HTTP_409_CONFLICT,
                "place is immutable; corrections mint a new geoid via predecessor_id",
            )
        return _error(status.HTTP_409_CONFLICT, "integrity constraint violation",
                      constraint=constraint, sqlstate=sqlstate)
