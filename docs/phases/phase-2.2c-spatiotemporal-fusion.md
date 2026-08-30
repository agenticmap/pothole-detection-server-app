---
updated: 2026-08-23
---

# Phase 2.2c — Spatiotemporal crowd fusion

> Status: **Implemented.** The integration half (§4.5) and the direction-awareness of the
> assignment step (§4.4) of *Probabilistic-based crowdsourcing technique for road surface anomaly
> classification* (Sattar, rev. Aug 2018 — the `.docx` in the repo root). 322 tests pass, up from
> 290. Enabled by default and inert on existing data, because every observation in `pothole_db`
> predates the class-posterior column and therefore takes the legacy path.

## Why

The question "is there any event fusion across multiple users?" had an uncomfortable answer. Two
fusion layers existed and neither combined evidence across users:

- **Sensor↔camera fusion** (`app/fusion/service.py`, `_PAIRING_SQL`) joins
  `ON o.device_id = f.device_id` — strictly same-device. Multi-*modal*, not multi-*user*.
- **Clustering** pooled observations across devices but only *aggregated* them: `avg(confidence)`,
  `percentile_cont(0.5)` for severity, `count(DISTINCT device_id)`. So `distinct_devices` was
  computed and stored but was never an input to confidence — only a read-path visibility filter
  (`app/config.py`). Worse, because `avg()` ran over *observations*, one phone firing eight times
  over the same pothole outweighed two other devices reporting once each.

The paper specifies exactly the missing layer, so it was implemented rather than invented.

## What was built

### 1. The per-event class posterior is persisted (`migrations/011`)

`app/sensor_model/score.py::_class_posteriors` always computed the full `{pothole, crack, not}`
distribution and `score_observation` kept only the argmax and `P(pothole)`. §4.5 integrates
distributions, not scalars. `ScoreResult` now carries the vector and the fusion job writes it to
`asset_observation.sensor_class_probs`. **The cheapest change in the phase — the value already
existed at the point of scoring.** Pinned by
`test_scoring_persists_the_full_class_posterior`, which also asserts the scalar and the vector
cannot drift apart.

### 2. Spatiotemporal weighting (`app/fusion/spatiotemporal.py`)

Each member gets a Gaussian RBF weight (Eq. 2), computed twice — over distance to the cluster
centroid and over age relative to the newest member — with γ = 1/(2σ²) and σ from that cluster's
own spread, so a tight cluster discriminates finely and a loose one does not. The two weights are
summed and normalised, then multiplied into each member's class distribution.

Verified against the paper's own worked example (Table 8): the most recent detection takes the
highest temporal weight, and a member ~13 m from the centroid is driven to near-zero spatial
weight, which is what the paper reports.

### 3. Direction-aware cluster identity (§4.4)

Two carriageways of one road are within `eps_m` of each other and are separate defects.
`_split_by_direction` splits each spatial group into direction sub-groups by single-linkage over
*circular* distance, and `_FIND_EXISTING_SQL` gained a circular bearing check so a group only
merges into a cluster with a compatible heading. `asset_cluster.bearing_deg` stores the circular
mean of its members.

Verified end to end: northbound-then-southbound surveys of the same spot now produce **two**
clusters of three members with bearings 0° and 180°, while surveys at 350° and 10° — 20° apart —
correctly produce **one** cluster of six.

## Three things measured during implementation that changed the design

**The Dirichlet-multinomial's concentration is not identified, and cannot mean "corroboration".**
This was assumed at planning time and is wrong. When members agree closely the observed
over-dispersion is zero, so the ML concentration is genuinely *infinite* and Minka's fixed-point
iteration simply climbs until stopped. Measured: identical members returned ~200 under a
200-iteration cap and ~183 with ten members — the number was reporting the cap, not the data. It
is now bounded (`_MAX_CONCENTRATION`) and flagged via `converged`, and the module docstring says
plainly that cluster corroboration lives in `distinct_devices`.

**So the distribution is computed in closed form.** The fit's *mean* provably converges to the
weight-normalised mean of the members, and measurement confirmed it exactly (identical members at
`[0.6, 0.2, 0.2]` fit to `[0.598, 0.201, 0.201]`; three fully disagreeing members fit to uniform —
both the weighted mean). Iterating to reach a value with a closed form would be slower and would
risk reporting a cap-limited number as an estimate.

**The paper's method does not accumulate evidence across users.** Three devices agreeing at 0.6
integrate to the same 0.6 as one device would. Its contribution is *spatiotemporal weighting*, not
evidence combination. This is pinned by
`test_the_paper_s_method_does_not_accumulate_across_devices` so nobody later "fixes" the weighting
expecting agreement to raise confidence. `CLUSTER_PRIOR_CONCENTRATION` is offered as an explicit
**extension beyond the paper** — a symmetric Dirichlet prior that shrinks small clusters toward
uniform so they only approach their observed value as corroborating members accumulate. Default
`0.0`, which reproduces the published result exactly.

## Conflicts between the paper and the codebase

Ten were identified before implementation; two changed shape during it.

| # | Conflict | Resolution |
|---|---|---|
| C1 | The paper assigns incrementally and allows one event in several clusters; DBSCAN gives one label per point and re-clusters from scratch | Direction split in Python over DBSCAN's spatial groups. **Multi-cluster membership is deferred** — see below |
| C2 | Multi-cluster membership would break count disjointness | Moot while C1's membership half is deferred |
| C3 | §4.4 buffers by 2σ of GPS accuracy; Android's *bearing* accuracy is never uploaded | Fixed `CLUSTER_BEARING_TOLERANCE_DEG` (45°) until Phase 2.8 adds the field (`app-capture-findings.md` F3) |
| C4 | `asset_cluster` had no bearing | Added in `migrations/011` |
| C5 | The per-event distribution was computed and discarded | Persisted — see above |
| C6 | The RBF's units are ambiguous in the paper | Two independent 1-D kernels. §4.5 first says "Euclidean distance of both time and location" (dimensionally meaningless — seconds and metres in one norm) then "the weigh values of time and location … should be summed", which is well-defined |
| C7 | γ from per-cluster σ divides by zero on identical members | σ floors, plus a uniform-weight fallback when the weight sum underflows |
| C8 | Time was a hard 30-day cliff, not a decay | The window stays as an outer bound; the RBF weights inside it |
| C9 | The paper's accuracy assessment counts cracks, manholes and catch basins | Not reproducible under the "potholes only" framing chosen for this server. Recorded, not attempted |
| C10 | `confidence` changes meaning | Documented; the legacy mean is one flag away, and it is what real data currently gets anyway |

**Two corrections made during implementation, recorded because the first attempt shipped a
demonstrably wrong behaviour before tests caught it:**

- Applying the bearing check only when *matching* a group to an existing cluster is **not
  sufficient**. DBSCAN had already merged both carriageways into one 6-member group whose circular
  mean heading was the meaningless bisector (0° and 180° average to 90°); that group then failed the
  bearing check and was inserted as a *new* cluster spanning both directions, orphaning the
  original. The grouping itself has to be direction-aware.
- The first fix — partitioning DBSCAN on a fixed 90° heading sector — split any road lying on a
  sector boundary. A test with headings of 350° and 10°, 20° apart, correctly failed. Replaced with
  single-linkage over circular distance, which has no boundary.

## An unrelated finding worth acting on

While checking the real-data impact: **139 of 140 pothole-classed observations in `pothole_db`
are flagged as outliers** — 99.3% — against 5.7% of cracks and 13.9% of `not`. Overall the rate is
13.2%, close to the configured `sensor_iforest_contamination = 0.1`.

That is mechanically unsurprising and practically serious. The Isolation Forest is fitted on a
*superset* of the classifier's features — `(ratio, gbar, magnitude, accel_std, speed_mps)` against
the classifier's `(ratio, gbar)` — and potholes *are* the high-energy tail of the first three, so an
unsupervised outlier gate at 10% contamination removes precisely the strongest signals the pipeline
exists to find. (Corrected: this section originally said "the same two features as the classifier".
The distinction matters, because it means adding features has already been tried.
`phase-2.2d-pairing-search.md` measures the whole thing — no `contamination` value both gates and
retains potholes, but a class-*neutral* feature set keeps 122 of 140 at the unchanged 0.1.) With `_MEMBERS_CTE` requiring `sensor_is_outlier IS NOT TRUE`, the dev
database has **one** clusterable pothole member, and `cluster_min_points = 3` means no cluster can
form from it.

So "no clusters because there is only one device" was only part of the story; the outlier gate is
the larger cause. `docs/phases/phase-2.1-fusion-engine-plan.md` listed tuning
`sensor_iforest_contamination` on pilot data as an open item — and Phase 2.2d has now measured it
and found that tuning **cannot** fix this: below contamination 0.05 the gate flags nothing but
potholes, so the dial runs between "no gate" and "no potholes". The fix is the feature set, not the
threshold. Still sensor-model work, not this phase's.

## Verification

- **322 tests pass** (290 before): +26 unit tests on the maths, +5 clustering DB tests, +1 on
  posterior persistence.
- Two of the paper's worked numbers reproduced qualitatively (Table 8's recency ordering and its
  ~13 m near-zero-weight member).
- Order independence, single-member clusters, σ=0, all-zero weights, and negative priors all pinned.
- Carriageway separation and the 350°/10° circular case verified end to end through the job.
- **Real data**: enabling this on `pothole_db` changes nothing — 0 of 2728 observations carry a
  posterior, so every cluster takes the legacy-mean path
  (`test_members_without_a_posterior_fall_back_to_the_legacy_mean` pins that). Re-scoring
  (`UPDATE asset_observation SET scored_at = NULL`) is the operator action that activates it.
- `CLUSTER_SPATIOTEMPORAL_ENABLED=false` and `CLUSTER_BEARING_AWARE=false` restore the previous
  behaviour.

## Deferred

- **Multi-cluster membership** (C1). §4.4 lets one event join several clusters (its Figure 2, cases
  A and B); DBSCAN assigns exactly one label. `observation_cluster_link` already has the right
  shape — PK `(cluster_id, member_id, kind)` — so no migration is needed when it lands, but
  `observation_count` and `distinct_devices` stop partitioning the observations at that point (C2)
  and `/clusters/stats` would need to say so.
- **DPGMM** (§4.2). The fixed 3-component `GaussianMixture` stays: `sensor_class='pothole'` is
  depended on by the clustering member filter, the public read path and the dashboard, and the
  paper's per-segment variable class count would break all three. sklearn 1.6.1 already ships
  `BayesianGaussianMixture(weight_concentration_prior_type='dirichlet_process')` if it is ever
  wanted, but note it truncates at `n_components` and under-estimates posterior variance, so its
  class count can differ from the paper's Gibbs run.
- **The 2σ accuracy buffer** for assignment (C3), pending the bearing-accuracy wire field.
- **Per-device reliability weighting.** The paper motivates it — different vehicles and sensors
  respond differently — but does not implement it either. It needs Phase 3's labelled data.
- **Road-segment partitioning.** The paper classifies per segment, derived from bearing-change
  segmentation on the phone. The server has no segment concept and the app's `survey_session`
  segments are never uploaded (`app-capture-findings.md` F5/F6).
