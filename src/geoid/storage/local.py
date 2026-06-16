"""Local filesystem BlobStore — the on-prem default (and the test default)."""

from __future__ import annotations

from pathlib import Path

import anyio


class LocalFSStore:
    """Stores blobs under a root directory. Async methods offload blocking IO."""

    def __init__(self, root: str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Prevent path traversal: keys are treated as relative, '..' stripped.
        safe = Path(key.lstrip("/"))
        parts = [p for p in safe.parts if p not in ("..", ".")]
        return self._root.joinpath(*parts)

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        path = self._path(key)
        await anyio.to_thread.run_sync(lambda: path.parent.mkdir(parents=True, exist_ok=True))
        await anyio.to_thread.run_sync(path.write_bytes, data)
        return self.url_for(key)

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        return await anyio.to_thread.run_sync(path.read_bytes)

    async def exists(self, key: str) -> bool:
        path = self._path(key)
        return await anyio.to_thread.run_sync(path.exists)

    def url_for(self, key: str) -> str:
        return self._path(key).resolve().as_uri()

    async def signed_url(self, key: str, *, expires_seconds: int) -> str:
        # The local store cannot sign; the file URI IS the reference (on-prem the
        # blob is served by the deployment's own static/file handler).
        return self.url_for(key)
