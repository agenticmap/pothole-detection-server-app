---
updated: 2026-09-03
---

# Phase 2.7d — The review surface, and a defect that would have poisoned the training set

Phase 2.7b ended with a diagnosis: **every real-domain box in the training set is a negative.**
Phase 2.7c bought 2,575 public positives from RDD2022, recovered recall 0.215 → 0.677, and still
did not beat v1. Both phases closed with the same remaining remedy — *box our own positives* — and
both noted it was blocked, because boxing requires labelling first and labelling happened only in
a terminal on one machine.

This phase builds the instrument. It does not train a model and it does not claim a recall number;
it is the thing that has to exist before either is possible.

For the constraint it exists to lift, see
[`phase-2.7b-road-surface-classes.md`](./phase-2.7b-road-surface-classes.md) and
[`phase-2.7c-public-data.md`](./phase-2.7c-public-data.md). For the consolidated model account, see
[`detection-research-record.md`](../research/detection-research-record.md).

## The constraint, reproduced from the database rather than the write-up

Queried against `pothole_db` on 2026-09-02, before any of this work ran:

| | |
|---|---|
| frames in corpus | 5,615 |
| `frame_label` rows | 375 (340 with a 0/1 verdict, **65 pothole**) |
| frames signed off (`boxed_at`) | 200 |
| frames judged but not signed off | 175 |
| open drafts | 0 |
| **boxes drawn, total** | **43** |
| — manhole / grate / patch | 30 / 7 / 6 |
| — **pothole** | **0** |
| — crack | 0 |

**Not one real pothole has ever been boxed.** 2.7b said so; this is the same fact read straight
out of `frame_box`. Every hand-drawn box in the corpus is a negative, which is why the detector
got monotonically worse each time it was given more of them.

## What shipped

### Server

`migrations/017_frame_review.sql` — additive, idempotent:

- **`frame_label.boxes_drafted_at`**, which closes a hole the CLI documents about itself. 013's
  `boxed_at` means "a human reviewed this", deliberately *not* "this frame has boxes" — a frame
  reviewed and found genuinely clean has zero boxes and IS finished, while a frame nobody opened
  also has zero boxes and is NOT, and only the first may be exported as a YOLO background image.
  But the CLI recorded a draft by writing the boxes themselves, so a **zero-box draft left no
  trace at all** and could not be re-adopted after a crash. Over HTTP that hole is worse: a client
  that POSTed an empty set and a client that never opened the frame are indistinguishable.
- **`frame_label_history`** — append-only, one row per verdict. `frame_label`'s PK is one row per
  frame and its write is an upsert, so with one CLI and one person "last write wins" was
  invisible. A multi-user console makes it reachable, and the research record already named the
  limitation: *"Single annotator, single pass… the schema cannot currently express disagreement."*
  This does not prevent an overwrite; it makes one recoverable and inter-annotator agreement
  measurable retroactively, for one INSERT per label. Two FK asymmetries copied from `repair_log`
  on purpose: `labeled_by` and `frame_client_id` carry no foreign key, so the record of who judged
  a frame outlives the deletion of their account.
- Queue indexes on `asset_frame.server_probability` and the unboxed `frame_label` partial.

Five endpoints in `app/routes/review.py`, backed by `app/services/review_service.py`:

```
GET  /api/v1/review/frames                       the queue      ViewerOrAbove
POST /api/v1/review/frames/{id}/verdict          1 / 0 / -1     StaffOrAboveLive
POST /api/v1/review/frames/{id}/boxes            replace-all    StaffOrAboveLive
POST /api/v1/review/frames/boxes/submit          sign off       StaffOrAboveLive
POST /api/v1/review/frames/boxes/unsubmit        retract        AdminOnly
```

Reads sit at `ViewerOrAbove` because that is *strictly less* than a viewer already has:
`GET /clusters/{id}` is `ViewerOrAbove` and returns frames with their scores behind a
`ViewerOrAbove` image route, so gating the queue higher would mean a viewer could see a frame
through the panel but not through the list. Writes use **`StaffOrAboveLive`**, which re-reads
`org_member` rather than trusting the token's login-time snapshot: this is ground truth feeding
`promote_model.py`, so a revoked reviewer must stop writing *now*, not within the token's TTL.
Unsubmit is admin-only because it retracts somebody else's attestation.

### Client

`dashboard/src/review/` — a new module behind the rail, which required building module switching
from scratch (the rail's buttons had **no click handler at all**).

| file | owns |
|---|---|
| `geometry.ts` | pure. px↔normalized, clamping, the 6px click-vs-drag floor, `boxKey`, `isThin` |
| `queue-state.ts` | pure. the frame state machine, cursor, drafts, submit folding |
| `api.ts` | the five endpoints, typed against `app/models/review.py` |
| `images.ts` | bounded object-URL cache + prefetch |
| `overlay.ts` | the SVG box layer: draw, hit-test, drag-to-draw |
| `keys.ts` | two key maps and the legend generated from them |
| `review.ts` | the module controller |

Also: `app/services/label_queue.py` (queue predicate extracted from the CLI so both clients share
one implementation), `app/services/detection_boxes.py` (parses `server_detections` into boxes), and
`server_boxes` / `device_boxes` / `vlm_verdict` on `ClusterFrameItem`.

## The defect this phase found, and why it mattered

`finalize_boxes` originally marked a frame signed off with:

```sql
UPDATE frame_label SET boxed_at = now()
WHERE frame_client_id = ANY($1::text[]) AND boxed_at IS NULL
```

No check that boxes were ever saved. So **submitting the id of a judged-but-never-boxed frame
signed it off with zero boxes** — and `scripts/export_labeled_frames.py` keys on `boxed_at` alone.
Its own docstring states the rule this violates:

> REVIEWED IS NOT THE SAME AS EMPTY. A frame with `boxed_at IS NULL` has never been opened, so its
> lack of boxes means nothing; exporting it as background is precisely the mistake above.

The frame would have become an empty `.txt` — a YOLO **background image asserting "genuinely clean
road"** about an image nobody looked at. That is, exactly, the hard-negative poisoning that took
recall 0.708 → 0.215 across v2/v3/v4. Adding ~200 unexamined negatives per session is the same
failure at the same scale, arriving through a different door.

Reproduced against the database before fixing, then guarded in SQL with
`AND boxes_drafted_at IS NOT NULL`, and reported to the caller as a new `skipped_undrafted` field
rather than silently dropped. Two regression tests pin it, including the mirror case: an **empty
draft** *is* submittable, because "I looked, there is nothing here" is a real answer and the whole
reason the guard keys on `boxes_drafted_at` rather than on box count.

**The guard is server-side on purpose.** The client sends the id list, so a guard in a browser is
not a guard. A design pass argued the client was the only place this could live; that is wrong for
any write that feeds the promotion gate.

Two existing tests had been passing *because* of the hole and were corrected.

## Design decisions worth keeping

**Blind mode withholds server-side rather than hiding client-side.** In the CLI, blindness is a
rendering property behind the `b` key. In a browser that is not blindness — the number is in the
JSON, one devtools panel away. So in `order=blind` the server omits `server_probability`,
`device_probability` and every box list, and it does so **even when `include_model_boxes=true`**.
Anchoring the labeller to the model's opinion is a *measured* cause of bad labels, not a style
preference.

**Model boxes are opt-in and off by default**, for the same reason. There is deliberately **no
"accept the model's boxes" button** — it is the obvious productivity feature and the wrong one,
because labels that flatter the model are precisely what 2.7b measured going wrong.

**Two key maps, never one.** `1` means "this frame is a pothole" in verdict mode and "draw the next
box as a pothole" in box mode. The CLI keeps them in separate branches with the reason written
down — sharing the handler *"would make the visible legend a lie in one mode or the other"* — and
here the legend is **generated from the binding array the dispatcher reads**, so drift is
unrepresentable rather than merely discouraged. Digits match on `event.code`, not `event.key`:
on AZERTY the unshifted number row produces `&é"'(`, and this is a tool people sit with for hours.

**Navigation is gated on the write.** In box mode a move saves first, and a failed save does not
move. Leaving a frame whose boxes were not stored is the one failure an operator cannot discover
later. The save is attempted **even when the move is a no-op** — at the last frame the *move* does
nothing but the *save* still must happen, which is the CLI's recorded bug where the final frame
*"never became a draft, so Submit had no id to sign off"* and never left the queue.

**One FIFO chain for navigation**, so a fast `j j j` cannot land an older save on top of a newer
one. There is never more than one box write in flight, which makes out-of-order landing
unrepresentable rather than unlikely.

**A bounded object-URL cache, not the panel's revoke-on-decode.** `panel/frames.ts` revokes each
URL immediately and holds no registry, which is right for a thumbnail grid opened once and wrong
for a queue where `k` must be instant. This module keeps an LRU of 12 with revocation on eviction
and unmount — bounded, not open-ended — and prefetches only 2 ahead at concurrency 2, *below* the
panel's 3, because `GET /frames/{id}/image` is guarded by a `Semaphore(6)` shared across all users
and a review session is a sustained consumer rather than a burst.

**SVG in a normalized viewBox, not a canvas.** Boxes are stored normalized 0..1, and the viewBox
maps that onto whatever size the image is rendered at — so resize, browser zoom, DPR change, the
panel opening and the rail collapsing all need *zero* JavaScript. Two details are load-bearing:
`vector-effect="non-scaling-stroke"` (with `preserveAspectRatio="none"` the x and y scales differ,
so a plain stroke renders thicker on one axis), and **never `object-fit`** on the image — with
`contain` the rendered content box stops matching the element box and every coordinate is off by
the letterbox.

**Colour is never read into JS.** The five class hues are custom properties referenced through
`var()` from CSS, so a theme flip repaints with no JS at all. The map legend takes the other
approach — it resolves through `cssVar()` to a hex and therefore has to re-render itself — and this
is the better of the two patterns.

**Drag coordinates come from `getBoundingClientRect()`, never `offsetX`/`offsetY`**, ported from
the CLI comment and all: those are relative to whatever element is under the cursor — usually an
existing box — and would silently shift every rectangle drawn over another one.

## Four defects the browser found that no test would have

The Playwright walkthrough was worth more than its cost. All four were invisible to the type
checker and to 69 unit tests:

1. **Verdict mode said "50 left to submit"** — box-mode wording for a mode with no submit step,
   sending the operator looking for a button that is not there.
2. **Every class drew in the same terracotta.** `.review-box-human` set
   `stroke: var(--color-primary)` and the class was never applied, so the five-colour class picker
   was a promise the overlay did not keep.
3. **The selection affordance was invisible.** Replaced with a white halo rect drawn *beneath* the
   coloured stroke — recolouring the box would hide the one thing it exists to communicate.
4. **A pre-existing race in `map.ts`, not introduced here.** `on('load', addClusterLayers)` was
   unguarded while `applyTheme`'s `styledata` handler was guarded, so flipping the theme within a
   second of signing in threw `Source "clusters" already exists`, which aborts `addClusterLayers`
   partway and leaves the map with **no cluster layers at all**. Confirmed against `git show HEAD`
   before fixing. Same one-line guard applied.

## Verification

**552 backend tests** (487 before this work), **69 frontend specs** — the dashboard had none and
CI never touched it; there is now a `dashboard` job running `npm ci`, `test:unit` and `build`. That
build step is a real gate: its `prebuild` hook copies MapLibre's worker into `public/`, and a 404
there makes *every* vector source silently never load, which has shipped once before.

The frontend specs are pure — no jsdom — because the state machine and the geometry are the two
places a mistake is invisible on screen.

Driven in a browser against `pothole_db`:

| check | result |
|---|---|
| queue at `min_score=0.30` | 1,041 outstanding, matching the research record exactly |
| band counts | 2,183 / 1,336 / 993 / 400 / 698 / 5 — phase-2.9's table row for row |
| top score | 0.779 = the documented corpus maximum |
| blind mode | scores and boxes absent from the payload *with* `include_model_boxes=true` |
| box drawn at 55%/55% → 75%/70% | stored `x .55 / y .55 / w .20 / h .15` — exact |
| `labeled_by` | `usr_…` from the JWT, never the body |
| after save | `boxes_drafted_at` set, `boxed_at` **null** — saving is not submitting |
| full reload + re-login | hash restored box mode *and* the drafted frame, box intact |
| empty draft | zero boxes, `boxes_drafted_at` still recorded — the case the CLI could not do |
| return to map | legend never flipped to compact, canvas 808×785 not 0×0, control stack 112px not 0 |
| console | zero errors, zero warnings |

The frame used was **restored exactly** afterwards — 43 boxes / 0 drafts / 200 signed / 375 labels,
matching the pre-walkthrough snapshot. Submit was deliberately *not* exercised against real data,
since it sets `boxed_at`; it is covered by 49 server tests including both refusal paths.

## The UX pass, and what a design skill was and was not good for

The surface above shipped working and was then reviewed against `ui-ux-pro-max` plus an
independent audit. Both were worth running; only one was worth *following*.

**The skill's rule domains were useful. Its generative `--design-system` output was a category
mismatch and was not applied.** Asked for "internal operator console data annotation review dense",
it keyed on the word *review* and returned an **e-commerce product-ratings landing page** — "Hero
(product + aggregate rating)", a Buy CTA, star-rating gold, and an "Exaggerated Minimalism" style
with `font-size: clamp(3rem, 10vw, 12rem)` at weight 900. That is a luxury-fashion editorial
system. Adopting it would have destroyed the Organic skin, whose palette and severity ramp carry
recorded reasoning *and* recorded past failures. The skill is also scoped to App UI
(iOS/Android/React Native) and says so twice, so its touch-target, safe-area, bottom-nav and
haptics rules do not transfer to a keyboard-driven desktop console.

What the audit found was worse than what the plan had been written for, and reordered the work.

### Invisible failure, and one integrity risk

| defect | before | after |
|---|---|---|
| `--color-danger` had **no dark value** | **1.56:1** on the dark canvas — not rendered | 6.14:1 |
| `--color-success` had no dark value, and no consumer | 2.18:1 | 7.89:1 |
| no `e.repeat` guard | holding `1` wrote a verdict per key-repeat | writes nothing |
| `dispatch` ignored interactive targets | `Enter` on **Sign out** cancelled it and saved a frame | reaches the button |
| `status` was one field | success rendered in danger red as `role="alert"` | ok / warn / error, each with a glyph |
| status never cleared | persisted for the whole session, re-announced every `j` | cleared on move; one live region, mutated |
| `writeError` | assigned 4x, rendered nowhere | per-frame "not saved" flag |
| `peeked` | silently excluded work from submit | flagged on the frame, named in the summary |

The `--color-danger` gap was **console-wide** — `panel.ts` uses `.error-text` too, so every error
message in the detail panel was equally invisible in dark mode. The `e.repeat` hole is the same
class of risk as the submit defect above: a stuck key writes ground truth the promotion gate is
judged on.

### The layout was one rule, not two problems

Measured at 1884x914: **73% of the pane was unreachable** and the verdict buttons sat at 879px in a
914px viewport. Both came from `max-height: 68vh` on a **portrait** corpus — height-bound, the image
could never exceed ~466px wide however wide the screen was, while the chrome sharing its column
pushed the controls off the bottom.

Now two columns: image left, controls in a rail at `--panel-width`. The stage takes each frame's
own aspect ratio, set per frame from `naturalWidth/Height`, so it scales *up* to fill the pane. That
was the only way to grow it without `object-fit` — with `contain` the rendered content box stops
matching the element box and every normalized overlay coordinate is off by the letterbox. Giving the
element the image's ratio makes the two identical by construction; the browser confirms the boxes
match to sub-pixel. Image went **466x622 -> 556x741**, controls are always visible, and it handles
landscape frames too.

### Contrast: every pairing now clears 4.5:1 in both themes

Two of the fixes were corrections to the *same pass*. The first `.review-thin-flag` fix swapped one
failing colour (`--severity-3`, 3.30 — and the severity ramp is reserved for ordinal severity
anyway) for another (`--color-primary`, **3.03**). `--color-primary` is a *fill* colour carrying
inverse text on buttons; it fails as a foreground. That exposed a real gap: the console had `danger`
and `success` but **no warning voice**, so `warn` was borrowing a fill. `--color-warning` is now a
proper semantic token in both themes.

`--color-text-subtle` was raised from `ramp-600` (3.61 canvas / 3.41 surface) to `#6e6659`
(4.76 / 4.50), staying a visible step lighter than `--color-text-muted` at 5.53 — the distinction
the two tokens exist to make. That was **pre-existing and console-wide**: `panel.ts` uses
`.empty-note` four times.

> **Correction, 2026-09-03.** The heading above is wrong for that one token, and this section's
> earlier claim that dark `--color-text-subtle` measured 6.81 / 5.87 was asserted rather than
> computed. **There was no dark value to measure.** `#6e6659` was tuned against the light canvas
> and surface — the two numbers quoted above are light-canvas and light-surface, not light and
> dark — and it was inherited unchanged into the dark theme, where it measures **2.49 canvas /
> 2.15 surface / 2.35 sunken**. Twenty-six rules consume the token, so every secondary label in
> the console was effectively unreadable in dark mode for the whole of this phase.
>
> This is the identical defect to `--color-danger` recorded in the P0 table above, and it survived
> the pass that found that one. The generalisation worth keeping: **a colour token defined in only
> one theme block is the recurring shape of this bug.** `--color-text-muted` never had it, because
> it resolves through `var(--ramp-neutral-700)` and each block redefines the ramp.
>
> Fixed in the following round: dark `--color-text-subtle: #aba191`, measuring 5.53 / 4.77 / 5.22,
> verified in the browser from the computed style and the first non-transparent ancestor
> background rather than from the token table. See `phase-2.10-imagery-surfaces.md`.

### Keyboard and screen reader

`render()` rebuilds the subtree, so anything focused was destroyed and a Tab user restarted from the
top of the document on every keystroke. Hidden until the `blur()` workarounds came out — those had
been throwing focus away deliberately. Controls now carry a stable `data-focus-key`, captured and
restored around the render **and around `load()`**: the first attempt missed the reload path,
because `load()` clears the DOM at skeleton time long before `render()` runs.

Pressing `1` used to change only the photograph and an 11px counter. One persistent
`aria-live="polite"` region now announces "Pothole recorded. Frame 2 of 50."

### Class is no longer conveyed by hue alone

Each box carries its class name on the image — a sibling DOM layer positioned in percent, not SVG
`<text>`, which `preserveAspectRatio="none"` would stretch. Five hues is exactly where deuteranopia
breaks down, and on the image the stroke had been the only signal.

The swatches keep their hues and gained a border instead: they are tuned to sit on road photography,
and against the cream canvas the yellow measures 1.20:1 and vanishes as a shape — but retuning them
for a chip would hurt the job they actually do, and the class name is always beside them.

### Reachability

`order=blind` and `review=true` were parsed from the hash and honoured by the queue from the start
but had **no control anywhere**, so a blind pass could only be entered by editing the address bar
and the "No finished frames to review yet" empty state was for a mode nothing could reach. Both are
now in a **Pass** group. Check-my-work is a checkbox rather than a chip, because `styles.css`
records the console's own rule that chips are filters over one set and this switches to a different
one. `renderError` also stopped wiping the band chips, so a 403 is recoverable without a page
reload.

### Also corrected

Six `span.dock-group-title` became `h3`, matching `dock.ts`'s own convention — the page had one
heading and no navigable structure. And a **pre-existing race in `map.ts`**, confirmed against
`git show HEAD` before touching: `on('load', addClusterLayers)` was unguarded while `applyTheme`'s
handler was guarded, so flipping the theme within a second of signing in threw
`Source "clusters" already exists` and left the map with no cluster layers.

## One housekeeping note

`migrations/017`'s header was written as "Phase 2.8" before this phase was numbered, and has been
corrected to 2.7d. That is a comment-only edit to an already-applied file, so its checksum no
longer matches the ledger and `run_migrations` now logs *"changed since it was applied; not
re-applying"* for it — verified against `pothole_test`, schema intact. It joins six migrations
(002, 003, 004, 011, 012, 013) that already drift for the same reason. A migration header naming
the wrong phase is the more misleading of the two, but the drift is real and is recorded here
rather than left to surprise the next person who reads the boot log.

## Deliberately not done

- **Submit tested in the browser.** It writes `boxed_at` to real ground truth. Server-side coverage
  is the right place for it.
- **`order=stratified`.** The CLI keeps it; the seam this phase exists to clear only needs `score`.
- **Box labels drawn on the image.** `<text>` inside a `preserveAspectRatio="none"` SVG is
  distorted, so labels need a sibling DOM layer positioned in percent. The box list under the stage
  carries class identity for now.
- **Concurrency control on box writes.** `frame_box` is replace-all with no history table, so two
  reviewers on the same band silently overwrite each other's boxes — `frame_label_history` covers
  verdicts only. Consistent with the router's "NOT org-scoped, deliberately" note; worth knowing
  before a second labeller starts.
- **Accessibility parity.** The verdict path is fully keyboard-operable. Box drawing is mouse-only
  and cannot reasonably be made otherwise; saying so is better than implying parity.

## What this unblocks

Labelling the 1,041-frame seam at ≥0.30, then boxing the positives it finds. That is the only
untried remedy for 2.7b's diagnosis that uses exactly-in-domain data.

One sequencing trap to clear first, and it is sharper than "we need more positives":
`promote_model.py`'s `_LABELLED_SQL` is a **deny-list** — it takes every `frame_label` row and
`--exclude-ids` subtracts the training share, so a frame in neither list lands in the eval set *by
default*. Label 1,041 frames, train on some, and any missing from the exclude file silently become
evaluation data the model trained on, manufacturing an improvement out of leakage. Before training
on seam labels, `promote_model.py` and `detect_eval.py` need a `--holdout-ids` **allow-list** and a
frozen, SHA-256'd `runs/holdout-v2-ids.txt`. `export_labeled_frames._is_train` already splits by
md5 of `client_id` and is stable across runs, so the mechanism exists; the frozen artefact and the
flag do not.
