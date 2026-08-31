---
updated: 2026-08-30
---

# Runbook — one full round, from collected data to a populated map

Procedure only. Run this to take a database of collected drives and get every
reported pothole onto the operator console, then verify the mobile read path
against the same data.

Every command runs **from the repo root** with `DATABASE_URL` pointing at the
database you mean. Two steps write to derived columns across the whole
observation table (§3) and to the frame store (§7); both are called out.

**What this is not.** It does not train a detector, and it does not turn
server-side detection on. `DETECTION_ENABLED` stays `false` throughout — the
frames in `pothole_db` were already scored by `yolo11s_pothole_v1` via
`scripts/backfill_detection.py`, and the round consumes those scores.

---

## Before anything: three things not to do

1. **Do not run `docker compose up -d` and `uvicorn` together.** Both bind host
   8000 and the second fails with "port is already allocated". Use
   `docker compose up -d postgres` and run uvicorn by hand, or use compose alone.
2. **Do not let the scheduler run during §3.** Fusion ticks every 5 minutes and
   retires frames it has paired; pairing them against half-restored sensor scores
   costs you the whole drain. Stop the server for §3–§4.
3. **Do not point `pytest` at `pothole_db`.** The fixtures TRUNCATE every table.
   `tests/conftest.py` refuses anything but `pothole_test`/`pothole_ci`, but the
   habit is worth keeping.

---

## 1. Stand the stack up

```bash
docker compose up -d postgres
cd dashboard && npm run build && cd ..
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The dashboard build is not optional after any `dashboard/src` change, and its
`prebuild` hook copies MapLibre's worker into `public/maplibre/`. **A 404 on that
worker makes every vector source hang forever with no error and no tile
requests** — a working basemap with no markers looks identical to "no data".

Check four things, not one:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/health
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/dashboard/
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/dashboard/maplibre/maplibre-gl-worker.mjs
curl -s -o /dev/null -w "%{http_code}\n" -H "Range: bytes=0-99" http://127.0.0.1:8000/basemap/toronto.pmtiles
```

Expect `200 / 200 / 200 / 206`. The startup log should read
`Scheduler started (fit=True, fusion=True, clustering=True, detection=False)`.

`Migration NNN changed since it was applied` warnings are cosmetic if the schema
matches; confirm with a column diff against `pothole_test` rather than assuming.

## 2. Provision an operator

```bash
POTHOLE_STAFF_PASSWORD='<pick one>' .venv/Scripts/python.exe scripts/create_staff.py \
  --org org_test --email you@example.com --role admin
```

**`admin`, not `staff`.** The clustering job sets no `org_id`, so every cluster it
produces is unowned and a plain `staff` account gets a 403 that reads like a role
bug. See [`phase-2.6-hardening.md`](../phases/phase-2.6-hardening.md) §6.

## 3. Re-fit and re-score — the step that makes data appear

**Stop the server first.**

This is the unlock. The IsolationForest outlier gate used to be fitted on
`ratio`, `gbar` and `magnitude` — the three features potholes separate on by
14–15× — so it learned "pothole" and reported it as "outlier". It flagged **285 of
286** pothole-classed observations, and the cluster member gate is
`sensor_class = 'pothole' AND sensor_is_outlier IS NOT TRUE`.

`SENSOR_OUTLIER_FEATURES` now defaults to the class-neutral `accel_std,speed_mps`.
Changing it (or `SEVERITY_SCALE`) forces a refit on the next fit tick, because
both live *on* the model — but the existing `sensor_is_outlier` and
`sensor_severity` values are stale until every row is re-scored:

```sql
UPDATE asset_observation SET scored_at = NULL;
```

Then drive `run_fit_job` once and `_score_unscored` in a loop until it returns 0.
Waiting for the scheduler takes ~45 minutes at 500 rows per 5-minute tick.

Verify:

```sql
SELECT count(*) FILTER (WHERE sensor_class='pothole') AS pothole,
       count(*) FILTER (WHERE sensor_class='pothole'
                        AND sensor_is_outlier IS NOT TRUE) AS admitted
FROM asset_observation;
```

On the 2026-08 drives this goes **1 → 166 admitted**. If `admitted` is still near
zero, the refit did not happen — check the log for `Refitting: calibration changed`.

## 4. Re-fuse, then re-cluster

```bash
.venv/Scripts/python.exe scripts/requeue_frames.py --dry-run
.venv/Scripts/python.exe scripts/requeue_frames.py
```

Pairing geometry does not depend on sensor scores, so `mean delta_m` should not
move. What moves is `fused_confidence`, which the member gate reads.

Then run `run_cluster_job` once, or restart the server and wait 15 minutes.
Expect **~25 clusters** where there were 4.

## 5. Check severity actually spreads

```sql
SELECT width_bucket(severity, ARRAY[0,0.25,0.5,0.75]::float8[]) AS tier, count(*)
FROM asset_cluster WHERE repaired_at IS NULL GROUP BY 1 ORDER BY 1;
```

Expect roughly `2 / 12 / 9 / 2`. **All-in-one-tier means the severity scale is
wrong for your data**, not that your roads are uniform. `SEVERITY_SCALE` was 2.0,
which saturates at `magnitude/max(speed,5) >= 0.5` — below the *minimum* of the
observed pothole distribution, so every cluster scored exactly 1.0. It is now
0.25, fitted to p95. Re-measure for a new city:

```sql
SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY magnitude/GREATEST(speed_mps,5.0))
FROM asset_observation WHERE sensor_class='pothole' AND sensor_is_outlier IS NOT TRUE;
```

Set `SEVERITY_SCALE ≈ 1/p95`, then repeat §3 — severity is computed at score time.

## 6. Point the map at the data

```bash
cp dashboard/.env.example dashboard/.env   # then edit
cd dashboard && npm run build
```

**Use the densest cluster cell, not the observation centroid.** A drive is a long
corridor; its centroid usually falls in a gap and its tile is empty.

```sql
SELECT ST_SnapToGrid(centroid::geometry, 0.01), count(*)
FROM asset_cluster GROUP BY 1 ORDER BY 2 DESC LIMIT 1;
```

Keep `VITE_MAP_ZOOM` above 12: individual clickable clusters only exist at z ≥ 13.

## 7. Reconcile the frame store

```bash
.venv/Scripts/python.exe scripts/storage_audit.py                    # report only
.venv/Scripts/python.exe scripts/storage_audit.py --delete-orphans --delete-demo
```

`seed_demo.py` refuses to write to any database but `pothole_test`, but its JPEGs
go to the shared `STORAGE_LOCAL_PATH` — so a demo leaves `demo-dev-*` trees in the
real store. Deletion refuses to run against a scratch database, because against a
TRUNCATEd one every real frame looks like an orphan.

Also drop any test fixture that leaked in. A cluster at exactly
`43.6532, -79.3832` is the conftest coordinate, not a pothole.

## 8. Verify, as an operator

Open `http://127.0.0.1:8000/dashboard/`, sign in, and check:

- markers in more than one severity colour;
- **zoom in / out and the attribution are visible and clickable** — the legend
  used to cover both, and the attribution is a basemap licence requirement;
- toggle **Raw detections → Sensor observations** on and zoom to 15+: individual
  readings appear, outlier-rejected ones as hollow rings. Click one for its full
  record;
- toggle **Camera frames** on: the detector's own view, radius by
  `server_probability`, **hollow where the frame paired with nothing** and so
  reached no cluster. Same grammar as the sensor layer — hollow means it did not
  contribute. Grey means the detector never ran on it;
- click a cluster: members and camera frames with `p=` captions;
- mark one repaired, then reopen it;
- **zero console errors**.

## 9. Verify the mobile read path

```bash
BBOX="-79.55,43.70,-79.30,44.10"
curl -s "http://127.0.0.1:8000/api/v1/potholes?bbox=$BBOX&zoom=16" -H "Accept-Version: v1"
curl -s "http://127.0.0.1:8000/api/v1/potholes?bbox=$BBOX&zoom=16&min_devices=1" -H "Accept-Version: v1"
```

The first returns `[]` on single-vehicle data and **that is correct**. The floor
is `CLUSTER_MIN_DISTINCT_DEVICES` (2), applied *only* here and on
`/potholes/detail` — not on the tile endpoints or `/clusters/stats`, which is why
the dashboard and the app disagree by design.

Before lowering it, measure:

```bash
.venv/Scripts/python.exe scripts/device_gate_eval.py
```

On the 2026-08 drives every cluster spans a median of **2.0 seconds** — one car,
one pass. `CLUSTER_EPS_M` is 25 m and the median speed 13 m/s, so 25 m is 1.9
seconds of travel and "3 detections within 25 m" is one drive-past of one rough
patch. **`CLUSTER_MIN_POINTS` has never required corroboration; the device floor
is the only thing that ever did.** Setting it to 1 does not reveal hidden
confirmed defects — it publishes single-pass artefacts. Use `min_devices=1` as a
query parameter to demonstrate the loop instead, and get a second vehicle before
changing the default.

---

## Known limits of a round run this way

- **No cluster has two devices**, so nothing here proves crowd corroboration.
- **The outlier gate is better, not neutral**: it still flags 31.7% of
  pothole-classed observations against 8–9% of other classes.
- **The 30-day window is invisible in the UI.** Data older than
  `CLUSTER_WINDOW_DAYS` silently disappears from every read surface — both the
  member gate (`received_at`) and the read filter (`last_seen`) — behind an
  honest-looking "No potholes in this view". **Mitigated on this machine**:
  `.env` sets `CLUSTER_WINDOW_DAYS=3650` so the dev database behaves like an
  archive of past drives. Keep the 30-day default in any deployment; the rolling
  window is what lets a resurfaced road stop being reported.
- **Repair is admin-only** until the clustering job assigns `org_id`.
