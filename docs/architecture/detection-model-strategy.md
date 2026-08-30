---
updated: 2026-08-28
---

# Detection model strategy — how many models, and what goes in each

**Read this before adding a detection class.** It is the decision record for how the visual side of
the platform expands beyond potholes, and it exists because the intuitive answer — one model that
detects everything — is wrong here for reasons that were measured rather than assumed.

For *why server-side detection is a YOLO → VLM hybrid*, see
[`detection-approach.md`](./detection-approach.md). That question is about the pipeline **inside**
Model A below. This document is about which model families exist at all.

## The decision

Three families, split by **where the object sits in the frame** and **whether the training data
already exists in public**, not by semantics:

| | **A — road surface** | **B — street furniture** | **C — road markings** |
|---|---|---|---|
| Objects | **pothole, manhole, grate, patch, crack** | traffic light, sign, pole, hydrant | lane lines, arrows, crosswalks |
| Technique | detection | detection | **segmentation** |
| Weights | **custom-trained** | **pretrained, off-the-shelf** | pretrained / fine-tuned |
| Geometry | ROI crop 0.45–0.90 | full frame | road plane |
| Sample rate | every frame | every Nth frame / >10 m of travel | offline batch |
| Consumer | `server_probability` → fusion → `asset_cluster` | asset inventory table | condition reporting |
| Status | **shipped, being improved** (Phase 2.7 / 2.7b) | not started | not started |

The Model A class list lives in one place, `app/detection/classes.py`, and is imported by the
labelling tool, the dataset exporter and the decoder. Position *is* the class id, so a disagreement
between any two of them mislabels every box and can source the frame probability from the wrong
class. Only ever append to it.

> **Hard rule: only Model A may write `server_probability`.**
> That column is the visual term in the fusion blend
> (`app/fusion/service.py` `_CANDIDATE_COLUMNS`). A confident traffic-light detection reaching it
> would be read as a confident *pothole*, because the blend has no notion of class. Furniture and
> markings write to their own tables and never touch the fusion path.

## Why A and B cannot share a model

Three reasons. Two are measured on this repo's own data, not borrowed from general practice.

### 1. The ROI crop conflicts irreconcilably

`DETECTION_ROI_ENABLED` crops to 0.45–0.90 of frame height — below the horizon, above the hood —
and it is worth about **+0.07 F1** on potholes. Measured twice, on two independently trained models:

| model | ROI on | ROI off |
|---|---|---|
| `yolo11s_pothole_v1` | **0.382** | 0.315 |
| `yolo11s_pothole_v3` | **0.479** | 0.326 |

That crop discards the top 45% of the frame, which is exactly where signs, lights and poles live. A
combined model must either drop the crop — giving back accuracy that took three training runs to
find — or never see furniture. There is no configuration that serves both.

### 2. Data economics: spend labelling hours only where public data does not exist

The scarce resource is **human labelling time**, not GPU time.

- Nobody else has 480×640 portrait windshield potholes, at night, through Toronto rain. That data
  can only come from these drives, and `model-attribution.md` records that the one public archive
  in use is landscape, daylight, and contains **zero** background images.
- Everybody has traffic signs and lights: Mapillary Traffic Sign Dataset, BDD100K, Cityscapes, COCO.

This is not theoretical. The stock `yolo11s.pt` already sitting in this repo — downloaded as a
training starting point — detects the following **with zero training and zero labelling**:

```
class  9: traffic light      class 11: stop sign
class 10: fire hydrant       class 12: parking meter
class 13: bench              (plus person, bicycle, car, motorcycle, bus, truck)
```

Hand-boxing traffic signs would be paying in the project's scarcest currency for something already
available for free.

### 3. Gradient competition — a failure this project has already suffered

Adding 200 easy background images cut pothole recall from **0.708 to 0.354**. Common, easy examples
dominated the loss and the rare, hard class absorbed the suppression; see
[`phase-2.7-detection-enablement.md`](../phases/phase-2.7-detection-enablement.md).

Signs and lights appear in nearly every frame. Potholes appear in a small fraction. Folding
thousands of easy furniture instances into the same loss function is the same failure mode at an
order of magnitude more scale.

### The counter-argument, and why it does not hold

The honest case for one model is one inference pass — real, since production scoring is CPU-bound at
roughly 0.3 s/frame.

It dissolves on sample rate. Furniture is **static infrastructure**: a pole needs detecting once per
location, not three times a second. Model B can run on every Nth frame, or only when GPS has moved
more than ~10 m, or as an offline batch entirely outside the fusion path. The two families differ in
required sample rate by about an order of magnitude, so the "2× cost" never materialises.

## Why markings are family C, not a class in A

A bounding box describes a compact object. A lane line is long, thin and usually diagonal, so an
axis-aligned box around one is mostly asphalt — the box carries almost no information about the
thing it supposedly localises. Marking condition and coverage want **semantic segmentation**, or a
purpose-built lane model (CLRNet, LaneATT), and a different evaluation regime (IoU per pixel, not
mAP per box).

Adding `road_marking` as a class in Model A would therefore add a class the representation cannot
express. It is deferred as its own family rather than bolted on.

## What this means in practice

**Model A is the moat.** Custom data, custom training, every labelling hour. Manhole and grate are
classes *within* it specifically because they are the hard negatives currently destroying pothole
recall — giving the model somewhere to put a manhole other than "background" is the fix, and it only
works if they are classes rather than suppressed background.

**Model B is an integration, not an ML project.** Pretrained weights, its own table, its own
schedule, third-party licence and attribution recorded in `model-attribution.md`. It can be
validated in an afternoon by running stock `yolo11s.pt` over the stored frames and inspecting the
`traffic light` / `stop sign` hits.

**Model C is a research task**, deferred until A is settled and B is proving useful.

## Wire-contract note

Multi-class within Model A is safe for existing clients, verified 2026-08-28:

- The Android app has **no** reference to `server_detections`, `server_probability` or detection
  labels — it uploads `device_detections` and reads clusters.
- The dashboard's `.label` references are all UI strings (severity tiers, KPI captions), never
  detection box labels.
- `server_detections` already emits `{"bbox", "label", "class_id", "confidence"}` per box, so the
  storage shape needs no change to carry more classes.

The frame probability **was** the one unsafe part: `onnx_v1.py` derived it as the maximum score over
*all* classes, so a confident manhole would have reached fusion as a confident pothole. Phase 2.7b
made it class-aware (`_frame_probability` takes the best primary-class box) and made a labels/nc
mismatch fail at construction. That is what enforces the hard rule above in code rather than by
convention.

## Adding a new object type — the checklist

1. Which family? Where does it sit in the frame, and does public data exist?
2. If A: does a box represent it well? Does it need to influence fusion, or only be catalogued?
3. If it must not influence fusion, it does not belong in A regardless of geometry.
4. Does public pretrained coverage exist? If yes, integrate rather than train.
5. Record weights provenance and licence in [`model-attribution.md`](../reference/model-attribution.md).
