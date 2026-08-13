# Road-Test Readiness

> What it takes to run a **real on-road test** of the full loop, and the gaps that
> block one today. Status as of this writing; re-check `app/config.py` and the app
> repo before a drive.

## The loop under test

```
phone (sensor + camera YOLOv8n)  →  POST /api/v1/events + /api/v1/frames
   →  server: fusion (5 min)  →  clustering (15 min)  →  asset_cluster
   →  app: GET /api/v1/potholes?bbox&zoom  →  red markers on the map
```

A real test means driving a route, letting the phone upload, and seeing confirmed
potholes come back as markers on a later fetch.

## ✅ What's ready

- **Ingestion** — `POST /api/v1/events` (batch) and `POST /api/v1/frames` (multipart),
  validated, idempotent, rate-limited.
- **Background jobs** — sensor-fit, fusion (5 min), clustering (15 min) all start in-process
  via the scheduler (`app/fusion/scheduler.py`) when enabled.
- **Read path** — `GET /api/v1/potholes` (public locations) + `/potholes/detail` (staff).
- **Schema** — migrations 001–005 applied (`asset_observation/frame/cluster`, fusion, sensor
  model, detection, auth).
- **On-device camera model** — a real YOLOv8n (`yolov8n_pothole_v1`, mAP50 ≈ 0.56) is bundled
  in the app (`app/src/main/assets/road_gate.tflite`) and verified on-device. The visual
  signal uploaded as `device_probability` is now meaningful (no longer the old `p=0.5` stub).
- **Tests** — full suite green (100 passed) against the local PostGIS on `:5433`.

## 🔴 Blockers (resolve before a drive)

### 1. Solo-driver cluster gate
`cluster_min_distinct_devices = 2` (`app/config.py`) — a cluster only becomes public once
**≥ 2 distinct devices** have detected it. A single phone driving solo will detect real
potholes, but they stay private, so `GET /api/v1/potholes` returns **empty** and no red
markers appear. (The app's own queued detections may still show as local markers, but
nothing server-confirmed.)
- **Fix**: drive the route with **2+ devices**, or for a solo test temporarily set
  `CLUSTER_MIN_DISTINCT_DEVICES=1`. Note clustering also needs **≥ 3 points within 25 m**
  (`cluster_min_points=3`, `cluster_eps_m=25`), so make multiple passes / detections at the
  same spot.

### 2. Network reachability
The phone cannot reach `localhost`. The server must be on a URL the phone can hit over
cellular/Wi-Fi.
- **Fix**: deploy (Fly.io / Railway / Cloud Run, see README) or tunnel (e.g. ngrok), and set
  the app's `POTHOLE_API_BASE_URL` to that URL. Confirm `GET /health` from the phone's
  network first.

### 3. (Optional) Server-side detection is off
`detection_enabled = false` and no Stage-1 model is configured. Fusion falls back to the
device probability via `COALESCE(server_probability, device_probability)`, so the loop works
without it — but you get no server re-scoring or false-positive filtering on this drive.
- **Fix (optional)**: stand up the hybrid detector — see
  [`docs/detection-approach.md`](./detection-approach.md) and
  [`docs/phase-2.3-detection-plan.md`](./phase-2.3-detection-plan.md). Not required for a
  first sensor+device-camera drive.

## 🟡 Expected — know before you drive

- **Latency** — results are not instant. Fusion runs every 5 min, clustering every 15 min, so
  a freshly-driven pothole can take up to ~15 min to surface as a public cluster.
- **30-day window** — clustering only considers members from the last `cluster_window_days = 30`.
- **Auth** — the staff tier uses an **ephemeral** RS256 keypair in dev (`auth_jwt_private_key_pem`
  empty → regenerated each restart). This does **not** affect anonymous device ingestion or the
  public read path; it only matters for `/potholes/detail`.
- **Uncommitted work** — the hybrid detector + these docs are on the `development` branch and
  not yet committed.

## Pre-drive checklist

1. **DB + tests green**: `docker compose up -d --wait`, then
   `DATABASE_URL=postgresql://pothole:pothole@localhost:5433/pothole_db pytest -q` → all green.
2. **Deploy or tunnel** the server; verify `GET /health` returns `{"status":"healthy",...}`
   from the **phone's** network.
3. **Point the app** at that URL (`POTHOLE_API_BASE_URL`) and rebuild/install.
4. **Set the cluster gate** for your party size: leave `CLUSTER_MIN_DISTINCT_DEVICES=2` for a
   2+-device drive, or set `=1` for a solo test.
5. **(Optional)** enable the hybrid server detector if you want server re-scoring on this run.
6. **Smoke-test the round-trip** (below) before relying on the drive.
7. Drive the route (Both mode), wait one clustering cycle (~15 min), then fetch
   `GET /api/v1/potholes?bbox=...&zoom=16` over the route's bbox and confirm items appear.

## Optional smoke test (before trusting a real drive)

Confirms ingestion → fusion → clustering → read path end to end without a phone. With the
server running and the cluster gate set to `1` for a single synthetic device:

1. `GET /health` → healthy.
2. `POST /api/v1/events` with `X-Device-Id` + `Accept-Version: v1` — send several events at
   ~the same lat/lon (≥3 within 25 m) so DBSCAN can form a cluster (see the payload shape in
   the README and the fixtures in `tests/conftest.py` / `tests/test_*_db.py`).
3. `POST /api/v1/frames` (multipart) at the same location with a small JPEG, so fusion has a
   visual pair.
4. Let the schedulers run (or trigger the jobs directly as the DB tests do via
   `run_fusion_job` / `run_cluster_job`).
5. `GET /api/v1/potholes?bbox=<around the point>&zoom=16` → expect ≥1 `pothole` item.
6. Verify the row directly:
   `SELECT cluster_id, distinct_devices, ST_AsText(centroid::geometry) FROM asset_cluster;`

If the cluster appears here, the server side of the road-test loop is good; the remaining
variables on a real drive are reachability and having enough distinct devices/passes.
