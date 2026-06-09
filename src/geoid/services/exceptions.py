"""Service-layer exceptions, mapped to HTTP in ``api/errors.py``."""

from __future__ import annotations


class GeoidServiceError(Exception):
    """Base class for service errors."""


class CollectionNotFoundError(GeoidServiceError):
    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"collection not found: {slug!r}")


class WorkspaceNotFoundError(GeoidServiceError):
    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"workspace not found: {slug!r}")


class GeometryInvalidError(GeoidServiceError):
    """Geometry failed RFC 7946 / ST_IsValid (we reject, never repair)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class AnonymousWriteForbiddenError(GeoidServiceError):
    """Anonymous POST to a collection that is not ``writable_anon``."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"anonymous writes are not permitted to collection {slug!r}")


class PlaceNotFoundError(GeoidServiceError):
    def __init__(self, identifier: str) -> None:
        self.identifier = identifier
        super().__init__(f"place not found: {identifier}")
