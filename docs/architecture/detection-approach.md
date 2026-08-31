# Detection Approach — why server-side detection is a YOLO → VLM hybrid

> **Scope: this is the pipeline *inside* Model A (road-surface defects).** How many detection models exist at all, and why street furniture and road markings get their own, is a separate decision recorded in [`detection-model-strategy.md`](./detection-model-strategy.md). The short version: only Model A may write `server_probability`, because the fusion blend has no notion of class.

> **Decision record (the "why").** The as-built mechanics live in
> [`docs/phases/phase-2.3-detection-plan.md`](../phases/phase-2.3-detection-plan.md) §Phase 2.3b.
> This doc explains the choice behind them. Research is mid-2026; numbers are from
> the sources at the bottom.

## The question

The device already runs an on-device YOLOv8-nano (`yolov8n_pothole_v1`, mAP50 ≈ 0.56,
bundled at `app/src/main/assets/road_gate.tflite` in the app repo). Roadmap §2.3
proposed running a **second, bigger YOLO on the server** to re-score uploaded frames.

Is a second YOLO the right server-side move — or is a mix of computer vision + an LLM
better?

## Options compared

| Approach | Accuracy on potholes | Strength | Weakness | Cost |
|---|---|---|---|---|
| **Fine-tuned YOLO / RF-DETR** (server) | ~87–91% mAP (YOLOv8-class); RDD2022 SOTA via YOLOv11m / RF-DETR-S | Best localization; cheap per image | Same blind spots as any detector — shadows, manholes, wet patches, markings read as potholes | ~$0.0007/img (GPU) or CPU ~50–200ms/img |
| **Pure VLM** (per frame) | ~68% (GPT-4V zero-shot) — **loses at localization** | Reasoning; rejects look-alikes; explainable | Weak boxes; far slower; expensive per frame | $3–40 / 1k images |
| **YOLO → VLM hybrid** *(chosen)* | ~97% with **<2% false positives** | Detector localizes; VLM kills FPs on ambiguous frames; adds rationale + severity hint | Two moving parts | ~$0.05–0.35 / 1k images (VLM only on the gray zone) |
| Open-vocab (Grounding DINO / YOLO-World) | promptable, no fine-tune | no training data needed | weaker on small/irregular potholes; slower | — |

Key finding: **a pure VLM is worse than a fine-tuned YOLO at localization**, so replacing
YOLO with an LLM is the wrong move. But a VLM is excellent at *verification* — rejecting
the shadows, manhole/utility covers, wet patches, tar sealant lines, and lane markings that
crowd-sourced phone frames are full of and that a bare detector confuses for potholes.

## Decision & rationale

**Run a fast detector as Stage 1 and a VLM as a Stage-2 verifier on the *ambiguous* frames
only.** This gets the detector's localization and the VLM's false-positive discrimination,
and the cost stays viable because the VLM never touches confident frames.

Why a second *server* YOLO is only **partly** redundant with the device YOLO:
- The device model is a compute-limited **nano at modest accuracy** (mAP50 ≈ 0.56) — a
  larger server model (or an external GPU one) is a genuine upgrade in recall/precision.
- But the **genuinely new value is the VLM verifier**, not merely a bigger detector. Two
  YOLOs share the same failure modes; the VLM catches what both miss. False-positive
  reduction is the project's real weakness once crowd data scales, so that is where the
  marginal effort goes.

## How it maps to the code

The hybrid drops into the existing pluggable detection seam with **zero schema changes**:

- **`FrameDetector` protocol** (`app/detection/engine.py`) — `detect(jpeg) -> DetectionResult`.
  The hybrid is just another backend; the worker, scheduler, and DB are untouched.
- **`HybridDetector`** (`app/detection/hybrid_v1.py`) — Stage-1 detector + VLM verifier:
  - **Gray-zone gate**: the VLM is called only when Stage-1 probability ∈
    `[VLM_VERIFY_LOW, VLM_VERIFY_HIGH]` (defaults 0.40–0.75). Confident frames skip it.
  - **Logit-space blend** (reuses `app/fusion/engine.py` helpers), `VLM_BLEND_WEIGHT`
    toward the verdict so the VLM dominates in the gray zone.
  - **Cost cap** `VLM_MAX_CALLS_PER_RUN` per worker tick; overflow falls back to
    Stage-1-only (logged).
  - The verdict (incl. severity + one-line rationale) rides along in the existing
    `server_detections` JSONB as a `{"_vlm_verdict": {...}}` entry — **no migration**.
- **Pluggable VLM verifier** (`app/detection/vlm/`, mirrors `app/detection/registry.py`):
  `get_verifier()` selects `claude` / `gemini` / `local_http`; all SDKs lazy-imported,
  `local_http` (vLLM/Ollama/LM Studio for Qwen2.5-VL/LLaVA) needs no extra dependency.
- **Fusion is unchanged** — it already prefers the server signal via
  `COALESCE(server_probability, device_probability)` (`app/fusion/service.py`).

## Enabling & tuning

1. **Stage-1 model** (the only real external dependency):
   - *Fastest unblock*: a pretrained pothole YOLO from Roboflow Universe → export to ONNX →
     `DETECTION_BACKEND=onnx`, `DETECTION_MODEL_PATH=...`.
   - *Most robust*: fine-tune **YOLOv11m** (proven RDD2022 workhorse) or **RF-DETR-S**
     (DINOv2 backbone, most robust to phone-camera domain shift) on **RDD2022** (47k images)
     + crowd data; export ONNX. Or host on GPU and use the `http` Stage-1 backend.
2. **Turn on the hybrid**: `DETECTION_ENABLED=true`, `DETECTION_BACKEND=hybrid`,
   `DETECTION_HYBRID_STAGE1=onnx` (or `http`).
3. **Pick a verifier** (`VLM_BACKEND`): `claude` / `gemini` / `local_http`.
   - **Cost note**: the Claude backend defaults to `claude-opus-4-8`. For a high-volume
     crowd app set `VLM_MODEL_ID=claude-haiku-4-5` or `claude-sonnet-4-6`, or use `gemini`
     (2.5 Flash, cheapest cloud), or `local_http` (no per-image cost).
4. **Tune** `VLM_VERIFY_LOW`/`VLM_VERIFY_HIGH` (gray-zone width = VLM spend) and
   `VLM_BLEND_WEIGHT` against real frames once data exists. The `model_disagreement` table
   (device vs server probability) is the natural place to mine tuning examples.

See [`.env.example`](../../.env.example) for every knob.

## Deferred (Phase C)

Visual severity via **Depth Anything v2** (pothole depth/area → repair prioritization,
~8% IRI error). The project already derives IRI-style severity from the sensor signal, so
this is an additive, optional stage — not on the critical path.

## Sources

- YOLOv12: Attention-Centric Real-Time Object Detectors — arXiv 2502.12524
- RF-DETR (ICLR 2026), real-time transformer detector on a DINOv2 backbone — arXiv 2511.09554
- RDD2022 multi-national road-damage dataset — arXiv 2209.08538
- Optimizing YOLO architectures for road damage detection — arXiv 2410.08409
- RoadBench: vision-language foundation model for road damage — arXiv 2507.17353
- "Hidden economics of object detection in the VLM era" (when supervised pays off) — arXiv 2510.11302
- Pothole detection + depth estimation with Depth Anything v2 — arXiv 2504.13648
- Roboflow, "Best object detection models 2026" — blog.roboflow.com/best-object-detection-models
- Roboflow, serverless GPU inference cost comparison — blog.roboflow.com/serverless-inference-vision-ai-cost-comparison
