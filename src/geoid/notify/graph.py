"""``GraphNotifier`` — completion email via Microsoft Graph ``sendMail``.

App-only (client-credentials) OAuth over HTTPS/443 — no SMTP ports, future-proof
against M365 Basic-auth SMTP (disabled end-2026). Requires an Entra app
registration with the ``Mail.Send`` application permission (admin consent) and a
sender mailbox. The retry pattern (3 tries, exponential backoff + jitter) is ported
from FAO's ``dwh-email-notifier`` ``email_service._send_with_retry``; the FAO Groups
recipient resolution is intentionally dropped (GeoID has no workspace concept — the
recipient is the job's ``notify_email``).

A transport failure is logged, never raised: the send is at-most-once (the
``notified_at`` claim is already taken by the caller), so a failed email must not
poison the worker transaction.
"""

from __future__ import annotations

import asyncio
import html
import logging
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx

from geoid.config import Settings
from geoid.models import IngestJob
from geoid.schemas.ingest import IngestionReport

logger = logging.getLogger("geoid.notify.graph")

_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_SENDMAIL_URL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
_SCOPE = "https://graph.microsoft.com/.default"

_MAX_TRIES = 3
_BASE_DELAY = 0.5  # seconds; doubled per attempt + jitter
_TIMEOUT = 15.0
_TOKEN_SKEW = 60  # refresh this many seconds before expiry


def _compose(job: IngestJob, report: IngestionReport | None) -> tuple[str, str]:
    if report is not None:
        subject = f"GeoID bulk ingest {job.status}: {report.accepted_count}/{report.total} accepted"
        body = (
            f"<p>Bulk ingest job <code>{job.id}</code> finished: "
            f"<strong>{job.status}</strong>.</p>"
            f"<ul><li>Collection: {html.escape(report.collection)}</li>"
            f"<li>Accepted: {report.accepted_count}</li>"
            f"<li>Rejected: {report.rejected_count}</li>"
            f"<li>Total: {report.total}</li>"
            f"<li>Place set: {html.escape(report.place_set_uri or '')}</li></ul>"
        )
    else:
        subject = f"GeoID bulk ingest {job.status}"
        body = (
            f"<p>Bulk ingest job <code>{job.id}</code> finished: "
            f"<strong>{job.status}</strong>.</p>"
            f"<p>{html.escape(job.message or '')}</p>"
        )
    return subject, body


class GraphNotifier:
    """Sends a completion email via Graph ``sendMail`` (app-only OAuth)."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client  # injectable for tests; otherwise one is created per send
        self._token: str | None = None
        self._token_expiry = 0.0

    async def send_completion(self, job: IngestJob, report: IngestionReport | None) -> None:
        recipient = job.notify_email
        if not recipient:
            return  # no email requested — callback/polling still apply
        subject, body = _compose(job, report)
        if self._settings.graph_suppress_send:
            logger.info("SUPPRESS_SEND: would email %s about job %s", recipient, job.id)
            return
        message = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
        }
        await self._send_with_retry(message)

    async def _send_with_retry(self, message: dict) -> None:
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_TRIES + 1):
            try:
                await self._send_once(message)
                return
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == _MAX_TRIES:
                    break
                delay = _BASE_DELAY * 2 ** (attempt - 1) + random.uniform(0, _BASE_DELAY)
                await asyncio.sleep(delay)
        logger.warning("graph sendMail failed after %d attempts: %s", _MAX_TRIES, last_exc)

    @asynccontextmanager
    async def _http(self) -> AsyncIterator[httpx.AsyncClient]:
        """Yield the injected client (tests) or a fresh one closed on exit (prod)."""
        if self._client is not None:
            yield self._client
        else:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                yield client

    async def _send_once(self, message: dict) -> None:
        token = await self._access_token()
        url = _SENDMAIL_URL.format(sender=quote(self._settings.graph_sender or ""))
        payload = {"message": message, "saveToSentItems": False}
        async with self._http() as client:
            resp = await client.post(
                url, headers={"Authorization": f"Bearer {token}"}, json=payload
            )
            resp.raise_for_status()

    async def _access_token(self) -> str:
        now = time.monotonic()
        if self._token and now < self._token_expiry:
            return self._token
        url = _TOKEN_URL.format(tenant=self._settings.graph_tenant_id or "")
        data = {
            "grant_type": "client_credentials",
            "client_id": self._settings.graph_client_id or "",
            "client_secret": self._settings.graph_client_secret or "",
            "scope": _SCOPE,
        }
        async with self._http() as client:
            resp = await client.post(url, data=data)
            resp.raise_for_status()
            token_data = resp.json()
        self._token = token_data["access_token"]
        self._token_expiry = now + int(token_data.get("expires_in", 3600)) - _TOKEN_SKEW
        return self._token
