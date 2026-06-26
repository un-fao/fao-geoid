#!/usr/bin/env python3
"""Mint a Keycloak access token for GeoID — paste into Swagger or use in CI.

Two flows, both stdlib + httpx (no new dependency):

  uv run python scripts/get_token.py            # Authorization Code + PKCE (loopback)
  uv run python scripts/get_token.py --device   # Device Authorization Grant (headless)

The token is printed to **stdout**; every human-facing message goes to **stderr**,
so ``TOKEN=$(uv run python scripts/get_token.py)`` captures just the token.

Env:
    GEOID_OIDC_ISSUER         required — the realm URL (auth/token/device endpoints derive from it)
    GEOID_OIDC_CLIENT_ID      default "geoid-fe" — the public SPA client (PKCE, no secret)
    GEOID_OIDC_SCOPE          default "openid" — the realm already puts geoid-be in `aud` for FE-issued
                              tokens (verified from a real token), so no special scope is needed
    GEOID_OIDC_REDIRECT_HOST  default "127.0.0.1" — Keycloak REJECTS "localhost" for loopback redirects
                              (RFC 8252); it must be 127.0.0.1 or [::1], and any port is then allowed
    GEOID_OIDC_REDIRECT_PORT  default 8765 — loopback port (the registered redirect URI's port is ignored)
    GEOID_OIDC_REDIRECT_PATH  default "/callback" — must match the path CSI registered on geoid-fe exactly

PREREQUISITE (CSI/realm-admin): geoid-fe must have a loopback Valid Redirect URI registered, e.g.
`http://127.0.0.1:8765/callback` — verified 2026-06-22 that none is registered yet, so this flow 400s
("Invalid parameter: redirect_uri") until they add it. The device-code grant is also disabled on
geoid-fe, so `--device` needs CSI to enable it. Until then, paste a CSI/FE-obtained token directly.

Exit codes: 0 token printed · 1 error (message on stderr)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

ISSUER = os.environ.get("GEOID_OIDC_ISSUER", "").rstrip("/")
CLIENT_ID = os.environ.get("GEOID_OIDC_CLIENT_ID", "geoid-fe")
SCOPE = os.environ.get("GEOID_OIDC_SCOPE", "openid")
# Keycloak rejects "localhost" for loopback redirects (RFC 8252) — must be 127.0.0.1 / [::1].
REDIRECT_HOST = os.environ.get("GEOID_OIDC_REDIRECT_HOST", "127.0.0.1")
REDIRECT_PORT = int(os.environ.get("GEOID_OIDC_REDIRECT_PORT", "8765"))
REDIRECT_PATH = os.environ.get("GEOID_OIDC_REDIRECT_PATH", "/callback")


def _log(*args: object) -> None:
    print(*args, file=sys.stderr)


def _auth_url() -> str:
    return f"{ISSUER}/protocol/openid-connect/auth"


def _token_url() -> str:
    return f"{ISSUER}/protocol/openid-connect/token"


def _device_url() -> str:
    return f"{ISSUER}/protocol/openid-connect/auth/device"


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for the S256 PKCE method."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    """One-shot handler that captures ``?code=`` from the OIDC redirect."""

    captured: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        parts = urlsplit(self.path)
        if parts.path != REDIRECT_PATH:
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        _CallbackHandler.captured = params
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h3>GeoID: token received.</h3>"
            b"You may close this tab and return to the terminal.</body></html>"
        )

    def log_message(self, *args: object) -> None:  # silence the default access log
        pass


def auth_code_pkce() -> str:
    """Run the Authorization Code + PKCE loopback flow and return the access token."""
    redirect_uri = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}{REDIRECT_PATH}"
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    query = urlencode(
        {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "scope": SCOPE,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    authorize = f"{_auth_url()}?{query}"

    # Bind the loopback listener to the same host the redirect targets (default 127.0.0.1).
    server = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _CallbackHandler)
    _log(f"→ opening browser for login (redirect: {redirect_uri})")
    _log(f"  if it does not open, visit:\n  {authorize}\n")
    webbrowser.open(authorize)
    server.handle_request()  # blocks until the single callback arrives
    server.server_close()

    params = _CallbackHandler.captured
    if "error" in params:
        _log(f"✗ authorization failed: {params.get('error')} {params.get('error_description', '')}")
        sys.exit(1)
    if params.get("state") != state:
        _log("✗ state mismatch — possible CSRF; aborting")
        sys.exit(1)
    code = params.get("code")
    if not code:
        _log("✗ no authorization code in the redirect")
        sys.exit(1)

    resp = httpx.post(
        _token_url(),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        _log(f"✗ token exchange failed [{resp.status_code}]: {resp.text[:300]}")
        sys.exit(1)
    return resp.json()["access_token"]


def device_code() -> str:
    """Run the Device Authorization Grant and return the access token."""
    start = httpx.post(_device_url(), data={"client_id": CLIENT_ID, "scope": SCOPE}, timeout=30.0)
    if start.status_code != 200:
        _log(f"✗ device authorization failed [{start.status_code}]: {start.text[:300]}")
        sys.exit(1)
    grant = start.json()
    complete = grant.get("verification_uri_complete")
    _log("→ to authorize, open:")
    _log(f"  {complete or grant['verification_uri']}")
    if not complete:
        _log(f"  and enter code: {grant['user_code']}")
    _log("  waiting for authorization...")

    interval = int(grant.get("interval", 5))
    deadline = time.monotonic() + int(grant.get("expires_in", 600))
    while time.monotonic() < deadline:
        time.sleep(interval)
        poll = httpx.post(
            _token_url(),
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": grant["device_code"],
                "client_id": CLIENT_ID,
            },
            timeout=30.0,
        )
        if poll.status_code == 200:
            return poll.json()["access_token"]
        error = poll.json().get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        _log(f"✗ device grant failed: {error} {poll.json().get('error_description', '')}")
        sys.exit(1)
    _log("✗ timed out waiting for device authorization")
    sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint a Keycloak token for GeoID")
    parser.add_argument(
        "--device",
        action="store_true",
        help="use the Device Authorization Grant (headless) instead of loopback PKCE",
    )
    args = parser.parse_args()

    if not ISSUER:
        _log("✗ GEOID_OIDC_ISSUER is required (the realm URL)")
        return 1
    _log(
        f"→ GeoID token via {'device-code' if args.device else 'auth-code+PKCE'} "
        f"(issuer {ISSUER}, client {CLIENT_ID!r}, scope {SCOPE!r})"
    )
    token = device_code() if args.device else auth_code_pkce()
    print(token)  # the ONLY stdout line — captured by TOKEN=$(...)
    return 0


if __name__ == "__main__":
    sys.exit(main())
