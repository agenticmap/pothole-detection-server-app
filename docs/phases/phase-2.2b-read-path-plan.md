# Phase 2.2b — Pothole Read Path (As-Built)

> Status: **Implemented** (server side). Companion to [`docs/roadmap.md`](../roadmap.md) §2.6
> and the [`docs/phases/phase-2.2-clustering-plan.md`](./phase-2.2-clustering-plan.md) it follows.
> The Android consumer (roadmap §2.7) is documented in the app repo's
> `docs/phase-2.2b-changes.md`.

## Context

Phase 2.2 (shipped) fills `asset_cluster` server-side via the `ST_ClusterDBSCAN` job, but
**nothing served that data to clients** — the clusters sat in Postgres, invisible, and the
Android app still showed only a device's own local detections. 2.2b closes the loop:
detect → fuse → cluster → **serve back** → map markers, so a user sees potholes that *other*
users have independently confirmed. This is the read endpoint specced in roadmap §2.6.

Scope was kept to the **read endpoint only**; the Android consumer ships alongside in the
app repo. Decisions: full **zoom-aware** behavior; clusters are filtered to the public,
repair-safe subset.

## The endpoint

```
GET /api/v1/potholes?bbox=minLon,minLat,maxLon,maxLat&zoom=<int>&since=<iso8601?>
Header: Accept-Version: v1   (required; no X-Device-Id — this is a public read)

200 →
{
  "items": [
    { "type":"pothole", "id":"clu_..", "lat":.., "lon":.., "severity":.., "confidence":..,
      "observation_count":.., "distinct_devices":.., "last_seen":"<iso>", "source":"crowd" },
    { "type":"cluster", "centroid_lat":.., "centroid_lon":.., "count":.., "max_severity":.. }
  ],
  "generated_at":"<iso>",
  "next_since":"<iso>"
}
```

- **bbox order is `minLon,minLat,maxLon,maxLat`** (x,y — matches `ST_MakeEnvelope`). The route
  validates ranges (lon ∈ [-180,180], lat ∈ [-90,90], min < max) → 400 on malformed input.
- **zoom > 14** → individual `pothole` items (one per `asset_cluster` row).
- **zoom <= 14** → grid-aggregated `cluster` items (count + max severity), so a city-wide
  view isn't flooded with pins.
- `since` is optional (incremental fetch); `next_since` echoes `generated_at`.

## How it works

`app/services/cluster_query_service.py :: query_potholes()` runs one of two queries over the
**public-visibility filter** (shared `_FILTER`):

```sql
asset_type = 'pothole'
AND repaired_at IS NULL
AND distinct_devices >= settings.cluster_min_distinct_devices   -- default 2
AND last_seen >= now() - make_interval(days => settings.cluster_window_days)  -- default 30
AND centroid && ST_MakeEnvelope(minLon,minLat,maxLon,maxLat, 4326)::geography
AND (since IS NULL OR updated_at > since)
```

- **High zoom** — select rows, `ST_X/ST_Y(centroid::geometry)` for lon/lat, `ORDER BY cluster_id`,
  `LIMIT 1000` (logs a warning if the cap truncates the payload).
- **Low zoom** — `GROUP BY ST_SnapToGrid(centroid::geometry, cell)` where
  `cell_deg = 360.0 / 2^zoom` (one tile-width; coarser at lower zoom). Emits `count(*)`,
  `max(severity)`, and an averaged centroid.

`distinct_devices >= 2` is enforced **here** (the write side stores sub-threshold clusters
but they never surface publicly), and `repaired_at IS NULL` drops repaired defects — together
with 2.2's repair-safe identity, a fixed pothole disappears from the map automatically.

## Files

- `app/models/potholes.py` (new) — `PotholeItem`, `ClusterAggItem`, `PotholesResponse`.
- `app/services/cluster_query_service.py` (new) — `query_potholes()` + the two SQL paths.
- `app/routes/potholes.py` (new) — `GET /api/v1/potholes`; bbox/since parsing + validation;
  reuses the `ApiVersion` / `DbPool` dependencies.
- `app/main.py` — registers the router (CORS already permits GET).
- `tests/test_potholes.py` (new) — validation + high/low-zoom + filter tests.

## Verification

`docker compose up -d --wait`, then
`DATABASE_URL="postgresql://pothole:pothole@localhost:5433/pothole_db" pytest -q`
→ **66 passed** (59 prior + 7 new); `ruff check app/ tests/` clean. The new tests exercise
both zoom paths at the HTTP layer plus the repaired/single-device exclusions. Manual:
`curl -H "Accept-Version: v1" ".../api/v1/potholes?bbox=-79.5,43.6,-79.3,43.7&zoom=16"`
(individual) vs `&zoom=12` (aggregated).

## Out of scope / fast-follow
- App-side `since`/`next_since` incremental fetch (server returns it; app omits `since` in v1).
- Auth / rate-limiting on the public read path (roadmap §2.8).
- Tunable: the `cell_deg` grid uses dataset-agnostic tile widths; a per-region projection
  would aggregate more evenly at extreme latitudes.
