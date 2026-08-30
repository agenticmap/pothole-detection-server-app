# Phase 2.3 — Server-side detection model (As-Built)

> Status: **Implemented** (code + tests; ships gated off until a model is supplied).
> **Phase 2.7 built the enablement path around this** — offline evaluation, ground-truth
> labels, a backfill, an ROI crop for the real portrait frames, and the first tests that
> actually execute `onnx_v1.py`. See
> [`phase-2.7-detection-enablement.md`](./phase-2.7-detection-enablement.md). Two claims
> below are corrected there: `server_detections` now uses the device's box shape, and
> "re-fusing frames detected after they were already paired" is done.
> Companion to [`docs/roadmap.md`](../roadmap.md) §2.3 and follows
> [`docs/phases/phase-2.2b-read-path-plan.md`](./phase-2.2b-read-path-plan.md).
> For *why* server detection is a YOLO → VLM hybrid (the decision + research), see
> [`docs/architecture/detection-approach.md`](../architecture/detection-approach.md).

## Context

Phases 2.0–2.2b were shipped, but the one **core Phase-2 server capability never built** was
server-side ML on uploaded frames. `asset_frame.server_probability` / `server_model_id` /
`server_detections` were dead NULL columns, and the fusion engine's visual term was only the
weak on-device probability. Phase 2.3 adds an inference worker that runs a bigger detector on
stored JPEGs, populates those columns, upgrades fusion to prefer the server signal, and logs
device↔server disagreement (seeding the Phase-3 labeling flywheel).

This is **server-internal** — no wire-contract change. The Android client never reads the
`server_*` columns, and `POST /api/v1/frames` still returns `server_p: null` at ingest.

## Key decisions

1. **Own completion flag.** Detection claims frames via a new `asset_frame.detected_at`,
   independent of `processed_at` (owned by the fusion job) — the two jobs never race.
2. **Decoupled job, ordered by cadence.** `run_detection_job` runs on its own (shorter, 2-min)
   interval. Fusion's pairing SQL now uses
   `COALESCE(f.server_probability, f.device_probability)` — a frame fused before detection
   gracefully falls back to the device signal (no re-fusion).
3. **Pluggable detector**, mirroring the fusion engine/registry indirection, so backends swap
   by config and the worker is unit-testable with a stub.
4. **Gated off** (`DETECTION_ENABLED=false`, `DETECTION_BACKEND=none`) until a model/endpoint
   is configured — like 1.6 needing a `.tflite` drop.

## What was built

**`app/detection/`** (new package):
- `engine.py` — `FrameDetector` Protocol + frozen `DetectionResult(probability, detections, model_id, version)`.
- `onnx_v1.py` — `OnnxYoloDetector` (default): lazy-imports `onnxruntime` + `Pillow`, letterboxes
  to `detection_input_size`, runs a YOLOv8 ONNX session (CPU), thresholds + greedy NMS,
  `probability = max box confidence`, maps boxes back to original-image coords.
- `http_v1.py` — `HttpDetector`: POSTs the JPEG to an external GPU service (stdlib `urllib`).
- `registry.py` — `get_detector()` → backend per config, or `None` for `"none"`.
- `service.py` — `run_detection_job(pool, detector=None)`: advisory lock (key `0x504F56`, distinct
  from fusion/cluster), polls `detected_at IS NULL`, runs inference, writes `server_*` +
  `detected_at`, logs `model_disagreement` when `|device − server| > threshold`. On inference
  error it still sets `detected_at` (server fields NULL) so a bad file can't loop forever.

**Wiring & schema:**
- `migrations/004_detection.sql` — `asset_frame.detected_at` + partial index; `model_disagreement` table.
- `app/services/frame_service.py` — `load_frame_bytes(jpeg_url)` (inverse of `_store_jpeg_*`).
- `app/fusion/service.py` — pairing SQL prefers `server_probability` (added to the `unprocessed` CTE + `COALESCE`).
- `app/fusion/scheduler.py` — registers the `detection` job under `detection_enabled`.
- `app/config.py` — `detection_*` settings block.
- `requirements.txt` — `onnxruntime` + `Pillow` (lazy; only needed for `backend=onnx`).

## Configuration (to enable)

| Setting | Default | Notes |
|---|---|---|
| `DETECTION_ENABLED` | `false` | turn the worker on |
| `DETECTION_BACKEND` | `none` | `onnx` (in-process CPU) or `http` (external GPU) |
| `DETECTION_MODEL_PATH` | `""` | path to the YOLOv8 `.onnx` (backend=onnx) |
| `DETECTION_MODEL_ID` | `yolov8s_pothole_v1` | stamped into `server_model_id` |
| `DETECTION_HTTP_URL` | `""` | external endpoint (backend=http) |
| `DETECTION_INTERVAL_MINUTES` | `2` | poll cadence (shorter than fusion's 5) |
| `DETECTION_DISAGREEMENT_THRESHOLD` | `0.3` | `|device − server|` above this → logged |

You supply the model (see `docs/reference/model-attribution.md`).

## Verification

`docker compose up -d --wait`, then
`DATABASE_URL=...localhost:5433... pytest -q` → **72 passed** (66 prior + 6 new);
`ruff check app/ tests/` clean (pre-existing `test_frames.py` warnings aside).
- `tests/test_detection_db.py` (injected `StubDetector`): server_* written; disagreement logged;
  idempotent re-run; inference-error still marks `detected_at`; `backend=none` is a no-op.
- `tests/test_fusion_db.py`: a frame with `server_probability` fuses higher than a device-only frame.
- Manual (real model): drop a `.onnx`, set `DETECTION_ENABLED/BACKEND/MODEL_PATH`, POST a frame,
  confirm `server_*` populated and fusion uses it. **Phase 2.7 replaced most of this with
  scripts**: `scripts/detect_eval.py` scores stored frames without writing anything, and
  `tests/test_onnx_detector.py` verifies the decode arithmetic with no model at all.

## Out of scope / fast-follow
- GPU autoscaling / batched inference / retry queues for the HTTP backend.
- Surfacing `model_disagreement` for review (Phase-3 flywheel, §3.1–3.3).
- ~~Re-fusing frames detected after they were already paired.~~ **Done in Phase 2.7.**
  fusion selects `WHERE processed_at IS NULL` and `_UPSERT_PAIR_SQL` upserts on
  `(event_client_id, frame_client_id)`, so clearing `processed_at` re-scores in place with
  no duplicates. `scripts/backfill_detection.py --reset-fusion` does it, pinned by
  `test_detection_backfill_can_rescore_existing_pairs`.

## Phase 2.3b — Hybrid detector (YOLO Stage 1 + VLM verifier)

Research (mid-2026) is clear that a pure VLM loses to a fine-tuned YOLO at localization,
but a **YOLO → VLM hybrid** — fast detector proposes, VLM verifies only the *ambiguous*
frames — reaches ~97% accuracy with <2% false positives, rejecting exactly what crowd
frames are full of (shadows, manholes, wet patches, lane markings). It also adds a
human-readable rationale and a coarse severity hint.

This ships as **new backends, zero schema changes** — the hybrid implements the same
`FrameDetector` protocol, so the worker, fusion (`COALESCE(server_probability, …)`), and
`asset_frame.server_*` columns are untouched. The VLM verdict (incl. severity + rationale)
rides inside the existing `server_detections` JSONB as a `{"_vlm_verdict": {...}}` entry.

- `DETECTION_BACKEND=hybrid` → `app/detection/hybrid_v1.py` composes a Stage-1 detector
  (`DETECTION_HYBRID_STAGE1` = `onnx`|`http`) with a pluggable VLM verifier.
- **Gray-zone gate**: the VLM is called only when Stage-1 probability ∈ `[VLM_VERIFY_LOW,
  VLM_VERIFY_HIGH]`. Decisive frames skip it → bounded cost. `VLM_MAX_CALLS_PER_RUN` caps
  calls per worker tick (overflow falls back to Stage-1-only, logged — not silent).
- **Blend**: logit-space, `VLM_BLEND_WEIGHT` toward the verdict (reuses `app/fusion/engine`
  helpers), so the VLM dominates in the gray zone; clamped to [0,1].
- **Pluggable verifier** (`app/detection/vlm/`, mirrors the detector registry): `claude`
  (Anthropic vision + structured outputs), `gemini` (2.5 Flash, cheapest cloud),
  `local_http` (OpenAI-compatible — vLLM/Ollama/LM Studio running Qwen2.5-VL/LLaVA). All
  SDKs lazy-imported; `local_http` needs no extra dependency.

**Verification:** `tests/test_hybrid_detection.py` (no DB / SDK) covers the gray-zone gate
(no VLM call outside the band or when `p1` is None), blend direction (positive verdict
raises, negative lowers), the per-run cap, crop on/off, graceful VLM-failure fallback, and
`parse_verdict` on fenced / prose-wrapped / malformed replies. Full suite **100 passed**.
Manual: set `DETECTION_BACKEND=hybrid`, `DETECTION_HYBRID_STAGE1=onnx` (+ model), `VLM_BACKEND=claude`
(or `local_http`), POST clear-pothole / shadow / manhole frames, and confirm only gray-zone
frames incurred a VLM call (logs / provider dashboard) and the rationale landed in `server_detections`.
