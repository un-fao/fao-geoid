"""The ``BlobStore`` Protocol — object storage behind a swappable seam.

Keeping storage behind a Protocol + env config is what lets the *same image* be
the FAO-GCP artifact and the on-prem artifact: the core imports no GCP SDK, and
``GCSStore`` only imports ``gcsfs`` when explicitly selected.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class BlobStore(Protocol):
    """Minimal async object-store contract used by export/import seams."""

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        """Store ``data`` at ``key`` and return a resolvable reference (URL or path)."""
        ...

    async def get(self, key: str) -> bytes:
        """Fetch the bytes stored at ``key``. Raises if absent."""
        ...

    async def exists(self, key: str) -> bool:
        """True iff an object exists at ``key``."""
        ...

    def url_for(self, key: str) -> str:
        """Return a stable reference (file URI or gs:// / https:// URL) for ``key``."""
        ...
