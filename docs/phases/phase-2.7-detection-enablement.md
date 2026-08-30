---
updated: 2026-08-24
---

# Phase 2.7 — Server-side detection enablement

> Status: **Groundwork complete; blocked on a model.** Everything needed to turn detection on,
> prove it, measure it and roll it into fusion is built and tested. What does not exist is a
> `.onnx` file. The remaining steps are the ones only a model can unblock — export it, run
> `scripts/detect_eval.py`, label ~300 frames, backfill, pick a threshold.

## Why this phase exists

Phase 2.3 shipped the detection *seam* — a `FrameDetector` protocol, ONNX/HTTP/hybrid backends,
a locked scheduled worker, `model_disagreement`, and fusion's
`COALESCE(server_probability, device_probability)` — and shipped it gated off because no model
existed. Two collection drives later, the natural assumption was that only the flag was left.

The data said otherwise. Three findings reframed the work:

**1. There were no labels, anywhere.** 2916 real frames, all with a `device_probability`, and
**0 of 2916** with a `server_probability` or `detected_at`: server-side detection had never run
on a single frame. `asset_observation.visual_confirmed` — the only ground-truth slot in the
schema — was NULL on all 2728 rows, and `model_disagreement`, which
[`detection-approach.md`](../architecture/detection-approach.md) nominates as "the natural place to mine
tuning examples", had 0 rows and could not have had any.

**2. The on-device signal is close to noise on this data.** Median `device_probability` 0.118,
p99 0.68. Opening the three highest-scoring real frames:

| device p | what is actually in the frame |
|---|---|
| 0.811 | night, rain on the windshield, headlight glare — no visible road surface |
| 0.773 | clean daytime residential street, **no pothole** |
| 0.739 | clean daytime street, **no pothole** — a manhole cover and a painted crosswalk centre-frame |

Shadows, manhole covers, wet patches and lane markings are exactly the false-positive classes
`detection-approach.md` predicted, which is what the Stage-2 VLM verifier was designed for. The
consequence for this phase: **dataset mAP would have been a misleading headline number**, so the
phase added real ground truth instead of reporting one.

**3. The geometry is off-distribution.** Uploads are 480×640 **portrait** windshield frames,
~27 KB (min 10, max 71). The top half is sky and trees; the hood occupies the bottom ~15%; the
road surface is a narrow, shallow-angle band. The training archive is landscape, road-facing
GoPro/Japan imagery. Letterboxing a portrait frame to 640² spends most of the budget on regions
where a pothole cannot be.

And **`app/detection/onnx_v1.py` had never executed** — no test imported `onnxruntime`, every
test injected a stub. Its letterbox, `[1, 4+nc, N]` decode, NMS and box back-mapping were
unverified code about to write 2916 rows.

## What shipped

### 1. The ONNX decoder is now verified — without a model

`tests/test_onnx_detector.py` (18 tests) injects a fake `InferenceSession` through
`monkeypatch.setitem(sys.modules, "onnxruntime", ...)`, so the arithmetic is checked exactly and
the suite needs no weights file and no onnxruntime install. It pins the coordinate chain (a box
at letterbox `(320, 320, 48, 64)` on a 480×640 frame must come back as normalized
`(0.45, 0.45, 0.10, 0.10)`), NMS collapse, conf-threshold filtering, class argmax, frame
clamping, and portrait *and* landscape inputs.

**A layout guard was added** (`_check_layout`). A wrong export does not raise — it decodes into
plausible boxes, which is the worst possible failure for a scoring pipeline. Raw Ultralytics
output is `[1, 4+nc, N]` with `N` in the thousands, so the channel axis is the small one. Both
`[1, 300, 6]` (exported with `nms=True`) and `[1, N, 4+nc]` (transposed) now raise with the
correct re-export command in the message.

### 2. ROI crop — configurable, and measured

`DETECTION_ROI_ENABLED` / `_TOP` / `_BOTTOM` (default on, 0.45–0.90 of frame height, full width
always kept). Cropping to the road band before letterboxing puts **1.78× more road pixels** into
the 640² input — measured, not asserted:

| | road-band pixels in the model input |
|---|---|
| ROI off | 138,240 |
| ROI on (0.45–0.90) | 245,760 |

Boxes are always returned in **full-frame** coordinates, so nothing downstream knows or cares
whether the crop ran. `_to_detection` takes the offset explicitly for that reason, and
`test_roi_crop_shifts_the_box_by_the_crop_offset` feeds the identical output tensor with the
crop on and off to pin the difference — forgetting the offset hop would shift every box up by
288 px, silently.

~~The default is a hypothesis, not a finding.~~ **Settled 2026-08-25: the crop wins, keep it on.**
Measured against 340 definite labels with `yolo11s_pothole_v1`, at both detector thresholds:

| geometry | best F1 | at detector `--conf` |
|---|---|---|
| **ROI on (0.45–0.90)** | **0.382** | 0.05 |
| ROI off | 0.315 | 0.05 |
| ROI on | 0.279 | 0.25 |
| ROI off | 0.242 | 0.25 |

The crop is worth about +0.07 F1 at matched settings, in the direction the pixel-count argument
predicted. `DETECTION_ROI_ENABLED` stays `true`.

### 3. `server_detections` now matches `device_detections`

The two halves of one column family disagreed on keys, origin **and** units:

| | shape |
|---|---|
| Device (verified against `pothole_db`) | `{"bbox": {x,y,w,h} normalized corner-origin, "label", "class_id", "confidence"}` |
| Server, before | `{"conf": float, "xywh": [cx,cy,w,h] absolute pixels, centre-origin}` |

Nothing consumed `server_detections` — no API field exposes it (`FrameDetail` carries only the
scalar `server_probability`), and the dashboard draws no boxes — so this was the free moment to
fix it, before the Phase 3 flywheel inherited the fork. Both now emit the device shape.

`hybrid_v1.py::_crop` was hard-coupled to the old shape and moved with it. That path had **zero
coverage** (every hybrid test set `crop=False` to avoid needing Pillow); it now has seven tests
covering the bbox union, the margin, frame clamping, scale-independence, non-`bbox` entries such
as `_vlm_verdict`, and the degenerate-box guard. Using normalized coordinates has a side
benefit: the crop is correct whether or not Stage 1 applied an ROI crop.

### 4. Ground truth: `frame_label` (`migrations/010`)

Frame-level binary labels — `1` pothole / `0` not / `-1` unsure — keyed one row per frame so
re-labelling is an upsert and no frame can be double-counted.

**Frame-level, not boxes**, because everything downstream consumes a scalar: `app/fusion/service.py`
takes `COALESCE(server_probability, device_probability)` and never looks at the boxes. So the
question that decides whether detection works is "does this frame contain a pothole", and
answering it for a few hundred frames is minutes rather than hours. The `1/0/-1` values match
roadmap §3.1's planned `event_label` so the flywheel's `POST /api/v1/labels` can adopt them
unchanged. `-1` is kept rather than dropped: an unreadable night frame is a real operating
condition, and discarding it would bias measured precision upward.

`scripts/label_frames.py` serves a keyboard-driven page on localhost. Two deliberate choices:

- **The device's boxes are hidden by default** (`b` toggles). Showing a model's guess while a
  human decides ground truth anchors the human to the model, and the labels would then flatter
  whatever produced them.
- **The sample is stratified, not random.** With a median `device_probability` of 0.118, a random
  300 would be ~300 negatives and would measure recall not at all. The queue round-robins across
  (device-p decile × day/night); a 300-frame queue came out as 42/42/42/40/40/40/35/18/1 across
  deciles 0–8 and 159 day / 141 night. **Consequence to remember: the positive rate in the
  labelled set is not an estimate of the real-world positive rate.** It is a deliberately
  enriched evaluation set, and any prevalence claim needs reweighting.

The database guard is the **inverse** of `tests/conftest.py`'s: the labelling tool refuses
`pothole_test`/`pothole_ci` unless `--allow-test-db` is passed, because the fixtures TRUNCATE
every table and would destroy the labels. It writes `frame_label` and nothing else.

### 5. Offline evaluation: `scripts/detect_eval.py`

Runs a model over stored frames and **writes nothing** — safe against the dev database. Config
comes from CLI flags, not `DETECTION_*` env vars, so an evaluation run cannot silently be
scoring with a different model than the command says. It reports a probability histogram, the
top-scoring frames for eyeballing, gray-zone occupancy, and — with `--labels` — a
precision/recall/F1 table at every threshold with the best-F1 operating point called out as the
candidate `DETECTION_CONF_THRESHOLD`. `--annotate` writes boxes back onto the JPEGs, filenames
sorted by score; that is the visual acceptance test for the coordinate maths.

It stays read-only even when `frame_label` is absent: it probes `to_regclass` and drops the join
rather than migrating.

### 6. Backfill and re-fusion: `scripts/backfill_detection.py`

Drives the real `run_detection_job` through its existing `detector=` injection point, in chunks,
with progress and a frames/sec rate. Reports server-vs-device means, which direction the server
moved, counts either side of the threshold, gray-zone occupancy and `model_disagreement` rows.

**One worker fix was required first.** `app/detection/service.py` called `detector.detect(jpeg)`
— synchronous CPU inference — directly inside the async loop. At `detection_batch_size=200` and
~0.3 s/frame that is a **~60 s API stall every two minutes**: every request in that worker
blocks. It is now `await asyncio.to_thread(detector.detect, jpeg)`.
(`frame_service.py:163-168` argues the blocking *disk read* is fine off the request path — true
of the read, not of a coroutine that never yields.)

**Re-fusion closes a documented hole.** 1842 `fusion_pair` rows were scored from
`device_probability`, and since fusion only selects frames `WHERE processed_at IS NULL` they
would have kept that score forever — `phase-2.3-detection-plan.md`'s out-of-scope item
*"re-fusing frames detected after they were already paired"*, which a 2916-frame backfill turns
from theoretical into universal. It turned out nearly free: `_UPSERT_PAIR_SQL` already upserts on
`(event_client_id, frame_client_id)`, so `UPDATE asset_frame SET processed_at = NULL WHERE
detected_at IS NOT NULL AND server_probability IS NOT NULL` makes the next fusion tick re-score
in place with no duplicate rows. `--reset-fusion` (default on) does it;
`test_detection_backfill_can_rescore_existing_pairs` pins both halves — that a processed frame is
*not* re-paired on its own, and that after the reset the pair count stays at one while the
confidence moves.

### 7. Misconfiguration is now loud once, not silent forever

A bad `DETECTION_MODEL_PATH` raised inside `get_detector()` at `service.py:55` — *outside* the
per-frame `try` — so it threw every two minutes with nothing marking the service unhealthy and
no detection probe in `/health`. `start_scheduler` now constructs the detector once at boot
(`_detector_loads()`), and on failure logs one ERROR naming the backend, the reason, and the
consequence — *"frames stay unscored and fusion falls back to device_probability"* — then skips
registering the job. It deliberately does **not** fail the boot: a model that will not load is a
degraded background job, not a reason to take ingestion down with it. `DETECTION_ENABLED=true`
with `DETECTION_BACKEND=none` is caught by the same path.

### 8. Packaging and hygiene

- `requirements.txt` had `onnxruntime` and `Pillow`; `pyproject.toml`'s `dependencies` **omitted
  both**, so `pip install -e .` produced an install that could not run `DETECTION_BACKEND=onnx`
  at all. Added, with a note to keep them in sync.
- `requirements-train.txt` (new) holds `torch` / `ultralytics` / `onnx` / `onnxslim` for a
  **separate** virtualenv. ~3 GB of CUDA wheels must never enter the server image.
- `models/` is now a tracked directory (README only, weights gitignored) so
  `docker-compose.yml`'s new read-only `./models:/opt/server/models:ro` mount works on a fresh
  clone. Without the mount, `backend=onnx` in the container could not find a model — and per §7
  that now produces one clear ERROR.
- `.gitignore` also covers `runs/`, `*.onnx`, `*.pt`, `.venv-train/`. **Extract the dataset
  archives outside the repo** — `.gitignore` covers `files/*.zip`, not the ~10,700 files they
  unpack into, and this repo's history has already been rewritten once to strip large binaries.

## Verification

- **290 tests pass**, up from 264: +18 ONNX decoder, +7 hybrid crop, +1 re-fusion.
- Migration 010 applies cleanly to `pothole_test` and to `pothole_db` (10 rows in
  `schema_migrations`; `asset_frame` untouched at 2916).
- `scripts/label_frames.py` end-to-end against the real frames: page 200, queue 200 with 300
  stratified frames, frame images 200 (`image/jpeg`), unknown id 404, traversal probe 404, a
  `POST /api/label` persisted and read back, `label: 9` rejected 400. The smoke-test label was
  then deleted — an unexamined label would have corrupted the eval set.
- The test-DB guard refuses `pothole_test` with exit 2.
- `_detector_loads()` verified for both failure modes (unloadable model, `backend=none`).
- ROI gain of 1.78× measured directly from the fed tensor.
- `ruff check` clean across every touched file.

## What is left, and what it needs

Only the steps a model unblocks. The copy-pasteable version of the list below, with expected
output and a troubleshooting table, is [`phase-2.7-runbook.md`](../runbooks/phase-2.7-runbook.md). In
order:

1. **Get a Stage-1 model.** Fastest: a pretrained single-class pothole ONNX from Roboflow
   Universe, purely to prove the seam. Then fine-tune — see the recipe below.
2. **Prove it:** `python scripts/detect_eval.py --model models/x.onnx --limit 20 --annotate out/`.
   Boxes must land on road surface. This is the acceptance test for §1.
3. **Label ~300 frames:** `python scripts/label_frames.py --count 300 --by "<you>"`.
4. **Backfill:** `python scripts/backfill_detection.py --model models/x.onnx` (`--dry-run` first).
5. **Measure and choose a threshold:** `detect_eval.py --labels`, with and without `--roi`.
   Record both tables here, plus the chosen `DETECTION_CONF_THRESHOLD` and why.
6. **Turn it on:** `DETECTION_ENABLED=true`, `DETECTION_BACKEND=onnx`, `DETECTION_MODEL_PATH`,
   a versioned `DETECTION_MODEL_ID`; fill in `docs/reference/model-attribution.md`'s weights rows.

### Fine-tuning recipe (runs outside this repo)

```
python -m venv .venv-train && .venv-train\Scripts\Activate.ps1
pip install -r requirements-train.txt

# Extract OUTSIDE the repo. .gitignore covers files/*.zip, not the 10,700 unpacked files.
mkdir C:\Users\satta\Desktop\Projects\_pothole-training
tar -xf "files\pothole detection.yolov8.zip" -C C:\Users\satta\Desktop\Projects\_pothole-training

yolo detect train model=yolo11s.pt data=<extracted>/data.yaml imgsz=640 batch=8 epochs=80 \
     patience=15 amp=True project=runs name=pothole_v1
yolo export model=runs/pothole_v1/weights/best.pt format=onnx imgsz=640 opset=12 nms=False
```

Three traps, in the order they bite:

- **`batch=8` is the ceiling.** The dev GPU is a 4 GB RTX 3050 Ti laptop part; `yolo11m` at 640
  will OOM. `s` is the honest limit for this machine — reach for a cloud GPU if `m` is wanted.
- **Roboflow's `data.yaml` carries relative `train:`/`val:` paths** that assume its own layout.
  Check them against where the archive actually unpacked before blaming the model.
- **`nms=False`.** See §1 — with NMS baked in the graph is `[1, 300, 6]` and would decode to
  garbage if the guard were ever removed.

Report mAP50 / mAP50-95 on the archive's own 534-image test split, and label it as **dataset**
metrics, kept separate from the real-frame numbers. They measure different distributions and
conflating them is how the false positives above would get missed.

## Measured: the first real model, and why it is not enabled

`yolo11s_pothole_v1` (see [`model-attribution.md`](../reference/model-attribution.md)) scored against 375 hand
labels — 65 pothole, 275 not, 35 unsure — on 2026-08-25. Sweeping the *frame* threshold with the
detector's own box threshold at 0.05:

| thresh | TP | FP | FN | prec | recall | F1 |
|---|---|---|---|---|---|---|
| 0.05 | 46 | 130 | 19 | 0.261 | 0.708 | **0.382** |
| 0.10 | 34 | 93 | 31 | 0.268 | 0.523 | 0.354 |
| 0.20 | 23 | 58 | 42 | 0.284 | 0.354 | 0.315 |
| 0.25 | 18 | 46 | 47 | 0.281 | 0.277 | 0.279 |
| 0.40 | 10 | 30 | 55 | 0.250 | 0.154 | 0.190 |
| 0.55 | 5 | 8 | 60 | 0.385 | 0.077 | 0.128 |
| 0.60 | 2 | 2 | 63 | 0.500 | 0.031 | 0.058 |

Three things to read off this, in order of how much they matter.

**1. Precision is flat.** It sits at 0.25–0.29 across the entire usable range and only rises where
recall has collapsed below 8%. Raising the threshold discards true and false positives at the same
rate, so the score barely ranks correctness. Against a base rate of 65/340 = 0.191 that is a lift
of roughly 1.4×: real, but weak.

**2. It still beats the phone, which is the relevant comparison.** The same sweep over
`device_probability` on the same labels gives precision 0.204–0.229 at *every* threshold against a
base rate of 0.204 — a lift of essentially 1.0. The on-device model's confidence carries almost no
information. So `COALESCE(server_probability, device_probability)` would be an upgrade.

**3. But it must not be switched on as-is, and the reason is arithmetic, not accuracy.** The model
returns **exactly 0.0** on 305 of 375 frames (81%) — no box above threshold. `logit` clamps 0.0 to
1e-6, i.e. −13.8155, which the blend reads as near-certain evidence *against* a pothole:

| `p_sensor` | `p_visual` | fused |
|---|---|---|
| 0.90 | 0.5 | 0.7500 |
| 0.90 | 0.26 | 0.6401 |
| 0.90 | **0.0** | **0.0030** |
| 0.52 | **0.0** | **0.0010** |
| 1.00 | **0.0** | **0.5000** |

A confident sensor pothole fused with a silent camera lands at 0.003. The `p_s = 1.0` row is worth
staring at: the two clamps are equal and opposite, so they cancel to *exactly* 0.5 — the value the
member gate's floor sits on.

The bug is the semantics of zero. A detector that finds no box has **not** observed the absence of a
pothole; it may not even have had road in the ROI. That is a missing modality, not negative
evidence, and it should reach the engine as `None` — which the engine already handles — rather than
as 0.0. Fixing that is a precondition for `DETECTION_ENABLED=true`, and it interacts with the
missing-modality shrinkage recorded in `phase-2.2d-pairing-search.md`, so the two want doing
together.

**The cheapest accuracy win is now available and was not before.** The 275 frames labelled *not a
pothole* are domain-matched negatives — real windshield, real night, real rain — and the training
archive contains **zero** background images. That was named in `model-attribution.md` as the
likeliest source of false positives, and false positives are exactly what the flat precision column
is made of.

### Mining the negatives: the v2 dataset, and the baseline it must beat

`scripts/export_labeled_frames.py` turns the hand-labelled `not a pothole` frames into YOLO background
images (an image with an empty label file). It exports **only** negatives: `frame_label` records a
verdict per frame, not a box, and a detector cannot learn a positive without coordinates. That is
convenient rather than limiting, because it leaves all 65 positives free for evaluation.

Of 275 negatives, **200 went to training and 75 are held out**, split on `md5(client_id)` so the
assignment is stable across re-runs — `hash()` is salted per process and would leak holdout frames
into training on the second pass. Backgrounds are **ROI-cropped** to 0.45–0.90, matching what the
detector is actually fed at inference. `detect_eval.py --exclude-ids` consumes the training list.

The v2 dataset (`_pothole-training-v2`) hardlinks the 5322 archive images, so it costs no disk and
leaves v1 byte-identical and reproducible; the only real files in it are the `neg-*` backgrounds.
Ultralytics confirms the composition: `3928 images, 200 backgrounds, 0 corrupt` — **5.1% background**,
against the ~10% its own guidance suggests.

**The baseline v2 has to beat**, `yolo11s_pothole_v1` on the 140-frame holdout (65 pothole, 75 not):

| thresh | TP | FP | FN | prec | recall | F1 |
|---|---|---|---|---|---|---|
| 0.05 | 46 | 35 | 19 | 0.568 | 0.708 | **0.630** |
| 0.15 | 28 | 19 | 37 | 0.596 | 0.431 | 0.500 |
| 0.25 | 18 | 9 | 47 | 0.667 | 0.277 | 0.391 |
| 0.55 | 5 | 1 | 60 | 0.833 | 0.077 | 0.141 |

> **Do not compare these numbers to the 340-frame table above.** Precision looks far better — 0.568
> against 0.261 at the same threshold — purely because moving 200 negatives into training raised the
> holdout's base rate from 65/340 = 0.191 to 65/140 = 0.464. The like-for-like measure is **lift over
> base rate**: 1.37× on the full set, 1.22× here. Compare v2 to *this* table, and prefer lift to raw
> precision.
>
> The holdout is also small: 65 positives and 81 predictions at threshold 0.05 put roughly ±6
> percentage points on recall and ±5 on precision, so only sizeable movements mean anything. Both
> sides of this tension — 5.1% backgrounds is thin, and a 75-negative holdout is noisy — are fixed by
> the same thing, and it is not a code change: **2541 of the 2916 frames are still unlabelled.**

### v2: the negatives worked, and the crop choice spoiled the experiment

`yolo11s_pothole_v2` = v1's recipe on the v2 dataset (200 ROI-cropped backgrounds added), 80
epochs, 4.57 h. Measured on the same 140-frame holdout, at detector `--conf 0.05`, frame
threshold 0.05:

| model | geometry | TP | FP | prec | recall | F1 | lift over base |
|---|---|---|---|---|---|---|---|
| v1 | ROI on | 46 | 35 | 0.568 | 0.708 | **0.630** | 1.22x |
| v2 | ROI on | 28 | 11 | **0.718** | 0.431 | 0.538 | **1.55x** |
| v2 | ROI off | 36 | 18 | 0.667 | 0.554 | 0.605 | 1.44x |

**The backgrounds did their job**: false positives fell 35 -> 11, a 69% cut, and precision lift went
1.22x -> 1.55x. That is the effect they were mined for.

**But 39% of the true positives went with them** (46 -> 28), and F1 fell. The cause is a mistake in
how the negatives were exported, not in the idea:

- v2 on the **archive** test split is P 0.621 / mAP50 0.494, against v1's 0.623 / 0.512 — the same
  model, statistically speaking. Its ability to detect potholes in full images is intact.
- The recall collapse appears **only** on real frames with the ROI crop applied, and switching the
  crop off at inference recovers most of it (0.431 -> 0.554).
- For v1 the crop was clearly *better* (F1 0.382 vs 0.315). For v2 the preference **reverses**.

That reversal is the tell. The backgrounds were ROI-cropped and the archive positives were not, so
crop geometry correlated perfectly with class and the model took the shortcut: an ROI-shaped input
is background. Cropping to match inference was the right instinct applied to only one class, which
turned it into a label leak.

**The fix is the v3 dataset**: identical in every respect except that the negatives are exported
uncropped (`export_labeled_frames.py --no-roi`), so geometry carries no class signal and only content
differs. The train/holdout split is byte-identical to v2's — it is derived from `md5(client_id)` —
so v3's numbers drop straight into the table above.

The general lesson is worth keeping: **any preprocessing applied to one class and not the other is a
label, however sensible it looks in isolation.**

### v3: the crop confound was real and is fixed -- and it was not the cause

`yolo11s_pothole_v3` = v2's dataset with the negatives exported **uncropped**, so geometry no
longer predicts class. 80 epochs, 2.50 h, clean (`PIN_MEMORY=false`; see the runbook). Same
140-frame holdout, detector `--conf 0.05`, frame threshold 0.05:

| model | geometry | TP | FP | prec | recall | F1 | lift over base (0.464) |
|---|---|---|---|---|---|---|---|
| v1 | ROI on | 46 | 35 | 0.568 | **0.708** | **0.630** | 1.22x |
| v2 | ROI on | 28 | 11 | 0.718 | 0.431 | 0.538 | 1.55x |
| v2 | ROI off | 36 | 18 | 0.667 | 0.554 | 0.605 | 1.44x |
| v3 | ROI on | 23 | 8 | **0.742** | 0.354 | 0.479 | **1.60x** |
| v3 | ROI off | 14 | 7 | 0.667 | 0.215 | 0.326 | 1.44x |

**The crop hypothesis was half right.** It predicted two things. The first held: v2 preferred ROI
*off* (0.605 vs 0.538) while v1 and now v3 prefer ROI *on* (0.479 vs 0.326). Removing the crop from
the negatives restored the normal geometry preference, so the label leak was real and is gone.

**The second prediction failed.** Recall did not recover -- it fell further, 0.708 -> 0.431 -> 0.354.
So the crop never caused the recall loss. **The negatives themselves do.**

The likely mechanism, and it is worth stating because it inverts the intuition: v2's crop gave the
model a *shortcut*. It could satisfy the background class on geometry alone and never had to learn
what the pixels meant. v3 removed the shortcut, so it actually learned the content lesson -- and the
content lesson is harmful, because these negatives are **hard** negatives. They are manholes, tar
seals and patches: dark, roughly pothole-shaped, on road surface. Teaching "this is background"
generalises to suppressing real potholes that look like them. v3 is *better trained* on the same
data and *worse at recall* for exactly that reason.

**This confirms the labelling concern** raised when the manholes were marked `0`: the marking was
correct for a single-class `pothole` detector, but the resulting hard negatives cost recall. The
fix is not to relabel them as potholes -- it is to stop collapsing "clean asphalt" and "uneven
manhole" into one class.

`frame_label.note` was added here to record which is which, but it landed *after* these 375 labels
were made, so **every existing note is NULL** and the manholes cannot be found by query. Phase 2.7b
adds a box-review pass (`label_frames.py --box`) which is how they get identified; the note field
starts paying off from the next verdict session onward. See
[`phase-2.7b-road-surface-classes.md`](./phase-2.7b-road-surface-classes.md).

**What each model is actually good for.** The trade is monotone: every negative added buys
precision and costs more recall than it buys. By F1, **v1 is still the best model**. But F1 is the
wrong metric for this pipeline. Fusion consumes the visual term as a *modifier* on a sensor verdict,
and since `_CANDIDATE_COLUMNS` now reads a 0.0 as "no measurement" rather than "clean road",
**silence costs nothing new** -- to be precise, it falls back to `device_probability`, which is
exactly what fusion uses today, so a silent detector leaves `fused_confidence` unchanged from the
status quo rather than being literally "no evidence". Under that objective a detector that rarely
speaks but is right when it does beats a noisy one, and v3 dominates:

| threshold | v3 TP | v3 FP | precision |
|---|---|---|---|
| 0.25 | 10 | 1 | 0.909 |
| 0.30 | 8 | 0 | **1.000** |

Eight true positives and zero false positives is a usable confirmation signal. Note the sample size
before over-reading it: 8 detections put the 95% lower bound on that 1.000 near 0.63.

**v3 does not merely trade recall for precision against v1 -- it dominates it.** Matched on true
positives caught, v3 always pays fewer false positives:

| TP caught | recall | v1 thr | v1 FP | v1 prec | v3 thr | v3 FP | v3 prec |
|---|---|---|---|---|---|---|---|
| 23 | 0.354 | 0.20 | 12 | 0.657 | 0.05 | 8 | 0.742 |
| 14 | 0.215 | 0.35 | 8 | 0.636 | 0.15 | 4 | 0.778 |
| 10 | 0.154 | 0.40 | 6 | 0.625 | 0.25 | 1 | 0.909 |
| 8 | 0.123 | 0.45 | 4 | 0.667 | 0.30 | **0** | **1.000** |
| 5 | 0.077 | 0.55 | 1 | 0.833 | 0.35 | **0** | **1.000** |

v1's only real advantage is that it can *reach* recall above 0.354, which v3 cannot at any
threshold. Below that ceiling there is no operating point where v1 is preferable.

The practical consequence, over the 140-frame holdout: **v1 at its best-F1 threshold injects an
opinion on 58% of frames and is wrong about 43% of the time it speaks. v3 at 0.30 speaks on 5.7%
and was wrong zero times.** Since fusion can only be moved by a frame the detector speaks on, that
is the difference between adding noise to most pairs and adding signal to a few.

## Deliberately not in this phase

- **The VLM verifier.** `hybrid_v1.py` is built and now has crop coverage, but
  `VLM_VERIFY_LOW/HIGH` would be guesses until Stage 1 has been measured. `detect_eval.py` and
  the backfill both report gray-zone occupancy specifically to set them from data. The
  manhole/raindrop false positives above are its case, so this is the natural next step.
- Depth Anything v2 visual severity (`detection-approach.md` "Deferred (Phase C)").
- Plate/face redaction (`phase-2.6-hardening.md` §7 — "plates and faces are in the *pixels*").
- A review surface for `model_disagreement` (Phase 3 flywheel).
- Negative/background images from the drives added to the training set — the cheapest likely
  accuracy win, but it is a dataset change, not an enablement change.
- App-side camera geometry. The portrait, sky-heavy framing is an app-repo concern; §2 works
  around it server-side. What the collected data says about the capture path — and the
  recommended Phase 2.8 — is written up in
  [`app-capture-findings.md`](../research/app-capture-findings.md).
- 59 orphaned JPEGs under `storage/frames` with no `asset_frame` row, and 425 `demo-dev-*` files
  from `seed_demo.py` sharing the real storage root.
- Phase 2.6's remaining bulk, none of which is detection: shared rate limiter, per-IP limits,
  frame GC, storage budget, TLS, shadow-ban, stale clusters, `org_id` on new clusters.

## Expect this: still no clusters

Even with detection working end-to-end, **no real clusters will appear on the dashboard.**
`CLUSTER_MIN_DISTINCT_DEVICES=2` and there is only one substantive device — 2889 of 2916 frames
and 2630 of 2728 observations come from a single `device_id`; the second contributed 27 frames.
The single `asset_cluster` row in `pothole_db` is a hand-made `cluster_id='solo'` fixture, not a
produced cluster.

That is a data-collection gap, not a detection bug, and the fix is a second phone driving the
same roads — not a code change. It should not be read as this phase failing.
