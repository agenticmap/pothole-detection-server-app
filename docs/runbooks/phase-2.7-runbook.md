---
updated: 2026-08-24
---

# Runbook — turning server-side detection on

Procedure only. For *why* any of this is shaped the way it is, read
[`phase-2.7-detection-enablement.md`](../phases/phase-2.7-detection-enablement.md).

Every command runs **from the repo root**. Storage and model paths resolve against the working
directory, so running from `scripts/` will not work.

**Total time: about 30 minutes of your attention, plus one overnight training run.** Steps 0–4
get you a real `server_probability`; steps 5–7 are what make the number defensible.

---

## Before anything: three things not to do

| Don't | Because |
|---|---|
| Point `pytest` at `pothole_db` | The fixtures `TRUNCATE` every table. `conftest.py` refuses anything but `pothole_test`/`pothole_ci`, but do not disable that guard. Your 2916 collected frames are unrecoverable. |
| Set `DETECTION_ENABLED=true` before step 4 passes | An unproven model writes `server_probability` to every frame, and `detected_at` is set even on failure, so a bad run does not retry itself. Step 4's `--redo` can undo it, but prove the model first. |
| Extract the dataset into `files/` | `.gitignore` covers `files/*.zip`, **not** the ~10,700 files inside. This repo's history has already been rewritten once to strip large binaries, and that rewrite destroyed 4.4 GB (see `phase-2.6-hardening.md`). |

---

## Which interpreter — read this first

There are **two** virtualenvs and they are not interchangeable:

| venv | holds | used by |
|---|---|---|
| `.venv` | FastAPI, asyncpg, pydantic-settings, onnxruntime | every `scripts/*.py` in this runbook |
| `.venv-train` | ultralytics, torch, ~3 GB of CUDA wheels | the `yolo` commands only |

`python` on this machine resolves to conda, which has neither. A bare
`python scripts/label_frames.py` therefore fails with
`ModuleNotFoundError: No module named 'pydantic_settings'` — that is the wrong interpreter, not a
missing install. Either activate the venv for the session:

```powershell
.venv\Scripts\Activate.ps1
```

…or call it explicitly, which is what every command below assumes:

```bash
.venv/Scripts/python.exe scripts/label_frames.py ...
```

The training venv is kept separate on purpose: those CUDA wheels must never enter the server
environment, which is what gets containerised.

---

## Step 0 — One-time setup (~15 min, mostly downloading)

Training dependencies go in their **own** virtualenv. ~3 GB of CUDA wheels must never enter the
server environment.

```bash
python -m venv .venv-train
.venv-train\Scripts\Activate.ps1          # PowerShell;  source .venv-train/bin/activate elsewhere
pip install -r requirements-train.txt
```

Extract the dataset **outside the repo**:

```bash
mkdir C:\Users\satta\Desktop\Projects\_pothole-training
tar -xf "files\pothole detection.yolov8.zip" -C C:\Users\satta\Desktop\Projects\_pothole-training
```

> Use `files\pothole detection.yolov8.zip` (1.76 GB). The other archive,
> `AI Pothole Detection.yolov8.zip`, is a **truncated download** and cannot be opened by anything.

Expect: 5322 images — 3728 train / 1060 valid / 534 test — and a `data.yaml` with `nc: 1`.

---

## Step 1 — Get a model to prove the pipeline with (~5 min)

Download any pretrained **single-class pothole** ONNX (Roboflow Universe is easiest) and drop it in
`models/`. Its accuracy does not matter yet; step 2 is testing *our* code, not the model.

It must be a **raw** export. If you export it yourself:

```bash
yolo export model=best.pt format=onnx imgsz=640 opset=12 nms=False
```

`nms=False` is not optional — see the troubleshooting table.

---

## Step 2 — Prove the seam (~2 min)

This writes nothing to the database and is safe to run against the dev data.

```bash
python scripts/detect_eval.py --model models/<your>.onnx --limit 20 --sample \
    --annotate runs/detect-out
```

Expect output shaped like:

```
model=models/x.onnx  conf=0.25  iou=0.45  roi=on (0.45-0.9)
source=pothole_db (read-only)  frames=20

Scored 20 frames (0 failed)
  mean 0.203  median 0.118  max 0.622
  frames with >=1 box: 14
      range      n
  0.00-0.10      7  ##############
  ...
  Gray zone [0.4, 0.75]: 3 of 20 frames (15.0%)
Top-scoring frames (eyeball these ...)
  p=0.622  device=0.481  boxes=2   79ab03b4-...
```

**Then actually open the annotated JPEGs.** Filenames sort by score, and `runs/` is gitignored so
they stay out of the repo. The acceptance test is visual: boxes must sit on road surface, not on
sky, trees or the hood. If they are systematically offset or inverted, stop — that is a coordinate
bug, not a model problem.

Compare both geometries while you are here:

```bash
python scripts/detect_eval.py --model models/<your>.onnx --limit 20 --sample --no-roi
```

---

## Step 3 — Label ~300 frames (~25 min of clicking)

```bash
python scripts/label_frames.py --count 300 --by "s.sattar"
```

Expect:

```
database: pothole_db   labeled_by: s.sattar
queued 300 frames across 19 strata (device-p decile x day/night)

  open http://127.0.0.1:8020/   (Ctrl-C when finished)
```

Keys: `1` pothole, `0` not a pothole, `u` can't tell, `j`/`k` to move without labelling,
`b` to toggle the phone's own boxes.

Two things to hold in mind while labelling:

- **Leave the boxes off** until after you have decided. They are hidden by default on purpose: if
  you look first, you end up scoring the model's opinion rather than the road.
- **Use `u` freely.** A night frame with rain on the glass and no visible road surface is genuinely
  unratable, and marking it so is more useful than guessing. Those are excluded from precision and
  recall but counted and reported separately.

It is resumable — Ctrl-C and re-run whenever. Progress prints per label.

---

## Step 4 — Backfill (~15–25 min unattended)

Dry run first; it touches nothing:

```bash
python scripts/backfill_detection.py --model models/<your>.onnx --dry-run
```

Then for real:

```bash
python scripts/backfill_detection.py --model models/<your>.onnx
```

Expect progress at roughly 3–6 frames/s on CPU, then a comparison block:

```
Scored 2916 frames in 700s (4.2/s)

After:
  frames 2916   detected 2916   scored 2916   failed 0

Server vs device, over frames both scored:
  mean probability     device 0.1583   server 0.2011
  server scored higher 1804, lower 1085
  at or above conf 0.25: device 646, server 912
  in the VLM gray zone [0.4, 0.75]: 431
  model_disagreement rows: 588 (threshold 0.3)

Cleared processed_at so fusion re-scores existing pairs (UPDATE 2916).
```

The API stays responsive throughout — inference runs on a worker thread. If you want to confirm,
poll `/health` in another terminal while it runs.

The last line matters: it makes the next fusion tick re-score the 2158 existing pairs with the
server probability instead of the phone's. `fusion_pair` must stay at 2158 rows afterwards — it
was 1842 until Phase 2.2d's wider window and cost ranking re-paired them (see
[`phase-2.2d-runbook.md`](./phase-2.2d-runbook.md)) — because it
upserts; if it grows, something is wrong.

---

## Step 5 — Measure, and pick a threshold (~2 min)

```bash
python scripts/detect_eval.py --model models/<your>.onnx --labels
python scripts/detect_eval.py --model models/<your>.onnx --labels --no-roi
```

Expect a table per run:

```
  Labelled: 274 definite (61 pothole, 213 not) + 26 unsure

   thresh    TP    FP    FN    prec  recall      F1
     0.05    58   171     3   0.253   0.951   0.400
     ...
     0.45    31    18    30   0.633   0.508   0.564

  Best F1 0.564 at threshold 0.45  -> candidate DETECTION_CONF_THRESHOLD
```

Record **both** tables (crop on and off) in `phase-2.7-detection-enablement.md`, and say which
geometry won. If ROI-off wins, flip `DETECTION_ROI_ENABLED` and write down that the hypothesis was
wrong — that is a result, not a failure.

Read the numbers with two caveats in mind, both documented in `app-capture-findings.md`:

- The labelled set is **deliberately enriched** by stratified sampling, so its positive rate is not
  the real-world rate. Precision at a threshold is comparable across models; prevalence is not.
- Frames only reach the server if the phone's own model already fired above its record-time floor,
  so this is not a random sample of road either.

---

## Step 6 — Fine-tune properly (~4–7 h, unattended)

In the **training** venv:

```bash
yolo detect train model=yolo11s.pt data=<extracted>/data.yaml imgsz=640 batch=8 \
     epochs=80 patience=15 amp=True project=runs name=pothole_v1 workers=2
yolo export model=runs/pothole_v1/weights/best.pt format=onnx imgsz=640 opset=12 nms=False
```

`batch=8` and the `s` size are the ceiling for a 4 GB RTX 3050 Ti. `yolo11m` will OOM.

Measured on this machine: 2.2 GB of the 4 GB at `batch=8`, ~3.6 it/s, ~2.4 min/epoch, so the
80-epoch run is ~3.2 h rather than the 4-7 h estimated above.

`workers=2` is **not optional here**, and its failure mode does not look like a worker problem.
Ultralytics defaults to `workers=8`; on Windows each worker is a process importing torch, and the
pinned-memory allocation for the host-to-device copy then fails at batch 0/466 with

```
fatal   : Memory allocation failure
RuntimeError: CUDA error: unknown error
```

That reads as an out-of-VRAM error and is not one -- the GPU is barely touched at that point, and
the identical run with `workers=2` sits at 2.19 GB and proceeds normally. Do not respond to it by
lowering `batch`.

Check Roboflow's `data.yaml` paths point at where the archive actually unpacked — its relative
paths assume its own layout, and a wrong path looks like a model problem.

Then repeat steps 2, 4 (with `--redo`), and 5 with the new export, and report mAP50 / mAP50-95 on
the 534-image test split, labelled clearly as **dataset** metrics.

---

## Step 6b — Box the distractors, and train v4 (Phase 2.7b)

Steps 1–6 produce a single-class model whose recall *falls* every time it is given more hand
labels — 0.708 → 0.431 → 0.354 across v1/v2/v3. The cause and the fix are in
[`phase-2.7b-road-surface-classes.md`](../phases/phase-2.7b-road-surface-classes.md); briefly, hand-labelled
negatives are manholes and tar seals, and with one class the model can only explain them by
suppressing the features a pothole shares with them.

**Review the training negatives for boxes** (~35–45 min). Scoped by `--ids` to the 200 frames
already in the training split, so the 140-frame holdout the earlier models were measured on is
untouched and v4 stays comparable to them:

```bash
.venv/Scripts/python.exe scripts/label_frames.py --box --ids runs/negatives-train-ids.txt --by "s.sattar"
```

Keys: `1`–`5` pick the class (pothole / manhole / grate / patch / crack), drag to draw,
click to select, `Del` to remove, `Enter` to save a draft and advance, **`s` to submit**.
Move back and forward with the **arrow keys** (or `k`/`j`); `Home`/`End` jump to either end.
Revisiting a frame reloads the boxes you drew on it.

Submit signs off the frame on screen as well as every earlier draft, so the last frame in a queue
is never stranded. The exception is a frame you arrived on with a `Shift`-move and did not touch:
those stay out, because an untouched frame signs off as "reviewed, genuinely clean" and becomes a
background image in the training set.

**Nothing is final until you press `s`.** `Enter` writes the boxes to the database immediately, so a
crash or a stray Ctrl-C costs nothing — but it leaves the frame a *draft*: `boxed_at` stays NULL, so
it is still editable and the exporter still cannot see it. Move back with `k`, fix anything, and
submit when the batch looks right. An interrupted session re-adopts its drafts next run; the only
kind that cannot be recovered is a zero-box draft, since "I looked and there is nothing here" leaves
no trace until it is submitted.

The queue holds **outstanding work only**: a frame leaves it the moment you submit, so finished
work never sits between you and the next real frame. `--review` is the other half — it queues *only*
submitted frames, for checking or correcting a pass:

```bash
.venv/Scripts/python.exe scripts/label_frames.py --box        --ids runs/negatives-train-ids.txt --review
```

Run **one session at a time**. Each holds its own snapshot taken at startup, so two sessions will
show each other stale counts. If one does go stale, `r` re-reads the queue in place instead of
making you restart.

Botched a pass? Un-submit those frames and they return to the queue — the marker is cleared, the
boxes are kept:

```bash
.venv/Scripts/python.exe scripts/label_frames.py --box        --ids runs/negatives-train-ids.txt --reset-reviewed
```

**Box crack REGIONS, not crack LINES.** Cracking is the one class here with no natural
compact extent — a hairline crack boxes to a sliver of almost entirely undamaged asphalt, and
training on that teaches the model that the asphalt *is* the class. That is the v2/v3 recall
failure aimed at a new target. Prefer the cracked patch: alligator or block cracking, a crazed
area. A warning appears for boxes thinner than about 1:6 and is repeated in the terminal; it
never blocks the save, because a genuinely thin region is occasionally right.

**Press `Enter` even on a frame you drew nothing on.** That records "reviewed, genuinely clean" in
`frame_label.boxed_at`, which is what separates a real background image from a frame nobody opened.
The exporter refuses to ship the latter as training data — by design, since doing exactly that is
what broke v2 and v3.

**Export, then train.** The exporter writes `data.yaml` itself, from the same class list the decoder
reads, so the dataset and the model cannot drift apart:

```bash
.venv/Scripts/python.exe scripts/export_labeled_frames.py \
       --dest <dataset>/train --no-roi --ids runs/negatives-train-ids.txt
```

Check its per-class histogram before spending hours on a GPU: a class with two boxes in it will not
learn, and it is cheaper to notice now.

```bash
yolo detect train model=yolo11s.pt data=<dataset>/data.yaml imgsz=640 batch=8 \
     epochs=80 patience=15 amp=True project=runs name=pothole_v4 workers=2
yolo export model=runs/pothole_v4/weights/best.pt format=onnx imgsz=640 opset=12 nms=False
```

Single stage, identical to step 6's recipe — the only change is that those 200 frames now carry
boxes instead of empty label files. `workers=2` and the never-resume rule below still apply.

**Evaluate against the unchanged holdout**, and set `--classes` so the decoder's class map matches
the model's:

```bash
.venv/Scripts/python.exe scripts/detect_eval.py \
       --model runs/pothole_v4/weights/best.onnx --labels --conf 0.05 \
       --exclude-ids runs/negatives-train-ids.txt --classes pothole,manhole,grate,patch,crack
```

Read it at **matched recall**, not matched threshold and not on F1 — fusion consumes the visual term
as a modifier on a sensor verdict, so a detector that rarely speaks but is right when it does beats
a noisy one. Success is **pothole recall recovering toward v1's 0.708 while precision stays above
v1's 0.568**. Manhole AP will look bad; that class is data-poor by construction and its job is to
give the model an alternative hypothesis, not to be accurate.

Before turning a multi-class model on, set the class map in `.env` — the decoder rejects a model
whose class count disagrees with it, at startup rather than per frame:

```
DETECTION_CLASS_NAMES=pothole,manhole,grate,patch,crack
DETECTION_PRIMARY_CLASS_ID=0
```

---

## Step 7 — Turn it on

In `.env`:

```
DETECTION_ENABLED=true
DETECTION_BACKEND=onnx
DETECTION_MODEL_PATH=models/<your>.onnx
DETECTION_MODEL_ID=yolo11s_pothole_v1
DETECTION_CONF_THRESHOLD=<from step 5>
```

Restart. Expect one line at startup:

```
INFO  Detection backend 'onnx' ready (detection.onnx_yolo_v2).
```

If instead you see a single `ERROR ... could not be built`, the model did not load and the job was
**not** registered — the API still serves, and frames stay unscored. Fix and restart.

Finally, fill in the weights rows of [`model-attribution.md`](../reference/model-attribution.md). A
`server_model_id` in the database that cannot be traced to a dataset and a licence is not
auditable.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Detector output (1, 300, 6) is not the raw Ultralytics [1, 4+nc, N] layout` | The export has NMS baked in. Re-export with `nms=False`. The error prints the exact command. This guard exists because a wrong layout otherwise decodes into plausible-looking boxes rather than failing. |
| `Detector output (1, 8400, 5) ...` | Transposed export. Same fix. |
| `--labels needs the frame_label table (migration 010)` | Run `scripts/label_frames.py` once — it applies migrations. `detect_eval.py` is deliberately read-only and will not migrate. |
| `Refusing to label against 'pothole_test'` | `DATABASE_URL` points at the test database, which every test run wipes. Point it at the database with the real frames. |
| Dashboard suddenly empty; logins fail | A `pytest` run truncated `pothole_test`. Recreate the two accounts with `scripts/create_staff.py` and re-run `scripts/seed_demo.py --reset`. This is expected, not a bug — see `dashboard/README.md`. |
| `{"detail":"Missing required header: Accept-Version"}`, HTTP 400 | Your `curl` is missing `-H 'Accept-Version: v1'`. Every `/api/v1/*` call needs it. A correct login looks broken without it. |
| `uvicorn` fails: port already allocated | `docker compose up` also serves the API on 8000. Use one or the other. The demo instance conventionally runs on 8010. |
| `RuntimeError: DataLoader worker (pid(s) N) exited unexpectedly`, mid-run | Host memory, not the dataset. Ultralytics runs **validation** at `batch*2` with `workers*2` (`models/yolo/detect/train.py:54`, `engine/trainer.py:290`), so `workers=2` becomes 4 pinned-memory workers at every epoch boundary. Free RAM first: `docker compose stop`, close browsers and game launchers. `workers=0` loads in-process and cannot fail this way, at a speed cost. |
| Losses go `nan` immediately after `resume=True` | **Do not resume on this setup -- restart instead.** Resuming a run that died mid-epoch restored optimizer/AMP state that diverged: every loss column went `nan` from the resumed epoch onward, AMP's scaler then skipped every step so the weights froze, validation metrics repeated identically, and `patience` ended the run looking like a clean finish. The result was `last.pt` with NaN in **417 of 418** tensors. `amp` is not in the resume-overridable list (`imgsz`, `batch`, `device`, `close_mosaic`), so there is no safe resume. Check `results.csv` for `nan` before trusting any resumed run. |
| Training dies at batch 0 with `fatal   : Memory allocation failure` then `RuntimeError: CUDA error: unknown error` | Not VRAM, despite appearances. Ultralytics' default `workers=8` spawns eight torch-importing processes on Windows and the pinned-memory allocation fails. Add `workers=2`; do not lower `batch`. |
| `ValueError: detection_model_path is required` | `DETECTION_BACKEND=onnx` with an empty `DETECTION_MODEL_PATH`. |
| Detection job silently never runs | `DETECTION_ENABLED=true` but `DETECTION_BACKEND=none`. Startup logs one ERROR saying exactly this. |
| Container: model not found | The image copies only `app/` and `migrations/`. `docker-compose.yml` mounts `./models` read-only; on a fresh clone the directory must exist (it does — `models/README.md` is tracked for that reason). |

### Useful queries

```sql
-- progress
SELECT count(*) total, count(detected_at) detected, count(server_probability) scored
FROM asset_frame;

-- did fusion re-score?  (must stay at 2158 rows)
SELECT count(*), round(avg(fused_confidence)::numeric,4) FROM fusion_pair;

-- label progress
SELECT label, count(*) FROM frame_label GROUP BY label ORDER BY label;
```

Run the full server suite any time with:

```bash
DATABASE_URL=postgresql://<user>:<pass>@localhost:5433/pothole_test python -m pytest -q
```

Expect `339 passed`.
