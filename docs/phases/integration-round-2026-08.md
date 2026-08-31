---
updated: 2026-08-31
---

# The first full integration round

What it took to get collected drive data from `asset_observation` onto an
operator's screen, and what that exposed. For the procedure, read
[`integration-round-runbook.md`](../runbooks/integration-round-runbook.md); this
is the record of why each step exists and what it measured.

**One-line summary.** The pipeline was complete and almost nothing reached the
map, because an unsupervised outlier gate fitted on the features that define a
pothole had learned to reject potholes. Fixing it moved cluster-admitted
observations from **1 to 166**. The round also established, against expectation,
that this system has never produced a corroborated defect.

---

## Context — what the round found on arrival

`pothole_db` held 4,637 observations and 5,615 frames from two devices (4,539 and
98). Every frame had been scored by `yolo11s_pothole_v1`. Against that:

| | measured 2026-08-30, before |
|---|---|
| observations classed `pothole` | 286 |
| …admitted to clustering | **1** |
| clusters | 4 — three real, one named `solo` at the `tests/conftest.py` fixture coordinate |
| clusters on `GET /api/v1/potholes` | 0 |
| frames awaiting fusion | 5,615 — all of them |

Opening the dashboard at its default view showed exactly one marker: the leaked
test fixture. Everything real was 18 km north and off-screen.

---

## 1. The outlier gate — the unlock

`app/sensor_model/features.py` fitted its IsolationForest on
`(ratio, gbar, magnitude, accel_std, speed_mps)`. The first three are the
features on which potholes separate from ordinary driving by 14–15×. An
unsupervised anomaly detector trained on them does the only thing it can: it
learns that potholes are the anomaly and flags them.

This was **already known and already measured**.
[`phase-2.1-fusion-engine-plan.md`](./phase-2.1-fusion-engine-plan.md) recorded
that the gate removed 139 of 140, that tuning `contamination` cannot help
(below 0.05 it flags nothing *but* potholes, so the dial runs between "no gate"
and "no potholes"), and that a class-neutral `(accel_std, speed_mps)` keeps 122
of 140. It was left unapplied because it needs a re-fit and a re-score and
changes what every existing `sensor_is_outlier` means.

The live database agreed at scale: **285 of 286** pothole-classed observations
carried the flag. The cluster member gate is
`sensor_class = 'pothole' AND sensor_is_outlier IS NOT TRUE`, so the entire
sensor arm of the crowd pipeline contributed **one row**.

### What shipped

- **`SENSOR_OUTLIER_FEATURES`**, defaulting to `accel_std,speed_mps`. A setting
  rather than an edited constant, because the old behaviour has to stay
  reproducible for comparison.
- **`migration 014`** adds `sensor_model.outlier_features_jsonb`. A model must
  record what it was fitted on: scoring it with a different set feeds sklearn a
  vector of the wrong width, or — worse — the right width in the wrong order.
  `NULL` resolves to the legacy five, not to today's default, so a pre-014 row
  still scores exactly as it was fitted.
- **A mismatch guard** in `score.py`: if the stored feature list disagrees with
  the forest's `n_features_in_`, scoring raises rather than guessing.
- **A refit trigger on calibration change.** The fit gate only fired after 200
  *new* observations, and both the feature set and the severity calibration live
  *on* the model — so an operator could edit either, restart, see no error, and
  get no change for however long it took 200 rows to arrive. `run_fit_job` now
  refits when the configured calibration differs from the active model's, and
  logs that existing scores are stale until a re-score.

### Result

| | before | after |
|---|---|---|
| observations admitted to clustering | 1 | **166** |
| cluster member pool | 11 | **192** |
| clusters | 4 | **25** |

### The honest caveat

The gate is better, **not neutral**:

| class | flagged |
|---|---|
| `crack` | 9.0% |
| `not` | 8.4% |
| **`pothole`** | **31.7%** |

Down from ~100%, but still 3.5× the base rate. `accel_std` evidently carries some
pothole signal on real roads — a rough defect raises the local noise floor. The
synthetic fixture in `tests/test_fit.py` shows 3.3% precisely because it makes
those features independent by construction. **Do not read the unit test as
evidence about the field.**

A related artefact: pothole-classed observations went 286 → 243 across the
refit. The old figure was not a like-for-like comparison — those rows had been
scored by **three different model versions** as data arrived. All 4,637 now carry
one `sensor_model_version`.

---

## 2. Severity was saturating, and it would have hidden the fix

Severity is `clamp(SEVERITY_SCALE * magnitude / max(speed, SEVERITY_SPEED_REF), 0, 1)`.
Across *all* observations, scale 2.0 looked fine — 2113 / 1016 / 425 / 1083 across
the four tiers. Restricted to the population the map actually shows it collapsed
completely: **all 286 pothole-classed observations in tier 4, 281 of them at
exactly 1.000**.

That is not bad luck. Potholes are the high-magnitude tail, which is exactly
where the clamp bites. Cluster severity is a median over members, so every one of
the new 25 clusters painted "Severe" — one colour, one radius, tier chips and
legend counts meaningless. Fixing §1 without this would have produced 25 markers
that all looked identical.

Measured over the 166 admitted potholes, the raw ratio runs
`p0 0.50 | p25 1.20 | p50 1.76 | p75 2.24 | p95 3.82 | max 14.49`. **Scale 2.0
saturates at ratio ≥ 0.5 — below the p0 of that distribution.** `1/p95 ≈ 0.26`,
rounded to **0.25**, spreads the same clusters as **2 / 12 / 9 / 2**.

`dashboard/src/severity.ts` had already recorded one instance of this failure
(floors above the ceiling, everything in tier 1). The lesson now written down
there: the tier floors and `SEVERITY_SCALE` are **one calibration split across
two files**, and changing either alone re-breaks the ramp.

This is a fit to one city's data. The runbook carries the re-measure query.

---

## 3. Raw detections are now visible — both kinds

Even after §1, only 110 of 166 admitted observations land in a cluster. A pothole
seen once or twice within 25 m becomes DBSCAN noise and appeared **at no zoom, on
no surface**. "Show the clusters" and "show what was reported" are different
requests, and only the first was answerable.

`GET /api/v1/tiles/observations` already existed, unfiltered by class or outlier
flag, tested — and had never been wired into the frontend. It now is:

- a second vector source with `minzoom: 15`, because the endpoint 400s below
  `TILE_OBSERVATIONS_MIN_ZOOM` and **MapLibre never retries an errored tile**;
- a circle layer coloured by `sensor_class`, drawn beneath clusters, with
  **outlier-flagged points hollow** — they are the rows the member gate silently
  drops, so hiding them would make the gate unfalsifiable from the UI;
- a dock toggle, off by default, stating the zoom floor;
- a click popup showing every attribute the tile carries. Unknown keys render
  rather than being dropped, so a new column in `_OBSERVATION_TILE_SQL` appears
  without a frontend change.

**A caught bug worth recording.** The source was first written as
`minzoom: 15, maxzoom: SOURCE_MAX_ZOOM` — and `SOURCE_MAX_ZOOM` is 14. A vector
source whose maxzoom is below its minzoom has an empty range and silently fetches
nothing. It presents identically to "no data". It now has its own
`OBSERVATIONS_MAX_ZOOM = 16`.

The first observation opened in the popup read **`P(pothole) 1.000`, "Rejected by
the outlier gate"** — §1's pathology, legible in one click.

### And the camera half

The sensor layer answers "where did a wheel hit something?". It says nothing
about the other input, and the two sets barely overlap — 98.6% of pothole-classed
observations have no coincident frame, and there are more frames (5,615) than
observations (4,637). So `GET /api/v1/tiles/frames` was added alongside, with the
same zoom floor (`tile_frames_min_zoom`, its own setting rather than a shared
one) and a matching source, layer, toggle and popup.

The tile LEFT JOINs `fusion_pair`, because the interesting question about a
camera detection is **not its score but whether it reached fusion**. A frame that
scored 0.9 and paired with nothing contributed nothing to any cluster, and no
arrangement of the score alone says so. Encoded as the same visual grammar the
observations layer uses: **hollow means it did not contribute** — there an
outlier-rejected reading, here an unpaired frame. Radius carries
`server_probability`, because the mean is 0.151 and only 352 of 5,615 exceed 0.5,
so without it the layer is a uniform smear. Unscored frames are grey rather than
hidden: the detection backlog is exactly what this layer is well placed to show.

The measurement it surfaces:

| | count |
|---|---|
| frames scored ≥ 0.5 | 352 |
| …that are the primary frame of a pair | 123 |
| …that paired with nothing at all | **38** |
| frames unpaired at any score | 1,183 of 5,615 |

The first one clicked read **`server p 0.757` · "Scored but unpaired — no sensor
event matched, so it reached no cluster"**. That is the camera-side counterpart
of §1: a confident detection the pipeline discards, previously invisible.

---

## 4. The corroboration finding — the round's real result

`min_devices` is now an optional query parameter on `/potholes` and
`/potholes/detail`, defaulting to `CLUSTER_MIN_DISTINCT_DEVICES` so the frozen v1
contract is untouched (pinned by a test). `scripts/device_gate_eval.py` sweeps it.

The plan was to set the default to 1 for the round, on the theory that the floor
was hiding real defects. **The sweep refuted that.**

```
Member time span per cluster (is this corroboration, or one pass?):
        < 10 s    23   92.0%
       10-30 s     2    8.0%
  median span: 2.0 s
```

Every cluster is one car, one pass, spanning a median of **2.0 seconds**. None
spans more than a day. The cause is structural: `CLUSTER_EPS_M` is 25 m and the
measured median speed 13 m/s, so **25 m is 1.9 seconds of travel**. "Three
detections within 25 m of each other" is trivially satisfied by a single
drive-past of one rough patch.

So `CLUSTER_MIN_POINTS = 3` has never required corroboration. The
`distinct_devices >= 2` floor is the only thing that ever did, which makes it
**load-bearing rather than merely conservative** — the opposite of the
assumption. Lowering it to 1 would not reveal hidden confirmed defects; there are
none. It would publish 25 single-pass artefacts to every phone.

The default was therefore left at 2, and the loop demonstrated with
`min_devices=1` as a query parameter instead: `[]` by default, **24 potholes**
with the override.

**The fix this points at** is corroboration by distinct *passes* —
`(device_id, time bucket)` — rather than distinct devices. One car over the same
defect on three separate days is real evidence that this pipeline currently
cannot express, and it would let a single-vehicle survey confirm anything at all.
Not implemented: it changes `_MEMBERS_CTE`/`_CLUSTER_SQL`, and it deserves its own
measurement rather than being folded into this round.

---

## 5. Housekeeping that turned out to matter

- **`scripts/storage_audit.py`** reconciles `storage/frames` against
  `asset_frame` both ways. It independently reproduced the two recorded figures —
  59 orphans, 425 `demo-dev-*` files — and the store now matches the table
  exactly (5,615 ↔ 5,615, zero orphans, zero dangling).
  Its deletion guard **refuses a scratch database**: against a TRUNCATEd
  `pothole_test`, every real frame looks like an orphan, so `--delete-orphans`
  would destroy the archive. That is the opposite direction from `seed_demo.py`
  and the same as `label_frames.py`. Ten tests, because the guard is the point.
- **The `solo` cluster** — a test fixture at `43.6532, -79.3832` that had leaked
  into the collected database — was removed. 24 real clusters remain.
- **The legend covered the zoom control and the attribution.** Playwright found
  it deterministically (`.legend intercepts pointer events`). The attribution
  half is a basemap licence obligation, not cosmetics. The control stack height
  is now measured at runtime and published as a CSS variable, because the
  attribution re-wraps on a narrow pane.
- **CI now exists.** `.github/workflows/ci.yml` runs ruff and the full suite
  against a `postgis/postgis:16-3.4` service on `pothole_ci` — a database name
  `tests/conftest.py` has allowed since it was written and nothing had ever used.
  `_in_ci()` turns an unreachable database into a failure instead of a silent
  mass skip, which is the exact shape of green-but-meaningless a CI run would
  otherwise report.

---

## 6. Day two — the evidence model, and what it proved

Everything above got data onto the screen. This half changed what the pipeline *counts*, and
mostly produced negative results.

### Passes, not just devices (`migrations/015`)

The paper integrates "from multiple users **and/or multiple passes** of any road segment", and
its own five-survey validation was one phone on five days. The server counted only distinct
devices, so that campaign scores 1. A pass key is now derived from each device's full timeline —
a contiguous run with no gap over `CLUSTER_PASS_GAP_MINUTES` (20). `distinct_passes` and
`member_span_s` land on `asset_cluster`, and the read path admits a cluster on **either** floor,
both overridable per request.

### `CLUSTER_MIN_POINTS` 3 → 1

The paper has no quorum; a lone detection forms a cluster. The old 3 was not corroboration
either — 25 m is 1.9 s of travel at the measured median speed — but it *was* discarding **87 of
191 admitted members (46%)** as DBSCAN noise. Clusters 23 → 90. Safe only because publication is
now gated separately, pinned by a test.

### Sweep-wise assignment with an adaptive radius

`ST_ClusterDBSCAN` could never implement §4.4: it takes one scalar eps, where the paper buffers
each event at 2σ of *its own* GPS accuracy (median 6.8 m here, against a flat 25 m). It also
chains — A–B, B–C, therefore A–C at any distance.

Replaced with the paper's rule: within-sweep collapse first (our phone re-triggers where the
paper's emits one anomaly per defect), then each sweep-event matched against cluster centroids
**as they stood before that sweep began**.

| | before | after |
|---|---|---|
| clusters | 90 | 150 |
| widest cluster | **124 m** | **19.9 m** |
| clusters > 25 m across | 15 | **0** |
| member pairs beyond their own 2σ | 204/258 (79%) | 1/51 (2%) |

A 124 m "single pothole" was a stretch of road drawn as one marker.

### Rate limiter moved into Postgres (`migrations/016`)

`device_rate_limit` had sat unused since migration 001 while the live limiter kept module-level
dicts — so `--workers 2` enforced **double** the configured ceiling, inconsistently. Verified
live: five requests across two workers produced one shared counter of five. It fails open and
logs at ERROR, because a device that cannot upload loses drive data permanently.

### The window trap

Collection ran 2026-08-16 → 08-29. Under the 30-day default the member gate would have begun
dropping rows on **15 September** and the last on **28 September**, at which point the job
returns zero clusters and the dashboard empties — no error, no log line. The dev `.env` now sets
`CLUSTER_WINDOW_DAYS=3650`; the default stays 30, which is right for a deployment.

Finding it also exposed that **the test suite inherits the developer's `.env`**: three tests
assert on the window by construction and failed. `CLUSTER_WINDOW_DAYS` is now pinned in
`tests/__init__.py`. That failure was the harmless direction — the dangerous one is the same
mechanism making an assertion silently vacuous.

### What none of it achieved

**Still zero corroborated defects.** §4's finding survived every change above, and two further
measurements closed the remaining hypotheses: no clustering parameter recovers a cross-day
repeat, and neither does per-survey classification (`crowd_sweep.py --sweep` / `--reclassify`,
recorded in [`paper-fidelity-assessment.md`](../research/paper-fidelity-assessment.md) §4b–4c).
The crowd layer is now faithful to the paper and has nothing to integrate.

### Day three (2026-08-31): why there is nothing to integrate — and the retraction

The zero stands. The explanation given for it above does **not**, and
[`paper-fidelity-assessment.md`](../research/paper-fidelity-assessment.md) §4d has the full
working.

Deriving sessions from a 20-minute gap gives 12 sessions over 2 devices, and they split
cleanly into two **instrument regimes** on `gbar_in_max / accel_max_g`: nine sessions at
1.75–3.48 producing 0–4.7 % potholes, three at 9.91–19.05 producing 20.4–24.2 %, with an
empty corridor between. Raw `accel_max_g` and `accel_std` span the *same* range in both
bands, so the roads and the forces were comparable — only the app's derived window features
differ (`magnitude` 4.1x, `gbar_in_max` 5.0x). One phone samples at ~238 Hz against the
other's 29.81 Hz; 2026-08-25 shifted for a second, separate reason that is not a constant
gain.

**§4c's null pooled both regimes**, which is what made the zero look impossible. Redone
within one regime it is ordinary — p ≈ 0.49 against a null whose own median is 2 — and a
denser class over the same roads and days co-locates *above* the top of its null, so the
test demonstrably has power. The corrected verdict is **underpowered, not irreproducible**:
106 pothole detections over 5 days cannot corroborate and should not be expected to.

Two consequences for planning. Repeat-route collection in one unchanged instrument state
becomes the top item, because no analysis is possible without it. And session-provenance
upload drops from *blocking dependency* to *confirmation*, because the regime is already
fingerprintable server-side from fields on the wire today — `time_in_max` grid spacing for
the sample rate, median `gbar/g` for the energy-persistence regime.

---

## Verification

- **453 tests pass** (up from 429), ruff clean, `tsc --noEmit` clean.
- **In a browser**, signed in as a real admin: markers across three severity
  tiers at z13; raw observations rendering at z16 with hollow outliers; the
  popup; repair and reopen; **zero console errors**; zoom control and attribution
  both visible and clickable.
- **Tiles decoded, not eyeballed**: z13 → 5 individual clusters with full
  attributes, z12 → 11 aggregate bins, z15 observations → 69 features.
- **The read path**: `[]` by default, 24 items with `min_devices=1`, 2 aggregate
  bins at z10.

## What is still open

- **No cluster has two devices.** A second vehicle over the same roads is the
  only thing that proves crowd corroboration, and no server change substitutes
  for it.
- **The outlier gate still over-flags potholes 3.5×.**
- ~~**The 30-day window is invisible in the UI.**~~ **Mitigated 2026-08-31.** This
  data runs 2026-08-16 → 08-29, so under the 30-day default it would have begun
  vanishing on **2026-09-15** and been entirely gone by **2026-09-28** — the
  clustering job returning zero clusters and the dashboard empty, with no error
  and no log line. `.env` now sets `CLUSTER_WINDOW_DAYS=3650` for the dev
  database. The default stays 30 and should: a rolling window is what lets a
  resurfaced road stop being reported.
- **Repair is admin-only**, because the clustering job assigns no `org_id`.
- **`FUSION_FRAME_ONLY_ENABLED` is still off**, and its justifying comment —
  "`server_probability` is NULL on every frame" — is now stale. Worth reopening,
  but not during a round that already changed the member population.
- **Phase 2.6 hardening is untouched**: shared rate limiter, per-IP limits, frame
  GC, TLS. Those gate a pilot, not a demo.
