---
updated: 2026-08-23
---

# Phase 2.6 — Production hardening (As-Built, partial)

> Status: **In progress.** This first pass landed the startup-path correctness fixes, the
> container deployment story, and the three security leftovers Phase 2.5 deferred. The
> throughput/abuse items from [`roadmap.md`](../roadmap.md) §2.8 — shared rate limiter, per-IP
> limits, frame GC, storage budget, TLS, shadow-ban — are **not** done and remain the bulk of
> the phase.
>
> Companion to [`roadmap.md`](../roadmap.md) §2.8–2.10,
> [`enterprise-architecture-plan.md`](../architecture/enterprise-architecture-plan.md) §4.1 (P2.6), and the
> "Known gaps" list in [`road-test-readiness.md`](../runbooks/road-test-readiness.md).

## Context

Everything through Phase 2.5c shipped: ingestion, the ported sensor classifier, fusion,
clustering, both read paths, staff auth, tiles, cluster detail, audited repair marking, and the
operator dashboard. What remained was the unglamorous half — the things that only bite in
deployment or under an adversary.

Three of those were **live defects rather than missing features**, which is why they went first:
a 500 on a public unauthenticated endpoint, a health check that could not report ill health, and
a production boot that silently created no tables. A fourth, cross-tenant repair writes, was a
security hole the codebase already documented against itself.

---

## 1. A 500 on the public read path (`GET /api/v1/potholes`)

`_parse_bbox` validated ranges and ordering but not **span**. A bbox wider than 180° of
longitude produces a geography envelope whose corners can be antipodal, PostGIS raises
`Antipodal (180 degrees long) edge detected!`, and the asyncpg exception went unhandled — an
HTTP 500 on the one endpoint that is public and unauthenticated.

Rejected at two layers:

- **`app/routes/potholes.py`** — `MAX_BBOX_LON_SPAN_DEG = 180.0`, checked in `_parse_bbox`.
  Exactly 180 is still accepted (verified to work); no real map viewport comes close.
- **`app/services/cluster_query_service.py`** — the two `conn.fetch` calls are wrapped in an
  `asyncpg.exceptions.PostgresError` handler that logs the bbox and re-raises as 400. Defence in
  depth: anything PostGIS refuses to *measure* is a bad request, not a server fault.

> **Note on the fix's provenance.** Three committed files already asserted this guard existed —
> `app/routes/clusters.py` (reusing `_parse_bbox` precisely because "it already… caps the
> longitude span"), `migrations/007_tiles.sql`, and
> [`phase-2.5-dashboard-plan.md`](./phase-2.5-dashboard-plan.md). It did not. This was the
> implementation catching up to its own documentation.

The same refactor hoists the row→model mapping out of the `pool.acquire()` block, so the
connection is held only for the query.

**Deliberate wart:** `cluster_query_service` now raises `fastapi.HTTPException`, which no other
service in `app/services/` does. A domain error plus a route translation is more machinery than
one call site earns.

## 2. `GET /health` could not report ill health

The `except` branch **returned a dict**, which FastAPI served as **HTTP 200** with
`{"status": "unhealthy"}` — while the docstring promised 503. Any status-code-only uptime check
reported green against a dead database, and the compose healthcheck added in §4 below would have
been decorative.

Now returns `JSONResponse(status_code=503, ...)`. The body shape is unchanged, so anything
parsing `status` keeps working.

## 3. `run_fit_job` had no advisory lock

Fusion (`0x504F54`), clustering (`0x504F55`) and detection (`0x504F56`) were all single-flight.
The sensor-model fit was not. The Dockerfile runs `uvicorn --workers 2`, so two schedulers can
tick simultaneously, both fit, and both call `save_model` — racing on the
`idx_sensor_model_active` partial unique index.

`run_fit_job` now takes **`0x504F57`** and delegates to `_run_fit_locked`. The lock is held on
its **own** connection for the whole job because `load_active_model` / `save_model` take the
pool rather than a connection.

### Advisory lock registry

| Key | Holder |
|---|---|
| `0x504F53` | `run_migrations` (`app/database.py`) — **blocking**, not `try` |
| `0x504F54` | `run_fusion_job` |
| `0x504F55` | `run_cluster_job` |
| `0x504F56` | detection worker (`app/detection/service.py`) |
| `0x504F57` | `run_fit_job` |

## 4. Migrations: `ENV=production` created no tables

`app/main.py` ran `run_migrations()` only when `settings.env == "development"`. Starting with
`ENV=production` against a fresh database produced **no schema at all**, then served 500s.
Compounding it, `run_migrations` had no ledger — it re-executed all files on every dev boot,
which only worked because every migration is written idempotently.

Replaced with a tracked ledger:

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

- Runs in **every** environment; the `env` gate is gone.
- **One transaction per file.** A failure leaves no ledger row, so the next boot retries that
  file rather than skipping it.
- **Blocking** advisory lock (`pg_advisory_lock`, not `try`): a second worker waits and then
  finds nothing to do, rather than racing onto a half-built schema.
- A **changed checksum warns** and skips rather than failing the boot. Editing an applied
  migration is a mistake, but refusing to start over it is worse.

**Upgrading an existing database** is a no-op in effect: the ledger starts empty, so all files
re-apply once, and all of them are idempotent (`CREATE … IF NOT EXISTS`, `ON CONFLICT DO
NOTHING`). Verified live — a subsequent boot applied only the new `009` and skipped the other
eight.

## 5. The container served no dashboard and no basemap

The compose `app` service built from a Dockerfile that copied only `app/` and `migrations/`. It
never ran `npm run build` and never copied `dashboard/dist`. Because `app/main.py` guards both
static mounts with `is_dir()`, the container **booted clean and silently served nothing** —
`/dashboard/` returned 404, and the whole Phase 2.5/2.5b operator console was unreachable in the
containerised app. Root `.gitignore` has `dist/`, so the bundle was not in the repo either.

- **Two-stage `Dockerfile`.** `node:22-slim` builds the dashboard, the python stage takes it via
  `COPY --from`. Manifests are copied before `npm ci` so the dependency layer caches
  independently of source. The `prebuild` hook copying MapLibre's worker out of `node_modules`
  is load-bearing: without it every vector source silently fails and the map is blank, not
  broken.
- **`storage/basemap` is a read-only mount, not baked in.** It is a per-city build artefact
  outside git (~19 MB for Toronto); a different pilot city means a different archive behind the
  same image.
- **`VOLUME ["/opt/server/storage/frames"]`** — scoped to frames deliberately. Declaring the
  parent `storage/` would shadow the basemap mount.
- **Healthcheck** on the `app` service, using the interpreter already present (no `curl` in
  `python:3.12-slim`). `urlopen` raises on non-2xx, so §2's real 503 is what makes it work.
- **`env_file` is `required: false`** so a fresh clone can `docker compose up` before anyone
  copies `.env.example` across. Image defaults are enough to boot; secrets only gate the staff
  tier.
- **Port 8000 collides** with the hand-run `uvicorn` the README prescribes. They are
  alternatives, not a sequence — running both gives "port is already allocated".

---

## 6. Per-municipality write scoping (`migrations/009_org_scoping.sql`)

`asset_cluster` had no `org_id`, so **any staff member of any org could mark any city's cluster
repaired**. `005_auth.sql` had laid the `org_id` columns on the auth tables and explicitly
deferred RLS; `app/routes/clusters.py` documented the resulting hole against itself. Reads were
already global, which was tolerable — the audited *write* endpoint is what made it matter.

```sql
ALTER TABLE asset_cluster ADD COLUMN IF NOT EXISTS org_id TEXT REFERENCES org(org_id);
CREATE INDEX IF NOT EXISTS idx_asset_cluster_org ON asset_cluster (org_id)
    WHERE org_id IS NOT NULL;
```

Enforced in `app/services/repair_service.py`:

| Cluster `org_id` | Caller | Result |
|---|---|---|
| matches caller's org | `staff`+ | allowed |
| differs | any role, **including `admin`** | **403** |
| `NULL` (unowned) | `admin` only | allowed |

Three decisions worth keeping visible:

- **No backfill.** There is no municipal boundary table to assign existing rows by geography,
  and defaulting every row to one org asserts ownership nobody established the moment a second
  municipality exists. `NULL` means unowned, and fails closed.
- **The check runs inside the same transaction as the row's `FOR UPDATE`.** Reading the owner,
  deciding, then writing in separate transactions would let a concurrent re-assignment slip
  between them. This is why the former single-statement CTE is now a lock, a decision, and a
  write.
- **403, not 404.** The caller holds a valid staff token and the cluster is already visible to
  them on the map and in the detail panel, so pretending it does not exist would only confuse.
- **Enforcement is in the API, not RLS.** The API is the only writer, and an RLS policy would
  need a per-request `SET LOCAL` to see the caller's org through the shared asyncpg pool.

### ⚠️ Live limitation — repair is effectively admin-only

**The clustering job sets no `org_id`**, so every cluster the pipeline produces is unowned, and
a plain `staff` operator cannot close any of it out — which is the normal operator workflow.
Pinned by `test_job_created_cluster_is_admin_only` so it cannot be forgotten.

Assigning an owner in `_compute_clusters` needs a device→org mapping that does not exist:
ingestion is anonymous by design, and `asset_observation` carries a `device_id` UUID with no
tenant attribution. Resolving it means either the road-centreline/boundary import (see
[`phase-2.5b-dashboard-design.md`](./phase-2.5b-dashboard-design.md) §10) or a device
registration tier. Until then, provision operators who need to mark repairs as `admin`.

**Reads remain global.** Narrowing `/tiles/*`, `/clusters/*` and `/potholes/detail` is a visible
behaviour change for existing operator accounts and was not needed to close the write hole.

## 7. EXIF stripped on ingest (`app/services/jpeg_metadata.py`)

`_store_jpeg_local` wrote client bytes verbatim. Road frames are photographs of public streets —
plates, faces, house numbers — and phone cameras stamp GPS into EXIF. Every stored frame is
served to any `viewer` account via `GET /api/v1/frames/{client_id}/image`, so the metadata was a
wider exposure than the pixels.

Stripping happens in `store_frame`, **before** `_store_jpeg`, so it covers both the local and
Supabase backends and the archive never holds the metadata at all — scrubbing later would leave
it in any backup taken in between.

**Lossless segment removal, not an image-library re-encode.** The entropy-coded scan is copied
byte for byte, so there is no generation loss and no quality setting to get wrong; it needs no
new dependency and is fast enough for the ingestion path.

| Marker | Action | Why |
|---|---|---|
| APP1 (`0xE1`) | **dropped** | EXIF *and* XMP — either can hold a location |
| APP3–APP13 (`0xE3`–`0xED`) | **dropped** | maker notes, Photoshop/IPTC |
| APP15 (`0xEF`), COM (`0xFE`) | **dropped** | free-text comment is a plausible location note |
| APP0 (JFIF) | kept | harmless; some decoders expect it |
| APP2 (ICC) | kept | dropping it can visibly shift colour |
| APP14 (Adobe) | kept | governs colour-transform interpretation |

Conservative by construction: anything unparseable is copied through verbatim and returned, so a
frame is never corrupted or lost because the parser disagreed with it.

**Still open:** plates and faces are in the *pixels*. Redacting those is a detection model, not
a parser.

## 8. `POST /api/v1/auth/logout`

Did not exist. Clearing client memory left the refresh token valid server-side for its full
30 days. `app/auth/service.py` already stored refresh tokens SHA-256-hashed and revocable, so
this is a thin route over existing machinery.

- **Always 204**, even for an unknown, expired or already-revoked token. A caller able to
  distinguish "revoked something" from "that was never valid" has a token-probing oracle.
- **Scoped to the presented token**, so signing out one browser leaves the account's other
  sessions alone.
- **POST, not DELETE** — CORS `allow_methods` is `GET/POST/OPTIONS`.
- The **access token stays valid until it expires** (30 min default). Revoking that needs a
  denylist, which this gap did not justify.

---

## Wire-contract impact

**None.** Ingestion payloads, the `X-Device-Id` tier and the `GET /api/v1/potholes` response
shape are untouched; §6–§8 affect only the staff tier and stored bytes. The one client-visible
change is §1 turning a 500 into a 400 on a bbox no real viewport produces. Confirmed the Android
client never calls `/health`, so §2's new 503 cannot break it.

## Verification

- **264 tests pass** (up from 233), against local PostGIS on `:5433`. New coverage: 5 bbox-span
  and PostGIS-error cases, 1 health-503 case, 1 fit-lock case, 13 JPEG-metadata cases, 1
  end-to-end EXIF-on-disk case, 3 logout cases, 7 org-scoping cases.
- **Health 503, live** — stopped `postgres`, `/health` returned 503, and the compose healthcheck
  went `exit=0 → exit=1` and marked the container unhealthy. It had reported green before.
- **Migration ledger, live** — two workers: one applied 8 files, the other waited and reported
  the schema current. A later boot applied only `009`.
- **Container, live** — `/dashboard/` 200, `/basemap` range request 206, MapLibre worker 200; the
  "No dashboard bundle" / "No basemap archive" log lines are gone.
- **Dashboard, in a browser** — logged in, dock and detail panel render against the vector
  basemap with **zero console errors**.
- **EXIF** — a JPEG with a GPS IFD ingested; the file on disk has no `Exif\0\0` and no camera
  make, and decodes pixel-identical to the original.

## Repo hygiene done alongside

The two `files/*.zip` training datasets (~4.1 GB of blobs) were tracked in git. GitHub
hard-rejects any file over 100 MB, so `development` was **unpushable**. They were stripped from
the 21 unpushed commits and added to `.gitignore`; `files/MatlabCode/*.m` (29 KB of original
2017 research code) stays tracked. The push then carried 1.74 MB.

> **Data-loss note, recorded so it is not repeated.** The working-tree copy of
> `files/AI Pothole Detection.yolov8.zip` was ~6.87 GB while the committed blob was 2.55 GB.
> `git filter-branch` checks out the rewritten tree, which deleted both zips from disk, and the
> restore put back the smaller *committed* version. The larger copy was never in git and is
> gone. **Move large untracked-in-effect files aside before any history rewrite, and check
> worktree size against `git ls-tree -l` rather than trusting a clean `git status`.**

## What is left in Phase 2.6

From `roadmap.md` §2.8–2.9 and `road-test-readiness.md` "Known gaps":

- ~~**Shared rate limiter.**~~ **Done 2026-08-31.** `app/middleware/rate_limit.py` now counts in
  `device_rate_limit` (unused since migration 001; `migrations/016` adds the prune index).
  Verified live against `--workers 2`: five requests produced one shared counter of five, where
  the old module-level `defaultdict`s would have kept two private ceilings. It **fails open** and
  logs at ERROR if the quota query breaks — a device that cannot upload loses collected drive data
  permanently, whereas overshooting a quota costs a few rows of disk.
- **Per-IP limits** (roadmap §2.8) and **any limiting at all on the public read path** — the
  latter explicitly out of scope in `phase-2.2b-read-path-plan.md`.
- **Frame retention/GC + 500 MB/device storage budget.** `storage/` grows without bound.
- **TLS.** Plain HTTP; fine behind a tunnel, not for a pilot.
- **Shadow-ban** for pathological clusters, and manual `verified` cluster creation (§2.9).
- **Stale clusters are never deleted** once their members age out of the 30-day window.
- **`org_id` on new clusters** — see §6's live limitation.
