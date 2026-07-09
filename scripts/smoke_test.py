#!/usr/bin/env python3
"""Post-deploy smoke test — verify a deployed GeoID instance end-to-end.

Read-only by default, so it is safe against production (places are append-only;
nothing is minted unless asked). ``--mint`` adds a write probe that mints ONE
fixed sentinel feature; global exact-match dedup makes it idempotent — first
run 201, every later run 409 with ``constraint == "uq_geoid_registry_geom_hash"``.
The 409's incumbent fields are disclosed when the incumbent's collection is
``public_read`` (the default ``public`` collection is — so the full three-probe
run works anonymously) or the caller is a member (sysadmin / creator / any
grant); a PRIVATE incumbent yields a MASKED 409 (null geoid) for non-members —
reported as such, with the resolve probes skipped.
At most one permanent row per catalog, ever.

    uv run python scripts/smoke_test.py            # read-only checks
    uv run python scripts/smoke_test.py --mint     # + idempotent write probe

Env:
    GEOID_BASE_URL     default http://localhost:8000 — the public URL under test;
                       every returned link/uri must carry its host (catches the
                       BASE_URL misconfiguration, the #1 launch risk)
    GEOID_COLLECTION   default public
    GEOID_BEARER_TOKEN a valid Keycloak access token (JWT). One carrying the
                       sysadmin role enables the admin-gated collection read checks
                       (GET /collections, GET /collections/{id}) and minting into a
                       managed (non-anon) collection. Without it those read checks
                       are skipped.

Exit codes: 0 all checks passed · 1 one or more failed (CI / Cloud Run job friendly)
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx
from _timing import print_timings, record

BASE = os.environ.get("GEOID_BASE_URL", "http://localhost:8000").rstrip("/")
COLLECTION = os.environ.get("GEOID_COLLECTION", "public")
BEARER_TOKEN = os.environ.get("GEOID_BEARER_TOKEN")

SENTINEL_EXTERNAL_ID = "geoid-smoke-sentinel"
# Fixed tiny (~11 m) square in the Gulf of Guinea. Constant on purpose: the
# exact-match dedup recipe maps every re-run onto the same incumbent geoid.
SENTINEL_FEATURE = {
    "type": "Feature",
    "id": SENTINEL_EXTERNAL_ID,
    "geometry": {
        "type": "Polygon",
        "coordinates": [[[0.0, 0.0], [0.0001, 0.0], [0.0001, 0.0001], [0.0, 0.0001], [0.0, 0.0]]],
    },
    "properties": {"name": "GeoID smoke-test sentinel", "purpose": "post-deploy verification"},
}


class CheckFailed(Exception):
    """A smoke check failed. Raised instead of ``assert`` so the deploy gate

    survives ``python -O`` / ``PYTHONOPTIMIZE`` (which strip asserts into
    silent success).
    """


def _ensure(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {BEARER_TOKEN}"} if BEARER_TOKEN else {}


def _expected_netloc() -> str:
    return urlsplit(BASE).netloc


def _get_json(
    client: httpx.Client,
    path: str,
    *,
    expect_type: str | None = None,
    headers: dict[str, str] | None = None,
) -> dict:
    resp = record(f"GET {path}", client.get(f"{BASE}{path}", headers=headers or {}))
    _ensure(resp.status_code == 200, f"GET {path} → {resp.status_code}: {resp.text[:200]}")
    if expect_type:
        ctype = resp.headers.get("content-type", "")
        _ensure(
            ctype.startswith(expect_type),
            f"GET {path} content-type {ctype!r}, expected {expect_type!r}",
        )
    return resp.json()


# --- Read checks (safe against production) -----------------------------------


def check_landing(client: httpx.Client) -> str:
    body = _get_json(client, "/")
    links = body.get("links") or []
    _ensure(bool(links), "landing page advertises no links")
    expected = _expected_netloc()
    wrong = [link["href"] for link in links if urlsplit(link["href"]).netloc != expected]
    _ensure(
        not wrong,
        f"links not on {expected!r}: {wrong} — is GEOID_BASE_URL misconfigured on the server?",
    )
    return f"{len(links)} links, all on {expected!r}"


def check_conformance(client: httpx.Client) -> str:
    classes = _get_json(client, "/conformance").get("conformsTo") or []
    _ensure(bool(classes), "empty conformsTo")
    return f"{len(classes)} conformance classes"


def check_collections(client: httpx.Client) -> str:
    # /collections is admin-gated — send the bearer token.
    ids = [
        c.get("id")
        for c in _get_json(client, "/collections", headers=_headers()).get("collections", [])
    ]
    _ensure(COLLECTION in ids, f"collection {COLLECTION!r} not in {ids}")
    return f"{len(ids)} collections, {COLLECTION!r} present"


def check_collection_desc(client: httpx.Client) -> str:
    body = _get_json(client, f"/collections/{COLLECTION}", headers=_headers())
    _ensure(body.get("id") == COLLECTION, f"unexpected collection id {body.get('id')!r}")
    return f"id={body['id']!r}"


# Public read checks (no token, safe against production).
READ_CHECKS: tuple[tuple[str, Callable[[httpx.Client], str]], ...] = (
    ("landing links", check_landing),
    ("conformance", check_conformance),
)

# Admin-gated read checks — only run when GEOID_BEARER_TOKEN is set.
ADMIN_READ_CHECKS: tuple[tuple[str, Callable[[httpx.Client], str]], ...] = (
    ("collections list", check_collections),
    ("collection describe", check_collection_desc),
)


# --- Write probe (--mint, idempotent) ----------------------------------------


def run_mint_probe(client: httpx.Client) -> list[bool]:
    minted: dict = {}

    def probe_mint() -> str:
        resp = record(
            "POST items",
            client.post(
                f"{BASE}/collections/{COLLECTION}/items", json=SENTINEL_FEATURE, headers=_headers()
            ),
        )
        if resp.status_code == 409:
            body = resp.json()
            if body.get("constraint") == "uq_geoid_registry_geom_hash":
                # Expected on every run after the first: global dedup rejects the
                # duplicate. A public_read incumbent (the default `public`
                # collection) discloses to every caller; only a PRIVATE incumbent
                # is masked for non-members — still a healthy dedup.
                minted.update(body)
                if body.get("geoid") is None:
                    return "[409] duplicate geometry → incumbent masked (caller is not a member)"
                return f"[409] duplicate geometry → incumbent geoid={body['geoid']}"
            raise CheckFailed(
                "409 external_id conflict — the sentinel external_id exists with a "
                "DIFFERENT geometry; was SENTINEL_FEATURE changed since the first run?"
            )
        _ensure(resp.status_code == 201, f"{resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        minted.update(body)
        return f"[201] minted (first run against this catalog), geoid={body['geoid']}"

    def probe_resolve_geoid() -> str:
        # Resolver lives at the app root: BASE already carries the /geoid root_path.
        props = _get_json(client, f"/{minted['geoid']}").get("properties") or {}
        uri = props.get("uri")
        uri_netloc = urlsplit(uri or "").netloc
        _ensure(
            uri_netloc == _expected_netloc(),
            f"uri host {uri_netloc!r} != expected {_expected_netloc()!r} (uri={uri!r})",
        )
        return f"resolved; uri on {uri_netloc!r}"

    def probe_resolve_external() -> str:
        # The incumbent's collection (from the 201/409 body) — with global dedup
        # it may differ from the collection this run targeted. A masked 409 body
        # carries the key with value None (present, so .get's default won't fire),
        # hence `or`.
        collection = minted.get("collection") or COLLECTION
        props = (
            _get_json(client, f"/collections/{collection}/external/{SENTINEL_EXTERNAL_ID}").get(
                "properties"
            )
            or {}
        )
        _ensure(
            props.get("geoid") == minted["geoid"],
            f"external-id resolve returned {props.get('geoid')!r}, expected {minted['geoid']!r}",
        )
        return f"external_id {SENTINEL_EXTERNAL_ID!r} → same geoid"

    results = [_run_check("mint sentinel", probe_mint)]
    if not results[0]:
        print("  – resolve checks skipped (mint failed)")
        return results
    if minted.get("geoid") is None:
        # Masked 409 (private incumbent, caller not a member): dedup verified,
        # but there is no geoid to resolve. Run with a member/sysadmin
        # GEOID_BEARER_TOKEN for the full probe.
        print("  – resolve checks skipped (incumbent masked for this caller)")
        return results
    return results + [
        _run_check("resolve by geoid", probe_resolve_geoid),
        _run_check("resolve by external_id", probe_resolve_external),
    ]


# --- Runner -------------------------------------------------------------------


def _run_check(name: str, thunk: Callable[[], str]) -> bool:
    try:
        detail = thunk()
    except CheckFailed as exc:
        print(f"✗ {name:<24} {exc}")
        return False
    except httpx.HTTPError as exc:
        print(f"✗ {name:<24} {type(exc).__name__}: {exc}")
        return False
    except ValueError as exc:
        # A non-JSON body where JSON was expected (JSONDecodeError is a ValueError).
        print(f"✗ {name:<24} {type(exc).__name__}: {exc}")
        return False
    print(f"✓ {name:<24} {detail}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="GeoID post-deploy smoke test")
    parser.add_argument(
        "--mint",
        action="store_true",
        help="also mint the fixed sentinel feature (idempotent write probe)",
    )
    args = parser.parse_args()

    mode = "+ mint probe" if args.mint else "read-only"
    print(f"→ GeoID smoke test against {BASE}, collection {COLLECTION!r} ({mode})\n")
    with httpx.Client(timeout=30.0) as client:
        try:
            record("GET /conformance (reachability)", client.get(f"{BASE}/conformance"))
        except httpx.HTTPError as exc:
            print(f"✗ cannot reach GeoID at {BASE} ({exc})")
            return 1
        results = [_run_check(name, lambda fn=fn: fn(client)) for name, fn in READ_CHECKS]
        if BEARER_TOKEN:
            results += [
                _run_check(name, lambda fn=fn: fn(client)) for name, fn in ADMIN_READ_CHECKS
            ]
        else:
            print(
                "  – collection read checks skipped (no GEOID_BEARER_TOKEN; they are admin-gated)"
            )
        if args.mint:
            results += run_mint_probe(client)

    passed, total = sum(results), len(results)
    print(f"\n{'✓' if passed == total else '✗'} {passed}/{total} checks passed")
    print_timings()
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
