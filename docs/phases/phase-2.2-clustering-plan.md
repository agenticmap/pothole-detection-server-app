# Phase 2.2 — Crowd Clustering Job (As-Built)

> Status: **Implemented.** Companion to [`docs/roadmap.md`](../roadmap.md) §2.5 and the
> [`docs/phases/phase-2.1-fusion-engine-plan.md`](./phase-2.1-fusion-engine-plan.md) it follows.
> This adds the spatial clustering job that was explicitly deferred from Phase 2.1.

## Context

Phase 2.0 (ingestion) and Phase 2.1 (sensor classifier + sigmoid fusion) were shipped.
The fusion job writes `fusion_pair` rows and per-observation scoring
(`sensor_class`, `sensor_p_pothole`, `sensor_severity`, `sensor_is_outlier`) on a
5-minute cron — but every detection was still its own point. The roadmap (§2.5) calls
for collapsing many independent crowd detections of the same physical pothole into a
single confirmed `asset_cluster`, suppressing single-device noise so the future read
path (§2.6, `GET /api/v1/potholes`) can serve clean map markers.

Framing note: the original MATLAB `clusbearing.m`/`DScan.m` was *feature-space*
GMM/DBSCAN used to bootstrap the unsupervised classifier — that work was already
absorbed into the Phase 2.1 `sensor_model` fit. Phase 2.2 is the *geographic*
clustering the server actually needs. The destination tables (`asset_cluster`,
`observation_cluster_link`) were scaffolded empty back in `001_initial_schema.sql`;
this phase only adds the job that fills them.

## Scope (as built)

- **Clustering job only.** The `GET /api/v1/potholes` read path (§2.6) is left to a
  later 2.2b, keeping this phase tight (as 2.1 was).
- **Members = sensor potholes + fused pairs.** An observation is clusterable if
  `sensor_class = 'pothole'` AND `sensor_is_outlier IS NOT TRUE`, *or* it appears in a
  `fusion_pair` with `fused_confidence >= cluster_member_min_confidence` — within the
  last `cluster_window_days`.

## How it works

A third in-process scheduled job, `run_cluster_job` (`app/fusion/service.py`), runs
every `clustering_interval_minutes` (default 15) alongside the fit and fusion jobs.

1. **Single-flight** via a dedicated advisory lock (`_CLUSTER_LOCK_KEY`, distinct from
   the fusion lock — the two jobs may run concurrently).
2. **Member selection** (`_MEMBERS_CTE`) — one row per qualifying observation, weight =
   `GREATEST(sensor_p_pothole, max fused_confidence)`. Members already explained by a
   nearby *repaired* cluster (older than its `repaired_at`) are excluded (see below).
3. **Gate**: if fewer than `cluster_min_points` members, do nothing.
4. **Cluster assignment**: PostGIS `ST_ClusterDBSCAN` over members projected to Web
   Mercator (EPSG:3857). Eps is supplied in 3857 map units, corrected for Mercator
   scale at the data's mean latitude (`eps_m / cos(lat)`), so the threshold is
   `cluster_eps_m` ground meters. Noise (NULL label) is dropped.
5. **Aggregate per cluster**: confidence-weighted centroid, median severity, mean
   confidence, `observation_count`, `distinct_devices`, `last_seen`.
6. **Repair-safe upsert** (Python, row-by-row): each computed cluster is matched to an
   existing **non-repaired** cluster within `cluster_eps_m` of its centroid. Match →
   `UPDATE` in place (keeps `cluster_id` + `created_at`, bumps `updated_at`). No match →
   `INSERT` a new `clu_<uuid4>` with `source = 'crowd'`. Links in
   `observation_cluster_link` are rebuilt for each touched cluster.
7. A `cluster_run` audit row records params + input/output counts.

### Repair semantics (roadmap §2.5)

An admin sets `repaired_at` (e.g. via Supabase Studio) → the cluster drops from public
results (read-path filter, 2.2b). Because repaired clusters are excluded from matching
*and* their pre-repair members are excluded from re-clustering, a fresh detection in the
same spot after repair forms a **new** cluster (defect returned) while the repaired row
is preserved as an audit record.

`cluster_min_distinct_devices` is **not** enforced at write time — sub-threshold
clusters are still stored; it is the read-path's public-visibility filter
(`distinct_devices >= N AND repaired_at IS NULL`).

## Files

- `app/config.py` — clustering config block.
- `migrations/003_clustering.sql` — `cluster_run` audit table + lookup indexes
  (additive/idempotent; `asset_cluster`/`observation_cluster_link` already existed).
- `app/fusion/service.py` — `run_cluster_job` + SQL constants.
- `app/fusion/scheduler.py` — registers the `cluster` job under `clustering_enabled`.
- `tests/conftest.py` — cluster tables added to the truncate-between-tests set.
- `tests/test_cluster_db.py` — integration tests (form / reject-noise / idempotent /
  repair / fused-pair membership).

## Verification

`docker compose up -d --wait`, then
`DATABASE_URL="postgresql://pothole:pothole@localhost:5433/pothole_db" pytest -q`
(`ruff check app/ tests/` clean). The clustering tests exercise cluster formation,
noise rejection below `min_points`, idempotent re-runs (same `cluster_id`,
`created_at` preserved), repair preservation + new-defect re-clustering, and
fused-pair-only membership.

## Open tuning items (deferred)

- `cluster_eps_m` / `cluster_min_points` — tune on pilot data (25 m / 3 from roadmap).
  The Mercator eps correction uses the dataset mean latitude; for very wide-area data a
  per-point/UTM projection would be more exact.
- ~~`confidence` aggregate is the member mean — revisit vs. max.~~ **Done in Phase 2.2c**
  ([`phase-2.2c-spatiotemporal-fusion.md`](./phase-2.2c-spatiotemporal-fusion.md)): it is now
  a spatiotemporally weighted combination of the members' class distributions, so a recent
  detection on the centroid outweighs a stale one 20 m out. Neither mean nor max.
- Whether `crack`-classed observations should also be eligible members (currently no).
- Stale clusters whose members all age out of the window are left in place (not
  deleted); the read path should filter by `last_seen`/`since`.
- **2.2b**: the `GET /api/v1/potholes?bbox&zoom` read path + mobile `PotholeReadApi`.
