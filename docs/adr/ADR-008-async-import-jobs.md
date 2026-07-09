# ADR-008: Async bulk import from storage blobs — OGC-Processes-subset jobs

- **Status:** Accepted (2026-07-07)
- **Relates to:** ADR-002/SCALING §8 (bulk-write ladder), the synchronous
  `POST /collections/{id}/items/bulk` route (kept byte-identical)

## Context

The only bulk write was the synchronous FeatureCollection POST, capped at 2500
features deployed and bounded by the 600 s Cloud Run request window. large-scale
loads (~40k+ features) need many round trips and babysitting. Clients hold the
data in object storage (GCS/S3) anyway, so the natural contract is: point GeoID
at a blob (or a whole `gs://` prefix), get a job URL, poll until the per-feature
outcome report is ready.

An earlier async ingest pipeline existed in this repo and was removed as
overkill. This reintroduction is deliberately minimal: **one new table, one new
Cloud Run Job (same image, same pattern as the `geoid-upgrade-head` migrate
job), verbatim reuse of `registry_service.create_places_bulk`** so sync and
async minting can never drift.

## Decision 1 — the API is an OGC API - Processes Part 1 (18-062r2 v1.0) subset

`POST /collections/{id}/items/import` answers **201 + `Location` + statusInfo**
(Req 34 — 201, not 202); `GET /jobs/{jobID}` serves the v1.0 `statusInfo`
document **verbatim** (`jobID`/`status`/`type:"process"`; the five-value enum
`accepted|running|successful|failed|dismissed`; the draft-2.0 renames are NOT
adopted); `GET /jobs/{jobID}/results` serves the report (running → 404
`…/result-not-ready`; failed → 500 + an RFC 7807-shaped exception carrying the
stored failure message, Req 46; unknown/not-yours → 404 `…/no-such-job`).

Deliberately skipped (re-add triggers in parentheses): `/processes` discovery,
`GET /jobs` listing, `DELETE` dismiss (client need — the heartbeat guard is
already the kill-switch seam), `Prefer:` negotiation,
`application/problem+json` media type, native `s3://` (S3 users presign),
checkpoint/resume (a resubmit converges via global dedup — every already-minted
feature reports `geometry_conflict`), results pagination (the feature cap
bounds the doc), retention sweep (when the table matters).

Per OGC semantics the job is `successful` even if every feature was rejected —
`failed` is reserved for fetch/parse/infra faults.

## Decision 2 — backend engine: Cloud Run Jobs

One execution per import, triggered from the API via
`run_v2.JobsClient.run_job` with a GA per-execution env override
(`GEOID_JOB_ID`). Verdict table (all doc-verified):

| Option | Verdict |
|---|---|
| **Cloud Run Jobs** | ✅ 7-day task timeout, `--max-retries 0`, zero idle cost (~$0.012 / 10-min 1 vCPU run; free tier ≈ 400/mo), same image + VPC/Cloud SQL wiring proven by the migrate job, job-scoped IAM, no actAs |
| Cloud Tasks → HTTP | ❌ 30-min dispatch deadline < worst case; couples ingest memory to the API service |
| Pub/Sub | ❌ 600 s max ack deadline → redelivery mid-run |
| CPU-always-allocated in-process worker | ❌ dies with instance scale-down/revision replace; ~$100/mo idle |
| Cloud Run worker pools | ⚠️ no autoscaling — a fixed 24/7 instance for a few runs/week |
| Cloud Batch / spot VMs | ⚠️ runner-up; no benefit until a run needs >7 days or many vCPUs |

**Postgres (`import_job`) is the job store and single source of truth.** Cloud
Run execution status (`execution_name`) is forensics only. The API never reads
the Executions API.

## Decision 3 — source refs and their security posture

Body: exactly one of `{"href": …}` (one object — HTTPS or `gs://`) or
`{"prefix": "gs://bucket/path/"}` (every `.json/.geojson/.ndjson/.geojsonl`
object under it, name-ordered, capped by `GEOID_JOB_MAX_FILES`).

- **https**: scheme allowlist, **vendor host allowlist**
  (`GEOID_JOB_ALLOWED_URL_HOSTS`, defaults = GCS + S3 hosts), no userinfo, no
  IP literals, port 443 only. The host allowlist is THE SSRF control (GCP
  metadata's `Metadata-Flavor` header requirement is only a backstop);
  DNS-rebinding machinery is deliberately skipped while hosts are vendor-fixed.
- **gs**: bucket must be in `GEOID_JOB_ALLOWED_BUCKETS` (default **empty = gs
  disabled**) — kills the confused-deputy problem (any authenticated caller
  pointing our runtime SA at any bucket it can read).
- A signed URL's query string is a **bearer secret**: stored in
  `import_job.source_ref` for the worker, but never echoed — results/logs/error
  text carry only the query-stripped form.
- Validation runs at submit (422) AND re-runs in the worker (defense in depth).
- Job creation is **authenticated-only** (anon → 401) even for `public_write`
  collections: a job snapshots its creator for later authz re-checks and status
  visibility. Status reads are creator-or-sysadmin; everyone else gets the
  masked `no-such-job` 404.

## Decision 4 — worker mechanics

- **Claim CAS** (`UPDATE … SET status='running' WHERE status='accepted'`) is
  the idempotency hinge: a duplicate dispatch / manual re-execute no-ops.
- **Spool to disk first, then DB work** — a transaction is never held across
  network I/O (the 60 s idle-in-tx timeout would kill it). Byte cap counted
  while streaming (a declared Content-Length is a fast-fail, never trusted).
- **Streaming parse** — first-byte sniff: RS (0x1E) → RFC 8142 GeoJSONSeq; a
  first line that parses as complete non-FeatureCollection JSON → NDJSON;
  otherwise a FeatureCollection via `ijson` (constant memory; `json.loads` of a
  100 MB body on this container is an OOM coin flip). Global feature cap
  enforced mid-stream; a write bound errors, never truncates.
- **Chunked minting (500/chunk) through `create_places_bulk` verbatim** —
  SAVEPOINT-per-feature machinery untouched; outcomes re-indexed by chunk
  offset; commit per chunk. `_authorize_write` re-runs inside every chunk, so a
  grant revoked between submit and run is honored for free.
- **Heartbeat is the LAST statement of each chunk transaction** — atomic with
  the work it reports. It is guarded `WHERE status='running'`; zero rows means
  the row was flipped externally → the worker aborts and can never resurrect
  the row (this guard is also the future cancel seam).
- **On-read reaper** (no scheduler): reading a status flips a `running` row
  whose heartbeat is older than `GEOID_JOB_STALE_SECONDS` (600) or an
  `accepted` row older than 15 min to `failed`. Convergent against a live
  worker because of the guarded writes; a reaped zombie's stray inserts are
  harmless (dedup).
- **In-row JSONB report**, written once at success: read-whole, ≤ ~25 MB at the
  100k-feature cap — a child table buys paging nobody serves. No secondary
  indexes (the PK covers every query; add `(created_by, created_at)` with
  `GET /jobs`).
- Failure messages are sanitized (exception class / query-stripped source
  label; never upstream bodies). DB connection loss is NOT retried
  (`failed('database connection lost; resubmit')`); SIGTERM (Cloud Run sends it
  10 s before SIGKILL) → best-effort guarded `failed`.

## Consequences

- large-scale loads become one submit + polling; committed chunks survive any
  mid-run failure and a resubmit converges via dedup.
- New table `import_job` (migration 0010, MUTABLE — trigger inventory stays 7),
  new Cloud Run Job `${SERVICE}-import` (deploy-only in CD; executions are
  created by the service), `jobs` dependency extra
  (httpx/ijson/google-cloud-run/google-cloud-storage, all lazily imported).
- One-time IAM: runtime SA → `roles/run.jobsExecutorWithOverrides` on the
  import job (job-scoped; no actAs) + `storage.objectViewer` on each allowed
  source bucket.
