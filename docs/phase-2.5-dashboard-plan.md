---
updated: 2026-08-17
---

# Phase 2.5 — Operator dashboard

> Continued by [`phase-2.5b-dashboard-design.md`](./phase-2.5b-dashboard-design.md), which
> covers the Organic redesign, the vector basemap, the dock and the stats endpoint — and
> which supersedes this document wherever the two disagree about the basemap.
>
> Status: **Steps 1–3 implemented** — vector tiles, detail panel + repair marking, and the
> browser frontend. Step 4 (live WebSocket push) is **not started** and is arguably not worth
> starting; see "Out of scope / next".
>
> Commits: `e75d15f` groundwork · `461ad04` step 1 · `1086e4a` step 2 · `5ad6d1a` step 3.
> Plus `d232031`, a security fix found along the way that is not part of the dashboard.
>
> Superseded numbering: `enterprise-architecture-plan.md` §4.1 calls this "P2.3", which
> collides with the shipped Phase 2.3 (server detection). It is 2.5 here and in the code.

## Why

Phase 2.4 built a staff auth tier for exactly one reason — to gate the
severity/confidence detail that *is* the municipal product — and then used it on a single
JSON endpoint that no human-facing surface consumed. The operator dashboard
(`enterprise-architecture-plan.md` §3) is the surface the whole enterprise plan aims at, and
it was the largest missing deliverable.

This phase builds it end to end: vector tiles for the map, a detail-panel payload, the first
write path to `asset_cluster` outside the clustering job, and a browser console that an
operator actually uses.

## The loop this completes

```
phone (sensor + camera)  →  POST /events + /frames
   →  fusion (5 min)  →  clustering (15 min)  →  asset_cluster
   →  GET /tiles/clusters/{z}/{x}/{y}.mvt   →  operator sees it on a map
   →  click  →  GET /clusters/{id}          →  members, frames, history
   →  POST /clusters/{id}/repair            →  repaired, audited, off the map
```

Before this phase the chain stopped at `asset_cluster`. `repaired_at` had no write path
anywhere in `app/` — the roadmap assumed an admin would edit it in Supabase Studio, which does
not exist in this deployment.

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

### Step 3 — Browser frontend (`dashboard/`)

Vite 8 + TypeScript + MapLibre GL JS 6.4, **no framework**. ~1,700 lines of TS and ~760 of
CSS across 15 modules. Scope is the core loop only: sign in → map → click a marker → detail
panel with frames → mark repaired. Filters, an inventory list and work orders are not built,
but the shell has a defined slot for each. (Filters and dark mode arrived in Phase 2.5b.)

| Module | Responsibility |
|---|---|
| `src/main.ts` | Bootstrap; login screen ↔ app shell; WebGL2 support gate |
| `src/auth.ts` | Token lifecycle, single-flight refresh, role claim, error-envelope normalisation |
| `src/api.ts` | Authenticated fetch wrapper; 401 → refresh → retry once |
| `src/types.ts` | Hand-written mirrors of `app/models/clusters.py` |
| `src/dom.ts` | `el()` / `field()` / `plural()` — textContent only, no `innerHTML` anywhere |
| `src/severity.ts` | The ordinal ramp: colour + radius + label, one source of truth |
| `src/shell.ts` | Top bar, asset-type selector, module rail, legend, URL state |
| `src/map/map.ts` | MapLibre init, source, click handling, optimistic repaint |
| `src/map/tile-auth.ts` | `transformRequest` + 401 recovery |
| `src/map/layers.ts` | The two style layers and their paint expressions |
| `src/map/basemap.ts` | Basemap style. **Superseded in 2.5b:** now self-hosted Protomaps PMTiles, not raster OSM |
| `src/panel/panel.ts` | Detail panel; owns the per-open `AbortController` |
| `src/panel/frames.ts` | Blob-URL image loading with a client-side concurrency cap |
| `src/tokens.css` | Semantic design tokens (surfaces, severity ramp, spacing, type) |
| `src/styles.css` | Shell layout and components |

Server-side change is limited to `app/main.py` (a guarded `StaticFiles` mount at
`/dashboard`) and `app/config.py` (`dashboard_dist_path`). No endpoint was added or altered
for the frontend — a useful signal that the step-1/2 contracts were right.

## The UI/UX design

Researched against the products this competes with: **vialytics** (a UX Design Award winner in
this exact category), **Cartegraph + Esri ArcGIS** pavement dashboards, and Esri's
cartographic guidance. They converge on a map-first console driving
**Collect → Assess → Prioritise → Act → Track**.

### Layout — three-zone map-first console

```
┌──────────────────────────────────────────────────────────────────┐
│  ◆ RoadWatch   [ Potholes ▾ ]                usr_ops · Sign out  │ 56px
├────┬────────────────────────────────────────────┬────────────────┤
│ M  │                                            │  clu_a4f…   ×  │
│ I  │                MAP (fills)                 │  Open · Mod 2.2│
│ W  │                                            │  Corroboration │
│ R  │                                            │  [frames]      │
│ A  │      ┌────────┐                            │  [observations]│
│    │      │ legend │                            │  [Mark fixed]  │
└────┴──────┴────────┴────────────────────────────┴────────────────┘
  56px rail                                         400px panel
```

- **Module rail** — Map · Inventory · Work orders · Reports · Admin. Only Map is live; the
  rest render **disabled with tooltips**. This is the deliberate part: the competitor set says
  work orders and trend-over-time come next, and reserving the slots now means adding them is
  additive rather than a re-layout.
- **Asset-type selector** in the top bar, not a hardcoded "Potholes". The tile and detail
  endpoints already take `asset_type`, so Phase 5's multi-asset expansion is a registry entry
  plus an icon. Sign/streetlight/crosswalk options are present and disabled.
- **Detail panel** 400px, slides in on selection; becomes a bottom sheet under 1024px.
- **Legend always visible**, not behind a control. A ramp an operator has to go looking for is
  a ramp they will misread.

### Severity encoding — the most consequential visual decision

**Severity is ordinal, so it gets a sequential ramp, not red-amber-green.** RAG is the
category cliché and it is the worst case for the commonest colour-vision deficiency. The ramp
is ColorBrewer `YlOrRd`-family:

| Tier | Token | Hex | Relative luminance |
|---|---|---|---|
| Low (≥ 0) | `--severity-1` | `#fed976` | 0.720 |
| Moderate (≥ 1.5) | `--severity-2` | `#feb24c` | 0.534 |
| High (≥ 3) | `--severity-3` | `#fc4e2a` | 0.263 |
| Severe (≥ 5) | `--severity-4` | `#b10026` | 0.095 |

Two properties were **measured in the browser, not assumed**: it contains no green at all (so
red/green confusion cannot arise), and it is strictly monotonic in luminance — which is what
makes it survive CVD simulation *and* the greyscale printout a works crew actually carries.

**Colour is never the only channel.** Marker radius also scales with severity (5→11px), and
the panel prints the tier label *and* the number ("Moderate · 2.2"). **Repaired** sits
deliberately off the ramp — slate, half opacity — because a repaired defect is a different
kind of thing, not a low severity. Markers carry a 1.5px white halo so they stay legible over
both pale roads and dark parkland.

Tier boundaries are a first cut against `asset_cluster.severity` and **should be recalibrated
against real drive data before a pilot**.

> **Corrected in Phase 2.5b.** This phase set the floors at 0 / 1.5 / 3 / 5 on the belief that
> severity was an unbounded IRI-style figure. It is not: `app/sensor_model/features.py` clamps it
> to **[0, 1]**, so three of the four ramp colours were unreachable and every real cluster painted
> in the palest tier. The floors are now 0 / 0.25 / 0.5 / 0.75.

### Tokens and type

Everything is a semantic CSS custom property in `src/tokens.css` — `--color-surface`,
`--severity-3`, `--space-4` — never a raw hex in a component. Dark mode and per-municipality
white-labelling are then a redefinition of that one block. Light-first, because basemaps are
light, municipal offices are bright, and these views get printed.

Neutral slate chrome so the map carries the colour; one blue (`#2563eb`) for interactive;
red reserved for destructive and **excluded from the severity ramp** — if the ramp's red also
meant "error", neither signal would mean anything.

Type is **Inter** with `font-variant-numeric: tabular-nums` on every metric so figures don't
jitter between rows, and a monospace face **only** for identifiers and coordinates.

> The `ui-ux-pro-max` design tool was consulted and its palette/density guidance used. Its
> *style* recommendation — "Exaggerated Minimalism", stated fit "fashion, architecture,
> portfolios, luxury brands, editorial", with `clamp(3rem, 10vw, 12rem)` display type — was
> **discarded** as wrong for a data-dense operator console. Recorded so the divergence reads
> as a decision rather than an oversight.

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

### Tile auth: `transformRequest` rather than `addProtocol`

> **Corrected in Phase 2.5b.** This section originally concluded that `addProtocol` "does not
> work for vector tiles here" because MapLibre 6 loads them in a worker and "a protocol
> registered on the main thread is never consulted." **That diagnosis was wrong**, and the
> symptom had a different cause: MapLibre's worker script was 404ing, so *no* vector source
> could load. See `dashboard/src/map/worker.ts`. MapLibre's own typings state that workers
> delegate protocols they do not recognise back to the main thread, and PMTiles — which the
> vector basemap now depends on — is their canonical `addProtocol` example.

`addProtocol` was tried first, on the reasoning that it owns the fetch and can therefore see a
401 and retry inline. Tiles sat in `state: 'loading'` indefinitely, with **no error, no console
warning, and zero network requests**. Adding MapLibre's own public demo vector source at runtime
reproduced it exactly — which was read at the time as proof that the worker ignored the
protocol, when in fact it was the tell that *nothing vector-based worked at all*.

`transformRequest` remains the right tool for the authenticated cluster tiles regardless, for
the reasons below: it keeps the access token on the main thread, where the refresh logic lives.
`addProtocol` is now also in use, for `pmtiles://` — and it works.

`transformRequest` works because it runs on the **main thread** while the tile URL is being
built, and its headers travel with the request into the worker. Two rules make it safe:

1. **Return a plain object synchronously for anything that is not our API.** The hook applies
   to *every* resource type including the raster basemap, and an `async` function returns a
   Promise on every call — putting the basemap onto the awaited code path, which had two
   abort-related bugs fixed as recently as MapLibre 6.1. Only `/api/v1/` takes the async
   branch, and only when no cached token is in hand.
2. **Match on the `/api/v1/` pathname, not the origin.** In dev the API origin *is* the
   dashboard origin (Vite proxy), so an origin test would attach the bearer to `index.html`
   and every static asset.

`transformRequest` cannot see responses, and **an errored tile never retries itself** — so a
401 would leave a permanently blank map even after a successful refresh. Recovery is
explicit: `map.on('error')` → if `AJAXError.status === 401` → `refreshNow()` → `setTiles()`
with a bumped version to force a refetch.

### Tiles are requested with `include_repaired=true`

Not an oversight. With the default filter, marking a cluster repaired makes its marker vanish
from under the operator's own open panel — leaving a detail panel for something no longer on
the map and **no route back to un-repair it**, even though the API supports exactly that.
Repaired clusters are fetched and styled distinctly instead. `min_devices=0` likewise, because
the single-device triage queue is what an operator wants and what the public path hides.

### Optimistic repaint via feature state, not a tile reload

After a repair the marker repaints immediately from `setFeatureState`, using
`promoteId: {clusters: 'cluster_id'}` (needed because `cluster_id` is TEXT, so MVT carries no
numeric feature id). Feature state is **discarded by `setTiles`**, so the two mechanisms are
alternatives, not a pair — the optimistic path is used for repair, and `setTiles` is reserved
for the asset-type switch and 401 recovery.

Worth knowing: `Cache-Control: max-age=60` on tiles means MapLibre re-fetches every visible
tile every 60 seconds anyway, so the map is self-healing regardless; the optimistic update is
about immediacy, not correctness.

### Source `maxzoom` is 14 — deliberately, and it is a trap in both directions

MapLibre's default is 22, which would request a fresh tile from PostGIS at *every* integer
zoom up to 22. At 14 it fetches real tiles to z14 and overzooms (client-side slices the
parent) above, halving request count at street zooms while keeping feature properties intact
so clicks still work.

It must stay **above** `tile_aggregate_max_zoom` (12). Setting it to 12 would overzoom the
*aggregated* tiles forever and individual clusters would never appear at any zoom.

### Every numeric `get` is wrapped in `coalesce`

`ST_AsMVT` **omits NULL attributes from a tile entirely** — the key is simply absent from the
feature — and `asset_cluster.severity` is nullable. A bare `['get','severity']` therefore
feeds null into a `step` expression and MapLibre raises a render-time expression error, which
surfaces as console spam and miscoloured markers rather than an obvious failure. The sentinel
is `-1` so a missing value lands below the first tier and paints as "unrated", not "low".

### Two style layers over one source

The source's feature schema changes with zoom, so the layers are separated by filter:
`['has','point_count']` for aggregated bins, `['!', ['has','point_count']]` for individual
clusters. MapLibre filters are per-feature, so a mixed source is fine.

Both schemas can be on screen briefly — MapLibre renders a scaled parent tile until the child
loads, so crossing z12→z13 shows aggregate bubbles at z13 for a moment. The click handler
therefore binds to the individual layer *and* still guards on `cluster_id` being a string.

Aggregate counts are encoded by **radius, not a numeric label**, because the style declares no
`glyphs` URL — a `text-field` without one is a style-validation failure and the numbers
silently never render. Adding labels later means self-hosting one font range.

### Tokens are memory-only; refresh is single-flight

The access token lives in a module variable and the refresh token **is not persisted at all**.
`sessionStorage` was rejected because it is copied into a duplicated tab (Ctrl-click), where
both tabs would rotate the same rotating credential and a single-flight guard cannot reach.
Memory-only also caps what an XSS can steal at a ≤30-minute access token rather than a 30-day
refresh token. The cost is re-login after a page reload, acceptable for a single-sitting
triage tool.

Refresh is **single-flight** — one shared in-flight promise all callers await — because
`app/auth/service.py` revokes the presented token *before* issuing the new pair and there is
no reuse detection, so two concurrent refreshes would destroy the session. A caller that loses
the race and finds the token has since changed reuses the winner's token rather than logging
out.

### `Accept-Version: v1` on `/auth/*` only

`login` and `refresh` take the `ApiVersion` dependency and **400** without the header; the
tile and cluster routes deliberately do not (the dashboard ships with the server and is not
the versioned mobile client). Omitting it yields a 400, not a 401, which is easy to
misdiagnose as a body-validation problem.

### No `innerHTML`, anywhere

`src/dom.ts` builds elements with `textContent` and has **no escape hatch**, on purpose: the
repair note is operator free text up to 2,000 characters that the API echoes back in
`repair_history[].note` alongside `user_email`. Template-string `innerHTML` in a vanilla
implementation is precisely how that becomes stored XSS.

### One `AbortController` per panel open

Clicking marker A then quickly marker B would otherwise let A's detail request and its image
fetches resolve *after* B rendered — producing B's header with A's frames — and the
revoke-on-close cleanup never fires because the panel never closed. Every `open()` aborts the
previous one.

### Frame images: fetch + blob URL, capped at 3 concurrent

`<img src>` **cannot send an `Authorization` header**, so bytes come through `fetch` and become
object URLs, revoked as soon as the image decodes (the response is
`private, max-age=86400, immutable`, so nothing is re-fetched). Concurrency is capped at 3
client-side because the server's image route is guarded by a `Semaphore(6)` shared across
*all* users — firing 12 at once would queue behind whoever else has a panel open.

### URL-driven state

`#/asset=pothole&z=16.00&lat=…&lon=…&cluster=clu_x`. An operator can send a colleague a link
to the exact defect, and the panel is bookmarkable. The hash needs no server support, which is
why there is no SPA catch-all route to get wrong — a catch-all that failed to exclude `/api`
would turn every API 404 into `200 text/html` and make the fetch wrapper try to parse
`<!doctype html>`.

**Known limitation:** state is read on load only; there is no `hashchange` listener, so
browser back/forward does not restore a previous view within a live session.

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
| `dashboard_dist_path` | `dashboard/dist` | built bundle; relative resolves against the repo root |

Detail-panel bounds are module constants in `cluster_detail_service.py`: `MAX_MEMBERS` 200,
`MAX_FRAMES` 12, `MAX_REPAIR_HISTORY` 20. Frontend constants live in `src/map/map.ts`
(`AGGREGATE_MAX_ZOOM` 12, `SOURCE_MAX_ZOOM` 14) and `src/severity.ts` (tier boundaries).

Frontend build-time env (all optional, `VITE_`-prefixed): `VITE_MAP_LAT` / `VITE_MAP_LON` /
`VITE_MAP_ZOOM` for the initial view — there is **no extent endpoint**, so the starting view
is configured rather than discovered — and `VITE_API_TARGET` for the dev proxy.

### `AUTH_JWT_PRIVATE_KEY_PEM` is now effectively required

Left empty, `app/auth/keys.py` mints an **ephemeral keypair per process**. Two consequences,
the second of which is nasty: every `--reload` invalidates all tokens, and because the
Dockerfile runs `uvicorn --workers 2` and `get_key_material` is `@lru_cache`d *per process*,
each worker signs with a **different key** — a token minted by worker 1 fails validation on
worker 2, producing intermittent 401s that look exactly like a client bug.

`.env.example` now documents it with a generation command. Generate with
`openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048` (PKCS#8, matching what
`keys.py` produces itself); `_normalize_pem` accepts literal `\n` escapes so it fits on one
`.env` line. Outside `ENV=development` the server already refuses to start without it.

## Running it

```bash
# 1. Database
docker compose up -d

# 2. Provision an operator (CLI, not an endpoint — there is no self-signup)
POTHOLE_STAFF_PASSWORD='…' python scripts/create_staff.py \
    --org org_cambridge --name "City of Cambridge" \
    --email jane@cambridge.gov --role staff

# 3. Build the dashboard
cd dashboard && npm install && npm run build && cd ..

# 4. Serve. ENV must stay `development` or migrations never run.
uvicorn app.main:app --reload --port 8000
#    → http://127.0.0.1:8000/dashboard/
```

For frontend work, run Vite instead and get HMR — both dev and prod are then same-origin, so
there is no `OPTIONS` preflight per tile URL:

```bash
cd dashboard && VITE_API_TARGET=http://127.0.0.1:8000 npm run dev
#    → http://localhost:5173/dashboard/
```

Roles: `viewer` sees everything but no repair control; `staff` and `admin` can mark repairs.
The provisioning CLI supersedes the manual `INSERT` recipe in `phase-2.4-auth-plan.md`.

**The bundle is not in the Docker image.** The Dockerfile copies only `app/` and
`migrations/`, so a container serves no dashboard — the mount is skipped and logged. Shipping
it needs a `node:22` build stage, a `COPY --from`, and a `.dockerignore` for `node_modules`;
that belongs with deployment (Phase 2.7), not here.

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

### Frontend verification — driven in a real browser

There are no automated frontend tests; step 3 was verified with Playwright against a seeded
`pothole_test` and a provisioned account. What was confirmed:

- **Sign-in**, and a wrong password rendering as `Invalid credentials.` rather than
  `[object Object]` — the API has *three* error-envelope shapes (`{"detail": str}`,
  `{"detail": [...]}` for 422, `{"error":…, "detail":…}` for 500).
- **Deep links** restoring map centre/zoom (scale bar 50 m at z16) and reopening the panel.
- **Detail panel** — badges, tabular fields, per-cluster `device_ref` labels (three
  observations from one device correctly all show "A").
- **Frame images** decoding at 640×480 from `blob:` URLs — the `<img src>` auth workaround.
- **Repair** writing through to `asset_cluster.repaired_at` **and** a `repair_log` row with
  `user_id`/`org_id`, flipping the badge to "Repaired" and the button to "Reopen defect", and
  appending the history entry.
- **Role gating** — a `viewer` sees full detail but no repair control, with an explanation.
- **Severity ramp** measured in-page as monotonic in luminance.

**What could NOT be verified: marker rendering.** MapLibre's Web Worker stalls in this
headless environment — it accepts messages and never answers (16 pending actor callbacks), and
MapLibre's *own* public demo vector source hangs identically, so it is the environment and not
this code. No vector tiles paint there.

The tiles themselves were verified instead, over HTTP and decoded with `tests/mvt.py`:

| Zoom | Features | Attributes |
|---|---|---|
| z15 | 8 individual | `cluster_id`, `severity`, `confidence`, `distinct_devices`, `repaired`, … |
| z12 | 20 aggregated | `point_count`, `max_severity` |

which is exactly what the two style layers filter on. **Open it in a normal browser to confirm
the markers draw** — that is the one gap in this phase's verification.

## Bugs worth remembering

Four, all found by testing rather than review:

1. **Shared FastAPI `Path()` instance.** `x` and `y` in the tile routes initially shared one
   `Path()` object. FastAPI sets the alias on the object it is handed, so the second parameter
   overwrote the first's binding and both read the same path segment — the endpoint returned a
   valid, empty, **wrong** tile with a 200 rather than erroring.
2. **`Number(null)` is `0`, not `NaN`.** The URL-state parser treated an absent `lat`/`lon`/`z`
   as a valid view, dropping every first-time visitor at Null Island (0, 0) at zoom 0. Now
   parsed through a helper that distinguishes absent from zero.
3. **MapLibre applies `.maplibregl-map` to the container you hand it**, not to a child it
   creates. A `position: absolute; inset: 0` rule intended for an inner element therefore
   ripped the map out of the flex layout and floated it over the entire shell, hiding the top
   bar and rail.
4. **`LIMIT` without `ORDER BY`** in `_CLUSTER_TILE_SQL` (a step-1 bug found during the step-3
   review). Above `tile_max_features`, which clusters survived truncation was whatever the
   planner returned, so markers popped in and out between pans at the same zoom. Now ordered
   worst-first, so a truncated tile keeps the clusters an operator most needs to see.

## Measured

| | |
|---|---|
| z14 tile (individual), 20k clusters | p50 15 ms / p95 27 ms |
| z10 tile (aggregated), 20k clusters | p50 69 ms / p95 81 ms |
| §3.4 budget | p50 < 80 ms / p95 < 250 ms |
| detail queries, 5k clusters / 60k links | 0.24 ms, all index scans |
| real frame JPEGs on disk | 12–62 KB, median 39 KB |
| dashboard bundle | 251.7 KB gzip JS + 13.2 KB gzip CSS |
| §3.4 bundle budget | < 800 KB gzip |
| severity ramp luminance | 0.720 → 0.534 → 0.263 → 0.095 (monotonic) |

MapLibre is ~250 KB of that bundle; the application code is a few KB. The budget did **not**
drive the no-framework choice — React would have fit comfortably. The reason is that the core
loop is one map, one panel and one form, where a framework buys about forty lines of state
plumbing; the cost accepted in exchange is the async-race discipline (the `AbortController`
per panel open, the single-flight refresh) that a framework would have handled.

## Out of scope / next

- **Step 4 — live updates.** `NOTIFY` on `asset_cluster` → `LISTEN` → WebSocket. Note the data
  only changes every `clustering_interval_minutes` (15), so a sub-2-second push target is
  largely theatre; and because tiles carry `max-age=60`, MapLibre already re-fetches every
  visible tile each minute. `updated_at` polling — the index exists — is the honest answer,
  and arguably nothing needs doing here at all.
- **Frontend automated tests.** None exist. The core loop was verified by driving a browser,
  which is not a regression net. A Playwright suite covering login → panel → repair would be
  the highest-value addition.
- **Filters, inventory list, work orders, reports.** Deferred by scope. The rail has slots.
- **Basemap.** ~~Raster OSM tiles are dev-only under OSM's tile usage policy.~~
  **Resolved in Phase 2.5b:** replaced with a self-hosted Protomaps PMTiles archive served
  from `/basemap`. What remains outstanding is that glyphs and sprites still load from
  `protomaps.github.io`; see [`phase-2.5b-dashboard-design.md`](./phase-2.5b-dashboard-design.md) §4.
- **Shipping the bundle in Docker** — needs a `node:22` build stage (see "Running it").
- **Browser support.** MapLibre 6 requires **WebGL2**; the app shows a plain message rather
  than a blank screen if it is missing, which is realistic over RDP or in a VM.
- ~~**`POST /api/v1/auth/logout`.** Does not exist.~~ **Shipped.** Revokes the presented
  refresh token and answers 204 unconditionally, so it cannot be used to probe whether a token
  is real. Scoped to the one token, so other sessions survive. The access token stays valid
  until it expires (30 min) — revoking that needs a denylist, which this gap did not justify.
- **Back/forward navigation** does not restore state (no `hashchange` listener).
- ~~**Per-municipality scoping.**~~ **Writes are scoped** (`migrations/009_org_scoping.sql`).
  `asset_cluster.org_id` was added **without a backfill**: matching org → allowed, mismatched →
  403 even for an admin, `NULL` (unowned) → admin only. Enforced in `repair_service` inside the
  same transaction as the row's `FOR UPDATE`, so ownership cannot change between the check and
  the write. **Reads are still global.**
  - **Live limitation:** the clustering job sets no `org_id`, so everything the pipeline
    produces is unowned and therefore admin-only to repair — the normal `staff` operator
    workflow. Pinned by `test_job_created_cluster_is_admin_only`. Assigning an owner in the job
    needs a device→org mapping that does not exist yet; the alternative (backfilling one default
    org) was rejected as asserting ownership nobody established.
- ~~**EXIF / PII in frames.**~~ **Metadata is stripped on ingest**
  (`app/services/jpeg_metadata.py`), before the write, so the archive never holds it and no
  backup can. Lossless segment removal rather than an image-library re-encode: APP1 (EXIF/XMP,
  where the GPS IFD lives), APP3–APP13, APP15 and COM are dropped; APP0/APP2/APP14 are kept so
  colour rendering is unaffected; the entropy-coded scan is copied byte for byte.
  - **Still open:** plates and faces are in the *pixels*. Redacting those is a detection model,
    not a parser.
- **Unrepair can orphan a duplicate.** Un-repairing a cluster when another non-repaired one
  sits within `cluster_eps_m` leaves two; the next run updates only the nearest and the loser
  lingers until it ages out of the window.
- **Incremental clients get no tombstone.** Because `_FILTER` has `repaired_at IS NULL`, a
  `?since=` client sees a repaired cluster vanish rather than receive a deletion marker.
  Pre-existing gap in the `?since=` protocol.
- **Tile cache staleness.** `max-age=60` means a repair takes up to a minute to disappear from
  the map; the dashboard should cache-bust tile URLs after a successful repair.
