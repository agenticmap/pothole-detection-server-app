---
updated: 2026-08-23
---

# Road-Test Readiness

> What it takes to run a **real on-road test** of the full loop, and the gaps that block one.
> Last audited **2026-08-12**; re-check `app/config.py` and the app repo before a drive.
> App side: see the app repo's `docs/road-test-hardening-changes.md`.

## The loop under test

```
phone (sensor + camera YOLOv8n)  →  POST /api/v1/events + /api/v1/frames
   →  server: fusion (5 min)  →  clustering (15 min)  →  asset_cluster
   →  app: GET /api/v1/potholes?bbox&zoom  →  red markers on the map
```

A real test means driving a route, letting the phone upload, and seeing confirmed
potholes come back as markers on a later fetch.

**Operating mode for the first drive: offline-first.** The phone buffers everything in Room
during the drive (uploads are Wi-Fi-gated by default) and drains against this server when it
rejoins the home network. This is a supported mode, not a degraded one — see the app's hard
invariant #2. It also means the whole drive's data hits the server in one burst, which is what
made the rate limits below a blocker.

---

## 🔴 Blockers found and fixed (2026-08-12)

The prior revision of this document listed the solo-driver gate and network reachability. It did
**not** know about the following, which were found by auditing the wire format against the real
Android client rather than against the test suite.

### 1. Every camera frame upload returned 422 — *fixed*

`app/routes/frames.py` declared:

```python
metadata: UploadFile = File(...)
```

Starlette only constructs an `UploadFile` when the multipart part carries a
`Content-Disposition` **filename**. The Android client uses OkHttp:

```java
.addFormDataPart("metadata", null, RequestBody.create(metadata.toString(), JSON))
```

and OkHttp omits `; filename=` entirely when the filename is `null`. So the part arrived as a
plain `str`, FastAPI rejected it, and **every real frame upload failed with 422**. The app treats
that as a transport failure and retries forever, so the drive would have ended with zero frames
on the server.

**Why the suite was blind to it:** every case in `tests/test_frames.py` posts via httpx
`files={...}`, which *always* emits a filename. The tests were structurally incapable of
reproducing the client's bytes.

**Fix:** accept `Annotated[Union[UploadFile, str], File()]` and branch on `isinstance(metadata,
str)` — note the check is for `str`, because the form parser yields *Starlette's* `UploadFile`,
which is not an instance of FastAPI's subclass. `TestFramesAndroidWireFormat` now builds the
multipart body by hand and asserts both shapes.

Also hoisted `import json` to module scope: it sat *inside* the `try` whose `except` clause
referenced `json.JSONDecodeError`, so any earlier failure raised `UnboundLocalError` instead of
the intended 400.

### 2. Rate limits sized for a demo — *fixed*

`rate_limit_events_per_hour = 100` / `rate_limit_frames_per_hour = 100`. A drive's worth of
buffered data drains in one burst far above that → 429 → the client retries the same rows
indefinitely. Camera frames alone are ~900/hour at the app's `CONFIRM_EVENT_MIN_GAP_MS` floor.

**Fix:** defaults raised to **5000/hour** in `app/config.py`, with `.env` / `.env.example` synced.

### 3. One bad row wedged a device's queue forever — *fixed*

`accel_max_g` was bounded to `[-50, 50]`. Despite the name, **the client sends m/s², not g** —
`PotholeRefinementService` computes it as `linAccMax.getNorm()` over raw
`TYPE_LINEAR_ACCELERATION` values. Any hard pothole strike exceeds 50 m/s² (≈ 5 g).

Because Pydantic validates the whole `list[EventPayload]`, **one out-of-range value 422s the
entire batch of 100**, and the client re-selects the same oldest-100 rows on every retry
(`EventDao:30`) — so a single hard strike blocked that device's upload queue permanently.

> Note: the README claims "individual events can be rejected without failing the entire batch."
> That is true only for *database* errors (`event_service.py`), **not** for schema violations.

**Fix:** bounds widened to sanity ceilings (`accel_max_g` ±200, `accel_std` ≤ 200, `magnitude`
and `gbar_in_max` ≤ 2000) with the unit confusion documented in `app/models/__init__.py`. The
field name stays — it is frozen by the v1 wire contract; renaming is a v2 concern. The app also
clamps outbound values as a belt-and-braces guard.

### 4. Solo-driver cluster gate — *unchanged, configure it*

`cluster_min_distinct_devices = 2` — a cluster only goes public once **≥ 2 distinct devices**
have detected it (enforced in `cluster_query_service.py`). A single phone driving solo will
detect real potholes, but they stay private and `GET /api/v1/potholes` returns **empty**.

- **Fix:** drive with 2+ devices, or set `CLUSTER_MIN_DISTINCT_DEVICES=1` for a solo test (now
  set in the local `.env`). Clustering also needs **≥ 3 points within 25 m**
  (`cluster_min_points=3`, `cluster_eps_m=25`), so **make at least 3 passes** over the same spot.
- The shipped default stays **2**; `tests/test_potholes.py` now pins the threshold explicitly in
  both directions so the filter logic is tested regardless of local configuration.

### 5. Network reachability — *unchanged, your call*

The phone cannot reach `localhost`. For the offline-first drive this is a non-issue during the
drive: nothing uploads on cellular by default, and the queue drains when the phone rejoins the
Wi-Fi the server is on. **If you switch to live upload,** deploy or tunnel (ngrok/Cloudflare) and
repoint `POTHOLE_API_BASE_URL` — a LAN address (`192.168.x.x`) will not work from a moving car.

### 6. No logging configuration — *fixed*

There was no `basicConfig`/`dictConfig` anywhere. Uvicorn configures only its own `uvicorn.*`
loggers, leaving the root logger at WARNING, so **every** `logger.info` in the app was silently
discarded — including `"Events ingested: device=… accepted=N"` and `"Frame ingested: …"`, the
exact lines needed to watch a post-drive sync. `app/main.py` now configures the root logger from
a new `LOG_LEVEL` setting (default INFO).

---

## ✅ What's ready

- **Ingestion** — `POST /api/v1/events` (batch) and `POST /api/v1/frames` (multipart), validated,
  idempotent, rate-limited, and now verified against the **real client's byte format**.
- **GPS quality** — `migrations/006_gps_quality.sql` adds nullable `asset_observation.accuracy_m`,
  paired with the app's Room v4. `speed_accuracy_mps` now receives real values instead of a
  hardcoded `0.0`. Degraded fixes (tunnels, urban canyon) can finally be filtered in analysis.
- **Background jobs** — sensor-fit, fusion (5 min), clustering (15 min) all start in-process.
- **Read path** — `GET /api/v1/potholes` (public) + `/potholes/detail` (staff).
- **Schema** — migrations 001–006 applied.
- **Storage** — frame JPEGs land at `storage/frames/<device_id>/<client_id>.jpg`; the directory
  is created on first write.
- **Tests** — **264 passed** against local PostGIS on `:5433` (105 when this section was first written).

## 🟡 Expected — know before you drive

- **Fusion will not use the ported engine on the first drive.** `registry.py` selects
  `matlab_port_v1` only once an **active `SensorModel`** exists, and `sensor_fit_min_observations
  = 200` gates the first fit. Below that you get `PythonV1Engine`, the heuristic fallback. Both
  are real fusion — but the ported math is only exercised from the second run onward.
- **Latency** — fusion every 5 min, clustering every 15 min, sensor fit every 60 min. Allow
  ~20 min after a sync before judging results.
- **30-day window** — clustering only considers members from the last `cluster_window_days = 30`.
- ~~**`ENV` must stay `development`**~~ **Fixed.** Migrations are tracked in a
  `schema_migrations` ledger and applied at most once, in every environment, under an advisory
  lock. `ENV=production` against a fresh DB now builds the schema instead of creating no tables.
- ~~**`GET /health` never returns 503.**~~ **Fixed.** The unhealthy branch now returns a real
  503 (body shape unchanged), so uptime checks and the compose healthcheck can see a dead DB.
- **Server-side detection is off** (`detection_enabled = False`). Fusion falls back to the device
  probability via `COALESCE(server_probability, device_probability)`, so the loop works without
  it. Not required for a first drive. **Still true after Phase 2.7** — that phase built the
  enablement path (offline eval, ground-truth labels, backfill, ROI crop) but there is still no
  `.onnx` on disk, so the flag stays off. What did change: enabling it with a bad
  `DETECTION_MODEL_PATH` now logs one ERROR at startup and skips the job, instead of throwing
  every two minutes. See
  [`phase-2.7-detection-enablement.md`](./phase-2.7-detection-enablement.md).
- **The on-device probability should not be trusted on collected frames.** Measured over the
  2916 frames already collected: median 0.118, and the three top-scoring frames contain no
  pothole (a manhole cover, a crosswalk marking, and rain on the windshield at night). Judge a
  drive by the sensor path until a server model is in place.
- **Auth uses an ephemeral RS256 keypair in dev** — irrelevant to anonymous ingestion and the
  public read path.
- **`docker-compose.yml` maps host `5433`**, not 5432. The compose service is named `postgres`
  (not `db`). It now also defines an **`app`** service that builds the dashboard and serves the
  API on `8000` with a healthcheck — so `docker compose up` is a complete stack. Running the
  compose app *and* a hand-run `uvicorn` at once fails on the port; they are alternatives.

## Pre-drive checklist

1. **DB + tests green**: `docker compose up -d`, then `pytest -q` → 264 passed.
   Point `DATABASE_URL` at `pothole_test` first — the fixtures TRUNCATE every table.
2. **Confirm the frames fix** with a filename-less part (the shape the client actually sends):
   ```bash
   B=----OkHttpBoundary
   printf -- "--$B\r\nContent-Disposition: form-data; name=\"metadata\"\r\n\r\n{...}\r\n--$B--\r\n" \
     | curl -X POST http://127.0.0.1:8000/api/v1/frames \
         -H "X-Device-Id: t" -H "Accept-Version: v1" \
         -H "Content-Type: multipart/form-data; boundary=$B" --data-binary @-
   ```
   Expect **200**, not 422.
3. **Set the cluster gate** for your party size (`=1` solo, `2` otherwise).
4. **Rate limits** at 5000/h, `LOG_LEVEL=INFO`, `ENV=development`.
5. Drive the route (Both mode), **≥ 3 passes** per pothole.
6. Rejoin the server's Wi-Fi; watch for `Events ingested: …` / `Frame ingested: …` in the log.
7. Wait one clustering cycle (~15 min), then fetch
   `GET /api/v1/potholes?bbox=...&zoom=16` over the route's bbox.

## Post-drive verification

```sql
SELECT count(*) FROM asset_observation;              -- sensor events
SELECT count(*) FROM asset_frame;                    -- > 0 proves the 422 fix
SELECT count(*) FROM fusion_pair;                    -- after ~5 min
SELECT cluster_id, distinct_devices, ST_AsText(centroid::geometry) FROM asset_cluster;
```

Also confirm the JPEGs are on disk under `storage/frames/<device-id>/`, and check
`accuracy_m` is populated (not NULL) on recent observations.

**Verified end-to-end on 2026-08-12** without a phone: a live `POST /api/v1/events` with
`accuracy_m` and `accel_max_g = 78.5` (a value the old bounds rejected) was accepted and
persisted, and a filename-less multipart frame upload inserted a row and wrote its JPEG to disk.
Test rows were removed afterwards so they do not pollute the sensor-model fit.

## Since this was written (2026-08-16)

- **A security bug was found and fixed** that this document's audit missed:
  `X-Device-Id: ../../..` escaped the frame storage root, because neither `device_id` nor
  `client_id` was charset-validated before being interpolated into
  `"{device_id}/{client_id}.jpg"`. Since ingestion is anonymous, that was a live
  unauthenticated write primitive. Fixed at three layers; see
  [`phase-2.5-dashboard-plan.md`](./phase-2.5-dashboard-plan.md). Not a wire break — the client
  sends UUIDs.
- **Tests no longer run against the drive database.** The `db_pool` fixture TRUNCATEs every
  table and used to default to `pothole_db`, so a stray `pytest` would have destroyed
  collected drive data. It now targets `pothole_test` behind an allow-list guard. One-time
  setup: `docker compose exec postgres createdb -U pothole pothole_test`.
- **Repair marking no longer needs raw SQL** — `POST /api/v1/clusters/{id}/repair`, audited.
- Test count is now **264** (was 105 when this document was written, then 206). The counts
  above have been updated in place; per-phase docs keep their own historical figures.

### Phase 2.6 first pass (2026-08-23) — [`phase-2.6-hardening.md`](./phase-2.6-hardening.md)

Relevant to a drive:

- **`GET /api/v1/potholes` no longer 500s** on a bbox wider than 180° of longitude — PostGIS's
  antipodal-edge error reached the client as a server fault on the one public, unauthenticated
  endpoint. Now a 400.
- **Migrations are tracked and run in every environment.** `ENV=production` against a fresh
  database used to create no tables; the `schema_migrations` ledger applies each file at most
  once under an advisory lock. Existing databases re-apply all files once, harmlessly.
- **`GET /health` returns a real 503** when the database is unreachable, so an uptime check can
  finally detect a dead DB. Watch for this if you script a pre-drive check on `curl -f`.
- **`run_fit_job` is single-flight** (`0x504F57`), so two `--workers` cannot race the
  sensor-model activation index mid-drive.
- **`docker compose up -d` now starts the API too**, with the dashboard built into the image and
  a healthcheck. It conflicts with a hand-run `uvicorn` on 8000 — use one or the other.
- **Frame JPEGs are stored without EXIF.** Post-drive analysis that expected to read GPS or
  timestamps *out of the image files* must use `asset_frame` columns instead; the geometry and
  `ts_utc` were always carried in the metadata part, not the EXIF.
- **Repair writes are org-scoped.** Clusters the clustering job creates have no `org_id` and are
  therefore **admin-only** to mark repaired — provision your post-drive operator as `admin`.

## Known gaps (not blocking a drive)

- **The in-memory rate limiter is per-worker.** The Dockerfile runs `--workers 2`, so the
  effective limit doubles and is applied inconsistently. Fine for one device; wrong for a fleet.
- ~~**`run_fit_job` has no advisory lock**~~ **Fixed** — it now takes `0x504F57`, alongside
  fusion `0x504F54`, cluster `0x504F55` and detection `0x504F56`. Migrations take `0x504F53`.
- **No frame retention/GC.** Roadmap §2.8 describes a 90-day GC and a 500 MB/device budget;
  neither is implemented. A long collection campaign will grow `storage/` without bound.
- ~~**No `VOLUME` in the Dockerfile** and no app service in compose~~ **Fixed.** The Dockerfile
  declares `VOLUME /opt/server/storage/frames`, and compose defines the `app` service with the
  frames bind mount plus a read-only `storage/basemap` mount. Before this, the containerised app
  served **no dashboard and no basemap** — both mounts are `is_dir()`-guarded, so they failed
  silently.
- **No TLS anywhere.** Plain HTTP only; fine behind a tunnel, not for a real deployment.
