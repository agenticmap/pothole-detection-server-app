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
[`detection-approach.md`](./detection-approach.md) nominates as "the natural place to mine
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

The default is a hypothesis, not a finding. `scripts/detect_eval.py --roi / --no-roi` reports
both against the labelled set so the default can be settled with evidence.

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
output and a troubleshooting table, is [`phase-2.7-runbook.md`](./phase-2.7-runbook.md). In
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
   a versioned `DETECTION_MODEL_ID`; fill in `docs/model-attribution.md`'s weights rows.

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
  [`app-capture-findings.md`](./app-capture-findings.md).
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
