# Pothole Detection — Roadmap

This document tracks the full multi-phase plan for the app. Shipped phases get a one-paragraph summary with a link to their dedicated changes doc. Future phases get the full design here so contributors know what's coming.

| Phase | Status | What |
| --- | --- | --- |
| 1 | ✅ Shipped | Sensor-detection pipeline + Room buffer + WorkManager upload queue |
| 1.5 | ✅ Shipped | UI/UX modernization + camera-detection plumbing |
| 1.6 | 🚧 In progress | Real on-device YOLO + bounding-box overlay + server-fusion plumbing |
| 2 | 📋 Planned | Server backend + sensor↔visual fusion + crowd clustering |
| 3 | 📋 Planned | On-device ML upgrade + labeled-data flywheel |
| 4 | 📋 Planned | Production hardening + public release |

> ### Fusion status — important clarification
>
> **As of today, the sensor pipeline and the camera pipeline run completely independently on the device. They never read each other's results.** Confirmed by reading the code:
> - `Event.visualConfirmed` and `Event.frameClientId` exist as schema columns and serialize over the wire if present, but **nothing ever assigns them**.
> - `PendingFrame.eventClientId` is declared and serialized to upload metadata, but `MapsCameraBinder` never sets it — when a camera frame is persisted, there is no lookup of "is there a recent sensor event nearby?".
> - `PotholeRefinementService.persistEvent` builds its row from sensor data only — it doesn't subscribe to `DetectionStateBus.detections`, doesn't query `pending_frame`, doesn't read camera state.
> - The two pipelines share `DetectionStateBus` only so the **dashboard UI** can render both stat panels, and `MapsActivity.mLastLocation*` for GPS. Neither share is detection-related.
>
> **Fusion lands in two phases, both planned but not implemented:**
> - **Phase 2 (server-side)** — see §2.4 below. Time-window correlation `|Δt| < 3 s` + `Δdist < 25 m`, sigmoid-weighted `fused_confidence`, materialized in a new `fusion_pair` table. Done on the server so the device stays cheap.
> - **Phase 3 (on-device)** — see §3.4 below. Tighter `|Δt| < 1 s` + `Δdist < 5 m` window since same-device clock + GPS allow it. Drops `< 0.4` fused negatives client-side to save upload bandwidth.
>
> The schema slots (`visual_confirmed`, `frame_client_id`, `event_client_id`) ship in Phase 1.5/1.6 specifically so neither future phase needs a Room migration to land the fusion wiring — only logic.

---

## Phase 1 — Sensor pipeline (shipped)

The original v1 update brought the app to modern Android: toolchain migration, leaked API-key rotation, dead-server stripping, then a Room-backed event store and a WorkManager upload queue. The detection algorithm itself (orientation-corrected linear-acceleration spikes with adaptive thresholding against a calibrated baseline) is byte-for-byte the same as the original research code.

Full detail: [`docs/phase-1-changes.md`](./phase-1-changes.md).

## Phase 1.5 — UI/UX + camera plumbing (shipped)

Material 3 DayNight theme, single-screen map-first layout, a unified control-center dashboard with a `[Sensor | Camera | Both]` mode selector, split-view camera on the map, swipe-to-delete history with Undo, and search → Google Maps navigation handoff. Camera pipeline is fully wired (CameraX → throttled analyzer → Room → multipart upload) but gated by a stub classifier — every frame returns `p=0.5` until a real model is dropped in.

Full detail: [`docs/phase-1.5-changes.md`](./phase-1.5-changes.md).

## Phase 1.6 — Real on-device detection (in progress)

Replaces the stub classifier with a YOLOv8-nano pothole detector and surfaces live bounding-box overlays on the camera preview. Extends the upload contract with the on-device `detections` array so the Phase 2 server fusion job has full visual context.

**Code (already landed)**:
- `vision/Detection.kt`, `vision/RoadGateModel.kt` rewrite, `vision/CameraOverlay.kt`
- `PotholeFrameAnalyzer.FrameDecision.detections`
- `DetectionState.detections` + `DetectionStateBus.publishDetections`
- `PendingFrame.detectionsJson` + Room schema v3 + `MIGRATION_2_3`
- `PotholeApi.postFrame` serializes detections
- Dashboard "Boxes" stat card
- Activity layout: `camera_split` wrapped in a `FrameLayout` with overlay `ComposeView` sibling

**Still needed from the user**:
1. Pick a YOLOv8n pothole `.tflite` (Roboflow Universe / Kaggle / self-train via Ultralytics).
2. Drop it at `app/src/main/assets/road_gate.tflite`.
3. Optional: `app/src/main/assets/road_gate_labels.txt` (defaults to `["pothole"]`).
4. Fill in [`docs/model-attribution.md`](./model-attribution.md).
5. Rebuild + install.

After ship: [`docs/phase-1.6-changes.md`](./phase-1.6-changes.md) (to be written).

---

## Phase 2 — Server-side fusion + crowd

Goal: stand up the backend so events + frames stop accumulating forever, run server-side ML on uploaded frames, run the sensor↔visual fusion job whose contract was locked in Phase 1.6, cluster crowd-sourced points into confirmed potholes, and serve them back to the app.

> **Phase 2.0 (ingestion server) is shipped.** **Phase 2.1 — Sensor Classification + Fusion Engine v1 is now implemented** (see [`docs/phase-2.1-fusion-engine-plan.md`](./phase-2.1-fusion-engine-plan.md)). The original 2017 MATLAB accelerometer classifier (k-means++ → GMM → Gaussian-NB) was ported server-side as a self-bootstrapping `sensor_model`; its `P(pothole)` feeds a logit-space sigmoid fusion with the camera's on-device probability (this §2.4). An Isolation-Forest outlier gate and an IRI-style severity output were added. Jobs run in-process via APScheduler. **Phase 2.2 — the crowd clustering job (§2.5 below) is now implemented** (see [`docs/phase-2.2-clustering-plan.md`](./phase-2.2-clustering-plan.md)): a third APScheduler job runs PostGIS `ST_ClusterDBSCAN` over recent high-confidence detections (sensor potholes + fused pairs) and upserts repair-safe `asset_cluster` rows. The read path (§2.6) is the remaining 2.2b item.

### 2.1 Backend choice

**Supabase** (managed Postgres 15+ with PostGIS, auto-REST, auth, storage, Edge Functions). Free tier covers prototyping; pay-as-you-go scales. Schemas are vanilla PostGIS — migration off Supabase later is straightforward. The only Supabase-specific surfaces are Edge Functions (FastAPI rewrite if needed) and Storage (any S3-compatible bucket).

Alternative: FastAPI + Postgres + PostGIS + Celery on Fly.io or Railway, if full control matters more than dashboard ergonomics.

### 2.2 Database schemas

PostGIS-backed. Append-only except `pothole_cluster` (mutable for repair status) and `frame.processed_at` (set by the worker).

```sql
CREATE TABLE event (
    client_id           TEXT PRIMARY KEY,
    device_id           TEXT NOT NULL,
    ts_utc              TIMESTAMPTZ NOT NULL,
    geom                GEOGRAPHY(POINT, 4326) NOT NULL,
    speed_mps           DOUBLE PRECISION,
    bearing_deg         DOUBLE PRECISION,
    accel_max_g         DOUBLE PRECISION,
    accel_std           DOUBLE PRECISION,
    magnitude           DOUBLE PRECISION,
    visual_confirmed    BOOLEAN,
    frame_client_id     TEXT,
    confidence          DOUBLE PRECISION DEFAULT 1.0,
    raw_window_url      TEXT,
    received_at         TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX event_geom_idx ON event USING GIST(geom);

CREATE TABLE frame (
    client_id           TEXT PRIMARY KEY,
    device_id           TEXT NOT NULL,
    event_client_id     TEXT,
    ts_utc              TIMESTAMPTZ NOT NULL,
    geom                GEOGRAPHY(POINT, 4326) NOT NULL,
    device_probability  DOUBLE PRECISION,
    device_model_id     TEXT,
    device_detections   JSONB,
    jpeg_url            TEXT NOT NULL,
    server_probability  DOUBLE PRECISION,
    server_model_id     TEXT,
    server_detections   JSONB,
    received_at         TIMESTAMPTZ DEFAULT now(),
    processed_at        TIMESTAMPTZ
);
CREATE INDEX frame_geom_idx ON frame USING GIST(geom);
CREATE INDEX frame_unprocessed_idx ON frame (received_at)
    WHERE processed_at IS NULL;

CREATE TABLE pothole_cluster (
    cluster_id          TEXT PRIMARY KEY,
    centroid            GEOGRAPHY(POINT, 4326) NOT NULL,
    severity            DOUBLE PRECISION,
    confidence          DOUBLE PRECISION,
    event_count         INT NOT NULL DEFAULT 0,
    distinct_devices    INT NOT NULL DEFAULT 0,
    last_seen           TIMESTAMPTZ,
    source              TEXT CHECK (source IN ('crowd','verified','ml')),
    repaired_at         TIMESTAMPTZ
);

CREATE TABLE event_cluster_link (
    cluster_id          TEXT REFERENCES pothole_cluster(cluster_id) ON DELETE CASCADE,
    member_id           TEXT NOT NULL,
    kind                TEXT CHECK (kind IN ('event','frame')),
    fused_confidence    DOUBLE PRECISION,
    PRIMARY KEY (cluster_id, member_id, kind)
);

CREATE TABLE fusion_pair (
    event_client_id     TEXT REFERENCES event(client_id) ON DELETE CASCADE,
    frame_client_id     TEXT REFERENCES frame(client_id) ON DELETE CASCADE,
    fused_confidence    DOUBLE PRECISION,
    delta_ms            BIGINT,
    delta_m             DOUBLE PRECISION,
    PRIMARY KEY (event_client_id, frame_client_id)
);
```

Row-level security: events + frames writable only by the owning `device_id`. Reads on `pothole_cluster` are public; raw `event` / `frame` rows restricted to owner + admin.

### 2.3 Server-side detection model

A bigger YOLO variant runs as a worker downstream of frame uploads.

- **Model**: YOLOv8-small or -medium fine-tuned on the same dataset as the on-device model (consistency for fusion math).
- **Hosting**: Supabase Edge Function calling out to a separate inference service (Modal / Replicate / a tiny Triton instance) — Edge Functions don't have GPUs.
- **Pipeline**: poll `frame WHERE processed_at IS NULL`, download JPEG, optionally crop to the union of `device_detections` bboxes + 20% margin, run inference, write `server_*` columns.
- **Disagreement logging**: `abs(device_probability - server_probability) > 0.3` → log to `model_disagreement` for Phase 3 review.

### 2.4 Fusion job

Cron every 5 minutes. Implements the contract from Phase 1.6:

For every uploaded `frame` row with `ts T_f` and any nearby `event` row with `ts T_e` such that `|T_f − T_e| < 3000 ms` AND `haversine(loc_f, loc_e) < 25 m`:

```
fused_confidence = sigmoid(w_s · z(magnitude) + w_v · visual_confidence)
```

with `w_s = w_v = 0.5` to start; refined offline against labeled data.

### 2.5 Clustering job

Cron every 15 minutes. PostGIS `ST_ClusterDBSCAN(geom, eps_meters := 25, minpoints := 3)` over the last 30 days of high-confidence members. Promotes to `pothole_cluster`:
- `centroid` = weighted mean of member geoms by their `fused_confidence`
- `severity` = median of contributing events' magnitudes
- `distinct_devices` = `COUNT(DISTINCT device_id)`
- Only clusters with `distinct_devices >= 2` are public (suppresses single-user noise)

**Repair handling**: admin sets `repaired_at = now()` via Supabase Studio → cluster drops from public results. New detections in the same spot after repair create a new cluster (defect returned), preserving audit trail.

### 2.6 REST contract — the read path

```
GET /api/v1/potholes?bbox=lat1,lon1,lat2,lon2&zoom=<int>&since=<iso8601>
Headers: Accept-Version: v1
Response 200:
{
  "items": [
    { "type":"pothole", "id":"phc_...", "lat":..., "lon":...,
      "severity":..., "confidence":..., "event_count":...,
      "last_seen":"...", "source":"crowd" },
    { "type":"cluster", "centroid_lat":..., "centroid_lon":...,
      "count":..., "max_severity":... }
  ],
  "generated_at": "...",
  "next_since": "..."
}
```

`zoom <= 14` returns `type:"cluster"` aggregates; `zoom > 14` returns individual `type:"pothole"` items. Repaired clusters filtered out.

### 2.7 App-side Phase 2 changes (~1 day)

- New `network/PotholeReadApi.kt` exposing `getPotholes(bbox, zoom, since): List<MapItem>`.
- `MapsActivity.onCameraIdleListener` triggers a 300 ms-debounced fetch.
- Render `Pothole` items as red markers, `Cluster` items as numeric-badge custom-bitmap markers.
- Phase 1.5 own-events orange markers stay (the user's own queued detections remain visible).

### 2.8 Rate limiting + abuse

- Per-device: 100 events/hour + 100 frames/hour, enforced via a `rate_limit` table or RLS function. 429 on excess.
- Per-IP: Cloudflare in front of Supabase, default rule "100 requests/minute per IP".
- Storage budget: 500 MB JPEGs per device. Older frames GC'd after 90 days; raw events kept indefinitely.
- Shadow-ban for pathological clusters (e.g., all detections in 5×5 m).

### 2.9 Admin surface

Phase 2 v1: **Supabase Studio**. Direct SQL for repair updates, bulk shadow-ban, manual `verified` cluster creation.

Phase 2 v2 (if traffic justifies): small Next.js dashboard with map + repaired-toggle.

### 2.10 Verification

- **Unit**: fusion math fixtures, clustering on synthetic point clouds.
- **Integration**: local Supabase, POST 100 events + 100 frames matching by time/location, run jobs, assert ≥1 cluster with correct centroid.
- **Manual**: install app pointed at staging URL, drive a route with potholes, verify red markers on subsequent launches.
- **Load**: 10k events/hour + 1k frames/hour for an hour; worker keeps up, `/api/v1/potholes` p95 < 500 ms.

### 2.11 Phase 2 hard constraints

- Backwards-compatible REST. Phase 1+1.5+1.6 clients work unchanged.
- Crowd anonymity preserved — `device_id` is a UUID v4, no PII, never exposed to non-admin clients.
- Free Supabase tier covers ≤ 1k devices × ≤ 100 events/day. Pay-as-you-go bill cap at $50/month before any code change.

---

## Phase 3 — On-device ML upgrade + labeled-data flywheel

Goal: replace heuristics with custom-trained models that improve over time. The data flywheel runs on data the app already collects (Phase 1 raw windows + Phase 1.5/1.6 camera frames).

### 3.1 Label-this-trip mode

Settings → "Label trip" toggle. When on, the recording dashboard gets a three-button row:
- **Pothole** — true positive
- **Not a pothole** — false positive (speed bump, manhole, lane crack)
- **Hidden** — skip / `null` label

Tagging writes to a new Room table:

```kotlin
@Entity(tableName = "event_label")
data class EventLabel(
    @PrimaryKey val id: String,
    val targetClientId: String,
    val targetKind: String,    // "event" | "frame"
    val label: Int,            // 1 = pothole, 0 = not, -1 = unknown
    val labeledAtMs: Long,
    val uploaded: Boolean = false,
)
```

Voice trigger ("Hey app, pothole" via `SpeechRecognizer`) is post-shipping polish.

Labels upload via a new `POST /api/v1/labels` endpoint.

### 3.2 Sensor 1D-CNN

Replaces the heuristic adaptive-threshold classifier. The existing detector stays as a candidate-generator; the CNN re-classifies each candidate.

- **Input**: the existing `raw_window_blob` Phase 1 was already saving — `[180, 10]` after rotation correction.
- **Architecture**: 3-layer 1D-Conv (32 → 64 → 128, kernel 5, stride 2) + global average pool + 2 dense → sigmoid. Target size **< 500 KB** INT8.
- **Training data**: server-side `event.raw_window_url` × `event_label.label` (filtered to `label != -1`).
- **Inference**: new `vision/SensorCnnModel.kt`. Called from `PotholeRefinementService` after window collection, before persistence. If `cnnProbability < 0.4` → discard. Otherwise `event.confidence = cnnProbability`.

### 3.3 Camera model refinement

Fine-tune the Phase 1.6 starter YOLOv8n on the crowd dataset collected since Phase 1.6.

- Server-side training script pulls `frame` rows with paired `event_label`s + admin-labeled frames.
- Trains via `model.train(data="crowd.yaml", epochs=100, imgsz=640)` and exports INT8 TFLite.
- Replaces `app/src/main/assets/road_gate.tflite`, bumps `RoadGateModel.DEFAULT_MODEL_ID` to `yolov8n_pothole_v2`.
- **No app code changes** — the `RoadGateModel` Phase 1.6 interface is stable across model versions.

### 3.4 Multi-modal fusion on device

When sensor and camera fire close together, fuse on the device to reduce upload bandwidth.

- **Trigger**: sensor event + camera frame within `|Δt| < 1 s` AND `Δdist < 5 m` (same device clock + GPS = tighter than server fusion).
- **Math**: `p_fused = sigmoid(w_s · logit(p_sensor) + w_v · logit(p_visual))`. Start `w_s = w_v = 1.0`.
- **Decision**:
  - `>= 0.7` → persist + `visual_confirmed = true`, skip server fusion
  - `0.4–0.7` → persist as ambiguous, upload for server fusion
  - `< 0.4` → drop both event and frame. Saves bandwidth.
- New `state/DetectionFusion.kt` Kotlin object subscribes to `DetectionStateBus` and orchestrates merging.

### 3.5 Retrain CI pipeline

Monthly GitHub Action automates the flywheel.

```yaml
on:
  schedule: [{ cron: '0 6 1 * *' }]
  workflow_dispatch:
jobs:
  retrain:
    runs-on: ubuntu-latest-gpu
    steps:
      - pull labeled data (last 90 days)
      - train sensor CNN + camera YOLO
      - export TFLite
      - verify APK budget
      - open PR with updated assets + model-attribution.md
```

PR is human-gated: maintainer reviews val-set deltas. Rollback = revert PR.

### 3.6 Verification

- Sensor CNN val_acc ≥ 85% on a 20% held-out test split.
- Camera YOLO mAP@0.5 ≥ 0.65.
- Total APK growth Phase 1 → Phase 3 ≤ 25 MB.
- 1D-CNN inference < 5 ms / window, YOLO < 60 ms / frame (Pixel 6).
- Continuous Both-mode drain < 12% / hour (matches Phase 1.5 baseline).

### 3.7 Phase 3 hard constraints

- Phase 2 fusion contract unchanged — same `detections` array shape.
- `RoadGateModel.DEFAULT_MODEL_ID` just bumps version suffix; backwards-compatible.
- "Label trip" defaults OFF — flywheel works only on data from users who explicitly opt in.
- Labels carry no free-text fields, no audio recording.

---

## Phase 4 — Production hardening + public release

Goal: take the app from "research prototype with a backend" to "public app store install".

### 4.1 Crash & error reporting

Sentry (preferred — self-hostable, decent privacy story) or Firebase Crashlytics. Initialized in `PotholeApp.onCreate` **only after** the user accepts the privacy notice. Custom tags for `mode`, `model_id`, `sensor_running`, `camera_running` so crashes can be filtered. ANR detection on (catches the kind of main-thread-Room bugs we already fixed in Phase 1).

### 4.2 Performance & thermal budgets

- Sustain continuous Both-mode for ≥ 2 h on a Pixel 7a class device without thermal-throttling triggering a camera pause.
- Wire the thermal-throttle pause from Phase 1.5 §14 — `PowerManager.addThermalStatusListener` on API 29+, `BroadcastReceiver(ACTION_POWER_SAVE_MODE_CHANGED)` older.
- Memory ceiling: p95 RSS < 250 MB. Currently ~150 MB.

### 4.3 Privacy / GDPR

- Privacy notice on first launch. Plain language: what's collected, where it goes, how to delete.
- `pref_share_anonymously` (already exists) + new `pref_camera_uploads_enabled` (separate gate — camera frames are higher-stakes than scalar sensor data).
- "Delete my data" Settings action. Local: wipe Room. Server: `DELETE /api/v1/device/{device_id}` cascades.
- `docs/privacy.md` DPIA-style table: every data piece, why, retention, legal basis.

### 4.4 Onboarding flow

First-launch Compose wizard (~3 screens):
1. "What this app does" — pitch + animated illustration.
2. "What we collect" — privacy notice + "I understand" button.
3. "How to mount your phone" — windshield-placement illustration.

Skippable. Resurrectable via Settings → "Replay onboarding".

### 4.5 Feedback loop

- Settings → "Send feedback" → pre-filled email with device UUID + version + last-N crash IDs.
- Per-row "Report bad detection" in History → POSTs to `/api/v1/feedback`.

### 4.6 Release checklist

- Play Store listing: screenshots, descriptions, privacy policy URL, content rating.
- Signed AAB. ProGuard/R8 audited and `minifyEnabled true`.
- Separate API keys for debug vs release builds; Maps API key restricted to release signing fingerprint.
- Beta channel via Play internal testing for ≥ 2 weeks.
- Phased rollout 1% → 10% → 50% → 100% over 4 weeks. Hold each gate at crash-free-sessions ≥ 99.5%.

### 4.7 Phase 4 hard constraints

- No new tracking SDKs beyond the crash reporter. No Mixpanel/Amplitude.
- Crash reporter respects opt-out — if privacy notice not accepted, no reports ship.
- All Phase 4 changes additive — no breaking changes to Phase 1–3 surfaces.

---

## Beyond Phase 4 — speculative

Not committed; recorded so the trajectory is visible.

- **Route-level scoring**: aggregate cluster density per road segment → expose a "rough road" metric API for cycling routing apps.
- **Severity-aware navigation**: when the user picks "Navigate" from search, route around high-severity clusters if there's a reasonable alternate.
- **Municipal feed**: tag clusters by jurisdiction and offer city DOTs an export feed for repair scheduling.
- **Vibration profiling**: same sensor pipeline, different output — characterize road surface (asphalt / concrete / cobblestone) as a side product.
