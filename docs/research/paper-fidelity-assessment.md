---
updated: 2026-08-31
---

# How faithful is the server to the crowdsourcing paper?

A point-by-point comparison of the implementation against Sattar et al.,
*Probabilistic-based crowdsourcing technique for road surface anomaly classification*
(rev. Aug 2018), the paper the fusion and clustering layers claim to implement.

The paper is `docs/research/probabilistic-crowdsourcing-road-anomaly-2018-SHS.docx` — **untracked**,
because `.gitignore` excludes `*.docx`. Section and line references below are to the paper's own
numbering; quotes are verbatim from a text extraction of that file.

**Verdict in one line: the integration mathematics (§4.5) is a faithful reproduction; the evidence
model around it is inverted, and that is why the pipeline surfaces almost nothing.**

---

## 1. What the paper actually proposes

Three steps, from its own Discussion:

> "First, the road surface anomalies of each road segments are classified to different classes based
> on the severity level of anomalies sensed by vehicles. Second, **each new detected anomaly either
> combined with existing clusters composed of preceding detections in different times, or generated
> a new cluster.** These processes also considered the location uncertainty of each detected anomaly
> for clustering assignment purposes. Third, **the probability distribution of each clusters is
> updated whenever new road surface anomaly assigned to that cluster.**"

The unit of evidence is a **survey** — a pass over a road segment — and the paper is explicit that a
survey need not come from a different person:

> "...to classify and integrate detected road surface anomalies from **multiple users and/or multiple
> passes of any road segment**." (§4)

Its entire validation rests on that equivalence:

> "The process of data collection was **repeated in five different days** between March 21, 2018 and
> March 30, 2018 **to simulate the data collection model operated by different users**." (§5.1)

**One vehicle. One phone. Five days.** The paper never used two devices, never required them, and
the database it describes in §4.3 stores only "the probability distribution information and location
information" per anomaly — **no user id and no device id at all.**

Its answer to "how many passes are enough" is empirical, from §5.5:

> "In Cummer Avenue, the first round of survey was able to detect the road anomaly with overall
> accuracy barely over 65%. With one additional survey, the accuracy improved up to 77%. **The
> overall accuracy yielded better than 90% all the time after the Cummer Avenue was being surveyed
> for more than three times.**"

---

## 2. Faithful

Verified against the paper's own worked example (its Table 8, a six-member Cummer Avenue cluster).

| Paper | Implementation |
|---|---|
| RBF kernel `k(l,l') = exp(-γ‖l-l'‖²)` (Eq. 2) | `rbf_weights`, `app/fusion/spatiotemporal.py:105-123` |
| "γ ... calculated based on the **standard deviation of the computed time and geographic location distances of each cluster's member**" | `sigma = max(float(d.std()), sigma_floor)` — per cluster, per kernel, from the members' own spread |
| "the weigh values of time and location computed from Equation 2 **should be summed**" then normalised to sum 1 | `combine_weights` sums element-wise then divides by the total (`:126-150`) |
| `l'` holds "the centroid geographic location of each cluster and **the latest time recorded**" — Table 8's newest member has temporal distance exactly `0.000` | `_member_distances` uses `newest = max(cluster["member_ts"])`, not wall-clock (`service.py:1013`) |
| Integration only "for the corresponded road segment **with similar moving direction**" | `_split_by_direction` + the circular bearing check in `_FIND_EXISTING_SQL` |

Also correct, and worth defending: `spatiotemporal.py` states that with `prior_concentration = 0`
the whole thing collapses to a weighted mean of the member distributions, and that the fitted
Dirichlet concentration "must not be read as corroboration". That is right. **The paper contains no
law relating confidence to the number of observations** — its Figure 9 is a curve of *detection
rate*, not of confidence — so a reproduction should not invent one.

---

## 3. Divergent

Ordered by cost.

### 3.1 Passes are not counted at all — **RESOLVED 2026-08-31**

The paper's unit of evidence is the survey. The server has **no notion of a pass, sweep, survey,
trip, session or visit** — not on the wire, not in any table, not in any query. The only per-member
temporal facts available to the crowd layer are `ts_utc` and `received_at`.

In its place, `app/services/cluster_query_service.py:48` gates the public read path on:

```sql
AND distinct_devices >= $1        -- CLUSTER_MIN_DISTINCT_DEVICES, default 2
```

`distinct_devices` is `count(DISTINCT device_id)` (`service.py:738`) with **no temporal component**.
One device passing a defect on three separate days scores exactly the same as that device firing
three times in two seconds: `1`. The rule appears nowhere in the paper.

Measured consequence on `pothole_db`: **7 survey days on one device, 286 locations visited on 2+
days, 27 on 3+ — and 0 clusters visible on `GET /api/v1/potholes`.**

> **Fixed 2026-08-31.** A pass key is derived server-side from each device's full timeline —
> a contiguous run with no gap longer than `CLUSTER_PASS_GAP_MINUTES` (20), i.e. a drive.
> Gap-based rather than per-day so a drive crossing midnight stays one pass.
> `migrations/015` adds `asset_cluster.distinct_passes` and `member_span_s`, and the read
> path now admits a cluster on **either** floor:
>
> ```sql
> AND (distinct_devices >= $1 OR distinct_passes >= $8)
> ```
>
> Both are overridable per request (`min_devices`, `min_passes`), defaulting to config so
> the Android client's behaviour is byte-identical — pinned by a test.
>
> The Android app records the same notion as a `run` but does not upload it; when it does,
> the column can be sourced from the wire instead of derived.
>
> **This did not make anything appear.** No cluster on the collected data reaches two
> passes — see §4b/§4c. The machinery is correct and has nothing to count.

### 3.2 A quorum the paper does not have — **RESOLVED 2026-08-31**

`ST_ClusterDBSCAN(eps := 25, minpoints := 3)` requires three points within 25 m and **silently
discards** everything below that (`WHERE lbl IS NOT NULL`, `service.py:764`).

The paper has no minimum anywhere. A single detection creates a cluster and is reported:

> "if no cluster was queried from the database, the newly classified data event was considered as a
> new formed cluster and stored in the database." (§4.4)

It goes further and criticises exactly this class of rule, in the work it positions itself against:

> "The voting algorithm counts the number of reports made for each phenomenon by different sources.
> However, this simple algorithm ignored the fact that sources may have different degrees of
> trustworthiness ... this binary-based algorithm does not consider the temporal and probabilistic
> nature of detecting anomalies from smartphone sensors." (§3)

`minpoints := 3` is also not doing what it appears to. `CLUSTER_EPS_M` is 25 m and the measured
median speed is 13 m/s, so **25 m is 1.9 seconds of travel** — three detections "within 25 m of each
other" is one drive-past of one rough patch. Measured: every cluster on `pothole_db` spans a median
of **2.0 seconds**. The quorum has never once required corroboration.

> **Fixed 2026-08-31.** `CLUSTER_MIN_POINTS` is now **1** — a lone detection forms a
> cluster, as §4.4 says. On `pothole_db` that recovered the **87 of 191 admitted members
> (46%)** DBSCAN had been discarding as noise, taking clusters 23 → 90.
>
> Safe only because the read path now gates *publication* separately (§3.1). Relaxing the
> quorum moved where uncorroborated detections are filtered; it did not publish them, and a
> test pins that a lone detection stays off `GET /api/v1/potholes`.
>
> `min_points` survives as a floor on cluster **size** rather than a DBSCAN core minimum,
> so raising it still works and is still tested.

### 3.3 Fixed radius instead of per-event GPS uncertainty — **RESOLVED 2026-08-31**

The paper's radius is a property of each detection, not a constant:

> "Each newly classified data event was **buffered at the radius of 2σ** (based on the estimated
> accuracy for detected geographic location)." (§4.4)

with the confidence level chosen deliberately:

> "both absolute accuracy value estimated for each detected geographic location and bearing of
> moving direction has 68% confidence level (1 σ). However, in this study, **95% confidence level
> (2σ)** ... were considered."

The server uses a fixed `CLUSTER_EPS_M = 25.0`. `asset_observation.accuracy_m` **is populated on all
4,637 rows** (median 4.6 m, p95 9.8 m), so the paper's rule would give a median radius of ~9.2 m and
a p95 of ~19.6 m — the fixed 25 m is looser than the paper's own p95, i.e. it over-merges.

Recorded as deferred conflict C3 in `phase-2.2c-spatiotemporal-fusion.md` — though C3's
stated reason was that *bearing* accuracy is not uploaded, which is true and blocks the ±2σ
bearing gate but never covered the location buffer. `accuracy_m` was available all along.

> **Fixed 2026-08-31.** `ST_ClusterDBSCAN` was replaced with the paper's §4.3–4.4 assignment:
> sweeps replayed oldest-first, each sweep-event matched against cluster **centroids from
> prior sweeps only**, at a radius of `min(2 × accuracy_m, CLUSTER_EPS_M)`. `CLUSTER_EPS_M`
> is now a ceiling, not the radius; NULL accuracy falls back to it, so pre-GPS-quality rows
> are unaffected.
>
> Centroid matching also removes DBSCAN's transitive chaining, which was the larger half of
> the problem. Measured on `pothole_db`:
>
> | | before | after |
> |---|---|---|
> | clusters | 90 | 150 |
> | widest cluster | **124 m** | **19.9 m** |
> | clusters > 25 m across | 15 | **0** |
> | member pairs beyond their own 2σ | 204/258 (79%) | **1/51 (2%)** |
>
> All 191 members retained; severity still spreads across four tiers (15/75/31/29); the
> public read path still returns 0. As predicted it produced **no** corroboration — this was
> a grouping-quality fix, not an evidence fix.
>
> A within-sweep collapse precedes assignment: our phone re-triggers on one defect (every
> old cluster spanned a median of 2.0 s), where the paper's on-device k-means emits one
> anomaly per defect. Collapsing restores the assumption the paper's algorithm is built on
> and is what makes `distinct_passes` mean "sweeps that saw this defect".

### 3.4 The classifier features are not speed-normalised — divergent, but NOT the fix

> "Since **C(ratio) and V values have correlation with speed values, these values were normalized by
> dividing them to their respective speed values** to reduce the correlation. Then, the normalized
> values of C(ratio) and V are standardized (by calculating z-score values) before applying DPGMM."
> (§4.2)

`app/sensor_model/features.py::classifier_features` returns `[ratio, gbar]` and standardises them.
**There is no division by speed.**

> **Measured 2026-08-30, and the paper's rationale does not hold on this data.** The stated reason
> is that the ratios "have correlation with speed values". Here that correlation is **−0.058**, and
> dividing by speed makes it **−0.081** — slightly *worse*. So this divergence is real but
> reproducing it would buy nothing, and it is not the cause of the outlier gate's residual 31.7%
> pothole flagging either. **Do not spend a refit on it.** It is recorded as a divergence for
> completeness, not as a recommendation.

### 3.5 Fixed-k GMM, where the paper's contribution was to remove k — high

The paper adopted DPGMM *specifically* to replace the approach the server uses:

> "The Gaussian Mixture Model (GMM) classification approach utilized in the study conducted by
> Sattar et al. (2018) not only suffers from converging on some individual road segments, but also
> is a **supervised classification approach meaning that the numbers of classes should be predefined
> prior to any classification, which is impractical** in the application of road surface anomaly
> detection." (§3)

DPGMM is "an unsupervised, **nonparametric** Bayesian clustering model ... to infinite Gaussian
mixture models", fitted by Gibbs sampling, **per road segment per survey**. Its class count varied
1–4 across the paper's own surveys (Table 4).

The server fits a fixed `k=3` `GaussianMixture` with a BIC sweep, once, globally. Recorded as
deferred in `phase-2.2c-spatiotemporal-fusion.md`.

### 3.6 Batch re-clustering instead of incremental assignment — **partly resolved**

The paper assigns event by event, surveys applied in order:

> "first, collected road surface anomalies of March 21, 2018 was passed to this process, then
> collected anomalies data of March 23, 2018 passed for integration. This process continues until
> passing the last collected anomalies happened in March 30, 2018." (§5.3)

`run_cluster_job` used to re-run DBSCAN over the whole member set and overwrite. Cluster identity
survived only by centroid proximity + bearing (`_FIND_EXISTING_SQL`).

> **Half-fixed 2026-08-31.** The *assignment order* is now the paper's: sweeps are replayed
> oldest-first and each sweep-event matches against cluster centroids **as they stood before that
> sweep began**. A cluster created during a sweep is deliberately not a candidate for other events
> in the same sweep — without that snapshot a sweep silently merges into itself.
>
> What is still batch is the *execution*: every run replays the whole history rather than appending
> only new sweeps. That is a deliberate trade, not an oversight. It preserves the byte-identical
> re-run the job is built around, and it is what let every fix this week be validated by replaying
> the same data under a changed parameter. Truly incremental assignment would be cheaper at scale
> and match the paper's online framing, at the cost of never being able to apply a parameter change
> retroactively.

The remaining cost is unchanged: a member ageing past `CLUSTER_WINDOW_DAYS` silently *decrements* a
cluster's counts, so evidence is not monotonic. On a research database set
`CLUSTER_WINDOW_DAYS` high — the dev `.env` uses 3650 — or clusters quietly empty as collection
recedes.

### 3.7 One cluster per event — medium

> "the buffered area around the red points labelled with 'A' and 'B' encounter two formed clusters
> with similar moving directions. **Therefore, these newly detected events should be assigned to the
> both of the formed clusters** which they are encountered." (§4.4)

DBSCAN gives one label per point. Recorded as deferred conflict C1; `observation_cluster_link` is
already many-to-many, so the schema permits it.

### 3.8 Minor

- **Bearing gate** is a fixed 45°, where the paper uses ±2σ of the event's reported bearing accuracy.
  Android's bearing accuracy is not on the wire, so this one is blocked rather than skipped.
- **Centroid** is confidence-weighted (`SUM(lon*w)/SUM(w)`); the paper averages plainly: "the values
  of the geographic location and the bearing values of the clustered anomalies were **averaged**".

---

## 4. The gap in the paper itself

Worth stating because it shapes what should be built on top rather than reproduced.

**The paper has no publication step.** A cluster formed from a single detection is stored and
reported. That is defensible for an offline MATLAB study whose output is checked against field
inspection before anyone acts on it. It is not defensible for a public endpoint that tells drivers
where potholes are.

Its Figure 9 — 65% at one survey, >90% at three — *is* the missing statement about when a cluster
should be trusted. It simply never became part of the method.

So the right shape is: **cluster exactly as the paper does, and gate publication separately.** That
is additive rather than a distortion, because there was nothing there to distort.

### A caveat about accumulating evidence

The paper's temporal RBF **penalises** older passes. σ is derived from the spread of member ages, so
across three passes on three days the two-day-old member scores ≈0.003. Its own Table 8 makes this
concrete — the member detected nine days before the newest gets a temporal weight of **0.000**:

| Detection | Temporal distance (days) | Temporal weight | Overall weight |
|---|---|---|---|
| 21/03/2018 23:23:59 | 9.001 | 0.000 | 0.058 |
| 28/03/2018 22:41:10 | 2.030 | 0.575 | 0.241 |
| 30/03/2018 23:24:43 | 0.000 | 1.000 | 0.681 |

So in the paper, repeated surveys improve **whether a cluster exists**, not **how confident it is** —
which is exactly why there is no confidence-vs-N law to reproduce. Any corroboration count therefore
belongs in the visibility gate, and must not be folded into `integrate_cluster`.

---

## 4b. Measured 2026-08-30: no clustering parameter recovers corroboration

`scripts/crowd_sweep.py` (read-only) swept the geometry and accumulated the surveys.

**Parameter sweep** — every combination, `>= 2 passes` column:

| eps_m | min_points | clusters | members | noise | ≥2 passes | ≥2 devices | median span |
|---|---|---|---|---|---|---|---|
| 10 | 1 | 154 | 191 | 0 | **0** | 0 | 0.0 s |
| 15 | 1 | 129 | 191 | 0 | **0** | 0 | 0.0 s |
| 25 | 1 | 90 | 191 | 0 | **0** | 0 | 0.0 s |
| 25 | 3 *(configured)* | 23 | 104 | **87** | **0** | 0 | 3.0 s |
| 40 | 3 | 26 | 146 | 45 | **0** | 0 | 5.0 s |

Two results. First, `min_points = 3` discards **87 of 191 admitted members (46%)** as
DBSCAN noise; at the paper's `min_points = 1` nothing is discarded and the cluster count
rises 23 → 90. Second, and decisively: **no configuration produces a single cluster with two
passes.** Not one, at any radius, at any minimum. The median member span never exceeds 5
seconds.

**Survey accumulation** — clustering surveys 1..k:

| surveys | members | clusters | new | kept | lost | persistence | centroid drift | max passes |
|---|---|---|---|---|---|---|---|---|
| 1 | 19 | 2 | 2 | 0 | 0 | — | 0.0 m | 1 |
| 2 | 62 | 9 | 7 | 2 | 0 | 100% | 0.0 m | 1 |
| 3 | 101 | 15 | 6 | 9 | 0 | 100% | 0.0 m | 1 |
| 5 | 187 | 23 | 8 | 15 | 0 | 100% | 0.0 m | 1 |
| 7 | 191 | 23 | 0 | 23 | 0 | 100% | 0.0 m | 1 |

**100% persistence and 0.0 m drift at every step is not a good result — it is the
signature of the failure.** Clusters are purely additive: each survey discovers defects in
*new* places and never once joins an existing cluster. In the paper, accumulation *improves*
existing detections (65% → 90% → 100% accuracy, and it claims better locations). Here it
only ever adds. Nothing is corroborated, so nothing can improve.

This rules out the clustering geometry as the cause. It is upstream, in classification.

## 4c. Measured 2026-08-30: the classifier is not the cause either

`scripts/crowd_sweep.py --reclassify` re-ran classification three ways over the same
4,637 rows and counted how many pothole-classed observations have another pothole-classed
observation **from a different day** within 25 m.

| strategy | potholes | cross-day co-located |
|---|---|---|
| global GMM, shipped energy rule *(what is in the DB)* | 241 | **0** |
| global GMM, corrected energy rule | 241 | **0** |
| per-survey GMM, corrected energy rule | 190 | **0** |
| **day-matched null, 30 random draws** | 241 | **10-35, median 20** |

Two things follow.

**The paper's per-survey classification would not fix this.** That was the leading
hypothesis -- the paper classifies per road segment per survey (§5.2), making the class
assignment relative, while the server fits one GMM globally. Implementing it changes
nothing measurable here, so the schema change it would require is not justified.

**The zero is real, not an artefact of sparsity.** A bare zero would prove nothing: 241
points spread over 35 km might fail to co-locate purely by being sparse, and a class
concentrated on one day cannot co-locate across days at all. The null controls for both --
random subsets drawn with the *same per-day counts* from the *same days* co-locate 10-35
times. **0 of 30 draws reached the observed 0.** The pothole class is significantly
*anti*-co-located.

> ⚠️ **The paragraph above is wrong, and §4d retracts it.** The null it relies on is
> computed over a population that mixes **two different instrument regimes**. Once the
> regimes are separated the same test gives p ≈ 0.49 — an entirely ordinary zero. The
> observation (zero co-location) stands; the inference (significantly anti-co-located)
> does not. Left in place rather than deleted because the reasoning error is the
> instructive part: the null controlled for per-day counts and for sparsity, but not for
> the population being heterogeneous, and that is precisely what made it look decisive.

**Also settled:** the corrected energy rule selects the same 241 observations as the
shipped one, confirming that §3's `crack`/`not` swap does not affect which component is
`pothole`. That bug is contained to the two classes nothing downstream reads.

## 4d. Measured 2026-08-31: the swing is instrumentation, and §4c was confounded

Reproduce every number below with `scripts/session_regimes.py` (read-only):
`--regimes` for the session table, `--quarantine` for the retraction, `--power` for the
control.

Sessions derived from a 20-minute gap in each device's timeline give **12 sessions across
2 devices**. They separate cleanly on one derived quantity -- `gbar_in_max / accel_max_g`,
window energy over peak acceleration:

| band | sessions | median `gbar/g` | pothole rate |
|---|---|---|---|
| low | 9 | 1.75 – 3.48 | **0.0 – 4.7 %** |
| high | 3 | 9.91 – 19.05 | **20.4 – 24.2 %** |

Neither column overlaps, and nothing falls in the corridor between 3.48 and 9.91. That
single ratio orders the sessions by pothole rate essentially perfectly.

**The raw accelerometer statistics do not separate the bands.** `accel_max_g` medians run
1.64–3.40 and `accel_std` 0.45–0.74, and *both bands span that entire range*. Only the
derived window features are inflated -- `magnitude` 4.1x, `gbar_in_max` 5.0x. The peak
forces the phone measured are ordinary in both regimes; what differs is how much energy the
app attributed to the window around each peak. The classifier keys on exactly that
(`ratio`, `gbar`, `magnitude`), so it is largely reporting instrument state.

Two distinct causes, not one:

- **Sample rate.** `time_in_max` is quantised. All **4,539** observations from the main
  phone lie on a 0.033548 s grid -- 29.81 Hz, a 15-sample window. The second phone is
  **94.9 % off that grid**, on a grid 8x finer (0.0042 s ≈ 238 Hz). A window feature summed
  over 8x the samples is inflated for nothing.
- **Something else on 2026-08-25.** Sessions `4eb6:7` and `4eb6:8` are 100 % on the 29.81 Hz
  grid, same device, same peak-to-trough gap, ordinary `accel_max_g` -- yet `gbar/g` is 3.6x
  their own other sessions. Not a constant gain: the percentile curves *cross* at p10
  (1.12 vs 1.35) and diverge above, so it is a mixture with far more sustained-energy
  events. Mount or vehicle fits; the data cannot separate those.

### Quarantining the regimes retracts §4c's inference

Re-running the cross-day test within a single regime, with a null drawn from that regime
only (200 draws):

| population | obs | pothole | observed | null | draws ≤ observed |
|---|---|---|---|---|---|
| both regimes pooled *(this is §4c)* | 4,637 | 243 | 0 | 2–39, median 19 | **0 / 200** |
| low band only, 9 sessions | 4,055 | 106 | 0 | 0–8, median 2 | **98 / 200** |
| high band only, 3 sessions | 582 | 137 | 0 | 0–12, median 4 | 23 / 200 |

Pooled, zero looks impossible (p < 0.005). Within the low band the same zero has
**p ≈ 0.49** -- the null's own median is 2 and its range includes 0 nearly half the time.
The high band contributes 137 of the 243 detections from 2 days, and permutation is free to
scatter those across the pooled data; the real detections cannot cross regimes because the
regimes barely overlap in space or time. That inflated the null and manufactured the
significance.

### The test has power -- potholes are simply too sparse for it

The obvious worry is that nothing would co-locate here, making the whole method vacuous.
It does not hold. Same low band, same geometry, same day-matched null:

| class | n | observed | null | median | p(≥ obs) |
|---|---|---|---|---|---|
| pothole | 106 | 0 | 0–8 | 2 | 1.000 |
| `crack` | 2,860 | **746** | 654–742 | 698 | **0.000** |
| `not` | 1,089 | 175 | 119–212 | 159 | 0.165 |

The dense class co-locates **above the top of its own null**. The pipeline -- sensor, GPS,
25 m geometry, the null itself -- is demonstrably capable of detecting spatial
reproducibility across days. The pothole class at n=106 over 5 days is below the density
where this test can say anything at all.

> Caveat on reading the middle row as a claim about cracks: §3's `_energy_order` swap means
> the `crack` and `not` *labels* are exchanged, so the dense co-locating class cannot be
> named with confidence. The power result does not depend on which name is right.

### What this means for the crowd layer

Nothing in clustering, corroboration or classification can produce corroboration from
detections that never recur in the same place -- and the crowd layer is now correct. But
the reason there is nothing to work with has changed, and it is a much less alarming one:

**The dataset is underpowered for the corroboration question, and the detections are not
comparable across sessions.** Not "the detector is pathologically irreproducible". Zero
corroborated defects is what 106 pothole detections over 5 days *should* produce.

That reorders the next steps:

1. **Repeat-route collection is now the top item, not the cheapest one.** It is the only way
   to get statistical power: the same short loop, several passes, one unchanged instrument
   state. Nothing analysable exists until then.
2. **Session provenance is a confirmation, not a blocker.** The regime is already
   fingerprintable from fields on the wire today -- `time_in_max` grid spacing gives the
   sample rate, median `gbar/g` gives the energy-persistence regime. The upload
   ([never sent](./app-capture-findings.md), F5/F6) would explain *why* a session drifted;
   it is no longer needed to *see* that one did.
3. **A session-regime fingerprint belongs in the pipeline.** Pooling regimes is what
   produced a spurious result once already, and it would corrupt any cluster that mixed
   them.

## 5. Summary table

State as of **2026-08-31**, after the round.

| # | Paper | Server now | Status |
|---|---|---|---|
| 1 | Multiple users **and/or multiple passes** | Pass key derived per drive; `distinct_devices` **OR** `distinct_passes` gates publication | ✅ **Resolved** |
| 2 | One detection forms a cluster; no quorum | `CLUSTER_MIN_POINTS = 1`; publication gated separately | ✅ **Resolved** |
| 3 | Buffer = 2σ of the event's GPS accuracy | `min(2 × accuracy_m, CLUSTER_EPS_M)`, ceiling not radius | ✅ **Resolved** |
| 6 | Incremental, survey by survey | Sweeps replayed oldest-first against prior-sweep centroids; execution still batch by choice | ◐ **Half** |
| 10 | RBF, per-cluster γ, kernels summed, newest-member time base | Same | ✅ Faithful |
| 11 | Direction-aware cluster identity | Same | ✅ Faithful |
| 12 | No confidence-vs-N law | None invented | ✅ Faithful |
| 4 | Features divided by speed, then z-scored | z-scored only | ✗ **Won't fix** — measured, the rationale does not hold here (§3.4) |
| 5 | DPGMM, nonparametric | Fixed k=3 GMM + BIC | ✗ Deferred — and per-survey fitting was measured not to help (§4c) |
| 7 | An event may join several clusters | Nearest cluster only | ✗ Deferred (C1) |
| 8 | Bearing gate ±2σ | Fixed 45° | ✗ Blocked — bearing accuracy is not on the wire |
| 9 | Centroid = plain mean | Confidence-weighted mean | ✗ Minor |

**What went wrong originally, and it is worth naming.** None of it was concealed —
`phase-2.2c-spatiotemporal-fusion.md` recorded C1, C3 and the DPGMM deferral explicitly, and was
candid that "the paper's method does not accumulate evidence across users". What went unnoticed is
that **#1 and #2 together inverted the evidence model**: the paper counts surveys and the server
counted devices, so a single-vehicle survey campaign — which is exactly what the paper itself ran —
produced nothing. Each deferral was defensible alone; the interaction was not visible from any one
of them.

**And what fixing them did not do.** Every item above is now either faithful or a measured refusal,
and the pipeline still produces **zero** corroborated defects. That is not a gap in the
implementation — §4b and §4c establish that no clustering parameter, and no classification strategy,
recovers a single cross-day repeat. The evidence is not there to integrate.

**Why the evidence is not there — corrected 2026-08-31.** §4c read that zero as proof the detector
was anti-reproducible. §4d shows the null behind that reading pooled two instrument regimes; inside
one regime the same zero is unremarkable (p ≈ 0.49), and a denser class over the same roads and days
co-locates *above* the top of its null. So the honest verdict is **underpowered, not pathological**:
106 pothole detections over 5 days cannot corroborate, and would not be expected to. The next lever
is not a fix at all — it is collection designed for the question, several passes over one short loop
in one unchanged instrument state.
