"""SQLAlchemy ORM models — used for the read path and metadata access.

The schema is owned by Alembic migrations (they carry triggers and functions that
autogenerate cannot express); these models mirror that schema for reads. The write
hot path (place insert + dedup) goes through hand-written SQL in the registry
service so the trigger-computed ``geom_hash`` and ``ON CONFLICT`` semantics are
exercised directly.

Constraint names here MUST match the migration — ``api/errors.py`` switches on
them to map SQLSTATE 23505 to the right HTTP status.
"""

from geoid.models.catalog import Catalog
from geoid.models.change_log import ChangeLog
from geoid.models.collection import Collection
from geoid.models.geoid_registry import GeoidRegistry
from geoid.models.place import Place
from geoid.models.stubs import ApiKey, AuthorityAssertion

__all__ = [
    "Catalog",
    "Collection",
    "Place",
    "GeoidRegistry",
    "ChangeLog",
    "AuthorityAssertion",
    "ApiKey",
]

# Constraint names referenced by error mapping (single source of truth).
UQ_COLLECTION_CATALOG_SLUG = "uq_collection_catalog_slug"
UQ_PLACE_EXTERNAL_ID = "uq_place_collection_external_id"
# The global geometry-dedup UNIQUE lives on geoid_registry (sharding-ready), not place.
UQ_GEOID_REGISTRY_GEOM_HASH = "uq_geoid_registry_geom_hash"
PK_GEOID_REGISTRY = "geoid_registry_pkey"
PK_PLACE = "place_pkey"
