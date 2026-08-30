---
updated: 2026-08-30
---

# Phase 2.7c — Public data: RDD2022, and what it did not fix

Phase 2.7b ended with a diagnosis rather than a working detector: **every real-domain box in the
training set was a negative.** 43 hand-drawn boxes, none a pothole, plus 163 reviewed-clean frames,
against 3728 archive positives from a different visual domain. This phase tested the obvious
remedy — buy the missing positives from public data — and the answer is a qualified no.

For a consolidated, report-ready account of all five models, see
[`detection-research-record.md`](../research/detection-research-record.md).

For the failure this follows from, see
[`phase-2.7b-road-surface-classes.md`](./phase-2.7b-road-surface-classes.md).

## The result

`yolo11s_pothole_v5_rdd` — archive + 2575 RDD2022 potholes, `nc=1`, 80 epochs, 5.50 h, no NaN.
Same frozen 140-frame holdout (65 pothole / 75 not), `--conf 0.05`, ROI on:

| model | training positives | recall | precision | F1 |
|---|---|---|---|---|
| v1 | 3728 archive | **0.708** | **0.568** | **0.630** |
| v2 | + 200 cropped negatives | 0.431 | 0.718 | 0.538 |
| v3 | + 200 uncropped negatives | 0.354 | 0.742 | 0.479 |
| v4 | + 43 class boxes, nc=5 | 0.215 | 0.667 | 0.326 |
| **v5** | **+ 2575 RDD potholes** | **0.677** | 0.518 | 0.587 |

**The regression is fully reversed and fully explained.** Recall went 0.215 → 0.677, a 3.1×
recovery, confirming that the v2–v4 collapse was a data problem and not a permanent property of the
pipeline.

**But v5 does not beat v1.** Matched on true positives caught — the comparison that survives a
threshold change — v1 wins or ties everywhere:

| TP caught | recall | v1 thr | v1 FP | v1 prec | v5 thr | v5 FP | v5 prec | better |
|---|---|---|---|---|---|---|---|---|
| 34 | 0.523 | 0.10 | **25** | 0.576 | 0.10 | 28 | 0.548 | v1 |
| 14 | 0.215 | 0.35 | **8** | 0.636 | 0.30 | 12 | 0.538 | v1 |
| 8 | 0.123 | 0.45 | **4** | 0.667 | 0.35 | 7 | 0.533 | v1 |
| 6 | 0.092 | 0.50 | 3 | 0.667 | 0.45 | 3 | 0.667 | tie |
| 5 | 0.077 | 0.55 | 1 | 0.833 | 0.50 | 1 | 0.833 | tie |

v1 also has the higher ceiling: 46/65 true positives against v5's 44/65.

**So 2575 in-domain positives bought a return to the starting line, not an advance.** That is worth
stating plainly: four training runs after v1, the best model is still v1.

## Why it probably did not help

**Scale mismatch, the leading candidate.** A spot-check of RDD pothole boxes puts the median at
**0.53% of frame area** (min 0.11%, max 1.5%) — small, and frequently near the horizon. The
production detector crops to the 0.45–0.90 road band and therefore sees near-field surface at far
larger scale. RDD is *modality*-matched (windshield-mounted smartphone, which is why it was chosen
over every other public set) but not *scale*-matched.

**A batch-size confound, small but real.** v1–v4 trained at `batch=8`; v5 needed `batch=4` because
the 4 GB card OOM'd at batch 10/674 with browsers and a game launcher holding VRAM. The gap between
v1 and v5 is narrow enough (0.708 vs 0.677, i.e. 2 true positives) that batch size cannot be ruled
out as the whole explanation.

**RDD's definition of "pothole" is not ours.** It annotates for pavement-condition assessment
(D40), not for a fusion pipeline that must decide whether a wheel impact was real. Some D40 boxes
are surface spalling a driver would never feel.

## What this phase did establish

- **The ratchet is broken.** More data of the right kind restores recall; the pipeline is not
  inherently degrading.
- **The promotion gate proposed in the plan is necessary, and would have caught everything.** v2,
  v3, v4 *and* v5 all fail "beat the incumbent on the frozen holdout" — and every one of them looked
  fine on archive-split metrics, which sat at mAP50 ≈ 0.51–0.52 across models ranging from best to
  useless. No model should reach `DETECTION_MODEL_PATH` without passing that gate.
- **`scripts/ingest_rdd2022.py`** now converts VOC → YOLO with a class remap, area filter, and a
  manifest recording licence and SHA-256 per archive.

## Licence

**The two official sources disagree.** The GitHub README says CC BY-**SA** 4.0; the Figshare record
of deposit — DOI `10.6084/m9.figshare.21431547.v1`, which is where the data was actually obtained
and which carries a machine-readable licence field — says **CC BY 4.0**. The copy in use was
downloaded from Figshare.

Mitigations are kept regardless, because the ambiguity is real and they cost nothing: the images are
never redistributed, RDD-derived weights carry `_rdd` in the model id so they can be identified and
retired, and the non-RDD training path still works, so a clean-room retrain is always available.

The per-country archives referenced by the README are **gone** — every `bigdatacup.s3` URL returns
`AccessDenied`, including the RDD2020 ones. The working route is the combined 12.35 GB Figshare zip,
which contains the per-country zips as members.

## Deliberate choices, and why

**Potholes only; cracks ingested but not trained on.** The three countries carry 26,653 crack boxes
against 2575 potholes. Importing a 10:1 imbalance is the gradient-competition failure of v2–v4 at an
order of magnitude more scale, so `--keep-classes pothole --drop-empty` was used. The crack data is
on disk for when the pothole class is healthy enough to share a model.

**`nc=1`, and none of our own frames.** v5 was built to be comparable to v1 with exactly one
variable moved. Re-adding the 43 distractor boxes would have re-introduced the thing v4 measured as
harmful.

**No ROI crop on the RDD images.** Cropping part of a training set makes crop geometry a predictor
of class — the v2 leak — so full frames throughout.

## Threshold calibration — and why the VLM gray zone is not a detector threshold

An active-learning loop was proposed using a **0.40-0.75** gray zone: trust anything above as a
positive, anything below as a negative, review the middle. Those numbers are
`VLM_VERIFY_LOW` / `VLM_VERIFY_HIGH`, chosen for the **VLM verifier** — a different component
deciding when a VLM call is worth paying for — and never calibrated against this detector.

> **Superseded, and confirmed, by [`phase-2.9-vlm-verification.md`](./phase-2.9-vlm-verification.md).**
> The whole 5,615-frame corpus has since been scored and the table below recomputed over all **340**
> labelled frames rather than the 140-frame holdout. Two numbers change: the corpus max is **0.779**
> (not 0.691), so "above 0.75" fires on 5 frames of 5,615 rather than never — and the one labelled
> frame up there is a **false positive**. And the base rate falls 46.4% → **19.1%**, because the
> holdout was deliberately enriched; the lifts below are correspondingly overstated. The conclusion
> is unchanged and now rests on 2.4× the evidence. Use the 2.9 table for calibration.

v1 over the 140-frame holdout (65 pothole / 75 not), ROI on:

| band | frames | % of set | potholes | pothole rate |
|---|---|---|---|---|
| 0.00-0.05 | 59 | 42.1% | 19 | 32.2% |
| 0.05-0.15 | 34 | 24.3% | 18 | 52.9% |
| 0.15-0.30 | 23 | 16.4% | 12 | 52.2% |
| 0.30-0.40 | 8 | 5.7% | 6 | **75.0%** |
| 0.40-0.75 | 16 | 11.4% | 10 | 62.5% |

Base rate 46.4%.

**The highest score any frame reached is 0.691**, so "above 0.75 -> trust as positive" can never
fire. And "below 0.40 -> trust as negative" would discard 124 of 140 frames holding **55 of the 65
potholes** — 85% of every in-domain positive we have, which is precisely the data this phase
identified as the binding constraint. The loop would discard its own cure.

**Therefore: scores prioritise, they never auto-label.** Even the bottom band is 32% pothole against
a 46% base rate, so there is no cut point at which auto-rejection is safe. v1's ranking is only
weakly informative — the best band is a 1.6x lift — which is useful for ordering a queue and useless
as a decision rule. `scripts/label_frames.py --order score` implements the ordering; there is
deliberately no auto-accept or auto-reject anywhere.

## The promotion gate, and the two objectives it needs

`scripts/promote_model.py` compares a candidate against the incumbent at matched recall on this
holdout. Building it surfaced something the single-number view had hidden:

**v1 and v3 cannot be ranked on one axis.** v1 reaches 46/65 true positives to v3's 23, yet v3 pays
fewer false positives at every operating point they share. A "must win everywhere" rule rejects both
in both directions — a deadlock, not a tie. They are good at different jobs:

| objective | judged on | best model today |
|---|---|---|
| `fusion` — what ships as `DETECTION_MODEL_PATH` | false positives at matched recall | **v3** |
| `sampler` — what ranks a review queue | recall ceiling | **v1** |

That reproduces, mechanically, what
[`phase-2.7-detection-enablement.md`](./phase-2.7-detection-enablement.md) concluded by hand: *"v3
does not merely trade recall for precision against v1 — it dominates it"*, and v1's only advantage
is reach. **A frame the sampler never fires on never enters the queue**, which is why the
active-learning loop runs on v1 while fusion would ship v3.

Verified against the four real cases: v5 is rejected for `fusion` (regresses at 4 points, ceiling
drops 46 -> 44); v3 is promoted for `fusion`; v1 is promoted for `sampler`; v3 is rejected for
`sampler`.

## What to try next, in order of expected value

1. **Remove the batch-size confound**: retrain v5 at `batch=8` with the GPU otherwise idle. Cheapest
   way to learn whether the remaining 0.03 recall gap is real.
2. **Filter RDD by box scale** — keep only boxes above roughly 1% of frame area, so the training
   positives resemble what the ROI crop actually sees. Fewer images, better matched.
3. **Box our own positives.** Still the only source of exactly-in-domain positives, and still
   blocked on rebuilding the eval set, since the 65 holdout positives are the only ones we have.
