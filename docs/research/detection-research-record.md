---
updated: 2026-08-30
status: research record — source material for a written report
---

# Learning to detect road-surface defects from windshield video: a record of five models

A consolidated account of the detection research carried out on this platform, written to be
readable on its own and to serve as source material for a report. Engineering detail lives in the
phase documents ([2.7](../phases/phase-2.7-detection-enablement.md),
[2.7b](../phases/phase-2.7b-road-surface-classes.md), [2.7c](../phases/phase-2.7c-public-data.md)); this document
carries the question, the method, the measurements, and what can and cannot be concluded from them.

**Headline result.** Five detectors were trained. Four were worse than the first. The best model
remains the baseline, and the central finding is not a model but a **measurement failure**: the
metric everyone reports (mAP on a held-out split of the training archive) varied by 0.03 across
models whose real-world recall varied by a factor of **3.3**. Every intuition-driven data addition
made the system worse, and each was individually reasonable.

---

## 1. Problem and setting

The platform detects road-surface defects from a smartphone mounted on a vehicle windshield,
fusing an accelerometer-based sensor verdict with a camera-based visual verdict. This record
concerns only the visual half.

**Operating conditions.** Uploaded frames are **480×640 portrait** windshield captures from
driving in the Greater Toronto Area, including night and rain. Inference runs on CPU, server-side,
at roughly 0.3 s/frame.

**Corpus at the time of writing.**

| | |
|---|---|
| Frames | 5,615 |
| Distinct devices | 2 (5,588 / 27 — effectively single-device) |
| Sensor observations | 4,637 |
| Fusion pairs | 4,432 |
| Spatial clusters | 4 |
| Hand labels (`frame_label`) | 375 — 65 pothole, 275 not, 35 unsure |
| Hand boxes (`frame_box`) | 43 across 37 frames — 30 manhole, 7 grate, 6 patch |
| Frames reviewed for boxes | 200 (163 found genuinely clean) |

**Why the visual term matters.** Fusion blends the two modalities in logit space,
`sigmoid(w_s·logit(p_s) + w_v·logit(p_v))`. A **silent** detector is not a failure: the pipeline
falls back to `device_probability`, i.e. the status quo. This asymmetry — silence is free, error is
not — determines which metric is appropriate, and is the reason F1 is the wrong objective here.

---

## 2. Method

**Architecture.** Ultralytics YOLO11s fine-tuned from COCO weights, exported to ONNX
(`opset=12 nms=False`, raw `[1, 4+nc, N]` output decoded in-process). The `s` size is the ceiling
for the 4 GB RTX 3050 Ti used throughout.

**ROI crop.** Inference crops to 0.45–0.90 of frame height — below the horizon, above the hood —
placing ~1.8× more pixels on road surface. Measured worth on two independently trained models:

| model | ROI on | ROI off |
|---|---|---|
| v1 | **0.382** | 0.315 |
| v3 | **0.479** | 0.326 |

**Ground truth.** A frame-level verdict table (`frame_label`, 1 / 0 / −1) plus a later box table
(`frame_box`, corner-origin normalized). Labelling used a purpose-built localhost tool with three
deliberate properties:

- **Blind review.** Model boxes are hidden by default. Showing a model's guess while a human
  decides anchors the human to the model, producing labels that flatter whatever produced them.
- **Stratified sampling.** With a median `device_probability` of 0.118, a random 300 frames would
  be ~300 negatives and would measure recall not at all. The queue round-robins across
  (device-probability decile × day/night). **Consequence: the positive rate in the labelled set is
  not an estimate of real-world prevalence.**
- **Reviewed-clean ≠ never-reviewed.** A `boxed_at` marker distinguishes "a human looked and saw
  nothing" from "nobody opened this". Only the former may become a background training image.

**Evaluation.** A frozen holdout of **140 frames (65 pothole / 75 not)**, with the 200 frames used
for training explicitly excluded. Models are compared at **matched recall** — at the same number of
true positives caught, which pays fewer false positives — rather than at a matched threshold, since
two models rarely share a calibration.

---

## 3. The five models

| model | training data added | archive mAP50 | **recall** | precision | F1 |
|---|---|---|---|---|---|
| **v1** | — (3,728 archive positives) | 0.512 | **0.708** | 0.568 | **0.630** |
| v2 | + 200 hand-labelled negatives, ROI-cropped | ~0.51 | 0.431 | 0.718 | 0.538 |
| v3 | + 200 hand-labelled negatives, uncropped | 0.494 | 0.354 | **0.742** | 0.479 |
| v4 | + 43 class boxes, nc=5 | 0.513 | 0.215 | 0.667 | 0.326 |
| v5 | + 2,575 public in-domain potholes, nc=1 | 0.524 | 0.677 | 0.518 | 0.587 |

All at `--conf 0.05`, ROI on, on the same frozen holdout. Training was 80 epochs throughout;
3.4–5.5 h per run.

**The archive column is the point.** It spans 0.494–0.524 — a 0.03 band — while recall spans
0.215–0.708. **Archive mAP has never once predicted field performance in this project.**

---

## 4. Findings

### 4.1 Hard negatives without positives cause monotonic recall collapse

Adding 200 hand-labelled negatives cut recall from 0.708 to 0.431, and again to 0.354 when a
confound was removed. The negatives were *hard* by construction: the labelling queue is stratified
toward frames the model already found interesting, so `label = 0` frames are disproportionately
manholes, tar seals and patches — dark, roughly pothole-shaped, on road surface.

With a single class, the model's only way to explain a manhole is "background", which it achieves
by suppressing *dark irregular shape on asphalt*. Real potholes are dark irregular shapes on
asphalt. **This is a ratchet, not a flywheel: left alone it converges on a detector that never
speaks.**

### 4.2 A confound that looked like the cause, and was not

v2's negatives were ROI-cropped while its positives were not, making crop geometry a perfect
predictor of class. The leak was real and measurable: v2 preferred ROI-*off* (F1 0.605 vs 0.538)
while v1 and v3 prefer ROI-*on*.

Removing it (v3, uncropped negatives) restored the normal geometry preference **and recall fell
further**, 0.431 → 0.354. The crop was a confound; the negatives were the cause.

Mechanism worth stating because it inverts the intuition: v2's crop was a *shortcut* — the model
could satisfy the background class on image shape alone and never learn what the pixels meant. v3
removed the shortcut, so it actually learned the content lesson, and the content lesson is the
harmful one. **v3 is better trained on the same data and worse at the task.**

### 4.3 Data-poor classes steal from the class that matters

v4 gave the distractors their own classes (`pothole, manhole, grate, patch, crack`) on the
hypothesis that an alternative hypothesis for a manhole would relieve the suppression. Recall fell
again, to 0.215.

Scoring the holdout two ways separates *detecting* from *naming*:

| scoring | recall | precision |
|---|---|---|
| pothole class only (what fusion receives) | 0.215 | 0.667 |
| any class counts | **0.431** | 0.683 |

**The model localised twice as many potholes as it reported.** On **15 of 65** pothole frames a
non-pothole class outscored pothole. With 30 / 7 / 6 training boxes those classes were far too
data-poor to be selective, so they fired on potholes instead.

Boxes emitted on the 140-frame holdout: pothole 24, manhole 15, grate 7, patch 8, crack 0.

### 4.4 The binding constraint was a positive drought, not a class taxonomy

Across three labelling passes the project produced 43 hand-drawn boxes, **none of them a pothole**,
plus 163 reviewed-clean frames. The only positives available to any model were 3,728 archive images
from a different visual domain — landscape, daylight, GoPro, Japan.

**Every real-domain supervision signal said "road that looks like this is not a pothole", and
nothing said what a real pothole looks like.**

### 4.5 In-domain public data reverses the collapse but does not beat the baseline

v5 added **2,575 real windshield-smartphone pothole boxes** from RDD2022 (see §5). Recall recovered
0.215 → 0.677, a 3.1× improvement, confirming the diagnosis. But matched on true positives caught,
v1 still wins or ties everywhere:

| TP caught | recall | v1 FP | v5 FP | better |
|---|---|---|---|---|
| 34 | 0.523 | **25** | 28 | v1 |
| 14 | 0.215 | **8** | 12 | v1 |
| 8 | 0.123 | **4** | 7 | v1 |
| 6 | 0.092 | 3 | 3 | tie |
| 5 | 0.077 | 1 | 1 | tie |

v1 also has the higher ceiling: 46/65 true positives to v5's 44/65.

**Leading explanation — scale mismatch.** RDD pothole boxes have a **median area of 0.53% of frame**
(min 0.11%, max 1.5%), frequently near the horizon. The production detector crops to the road band
and sees near-field surface at far larger scale. RDD is *modality*-matched — windshield-mounted
smartphone, which is why it was selected over every alternative — but not *scale*-matched.

**Acknowledged confound.** v5 trained at `batch=4`; v1–v4 at `batch=8`. The 4 GB GPU exhausted
memory at batch 10/674 with other applications holding VRAM. The v1–v5 gap is 2 true positives,
narrow enough that batch size cannot be excluded as the explanation.

### 4.6 Two objectives, not one — a single ranking is impossible

v1 reaches 46/65 true positives to v3's 23, yet **v3 pays fewer false positives at every operating
point they share**. A "must win everywhere" criterion rejects both in both directions. They are good
at different jobs:

| objective | judged on | best model |
|---|---|---|
| **fusion** — what scores production frames | false positives at matched recall | **v3** |
| **sampler** — what ranks a human review queue | recall ceiling | **v1** |

The distinction follows from §1: for fusion, silence is free, so precision at low recall dominates.
For an active-learning queue, reach is everything — a frame the model never fires on never reaches a
human.

### 4.7 Thresholds do not transfer between components

An active-learning design proposed a 0.40–0.75 "gray zone": trust above as positive, below as
negative, review the middle. Those constants belong to a **VLM verifier** deciding when an expensive
call is worth making, and had never been calibrated against the detector.

Measured, v1 on the holdout:

| band | frames | potholes | pothole rate |
|---|---|---|---|
| 0.00–0.05 | 59 | 19 | 32.2% |
| 0.05–0.15 | 34 | 18 | 52.9% |
| 0.15–0.30 | 23 | 12 | 52.2% |
| 0.30–0.40 | 8 | 6 | **75.0%** |
| 0.40–0.75 | 16 | 10 | 62.5% |

Base rate 46.4%. **The highest score any holdout frame reached is 0.691**, so the auto-accept branch
can never fire. And auto-rejecting below 0.40 would discard 124 of 140 frames containing **55 of the
65 potholes — 85% of every in-domain positive available**, which is precisely the data §4.4
identifies as the binding constraint.

**Generalisable form: a confidence threshold is a property of a (model, dataset, operating point)
triple, not of a pipeline.** Even the lowest band here is 32% positive against a 46% base rate,
so no cut point supports automatic rejection. Scores are useful for *ordering* a queue and unsafe as
a *decision rule*.

---

## 5. Data provenance

| | Archive (v1–v5) | RDD2022 subset (v5) |
|---|---|---|
| Source | Roboflow `sean-sattar/pothole-detection-lsz3t-17s0n` | Figshare DOI `10.6084/m9.figshare.21431547.v1` |
| Licence | CC BY 4.0 | **disputed — see below** |
| Images | 5,322 (3,728 / 1,060 / 534) | 1,660 used, from 18,140 |
| Boxes used | 3,728 pothole | 2,575 pothole |
| Imagery | landscape, mostly daylight, GoPro + Japan road frames | windshield-mounted smartphone; US, Japan, Czech Republic |
| Background images | **none** | 4,363 available, not used |
| SHA-256 | `2f3589bc7806…5d2157` | recorded per country archive in `runs/rdd2022-*.json` |

**Licence conflict, unresolved.** The RDD2022 GitHub README states CC BY-**SA** 4.0; the Figshare
record of deposit — which carries the citable DOI and a machine-readable licence field, and is
where the data was actually obtained — states **CC BY 4.0**. Whether trained weights constitute an
"adaptation" of training images is legally unsettled and jurisdiction-dependent.

Mitigations were adopted regardless: images are never redistributed, RDD-derived weights carry
`_rdd` in their identifier so they can be retired, and the non-RDD training path remains functional
so a clean-room retrain is always available.

**Availability note.** The per-country download URLs published in the README now return
`AccessDenied`, as do the RDD2020 archives. The working route is the combined 12.35 GB Figshare
archive, which contains the per-country zips as members.

**Class remapping.** RDD annotates road *damage*; this project detects road-surface *objects*. Only
two of its four codes map: `D40 → pothole`; `D00/D10/D20 (longitudinal / transverse / alligator
crack) → crack`. The three crack subtypes were collapsed because the platform does not report crack
type and splitting a data-poor class three ways is the §4.3 failure.

**Cracks were excluded from training.** The three countries carry **26,653 crack boxes against
2,575 potholes**. Importing a 10:1 imbalance would repeat §4.3 at an order of magnitude more scale.

---

## 6. Threats to validity

- **Holdout size.** 65 positives gives roughly ±5–6 percentage points. Differences smaller than
  that are not interpretable; the v1–v5 gap is 2 true positives and sits inside this band.
- **Single device.** 5,588 of 5,615 frames come from one phone. Nothing here separates model
  quality from the optical and mounting characteristics of that device.
- **Single annotator, single pass.** No inter-annotator agreement was measured. `frame_label`'s
  primary key is one row per frame, so the schema cannot currently express disagreement.
- **Enriched label set.** Stratified sampling means the 46% positive rate in the holdout is a
  property of the sampler, not of Toronto roads. Any prevalence claim requires reweighting.
- **Batch-size confound on v5** (§4.5).
- **RDD label semantics.** RDD annotates for pavement-condition assessment. Some D40 boxes are
  surface spalling a driver would never feel, so "pothole" is not strictly the same construct.
- **No production deployment.** Detection remains gated off; all numbers are offline. Only 4
  spatial clusters exist, because clustering requires 2 distinct devices and the corpus is
  effectively single-device.

---

## 7. Conclusions

1. **Offline dataset metrics can be uninformative to the point of being misleading.** A 0.03 spread
   in archive mAP50 concealed a 3.3× spread in field recall. Reporting only dataset metrics would
   have described v4 — the worst model built — as marginally the best.

2. **Adding hard negatives without corresponding positives degrades a rare-class detector
   monotonically.** Three successive additions, each individually defensible, each reduced recall.

3. **Introducing data-poor classes to absorb false positives can invert.** The new classes captured
   the target class instead: v4 localised twice as many potholes as it named.

4. **Domain-matched public data reverses such a collapse but did not surpass the baseline here**,
   plausibly because modality match does not imply scale match.

5. **A promotion gate on held-out, in-domain data is necessary and sufficient to prevent all four
   regressions.** v2, v3, v4 and v5 each pass archive metrics and each fail the holdout.

6. **Detector selection is objective-dependent.** Production scoring and active-learning sampling
   are optimised by different models from the same set of five.

7. **Confidence thresholds are not portable** between components of the same pipeline.

---

## 8. Instruments produced

| Artefact | Purpose |
|---|---|
| `migrations/010_frame_label.sql`, `013_frame_box.sql` | frame-level verdicts; box annotations with a reviewed marker |
| `scripts/label_frames.py` | blind, stratified or score-ranked labelling; box drawing; draft-then-submit |
| `scripts/detect_eval.py` | threshold sweep against ground truth; never writes |
| `scripts/export_labeled_frames.py` | DB → YOLO; refuses unreviewed frames as background |
| `scripts/ingest_rdd2022.py` | VOC → YOLO with class remap, area filter, licence + SHA-256 manifest |
| `scripts/promote_model.py` | matched-recall gate, `--objective fusion \| sampler` |
| `scripts/backfill_detection.py` | bulk scoring; populates the active-learning sampler |
| `scripts/vlm_eval.py` | VLM verifier vs ground truth: binary verdict, matched-recall curves, band tables, cached blend sweep; never writes |

**Test coverage.** 429 automated tests, including the coordinate round-trip between the two box
conventions, the class-count guard, and the queue-selection logic.

---

## 9. Current state and next steps

The corpus is fully scored by v1 (5,615/5,615, 1,006 recorded device-vs-server disagreements).
The unlabelled review pool, ordered by score:

| band | unlabelled frames |
|---|---|
| 0.30+ | **1,041** ← densest expected pothole yield |
| 0.15–0.30 | 943 |
| 0.05–0.15 | 1,254 |
| 0.00–0.05 | 2,002 |

Corpus maximum score 0.779.

**Immediate next step:** label the 0.30+ seam to obtain in-domain positives, then box them. This is
the only untried remedy for §4.4 that uses exactly-in-domain data.

**The verifier has never been measured.** `app/detection/hybrid_v1.py` has shipped since Phase
2.3b with 21 tests, every one against a *fake* verifier, so the second stage of the intended
detector is entirely unevaluated on this imagery — and these are windshield frames where a
pothole occupies well under 1% of frame area, which is where vision-language models are
weakest. `scripts/vlm_eval.py` and
[`phase-2.9-vlm-verification.md`](../phases/phase-2.9-vlm-verification.md) close that gap; the
measurement itself is still pending a configured provider.

**Open questions worth a controlled experiment.** Whether the v1–v5 gap survives removing the
batch-size confound; whether filtering RDD to boxes above ~1% of frame area closes the scale
mismatch of §4.5; and whether a multi-class model becomes viable once the pothole class is healthy
enough that §4.3's competition no longer dominates.
