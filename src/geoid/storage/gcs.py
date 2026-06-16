"""Google Cloud Storage BlobStore (FAO GCP). Imports ``gcsfs`` lazily.

Selected only when ``GEOID_STORAGE_BACKEND=gcs``; install the ``gcs`` extra.
The lazy import keeps the core image free of any GCP SDK dependency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    # Type-only: keeps gcsfs lazy (optional extra) while typing the methods below.
    import gcsfs


class GCSStore:
    def __init__(self, bucket: str) -> None:
        if not bucket:
            raise ValueError("GEOID_STORAGE_GCS_BUCKET is required for the gcs backend")
        try:
            import gcsfs  # noqa: F401  (presence check)
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "gcsfs is not installed; install the 'gcs' extra to use the GCS backend"
            ) from exc
        self._bucket = bucket.rstrip("/")
        self._fs: gcsfs.GCSFileSystem | None = None

    def _filesystem(self) -> gcsfs.GCSFileSystem:
        if self._fs is None:
            import gcsfs

            self._fs = gcsfs.GCSFileSystem()
        return self._fs

    def _path(self, key: str) -> str:
        return f"{self._bucket}/{key.lstrip('/')}"

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        fs = self._filesystem()
        path = self._path(key)

        def _write() -> None:
            with fs.open(path, "wb", content_type=content_type) as handle:
                handle.write(data)

        await anyio.to_thread.run_sync(_write)
        return self.url_for(key)

    async def get(self, key: str) -> bytes:
        fs = self._filesystem()
        return await anyio.to_thread.run_sync(fs.cat_file, self._path(key))

    async def exists(self, key: str) -> bool:
        fs = self._filesystem()
        return await anyio.to_thread.run_sync(fs.exists, self._path(key))

    def url_for(self, key: str) -> str:
        return f"gs://{self._path(key)}"

    async def signed_url(self, key: str, *, expires_seconds: int) -> str:
        # V4 signed URL (gcsfs caps expiry at GCS's 7-day maximum). Runtime SA needs
        # the iam.serviceAccountTokenCreator role to sign without a private key.
        fs = self._filesystem()
        return await anyio.to_thread.run_sync(
            lambda: fs.sign(self._path(key), expiration=expires_seconds)
        )
