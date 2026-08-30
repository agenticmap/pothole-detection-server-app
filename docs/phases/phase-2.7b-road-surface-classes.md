---
updated: 2026-08-28
---

# Phase 2.7b — Road-surface classes, and breaking the recall ratchet

Phase 2.7 got a real detector running end to end. This phase fixes the reason that detector gets
*worse* every time it is given more human labels.

For the model-family split this phase assumes, see
[`detection-model-strategy.md`](../architecture/detection-model-strategy.md). For the measurements quoted below,
see [`phase-2.7-detection-enablement.md`](./phase-2.7-detection-enablement.md).

## The problem: labelling makes the model worse

Three trained models, each given more hand-labelled data than the last:

| model | negatives added | recall | precision | F1 |
|---|---|---|---|---|
| `v1` | 0 | **0.708** | 0.568 | **0.630** |
| `v2` | 200 (ROI-cropped) | 0.431 | 0.718 | 0.538 |
| `v3` | 200 (uncropped) | 0.354 | **0.742** | 0.479 |

Measured on the same 140-frame holdout (65 pothole, 75 not), detector `--conf 0.05`, frame
threshold 0.05.

The mechanism is structural, not a tuning accident:

**`frame_label` stores a verdict per frame, not boxes.** So `scripts/export_labeled_frames.py` can only
consume the `label = 0` rows, as YOLO background images. The 65 frames labelled *pothole* contribute
**zero training signal** — a detector cannot learn a positive without coordinates, and nobody drew
any.

Every labelling session therefore adds negatives and only negatives. Worse, they are **hard**
negatives: the labelling queue is stratified toward frames the detector already found interesting,
so the `label = 0` rows are disproportionately manholes, tar seals and patches — dark, roughly
pothole-shaped, sitting on road surface in the wheel path. Training on those as background does not
teach "ignore manholes"; with a single class the model has no such concept. It teaches *dark
irregular shape on asphalt is not a pothole*, and real potholes share those features.

That is a ratchet, not a flywheel. Left alone it converges on a detector that never speaks.

### Evidence it is the negatives, not the crop

Phase 2.7 first blamed the ROI crop, because `v2`'s negatives were cropped and its positives were
not, making crop geometry a perfect predictor of class. That leak was real: `v2` preferred ROI-*off*
while `v1` and `v3` prefer ROI-*on*. Fixing it (`v3`, uncropped negatives) restored the normal
geometry preference — **and recall fell further**, 0.431 → 0.354.

So the crop was a confound, not the cause. `v3` on the archive test split is P 0.621 / mAP50 0.494
against `v1`'s 0.623 / 0.512 — statistically the same model. The damage appears only on real frames,
and only where hard negatives can act.

The likely reason `v3` is *worse* than `v2` despite being better trained: `v2`'s crop was a
**shortcut**. The model could satisfy the background class on image shape alone and never learn what
the pixels meant. `v3` removed the shortcut, so it actually learned the content lesson — and the
content lesson is the harmful one.

## The fix

Two changes, both required; either alone is insufficient.

### 1. Boxes on positives — `migrations/013_frame_box.sql`

A new additive table, following `010_frame_label.sql`'s conventions
(`frame_client_id TEXT REFERENCES asset_frame(client_id) ON DELETE CASCADE`, `labeled_by`,
`labeled_at`). Migration 010's own comment anticipated this: *"If per-box localization ever needs
measuring, that is a separate, additive table."*

Boxes are stored **corner-origin, normalized 0..1** — the convention `device_detections` and
`server_detections` already emit (`onnx_v1.py:21`). YOLO's `.txt` format wants centre-origin, so the
conversion happens **once, at export**, with a round-trip test. Storing centre-origin here would put
two box conventions in one database, which is precisely the class of bug `_check_layout` exists to
catch: plausible-looking garbage rather than a loud failure.

`frame_label` keeps the frame-level verdict; `frame_box` adds localisation. Neither replaces the
other.

### 2. A class for the distractors

**The class set is `0 pothole`, `1 manhole`, `2 grate`, `3 patch` (tar seal / asphalt repair),
`4 crack`**, defined once in `app/detection/classes.py` and imported by the labelling tool, the
exporter and the decoder — position *is* the class id, so a disagreement between any two of them
mislabels every box. `wet/shadow` is deliberately not a class: it is a lighting condition, not an
object, so there is nothing to draw a box around and those frames stay background.

Manhole, grate and patch become **classes in Model A**, not background.

### Review is two-phase: draft, then submit

`Enter` saves boxes immediately but leaves `frame_label.boxed_at` NULL. Only an explicit submit
writes that marker, and the exporter keys on it — so a frame the operator has not signed off cannot
reach a training set, while the work itself is already durable against a crash.

The reason for splitting them is that the alternative silently punishes correction. When save *is*
sign-off, going back to fix an earlier frame means editing something already counted as ground
truth, and the operator has no way to hold a batch open while deciding. Two phases make revisiting
free and make "I have looked at all of these" a single deliberate act.

`--reset-reviewed` reverses a submit for a named scope, clearing the marker but keeping the boxes.

### `crack` is the one class that needs a labelling rule, not just a name

Cracking is real road damage and belongs in Model A, but unlike the others it has **no natural
compact extent**. A hairline longitudinal crack is long, thin and usually diagonal, so an
axis-aligned box around one is almost entirely undamaged asphalt — the same argument that put lane
markings in Model C rather than here.

The failure mode this creates is not hypothetical, it is *this phase's own failure mode pointed at a
new target*: a sliver box teaches the model that mostly-undamaged asphalt is the crack class, which
is exactly how 200 hard negatives taught v2 and v3 that dark-shape-on-asphalt is background.

So the rule is **box crack REGIONS, not crack LINES** — alligator and block cracking, a crazed or
spalled patch of surface. `label_frames.py` warns on boxes thinner than `THIN_ASPECT_RATIO` (~1:6),
in the browser and again in the terminal, and the warning is **advisory**: a genuinely thin region
is occasionally the right call and only the person looking at the frame can tell. Blocking the save
would be a machine overruling the only judgement in the loop that has the evidence. This is the part that recovers
recall, and the reasoning is worth stating because it inverts the intuition: the problem is not that
the model sees manholes, it is that its only way to *explain* one is "background", which it achieves
by suppressing pothole-like features. A manhole class gives it an alternative hypothesis and
relieves that pressure directly.

Note this also answers a labelling question raised during Phase 2.7: marking manholes as `0` was
**correct** for a single-class detector — a manhole is not a pothole. The error was not the label,
it was the taxonomy having nowhere else to put it.

`frame_label.note` was added in Phase 2.7 to record *why* a frame is negative — but it arrived
**after** the 375 labels were made, so **every note is NULL** (verified 2026-08-28: 65 / 275 / 35
rows at labels 1 / 0 / -1, zero notes). Manholes therefore cannot be found by query. The 200
training negatives need a box-review pass, which is what `--box` mode exists for; the note field
starts paying off from the next verdict session onward.

### Prerequisite, now shipped: the decoder was single-class in three places

`app/detection/onnx_v1.py` had to be fixed **before** any nc > 1 model could ship. It has been:

- **Frame probability came from the maximum over all classes.** A confident manhole would have
  reached fusion as a confirmed pothole, and fusion cannot detect that because it never sees a
  class. `_frame_probability` now takes the best **primary-class** box, after NMS. A frame holding
  only manhole boxes scores `0.0`, which `NULLIF(server_probability, 0.0)` already reads as *no
  measurement* → fall back to `device_probability`. That is the correct reading rather than a gap:
  "that is a manhole" is not evidence about a pothole in either direction.
- **`label` was one fixed string**, so every box would have read `pothole` regardless of class.
  `labels` is now a per-position tuple and `_label_for` indexes it.
- **A labels/nc mismatch was silent.** It is now rejected at *construction* — read from the export's
  declared output shape — because `app/detection/service.py` swallows per-frame exceptions into a
  NULL, so a mismatch caught only at decode time would present as "detection silently does nothing,
  forever". `_check_layout` repeats the check for dynamic-shape exports.

Verified behaviour-preserving: `yolo11s_pothole_v3` re-evaluated through the new decoder reproduces
its documented numbers exactly (best F1 0.479 at threshold 0.05; 8 TP / 0 FP at 0.30).

Two things needed no change: `_check_layout` was already generic over `4+nc`, and
`server_detections` already carries `class_id` and `label` per box. The Android client references
none of these fields (verified against the sibling repo), so multi-class is wire-safe.

## The first run (v4): one variable, holdout frozen

All 65 pothole labels are also the holdout's only positives. Boxing them and training on them would
empty the eval set, so v4 deliberately does **not**:

| | changes for v4 | stays put |
|---|---|---|
| training | the 200 negatives already in the training split gain manhole/grate/patch boxes | 5322 archive positives |
| holdout | — | 65 pothole + 75 not, byte-identical to v1/v2/v3's |

That makes v4 a single-variable comparison against three existing baselines, and it tests exactly
the claim this phase rests on: that giving the model an alternative hypothesis for a manhole
recovers pothole recall. Boxing positives is the other half of the fix and a separate experiment;
it requires rebuilding the eval set first.

**Single-stage training, identical to the v3 recipe.** The only difference is that those 200 frames
now carry class 1–3 boxes instead of empty `.txt` files.

An earlier draft of this plan called for a two-stage fine-tune — archive first, real frames second.
**That does not apply to v4**: stage 2 would consist of 200 real frames containing *zero* pothole
boxes, which teaches "no potholes ever". Two-stage returns when positives are boxed and stage 2 has
something positive in it.

**Known confound, recorded rather than fixed.** The 5322 archive images contain manholes nobody
annotated, so at nc=4 the trainer reads them as background — a signal directly opposing the manhole
class. The archive is a different visual domain (landscape, daylight, GoPro) so the conflict is
partly separable, and removing it means either re-annotating the archive or the two-stage recipe
that is not yet available. Accept it for v4 and state it alongside the results.

**Environment.** `PIN_MEMORY=false` and `workers=2`. **Never `resume=True`** — resuming a
crashed run produced NaN in 417 of 418 tensors while looking like a clean early stop. Both traps
are in [`phase-2.7-runbook.md`](../runbooks/phase-2.7-runbook.md)'s troubleshooting table.

## Evaluation discipline

- **Compare at matched recall, not matched threshold, and not on F1.** F1 is the wrong objective
  here: fusion consumes the visual term as a modifier on a sensor verdict, and a silent detector
  falls back to `device_probability` — i.e. today's behaviour — so a detector that rarely speaks but
  is right when it does is worth more than a noisy one.
- Keep `--exclude-ids` on every comparison so training frames never score their own model.
- **The holdout stays frozen for v4** (see above). Before any later run boxes the positives, note
  that they *are* the holdout's positives — so that run needs a rebuilt eval set, and the v1/v2/v3
  numbers must be re-measured on it before any comparison is valid.

## Measured: v4 did not work, and the diagnostic says why

`yolo11s_pothole_v4` — nc=5, 200 real frames carrying 43 human boxes (30 manhole, 7 grate,
6 patch) and 163 reviewed-clean backgrounds, single stage, 80 epochs, 3.62 h, no NaN. Archive split
P 0.607 / mAP50 0.513, statistically the same as v1 and v3. Same 140-frame holdout, `--conf 0.05`:

| model | negatives | recall | precision | F1 |
|---|---|---|---|---|
| v1 | 0 | **0.708** | 0.568 | **0.630** |
| v2 | 200 ROI-cropped | 0.431 | 0.718 | 0.538 |
| v3 | 200 uncropped | 0.354 | **0.742** | 0.479 |
| v4 | 200 + 43 class boxes | **0.215** | 0.667 | 0.326 |

**The hypothesis was wrong, or at least insufficient.** Giving the model an alternative hypothesis
for a manhole did not relieve the suppression; recall fell further, and v3 dominates v4 at matched
true positives. The ratchet is intact.

### The mechanism: data-poor classes steal the potholes

Scoring the holdout two ways separates "did it detect the object" from "did it name it right":

| scoring | recall @0.05 | precision |
|---|---|---|
| pothole class only (what fusion receives) | 0.215 | 0.667 |
| any class counts | **0.431** | 0.683 |

The model localises **twice as many** potholes as the pothole class alone reports. On **15 of 65**
pothole frames a non-pothole class outscores pothole. With 30 / 7 / 6 training boxes those classes
are far too data-poor to be selective, so they fire on potholes — and because
`_frame_probability` correctly reads only the pothole class, that confidence never reaches fusion.
The class-aware decoder is not the bug; it is what made the failure visible instead of silently
scoring a "manhole" as a pothole.

### The deeper asymmetry, which this phase did not fix

**Every real-domain box in the training set is a negative.** 200 real frames, 43 boxes, none of them
a pothole, plus 163 empty. The only positives are 3728 archive images from a different visual
domain — landscape, daylight, GoPro. So the model's real-domain supervision says, in effect,
*"road that looks like this is not a pothole"*, and nothing says what a real-domain pothole looks
like. Adding distractor classes changed which label the suppression wears; it did not add a single
positive anchor.

That is why boxing the 65 positives is now the load-bearing step rather than an optional second
half. It also means rebuilding the eval set first: those 65 are the holdout's only positives.

## What to expect, and how not to misread it

- **Judge v4 on pothole recall recovering toward v1's 0.708**, with precision holding above v1's
  0.568. That is the whole hypothesis.
- **The manhole class will be data-poor** (~200 frames). Its job is to give the model an alternative
  hypothesis, not to be accurate. Judge this phase by whether **pothole recall recovers**, not by
  manhole AP.
- The holdout is small — 65 positives, roughly ±5–6 percentage points. Only sizeable movements mean
  anything.
