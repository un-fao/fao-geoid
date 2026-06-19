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
    """Anonymous POST to a collection that is not ``writable_anon``."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"anonymous writes are not permitted to collection {slug!r}")


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

    Carries the incumbent geoid + its collection so the 409 body can point the
    client at the existing registration (Remi/Ken's ruling: the insert fails
    AND the response names the existing geoid).
    """

    def __init__(self, geoid: uuid.UUID, collection: str) -> None:
        self.geoid = geoid
        self.collection = collection
        super().__init__("identical geometry already exists in the catalog")


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
