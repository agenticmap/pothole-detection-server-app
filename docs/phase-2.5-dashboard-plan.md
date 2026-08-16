---
updated: 2026-08-16
---

# Phase 2.5 — Operator dashboard (server side)

> Status: **Steps 1–2 implemented** (vector tiles + detail panel + repair marking).
> Steps 3–4 (the browser frontend and live push) are **not started**.
> Superseded numbering: `enterprise-architecture-plan.md` §4.1 calls this "P2.3", which
> collides with the shipped Phase 2.3 (server detection). It is 2.5 here and in the code.

## Why

Phase 2.4 built a staff auth tier for exactly one reason — to gate the
severity/confidence detail that *is* the municipal product — and then used it on a single
JSON endpoint that no human-facing surface consumed. The operator dashboard
(`enterprise-architecture-plan.md` §3) is the surface the whole enterprise plan aims at, and
it was the largest missing deliverable.

This phase builds the **server side** of it: vector tiles for the map, a detail panel payload,
and the first write path to `asset_cluster` outside the clustering job.

## What landed

### Groundwork (two hard blockers)

- **`app/dependencies.py`** — `require_min_role(minimum)`, a dependency factory. Roles are
  **ranked** (`viewer` 10 → `staff` 20 → `admin` 30), not an allow-list, so it cannot develop
  the classic "forgot to include admin" bug. An unknown or absent role ranks 0 and is denied,
  which matters because `decode_access_token` defaults `role` to `""`. Returns 403, never 401
  — identity is already proven by then. Aliases: `ViewerOrAbove`, `StaffOrAbove`, `AdminOnly`.
  Before this, `get_current_staff` only proved a token was valid and **no route consulted
  `staff.role` at all**.
- **`require_min_role_live(minimum)`** + `StaffOrAboveLive` — same check, but re-reads
  `org_member.role` from the database. For **write** endpoints only. The JWT role is a
  login-time snapshot (`app/auth/service.py` reads it at login and refresh), so a demotion
  takes up to `auth_access_token_ttl_minutes` (30) to bite. That trade is right for reads — the
  alternative is a DB round-trip in front of every tile — and wrong for a mutation.
- **`scripts/create_staff.py`** — a CLI, deliberately not an endpoint (the staff tier is
  admin-provisioned; there is no self-signup). Before this, the only way to create an account
  was hand-written SQL, and the only `INSERT` in the tree was in `tests/test_auth.py`.
  Password comes from `POTHOLE_STAFF_PASSWORD` or an interactive prompt, never `argv`, so it
  stays out of shell history and the process list. It validates the email with the same
  `EmailStr` the login route uses — otherwise you can provision an account that can never log
  in, which this caught in practice (`EmailStr` rejects reserved TLDs such as `.local`).

### Step 1 — Vector tiles

- **`app/routes/tiles.py`** — `GET /api/v1/tiles/clusters/{z}/{x}/{y}.mvt` and
  `…/observations/{z}/{x}/{y}.mvt`, both `ViewerOrAbove`. Returns
  `application/vnd.mapbox-vector-tile` with `Cache-Control: private, max-age=60`.
- **`app/services/tile_service.py`** — `ST_AsMVT` / `ST_AsMVTGeom` generation, tile-coordinate
  validation, a concurrency semaphore and a per-query timeout.
- **`migrations/007_tiles.sql`** — functional GiST indexes on
  `ST_Transform(centroid::geometry, 3857)` (and the same for `asset_observation.geom`), plus a
  partial visibility index and one on `updated_at` for change polling.
- Query parameters: `asset_type`, `min_devices`, `include_repaired`, `window_days`,
  `severity_min`, so operators can see what the public path hides.

### Step 2 — Detail panel + repair marking

- **`app/routes/clusters.py`** —
  - `GET /api/v1/clusters/{cluster_id}` (`ViewerOrAbove`) — cluster header, member
    observations, paired frames, and recent repair history in one response.
  - `GET /api/v1/frames/{client_id}/image` (`ViewerOrAbove`) — the stored JPEG.
  - `POST /api/v1/clusters/{cluster_id}/repair` (`StaffOrAboveLive`) — body
    `{repaired: bool, note: str|null}`.
- **`app/services/cluster_detail_service.py`** — the four read queries.
- **`app/services/repair_service.py`** — the only mutation of `asset_cluster` outside the
  clustering job, kept in its own module so that stays auditable.
- **`app/models/clusters.py`** — `ClusterDetailResponse`, `ClusterMemberItem`,
  `ClusterFrameItem`, `RepairLogItem`, `RepairRequest`, `RepairResponse`.
- **`migrations/008_repair_log.sql`** — the repair audit trail.
- **`app/fusion/service.py`** — `_compute_clusters` extracted, and a repaired-covering guard
  before `_INSERT_CLUSTER_SQL` (see "The race", below).

## Decisions worth keeping

### Tiles are served from FastAPI, not Martin

`enterprise-architecture-plan.md` §3.3 specifies Martin (Rust) generating MVT from PostGIS.
Rejected for the MVP because **Martin has no authorization layer at all** — it expects a
reverse proxy — so putting it in front of staff-gated data means a second deployable plus a
proxy instead of reusing the `ViewerOrAbove` dependency that already exists. Its SQL-function
config would also have to duplicate the thresholds in `app/config.py`, where they would drift.
Measured cost of doing it in Python: 1–3 ms against an 80 ms p50 budget. The URL shape matches
Martin's, so it can be swapped in later without touching the client.

### The tile SQL stays planar, and needs its own index

`asset_cluster.centroid` is `GEOGRAPHY` and `idx_asset_cluster_centroid` is a GiST over it.
`ST_TileEnvelope` returns **3857 geometry**, which that index cannot serve — every tile would
be a sequential scan. The obvious fix (transform the envelope back to 4326 and cast to
geography, as the bbox read path does) is wrong: at z0/z1 a tile spans ≥ 180° of longitude and
`::geography` then raises `Antipodal (180 degrees long) edge detected!` — the exact failure
`app/routes/potholes.py` already had to work around. Indexing the transformed geometry keeps
the whole query planar and removes the hazard. Both casts are `IMMUTABLE`, so the expression
index is legal.

### Deferring materialized views is not deferring aggregation

§3.3 calls for `cluster_tile_mv_z10/14/18`. Those are premature at current row counts, but
without *in-query* aggregation a low-zoom tile is unbounded — at z10 one tile covers a city.
`ST_SnapToGrid` binning below `tile_aggregate_max_zoom` (12) plus a per-tile `LIMIT` fixes
that without any materialized view. Measured on 20k synthetic clusters over one city:

| tile | unaggregated | aggregated |
|---|---|---|
| z10 | 20,000 features, **425 ms** | **89 ms** |

Materialized views remain the escape hatch if p95 degrades.

### Tile queries must not starve ingestion

The asyncpg pool is 20 connections shared with `POST /api/v1/events`, and MapLibre fans out
4–8 tile requests per pan. Tile and image queries run under an `asyncio.Semaphore` with an
explicit per-query `timeout`, so one pathological tile cannot hold connections long enough to
queue ingestion behind it.

### The operator filter is not the public filter

`cluster_query_service._FILTER` encodes the *public* visibility rules (`repaired_at IS NULL`,
`distinct_devices >= 2`). Reusing it for the dashboard would hide exactly what operators need:
single-device clusters are the triage queue, and repaired clusters drive the sidebar toggle.
The tile layer takes its own parameterised filter, defaulting to the public values.

### Cluster → frames goes through `fusion_pair`

The clustering job writes **only** `kind='observation'` links (the literal is hardcoded in
`_INSERT_LINK_SQL`), so there are no `kind='frame'` rows despite the CHECK constraint allowing
them. And `asset_frame.event_client_id` is a **nullable, unindexed, no-FK client-supplied hint
that is almost always NULL** — `_PAIRING_SQL` reconstructs pairing by device, time and
distance and never reads it. The only reliable path is:

```
observation_cluster_link (kind='observation')
  → asset_observation.client_id
  → fusion_pair.event_client_id
  → fusion_pair.frame_client_id
  → asset_frame.client_id
```

Joining on `event_client_id`, or expecting `kind='frame'` links, yields a permanently empty
panel. `tests/test_cluster_detail.py::test_frame_linked_only_by_event_client_id_is_not_found`
fails if someone "simplifies" this.

**No index was added on `event_client_id`.** An earlier review recommended one; once the join
goes through `fusion_pair` nothing queries that column, so it would cost writes for no reader.

### Members and frames are separate queries

Pairing is many-to-many in both directions, so a single joined statement multiplies member
rows by their frame count and `LIMIT` then truncates a multiplied set at an arbitrary
boundary — "200 rows" that represent 40 observations. Four queries on one pooled connection
cost about a millisecond; the panel issues one HTTP request either way, which is what the
400 ms p95 budget measures. `DISTINCT ON (f.client_id)` collapses the other direction of the
fan-out so a frame paired to several observations appears once, carrying its strongest pairing.

Verified on 5,000 clusters / 60,000 links / 20,000 pairs: every join step is an index scan on
an existing primary key, 0.24 ms total. **No new indexes.**

### `device_id` never leaves Postgres

`roadmap.md` §2.11 commits that `device_id` is never exposed to non-admin clients. Members
carry `device_ref`, a **per-cluster ordinal** from `dense_rank() OVER (ORDER BY device_id)`,
rendered "Device A / B / C". Window functions evaluate before `LIMIT`, so the ranking covers
the full member set and stays consistent with `distinct_devices` even when the list is
truncated.

A truncated `sha256(device_id)` was the original design and was **rejected**: a pseudonym
stable across every cluster in the city lets an operator join it across the map and
reconstruct a driver's route. That is a *worse* privacy posture than withholding `device_id` —
it renames the pseudonym rather than removing it.

`ClusterFrameItem` also has **no `jpeg_url` field**, because the stored path is
`"{device_id}/{client_id}.jpg"`; the image endpoint is keyed on `client_id` instead. A test
asserts on the raw serialized body that neither the device id nor `jpeg_url` appears.

### No thumbnails (measured, not assumed)

A design review called thumbnails mandatory, reasoning from `max_frame_size_bytes` (10 MB).
The 59 real JPEGs on disk are **12–62 KB, median 39 KB**, because the client compresses at
`JPEG_QUALITY = 70` (`vision/PotholeFrameAnalyzer.kt`). Twelve frames ≈ 470 KB. The `?size=`
parameter is validated as `Literal["full"]` today so `?size=thumb` 422s rather than silently
serving full-size, which keeps adding `thumb` later from being a breaking change.

This also avoids promoting Pillow from a lazily-imported optional dependency into a
request-path one — note it is pinned in `requirements.txt` but **absent from
`pyproject.toml`**, so `pip install .` gets no Pillow.

### `POST`, not `PATCH`

Repair marking is a **command with an audit trail**, not a partial-resource update. (It also
happens to avoid widening `allow_methods` in `app/main.py`, which is `["GET","POST","OPTIONS"]`
— but that is a consequence, not the reason. A `PATCH` route would have passed every test,
since httpx's `ASGITransport` does not enforce CORS, and failed its preflight in a browser.)

### Repair is idempotent in the strict sense

**Re-stamping `repaired_at` is a data-loss bug.** `_MEMBERS_CTE` excludes observations older
than a nearby `repaired_at`, so a second `repaired: true` that re-stamps `now()` moves that
exclusion window forward and **retroactively swallows every observation recorded between the
two calls** — exactly the "the defect came back" evidence the system exists to capture. A
double-click would erase it.

So the `UPDATE` fires only on a real state change (`(t.repaired_at IS NULL) = $2`), in one
`FOR UPDATE` statement, and only then is an audit row written. `found=0` → 404;
`found=1, changed=0` → 200 no-op with no log row; `changed=1` → 200 plus exactly one row.

It also bumps `updated_at`, which is load-bearing rather than cosmetic: `007_tiles.sql` added
`idx_asset_cluster_updated_at` for change polling and `cluster_query_service._FILTER` selects
on `updated_at > $since`. Without the bump an incremental client never learns about the repair.

### The race: a repair mid-run could resurrect a repaired pothole

`run_cluster_job` runs `_MEMBER_STATS_SQL` and `_CLUSTER_SQL` **outside** the transaction,
which only opens afterwards. A repair committed in that window produces:

1. `_CLUSTER_SQL` computes members before the repair is visible → the closed-out observations
   are in the result set.
2. The repair commits.
3. The transaction opens; `_FIND_EXISTING_SQL` (`WHERE repaired_at IS NULL`) no longer matches
   the just-repaired cluster.
4. `_INSERT_CLUSTER_SQL` fires — creating a **new, un-repaired cluster at the same centroid,
   made of exactly the observations the operator just closed out.**

The window is one DBSCAN pass every 15 minutes, so it is unlikely — but silent and permanent:
the resurrected cluster is never cleaned up and the repair appears to have done nothing.
Widening the transaction does **not** fix it (READ COMMITTED re-snapshots per statement).

**Fix:** `_FIND_REPAIRED_COVERING_SQL` runs immediately before the insert and skips it if a
repaired cluster within `eps_m` has `repaired_at >= last_seen`. This closes the window
regardless of statement ordering and independently hardens against device-clock skew
(`repaired_at` is server time; `last_seen` derives from the device-supplied `ts_utc`).

`_compute_clusters` was extracted so a test can interpose between compute and write and
reproduce the race deterministically. **That test was verified to fail with the guard
disabled** — a regression test that passes either way proves nothing.

### Frame images

The storage path is looked up from the database, never taken from the client, and still passes
through `resolve_local_frame_path` — `jpeg_url` is unconstrained `TEXT`, and a bad or legacy
row must not become an authenticated arbitrary-file read.

Starlette's `FileResponse` re-raises a missing file as **`RuntimeError`**, which the catch-all
handler in `app/main.py` turns into a **500**. The route therefore stats the path first and
404s explicitly. `media_type="image/jpeg"` is passed explicitly (`guess_type` falls back to
`text/plain` for a path without a `.jpg` suffix) and `filename=` is deliberately omitted (it
would flip `content_disposition_type` to `attachment` and download instead of render).

## Security fix found during this phase (not part of the dashboard)

**Unauthenticated path traversal in frame storage** — `d232031`. Frame JPEGs are stored at
`"{device_id}/{client_id}.jpg"`, and neither component was charset-validated:
`require_device_id` only checked non-empty, and `FrameMetadata.client_id` only bounded length.
`X-Device-Id: ../../..` therefore resolved outside the storage root, and `_store_jpeg_local`
calls `mkdir(parents=True)`. Since the ingestion tier is anonymous by design, this was a
**live unauthenticated write primitive**. Confirmed empirically before fixing.

Fixed at three deliberately redundant layers (`app/validators.py::is_safe_id`, applied in
`require_device_id` and `FrameMetadata`, plus a containment check in `_store_jpeg_local`).
Note `.` is a legal charset member, so the charset alone still admits a bare `.` or `..`;
those are excluded separately — which is why `is_safe_id` is a function rather than a
`Field(pattern=…)` (Pydantic v2's Rust regex engine has no look-around).

Not a v1 wire break: the Android client sends `UUID.randomUUID().toString()` for both ids.

## Config (`app/config.py`)

| Setting | Default | Purpose |
|---|---|---|
| `tile_aggregate_max_zoom` | `12` | at/below this a tile is grid-aggregated |
| `tile_observations_min_zoom` | `15` | raw points are street-level only |
| `tile_max_features` | `4000` | per-tile cap |
| `tile_extent` / `tile_buffer` | `4096` / `64` | MVT coordinate space / edge bleed |
| `tile_aggregate_bins` | `32` | grid cells across a tile when aggregating |
| `tile_max_concurrency` | `6` | shared by tile + image routes |
| `tile_query_timeout_seconds` | `2.0` | fail fast rather than hold a connection |
| `tile_cache_seconds` | `60` | tile `Cache-Control: private, max-age=` |

Detail-panel bounds are module constants in `cluster_detail_service.py`: `MAX_MEMBERS` 200,
`MAX_FRAMES` 12, `MAX_REPAIR_HISTORY` 20.

## Provisioning a staff account

```bash
POTHOLE_STAFF_PASSWORD='…' python scripts/create_staff.py \
    --org org_cambridge --name "City of Cambridge" \
    --email jane@cambridge.gov --role staff
```

Supersedes the manual `INSERT` recipe in `phase-2.4-auth-plan.md`.

## Tests

**206 passed** (166 before step 2). New files:

- `tests/test_roles.py` (18) — rank ordering, `admin` satisfying a `staff` floor, fail-closed
  on unknown/absent roles, 401-vs-403 through a real bearer header.
- `tests/test_tiles.py` (28) — coordinate validation, auth, decoded tile contents, the
  aggregate/individual zoom tiers, operator filters, and a z0/z1 regression for the antipodal
  bug.
- `tests/mvt.py` — a ~60-line protobuf reader so tile assertions check decoded layer names,
  feature counts and attribute keys. Asserting on byte length alone would not have caught the
  wrong-tile bug below; adding `mapbox-vector-tile` (protobuf + shapely) purely for test
  assertions was not worth the dependency.
- `tests/test_cluster_detail.py` (23) — the `fusion_pair` join, both directions of the
  many-to-many fan-out, limits and truncation flags, `device_ref` labelling, the anonymity
  assertion on the raw body, and the frame-image 404 paths including a traversing `jpeg_url`.
- `tests/test_repair.py` (17) — authorization including a demoted user with a still-valid
  token, no-op idempotency, `updated_at`, unrepair appending a second row, downstream
  invisibility in both the public read path and tiles, and the three clustering-interaction
  regressions.
- `tests/test_path_traversal.py` (15) — all three layers of the security fix.

### Test-database isolation

The `db_pool` fixture TRUNCATEs every table and **defaulted to the dev database**, so a stray
`pytest` would have destroyed collected drive data (there were 137 real observations at the
time). Tests now target `pothole_test`, and `conftest.py` holds an allow-list that fails
loudly on any other database name — the guard is on the database *name* rather than on
trusting whoever set `DATABASE_URL`, because the action is unrecoverable.

One-time setup on an existing volume:
`docker compose exec postgres createdb -U pothole pothole_test`.

## A bug worth remembering

`x` and `y` in the tile routes initially shared **one** FastAPI `Path()` instance. FastAPI
sets the alias on the object it is handed, so the second parameter overwrote the first's
binding and both read the same path segment — the endpoint returned a valid, empty,
**wrong** tile with a 200 rather than erroring. Each parameter now gets its own instance.

## Measured

| | |
|---|---|
| z14 tile (individual), 20k clusters | p50 15 ms / p95 27 ms |
| z10 tile (aggregated), 20k clusters | p50 69 ms / p95 81 ms |
| §3.4 budget | p50 < 80 ms / p95 < 250 ms |
| detail queries, 5k clusters / 60k links | 0.24 ms, all index scans |
| real frame JPEGs on disk | 12–62 KB, median 39 KB |

## Out of scope / next

- **Step 3 — the browser frontend** (`dashboard/`, Vite + MapLibre + PMTiles). Three things
  are already known to bite: `transformRequest` is **synchronous** and cannot await a token
  refresh (MapLibre's failure mode on 401 is silently blank tiles); `<img src>` **cannot send
  an `Authorization` header**, so frame images need fetch-plus-blob-URL; and
  `AUTH_JWT_PRIVATE_KEY_PEM` is unset, so `app/auth/keys.py` mints an ephemeral keypair per
  process and every reload logs the dashboard out. Serve `dashboard/` from the FastAPI origin
  to avoid an `OPTIONS` preflight per tile URL.
- **Step 4 — live updates.** `NOTIFY` on `asset_cluster` → `LISTEN` → WebSocket. Note the data
  only changes every `clustering_interval_minutes` (15), so a sub-2-second push target is
  largely theatre today; `updated_at` polling is the honest MVP.
- **Per-municipality scoping.** `asset_cluster` has no `org_id`, so **any staff member of any
  org can repair any city's clusters**. Reads were already global; the write endpoint makes it
  matter. Direct consequence of the deferred RLS in `005_auth.sql`.
- **EXIF / PII in frames.** Road JPEGs contain plates, faces and house numbers, and
  `_store_jpeg_local` writes client bytes verbatim including any GPS EXIF. Serving them to
  every `viewer` is a larger exposure than anything `device_ref` guarded.
- **Unrepair can orphan a duplicate.** Un-repairing a cluster when another non-repaired one
  sits within `cluster_eps_m` leaves two; the next run updates only the nearest and the loser
  lingers until it ages out of the window.
- **Incremental clients get no tombstone.** Because `_FILTER` has `repaired_at IS NULL`, a
  `?since=` client sees a repaired cluster vanish rather than receive a deletion marker.
  Pre-existing gap in the `?since=` protocol.
- **Tile cache staleness.** `max-age=60` means a repair takes up to a minute to disappear from
  the map; the dashboard should cache-bust tile URLs after a successful repair.
