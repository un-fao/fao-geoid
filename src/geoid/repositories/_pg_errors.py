"""PostgreSQL error introspection — the SQLSTATE/constraint-name seam.

Lives in the repository layer (the SQL boundary) so BOTH the API error mapper
(``api/errors.py``) and the registry service (``services/registry_service.py``)
can import it without ``services`` reaching back into ``api`` — the import
inversion the per-feature bulk classification would otherwise create.
"""

from __future__ import annotations

from sqlalchemy.exc import DBAPIError, IntegrityError

# PostgreSQL SQLSTATEs the write path discriminates on.
SQLSTATE_CHECK_VIOLATION = "23514"  # invalid / unsupported-type / empty geometry (DB CHECK)
SQLSTATE_RESTRICT_VIOLATION = "23001"  # raised by the immutability trigger


def sqlstate_of(exc: DBAPIError) -> str | None:
    """The SQLSTATE on a SQLAlchemy asyncpg error (``exc.orig.sqlstate``)."""
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


def pg_fields(exc: IntegrityError) -> tuple[str | None, str | None]:
    """Extract ``(constraint_name, sqlstate)`` from a SQLAlchemy asyncpg IntegrityError.

    SQLAlchemy's asyncpg adapter exposes ``sqlstate`` on ``exc.orig`` but the
    ``constraint_name`` only on the wrapped asyncpg error at ``exc.orig.__cause__``.
    """
    orig = getattr(exc, "orig", None)
    cause = getattr(orig, "__cause__", None)
    constraint = getattr(cause, "constraint_name", None) or getattr(orig, "constraint_name", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(cause, "sqlstate", None)
    return constraint, sqlstate
