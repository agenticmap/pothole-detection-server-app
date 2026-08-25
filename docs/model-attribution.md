---
updated: 2026-08-23
---

# Server model attribution

The server-side detection worker (Phase 2.3, see `docs/phase-2.3-detection-plan.md`) runs a
**user-supplied** model — none is bundled in this repo. This file records its provenance,
mirroring the app repo's `docs/model-attribution.md`.

Phase 2.7 filled in the dataset half. The weights half is filled in when a model is exported;
see [`phase-2.7-detection-enablement.md`](./phase-2.7-detection-enablement.md) for the recipe.

## Detection model (`DETECTION_BACKEND=onnx`)

| | |
| --- | --- |
| Architecture | Ultralytics YOLO (`s` size fits the 4 GB dev GPU), ONNX export |
| `model_id` | set via `DETECTION_MODEL_ID` — version it, e.g. `yolo11s_pothole_v1` |
| Input | letterboxed `DETECTION_INPUT_SIZE` (default 640) RGB, float32 `[1,3,H,W]` |
| Expected output | **raw** `[1, 4+nc, N]`, post-sigmoid class scores (Ultralytics default) |
| File | path given by `DETECTION_MODEL_PATH`; lives in `models/`, gitignored |
| Trained on | `files/pothole detection.yolov8.zip` — see the dataset table below |
| License (weights) | _fill in after export — inherits the dataset's CC BY 4.0 attribution duty_ |
| SHA-256 (weights) | _fill in after export_ |
| Reported metrics | _fill in: mAP50 / mAP50-95 on the archive's 534-image test split_ |

### Export (Ultralytics)

```
yolo export model=best.pt format=onnx imgsz=640 opset=12 nms=False
```

`nms=False` is **not optional**. `app/detection/onnx_v1.py` decodes the raw graph itself; an
export with NMS baked in has shape `[1, 300, 6]`, which would decode to plausible-looking
garbage rather than error. `_check_layout` rejects it and prints this command back.

## Training dataset

| | |
| --- | --- |
| Archive | `files/pothole detection.yolov8.zip` (1,847,923,766 bytes) |
| SHA-256 | `2f3589bc7806bd7eae37c884bd5a73bc6e31509aa97558c90def72c9165d2157` |
| Source | Roboflow export, workspace/project `sean-sattar/pothole-detection-lsz3t-17s0n` |
| Exported | 2026-06-07, YOLOv8 format |
| Images | 5322 — 3728 train / 1060 valid / 534 test |
| Classes | `nc: 1`, `names: ['pothole']` |
| License | CC BY 4.0 |
| Empty labels | none — the set contains **no negative/background images** |
| Imagery | mixed GoPro (`G00xxxxx_*`) and RDD-style Japan road frames (`Japan_00xxxx_*`) |

Two properties of this set that bear directly on how its metrics should be read:

- **No background images.** Every image contains at least one pothole, so the training signal
  contains nothing about what a *non*-pothole road looks like. That is the likeliest source of
  the false positives on manholes, lane markings and rain artefacts observed on real frames.
  Adding negatives from the collected drives is the cheapest available improvement.
- **Domain shift.** The archive is landscape, road-facing, mostly daylight. Real uploads are
  480×640 portrait through a windshield, half of them at night in rain. mAP on the archive's own
  test split therefore *overstates* field performance, which is why Phase 2.7 added the
  `frame_label` ground-truth table rather than reporting dataset metrics alone.

### The other archive is unusable

`files/AI Pothole Detection.yolov8.zip` (2,547,235,730 bytes) is **truncated**: its final bytes
are not an End-of-Central-Directory record, and both Python `zipfile` and .NET `ZipFile` refuse
it. It is a partial download, not a corrupt-but-recoverable file. Re-fetching it from Roboflow
would roughly double the training set.

Related: `docs/phase-2.6-hardening.md` records that a `git filter-branch` destroyed the larger
~6.87 GB working copy of this same archive. The 2.4 GB file on disk is the restored *committed*
blob, so what is there now is all there is.

## External backend (`DETECTION_BACKEND=http`)

If inference is offloaded (Modal / Replicate / Triton), record the endpoint, model version,
and provider here instead. The endpoint must accept a raw JPEG body and return
`{"probability": float, "detections": [...], "model_id": str?}`, with `detections` in the
shared shape (`{"bbox": {x,y,w,h} normalized corner-origin, "label", "class_id",
"confidence"}`) so it matches what the device and the ONNX backend produce.
