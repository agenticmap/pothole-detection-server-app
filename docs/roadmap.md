---
updated: 2026-08-31
---

# Pothole Detection — Roadmap

> ## ⚠️ Read this first — what this document is
>
> This is the **original multi-phase design**, written before the server existed. It is kept for
> the reasoning behind each decision. It is **not** an implementation status report, and parts of
> it have been overtaken by the code:
>
> - **§2.2's SQL is stale.** The shipped schema uses generic multi-asset naming —
>   `asset_observation` / `asset_frame` / `asset_cluster` — not the `event` / `pothole_cluster`
>   tables shown below. The authoritative schema is `migrations/001`–`008`.
> - **Phase 2 is built, not "planned".** Ingestion, the ported sensor model, fusion, clustering,
>   the public and staff read paths, staff auth, and the operator dashboard (server + browser
>   console) all exist. See the per-phase plan docs:
>   [`phase-2.1-fusion-engine-plan.md`](./phases/phase-2.1-fusion-engine-plan.md),
>   [`phase-2.2-clustering-plan.md`](./phases/phase-2.2-clustering-plan.md),
>   [`phase-2.2b-read-path-plan.md`](./phases/phase-2.2b-read-path-plan.md),
>   [`phase-2.3-detection-plan.md`](./phases/phase-2.3-detection-plan.md),
>   [`phase-2.4-auth-plan.md`](./phases/phase-2.4-auth-plan.md),
>   [`phase-2.5-dashboard-plan.md`](./phases/phase-2.5-dashboard-plan.md).
> - **§2.9's admin surface is superseded.** It proposed Supabase Studio for repair marking;
>   `POST /api/v1/clusters/{id}/repair` now does it with an audit trail (Phase 2.5).
> - **The fusion status blockquote below is stale** — it describes the app circa Phase 1.5.
>   Server-side fusion runs today; the app assigns `visual_confirmed` / `frame_client_id` from the
>   camera path; the app's GPS now lives in `location/LocationHub`, not `MapsActivity`.
> - **The MATLAB methodology is fully ported to Python** (`app/fusion/matlab_port_v1.py`). There
>   is no MATLAB in the pipeline and no gRPC sidecar — the engine is in-process and swappable
>   behind `app/fusion/engine.py`.
>
> **For drive readiness, read [`road-test-readiness.md`](./runbooks/road-test-readiness.md) instead** — it
> is current as of 2026-08-12 and lists the blockers found and fixed before the first collection
> drive.

This document tracks the full multi-phase plan for the app. Shipped phases get a one-paragraph summary with a link to their dedicated changes doc. Future phases get the full design here so contributors know what's coming.

| Phase | Status | What |
| --- | --- | --- |
| 1 | ✅ Shipped | Sensor-detection pipeline + Room buffer + WorkManager upload queue |
| 1.5 | ✅ Shipped | UI/UX modernization + camera-detection plumbing |
| 1.6 | ✅ Shipped | Real on-device YOLO + bounding-box overlay + server-fusion plumbing |
| 2 | ✅ Built (2.0–2.4) | Server backend + sensor↔visual fusion + crowd clustering + read path + staff auth |
| 2.5 | ✅ Shipped | Operator dashboard: vector tiles, detail panel, repair marking, and the browser console (`dashboard/`). |
| 2.5b/c | ✅ Shipped | Dashboard design pass: Organic skin, self-hosted Protomaps vector basemap, KPI/filter dock + `/clusters/stats`, severity recalibrated to the real [0,1] scale, and a synthetic demo seed. Fixed a MapLibre worker 404 that had meant **no vector tile ever loaded**. |
| 2.6 | 🟡 In progress | Production hardening: public-read 500 fix, real `/health` 503, fit-job advisory lock, `schema_migrations` ledger, containerised dashboard/basemap, org-scoped repair writes, EXIF stripped on ingest, `POST /auth/logout`. See [`phase-2.6-hardening.md`](./phases/phase-2.6-hardening.md). |
| 2.7 | ✅ Shipped | Server-side detection enablement: the first tests that run `onnx_v1.py`, a raw-export layout guard, an ROI crop for the real portrait frames (1.78x more road pixels), `server_detections` reshaped to match the device's, `frame_label` ground truth + a labelling tool, offline evaluation, a backfill that also re-fuses stale pairs, and a `to_thread` fix for a ~60 s API stall per tick. Three models have since been trained on the 5322-image archive (`yolo11s_pothole_v1/v2/v3`) and measured against 375 hand labels. Detection is still gated **off** pending 2.7b, because adding hand-labelled negatives makes recall *worse*, not better. See [`phase-2.7-detection-enablement.md`](./phases/phase-2.7-detection-enablement.md). |
| 2.2c | ✅ Shipped | Spatiotemporal crowd fusion, implementing §4.4–4.5 of the *Probabilistic-based crowdsourcing* paper: cluster confidence is a spatiotemporally weighted (Gaussian RBF over distance-to-centroid and age) combination of its members' class distributions rather than a mean, opposing carriageways stay separate defects, and the per-event class posterior is finally persisted. See [`phase-2.2c-spatiotemporal-fusion.md`](./phases/phase-2.2c-spatiotemporal-fusion.md). |
| 2.2d | ✅ Shipped | The pairing search. The old ranking's ideal match was a frame taken at the same instant and place as the wheel impact — geometrically backwards, since the camera resolves a pothole while it is still *ahead* of the car. Replaced with a lookahead cost (lead band + kinematic residual + a penalty for frames shot after the event), a temporal window derived from speed instead of contradicting the spatial one, `is_primary` so the member gate reads the best *view* rather than the loudest *verdict*, and a frame-only member arm built but shipped off. Re-fusing `pothole_db` moved mean separation 5.25 m → 15.29 m and mean Δt −93 ms → −537 ms. See [`phase-2.2d-pairing-search.md`](./phases/phase-2.2d-pairing-search.md) for the design and [`phase-2.2d-runbook.md`](./runbooks/phase-2.2d-runbook.md) for the procedure. |
| 2.7b | ✅ Shipped (negative result) | **Road-surface classes.** Shipped: `frame_box` (migration 013) with a `boxed_at` reviewed-marker, box-drawing mode in the labelling tool (draft/submit, arrow navigation, thin-crack warning), a class-aware decoder — frame probability now comes from the pothole class alone and a labels/nc mismatch fails at startup — and an exporter that refuses to ship unreviewed frames as background. 200 frames reviewed, 43 boxes drawn (30 manhole / 7 grate / 6 patch), `yolo11s_pothole_v4` trained at nc=5. **Result: negative.** Recall fell 0.354 -> 0.215; the data-poor distractor classes steal potholes (any-class recall is 0.431, double the pothole-only 0.215; 15 of 65 positives are outscored by a non-pothole class). Root cause identified: **every real-domain box is a negative** — no real pothole has ever been boxed. Next: box the 65 positives, which requires rebuilding the holdout first. See [`phase-2.7b-road-surface-classes.md`](./phases/phase-2.7b-road-surface-classes.md). |
| — | 📖 Record | **Detection research record.** Consolidated account of the five detection models, the measurement failure behind them, and what can and cannot be concluded — written to be read on its own and to source a report. See [`detection-research-record.md`](./research/detection-research-record.md). |
| 2.7c | 🔧 In progress | **Public data (RDD2022).** The v2-v4 collapse was diagnosed as a positive drought: every real-domain box was a negative. Ingested 2575 real windshield-smartphone potholes from RDD2022 (US/Japan/Czech) via `scripts/ingest_rdd2022.py`. **Recall recovered 0.215 -> 0.677 -- the ratchet is broken -- but v5 still does not beat v1** (0.708 recall, and fewer false positives at every matched true-positive count). Likely causes: RDD boxes are small and distant (median 0.53% of frame area) while the ROI crop sees near-field road, plus a `batch=4` confound forced by VRAM. Establishes that a **promotion gate** is mandatory: v2, v3, v4 and v5 all pass archive metrics (mAP50 ~0.51 throughout) and all fail the holdout. See [`phase-2.7c-public-data.md`](./phases/phase-2.7c-public-data.md). |
| 2.7d | ✅ Shipped | **The review surface.** 2.7b and 2.7c both closed with the same remaining remedy — box our own positives — and both noted it was blocked, because boxing needs labelling first and labelling happened only in a terminal on one machine. This builds the instrument: five staff-gated endpoints (`app/routes/review.py`), `migrations/017` adding `frame_label.boxes_drafted_at` and an append-only `frame_label_history`, and a `dashboard/src/review/` module with a score-ranked queue, keyboard verdicts, band filters and drag-to-draw boxing. **Found and fixed a defect that would have poisoned the training set**: `finalize_boxes` did not check that boxes were ever saved, so submitting a judged-but-never-boxed frame signed it off with zero boxes — and the exporter keys on `boxed_at` alone, shipping it as a YOLO *background* image asserting "genuinely clean road" about an image nobody opened. That is the hard-negative poisoning of v2/v3/v4 arriving through a different door. Confirmed from the database that **0 of 43 hand-drawn boxes are potholes**. A following UX pass then fixed a **console-wide** defect the review surface exposed: `--color-danger` and `--color-success` had no dark-theme values, so **every error message in the console rendered at 1.56:1 in dark mode** — not low contrast, not rendered. Also: a held key wrote a verdict per key-repeat, `Enter` on Sign out saved a frame instead, and the single-column layout left **73% of the pane unreachable** while pushing the verdict buttons off the bottom (one cause: `max-height: 68vh` on a portrait corpus). Now two columns with the stage taking each frame's own aspect ratio, every contrast pairing clearing 4.5:1 in both themes, focus preserved across re-renders, a live region announcing each verdict, and class names on the boxes so hue is never the only signal. The dashboard also gained its first automated tests (69 specs) and its first CI job. See [`phase-2.7d-review-surface.md`](./phases/phase-2.7d-review-surface.md). |
| 2.9 | 🔧 In progress | **VLM verification — the instrument.** `hybrid_v1.py` has shipped since 2.3b with 21 tests, all against *fake* verifiers: no VLM has ever seen a frame from this corpus, so every number behind the design is an assumption. Shipped: `scripts/vlm_eval.py`, a read-only harness scoring the labelled frames through the real production path and reporting the VLM's binary verdict, matched-recall curves for Stage 1 / VLM / blend, band tables, and a free blend-weight sweep from a cached run; plus `openrouter` and `ollama` as named backends (same stdlib client as `local_http`, no new dependency). Also settles the thresholds on 340 labels rather than 140: **auto-accept >0.75 fires on 5 frames of 5,615 and the one labelled frame there is a false positive; auto-reject <0.40 would discard 55 of the 65 known potholes.** Scores prioritise, they never auto-label. See [`phase-2.9-vlm-verification.md`](./phases/phase-2.9-vlm-verification.md). |
| 2.10 | ✅ Shipped | **Showing what the detector saw, and the first VLM that ever answered.** The audit that opened this phase found the imagery arm built almost everywhere and exercised almost nowhere. **Two thousand frames had never been scored at all** — the corpus is 7,615, not 5,615, and nothing scores new arrivals while `DETECTION_ENABLED=false` — and they were invisible to the review queue, because `rank_by_score` drops unscored frames by design. **1,024 of them (51%) sit within 25 m of previously-covered road**, so the repeat-route coverage the integration round asked for had arrived unnoticed. Backfilled with v1 at `--conf 0.05` (the config default is 0.25, which would have silently split the only score the queue ranks on): 0 failures, and the new drive is materially better data — **39% at ≥0.30 against 20%**, 18% scoring exactly 0.0 against 39%. **The labelling seam went 1,041 → 1,819.** Console side: the panel had been rendering one number, `server_probability ?? device_probability`, for a frame carrying both detectors' boxes, both scores, the model id, the pairing deltas and a VLM verdict — and `.frame-thumb`'s `object-fit: cover` in a 4/3 box over a **portrait** corpus was **cropping away the bottom quarter of every frame, which is where the road surface is**. Both detector box sets now draw everywhere, separated by dash rhythm and an `srv`/`dev` prefix rather than by hue, with per-box confidence on screen for the first time. Added the codebase's first `<dialog>` as a full-size frame viewer (`showModal()`, so the top layer and document-inerting give the focus trap for free — which is why `--z-modal` stays unused). Fixed a panel that printed **"0 passes" for every cluster** while the database said 1, and **a second theme-blind colour token**: `--color-text-subtle` had no dark value and rendered at **2.15:1**, the identical defect to `--color-danger` and in the very phase that fixed that one — 26 rules consume it. 2.7d's contrast claim for that token is corrected in place. Also found `VLM_HTTP_URL=  # comment` assigning the **comment text as the value**, which is why no VLM had ever been reachable. Then a VLM answered: the blocker was a **4 GiB container memory limit**, not the GPU. See [`phase-2.10-imagery-surfaces.md`](./phases/phase-2.10-imagery-surfaces.md). |
| — | ✅ Round | **First full integration round (2026-08-30).** Collected drives taken end to end to a populated operator console. The unlock was the IsolationForest outlier gate: fitted on `ratio`/`gbar`/`magnitude`, it had learned "pothole" and reported it as "outlier", flagging **285 of 286** pothole-classed observations, so the crowd pipeline's sensor arm admitted exactly **one** row. `SENSOR_OUTLIER_FEATURES` (migration 014) makes the set configurable and defaults to the class-neutral `accel_std,speed_mps`; admitted observations went 1 → 166 and clusters 4 → 25. `SEVERITY_SCALE` was also recalibrated 2.0 → 0.25 — the old value saturated below the *minimum* of the observed pothole distribution, painting every cluster "Severe". Also shipped: a raw-observation map layer (the ~30% of detections that never reach a cluster were previously visible at no zoom), an optional `min_devices` read-path parameter, `device_gate_eval.py` and `storage_audit.py`. **Day two (2026-08-31)** then rebuilt the evidence model to match the paper: corroboration counted by **pass** rather than device (`migration 015`), `CLUSTER_MIN_POINTS` 3 → 1 (the old value silently discarded 46% of admitted members without ever requiring corroboration), and `ST_ClusterDBSCAN` replaced by the paper's §4.4 assignment — each event buffered at **2σ of its own GPS accuracy** and matched against cluster centroids from **prior sweeps only**. That took the widest cluster from **124 m to 19.9 m** and emptied the >25 m buckets entirely. The rate limiter moved into Postgres (`migration 016`), where it had had a table waiting since migration 001 while `--workers 2` enforced double the configured ceiling. **Headline negative result, and it survived all of it:** the pipeline has produced **zero** corroborated defects. Of 243 pothole-classed observations, none has another from a different day within 25 m — against a day-matched null of 10–35, with 0 of 30 random draws reaching the observed value. No clustering parameter and no classification strategy recovers one. **Corrected the same day, twice, and the second correction is the one that holds:** the zero is real but says nothing about the detector. **91.9% of pothole detections sit on road only one day ever covered**, against 68.4% for everything else, so they were never eligible to co-locate while the null was drawn from a population that was — that gap is the whole effect. Conditioned on road at least two days actually covered, the dataset holds **18** pothole detections: observed 0 against a null of 0–7 whose own median is **0**, p ≈ 0.64. The first correction, blaming two instrument regimes split on `gbar_in_max / accel_max_g`, is **retracted as circular** — `gbar` is the classifier's dominant input, so banding on it selects low-pothole sessions by construction. The failure is **route coverage**, not the detector: the days largely drove different roads (69.5% of all observations are on single-visit road), which also displaces "the same roads" from the swing story above. Repeat-route collection takes the eligible fraction from 8% to 100% by construction and is the only design that answers the question. See [`corroboration-coverage-analysis.md`](./research/corroboration-coverage-analysis.md) for the full working including both wrong turns, [`integration-round-2026-08.md`](./phases/integration-round-2026-08.md) for the record, [`paper-fidelity-assessment.md`](./research/paper-fidelity-assessment.md) for the point-by-point comparison, and [`integration-round-runbook.md`](./runbooks/integration-round-runbook.md) for the procedure. |
| 2.8 | 📋 Recommended next | Capture quality + provenance. Analysing the first 2916 real frames found the limiting factor is the data, not the detector: wire timestamps were whole-second (so fusion's Δt had only **three** possible values inside its 3000 ms window), one GPS fix is reused across ~3 frames, 25% of every inference is black padding the app adds itself, 69% of frames came from one night hour at 72 km/h, and the session join key the app already records is never uploaded — so there is no coverage denominator and no source of training negatives. See [`app-capture-findings.md`](./research/app-capture-findings.md). |
| 3 | 📋 Planned | On-device ML upgrade + labeled-data flywheel |
| 3.6 | 📋 Planned | **Model B -- street furniture inventory.** Traffic lights, signs, poles, hydrants. An *integration*, not an ML project: the stock `yolo11s.pt` already detects several of these with zero training. Full-frame (the pothole ROI crop discards the top 45% where they live), sampled at low rate (static infrastructure needs one detection per object, not 3/second), its own table, and it must **never** write `server_probability`. See [`detection-model-strategy.md`](./architecture/detection-model-strategy.md). |
| 3.7 | 📋 Planned | **Model C -- road markings.** Lane lines, arrows, crosswalks. **Segmentation, not detection** -- a lane line is long, thin and diagonal, so an axis-aligned box around one is mostly asphalt. Different architecture (CLRNet / LaneATT or semantic segmentation) and a different evaluation regime. See [`detection-model-strategy.md`](./architecture/detection-model-strategy.md). |
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

Full detail: the app repo's `docs/phase-1-changes.md`.

## Phase 1.5 — UI/UX + camera plumbing (shipped)

Material 3 DayNight theme, single-screen map-first layout, a unified control-center dashboard with a `[Sensor | Camera | Both]` mode selector, split-view camera on the map, swipe-to-delete history with Undo, and search → Google Maps navigation handoff. Camera pipeline is fully wired (CameraX → throttled analyzer → Room → multipart upload) but gated by a stub classifier — every frame returns `p=0.5` until a real model is dropped in.

Full detail: the app repo's `docs/phase-1.5-changes.md`.

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
4. Fill in [`docs/reference/model-attribution.md`](./reference/model-attribution.md).
5. Rebuild + install.

After ship: the app repo's `docs/phase-1.6-changes.md` (to be written).

---

## Phase 2 — Server-side fusion + crowd

Goal: stand up the backend so events + frames stop accumulating forever, run server-side ML on uploaded frames, run the sensor↔visual fusion job whose contract was locked in Phase 1.6, cluster crowd-sourced points into confirmed potholes, and serve them back to the app.

> **Phase 2.0 (ingestion server) is shipped.** **Phase 2.1 — Sensor Classification + Fusion Engine v1 is now implemented** (see [`docs/phases/phase-2.1-fusion-engine-plan.md`](./phases/phase-2.1-fusion-engine-plan.md)). The original 2017 MATLAB accelerometer classifier (k-means++ → GMM → Gaussian-NB) was ported server-side as a self-bootstrapping `sensor_model`; its `P(pothole)` feeds a logit-space sigmoid fusion with the camera's on-device probability (this §2.4). An Isolation-Forest outlier gate and an IRI-style severity output were added. Jobs run in-process via APScheduler. **Phase 2.2 — the crowd clustering job (§2.5 below) is now implemented** (see [`docs/phases/phase-2.2-clustering-plan.md`](./phases/phase-2.2-clustering-plan.md)): a third APScheduler job runs PostGIS `ST_ClusterDBSCAN` over recent high-confidence detections (sensor potholes + fused pairs) and upserts repair-safe `asset_cluster` rows. **Phase 2.2b — the read path (§2.6 below) is now implemented** (see [`docs/phases/phase-2.2b-read-path-plan.md`](./phases/phase-2.2b-read-path-plan.md)): a public, zoom-aware `GET /api/v1/potholes?bbox&zoom` endpoint serves the repair-filtered clusters back to the app (which renders them as map markers, §2.7). **Phase 2.3 — the server-side detection model (§2.3 below) is now implemented** (see [`docs/phases/phase-2.3-detection-plan.md`](./phases/phase-2.3-detection-plan.md)): a pluggable inference worker (in-process ONNX, or external HTTP) runs a bigger YOLO on uploaded frames, populates `asset_frame.server_*`, feeds the stronger visual signal into fusion via `COALESCE(server_probability, device_probability)`, and logs device↔server disagreement. Ships gated off (`DETECTION_ENABLED`) until a model is supplied. **Phase 2.3b** extends this with a `hybrid` backend — the Stage-1 detector plus a pluggable VLM verifier (Claude / Gemini / local) that confirms only ambiguous frames to cut false positives; the rationale and design are in [`docs/architecture/detection-approach.md`](./architecture/detection-approach.md). **Phase 2.4 — the city-staff auth tier** (see [`docs/phases/phase-2.4-auth-plan.md`](./phases/phase-2.4-auth-plan.md)) split the read path into a public locations-only tier and a staff detail tier behind RS256 bearer tokens. **Phase 2.5 — the operator dashboard is now implemented** (see [`docs/phases/phase-2.5-dashboard-plan.md`](./phases/phase-2.5-dashboard-plan.md)): staff-gated `ST_AsMVT` vector tiles for the map, a cluster detail endpoint (members, paired frames, repair history), authenticated frame image serving, and `POST /api/v1/clusters/{id}/repair` — the first write path to `asset_cluster` outside the clustering job, audited in a new `repair_log` table — plus the browser console itself (`dashboard/`: Vite + TypeScript + MapLibre GL JS, served at `/dashboard`), which closes the loop from a phone detecting a pothole to an operator marking it repaired. For what it takes to run a real on-road test of the full loop, see [`docs/runbooks/road-test-readiness.md`](./runbooks/road-test-readiness.md).

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
- **Disagreement logging**: `abs(device_probability - server_probability) > 0.3` → log to
  `model_disagreement` for Phase 3 review. *Built in Phase 2.3; the table is still empty,
  necessarily, because no server model has ever scored a frame. Phase 2.7's backfill is what
  finally populates it.*

### 2.4 Fusion job

Cron every 5 minutes. Implements the contract from Phase 1.6:

For every uploaded `frame` row with `ts T_f` and any nearby `event` row with `ts T_e`:

```
fused_confidence = sigmoid(w_s · logit(p_sensor) + w_v · logit(p_visual))
```

with `w_s = w_v = 0.5` to start; refined offline against labeled data.

> **Superseded by Phase 2.2d.** The candidate gate is no longer a fixed `|Δt| < 3000 ms` AND
> `dist < 25 m`: at the measured median 13.02 m/s those two contradict each other (3000 ms of travel
> is 39 m), so the temporal window is now derived from speed as `window_m / speed`, bounded by
> `FUSION_WINDOW_MS_MAX`, and `window_m` is 40 m. Nor is the winner the nearest candidate in time —
> that preferred the frame taken on top of the pothole, which the camera cannot see. It is now the
> lowest-cost candidate under a lookahead model. See
> [`phase-2.2d-pairing-search.md`](./phases/phase-2.2d-pairing-search.md).
>
> Note also that §3.4 below specifies `w_s = w_v = 1.0` for the on-device version while the server
> ships `0.5`. That is not a typo in one of the two: with weights summing to 1 the blend is a
> weighted geometric opinion pool (agreement does not strengthen the result), and at 1.0 it is
> log-odds accumulation (it does). Which is wanted has never been decided, and the server's 0.5 is
> "to start", unrefined for want of labels.

### 2.5 Clustering job

Cron every 15 minutes. PostGIS `ST_ClusterDBSCAN(geom, eps_meters := 25, minpoints := 3)` over the last 30 days of high-confidence members. Promotes to `pothole_cluster`:
- `centroid` = weighted mean of member geoms by their `fused_confidence`
- `severity` = median of contributing events' magnitudes
- `confidence` — **as built, this is no longer a plain mean.** Phase 2.2c integrates the
  members' class distributions with spatiotemporal RBF weighting, so recency and proximity
  to the centroid matter. See [`phase-2.2c-spatiotemporal-fusion.md`](./phases/phase-2.2c-spatiotemporal-fusion.md).
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

> **Partly stale.** The per-device limits shipped at **5000/hour** (a drive's buffered data
> drains in one burst — see `road-test-readiness.md`), and the limiter is in-memory per
> worker, not table-backed. Per-IP rules, the storage budget, frame GC and shadow-ban are all
> still unimplemented; they are what remains of Phase 2.6 after its first pass
> ([`phase-2.6-hardening.md`](./phases/phase-2.6-hardening.md)), which took the startup-path
> correctness fixes, the container deployment story and the Phase 2.5 security leftovers
> instead. Note `migrations/001` already carries an unused `device_rate_limit` table for the
> table-backed limiter.

- Per-device: 100 events/hour + 100 frames/hour, enforced via a `rate_limit` table or RLS function. 429 on excess.
- Per-IP: Cloudflare in front of Supabase, default rule "100 requests/minute per IP".
- Storage budget: 500 MB JPEGs per device. Older frames GC'd after 90 days; raw events kept indefinitely.
- Shadow-ban for pathological clusters (e.g., all detections in 5×5 m).

### 2.9 Admin surface

> **Superseded by Phase 2.5.** Repair marking is now
> `POST /api/v1/clusters/{cluster_id}/repair` (staff-gated, audited in `repair_log`), not
> direct SQL — there is no Supabase Studio in this deployment, and hand-editing `repaired_at`
> on a live database was the only option before. The "v2 dashboard" below is being built as
> the operator dashboard (MapLibre rather than Next.js); its server side is done, the browser
> frontend is not. See [`phase-2.5-dashboard-plan.md`](./phases/phase-2.5-dashboard-plan.md).
> Bulk shadow-ban and manual `verified` cluster creation remain unimplemented. Repair
> writes are now **org-scoped** — `migrations/009` added `asset_cluster.org_id`, so a
> staff member of one municipality can no longer close out another's clusters; see
> [`phase-2.6-hardening.md`](./phases/phase-2.6-hardening.md) §6 (including why unowned clusters
> are admin-only). Reads are still global.

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
