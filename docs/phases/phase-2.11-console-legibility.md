---
updated: 2026-09-03
---

# Phase 2.11 — Making the console say what it knows

> Status: **Shipped.** Five commits, `8868f1d`..`d68c253`, plus the document they kept pointing at
> ([`from-reading-to-defect.md`](../architecture/from-reading-to-defect.md)).

Four rounds that look unrelated and are not. Each began as "this surface is showing me something,
but not what it means": markers that could not be told apart, photographs stored on their side, a
verification backend with no way to invoke it, and a map that would not show you the picture the
marker was about. The connecting thread is **legibility** — the pipeline knew all of this already.

---

## 1. One shape per kind of thing

The map drew three different kinds of record and painted all of them as circles.

**The defect was exact, not aesthetic.** Sensor observations were painted from `--severity-4` and
`--severity-2`, which are the same variables the *cluster* layer uses. A pothole-classed observation
and a Severe cluster resolved to the identical string `#8c491a` — not similar, **bit-identical** —
and camera frames differed from observations by half a pixel of radius. Three record types, one
appearance. An operator asking "what am I looking at?" had no way to answer from the screen.

Colour was the wrong carrier anyway. Severity already owns the colour axis on this map, and a
categorical palette competing with an ordinal one on the same channel cannot be read. **Shape
survives greyscale, which is the test colour cannot pass:**

| shape | record |
|---|---|
| circle | an `asset_cluster` — a defect candidate |
| triangle | one sensor observation |
| square | one camera frame |

with three modifiers that apply across shapes: **hollow means it contributed to nothing** (an
observation the outlier gate rejected, or a frame that paired with no sensor event), grey means not
yet scored, and a large circle carrying a number means many defects grouped.

> **The parenthesis above was wrong, and §6 fixes it.** An outlier-flagged observation is not the
> same thing as one that contributed nothing — 25 of them are cluster members. The shape grammar
> survived; the fill's meaning did not.

The recolour itself was a correction, not an invention. `tokens.css` already defined a categorical
`--review-class-*` palette and said what it was for; only the review surface had ever obeyed it. The
map now reads from the same variables, so a pothole box in review and a pothole event on the map are
the same colour by construction.

**Two implementation traps, both recorded in the code:**

- **Pre-tinted bitmaps, not SDF.** MapLibre's `icon-color` applies only to SDF images. The icons are
  generated as `ImageData` at `pixelRatio: 2` in `dashboard/src/map/marker-icons.ts`, one bitmap per
  (shape, role, hollow) combination, tinted at construction.
- **Registration lives inside `addClusterLayers`.** `setStyle` discards imperatively added sources,
  layers **and images**. Registering the icons anywhere else means the markers silently vanish the
  first time someone toggles dark mode — a failure invisible in light mode, which is where it would
  be tested.

Also fixed while in there: aggregate bubbles and individual clusters shared a radius range, so "40
defects here" and "one defect here" could render identically. The aggregate ramp now starts at 13,
above the largest individual tier (11), and a new `aggregateCountLayer` writes `point_count` as a
symbol label. The legend grew to 12 rows to carry the grammar.

## 2. Twenty frames stored sideways

20 of 7,615 frames are 640×480 landscape holding a scene rotated 90° clockwise. The cutover is
clean — last landscape 2026-08-18 20:28, first portrait 2026-08-19 13:15 — so this is one app build
predating `setOutputImageRotationEnabled(true)`, not an ongoing fault.

> ### EXIF was never involved, and the first diagnosis said it was
>
> The reported cause was that the ingest metadata strip destroyed the orientation record. It did
> not: **the app writes no EXIF at all** (0 of 300 frames sampled carry an APP1 segment). With no
> tag, the pixel buffer's own shape is the only orientation record there has ever been. That is why
> the selector is a shape test and not a tag read.

**The fix had to be at rest, not at render.** Every box coordinate in `frame_box`,
`server_detections` and `device_detections` is normalized against the stored pixels. A display-time
rotation would leave every stored box describing a different image than the one on screen. So
`scripts/fix_frame_orientation.py` rotates the JPEG and transforms the boxes with it:

```python
def rotate_box_cw(x, y, w, h):
    """(x, y, w, h) -> (1 - y - h, x, h, w)"""
    return (1.0 - y - h, x, h, w)
```

It also clears `detected_at` on the frames it touched, so the worker rescores them. It deliberately
does **not** transform `server_detections` — those scores came from an ROI crop that was sampling
half sky, and are not worth carrying forward.

**Measured effect of turning them upright:** mean server probability **0.180 → 0.224**, frames with
at least one box **9 → 13**. A small absolute number, but the detector was being asked to find road
damage in a picture where the road ran vertically.

> **A near-miss worth recording.** The first version called `rotated.save(path)` inside
> `with Image.open(path)` and truncated its own source file. The backup made it recoverable. The
> shipped version decodes, rotates and re-encodes **entirely in memory**, then atomically replaces
> via a `.jpg.tmp` sibling.

`apply_exif_orientation()` in `app/services/jpeg_metadata.py` is the **ingest guard for the next
source**, not a fix for this one: it walks the TIFF header without decoding, applies the transform,
and runs before `strip_jpeg_metadata` — which drops APP1 and would otherwise delete the only
orientation record a tag-writing client ever sent. It returns the identical object when the tag is
absent or 1, so the common path allocates nothing.

The `t` key (**"Turn the frame 90°"**, in both review key maps, and `Turn 90°` in the frame viewer)
is the operator remedy for the next one-off. It is view-only, resets on every frame change, and
**pauses drawing while the frame is turned**, because a box drawn on a rotated view would be stored
against the unrotated pixels — the same coordinate hazard, one layer up.

## 3. A VLM request that is possible, and deliberately not offered

Phase 2.9 and 2.10 established the verification backend and then measured it: recall **0.015**,
precision 0.200 against a base rate of 0.191, ~40 s per frame on CPU. The obvious next move is an
"ask the VLM about this frame" button in the console. It was built up to, and stopped one step
short.

`migrations/018_vlm_on_demand.sql` adds `asset_frame.vlm_verified_at`, kept **separate from
`detected_at`** on purpose. Collapsing "the detector scored it" and "a VLM checked it" into one
timestamp is precisely the mistake that `boxed_at` vs `boxes_drafted_at` exists to prevent one layer
up: two different attestations, two different columns, or neither can be queried honestly.

`app/services/user_quota.py` is a sliding-window quota that is per-**user** and resource-generic,
and it **fails closed** — if it cannot account for the request, it raises. That is the deliberate
opposite of `app/middleware/rate_limit.py`, which is device-keyed, knows only `events` and `frames`,
and fails open. The difference is the cost model: dropping a rate-limit accounting write costs an
ingestion slot, while dropping a quota write costs 40 s of somebody else's CPU.

`VlmProfile` in `app/detection/vlm/registry.py` is frozen and carries `api_key_env` — the *name* of
an environment variable, never a value, so a profile can be logged.

**No endpoint and no button, and the migration header says so.** At recall 0.015 the model finds
almost nothing, and at 40 s per frame the operator waits. A button that makes a triaging operator
wait forty seconds to receive a confident wrong answer is worse than no button. The plumbing exists
so that a measurement which changes those numbers can be acted on in an afternoon.

`vlm_verified_at` is non-null on **0 frames**, which is the intended state.

## 4. The photograph in the popup, and the gap made visible

Clicking a camera-frame marker gave a text popup about a photograph you could not see.

**The popup now carries the image**, with four things handled rather than discovered:

- **Its own `Popup` instance.** The observation layer's popup was being reused; sharing it means an
  observation click silently discards a frame's in-flight fetch and leaks its object URL.
- **Abort and revoke bound to `close`.** `map.ts` previously registered zero `close` handlers and
  contained zero `AbortController`s.
- **A generation guard**, extracted as the pure `reducePreview` reducer in
  `dashboard/src/map/frame-preview.ts`. Click marker A then marker B and A's fetch can still land;
  the reducer **drops** a load for a frame no longer showing. It is a reducer rather than an `if`
  inside a callback so that it can be specced — the suite is node-environment with no jsdom.
- **The image box is reserved before the fetch**, so the popup does not resize when the blob decodes.

**"Open full size" needed a new endpoint.** The frames tile carries `server_box_count` and not the
boxes, so `GET /api/v1/frames/{client_id}` (`ViewerOrAbove`) was added over `asset_frame` LEFT JOIN
`fusion_pair`, reusing `parse_detection_boxes` and `parse_vlm_verdict` unchanged.

It needed **its own response model**. `ClusterFrameItem.paired_observation_id` is required, but the
frames layer deliberately includes frames that paired with nothing — and those are often the
interesting ones, a frame the detector scored highly that reached no cluster. `FrameDetailResponse`
makes the pairing fields nullable; the LEFT JOIN is what lets an unpaired frame survive the query at
all. On the TypeScript side `FrameDetail` is the wider type, so a `ClusterFrameItem` is assignable to
it and not the reverse.

Both call sites inherit the standing rule: **never select `jpeg_url` or `device_id`**. The stored
path is `{device_id}/{client_id}.jpg`, so exposing it leaks the device identifier in a single field.
The authenticated `image_url` is built server-side instead.

**And the console now says what it does not have.** The dock read "204 open defects in view" while
every one of those was a single drive-past that the public API would refuse to publish. A
**Corroborated** KPI now sits beside it, rendered as a ratio — `0 of 204`, never a bare `0`, because
the gap is the information. It is computed server-side from the *same predicate*
`/api/v1/potholes` applies (`distinct_devices ≥ 2 OR distinct_passes ≥ 3`), so the console and the
public read path cannot drift into disagreeing about what counts as confirmed.

The detail panel gained the matching per-cluster sentence via `panel/corroboration.ts` — *"All
observations within 12 s — one drive-past, not repeat corroboration."* — and the header query gained
`distinct_passes` and `member_span_s` to support it. `distinct_passes` was declared optional on the
TypeScript side while the server never sent it, so a `?? 0` had been rendering **"0 passes" for every
cluster**; it is now required, and a server that stops sending it fails to typecheck.

## 5. The popup the photograph would not fit inside

Adding the image made the frame popup tall enough to expose two latent layout bugs, both
reproduced by measurement before anything was changed.

**It was clipped by the map's own edge.** Measured: a **544px** popup in a **702px** map, opened at
a marker 226px from the top — so there was room on *neither* side, and MapLibre picks the roomier
anchor but never shrinks or repositions after that choice. The popup ran 68px past the bottom and
cut the **"Open full size" button in half**, which is the one action the popup exists to offer.

**And the dock and the legend were painted over it.** MapLibre leaves a popup at `z-index: auto`
while both overlays sit at `--z-overlay: 40`, so the legend covered the right 96px of every popup
opened near it — the photograph's edge and the close button.

Three changes, in the order they matter:

- **`--z-popup: 60`**, between the overlays and `--z-modal`. A popup answers a click the operator
  just made; the two persistent overlays can be covered. Still below the modal, so the frame viewer
  continues to cover everything.
- **The map pans so the popup fits.** `map/popup-fit.ts::popupPanOffset` is pure and rect-based —
  the suite is node-environment, and a function taking a `Map` and a `Popup` could not be tested at
  all. Capping the height alone would have "fixed" the clipping by making every popup small; panning
  keeps the photograph full size. When the popup is taller than the map can ever show, it aligns the
  **top** rather than centring, because the title, the image and the close button live there.
- **The title and the action are pinned outside the scrolling region.** The height cap
  (`calc(100vh - 140px)`) is only the backstop for a window too short for a pan to help — and in
  exactly that case a plain `overflow-y: auto` on the content would have scrolled the button out of
  reach again, re-creating the original defect by a different route.

Verified at 1440×780 (popup keeps its full 544px and sits entirely inside the map) and at 1100×560,
the case the cap is for: the popup shrinks to 430px, the facts scroll, and **"Open full size" stays
pinned and clickable**.

## 6. The map described a pipeline it did not have

The operator's report was *"some sensor observations are coloured some not, some clusters some
not"*, plus a cluster built from **one pass** where clustering was supposed to mean corroboration.
Both readings were correct, and investigating produced four defects — two of them statements on
screen that are simply **false**.

**Only pothole-classed readings ever reach a cluster**, and nothing said so:

| class | readings | reach a cluster |
|---|---|---|
| crack | 3,599 | **0** |
| not | 1,781 | **0** |
| pothole, gate passed | 229 | **229 (100%)** |
| pothole, gate flagged | 77 | 25 — all via the frame-pairing path |

- **A third of the triangles had no legend row.** 1,781 `not`-class readings rendered grey against a
  legend listing only pothole and crack.
- **Grey meant two unrelated things** — a `not` reading and an unscored frame — with only the frame
  meaning explained, so a grey triangle read as "not yet scored". Every reading is scored; there are
  **zero** unscored ones.
- **Hollow was mislabelled, and for triangles it was wrong.** The legend has always claimed hollow
  means "reached no cluster". It was driven by `sensor_is_outlier`, which answers a different
  question and gets it wrong both ways: **25 hollow triangles were cluster members** and ~4,971
  solid ones were members of nothing.
- **The popup asserted an exclusion that does not hold** — *"Rejected by the outlier gate — excluded
  from clustering"* — false for those same 25, because the member gate's second path
  (`fused_confidence ≥ 0.5`) never consults the flag.

The fixes make the fill mean one thing on all three shapes — **solid fed something, hollow fed
nothing**:

- `in_cluster` is now carried on the observations tile (a `LEFT JOIN LATERAL` on the link table's
  existing member index), because membership is **not derivable** from any column already there.
  `cluster_id` rides along for the popup. Emitted as an explicit boolean rather than inferred from
  whether `cluster_id` turned up, because ST_AsMVT omits NULL attributes — the one MVT behaviour
  this codebase has already been bitten by.
- The popup now says membership and measurement conditions as **two separate sentences**, because
  "outlier" was being read as "only seen once". It is not a count of anything: the gate sees only
  `accel_std` and `speed_mps`. Measured, flagged readings are the **speed tails at both ends** —
  p05 0.00 and p95 31.56 m/s against 5.65–18.77 for the rest.
- **Hollow now extends to clusters**: a solid circle is corroborated, a ring is a candidate, using
  the read path's own predicate (`isCorroborated`, guarded by a spec that parses `app/config.py` so
  the two languages cannot drift). The candidate's ring carries the severity colour, or emptying the
  fill would have thrown the tier away. **Every circle is a ring today** — all 204 — which is the
  honest picture and matches the dock's `0 of 204`.
- A **Class** chip group (Pothole / Crack / Other, pothole only by default) stops 94.6% of readings
  that can never form a defect from burying the 254 that did.
- The legend is grouped into *Severity* / *What it is* / *Hollow means*, filling both holes.

> **Two bugs found by building this.** The class filter was applied before `addClusterLayers`
> created the layer, so `setFilter` silently did nothing and every class rendered regardless of the
> chips — the filter is now held as state and re-applied at layer creation, exactly as visibility
> already was. And the legend's hollow-triangle swatch was **invisible**: a `clip-path` triangle
> sets `border: 0`, so "transparent background plus a border" renders nothing. Swatch colour moved
> to a custom property, and a hollow triangle is now drawn as a filled shape with a smaller
> surface-coloured one punched out.

## 7. "How come one observation shapes the cluster?"

The operator opened a cluster reading **Observations 1**, **Corroborating passes 1**, and directly
above them **Confidence 1.00**, and asked how one reading can be a defect.

The behaviour is right; the presentation was not.

**Why it is right.** `cluster_min_points = 1` — forming takes one admitted reading, and corroboration
lives on the read path. And nothing could have joined it: the radius is not the 25 m ceiling the
config advertises but `min(2 × accuracy_m, 25)`, which at a median reported accuracy of 4.37 m is
**6.9 m**. Only **94 of 254** admitted readings have any neighbour at that radius; 188 would at
25 m. So 163 of 204 clusters are singletons — and within one pass, two readings 14 m apart (the
measured median gap) are two different rough spots, not one defect seen twice. **Singletons are the
correct unit.**

**Why no threshold moved.** Two findings were recorded in
[`from-reading-to-defect.md`](../architecture/from-reading-to-defect.md) rather than acted on. The
2σ buffer omits the dominant error term — 56% of readings carry whole-second timestamps, worth
**±12.4 m of along-track uncertainty** at 12.38 m/s against the 8.7 m the buffer allows. But
widening it cannot manufacture corroboration: admitted readings with another admitted reading from a
different day number **2 at 7 m, 5 at 20 m, 5 at 25 m, 11 at 50 m, 17 at 100 m**. The same defect is
essentially never detected twice, and that is repeatability, not a parameter. Raising
`cluster_min_points` would only delete candidates, exactly as it did before.

**What was actually wrong.** `confidence` is `GREATEST(sensor_p_pothole, max_fused)`, so on a
single-member cluster it is the sensor model's own posterior — which §2 of the architecture doc says
saturates and must be read as *which component*, not *how sure*. Labelled **Confidence** and printed
above **Observations 1**, it read as strength of evidence. The map had already been made honest (a
hollow ring); the panel had not.

- A **Candidate** / **Corroborated** badge in the panel, from `isCorroborated` — moved out of
  `map/layers.ts` into a top-level `corroboration.ts` so the map, the panel and the dock share one
  predicate and cannot disagree. Outlined rather than filled, to echo the map's hollow ring.
- **`Confidence` → `Classifier score`**, with the caveat in a `title` on the row.
- The corroboration note moved **above** the facts it qualifies, and gained the case it was missing:
  it used to render *"All observations within 0 s"* for a cluster with **one** observation — a claim
  about a set of one, shown on 163 of 204 clusters. It now says it is one reading on one pass.

---

## Verification

Corpus at the close of the round:

| | |
|---|---|
| frames | 7,615 (all scored) |
| observations | 5,686 |
| clusters | 204 — **every one `org_id IS NULL`** |
| labels / positives | 375 / 65 |
| frames with boxes / signed off | 37 / 200 |
| unlabelled seam at p ≥ 0.30 | 1,819 |
| `vlm_verified_at` non-null | 0 (intended) |

Contract checks against the running image: `GET /api/v1/frames/{client_id}` returns 401
unauthenticated, `paired_observation_id` is nullable in the OpenAPI schema, `jpeg_url` and
`device_id` are absent from it, and `corroborated` is present on `ClusterStatsResponse`.

## What this leaves

1. **A `staff` account cannot mark any real detection repaired.** Every cluster the pipeline
   produces has `org_id IS NULL`, and `repair_service.py` requires `admin` for an unowned cluster.
   The 403 surfaces as *"Your account no longer has permission to do that"*, which misdescribes the
   cause. Found while writing the user guide; documented there and here, not patched — it is a
   behaviour change and wants its own round.
2. **The 1,819-frame seam is still unlabelled.** Unchanged from 2.10, and still the only untried
   remedy using exactly-in-domain data.
3. **A v2 holdout allow-list is still mandatory before any retrain on seam labels.** Still not
   needed until then.
4. **`VLM_VERIFY_LOW`/`HIGH` are still 0.40/0.75** with a measurement on the record that does not
   support them.
5. **`FUSION_FRAME_ONLY_ENABLED` is still false.** More labels is the unlock, so it follows the seam.
