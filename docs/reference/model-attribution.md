---
updated: 2026-08-23
---

# Server model attribution

The server-side detection worker (Phase 2.3, see `docs/phases/phase-2.3-detection-plan.md`) runs a
**user-supplied** model — none is bundled in this repo. This file records its provenance,
mirroring the app repo's `docs/reference/model-attribution.md`.

Phase 2.7 filled in the dataset half. The weights half is filled in when a model is exported;
see [`phase-2.7-detection-enablement.md`](../phases/phase-2.7-detection-enablement.md) for the recipe.

## Detection model (`DETECTION_BACKEND=onnx`)

| | |
| --- | --- |
| Architecture | Ultralytics YOLO (`s` size fits the 4 GB dev GPU), ONNX export |
| `model_id` | set via `DETECTION_MODEL_ID` — version it, e.g. `yolo11s_pothole_v1` |
| Input | letterboxed `DETECTION_INPUT_SIZE` (default 640) RGB, float32 `[1,3,H,W]` |
| Expected output | **raw** `[1, 4+nc, N]`, post-sigmoid class scores (Ultralytics default) |
| File | `models/yolo11s_pothole_v1.onnx` (36.2 MB), gitignored; path given by `DETECTION_MODEL_PATH` |
| Trained on | `files/pothole detection.yolov8.zip` — see the dataset table below |
| License (weights) | CC BY 4.0, inherited from the dataset. Attribution duty: Roboflow workspace/project `sean-sattar/pothole-detection-lsz3t-17s0n` |
| SHA-256 (weights) | `16ceb1471cf59c3157efef4a3191e0b859ba319d9d5214fc1ca1564b8fca1863` |
| Reported metrics | **mAP50 0.512, mAP50-95 0.234** (P 0.623, R 0.473) on the archive's 534-image test split, 1215 instances. These are **dataset** metrics and overstate field performance — see the two caveats below. |

### Training run (2026-08-25)

Fine-tuned from `yolo11s.pt`, 80 epochs, 3.37 h wall clock on an RTX 3050 Ti (4 GB):

```
yolo detect train model=yolo11s.pt data=<extracted>/data.yaml imgsz=640 batch=8      epochs=80 patience=15 amp=True project=runs name=pothole_v1 workers=2
```

| Split | P | R | mAP50 | mAP50-95 |
|---|---|---|---|---|
| valid (1060 img) | 0.608 | 0.465 | 0.492 | 0.227 |
| test (534 img) | 0.623 | 0.473 | 0.512 | 0.234 |

`patience=15` never fired: mAP50-95 was flat across the last ten epochs (0.218 → 0.226) and
`val/box_loss` bottomed at epoch 63 before drifting up, so the schedule had converged and more
epochs would not have helped. **Recall (0.47) is the weaker half** — worth remembering when
deciding what data to add next, because more images of the same kind mostly buy precision.

A note on the archive itself, found while training: Ultralytics reported **duplicate boxes** on 29
valid and 5 test images (`N duplicate labels removed`). It de-duplicates silently, so this changes
nothing about the run, but the instance counts in the Roboflow export are slightly inflated
relative to what was actually trained and scored against.

### Export (Ultralytics)

```
yolo export model=best.pt format=onnx imgsz=640 opset=12 nms=False
```

`nms=False` is **not optional**. `app/detection/onnx_v1.py` decodes the raw graph itself; an
export with NMS baked in has shape `[1, 300, 6]`, which would decode to plausible-looking
garbage rather than error. `_check_layout` rejects it and prints this command back.

## Class set

`yolo11s_pothole_v1`/`v2`/`v3` are single-class (`nc: 1`, `names: ['pothole']`). Phase 2.7b extends
Model A to the road-surface set -- pothole, manhole, grate -- so that hard negatives have somewhere
to go other than "background"; see
[`phase-2.7b-road-surface-classes.md`](../phases/phase-2.7b-road-surface-classes.md).

**Third-party weights get their own row here.** Model B (street furniture) is planned as an
integration of *pretrained* weights rather than a trained model -- stock COCO `yolo11s.pt` already
covers `traffic light`, `stop sign`, `fire hydrant`, `parking meter` and `bench`. Whatever ships
must record its source, version, licence and SHA-256 in this file exactly as a trained model does:
a `server_model_id` that cannot be traced to a dataset and a licence is not auditable, and that
applies to borrowed weights more than to our own. See
[`detection-model-strategy.md`](../architecture/detection-model-strategy.md).

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

Related: `docs/phases/phase-2.6-hardening.md` records that a `git filter-branch` destroyed the larger
~6.87 GB working copy of this same archive. The 2.4 GB file on disk is the restored *committed*
blob, so what is there now is all there is.

## External backend (`DETECTION_BACKEND=http`)

If inference is offloaded (Modal / Replicate / Triton), record the endpoint, model version,
and provider here instead. The endpoint must accept a raw JPEG body and return
`{"probability": float, "detections": [...], "model_id": str?}`, with `detections` in the
shared shape (`{"bbox": {x,y,w,h} normalized corner-origin, "label", "class_id",
"confidence"}`) so it matches what the device and the ONNX backend produce.
