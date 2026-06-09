"""Storage backend factory — selects a :class:`BlobStore` from configuration."""

from __future__ import annotations

from functools import lru_cache

from geoid.config import Settings, get_settings
from geoid.storage.base import BlobStore
from geoid.storage.local import LocalFSStore

__all__ = ["BlobStore", "LocalFSStore", "get_blob_store"]


def _build(settings: Settings) -> BlobStore:
    backend = settings.storage_backend.lower()
    if backend == "local":
        return LocalFSStore(settings.storage_local_root)
    if backend == "gcs":
        from geoid.storage.gcs import GCSStore  # lazy: avoids importing gcsfs unless needed

        return GCSStore(settings.storage_gcs_bucket or "")
    # Unreachable for a validated Settings (storage_backend is a Literal); defensive only.
    raise ValueError(  # pragma: no cover
        f"unknown GEOID_STORAGE_BACKEND: {settings.storage_backend!r}"
    )


@lru_cache
def get_blob_store() -> BlobStore:
    return _build(get_settings())
