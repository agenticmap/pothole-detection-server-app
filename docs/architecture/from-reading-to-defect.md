---
updated: 2026-09-03
---

# From a reading to a defect on the map

An operator looking at a popup sees `P(pothole) 0.998`, `Severity 0.634 · High`, and
`Rejected by the outlier gate`. Nothing in this repository explained where those numbers come
from, and the records that describe the clustering behind them are **wrong** — they still
describe an algorithm that was replaced. This follows one accelerometer reading end to end.

> **Read this before `phase-2.2-clustering-plan.md` or `migrations/003`.** Both describe
> `ST_ClusterDBSCAN` with `cluster_min_points = 3`. Neither is true any more.

---

## 1. What the phone sends

`asset_observation` carries four numbers per reading (`migrations/001`): `magnitude` (the peak
acceleration of the jolt), `accel_std` (the standard deviation of the surrounding window — the
road's background noise), `gbar_in_max` (the gravity-axis value at the peak), and `speed_mps`.
Orientation correction already happened on the phone, so the server consumes corrected values.

Two derived features do the actual work (`app/sensor_model/features.py`):

```
ratio = magnitude / accel_std      # how far the jolt stands out from this road's noise
gbar  = gbar_in_max
```

**`ratio` is the whole idea.** A pothole on a smooth road and the same pothole on a rough one
produce different absolute magnitudes but a similar ratio. Dividing by the local noise floor is
what makes the number comparable across roads.

## 2. P(pothole) — a Gaussian mixture, not a classifier trained on labels

`(ratio, gbar)` is standardised against the population mean and standard deviation, then handed to
a **3-component Gaussian mixture** fitted unsupervised over every fittable row
(`app/sensor_model/fit.py`). `P(pothole)` is the posterior mass of the pothole component at that
point (`app/sensor_model/score.py`).

**Nothing labels the components.** They are named by distance from the origin in standardised
space, lowest to highest: `not`, `crack`, `pothole`. So "pothole" means *the highest-energy
cluster in this dataset* — an inference from the shape of the data, never a human judgement. This
replaces the human cluster-naming that the original MATLAB `ClusterCalc` training files provided,
which the port does not have.

Three consequences worth stating plainly:

- **Speed is not an input.** A jolt at 9 km/h and the same jolt at 90 km/h get the same `P`.
- **It is not a calibrated probability of a real pothole.** Three well-separated full-covariance
  Gaussians saturate fast, so 0.998 means "comfortably inside that component", not "99.8% likely to
  be a real defect". Treat it as *which cluster*, not *how sure*.
- **It is refitted hourly** and only when there are ≥ 200 new fittable rows, so the meaning of a
  score drifts as the corpus grows. `sensor_model_version` on each row records which fit produced it.

## 3. Severity — a jolt normalised by speed

`app/sensor_model/features.py`:

```
severity = clamp(SEVERITY_SCALE * magnitude / max(speed_mps, SEVERITY_SPEED_REF), 0, 1)
```

with `SEVERITY_SCALE = 0.25` and `SEVERITY_SPEED_REF = 5.0` m/s. Dividing by speed is the point:
the same defect hit faster produces a bigger spike, so a large jolt at *low* speed implies a rougher
defect.

Worked example, from a live row:

```
magnitude 12.685 m/s², speed 2.62 m/s
0.25 × 12.685 / max(2.62, 5.0) = 0.25 × 12.685 / 5.0 = 0.6342     (stored: 0.6343)
```

**Note what happened to the speed.** 2.62 is below the 5 m/s floor, so the floor was used and the
speed contributed nothing. The floor is a divide-by-zero guard, not a filter, but on slow readings
it silently removes the normalisation the formula exists for.

`SEVERITY_SCALE` was **2.0** until the 2026-08-30 integration round, which saturates at
`magnitude/max(speed,5) ≥ 0.5` — below the p0 of the observed pothole distribution, so every
pothole scored exactly 1.0 and every cluster painted "Severe". 0.25 comes from `1/p95` of the
measured ratio on this corpus. **It is a fit to one city's data**; re-measure before trusting it
elsewhere.

**The tier labels live in a different file.** `dashboard/src/severity.ts` puts the floors at
0 / 0.25 / 0.5 / 0.75 for Low / Moderate / High / Severe, so 0.634 reads "High". That file's own
header warns about it: the scale and the tiers are **one calibration split across two files**, and
changing either alone silently shifts what "High" means.

## 4. The outlier gate — why a 0.998 reading gets rejected

A **separate** IsolationForest, fitted in the same job but on different features
(`app/sensor_model/features.py`):

```
OUTLIER_FEATURES = ("accel_std", "speed_mps")
```

It never sees `ratio`, `gbar`, `magnitude`, or the class. The outlier decision is computed
independently of the posteriors, so **`P(pothole)` has literally zero influence on it**. At
`contamination = 0.1` it isolates roughly the outer 10% of that two-dimensional distribution.

The example reading was rejected for being **slow**: 2.62 m/s against a fleet median of 12.38 m/s
(9 km/h against 45). An enormous jolt at walking pace is an unusual measurement condition, whatever
caused it.

**This class-blindness is deliberate, and it is the most important design decision in the project.**
The gate used to be fitted on `(ratio, gbar, magnitude, accel_std, speed_mps)` — the features on
which potholes separate from ordinary driving by 14–15×. An unsupervised anomaly detector trained on
those does the only thing it can: **it learns that potholes are the anomaly.** It flagged **285 of
286** pothole-classed observations, and since the cluster member gate is
`sensor_class = 'pothole' AND sensor_is_outlier IS NOT TRUE`, the entire sensor arm of the pipeline
contributed **one row**. Tuning `contamination` could not fix it — below 0.05 the gate flags nothing
*but* potholes, so the dial ran between "no gate" and "no potholes".

The fix is better, not neutral. Measured on the current corpus:

| class | readings | rejected | rate |
|---|---|---|---|
| pothole | 306 | 77 | **25.2%** |
| crack | 3,599 | 281 | 7.8% |
| not | 1,781 | 128 | 7.2% |

Potholes are still rejected at ~3× the base rate, because `accel_std` carries some pothole signal —
a rough defect raises the local noise floor. **A confident pothole being rejected is expected at
roughly one in four, and is a known, accepted recall cost.**

---

## 5. How a cluster is actually formed

### It is not DBSCAN any more

`ST_ClusterDBSCAN` was replaced by a two-stage assignment in Python
(`app/fusion/service.py::_assign_members`). Two reasons, both recorded in the code:

- DBSCAN takes **one scalar `eps`** for the whole window function, but the paper buffers each event
  by 2σ of *its own* reported GPS accuracy.
- DBSCAN **chains**: A joins B, B joins C, and A and C need never be within `eps` of each other. On
  the collected data that produced a "single pothole" spanning **124 m**.

The replacement collapses readings within one sweep, then matches each sweep-event against clusters
from *prior* sweeps only, event-to-centroid. Event-to-centroid matching cannot chain. Measured
effect: widest cluster 124 m → 19.9 m, clusters wider than 25 m 15 → 0.

### The radius is adaptive

`CLUSTER_EPS_M = 25` is a **ceiling**, not the radius. The radius is
`min(2 × accuracy_m, eps)` — the paper's own 2σ buffer. Measured 2σ on this corpus: p25 5.1 m,
median 6.8 m, p95 17.7 m. Camera frames have no accuracy column and take the 25 m fallback.

**So the working radius is about 6.9 m, and the 25 m in the config is misleading if read as the
grouping distance.** It is the single biggest reason 163 of 204 clusters have one member:

| | admitted readings with any neighbour (of 254) |
|---|---|
| effective radius (median 6.9 m) | **94** |
| the 25 m ceiling | 188 |

**And the buffer omits the dominant error term.** 56% of readings carry whole-second timestamps, and
at the median 12.38 m/s that is **±12.4 m of along-track uncertainty** — larger than the 8.7 m that
2σ of a 4.37 m accuracy reading allows. 5,686 readings share only 4,837 distinct positions, so GPS
fixes are reused across readings as well. A buffer of `2 × accuracy + speed × Δt` would take the
median radius to 19.5 m and the readings-with-a-neighbour count to 175.

**That change is not obviously right, and it would not fix corroboration.** Within one pass two
readings 14 m apart (the measured median gap) are two different rough spots, so a wider radius
would merge distinct defects. And across days, where a wider radius genuinely would help, there is
almost nothing to find — admitted readings having another admitted reading from a *different* day:

| radius | 7 m | 20 m | 25 m | 50 m | 100 m |
|---|---|---|---|---|---|
| readings | 2 | 5 | 5 | 11 | 17 |

The same defect is essentially never detected twice. **Repeatability is the binding constraint, not
the radius** — which is why the radius has been left alone and written down instead.

### One reading is enough to form a cluster

`cluster_min_points = 1`, not 3. At 25 m and a median 13 m/s, "three detections within 25 m" is
**1.9 seconds of travel** — one drive-past of one rough patch, not corroboration. Every cluster the
old value produced spanned a median of 2.0 s, and it discarded **46%** of admitted members as noise
that then appeared on no surface at all.

### What is admitted

A reading joins the member pool if it is inside the time window, is not covered by a repaired
cluster, **and** either:

- `sensor_class = 'pothole' AND sensor_is_outlier IS NOT TRUE`, **or**
- it won a camera-frame pairing at `fused_confidence ≥ 0.5`.

### What that means in practice, measured

Only pothole-classed readings are ever eligible, so the class is very nearly the whole story:

| class | readings | reach a cluster |
|---|---|---|
| crack | 3,599 | **0** |
| not | 1,781 | **0** |
| pothole, gate passed | 229 | **229 (100%)** |
| pothole, gate flagged | 77 | 25 — **all 25 via the frame-pairing path** |

**254 of 5,686 readings (4.5%) are members of any cluster.** Two things follow that are easy to get
wrong, and the console got both wrong until 2026-09-03:

- **The outlier flag does not decide membership.** 25 flagged readings are members, admitted by the
  second path below, while 4,971 readings the gate passed are members of nothing. Drawing a marker
  as "excluded" because it is flagged states something false.
- **A passed gate is not an admission ticket.** Class is the binding constraint.

Note the second is an **alternative path, not an extra requirement** — a crack-classed or
outlier-flagged reading with a confident frame pairing is admitted anyway. And note what is
*missing*: there is **no `sensor_p_pothole` floor anywhere**. The class is an argmax; a reading that
wins the pothole component by a nose is admitted on the same terms as one at 0.998.

---

## 6. Forming is not publishing

This is the distinction that makes the whole system legible, and it lives in two different places.

| | rule | where |
|---|---|---|
| **Form** a cluster | 1 admitted reading | write path, clustering job |
| **Publish** a cluster | `distinct_devices ≥ 2` **OR** `distinct_passes ≥ 3` | read path, `cluster_query_service` |

A **pass** is a contiguous run of one device's records with no gap longer than 20 minutes. Passes
exist because the paper's own validation was *one phone driven on five different days* — counting
only devices scores a single-vehicle survey campaign at zero, which is precisely the campaign this
project has run.

**The surfaces disagree, deliberately.** The tiles, `/clusters/stats` and `/clusters/{id}` apply
**no** corroboration floor — they are triage surfaces, and an operator needs to see candidates.
`/api/v1/potholes` applies both. That is why the dashboard can show N while the mobile app shows
none, and it is why the console now carries a **"Corroborated"** KPI computed from the same
predicate the public path uses.

### Where the corpus actually stands

| | |
|---|---|
| clusters | 204 |
| **publishable** | **0** |
| single-reading clusters | 163 |
| max distinct passes / devices | 1 / 1 |
| max member span | 4.0 s |

Every cluster is one drive-past. And the sharper measurement: **only 5 of 229 admitted pothole
readings (2.2%) have another admitted pothole reading from a different day within 25 m.** Widening
the radius barely helps — 4.8% at 50 m, 7.4% at 100 m, 17.9% at 200 m — so this is not a
GPS-precision problem.

The 31 Aug – 1 Sep drive *did* deliver repeat coverage: 51% of its frames sit within 25 m of
previously-covered road. Detections still did not recur at the same spots. So the bottleneck is
**detection repeatability**, not route coverage — a wheel has to strike the same defect on the same
line, and it usually does not. `docs/research/corroboration-coverage-analysis.md` established
coverage as the cause with the 91.9% figure; this narrows it further.

---

## 7. Cautions for anyone changing this

- **`CLUSTER_PASS_GAP_MINUTES` is not only a counting knob.** `pass_key` also partitions stage 1 of
  the assignment and snapshots cluster visibility in stage 2, so changing 20 minutes changes *which
  clusters exist*. Sweeping it is not the read-only experiment it looks like.
- **`SEVERITY_SCALE` and the dashboard tier floors are one calibration in two files.**
- **Changing `SENSOR_OUTLIER_FEATURES` or `SEVERITY_SCALE` forces a refit *and* a re-score**
  (`UPDATE asset_observation SET scored_at = NULL`). Until then every existing `sensor_is_outlier`
  and `sensor_severity` is a stale value from the previous calibration.
- **Lowering the publication floors will not help this corpus.** Max passes across all 204 clusters
  is 1, so even a floor of 2 publishes nothing. The unlock is a fixed loop driven repeatedly, which
  is a driving task.

## Where the numbers live

| what | file |
|---|---|
| `ratio`, `gbar`, severity | `app/sensor_model/features.py` |
| the mixture fit, component naming | `app/sensor_model/fit.py` |
| posteriors, outlier decision | `app/sensor_model/score.py` |
| admission, assignment, cluster rows | `app/fusion/service.py` |
| publication floors | `app/services/cluster_query_service.py` |
| tier labels and colours | `dashboard/src/severity.ts` |
| the popup an operator reads | `dashboard/src/map/map.ts` |
