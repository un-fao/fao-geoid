"""The ``Notifier`` Protocol — completion notification behind a swappable seam.

Email is a SECONDARY convenience (pull-polling ``GET /jobs/{id}`` is the contract),
so the seam is ``none``-capable: the rest of the feature ships without the org
dependency (Entra app registration + ``Mail.Send`` consent + a sender mailbox). The
concrete ``GraphNotifier`` is imported lazily by :func:`geoid.notify.get_notifier`,
so ``notify_backend=none`` never imports ``httpx`` or touches Microsoft Graph.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from geoid.models import IngestJob
from geoid.schemas.ingest import IngestionReport


@runtime_checkable
class Notifier(Protocol):
    """Send a completion notice for a finished ingest job. Must not raise on a
    transport failure that the caller cannot act on — log and return."""

    async def send_completion(self, job: IngestJob, report: IngestionReport | None) -> None: ...


class NullNotifier:
    """The ``notify_backend=none`` default — a no-op (polling remains the contract)."""

    async def send_completion(self, job: IngestJob, report: IngestionReport | None) -> None:
        return None
