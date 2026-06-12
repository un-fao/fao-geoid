"""Exception handlers — DB constraint violations + service errors → HTTP.

Constraint→HTTP mapping (DB is the source of truth; switch on constraint_name):

    geoid (registry/pk) duplicate          -> 409 (fail)
    (collection_id, external_id) duplicate -> 409 (fail)
    geom_hash duplicate (catalog-wide)     -> 409 carrying the incumbent geoid,
                                              raised as GeometryConflictError by
                                              the service; the IntegrityError
                                              branch is only a backstop
    invalid / non-polygon geometry         -> 422 with ST_IsValidReason
    immutability (restrict_violation)       -> 409
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from geoid.config import get_settings
from geoid.domain.identifiers import derive_identifiers
from geoid.models import (
    PK_GEOID_REGISTRY,
    PK_PLACE,
    UQ_PLACE_EXTERNAL_ID,
    UQ_PLACE_GEOM_HASH,
)
from geoid.services.exceptions import (
    AnonymousWriteForbiddenError,
    CatalogNotFoundError,
    CollectionNotFoundError,
    GeometryConflictError,
    GeometryInvalidError,
    PlaceNotFoundError,
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

    @app.exception_handler(CatalogNotFoundError)
    async def _catalog_not_found(_: Request, exc: CatalogNotFoundError) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, str(exc), catalog=exc.slug)

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

    @app.exception_handler(GeometryConflictError)
    async def _geometry_conflict(_: Request, exc: GeometryConflictError) -> JSONResponse:
        # The ruling: the insert fails AND the body names the existing geoid.
        # did/uri are derived purely (domain.identifiers) so the client can
        # resolve the incumbent without a second request; ``constraint`` lets
        # clients discriminate this 409 from the external_id one.
        settings = get_settings()
        ids = derive_identifiers(
            exc.geoid, base_url=settings.base_url_clean, did_host=settings.did_host or ""
        )
        return _error(
            status.HTTP_409_CONFLICT,
            str(exc),
            geoid=ids["geoid"],
            did=ids["did"],
            uri=ids["uri"],
            collection=exc.collection,
            constraint=UQ_PLACE_GEOM_HASH,
        )

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
            return _error(status.HTTP_409_CONFLICT, "geoid already exists", constraint=constraint)
        if constraint == UQ_PLACE_GEOM_HASH:
            # Backstop only: the service normally raises GeometryConflictError
            # (whose body carries the incumbent geoid — there is no session
            # here to look it up). Surfacing this branch is unexpected.
            return _error(
                status.HTTP_409_CONFLICT,
                "identical geometry already exists in the catalog",
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
        return _error(
            status.HTTP_409_CONFLICT,
            "integrity constraint violation",
            constraint=constraint,
            sqlstate=sqlstate,
        )
