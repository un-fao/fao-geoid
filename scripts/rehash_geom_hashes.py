#!/usr/bin/env python3
"""Re-hash every ``place.geom_hash`` under the live engine (audited, one transaction).

Run this when the dedup hash has drifted out from under the stored rows — the
golden-vector check (``scripts/dedup_vectors.py --check``, also bootstrap's exit-3
path) failing after a PostGIS/GEOS upgrade is the trigger. Drift never corrupts
identity (geoids are UUIDv7; the hash is operational dedup only), but stale hashes
stop deduplicating new submissions against old rows until they are recomputed.

    GEOID_DATABASE_URL='postgresql+asyncpg://geoid:...@127.0.0.1:5432/geoid' \\
    uv run python scripts/rehash_geom_hashes.py [--dry-run] [--yes]

Steps:
    1. config       GEOID_DATABASE_URL only — the APP role: it owns ``place``, so
                    it may DISABLE TRIGGER (Cloud SQL's ``postgres`` may not)
    2. pre-flight   latest dedup_recipe_stamp row (absent → migrate to 0003 first),
                    live PostGIS/GEOS versions, golden-vector check (informational:
                    all vectors passing means this run will likely be a no-op)
    3. scan         read-only SELECT recomputing every place's hash under the ONE
                    global grid (migration 0001 pins the trigger literal — if it
                    is ever retuned, DEFAULT_GRID_FALLBACK below must move with it)
    4. plan         pure collision resolution (decision D5; scope is GLOBAL —
                    geometry uniqueness is catalog-wide): within a new_hash
                    group a row already
                    holding the hash unchanged wins (updating past it would
                    violate uq_place_geom_hash); otherwise the earliest id
                    (UUIDv7 = mint-time order) wins; losers keep their old hash
                    and are reported as discovered duplicates; planned updates
                    targeting a skipped row's retained hash are cascade-demoted
                    to a fixed point. Rows are never deleted. --dry-run stops here.
    5. apply        one transaction: DISABLE TRIGGER place_block_mutation_bud →
                    batched UPDATEs (1000/chunk) → ENABLE TRIGGER → append a
                    dedup_recipe_stamp row ('rehash-script'). The stamp is
                    appended even when nothing changed — a no-op run is the
                    record "verified clean under this stack".
    6. report       skipped pairs printed as discovered duplicates for follow-up

``place`` is INSERT-only by design; the trigger toggle takes an ACCESS EXCLUSIVE
lock on it, so the runbook (docs/DEPLOYMENT.md §14) mandates a write freeze and a
PITR point before a live run. A failure rolls the whole transaction back —
including the trigger state.

Env:
    GEOID_DATABASE_URL   required — the canonical app URL (app-role credentials)

Exit codes:
    0 ok · 1 step failed · 2 config/usage error · 3 completed with
    discovered-duplicate skips (review the reported pairs)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import psycopg
from sqlalchemy.engine.url import make_url

REPO_ROOT = Path(__file__).resolve().parents[1]

RECIPE_VERSION = "v1"
MUTATION_TRIGGER = "place_block_mutation_bud"
UPDATE_CHUNK_SIZE = 1000
# Mirrors the 0001 trigger literal (the ONE global grid). A future retune is a
# migration event and must move this value too.
DEFAULT_GRID_FALLBACK = "1e-7"


class ConfigError(Exception):
    """Bad or missing configuration — exit 2, nothing was touched."""


class StepError(Exception):
    """A step failed — exit 1; the message carries remediation hints."""


@dataclass(frozen=True)
class RehashConfig:
    app_url: str
    dry_run: bool
    assume_yes: bool

    @property
    def dsn(self) -> str:
        """SQLAlchemy ``postgresql+asyncpg://`` URL → plain libpq/psycopg URL."""
        return (
            make_url(self.app_url)
            .set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )


@dataclass(frozen=True)
class ScanRow:
    id: uuid.UUID  # orderable; UUIDv7 sorts by mint time
    collection_id: uuid.UUID  # report context only — collisions are global
    old_hash: str  # hex
    new_hash: str  # hex


@dataclass(frozen=True)
class SkippedPair:
    collection_id: uuid.UUID  # the LOSER's collection (the winner may be elsewhere)
    loser_id: uuid.UUID
    winner_id: uuid.UUID
    geom_hash: str  # the hex digest both rows would share / collide on


@dataclass(frozen=True)
class RehashPlan:
    updates: tuple[ScanRow, ...]
    skipped_pairs: tuple[SkippedPair, ...]
    unchanged_count: int


def _redact(url: str) -> str:
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return re.sub(r"(password\s*=\s*)\S+", r"\1***", url)


def _load_config(argv: list[str] | None = None) -> RehashConfig:
    parser = argparse.ArgumentParser(
        description="Recompute place.geom_hash under the live engine (one transaction)."
    )
    parser.add_argument("--dry-run", action="store_true", help="scan + plan only; mutate nothing")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args(argv)

    raw_url = os.environ.get("GEOID_DATABASE_URL")
    if not raw_url:
        raise ConfigError(
            "GEOID_DATABASE_URL is required — the canonical app URL; the app role "
            "owns place and so may toggle its triggers"
        )
    try:
        url = make_url(raw_url)
    except Exception as exc:
        raise ConfigError(f"GEOID_DATABASE_URL is not a valid SQLAlchemy URL: {exc}") from exc
    if url.get_backend_name() != "postgresql":
        raise ConfigError(f"GEOID_DATABASE_URL must be a postgresql URL, got {url.drivername!r}")
    return RehashConfig(app_url=raw_url, dry_run=args.dry_run, assume_yes=args.yes)


def _load_dedup_vectors():
    """Load scripts/dedup_vectors.py as a module (scripts/ is not a package)."""
    import importlib.util

    path = Path(__file__).resolve().parent / "dedup_vectors.py"
    spec = importlib.util.spec_from_file_location("dedup_vectors", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["dedup_vectors"] = module
    spec.loader.exec_module(module)
    return module


def preflight(conn: psycopg.Connection) -> None:
    print("→ pre-flight")
    if conn.execute("SELECT to_regclass('dedup_recipe_stamp')").fetchone()[0] is None:
        raise StepError("dedup_recipe_stamp table missing — run `geoid migrate` to 0003+ first")
    stamp = conn.execute(
        "SELECT recipe_version, postgis_version, geos_version, stamped_at "
        "FROM dedup_recipe_stamp ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if stamp is None:
        raise StepError("dedup_recipe_stamp is empty — run `geoid migrate` to 0003+ first")
    if not conn.execute(
        "SELECT count(*) FROM pg_proc WHERE proname = 'geoid_geom_hash'"
    ).fetchone()[0]:
        raise StepError("geoid_geom_hash() is missing — run `geoid migrate` first")
    lib = conn.execute("SELECT postgis_lib_version()").fetchone()[0]
    geos = conn.execute("SELECT postgis_geos_version()").fetchone()[0]
    print(
        f"  stamped : recipe {stamp[0]} on PostGIS {stamp[1]} / GEOS {stamp[2]} "
        f"({stamp[3]:%Y-%m-%d})"
    )
    print(f"  live    : PostGIS {lib} / GEOS {geos}")

    vectors = _load_dedup_vectors()
    try:
        report = vectors.check_vectors(vectors._psycopg_run_sql(conn), vectors.load_fixture())
    except vectors.StepError as exc:
        print(f"  ⚠ golden-vector check unavailable: {exc}")
        return
    if report.strict_failures:
        print(
            f"  ⚠ {len(report.strict_failures)} strict golden vector(s) drifted — "
            "stored hashes are likely stale (this run is what fixes that)"
        )
    else:
        print("  ✓ all golden vectors match — this run will likely be a no-op")


_SCAN_SQL = f"""
    SELECT p.id, p.collection_id,
           encode(p.geom_hash, 'hex') AS old_hash,
           encode(geoid_geom_hash(p.geom, {DEFAULT_GRID_FALLBACK}), 'hex') AS new_hash
      FROM place p
     ORDER BY p.id
"""


def scan(conn: psycopg.Connection) -> tuple[ScanRow, ...]:
    print("→ scan (read-only recompute under the live engine)")
    rows = tuple(
        ScanRow(id=row[0], collection_id=row[1], old_hash=row[2], new_hash=row[3])
        for row in conn.execute(_SCAN_SQL)
    )
    print(f"  {len(rows)} places scanned")
    return rows


def plan_rehash(rows: Sequence[ScanRow]) -> RehashPlan:
    """Pure collision resolution (decision D5; geometry uniqueness is global,
    so the scope is catalog-wide). Never deletes; losers keep their old hash
    and surface as discovered-duplicate pairs."""

    def holds(row: ScanRow) -> bool:
        return row.old_hash == row.new_hash

    # Group winner per new_hash — catalog-wide: an incumbent already holding the
    # hash unchanged beats everyone (the UPDATE itself would violate
    # uq_place_geom_hash); otherwise the earliest id (= mint time).
    winners: dict[str, ScanRow] = {}
    for row in rows:
        incumbent = winners.get(row.new_hash)
        if (
            incumbent is None
            or (holds(row) and not holds(incumbent))
            or (holds(row) == holds(incumbent) and row.id < incumbent.id)
        ):
            winners[row.new_hash] = row

    drifted = [row for row in rows if not holds(row)]
    unchanged_count = len(rows) - len(drifted)
    updates = [row for row in drifted if winners[row.new_hash] is row]
    skipped: list[tuple[ScanRow, uuid.UUID]] = [
        (row, winners[row.new_hash].id) for row in drifted if winners[row.new_hash] is not row
    ]

    # Fixed point: a skipped row RETAINS its old hash, so any planned update
    # targeting that value would collide — demote it too, and so on.
    while True:
        retained = {row.old_hash: row for row, _ in skipped}
        demoted = [row for row in updates if row.new_hash in retained]
        if not demoted:
            break
        updates = [row for row in updates if row not in demoted]
        skipped += [(row, retained[row.new_hash].id) for row in demoted]

    pairs = tuple(
        SkippedPair(
            collection_id=row.collection_id,
            loser_id=row.id,
            winner_id=winner_id,
            geom_hash=row.new_hash,
        )
        for row, winner_id in skipped
    )
    return RehashPlan(tuple(updates), pairs, unchanged_count)


def apply_updates(conn: psycopg.Connection, plan: RehashPlan) -> None:
    print(f"→ apply ({len(plan.updates)} update(s), one transaction)")
    note = (
        f"updated {len(plan.updates)}, skipped {len(plan.skipped_pairs)}, "
        f"unchanged {plan.unchanged_count}"
    )
    with conn.transaction():
        # ACCESS EXCLUSIVE on place — the runbook mandates a write freeze.
        conn.execute(f"ALTER TABLE place DISABLE TRIGGER {MUTATION_TRIGGER}")
        for start in range(0, len(plan.updates), UPDATE_CHUNK_SIZE):
            chunk = plan.updates[start : start + UPDATE_CHUNK_SIZE]
            values = ", ".join(["(%s::uuid, %s)"] * len(chunk))
            params = [param for row in chunk for param in (str(row.id), row.new_hash)]
            conn.execute(
                f"UPDATE place SET geom_hash = decode(v.new_hash, 'hex') "
                f"FROM (VALUES {values}) AS v(id, new_hash) WHERE place.id = v.id",
                params,
            )
        conn.execute(f"ALTER TABLE place ENABLE TRIGGER {MUTATION_TRIGGER}")
        # Appended even when 0 updated: a no-op run is the record "verified
        # clean under this stack".
        conn.execute(
            "INSERT INTO dedup_recipe_stamp "
            "  (recipe_version, postgis_version, geos_version, postgis_full, "
            "   stamped_by, note) "
            "SELECT %s, postgis_lib_version(), postgis_geos_version(), "
            "       postgis_full_version(), 'rehash-script', %s",
            (RECIPE_VERSION, note),
        )
    print(f"  ✓ committed — {note}; trigger re-enabled; stamp appended")


def report(plan: RehashPlan) -> int:
    if not plan.skipped_pairs:
        return 0
    print(
        f"\n⚠ {len(plan.skipped_pairs)} discovered-duplicate pair(s) — losers keep "
        "their old hash; review and (if confirmed duplicates) supersede via the "
        "normal predecessor flow:"
    )
    for pair in plan.skipped_pairs:
        print(
            f"  - place {pair.loser_id} (collection {pair.collection_id}) now hashes "
            f"identically to place {pair.winner_id} (digest {pair.geom_hash}) — skipped"
        )
    return 3


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = _load_config(argv)
    except ConfigError as exc:
        print(f"✗ {exc}")
        return 2

    print(f"GeoID geom_hash re-hash ({'DRY-RUN' if cfg.dry_run else 'live'})")
    print(f"  app URL : {_redact(cfg.app_url)}")
    if not cfg.dry_run and not cfg.assume_yes:
        answer = input(
            "place gets an ACCESS EXCLUSIVE lock — writes must be frozen "
            "(docs/DEPLOYMENT.md §14). proceed? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 2

    try:
        with psycopg.connect(cfg.dsn, autocommit=True) as conn:
            preflight(conn)
            rows = scan(conn)
            plan = plan_rehash(rows)
            print(
                f"→ plan: {len(plan.updates)} to update, "
                f"{len(plan.skipped_pairs)} to skip, {plan.unchanged_count} unchanged"
            )
            if cfg.dry_run:
                exit_code = report(plan)
                print("\n✓ dry-run complete (nothing mutated)")
                return exit_code
            apply_updates(conn, plan)
            exit_code = report(plan)
            print("\n✓ re-hash complete")
            return exit_code
    except StepError as exc:
        print(f"✗ {exc}")
        return 1
    except psycopg.Error as exc:
        print(f"✗ database error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
