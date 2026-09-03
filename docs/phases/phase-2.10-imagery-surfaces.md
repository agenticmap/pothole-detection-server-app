---
updated: 2026-09-03
---

# Phase 2.10 — Showing what the detector saw, and the first VLM that ever answered

Phase 2.7d built the labelling instrument. This phase answers the question that followed it —
*what is left on the imagery side?* — and the audit's answer was uncomfortable: **the imagery arm
is built almost everywhere and exercised almost nowhere.**

Three things were done. The new drive was scored, the console was made to show what the detector
produces, and a VLM answered a question about a frame from this corpus for the first time.

---

## 1. Two thousand frames nobody had scored

The corpus is **7,615 frames, not 5,615**. Two thousand of them, captured 31 Aug – 1 Sep, had
`detected_at IS NULL`: nothing had scored them, because `DETECTION_ENABLED=false` means nothing
scores new arrivals. Every `server_probability` in this database was written by
`backfill_detection.py`, never by the scheduled worker.

Worse, they were **invisible to the review queue's primary mode**. `label_queue.rank_by_score`
drops unscored frames by design — "sorting it as if it were 0.0 would bury real frames under
unscored ones" — so a whole fresh drive could not be reached from the score-ranked pass.

And **1,024 of those 2,000 (51%) sit within 25 m of previously-covered road.** That is the
repeat-route coverage the integration round's corroboration failure asked for, and it had arrived
unnoticed.

### Scored with v1, at the operating point the rest of the corpus used

```bash
python scripts/backfill_detection.py \
  --model models/yolo11s_pothole_v1.onnx --model-id yolo11s_pothole_v1 \
  --conf 0.05 --iou 0.45 --roi --roi-top 0.45 --roi-bottom 0.90
```

**`--conf 0.05` is the whole correctness story.** `backfill.log` records the original 5,615 as
scored at `conf=0.05`, but the flag defaults to `settings.detection_conf_threshold` = **0.25**.
Taking the default would have scored the new frames on a different operating point and silently
split the only score the review queue ranks on. `models/yolo11s_pothole_v1.onnx` was confirmed
byte-identical to `runs/pothole_v1/weights/best.onnx` by SHA-256 first (`16ceb147…1863`, matching
`model-attribution.md`).

The documented backfill hazards did **not** apply: `_CLEAR_DETECTED_SQL` has no `WHERE` clause but
runs only under `--redo`, which this did not pass. No `FUSION_ENABLED=false` window was needed.

### The new drive is materially better data

| | old 5,615 | new 2,000 |
|---|---|---|
| at or above 0.30 | 1,103 (20%) | **778 (39%)** |
| scored exactly 0.0 | 2,183 (39%) | **361 (18%)** |
| max score | 0.779 | 0.770 |
| mean device probability | 0.287 | **0.436** |

2,000 frames in 1,256 s, **0 failures**. The matching maxima confirm both halves sit on one
operating point. `model_disagreement` rows went 1,006 → **1,551**, and frames in the VLM gray zone
698 → **1,225**.

**The labelling seam went 1,041 → 1,819 outstanding at ≥ 0.30.** Ground truth untouched throughout:
375 labels / 65 positives / 43 boxes / 200 signed off.

---

## 2. The console showed almost none of it

`app/models/clusters.py` ships `server_boxes`, `device_boxes`, `vlm_verdict`, `server_model_id`,
`fused_confidence`, `delta_ms` and `delta_m` on every frame. The panel rendered **one number**:
`p = server_probability ?? device_probability`. So an operator could not tell the server's score
from the phone's, could not see where either detector looked, and — under the hybrid backend —
could not tell a detector score from a VLM blend. `device_boxes` were drawn **nowhere at all**,
including in review. A whole `VlmVerdictItem` model and a dedicated parser existed for a field that
reached **zero pixels**.

### The panel was destroying evidence

`.frame-thumb` was `aspect-ratio: 4/3; object-fit: cover` over a corpus of **portrait 480×640**
frames. `cover` scales to fill and crops the overflow: for a 0.75 image in a 1.333 box that keeps
the middle ~56% of the height and discards the top and bottom quarters — and on a forward-facing
capture **the road surface is in the bottom quarter**. It had been invisible precisely because
nothing drew boxes on top to expose the mismatch.

The fix is the pattern review already proved: a fixed-ratio **cell** that never crops, holding a
**stage** that carries the decoded image's own aspect ratio. The stage box and the rendered image
box are then the same rectangle *by construction*, every normalized coordinate is exact with zero
JavaScript, and the leftover space lands in the cell where no coordinate lives.

`FrameStage.fit()` moved that aspect-ratio step out of `ReviewModule` and into the stage. It had
been living in the one consumer that happened to need it, so the next surface to mount a frame
would have had to know to copy two lines or silently get every box wrong.

Measured in the browser at 1884×914: **stage box identical to image box on all 12 cells**,
`object-fit: fill`, stage ratio `480 / 640`, **38 detector boxes across 12 frames**.

### A confidently wrong number on the triage screen

`panel.ts` printed **"Corroborating passes: 0 passes"** for every cluster. `distinct_passes` was
declared in `types.ts` and consumed by the panel, while `_HEADER_SQL` never selected it and
`ClusterDetailResponse` never defined it. The column is exposed on the *public* path
(`app/models/potholes.py`) and not the staff one. The database says **1**.

`member_span_s` now renders as a judgement rather than a float — `migrations/015` calls it "the
diagnostic that exposed the problem in the first place", so a cluster whose members arrive within
seconds now says *"All observations within 1 s — one drive-past, not repeat corroboration."*

**The test fixture had to change to write a non-default value.** `distinct_passes` defaults to 0
and the panel read `?? 0`, so `0 == 0 ?? 0` — a fixture that omitted the column would have passed
against the broken code. That is exactly how the bug survived.

### The first `<dialog>` in the codebase

`showModal()` rather than `show()`, and not as a style choice. The modal path puts the element in
the **top layer**, so it renders above MapLibre's canvas and the detail panel regardless of
stacking context; it makes the rest of the document `inert`, which **is** the focus trap,
implemented by the browser rather than by a hand-rolled Tab cycle; and it gives `::backdrop` and
Escape-to-close for free.

**`--z-modal: 100` therefore stays unused.** The top layer supersedes z-index entirely. Inventing a
`z-index: var(--z-modal)` on a top-layer element would have been cargo cult, so the token keeps a
comment saying so.

**Three layout traps, all the same shape: a cap cannot do the job of a length.** A definite height
has to be handed down the whole chain or the innermost element resolves its percentage against
nothing.

| where | wrong | measured | right |
|---|---|---|---|
| `.frame-dialog` | `max-height: 92vh` | sized to content | `height: 92vh` |
| `.frame-dialog-inner` | `max-height: 92vh` | **792px inside a 1104px dialog** | `height: 100%` |
| `.frame-dialog-body` | implicit `auto` row | **640px inside a 672px area** | `grid-template-rows: minmax(0, 1fr)` |

Until all three, a 480×640 frame rendered at 480×640 inside a 1400px dialog. This is the
`max-height: 68vh` bug from 2.7d's UX pass, three times over in one component.

Result at 1200px tall: **713×951, 3.9× the thumbnail**, ratio preserved, boxes still identical.

### Server and device separated without hue

The five class colours belong to human boxes; seven hues on one photograph is not a palette. The
detector sets differ by **dash rhythm** and, load-bearingly, by an `srv` / `dev` prefix in the
label — a word survives greyscale and deuteranopia, a dash pattern is a mnemonic. Per-box
`confidence` reaches the screen for the first time anywhere.

The disagreement the console could never show is now visible on a single frame:
**`srv pothole 0.41` beside `dev pothole 0.27`.**

### The modal keyboard guard, which is a ground-truth guard

A modal blocks clicks but **not** document-level `keydown`, and `ReviewModule` binds one. Without a
guard, `j` pressed over the open viewer advances the queue behind it and `1` records a verdict on a
frame the operator is examining *through a modal* — ground truth the promotion gate is judged on,
written by a keystroke aimed at something else.

The check lives in `review.ts`, not in `keys.ts::dispatch`. Putting it in the dispatcher would have
made that module need jsdom to test, and its guards are precisely the ones that stop a held key
writing labels. **Today the guard is defensive**: the only dialog lives in the map module, where
the review root is hidden and the existing `this.root.hidden` check already fires. It becomes
load-bearing the moment review gets its own way to open the viewer.

### What the browser found that no spec would have

At 185px wide, a cell cannot carry `srv pothole 0.41` three times over — the labels buried the
photograph they were annotating. Thumbnail labels are suppressed and the answer moved to the
viewer. This costs nothing in accessibility terms: detector boxes carry no class hue to begin with,
so hiding the text removes no colour-alone dependency.

### A second theme-blind token — the same defect as `--color-danger`

`--color-text-subtle` had **no dark value**. Tuned in 2.7d's UX pass against the light canvas
(`#6e6659`, 4.76 / 4.50) and inherited unchanged into dark, where it measures **2.49 canvas / 2.15
surface / 2.35 sunken**. Twenty-six rules in `styles.css` consume it — `.empty-note`,
`.field-label`, `.action-status`, the legend, every frame caption — so **every secondary label in
the console was effectively unreadable in dark mode**, for the whole of the phase that fixed the
same bug in `--color-danger`.

`phase-2.7d-review-surface.md` now carries a correction: its claim that dark `--color-text-subtle`
measured 6.81 / 5.87 was asserted, not computed. There was no dark value to measure.

**The generalisation worth keeping: a colour token defined in only one theme block is the recurring
shape of this bug.** `--color-text-muted` never had it, because it resolves through
`var(--ramp-neutral-700)` and each theme block redefines the ramp. That is the pattern to copy.

Dark `#aba191` measures **5.53 / 4.77 / 5.22** and stays well below `--color-text-muted`'s 7.19 —
the distinction the two tokens exist to make. Verified in the browser by computing from the
resolved `color` and the first non-transparent ancestor background, not from the token table:
**4.77 in dark, 5.18 in light**, on `.empty-note`, `.field-label` and the new frame captions alike.

### `VLM_HTTP_URL` was set to a comment

`VLM_HTTP_URL=   # e.g. http://localhost:11434/v1/chat/completions` in `.env.example` assigns the
**comment text as the value**: python-dotenv strips an inline `#` only when the value is non-empty.
Anyone enabling a VLM backend got `unknown url type: '# e.g. http://localhost:11434/...'` from
urllib — a message that names nothing useful and points at no file.

Found by running `vlm_eval.py` against a real provider for the first time; all five calls failed
identically. Every other key was checked: this was the only empty-valued one. `VLM_VERIFY_LOW=0.40`
and friends parse correctly because they have a value before the comment.

---

## 3. The first VLM that ever answered

Phase 2.9 shipped the harness and could not run it: "no provider is configured on this machine."
That is now resolved, and the resolution is worth recording because none of it was about the code.

**The blocker was a container memory limit, not the GPU.** `qwen2.5vl:3b` is 3.2 GB on disk and
Ollama refused it with *"model requires more system memory (8.4 GiB) than is available (7.2 GiB)."*
The machine has 31.8 GiB and the Docker VM 14.9 GiB free — but the `ollama` container carried a
hard **4 GiB** limit. `docker update --memory 12g --memory-swap 16g` fixed it without touching WSL
configuration or installing anything.

It runs at **100% CPU**: the 4 GB RTX 3050 Ti cannot hold the ~10 GB working set, so throughput is
roughly **40 s/frame** and a 340-frame sweep is about four hours. That is the number that decides
how this measurement gets run, and it is a hardware fact, not a tuning one.

### The 5-frame smoke, which is already a signal

| | said pothole | said not |
|---|---|---|
| **is a pothole** | 0 | 1 |
| **is not** | 0 | 4 |

precision 0.000, recall 0.000, F1 0.000, **accuracy 0.800** — and every point of that accuracy
comes from saying "no" to negatives. Five frames cannot support a conclusion; the full sweep over
all 340 labelled frames is running to `runs/vlm-ollama-qwen3b.json`, which the script writes
incrementally so any later analysis is free (`--analyse-only --sweep`).

What this already establishes: **the path works end to end against a real model**, which no test in
the suite covers — all 42 hybrid and VLM tests run against stubs or a monkeypatched `urlopen`.

---

## Verification

- **554 backend tests, 133 frontend specs** (69 at the start of the round), ruff and build clean.
- New pure specs: `frameview/evidence.spec.ts`, `panel/corroboration.spec.ts`,
  `review/overlay-classes.spec.ts`, `map/frame-facts.spec.ts`, `map/layers.spec.ts`, plus
  `resolveLanding` cases. All node-environment; **no jsdom was added**.
- `framesLayer()` gained an optional `colors` argument so its style expressions are assertable
  without a DOM. Its spec pins that radius keys on `server_probability` and not
  `device_probability`, that the zoom range is non-empty (an inverted range renders nothing and
  looks exactly like no data — this project has already shipped one silent nothing-renders bug),
  and that the `case` checks unscored *before* unpaired so a backlogged frame reads grey rather
  than invisible.
- Driven in a real browser in **both themes**: dialog `display: none` while closed, real Escape
  closing it, focus returning to the thumbnail, the object URL released, box toggles 3 → 2 → 0 → 3,
  arrows paging 1 of 12 → 2 of 12, and review still filling its pane at 771×1028 with device boxes
  drawn for the first time. **Zero console errors.**
- **Ground truth unchanged: 375 / 65 / 43 / 200, zero `frame_label_history` rows.** The temporary
  verification account was created and deleted each time.

## What this leaves

1. **Label the 1,819-frame seam at ≥ 0.30, then box the positives.** Still the only untried remedy
   using exactly-in-domain data, and the seam is 75% larger than it was this morning.
2. **Freeze a v2 holdout first.** `promote_model.py`'s `_LABELLED_SQL` is a **deny-list**, so a
   frame in neither list lands in the eval set by default. Training on seam labels without a
   `--holdout-ids` allow-list and a SHA-256'd `runs/holdout-v2-ids.txt` manufactures an improvement
   out of leakage. Deliberately not built this round — it is not needed until a retrain.
3. **Finish the VLM sweep and recalibrate `VLM_VERIFY_LOW`/`HIGH`, or record why not.** "Nothing
   passed" is a legitimate outcome; leaving 0.40/0.75 unremarked after a measurement exists is not.
4. **`FUSION_FRAME_ONLY_ENABLED` is still false**, and the code calls vision-without-impact "the
   largest recall ceiling in the pipeline". It needs a measured threshold, and the current 375
   labels cannot supply one — at p ≥ 0.50 precision is 0.300 and recall 0.092; at p ≥ 0.60, 0.500
   and 0.031. More labels is the unlock, so it follows the seam pass.
5. **The map → viewer path was cut**, as the plan said it would be if the round ran long. It needs a
   new `GET /api/v1/frames/{client_id}` endpoint, because the frames tile carries
   `server_box_count` and not the boxes. The pure `map/frame-facts.ts` extraction it depended on
   did land.
