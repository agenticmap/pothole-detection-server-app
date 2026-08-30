---
updated: 2026-08-24
---

# Phase 2.2d — The pairing search

> Status: **Implemented.** 339 tests pass, up from 322. Enabled by default. `pothole_db` has been
> re-fused under the new search; the measured before/after is below.
>
> This document is the *why*. For the procedure — applying the migration, measuring, re-fusing,
> fitting the lead band, rolling back — see [`phase-2.2d-runbook.md`](../runbooks/phase-2.2d-runbook.md).

## Why

The question was whether the event fusion *search* could be improved. It could, for a reason that
is geometric rather than statistical: the pre-2.2d search assumed the camera and the accelerometer
observe a pothole at the same place and the same instant.

They cannot. The camera resolves a pothole while it is still **ahead** of the vehicle; the
accelerometer registers it when the wheel arrives. The ranking was

```sql
ORDER BY abs(EXTRACT(EPOCH FROM (f.ts_utc - o.ts_utc))) ASC, ST_Distance(f.geom, o.geom) ASC
```

whose ideal candidate is Δt = 0, Δd = 0 — a frame taken directly on top of the pothole, by which
point it has left the ROI band or is under the hood. The search was optimising for the one frame
that cannot contain the defect.

## What was measured first

All figures from `pothole_db`: 2728 observations, 2916 frames, 1842 pairs, 2 devices.

| # | Finding |
|---|---|
| **M1** | Re-ranking the existing candidate set changed the winner for **713 of 2197 frames (32.5%)** under a 3000 ms window, or **850 of 2302 (36.9%)** under the shipped 8000 ms ceiling. Reproducible with `scripts/pairing_eval.py --diff`. |
| **M2** | The two windows contradicted each other. `fusion_window_ms = 3000` and `fusion_window_m = 25.0`; median speed is **13.02 m/s**, so 3000 ms of travel is **39 m** and 75 m at p90. Above ~8.3 m/s the spatial gate bound and the temporal one was dead weight; below it, the reverse. |
| **M3** | 25 m was narrower than the lookahead it had to span. 40 m brings **2197** frames into range instead of 1842 (**+19.3%**); 60 m reaches 2389. |
| **M4** | One observation won up to **19** frames — no `PARTITION BY o.client_id` — so 1842 pairs covered only **472** observations, and `_MEMBERS_CTE` took `max(fused_confidence)` over them. Across 346 multi-frame events that max inflated the visual term from **0.176 to 0.324**, a bias that grows with N. |
| **M5** | The primary sort key was degenerate: `delta_ms` took exactly five values — `0` (845 pairs), `±1000` (793), `±2000` (204). 46% of pairs tied on it, and 119 observations share an identical `(device_id, geom, ts_utc)` fix so the distance tie-break tied too. |

Two hypotheses were checked and **rejected**, recorded so they are not re-proposed:

- *Frames are permanently lost when marked `processed_at` before their partner event arrives.* The
  race is real — the whole batch was marked regardless of outcome, and 450 candidate pairs have
  `o.received_at > f.received_at` — but **0 frames** were processed-with-no-pair while having a valid
  partner. The 5-minute cadence absorbed it. Guarded anyway (below), not treated as a bug.
- *Pairing penalised confident potholes.* It did not: fusion raised the score in 1838 of 1842 pairs.
  The apparent penalty was an artefact of `sensor_p_pothole ≈ 0.0000` on crack-classed rows.

## What was built

### 1. A lookahead-aware cost replaces the ranking

```
lead_penalty = GREATEST(0, lead_near - delta_m) + GREATEST(0, delta_m - lead_far)
kinematic    = abs(delta_s + delta_m / GREATEST(speed_mps, speed_floor))
cost         = w_lead * lead_penalty + w_kinematic * kinematic
             + CASE WHEN delta_s > 0 THEN forward_penalty ELSE 0 END
```

- **lead_penalty** — metres by which the separation falls outside the camera's usable
  ground-distance band. The band is a *plateau*, not a target: any distance inside it is a
  legitimate view.
- **kinematic** — seconds of disagreement between the observed offset and the one implied by driving
  `delta_m` at this speed. Expected offset is `-delta_m/speed` (negative = frame first), so the
  residual is `|delta_s + delta_m/speed|`. This is what rejects a candidate 30 m away and 0 s apart.
- **forward** — flat charge for a frame taken *after* the event. Admissible, since GPS and clock
  noise straddle zero, but it must lose to any backward candidate.

`ORDER BY cost ASC, event_client_id ASC`; the id keeps the choice deterministic, which the repo's
byte-identical-rerun property requires.

**`FUSION_LEAD_NEAR_M` / `FUSION_LEAD_FAR_M` ship as 5.0 / 30.0, and those are a hypothesis, not a
finding** — the same status the ROI defaults carry in `phase-2.7-detection-enablement.md`. The band
is a property of the lens and the mount pitch, and only 3 potholes in `pothole_db` have a paired
frame, far too few to fit anything. `scripts/pairing_eval.py --fit-lead` replaces them from
`frame_label` once Phase 2.7's labels exist; below 30 labels it refuses to print percentiles rather
than emit noise someone would paste into the config.

### 2. The temporal window is derived from speed

```sql
abs(EXTRACT(EPOCH FROM (f.ts_utc - o.ts_utc))) * 1000
  < LEAST($window_ms_max, 1000.0 * $window_m / GREATEST(COALESCE(o.speed_mps, 0.0), $speed_floor))
```

The two gates are now one constraint. At 13 m/s with a 40 m window this is 3077 ms (≈ the old fixed
3000); at 24.9 m/s it tightens to 1606 ms; at walking pace it opens to the ceiling.
`fusion_window_ms` is gone, replaced by `fusion_window_ms_max` — a ceiling, not the operative
window. `fusion_window_m` rose 25 → 40 so the search can reach the lead band at all (M3).

### 3. `fusion_pair.is_primary` — the best view, not the loudest verdict

Rather than dropping the extra pairs (1:1 matching would discard 1370 of 1842 audit rows), each
observation's lowest-cost frame is flagged, and `_MEMBERS_CTE` reads

```sql
COALESCE(max(fused_confidence) FILTER (WHERE is_primary), max(fused_confidence))
```

The COALESCE is what keeps this inert on un-rescored data: every pre-2.2d pair has
`is_primary = false`, and without the fallback those observations would silently lose their fusion
confidence and drop out of clustering entirely.

`is_primary` is a per-*observation* fact while pairing runs per *frame batch*, so it is recomputed
across all of a touched observation's pairs, not just the batch's. Because
`idx_fusion_pair_primary` is a partial unique index and Postgres enforces it row by row, this is
demote-then-promote in two statements: a single UPDATE that promoted the new winner before demoting
the old would transiently hold two primaries and abort. A partial index cannot be a `DEFERRABLE`
constraint, so there is no one-statement version.

### 4. Re-fusing corrects the table instead of appending to it

Found while running the real-data verification, and it makes the difference between this phase being
shippable and not. `fusion_pair` is keyed `(event_client_id, frame_client_id)`, so the upsert can
only overwrite a pair whose *both* ends are unchanged — and the entire point of the new ranking is
that ~37% of frames choose a different observation. Each of those would have left its old row behind
under the old event id, so a re-fuse would grow a second, contradictory population rather than
replace the first. `_DELETE_PAIRS_FOR_FRAMES_SQL` now clears the batch's frames first and
`RETURNING`s the displaced observations, which are folded into the `is_primary` recompute (an
observation can lose its primary without appearing in the new candidate set).

This is a narrow fix for the frames being reprocessed. The general problem — stale pairs are never
garbage-collected — is still open, below.

### 5. Retry grace

A frame is retired once it has paired, **or** once it is older than `fusion_retry_grace_minutes`
(30). Previously the whole batch was marked regardless. This is the guard for the rejected
hypothesis: the loss did not occur, but observations lag frames (median upload lag 3.2 h vs 1.7 h)
and nothing bounded the exposure. A grace of 0 restores the old behaviour.

### 6. Frame-only cluster members — built, shipped OFF

A frame that sees a pothole nobody drove over contributes nothing, ever. Since **98.6%** of
pothole-classed observations have no coincident frame, vision-without-impact is the largest recall
ceiling in the pipeline. One `UNION ALL` arm in `_MEMBERS_CTE` admits unpaired frames above a
probability threshold as `kind = 'frame'` members — a value migration 001's `CHECK` constraint
already allowed.

`FUSION_FRAME_ONLY_ENABLED` defaults **false** and must stay there for now: `server_probability` is
NULL on all 2916 frames because no model exists, so the only available input is the on-device
probability, whose confidence floor was lowered to ~5% mid-collection (p50 **0.118**, p95 0.487).
Enabling it today would flood clustering with sub-threshold on-device guesses. The gate opens after
Phase 2.7 measures a threshold. `test_frame_only_members_are_inert_while_the_flag_is_off` pins the
off behaviour by monkeypatch rather than a skip marker, so the claim is verified even in a run where
the flag happens to be on.

Paired frames are excluded from the arm: their evidence already reaches clustering through their
observation's `fused_confidence`, and admitting them again would count one sighting as two members.

## Real-data result

`pothole_db` re-fused end to end (2916 frames over 6 batches):

| | before | after |
|---|---|---|
| pairs | 1842 | **2158** (+17.2%) |
| distinct observations paired | 472 | 476 |
| primaries | — | **476** (exactly one per observation) |
| mean `delta_m` | 5.25 m | **15.29 m** |
| mean `delta_ms` | −92.8 | **−536.6** |
| pairs ≥ 0.5 confidence | 11 | 3 |

The two middle rows are the point. Mean separation moved from 5.25 m — where the camera cannot
resolve a defect — to 15.29 m, the middle of the lead band; and the mean time offset became
decisively negative, i.e. frame-before-event, which is what the geometry demands. 478 of 2158 picks
(22%) are still forward-facing, those being frames with no backward candidate at all.
`match_cost` p50 is 3.617, with 824 of 2158 (38%) landing inside the band at cost < 1.0.

**Predicted 2302 frames paired, actual 2158.** The gap is expected and in the right direction:
`pairing_eval.py --diff` deliberately gates on the fixed 8000 ms ceiling so both rankings see one
candidate set, whereas the job applies the tighter speed-derived window. The script's frame count is
an upper bound on what the job pairs, not a prediction of it.

**The clusterable member pool fell from 11 to 4, and that is the interesting part.** Before, the pool
was 7 `not`-classed, 1 `crack`, 2 pothole-but-outlier-flagged, and 1 clean pothole — 10 of 11
admitted by the member gate's `OR max_fused >= 0.5` branch, which bypasses both the class filter and
the outlier gate. After, it is 3 pothole-but-outlier-flagged and 1 clean pothole. **The better
search stopped feeding the precision hole**: attaching camera verdicts to kinematically implausible
partners was what pushed crack- and `not`-classed observations over the confidence floor. The hole
itself is untouched and still listed below, but nothing non-pothole now falls through it.

Still **0 clusters** — 4 members, none within 25 m of two others. That is unchanged and expected:
the blocker is the outlier gate plus a single substantive device, not the search.

## The outlier gate — investigation, no default changed

Requested as measure-and-report. `sensor_iforest_contamination` stays at `0.1`.

**First, a correction to `phase-2.2c-spatiotemporal-fusion.md`, which said the IsolationForest is
"fitted on the same two features as the classifier".** It is not. It is fitted on a *superset*:
`OUTLIER_FEATURES = (ratio, gbar, magnitude, accel_std, speed_mps)` against
`CLASSIFIER_FEATURES = (ratio, gbar)`. The mechanism survives the correction — the classifier's two
features are *inside* the outlier set, so the gate still re-flags whatever the classifier calls a
pothole — but the distinction matters for the fix, because "add more features" has already been
tried.

Measured over all 2728 scored observations:

**Why it happens.** Potholes *are* the tail of the features the gate scores on:

| feature | pothole p50 | crack p50 | not p50 | all p99 |
|---|---|---|---|---|
| `ratio` | **35.50** | 2.33 | 7.91 | 55.23 |
| `gbar` | **74.83** | 4.89 | 20.07 | 108.53 |
| `magnitude` | **16.24** | 1.16 | 4.33 | 23.24 |
| `accel_std` | 0.42 | 0.49 | 0.52 | 0.83 |
| `speed_mps` | 14.16 | 13.67 | 11.74 | 31.49 |

The first three separate potholes from everything else by 14–15×. An unsupervised gate on those
features cannot help but spend its whole contamination budget on the pothole class.

**No contamination value both gates and retains.** Sweeping the shipped feature set:

| contamination | pothole flagged | crack | not | potholes kept |
|---|---|---|---|---|
| 0.001 | 2.1% | 0.0% | 0.0% | 137 |
| 0.005 | 10.0% | 0.0% | 0.0% | 126 |
| 0.010 | 20.0% | 0.0% | 0.0% | 112 |
| 0.020 | 39.3% | 0.0% | 0.0% | 85 |
| 0.050 | 82.9% | 0.1% | 2.2% | 24 |
| 0.100 | **98.6%** | 0.7% | 13.5% | **2** |

Read the crack and `not` columns: at every level below 0.05 the gate flags **nothing else at all**.
So tuning `contamination` is not a trade-off between sensitivity and recall — it is a dial between
"the gate does nothing" and "the potholes are gone". There is no useful middle setting, which means
the open item at `phase-2.1-fusion-engine-plan.md` cannot be closed by tuning.

**A class-neutral feature set does fix it.** Fitting on `(accel_std, speed_mps)` only — capture
quality, the two features that carry *no* class signal per the table above:

| contamination | pothole flagged | crack | not | potholes kept |
|---|---|---|---|---|
| 0.010 | 2.1% | 1.0% | 0.9% | 137 |
| 0.050 | 10.7% | 4.7% | 4.6% | 125 |
| 0.100 | 12.9% | 10.6% | 8.4% | **122** |

At the *unchanged* contamination of 0.1 the flag rate is roughly uniform across classes — which is
what an outlier gate is supposed to look like — and 122 of 140 potholes survive instead of 1.

**Recommendation, not applied:** change `OUTLIER_FEATURES` to a class-neutral set rather than tuning
`contamination`. It belongs to the sensor model, needs a re-fit and a re-score, and would change what
every existing row's `sensor_is_outlier` means, so it is its own change with its own verification —
not a line in a pairing-search phase.

## Verification

- **339 tests pass** (322 before): +13 in `test_fusion_db.py`, +5 in `test_cluster_db.py`, −1
  replaced.
- `ruff check .` — the 2 pre-existing errors only (`app/models/__init__.py:64` F401,
  `app/routes/frames.py:42` UP007).
- Flag matrix, all green: cost ranking on (35 passed), off (31 passed, 4 skipped via
  `requires_pairing_cost`), frame-only forced on (35 passed).
- The kill switch is proved by `test_the_legacy_ranking_is_restored_by_the_kill_switch`, which
  asserts the old ranking still picks the underfoot frame — so the behaviour change is pinned from
  both sides rather than only asserted in the new direction.
- Note the kill switch restores the **ranking**, not the old windows: `fusion_window_ms` no longer
  exists, so a byte-exact revert also needs `FUSION_WINDOW_M=25` and `FUSION_WINDOW_MS_MAX=3000`.

Two tests had to be rebuilt after their first version passed or failed for the wrong reason, which
is worth recording because both traps are easy to fall into again:

- A tie built by mirroring two candidates 15 m north and south of the frame **is not a tie**.
  `ST_Distance` on `geography` is spheroidal, so the two differ in the sixth decimal and the
  kinematic term separates them. The genuine tie is two observations sharing one GPS fix and one
  timestamp — not contrived, since 119 observations in `pothole_db` do exactly that.
- A re-fuse test whose two candidates differed by milliseconds of kinematic residual turned on the
  same rounding: a latitude offset of "15 m" is 14.97 m here. Both now separate candidates by the
  *lead band* (a metres-wide margin) instead.

## Found, deliberately not fixed here

Out of scope by decision, recorded so they are not lost.

- **The logit blend is arithmetically inert on saturated rows.** `sensor_p_pothole` is effectively
  three point masses — crack ≈ 0.0000, `not` ≈ 0.0025, pothole ≥ 0.52 with median **1.0000** — a
  hard-assigning GMM. `logit` clamps at 1e-6, so `p_s = 1.0` contributes `0.5 × 13.8155 = 6.91` and
  the camera would need to report `p_v < 1e-6` to move the verdict. Verified on all three paired
  potholes: visual 0.26 / 0.40 / 0.66 → fused 0.9983 / 0.9988 / 0.9993. **Better frame selection
  cannot help while the sensor term is saturated**; calibration is the higher-ceiling fix and it
  needs Phase 3's labels.
- **A missing modality shrinks the survivor toward 0.5** instead of renormalising the weights:
  `sigmoid(0.5 · logit(0.9)) = 0.75` (`app/fusion/matlab_port_v1.py`). And `fused = 0.5` when both
  terms are absent, which lands exactly on the `>= 0.5` member floor — so a no-evidence pair passes
  the gate. Not reachable on current data (no frame has both probabilities NULL) but reachable by
  construction.
- **The member gate's OR branch bypasses both the class filter and the outlier gate.** The search
  change stopped feeding it (above), but the branch is still there, and 3 of the 4 current members
  are outlier-flagged rows entering through it.
- **`GREATEST(sensor_p_pothole, max_fused)` is a max**, so fusion can only raise a member and never
  lower it — a frame showing clean pavement cannot pull down a false-positive sensor event.
- **Stale pairs are never garbage-collected.** Fixed only for frames being reprocessed (§4). A pair
  whose frame is never requeued survives indefinitely.
- **`received_at` gates the 30-day window while every temporal computation uses `ts_utc`.** Median
  upload lag is 3.2 h for observations and 1.7 h for frames, so the two are not interchangeable.
- **One global `mean_lat` corrects the Mercator eps for every cluster in the database.**
- **`_MEMBERS_CTE` is evaluated twice per run in separate statements** at READ COMMITTED, so
  `inputs_count` can disagree with what DBSCAN saw.
- **`sensor_segment_min_points` and `sensor_bearing_change_deg`** are configured but read by no code;
  **`fusion_pair.severity`** is written and read by no query.
- **`_subgroup_row` remains a hand-rolled Python mirror of a SQL aggregate with no parity test** —
  the audit's highest-ranked gap. This phase added `member_kinds` to it and restored the three arrays
  it had been silently dropping (`member_devices`, `member_severities`, `member_bearings`), which
  removes a latent `KeyError`, but it did not add the parity test it needs. The new cost function was
  deliberately kept SQL-only, with `pairing_eval.py` querying the database rather than
  reimplementing the maths, so that this phase does not create a second such mirror.
- **A composite `(device_id, ts_utc)` index on `asset_observation`.** The pairing plan is GiST-driven
  with `device_id` as a post-filter (56 ms / 500 frames). Harmless at 2 devices; with many devices on
  one road the spatial candidate set grows linearly and is then discarded. Not proposed because it
  cannot be measured at this data size.
