"""The async-import worker — one job per process run (`geoid import`).

Flow: claim (CAS — a re-executed worker no-ops) → rebuild the frozen submitter
Principal → resolve sources (one href, or every matching object under a gs://
prefix) → per file: **spool to disk first, then DB work** (a transaction is
never held across network I/O — the 60s idle-in-tx timeout would kill it) →
streaming parse → mint in chunks through the SAME ``create_places_bulk`` the
sync route uses (per-feature SAVEPOINT machinery untouched, so sync and async
minting can never drift) → per-chunk commit with the heartbeat as the LAST
statement of the chunk transaction (atomic with the work it reports; zero rows
= the row was flipped externally → abort, never resurrect) → aggregate →
``finish(successful, report)``.

Failure semantics: the JOB is ``successful`` even if every feature rejected —
``failed`` is reserved for fetch/parse/infra faults. Committed chunks persist
across a failure; a resubmit converges via global dedup (every already-minted
feature reports ``geometry_conflict``). Error messages are sanitized: never the
URL query (a signed URL is a bearer secret), never upstream response bodies.

``transport`` (httpx) and ``storage_client_factory`` (GCS) are the test seams.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from sqlalchemy.exc import InterfaceError, OperationalError

from geoid.config import Settings, get_settings
from geoid.db import get_sessionmaker
from geoid.deps import Principal
from geoid.models import Collection, ImportJob
from geoid.repositories import job_repo
from geoid.schemas.job import ImportFileReport, ImportFileSummary, ImportResults, build_results
from geoid.schemas.place import BulkAccepted, BulkRejected, BulkReport
from geoid.services import import_refs, registry_service
from geoid.services.exceptions import (
    AnonymousWriteForbiddenError,
    WriteNotAuthorizedError,
)

logger = logging.getLogger(__name__)

CHUNK_SIZE = 500
_READ_BLOCK = 65536
_GS_SUFFIXES = (".json", ".geojson", ".ndjson", ".geojsonl")


class ImportSourceError(Exception):
    """Fetch/parse/source failure that fails the JOB. Message is client-facing:
    sanitized source labels only — never a query string or an upstream body."""


class JobRevokedError(Exception):
    """The job row was flipped externally (reaper/cancel) — abort, don't finalize."""


async def run_job(
    job_id: str,
    *,
    transport: Any = None,
    storage_client_factory: Callable[[], Any] | None = None,
) -> int:
    """Execute one import job to a terminal row state. Returns the exit code."""
    settings = get_settings()
    jid = uuid.UUID(job_id)
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as session:
        job = await job_repo.claim(session, jid)
        await session.commit()
    if job is None:
        # --max-retries 0 means Cloud Run never auto-reruns; this is the manual
        # re-execute / double-dispatch safety: claimed or finished → clean no-op.
        logger.info("import job %s already claimed or finished; nothing to do", job_id)
        return 0

    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError, RuntimeError):
        # Cloud Run sends SIGTERM 10s before SIGKILL at the task timeout.
        loop.add_signal_handler(signal.SIGTERM, task.cancel)  # type: ignore[union-attr]
    try:
        results = await _execute(
            job,
            settings=settings,
            transport=transport,
            storage_client_factory=storage_client_factory,
        )
    except JobRevokedError:
        logger.warning("import job %s was flipped externally; aborting", job_id)
        return 1
    except asyncio.CancelledError:
        if task is not None:
            task.uncancel()
        await _fail(jid, "terminated before completion (task timeout or shutdown)")
        return 1
    except (OperationalError, InterfaceError):
        logger.error("import job %s: database connection lost", job_id, exc_info=True)
        await _fail(jid, "database connection lost; resubmit the import")
        return 1
    except (AnonymousWriteForbiddenError, WriteNotAuthorizedError):
        # Grant revoked between submit and run — honored because _authorize_write
        # re-runs inside create_places_bulk on every chunk.
        logger.warning("import job %s: write authorization revoked", job_id)
        await _fail(jid, "write authorization was revoked before the import completed")
        return 1
    except ImportSourceError as exc:
        logger.error("import job %s failed: %s", job_id, exc, exc_info=True)
        await _fail(jid, str(exc))
        return 1
    except Exception as exc:
        logger.error("import job %s failed", job_id, exc_info=True)
        await _fail(jid, f"{type(exc).__name__}: import failed")
        return 1
    finally:
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.remove_signal_handler(signal.SIGTERM)

    async with sessionmaker() as session:
        finished = await job_repo.finish(
            session,
            jid,
            status="successful",
            report=results.model_dump(mode="json"),
            progress=100 if job.source_is_prefix else None,
        )
        await session.commit()
    if not finished:
        logger.warning("import job %s was flipped externally before finalize", job_id)
        return 1
    logger.info(
        "import job %s successful: %d files, %d accepted, %d rejected",
        job_id,
        results.summary.files,
        results.summary.accepted,
        results.summary.rejected,
    )
    return 0


async def _fail(job_id: uuid.UUID, message: str) -> None:
    """Best-effort guarded failure record on a FRESH session (the working one may
    be poisoned)."""
    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            await job_repo.finish(session, job_id, status="failed", message=message)
            await session.commit()
    except Exception:
        logger.error("could not record failure for import job %s", job_id, exc_info=True)


async def _execute(
    job: ImportJob,
    *,
    settings: Settings,
    transport: Any,
    storage_client_factory: Callable[[], Any] | None,
) -> ImportResults:
    try:
        # Defense in depth: the submit-time check already ran, but the worker
        # trusts nothing it didn't verify itself.
        import_refs.validate_ref(job.source_ref, is_prefix=job.source_is_prefix, settings=settings)
    except ValueError as exc:
        raise ImportSourceError(f"source ref rejected: {exc}") from exc

    principal = Principal(
        subject=job.created_by,
        is_admin=job.created_by_admin,
        email=job.created_by_email,
        email_verified=job.created_by_email is not None,
    )

    storage = _StorageClientHandle(storage_client_factory)
    sources = await _resolve_sources(job, settings, storage)
    if not sources:
        raise ImportSourceError("no matching objects under the prefix")

    file_reports: list[ImportFileReport] = []
    features_seen = 0
    with tempfile.TemporaryDirectory(prefix="geoid-import-") as tmpdir:
        for file_index, source in enumerate(sources):
            label = import_refs.sanitized_source(source)
            progress = file_index * 100 // len(sources) if job.source_is_prefix else None
            path = Path(tmpdir) / f"source-{file_index}"
            await _spool(source, path, settings=settings, transport=transport, storage=storage)
            report, features_seen = await _mint_file(
                job,
                label,
                path,
                settings=settings,
                principal=principal,
                features_seen=features_seen,
                progress=progress,
            )
            file_reports.append(report)
            path.unlink(missing_ok=True)
    return build_results(file_reports)


class _StorageClientHandle:
    """Lazily build the GCS client once per job (only gs:// sources pay for it)."""

    def __init__(self, factory: Callable[[], Any] | None) -> None:
        self._factory = factory
        self._client: Any = None

    def get(self) -> Any:
        if self._client is None:
            if self._factory is not None:
                self._client = self._factory()
            else:
                from google.cloud import storage

                self._client = storage.Client()
        return self._client


async def _resolve_sources(
    job: ImportJob, settings: Settings, storage: _StorageClientHandle
) -> list[str]:
    if not job.source_is_prefix:
        return [job.source_ref]
    import anyio

    bucket, prefix = import_refs.parse_gs(job.source_ref)
    client = storage.get()

    def _list() -> list[str]:
        blobs = client.list_blobs(bucket, prefix=prefix)
        return sorted(b.name for b in blobs if b.name.lower().endswith(_GS_SUFFIXES))

    names = await anyio.to_thread.run_sync(_list)
    if len(names) > settings.job_max_files:
        raise ImportSourceError(
            f"prefix matches {len(names)} objects, exceeding the "
            f"{settings.job_max_files}-file cap (GEOID_JOB_MAX_FILES)"
        )
    return [f"gs://{bucket}/{name}" for name in names]


async def _spool(
    source: str,
    path: Path,
    *,
    settings: Settings,
    transport: Any,
    storage: _StorageClientHandle,
) -> None:
    label = import_refs.sanitized_source(source)
    if source.startswith("gs://"):
        import anyio

        await anyio.to_thread.run_sync(
            _spool_gs, storage.get(), source, path, settings.job_max_bytes, label
        )
        return
    await _spool_https(source, path, settings.job_max_bytes, transport, label)


async def _spool_https(url: str, path: Path, max_bytes: int, transport: Any, label: str) -> None:
    import httpx

    try:
        # No redirects (httpx default), no headers attached: the URL itself is
        # the credential (presigned), and a redirect could step off the
        # allowlisted host.
        async with (
            httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(60.0)) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                raise ImportSourceError(
                    f"fetching {label} failed: upstream answered {response.status_code}"
                )
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ImportSourceError(
                    f"{label} exceeds the {max_bytes}-byte cap (GEOID_JOB_MAX_BYTES)"
                )
            received = 0
            with path.open("wb") as sink:
                async for chunk in response.aiter_bytes(_READ_BLOCK):
                    received += len(chunk)
                    # Counted, not trusted: a chunked/lying upstream can't
                    # blow past the cap.
                    if received > max_bytes:
                        raise ImportSourceError(
                            f"{label} exceeds the {max_bytes}-byte cap (GEOID_JOB_MAX_BYTES)"
                        )
                    sink.write(chunk)
    except httpx.HTTPError as exc:
        # httpx exception text can embed the full URL (query = bearer secret) —
        # surface only the exception class.
        raise ImportSourceError(f"fetching {label} failed: {type(exc).__name__}") from exc


def _spool_gs(client: Any, source: str, path: Path, max_bytes: int, label: str) -> None:
    bucket, name = import_refs.parse_gs(source)
    blob = client.bucket(bucket).blob(name)
    received = 0
    with blob.open("rb") as src, path.open("wb") as sink:
        while True:
            chunk = src.read(_READ_BLOCK)
            if not chunk:
                break
            received += len(chunk)
            if received > max_bytes:
                raise ImportSourceError(
                    f"{label} exceeds the {max_bytes}-byte cap (GEOID_JOB_MAX_BYTES)"
                )
            sink.write(chunk)


def iter_features(path: Path) -> Iterator[dict[str, Any]]:
    """Stream features from a spooled source in constant memory.

    First-byte sniff: RS (0x1E) → RFC 8142 GeoJSONSeq; a first LINE that parses
    as a complete non-FeatureCollection JSON value → NDJSON of Features; anything
    else → a FeatureCollection, streamed via ijson (``json.loads`` of a 100 MB
    body on this container is an OOM coin flip).
    """
    with path.open("rb") as handle:
        head = handle.read(1)
        if not head:
            raise ValueError("source is empty")
        if head == b"\x1e":
            handle.seek(0)
            yield from _iter_lines(handle)
            return
        handle.seek(0)
        first_line = handle.readline()
        parsed: Any = None
        with contextlib.suppress(ValueError):
            parsed = json.loads(first_line)
        handle.seek(0)
        if isinstance(parsed, dict) and parsed.get("type") != "FeatureCollection":
            yield from _iter_lines(handle)
            return
        import ijson

        yield from ijson.items(handle, "features.item")


def _iter_lines(handle: Any) -> Iterator[dict[str, Any]]:
    for line in handle:
        stripped = line.strip().lstrip(b"\x1e").strip()
        if not stripped:
            continue
        try:
            yield json.loads(stripped)
        except ValueError as exc:
            raise ValueError("malformed NDJSON/GeoJSONSeq line") from exc


def _next_feature(iterator: Iterator[dict[str, Any]], label: str) -> Any:
    """Pull one feature, converting parse faults to a job-failing source error."""
    import ijson

    try:
        return next(iterator)
    except StopIteration:
        return _DONE
    except (ValueError, ijson.JSONError) as exc:
        raise ImportSourceError(f"parsing {label} failed: {exc}") from exc


_DONE = object()


async def _mint_file(
    job: ImportJob,
    label: str,
    path: Path,
    *,
    settings: Settings,
    principal: Principal,
    features_seen: int,
    progress: int | None,
) -> tuple[ImportFileReport, int]:
    sessionmaker = get_sessionmaker()
    accepted: list[BulkAccepted] = []
    rejected: list[BulkRejected] = []
    offset = 0

    async def _mint_chunk(chunk: list[dict[str, Any]]) -> BulkReport:
        async with sessionmaker() as session:
            collection = await session.get(Collection, job.collection_id)
            if collection is None:
                raise ImportSourceError("target collection no longer exists")
            report = await registry_service.create_places_bulk(
                session,
                settings=settings,
                principal=principal,
                collection=collection,
                features=chunk,
            )
            # Last statement of the chunk tx: atomic with the work it reports.
            if not await job_repo.heartbeat(session, job.id, progress=progress):
                raise JobRevokedError(str(job.id))
            await session.commit()
        return report

    def _collect(report: BulkReport) -> None:
        accepted.extend(a.model_copy(update={"index": a.index + offset}) for a in report.accepted)
        rejected.extend(r.model_copy(update={"index": r.index + offset}) for r in report.rejected)

    iterator = iter_features(path)
    chunk: list[dict[str, Any]] = []
    while True:
        raw = _next_feature(iterator, label)
        if raw is _DONE:
            break
        features_seen += 1
        if features_seen > settings.job_max_features:
            raise ImportSourceError(
                f"import exceeds the {settings.job_max_features}-feature cap "
                "(GEOID_JOB_MAX_FEATURES); split the submission"
            )
        chunk.append(raw)
        if len(chunk) >= CHUNK_SIZE:
            _collect(await _mint_chunk(chunk))
            offset += len(chunk)
            chunk = []
    if chunk:
        _collect(await _mint_chunk(chunk))
        offset += len(chunk)

    report = ImportFileReport(
        source=label,
        summary=ImportFileSummary(received=offset, accepted=len(accepted), rejected=len(rejected)),
        accepted=accepted,
        rejected=rejected,
    )
    return report, features_seen
